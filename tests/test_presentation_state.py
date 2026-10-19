"""Presentation commits use real temporary Config files and mocked rendering/IO."""
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from config import Config
from refresh_task import RefreshTask
from tests.test_location_navigation import (
    location_config, location_worker, settings, wait_until, W1, W2, W3, M1, M2)


def disk(cfg):
    return json.loads(Path(cfg.config_file).read_text())


def idle(task):
    wait_until(lambda: task.screen_selection._active is None
               and not task.screen_selection.has_pending)


@pytest.mark.parametrize('button,target', [('C', W2), ('B', M1)])
@pytest.mark.parametrize('phase', ['generation', 'display'])
def test_selection_commits_only_after_presentation(location_config, location_worker, button, target, phase):
    task, plugin, press = location_worker
    press('A', W1)
    before = disk(location_config)
    entered, release = threading.Event(), threading.Event()
    generate = plugin.generate_image.side_effect
    def blocked_generate(data, cfg):
        if phase == 'generation':
            entered.set()
            assert release.wait(3)
        return generate(data, cfg)
    def display(*args, **kwargs):
        assert disk(location_config)['button_navigation']['display_uncertain'] is True
        if phase == 'display':
            entered.set()
            assert release.wait(3)
    plugin.generate_image.side_effect = blocked_generate
    task.display_manager.display_image.side_effect = display
    try:
        assert task.request_button(button)
        assert entered.wait(3)
        during = disk(location_config)
        assert during['button_navigation']['selected'] == W1
        assert during['button_navigation']['remembered'] == {'weather': W1}
        assert during['refresh_info'] == before['refresh_info']
        assert during['button_navigation']['display_uncertain'] is (phase == 'display')
        # Neither worker/controller lock is held over rendering or physical IO.
        assert task.condition.acquire(timeout=0.1)
        task.condition.release()
        assert task.screen_selection._lock.acquire(timeout=0.1)
        task.screen_selection._lock.release()
    finally:
        release.set()
    idle(task)
    saved = disk(location_config)
    assert saved['button_navigation']['selected'] == target
    assert saved['button_navigation']['remembered'][target['plugin_id']] == target
    assert saved['button_navigation']['display_uncertain'] is False
    assert saved['refresh_info']['plugin_instance'] == target['instance']
    assert task.display_manager.display_image.call_count == 2


@pytest.mark.parametrize('button,target,displaced', [
    ('C', W2, False), ('B', M1, False), ('A', W1, True),
    ('C', W2, True), ('B', M1, True)])
def test_generation_exception_preserves_committed_state_and_allows_deliberate_retry(
        location_config, location_worker, monkeypatch, button, target, displaced):
    task, plugin, press = location_worker
    press('A', W1)
    location_config.update_section('button_navigation', lambda n: n.update(displaced=displaced))
    before = Path(location_config.config_file).read_bytes()
    failed = threading.Event()
    monkeypatch.setattr('refresh_task.logger.exception', lambda *a: failed.set())
    plugin.generate_image.side_effect = RuntimeError('mock provider failure')
    assert task.request_button(button)
    assert failed.wait(3)
    idle(task)
    assert Path(location_config.config_file).read_bytes() == before
    assert settings(location_config)['selected'] == W1
    assert settings(location_config)['remembered'] == {'weather': W1}
    assert task.display_manager.display_image.call_count == 1
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=5)) is None
    task.stop()
    assert task.request_button(button)
    result = task.screen_selection.consume_pending()
    assert result[1].name == target['instance']
    assert result[2] is True
    assert settings(location_config)['selected'] == W1  # Consuming still does not commit.


@pytest.mark.parametrize('label', ['CACHED', 'STALE', 'UNAVAILABLE'])
def test_metar_status_page_is_a_successful_presentation(location_config, location_worker, label):
    task, plugin, press = location_worker
    press('A', W1)
    # The METAR plugin returns a Pillow status page even without usable report data.
    # Its fetching/cache status semantics are covered separately in test_metar.py.
    image = Image.new('RGB', (8, 8), 'white')
    image.info['status'] = label
    plugin.generate_image.side_effect = lambda *args: image
    press('B', M1)
    assert disk(location_config)['button_navigation']['selected'] == M1
    assert disk(location_config)['button_navigation']['remembered'] == {'weather': W1, 'metar': M1}
    assert task.display_manager.display_image.call_count == 2


def test_marker_write_failure_aborts_before_display(location_config, location_worker, monkeypatch):
    task, plugin, press = location_worker
    press('A', W1)
    before = Path(location_config.config_file).read_bytes()
    failed = threading.Event()
    monkeypatch.setattr('refresh_task.logger.exception', lambda *a: failed.set())
    def fail(*args):
        raise OSError('mock pre-display storage failure')
    monkeypatch.setattr('config.os.replace', fail)
    assert task.request_button('C')
    assert failed.wait(3)
    idle(task)
    assert Path(location_config.config_file).read_bytes() == before
    assert task.display_manager.display_image.call_count == 1
    assert settings(location_config)['display_uncertain'] is False


