import json
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from config import Config
from model import Playlist, PlaylistManager, PluginInstance, RefreshInfo
from refresh_task import RefreshTask, ManualRefresh, PlaylistRefresh
from screen_selection import ScreenSelectionController
from PIL import Image
from utils.image_utils import compute_image_hash


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, 'Worker did not reach expected state'
        threading.Event().wait(0.005)


@pytest.fixture
def displacement_worker(config, monkeypatch):
    config.get_config('button_navigation')['manual_only'] = True
    colors = {'A': 'red', 'B': 'green', 'C': 'blue', 'D': 'white', 'comic': 'blue'}
    plugin = Mock(config={})
    plugin.generate_image.side_effect = lambda settings, cfg: Image.new('RGB', (8, 8), colors[settings['name']])
    monkeypatch.setattr('refresh_task.get_plugin_instance', lambda _: plugin)
    task = RefreshTask(config, Mock())
    task.start()
    def button(name):
        assert task.request_button(name)
        wanted = compute_image_hash(Image.new('RGB', (8, 8), colors[name]))
        wait_until(lambda: config.refresh_info.image_hash == wanted and
                   config.refresh_info.plugin_instance == name and task.screen_selection._active is None)
    try:
        yield task, plugin, button
    finally:
        task.stop()


def test_web_displacement_restores_same_weather_image_once(config, displacement_worker):
    task, plugin, button = displacement_worker
    button('A')
    weather_hash = config.refresh_info.image_hash
    assert not task.request_button('A')
    assert task.display_manager.display_image.call_count == 1
    task.manual_update(ManualRefresh('test', {'name': 'comic'}))
    assert task.screen_selection.displaced
    assert Config().get_config('button_navigation')['selected'] == ref('A')
    button('A')
    assert config.refresh_info.image_hash == weather_hash
    assert not task.screen_selection.displaced
    assert task.display_manager.display_image.call_count == 3
    assert not task.request_button('A')
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=2)) is None


@pytest.mark.parametrize('failure', ['generation', 'display'])
def test_failed_web_update_does_not_displace(config, displacement_worker, failure):
    task, plugin, button = displacement_worker
    button('A')
    if failure == 'generation':
        plugin.generate_image.side_effect = RuntimeError('web generation failed')
    else:
        task.display_manager.display_image.side_effect = RuntimeError('web display failed')
    with pytest.raises(RuntimeError):
        task.manual_update(ManualRefresh('test', {'name': 'comic'}))
    assert not task.screen_selection.displaced
    assert task.screen_selection.display_uncertain is (failure == 'display')
    task.stop()
    assert task.request_button('A') is (failure == 'display')
    assert config.refresh_info.plugin_instance == 'A'


def test_identical_web_image_does_not_displace(config, displacement_worker):
    task, plugin, button = displacement_worker
    button('A')
    task.manual_update(ManualRefresh('test', {'name': 'A'}))
    assert task.display_manager.display_image.call_count == 1
    assert not task.screen_selection.displaced
    assert not task.request_button('A')


def test_restoration_bypasses_stale_hash_and_suppresses_busy_duplicates(config, displacement_worker):
    task, plugin, button = displacement_worker
    button('A')
    task.manual_update(ManualRefresh('test', {'name': 'comic'}))
    # Even if a cached/current metadata hash equals old Weather, explicit restore draws.
    config.refresh_info.image_hash = compute_image_hash(Image.new('RGB', (8, 8), 'red'))
    entered, release = threading.Event(), threading.Event()
    def display(*args, **kwargs):
        entered.set()
        assert release.wait(3)
    task.display_manager.display_image.side_effect = display
    try:
        assert task.request_button('A')
        assert entered.wait(3)
        for _ in range(5):
            assert not task.request_button('A')
        assert task.screen_selection.displaced
    finally:
        release.set()
    wait_until(lambda: task.screen_selection._active is None)
    assert not task.screen_selection.displaced
    assert task.display_manager.display_image.call_count == 3


def test_restoration_failure_persists_displacement_without_retry(config, displacement_worker, monkeypatch):
    task, plugin, button = displacement_worker
    button('A')
    task.manual_update(ManualRefresh('test', {'name': 'comic'}))
    failed = threading.Event()
    monkeypatch.setattr('refresh_task.logger.exception', lambda *args: failed.set())
    task.display_manager.display_image.side_effect = RuntimeError('restore failed')
    assert task.request_button('A')
    assert failed.wait(3)
    wait_until(lambda: task.screen_selection._active is None)
    assert Config().get_config('button_navigation')['displaced'] is True
    assert Config().get_config('button_navigation')['selected'] == ref('A')
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=2)) is None
    assert task.display_manager.display_image.call_count == 3
    assert config.refresh_info.refresh_type == 'Manual Update'


@pytest.mark.parametrize('fail', [False, True])
def test_button_during_web_update_is_resolved_after_web_completion(config, displacement_worker, fail):
    task, plugin, button = displacement_worker
    button('A')
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = plugin.generate_image.side_effect
    def generate(settings, cfg):
        if settings['name'] == 'comic':
            entered.set()
            assert release.wait(3)
            if fail:
                raise RuntimeError('web failed')
        return original(settings, cfg)
    plugin.generate_image.side_effect = generate
    def web():
        try:
            task.manual_update(ManualRefresh('test', {'name': 'comic'}))
        except RuntimeError as exc:
            errors.append(exc)
    thread = threading.Thread(target=web)
    thread.start()
    try:
        assert entered.wait(3)
        assert not task.screen_selection.displaced  # No premature displacement.
        assert task.request_button('A')
        assert not task.request_button('A')
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    wait_until(lambda: not task.screen_selection.has_pending and
               task.screen_selection._consuming is None and task.screen_selection._active is None)
    assert bool(errors) is fail
    assert task.display_manager.display_image.call_count == (1 if fail else 3)
    assert config.refresh_info.plugin_instance == 'A'
    assert not task.screen_selection.displaced


def test_displacement_survives_restart_without_automatic_refresh(config, displacement_worker):
    task, plugin, button = displacement_worker
    button('A')
    task.manual_update(ManualRefresh('test', {'name': 'comic'}))
    task.stop()
    cfg = Config()
    restarted = RefreshTask(cfg, Mock())
    assert restarted.screen_selection.displaced
    assert restarted._scheduled_action(datetime.now(timezone.utc)) is None
    assert restarted.request_button('A')
    action = restarted._scheduled_action(datetime.now(timezone.utc))
    assert action.restore_selected and action.plugin_instance.name == 'A'
    assert cfg.get_config('button_navigation')['selected'] == ref('A')


def test_disabled_web_updates_do_not_mutate_button_state(config, displacement_worker):
    task, plugin, button = displacement_worker
    button('A')
    config.get_config('button_navigation')['enabled'] = False
    before = dict(config.get_config('button_navigation'))
    task.manual_update(ManualRefresh('test', {'name': 'comic'}))
    assert config.get_config('button_navigation') == before
    assert not task.request_button('A')
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=2)) is not None


def test_saved_instance_web_update_marks_displacement_and_can_restore(config, displacement_worker):
    task, plugin, button = displacement_worker
    button('A')
    playlist = config.playlist_manager.get_playlist('Default')
    task.manual_update(PlaylistRefresh(playlist, playlist.find_plugin('test', 'B'), force=True))
    assert task.screen_selection.displaced
    assert config.get_config('button_navigation')['selected'] == ref('A')
    # Rendering the selected saved instance through the web also restores it.
    task.manual_update(PlaylistRefresh(playlist, playlist.find_plugin('test', 'A'), force=True))
    assert not task.screen_selection.displaced
    assert not task.request_button('A')
    assert task.display_manager.display_image.call_count == 3