@pytest.mark.parametrize('failure', ['replace', 'directory_fsync'])
def test_completion_persistence_failure_preserves_uncertainty_and_models(
        location_config, location_worker, monkeypatch, failure):
    import os
    import stat
    task, plugin, press = location_worker
    press('A', W1)
    before = disk(location_config)
    failed = threading.Event()
    monkeypatch.setattr('refresh_task.logger.exception', lambda *a: failed.set())
    replace = os.replace
    fsync = os.fsync
    calls = []
    def failing_replace(*args):
        calls.append('replace')
        if failure == 'replace' and calls.count('replace') == 2:
            raise OSError('mock completion rename failure')
        return replace(*args)
    def failing_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            calls.append('directory')
            if failure == 'directory_fsync' and calls.count('directory') == 2:
                raise OSError('mock post-rename directory sync failure')
        return fsync(fd)
    monkeypatch.setattr('config.os.replace', failing_replace)
    monkeypatch.setattr('config.os.fsync', failing_fsync)
    assert task.request_button('C')
    assert failed.wait(3)
    idle(task)
    saved = disk(location_config)
    assert saved['button_navigation']['selected'] == W1
    assert saved['button_navigation']['remembered'] == {'weather': W1}
    assert saved['button_navigation']['display_uncertain'] is True
    assert saved['refresh_info'] == before['refresh_info']
    assert saved['playlist_config'] == before['playlist_config']
    assert location_config.get_config('refresh_info') == before['refresh_info']
    assert location_config.refresh_info.to_dict() == before['refresh_info']
    assert location_config.get_playlist_manager().to_dict() == before['playlist_config']
    assert task.display_manager.display_image.call_count == 2
    task.stop()
    restarted = RefreshTask(Config(), Mock())
    assert restarted.screen_selection.display_uncertain
    assert not restarted.screen_selection.has_pending
    assert restarted._scheduled_action(datetime.now(timezone.utc) + timedelta(days=3)) is None
    assert restarted.request_button('A')
    assert restarted._scheduled_action(datetime.now(timezone.utc)).restore_selected


@pytest.mark.parametrize('uncertain', [False, True])
def test_identical_image_selection_and_deliberate_uncertain_restoration(
        location_config, location_worker, uncertain):
    task, plugin, press = location_worker
    press('A', W1)
    if uncertain:
        location_config.update_section('button_navigation', lambda n: n.update(display_uncertain=True))
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=3)) is None
    plugin.generate_image.side_effect = lambda *args: Image.new('RGB', (8, 8), 'red')
    press('C', W2)
    assert task.display_manager.display_image.call_count == (2 if uncertain else 1)
    assert settings(location_config)['selected'] == W2
    assert settings(location_config)['remembered'] == {'weather': W2}
    assert settings(location_config)['display_uncertain'] is False