def test_displaced_pending_requests_remain_latest_wins(config, displacement_worker):
    task, plugin, button = displacement_worker
    button('A')
    task.manual_update(ManualRefresh('test', {'name': 'comic'}))
    entered, release = threading.Event(), threading.Event()
    original = plugin.generate_image.side_effect
    generated = []
    def generate(settings, cfg):
        generated.append(settings['name'])
        if settings['name'] == 'A':
            entered.set()
            assert release.wait(3)
        return original(settings, cfg)
    plugin.generate_image.side_effect = generate
    try:
        assert task.request_button('A')
        assert entered.wait(3)
        assert task.request_button('B')
        assert task.request_button('C')
        assert task.request_button('D')
    finally:
        release.set()
    wait_until(lambda: config.refresh_info.plugin_instance == 'D' and task.screen_selection._active is None)
    assert generated == ['A', 'D']
    assert not task.screen_selection.displaced
    assert Config().get_config('button_navigation')['selected'] == ref('D')


def test_failed_displacement_persistence_keeps_previous_settings(config, monkeypatch):
    config.get_config('button_navigation')['selected'] = ref('A')
    config.write_config()
    controller = ScreenSelectionController(config, Mock())
    original = open(config.config_file).read()
    def fail(*args):
        raise OSError('mock disk failure')
    monkeypatch.setattr('config.os.replace', fail)
    with pytest.raises(OSError):
        controller.record_display_success(None, web=True, image_changed=True)
    assert not controller.displaced
    assert open(config.config_file).read() == original


def ref(name):
    return dict(playlist='Default', plugin_id='test', instance=name)


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / 'device.json'
    instances = [PluginInstance('test', n, {'name': n}, {'interval': 3600}) for n in 'ABCD']
    manager = PlaylistManager([Playlist('Default', '00:00', '24:00', [i.to_dict() for i in instances])])
    path.write_text(json.dumps({'unrelated': {'keep': True}, 'playlist_config': manager.to_dict(),
                                'refresh_info': {}, 'plugin_cycle_interval_seconds': 3600,
                                'button_navigation': {'enabled': True, 'assignments': {n: ref(n) for n in 'ABCD'}}}))
    monkeypatch.setattr(Config, 'config_file', str(path))
    monkeypatch.setattr(Config, 'read_plugins_list', lambda self: [{'id': 'test'}])
    cfg = Config()
    cfg.plugin_image_dir = str(tmp_path)
    return cfg


def consume_presented(controller):
    result = controller.consume_pending()
    if result and result[2]:
        playlist, instance = result[:2]
        controller.record_display_success(
            dict(playlist=playlist.name, plugin_id=instance.plugin_id, instance=instance.name),
            web=False, image_changed=True, selection=True)
    controller.end_refresh()
    return result


def present_action(task, action):
    instance = action.plugin_instance
    task.screen_selection.record_display_success(
        dict(playlist=action.playlist.name, plugin_id=instance.plugin_id, instance=instance.name),
        web=False, image_changed=True, selection=getattr(action, 'button_selection', False))
    task.screen_selection.end_refresh()
    return action


def test_persistence_and_unrelated_values(config):
    wake = Mock()
    controller = ScreenSelectionController(config, wake)
    assert controller.request_button('A')
    assert 'selected' not in config.get_config('button_navigation')
    assert consume_presented(controller)[1].name == 'A'
    assert Config().get_config('button_navigation')['selected'] == ref('A')
    assert config.get_config('unrelated') == {'keep': True}
    assert not controller.request_button('A')
    wake.assert_called_once()


def test_latest_pending_wins(config):
    controller = ScreenSelectionController(config, Mock())
    for button in 'ABCD':
        assert controller.request_button(button)
    assert consume_presented(controller)[1].name == 'D'
    assert consume_presented(controller) is None


@pytest.mark.parametrize('reference', [None, {}, ref('missing'), 'A'])
def test_invalid_selection(config, reference):
    controller = ScreenSelectionController(config, Mock())
    assert not controller.request_selection(reference)
    assert consume_presented(controller) is None


def test_deleted_pending_and_selected(config):
    controller = ScreenSelectionController(config, Mock())
    controller.request_button('A')
    config.get_playlist_manager().get_playlist('Default').plugins.clear()
    assert consume_presented(controller) is None
    config.get_config('button_navigation')['selected'] = ref('A')
    assert controller.selected() is None


def test_assignment(config):
    controller = ScreenSelectionController(config, Mock())
    controller.assign_button('A', ref('B'))
    assert controller.request_button('A')
    assert consume_presented(controller)[1].name == 'B'
    controller.assign_button('A', None)
    assert not controller.request_button('A')
    with pytest.raises(ValueError):
        controller.assign_button('X', ref('B'))
    with pytest.raises(ValueError):
        controller.assign_button('A', ref('missing'))


@pytest.mark.parametrize('mode', [False, None, 'true'])
def test_disabled_preserves_playlist(config, mode):
    config.update_value('button_navigation', {'enabled': mode})
    task = RefreshTask(config, Mock())
    assert not task.request_button('A')
    action = task._scheduled_action(datetime.now(timezone.utc))
    assert action.plugin_instance.name == 'A'
    assert 'selected' not in config.get_config('button_navigation')


def test_unsupported_selected_plugin_does_not_enable_automatic_scheduling(config):
    task = RefreshTask(config, Mock())
    task.request_button('B')
    action = task._scheduled_action(datetime.now(timezone.utc))
    assert action.plugin_instance.name == 'B' and action.force
    now = datetime.now(timezone.utc)
    action.plugin_instance.latest_refresh_time = now.isoformat()
    present_action(task, action)
    assert task._scheduled_action(now) is None
    assert task._scheduled_action(now + timedelta(seconds=3600)) is None
    assert config.get_playlist_manager().get_playlist('Default').current_plugin_index is None


def test_legacy_daily_schedule_does_not_bypass_group_scheduler(config):
    instance = config.get_playlist_manager().get_playlist('Default').plugins[0]
    instance.refresh = {'scheduled': '12:00'}
    instance.latest_refresh_time = '2026-10-10T09:00:00+00:00'
    task = RefreshTask(config, Mock())
    task.request_button('A')
    present_action(task, task._scheduled_action(datetime.now(timezone.utc)))
    assert task._scheduled_action(datetime(2026, 10, 10, 12, tzinfo=timezone.utc)) is None


@pytest.mark.parametrize('manual_only', [False, True])
@pytest.mark.parametrize('fail', [False, True])
def test_busy_worker_latest_wins_and_recovers(config, monkeypatch, fail, manual_only):
    config.get_config('button_navigation')['manual_only'] = manual_only
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    names = []
    active = []
    def generate(settings, cfg):
        names.append(settings['name'])
        return Image.new('RGB', (8, 8), {'A': 'red', 'B': 'green', 'C': 'blue', 'D': 'white'}[settings['name']])
    plugin = Mock(config={})
    plugin.generate_image.side_effect = generate
    monkeypatch.setattr('refresh_task.get_plugin_instance', lambda _: plugin)
    def display(image, **kwargs):
        assert not active
        active.append(True)
        try:
            if len(names) == 1:
                entered.set()
                assert release.wait(3)
                if fail:
                    raise RuntimeError('mock SPI failure')
            else:
                finished.set()
        finally:
            active.clear()
    task = RefreshTask(config, Mock(display_image=display))
    task.start()
    try:
        task.request_button('A')
        assert entered.wait(3)
        assert task.request_button('B')
        assert task.request_button('C')
        assert task.request_button('D')
        release.set()
        assert finished.wait(3)
    finally:
        release.set()
        task.stop()
    assert names == ['A', 'D']
    assert task.thread.is_alive() is False
    assert config.get_config('button_navigation')['selected'] == ref('D')
    if fail:
        assert config.get_playlist_manager().get_playlist('Default').plugins[0].latest_refresh_time is None