@pytest.mark.parametrize('phase', ['generation', 'display_success', 'display_failure'])
def test_shutdown_discards_pending_and_only_commits_known_success(
        location_config, location_worker, monkeypatch, phase):
    task, plugin, press = location_worker
    press('A', W1)
    before = disk(location_config)
    entered, release = threading.Event(), threading.Event()
    generate = plugin.generate_image.side_effect
    def blocked_generate(data, cfg):
        if phase == 'generation':
            entered.set()
            assert release.wait(3)
        return generate(data, cfg)
    def display(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        if phase == 'display_failure':
            raise RuntimeError('mock interrupted display')
    plugin.generate_image.side_effect = blocked_generate
    task.display_manager.display_image.side_effect = display
    assert task.request_button('C')
    assert entered.wait(3)
    assert task.request_button('C')  # Newer W3 intent must not replay after shutdown.
    stopper = threading.Thread(target=task.stop)
    stopper.start()
    try:
        wait_until(lambda: not task.running)
    finally:
        release.set()
        stopper.join(3)
    assert not stopper.is_alive()
    assert not task.screen_selection.has_pending
    saved = disk(location_config)
    if phase == 'display_success':
        assert saved['button_navigation']['selected'] == W2
        assert saved['button_navigation']['remembered'] == {'weather': W2}
        assert saved['button_navigation']['display_uncertain'] is False
    else:
        assert saved['button_navigation']['selected'] == W1
        assert saved['button_navigation']['remembered'] == {'weather': W1}
        assert saved['refresh_info'] == before['refresh_info']
        assert saved['button_navigation']['display_uncertain'] is (phase == 'display_failure')
    assert task.display_manager.display_image.call_count == (1 if phase == 'generation' else 2)
    restarted = RefreshTask(Config(), Mock())
    assert not restarted.screen_selection.has_pending
    assert restarted._scheduled_action(datetime.now(timezone.utc) + timedelta(days=3)) is None


def test_abrupt_interruption_keeps_durable_marker_without_replaying_intent(location_config):
    from tests.test_location_navigation import consume
    controller = RefreshTask(location_config, Mock()).screen_selection
    consume(controller, 'A')
    task = RefreshTask(location_config, Mock())
    task.running = True
    # Execute directly so simulated process interruption does not kill a test thread.
    assert task.request_button('C')
    def abort(*args, **kwargs):
        assert disk(location_config)['button_navigation']['display_uncertain'] is True
        raise SystemExit('simulated process crash during hardware IO')
    task.display_manager.display_image.side_effect = abort
    from unittest.mock import patch
    with patch('refresh_task.get_plugin_instance', return_value=Mock(
            config={}, generate_image=Mock(return_value=Image.new('RGB', (8, 8), 'blue')))):
        with pytest.raises(SystemExit):
            task._run()
    restored = Config()
    assert restored.get_config('button_navigation')['selected'] == W1
    assert restored.get_config('button_navigation')['remembered'] == {'weather': W1}
    assert restored.get_config('button_navigation')['display_uncertain'] is True
    restarted = RefreshTask(restored, Mock())
    assert restarted._scheduled_action(datetime.now(timezone.utc)) is None
    assert restarted.request_button('A')


@pytest.mark.parametrize('fail', [False, True])
def test_latest_pending_return_to_committed_reference_is_not_lost(
        location_config, location_worker, monkeypatch, fail):
    task, plugin, press = location_worker
    press('A', W1)
    entered, release, failed = threading.Event(), threading.Event(), threading.Event()
    original = plugin.generate_image.side_effect
    names = []
    def generate(data, cfg):
        names.append(data['name'])
        if data['name'] == W2['instance']:
            entered.set()
            assert release.wait(3)
            if fail:
                raise RuntimeError('mock provider failure')
        return original(data, cfg)
    monkeypatch.setattr('refresh_task.logger.exception', lambda *a: failed.set())
    plugin.generate_image.side_effect = generate
    try:
        assert task.request_button('C')
        assert entered.wait(3)
        assert task.request_button('C')  # W3 pending.
        assert task.request_button('C')  # W1 replaces W3, while W1 is still committed.
    finally:
        release.set()
    if fail:
        assert failed.wait(3)
    idle(task)
    assert names == ([W2['instance']] if fail else [W2['instance'], W1['instance']])
    assert settings(location_config)['selected'] == W1
    assert settings(location_config)['remembered'] == {'weather': W1}
    assert task.display_manager.display_image.call_count == (1 if fail else 3)


def test_wrap_to_active_target_cancels_pending_without_retry_on_failure(
        location_config, location_worker, monkeypatch):
    task, plugin, press = location_worker
    press('A', W1)
    entered, release, failed = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr('refresh_task.logger.exception', lambda *a: failed.set())
    def generate(*args):
        entered.set()
        assert release.wait(3)
        raise RuntimeError('mock provider failure')
    plugin.generate_image.side_effect = generate
    try:
        assert task.request_button('C')  # W2 in flight.
        assert entered.wait(3)
        for _ in range(3):
            assert task.request_button('C')  # W3 -> W1 -> W2 cancels pending work.
        assert not task.screen_selection.has_pending
    finally:
        release.set()
    assert failed.wait(3)
    idle(task)
    assert plugin.generate_image.call_count == 2  # Initial W1 and exactly one failed W2.
    assert settings(location_config)['selected'] == W1
    assert settings(location_config)['remembered'] == {'weather': W1}
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=3)) is None


def test_newer_c_request_during_completion_survives_older_commit(
        location_config, location_worker, monkeypatch):
    task, plugin, press = location_worker
    press('A', W1)
    entered, release = threading.Event(), threading.Event()
    commit = location_config.commit_refresh
    def blocked_commit(info, *args, **kwargs):
        if info.plugin_instance == W2['instance']:
            entered.set()
            assert release.wait(3)
        return commit(info, *args, **kwargs)
    monkeypatch.setattr(location_config, 'commit_refresh', blocked_commit)
    try:
        assert task.request_button('C')
        assert entered.wait(3)  # W2 physically completed; persisted selection is still W1.
        assert settings(location_config)['selected'] == W1
        assert task.request_button('C')  # Must advance from in-flight W2 to W3.
    finally:
        release.set()
    wait_until(lambda: location_config.refresh_info.plugin_instance == W3['instance']
               and task.screen_selection._active is None)
    assert settings(location_config)['selected'] == W3
    assert settings(location_config)['remembered'] == {'weather': W3}
    assert task.display_manager.display_image.call_count == 3