@pytest.mark.parametrize('value', [None, False, 'true', 1])
def test_manual_only_defaults_to_false(config, value):
    if value is not None:
        config.get_config('button_navigation')['manual_only'] = value
    task = RefreshTask(config, Mock())
    assert not task.screen_selection.manual_only
    assert task.request_button('A')
    present_action(task, task._scheduled_action(datetime.now(timezone.utc)))
    # The pause is off, but this generic plugin has no supported location group.
    assert task._scheduled_action(datetime.now(timezone.utc)) is None


@pytest.mark.parametrize('failure', [None, 'generation', 'display'])
def test_manual_only_worker_attempts_once_even_on_failure(config, monkeypatch, failure):
    config.get_config('button_navigation')['manual_only'] = True
    completed, checked = threading.Event(), threading.Event()
    plugin = Mock(config={})
    plugin.generate_image.return_value = Image.new('RGB', (8, 8), 'red')
    display = Mock()
    if failure == 'generation':
        plugin.generate_image.side_effect = RuntimeError('mock generation failure')
    elif failure == 'display':
        display.display_image.side_effect = RuntimeError('mock display failure')
    monkeypatch.setattr('refresh_task.get_plugin_instance', lambda _: plugin)
    monkeypatch.setattr('refresh_task.logger.exception', lambda *args: completed.set())
    write = config.write_config
    def record_write():
        write()
        if config.refresh_info.refresh_time:
            completed.set()
    monkeypatch.setattr(config, 'write_config', record_write)
    task = RefreshTask(config, display)
    schedule = task._scheduled_action
    # Advance beyond the instance's configured interval without waiting an hour.
    monkeypatch.setattr(task, '_get_current_datetime',
                        lambda: datetime.now(timezone.utc) + timedelta(days=2))
    def observe_schedule(now):
        action = schedule(now)
        if action is None:
            checked.set()
        return action
    monkeypatch.setattr(task, '_scheduled_action', observe_schedule)
    task.start()
    try:
        assert task.request_button('A')
        assert completed.wait(3)
        wait_until(lambda: task.screen_selection._active is None)
        task.signal_config_change()
        assert checked.wait(3)
    finally:
        task.stop()
    assert plugin.generate_image.call_count == 1
    assert display.display_image.call_count == (0 if failure == 'generation' else 1)
    saved = Config().get_config('button_navigation')
    assert saved.get('selected') == (ref('A') if failure is None else None)
    assert task.request_button('A') is (failure is not None)
    assert not task.thread.is_alive()


def test_manual_only_switch_persistence_and_restart(config):
    config.get_config('button_navigation')['manual_only'] = True
    task = RefreshTask(config, Mock())
    now = datetime.now(timezone.utc)
    assert task._scheduled_action(now) is None  # No playlist fallback.
    assert task.request_button('A')
    assert present_action(task, task._scheduled_action(now)).plugin_instance.name == 'A'
    assert not task.request_button('A')
    assert task._scheduled_action(now + timedelta(days=2)) is None
    restarted = RefreshTask(Config(), Mock())
    assert restarted.screen_selection.selected()[1].name == 'A'
    assert restarted._scheduled_action(now + timedelta(days=2)) is None
    assert not restarted.request_button('A')
    assert restarted.request_button('B')
    assert present_action(restarted, restarted._scheduled_action(now)).plugin_instance.name == 'B'
    assert restarted._scheduled_action(now + timedelta(days=2)) is None
    assert Config().get_config('button_navigation')['selected'] == ref('B')


def test_manual_only_disabled_mode_still_uses_playlists(config):
    config.get_config('button_navigation').update(enabled=False, manual_only=True)
    task = RefreshTask(config, Mock())
    assert not task.request_button('A')
    assert task._scheduled_action(datetime.now(timezone.utc)).plugin_instance.name == 'A'


def test_manual_only_toggle_does_not_enqueue_or_force_refresh(config):
    task = RefreshTask(config, Mock())
    now = datetime.now(timezone.utc)
    assert task.request_button('A')
    action = task._scheduled_action(now)
    action.plugin_instance.latest_refresh_time = now.isoformat()
    present_action(task, action)
    for enabled in (True, False, True):
        config.update_section('button_navigation', lambda settings: settings.update(manual_only=enabled))
        task.signal_config_change()
        assert not task.screen_selection.has_pending
        assert task._scheduled_action(now) is None
        assert config.get_config('button_navigation')['selected'] == ref('A')
    config.get_config('button_navigation')['manual_only'] = False
    # Removing the pause does not invent a supported group for this generic plugin.
    assert task._scheduled_action(now + timedelta(hours=1)) is None


def test_disable_discards_pending(config):
    task = RefreshTask(config, Mock())
    task.request_button('D')
    config.get_config('button_navigation')['enabled'] = False
    assert task._scheduled_action(datetime.now(timezone.utc)).plugin_instance.name == 'A'
    assert not task.screen_selection.has_pending


def test_manual_failure_reaches_caller_and_worker_survives(config, monkeypatch):
    plugin = Mock(config={})
    plugin.generate_image.side_effect = ValueError('generation failed')
    monkeypatch.setattr('refresh_task.get_plugin_instance', lambda _: plugin)
    task = RefreshTask(config, Mock())
    task.start()
    try:
        with pytest.raises(ValueError, match='generation failed'):
            task.manual_update(ManualRefresh('test', {}))
        assert task.thread.is_alive()
        task.display_manager.display_image.assert_not_called()
    finally:
        task.stop()


def test_default_mode_is_disabled(config):
    config.config.pop('button_navigation')
    assert not ScreenSelectionController(config, Mock()).enabled


def test_return_to_previous_selection_during_commit(config, monkeypatch):
    controller = ScreenSelectionController(config, Mock())
    controller.request_button('A')
    consume_presented(controller)
    original = config.update_section
    def committing(key, edit):
        # B has left the mailbox but A is still the persisted selection.
        assert controller.request_button('A')
        original(key, edit)
    controller.request_button('B')
    monkeypatch.setattr(config, 'update_section', committing)
    assert consume_presented(controller)[1].name == 'B'
    monkeypatch.setattr(config, 'update_section', original)
    assert consume_presented(controller)[1].name == 'A'


def test_atomic_write_failure_preserves_file_and_selection(config, monkeypatch):
    import os
    from pathlib import Path
    path = Path(config.config_file)
    before = path.read_bytes()
    controller = ScreenSelectionController(config, Mock())
    controller.request_button('A')
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        consume_presented(controller)
    assert path.read_bytes() == before
    assert 'selected' not in config.get_config('button_navigation')
    assert not list(path.parent.glob('.device-*'))


def test_restart_restores_without_forced_refresh(config):
    task = RefreshTask(config, Mock())
    task.request_button('B')
    now = datetime.now(timezone.utc)
    action = task._scheduled_action(now)
    action.plugin_instance.latest_refresh_time = now.isoformat()
    present_action(task, action)
    config.write_config()
    restarted = RefreshTask(Config(), Mock())
    assert restarted.screen_selection.selected()[1].name == 'B'
    assert restarted._scheduled_action(now) is None
    assert restarted._scheduled_action(now + timedelta(seconds=3600)) is None


def test_concurrent_assignments_preserve_all_updates(config):
    controller = ScreenSelectionController(config, Mock())
    barrier = threading.Barrier(4)
    errors = []
    def assign(button):
        try:
            barrier.wait(timeout=3)
            controller.assign_button(button, ref('D'))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=assign, args=(button,)) for button in 'ABCD']
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert not errors
    assert Config().get_config('button_navigation')['assignments'] == {b: ref('D') for b in 'ABCD'}