def test_failed_web_display_allows_same_hash_button_restoration(location_config, location_worker):
    from refresh_task import ManualRefresh
    task, plugin, press = location_worker
    press('A', W1)
    task.display_manager.display_image.side_effect = RuntimeError('mock web display failure')
    with pytest.raises(RuntimeError):
        task.manual_update(ManualRefresh('metar', {'name': 'Comic'}))
    assert settings(location_config)['selected'] == W1
    assert settings(location_config)['displaced'] is False
    assert settings(location_config)['display_uncertain'] is True
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=3)) is None
    task.display_manager.display_image.side_effect = None
    press('A', W1)  # Same image hash as last successful Weather, but still must draw.
    assert task.display_manager.display_image.call_count == 3
    assert settings(location_config)['display_uncertain'] is False
    assert settings(location_config)['displaced'] is False
    assert not task.request_button('A')


def test_dedup_commit_failure_keeps_known_display_and_previous_selection(
        location_config, location_worker, monkeypatch):
    task, plugin, press = location_worker
    press('A', W1)
    before = Path(location_config.config_file).read_bytes()
    failed = threading.Event()
    plugin.generate_image.side_effect = lambda *args: Image.new('RGB', (8, 8), 'red')
    def fail(*args):
        raise OSError('mock dedup commit failure')
    monkeypatch.setattr('config.os.replace', fail)
    monkeypatch.setattr('refresh_task.logger.exception', lambda *a: failed.set())
    assert task.request_button('C')
    assert failed.wait(3)
    idle(task)
    assert Path(location_config.config_file).read_bytes() == before
    assert settings(location_config)['selected'] == W1
    assert settings(location_config)['display_uncertain'] is False
    assert task.display_manager.display_image.call_count == 1


def test_payload_and_directory_synced_before_display_and_after_commit(
        location_config, location_worker, monkeypatch):
    import os
    import stat
    task, plugin, press = location_worker
    press('A', W1)
    events = []
    fsync = os.fsync
    def sync(fd):
        events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'payload')
        return fsync(fd)
    def display(*args, **kwargs):
        events.append('display')
        assert events == ['payload', 'directory', 'display']
        saved = Config().get_config('button_navigation')
        assert saved['display_uncertain'] is True and saved['selected'] == W1
    monkeypatch.setattr('config.os.fsync', sync)
    task.display_manager.display_image.side_effect = display
    press('C', W2)
    assert events == ['payload', 'directory', 'display', 'payload', 'directory']
    assert Config().get_config('button_navigation')['display_uncertain'] is False


def test_button_selection_matching_active_web_target_is_not_suppressed(
        location_config, location_worker):
    from refresh_task import PlaylistRefresh
    task, plugin, press = location_worker
    press('A', W1)
    entered, release = threading.Event(), threading.Event()
    generate = plugin.generate_image.side_effect
    calls = []
    def blocked_generate(data, cfg):
        calls.append(data['name'])
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
        return generate(data, cfg)
    plugin.generate_image.side_effect = blocked_generate
    playlist, instance = task.screen_selection.resolve(W2)
    errors = []
    def web():
        try:
            task.manual_update(PlaylistRefresh(playlist, instance, force=True))
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=web)
    thread.start()
    try:
        assert entered.wait(3)
        assert settings(location_config)['selected'] == W1
        assert task.request_button('C')  # Web target W2 is not a button selection.
        assert not task.request_button('A')  # Already heading to W2 within Weather.
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    wait_until(lambda: settings(location_config)['selected'] == W2
               and task.screen_selection._active is None)
    assert settings(location_config)['remembered'] == {'weather': W2}
    assert settings(location_config)['displaced'] is False
    assert calls == [W2['instance'], W2['instance']]
    # Web already presented the same image, so the button commit deduplicates IO.
    assert task.display_manager.display_image.call_count == 2


def test_disabled_mode_still_tracks_physical_uncertainty_without_selecting(
        location_config, location_worker):
    from refresh_task import ManualRefresh
    task, _, press = location_worker
    press('A', W1)
    location_config.update_section('button_navigation', lambda n: n.update(enabled=False))
    task.display_manager.display_image.side_effect = RuntimeError('mock disabled-mode display failure')
    with pytest.raises(RuntimeError):
        task.manual_update(ManualRefresh('metar', {'name': 'Comic'}))
    saved = disk(location_config)['button_navigation']
    assert saved['selected'] == W1 and saved['remembered'] == {'weather': W1}
    assert saved['display_uncertain'] is True
    assert not task.request_button('C')
    now = datetime.now(timezone.utc)
    assert task._scheduled_action(now) is None  # Uncertainty alone schedules nothing.
    assert task._scheduled_action(now + timedelta(hours=2)) is not None  # Ordinary playlists.