def test_refresh_timestamp_not_persisted_while_display_busy(config, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    plugin = Mock(config={})
    plugin.generate_image.return_value = Image.new('RGB', (8, 8), 'red')
    monkeypatch.setattr('refresh_task.get_plugin_instance', lambda _: plugin)
    def display(*args, **kwargs):
        entered.set()
        assert release.wait(3)
    task = RefreshTask(config, Mock(display_image=display))
    task.start()
    try:
        task.request_button('A')
        assert entered.wait(3)
        task.screen_selection.assign_button('B', ref('C'))
        saved = Config().get_playlist_manager().get_playlist('Default').plugins[0]
        assert saved.latest_refresh_time is None
    finally:
        release.set()
        task.stop()
    assert Config().get_playlist_manager().get_playlist('Default').plugins[0].latest_refresh_time is not None


@pytest.mark.parametrize('schedule', [{'interval': 0}, {'interval': -1}, {'interval': None}, {'scheduled': 'bad'}])
def test_invalid_schedule_is_safe(config, schedule):
    instance = config.get_playlist_manager().get_playlist('Default').plugins[0]
    instance.latest_refresh_time = datetime.now(timezone.utc).isoformat()
    instance.refresh = schedule
    task = RefreshTask(config, Mock())
    task.request_button('A')
    present_action(task, task._scheduled_action(datetime.now(timezone.utc)))
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=1)) is None


def test_parallel_start_creates_one_worker(config):
    task = RefreshTask(config, Mock())
    barrier = threading.Barrier(8)
    def start():
        barrier.wait(timeout=3)
        task.start()
    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    try:
        assert task.thread.is_alive()
        assert sum(t is task.thread for t in threading.enumerate()) == 1
    finally:
        task.stop()


@pytest.mark.parametrize('cancel', [False, True])
def test_screenshot_timeout_or_shutdown_never_reaches_display(config, monkeypatch, tmp_path, cancel):
    import subprocess
    from utils import image_utils as screenshots
    config.get_config('button_navigation')['manual_only'] = True
    entered = threading.Event()
    terminated = threading.Event()
    process = Mock(pid=987654, returncode=0)
    def communicate(timeout):
        entered.set()
        if terminated.wait(min(timeout, 0.005)):
            return b'', b''
        raise subprocess.TimeoutExpired('fake Chromium', timeout)
    process.communicate.side_effect = communicate
    monkeypatch.setattr(screenshots, '_find_chromium_binary', lambda: '/fake/chromium')
    monkeypatch.setattr(screenshots.subprocess, 'Popen', Mock(return_value=process))
    monkeypatch.setattr(screenshots, '_signal_screenshot_group', lambda *args: terminated.set())
    monkeypatch.setattr(screenshots, '_screenshot_group_exists', lambda _: False)
    monkeypatch.setattr(screenshots.tempfile, 'tempdir', str(tmp_path))
    plugin = Mock(config={})
    def generate(*args):
        image = screenshots.take_screenshot('mock target', (8, 8),
                                           process_timeout_seconds=10 if cancel else 0.02)
        if image is None:
            raise RuntimeError('Failed to take screenshot, please check logs.')
        return image
    plugin.generate_image.side_effect = generate
    monkeypatch.setattr('refresh_task.get_plugin_instance', lambda _: plugin)
    failed = threading.Event()
    monkeypatch.setattr('refresh_task.logger.exception', lambda *args: failed.set())
    task = RefreshTask(config, Mock())
    original_info = config.refresh_info
    task.start()
    try:
        assert task.request_button('A')
        assert entered.wait(3)
        # The condition is available throughout Chromium execution.
        assert task.condition.acquire(timeout=0.1)
        task.condition.release()
        if cancel:
            task.stop()
        assert failed.wait(3)
        assert terminated.is_set()
        task.display_manager.display_image.assert_not_called()
        assert config.refresh_info is original_info
        assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=2)) is None
        assert plugin.generate_image.call_count == 1
        assert not list(tmp_path.glob('inkypi-screenshot-*'))
    finally:
        task.stop()
