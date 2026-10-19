"""Location navigation uses saved instances, fake rendering, and temporary config."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from config import Config
from model import Playlist, PlaylistManager, PluginInstance
from refresh_task import ManualRefresh, PlaylistRefresh, RefreshTask
from screen_selection import ScreenSelectionController


def reference(plugin, name):
    return dict(playlist='Default', plugin_id=plugin, instance=name)


W1, W2, W3 = [reference('weather', name) for name in ('Weather One', 'Weather Two', 'Weather Three')]
M1, M2 = [reference('metar', name) for name in ('Airport One', 'Airport Two')]


@pytest.fixture
def location_config(tmp_path, monkeypatch):
    instances = [PluginInstance(ref['plugin_id'], ref['instance'], {'name': ref['instance']},
                                {'interval': 900}) for ref in (W1, W2, W3, M1, M2)]
    manager = PlaylistManager([Playlist('Default', '00:00', '24:00', [i.to_dict() for i in instances])])
    path = tmp_path / 'device.json'
    path.write_text(json.dumps({
        'unrelated': {'preserve': True}, 'playlist_config': manager.to_dict(), 'refresh_info': {},
        'plugin_cycle_interval_seconds': 3600,
        'button_navigation': {
            'enabled': True, 'manual_only': True, 'selected': None, 'displaced': False,
            'assignments': {'A': W1, 'B': M1}, 'actions': {'C': 'cycle_active_plugin'},
            'groups': {'weather': {'instances': [W1, W2, W3]}, 'metar': {'instances': [M1, M2]}}
        }
    }))
    monkeypatch.setattr(Config, 'config_file', str(path))
    monkeypatch.setattr(Config, 'read_plugins_list', lambda self: [{'id': 'weather'}, {'id': 'metar'}])
    cfg = Config()
    cfg.plugin_image_dir = str(tmp_path)
    return cfg


def settings(cfg):
    return cfg.get_config('button_navigation')


def consume(controller, button):
    assert controller.request_button(button)
    result = controller.consume_pending()
    present(controller, result)
    return result


def present(controller, result):
    """Simulate a successful presentation in controller-only tests."""
    if result and result[2]:
        playlist, instance = result[:2]
        controller.record_display_success(
            reference(instance.plugin_id, instance.name), web=False,
            image_changed=True, selection=True)
    controller.end_refresh()


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, 'Worker did not complete expected action'
        threading.Event().wait(0.005)


def test_cycle_wrap_and_separate_remembered_locations(location_config):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    assert consume(controller, 'A')[1].name == W1['instance']
    assert consume(controller, 'C')[1].name == W2['instance']
    assert consume(controller, 'B')[1].name == M1['instance']
    assert consume(controller, 'C')[1].name == M2['instance']
    assert consume(controller, 'A')[1].name == W2['instance']
    assert consume(controller, 'C')[1].name == W3['instance']
    assert consume(controller, 'C')[1].name == W1['instance']
    assert consume(controller, 'B')[1].name == M2['instance']
    assert settings(cfg)['remembered'] == {'weather': W1, 'metar': M2}
    assert cfg.get_config('unrelated') == {'preserve': True}
    assert settings(cfg)['assignments'] == {'A': W1, 'B': M1}


def test_group_order_and_references_survive_reordering(location_config):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    consume(controller, 'A')
    consume(controller, 'C')
    consume(controller, 'B')
    settings(cfg)['groups']['weather']['instances'] = [W3, W1, W2]
    assert consume(controller, 'A')[1].name == W2['instance']
    assert consume(controller, 'C')[1].name == W3['instance']


def test_callback_does_not_write_config_or_mutate_plugin_instances(location_config):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    before = Path(cfg.config_file).read_bytes()
    playlist_before = cfg.get_playlist_manager().to_dict()
    assert controller.request_button('A')
    assert controller.request_button('C')  # Uses pending Weather One as its context.
    assert Path(cfg.config_file).read_bytes() == before
    assert settings(cfg)['selected'] is None and 'remembered' not in settings(cfg)
    assert cfg.get_playlist_manager().to_dict() == playlist_before
    assert controller.consume_pending()[1].name == W2['instance']
    assert cfg.get_playlist_manager().to_dict() == playlist_before


@pytest.mark.parametrize('plugin,button,initial', [('weather', 'A', W1), ('metar', 'B', M1)])
@pytest.mark.parametrize('instances', [[], [W1], [M1], [None, {}, 'bad']])
def test_empty_or_single_valid_group_is_noop(location_config, plugin, button, initial, instances):
    cfg = location_config
    settings(cfg)['groups'][plugin]['instances'] = instances
    controller = ScreenSelectionController(cfg, Mock())
    consume(controller, button)
    assert not controller.request_button('C')
    assert not controller.has_pending
    assert settings(cfg)['selected'] == initial


def test_no_selected_context_and_unsupported_context_are_safe(location_config):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    assert not controller.request_button('C')
    settings(cfg)['selected'] = reference('comic', 'Comic')
    assert not controller.request_button('C')


def test_invalid_duplicate_cross_plugin_and_deleted_members_are_skipped(location_config):
    cfg = location_config
    settings(cfg)['groups']['weather']['instances'] = [
        None, {}, 'bad', W1, dict(W1, ignored='extra'), M1,
        reference('weather', 'Missing'), dict(W2, playlist='Missing'), W2, W3
    ]
    controller = ScreenSelectionController(cfg, Mock())
    consume(controller, 'A')
    playlist = cfg.get_playlist_manager().get_playlist('Default')
    playlist.plugins = [i for i in playlist.plugins if i.name != W2['instance']]
    assert consume(controller, 'C')[1].name == W3['instance']
    assert consume(controller, 'C')[1].name == W1['instance']
    cfg.get_plugin('weather')['disabled'] = True
    assert not controller.request_button('C')
    assert not controller.request_button('A')


@pytest.mark.parametrize('groups', [None, [], 'bad', {'weather': []},
                                    {'weather': {'instances': None}}, {'weather': {'instances': {}}}])
def test_malformed_or_absent_groups_preserve_assignments(location_config, groups):
    cfg = location_config
    settings(cfg)['groups'] = groups
    settings(cfg)['remembered'] = {'weather': W2}
    controller = ScreenSelectionController(cfg, Mock())
    assert consume(controller, 'A')[1].name == W1['instance']
    assert not controller.request_button('C')


@pytest.mark.parametrize('actions', [None, [], {'C': 'unknown'}, {}])
def test_no_cycle_action_retains_explicit_c_assignment(location_config, actions):
    cfg = location_config
    settings(cfg)['actions'] = actions
    settings(cfg)['assignments']['C'] = M2
    controller = ScreenSelectionController(cfg, Mock())
    assert consume(controller, 'C')[1].name == M2['instance']


def test_cycle_action_takes_precedence_over_c_assignment(location_config):
    cfg = location_config
    settings(cfg)['assignments']['C'] = M2
    controller = ScreenSelectionController(cfg, Mock())
    consume(controller, 'A')
    assert consume(controller, 'C')[1].name == W2['instance']


@pytest.mark.parametrize('remembered', [None, [], {'weather': 'bad'}, {'weather': M1},
                                        {'weather': reference('weather', 'Missing')}])
def test_invalid_remembered_reference_falls_back_without_losing_other_state(location_config, remembered):
    cfg = location_config
    settings(cfg)['remembered'] = remembered
    controller = ScreenSelectionController(cfg, Mock())
    assert consume(controller, 'A')[1].name == W1['instance']
    assert settings(cfg)['remembered']['weather'] == W1


def test_removed_assignment_falls_back_to_group_but_explicit_unassignment_is_respected(location_config):
    cfg = location_config
    settings(cfg)['assignments']['A'] = reference('weather', 'Missing')
    controller = ScreenSelectionController(cfg, Mock())
    assert consume(controller, 'A')[1].name == W1['instance']
    settings(cfg)['assignments']['A'] = None
    assert not controller.request_button('A')
    assert not controller.request_button('D')


def test_current_selection_initializes_memory_without_migration(location_config):
    cfg = location_config
    settings(cfg)['selected'] = W2
    controller = ScreenSelectionController(cfg, Mock())
    assert not controller.request_button('A')  # Do not jump back to the default.
    assert consume(controller, 'C')[1].name == W3['instance']
    assert settings(cfg)['remembered']['weather'] == W3


def test_cycle_from_removed_or_unlisted_selection_starts_at_first_valid_member(location_config):
    cfg = location_config
    settings(cfg)['selected'] = reference('weather', 'Removed')
    controller = ScreenSelectionController(cfg, Mock())
    assert consume(controller, 'C')[1].name == W1['instance']
    settings(cfg)['groups']['weather']['instances'] = [W2, W3]
    assert consume(controller, 'C')[1].name == W2['instance']


def test_deleted_pending_selection_does_not_change_persisted_memory(location_config):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    consume(controller, 'A')
    assert controller.request_button('C')
    playlist = cfg.get_playlist_manager().get_playlist('Default')
    playlist.plugins = [i for i in playlist.plugins if i.name != W2['instance']]
    assert controller.consume_pending() is None
    assert settings(cfg)['selected'] == W1
    assert settings(cfg)['remembered'] == {'weather': W1}
    assert consume(controller, 'C')[1].name == W3['instance']


def test_c_uses_consuming_target_before_persistence(location_config, monkeypatch):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    consume(controller, 'A')
    assert controller.request_button('C')
    original = cfg.update_section
    def committing(key, edit):
        assert settings(cfg)['selected'] == W1
        assert controller.request_button('C')  # W2 is consuming, so advance to W3.
        original(key, edit)
    monkeypatch.setattr(cfg, 'update_section', committing)
    result = controller.consume_pending()
    assert result[1].name == W2['instance']
    present(controller, result)
    monkeypatch.setattr(cfg, 'update_section', original)
    assert controller.consume_pending()[1].name == W3['instance']


def test_simultaneous_c_requests_advance_without_losing_steps(location_config):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    consume(controller, 'A')
    barrier = threading.Barrier(8)
    accepted = []
    def cycle():
        barrier.wait(3)
        accepted.append(controller.request_button('C'))
    threads = [threading.Thread(target=cycle) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert accepted == [True] * 8
    assert controller.consume_pending()[1].name == W3['instance']  # Eight steps modulo three.


def test_selection_and_memory_rollback_together_on_persistence_failure(location_config, monkeypatch):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    consume(controller, 'A')
    before = Path(cfg.config_file).read_bytes()
    assert controller.request_button('C')
    def fail(*args):
        raise OSError('mock disk failure')
    monkeypatch.setattr('config.os.replace', fail)
    result = controller.consume_pending()
    with pytest.raises(OSError):
        present(controller, result)
    assert Path(cfg.config_file).read_bytes() == before
    assert settings(cfg)['selected'] == W1
    assert settings(cfg)['remembered'] == {'weather': W1}


def test_restart_restores_separate_locations_without_rendering(location_config):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    for button in 'ACBC':
        consume(controller, button)
    restarted = RefreshTask(Config(), Mock())
    assert restarted.screen_selection.selected()[1].name == M2['instance']
    assert restarted._scheduled_action(datetime.now(timezone.utc) + timedelta(days=5)) is None
    assert restarted.request_button('A')
    action = restarted._scheduled_action(datetime.now(timezone.utc))
    assert action.plugin_instance.name == W2['instance']
    assert settings(restarted.device_config)['manual_only'] is True


@pytest.mark.parametrize('enabled', [False, None, 'true'])
def test_disabled_mode_preserves_playlists_and_does_not_rewrite_memory(location_config, enabled):
    cfg = location_config
    settings(cfg).update(enabled=enabled, remembered={'weather': W2})
    task = RefreshTask(cfg, Mock())
    for button in 'ABC':
        assert not task.request_button(button)
    assert task._scheduled_action(datetime.now(timezone.utc)).plugin_instance.name == W1['instance']
    assert settings(cfg)['remembered'] == {'weather': W2}


@pytest.fixture
def location_worker(location_config, monkeypatch):
    cfg = location_config
    colors = dict(zip([r['instance'] for r in (W1, W2, W3, M1, M2)],
                      ('red', 'green', 'blue', 'white', 'yellow')))
    plugin = Mock(config={})
    plugin.generate_image.side_effect = lambda data, cfg: Image.new('RGB', (8, 8), colors.get(data['name'], 'black'))
    monkeypatch.setattr('refresh_task.get_plugin_instance', lambda _: plugin)
    task = RefreshTask(cfg, Mock())
    task.start()
    def button(name, target):
        assert task.request_button(name)
        wait_until(lambda: cfg.refresh_info.plugin_instance == target['instance']
                   and not task.screen_selection.has_pending
                   and task.screen_selection._consuming is None
                   and task.screen_selection._active is None)
    try:
        yield task, plugin, button
    finally:
        task.stop()


@pytest.mark.parametrize('phase', ['generation', 'display'])
@pytest.mark.parametrize('presses,final', [(2, W3), (3, W1), (4, W2)])
def test_rapid_c_during_active_work_is_latest_wins_without_overlap(location_config, location_worker,
                                                                  phase, presses, final):
    task, plugin, _ = location_worker
    entered, release = threading.Event(), threading.Event()
    generated = []
    original = plugin.generate_image.side_effect
    active = []
    def generate(data, cfg):
        generated.append(data['name'])
        if len(generated) == 1 and phase == 'generation':
            entered.set()
            assert release.wait(3)
        return original(data, cfg)
    def display(*args, **kwargs):
        assert not active
        active.append(True)
        try:
            if task.display_manager.display_image.call_count == 1 and phase == 'display':
                entered.set()
                assert release.wait(3)
        finally:
            active.clear()
    plugin.generate_image.side_effect = generate
    task.display_manager.display_image.side_effect = display
    try:
        assert task.request_button('A')
        assert entered.wait(3)
        for _ in range(presses):
            assert task.request_button('C')
        release.set()
        wait_until(lambda: not task.screen_selection.has_pending
                   and task.screen_selection._consuming is None
                   and task.screen_selection._active is None)
    finally:
        release.set()
    assert settings(location_config)['selected'] == final
    expected = [W1['instance']] + ([] if final == W1 else [final['instance']])
    assert generated == expected
    assert task.display_manager.display_image.call_count == len(expected)
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=5)) is None


def test_c_follows_pending_plugin_and_ab_use_its_latest_target(location_config):
    cfg = location_config
    controller = ScreenSelectionController(cfg, Mock())
    consume(controller, 'A')
    assert controller.request_button('B')
    assert controller.request_button('C')
    assert not controller.request_button('B')  # Already heading to Airport Two.
    result = controller.consume_pending()
    assert result[1].name == M2['instance']
    present(controller, result)
    assert settings(cfg)['remembered']['weather'] == W1
    assert consume(controller, 'A')[1].name == W1['instance']
    assert consume(controller, 'B')[1].name == M2['instance']


def test_web_displacement_restoration_keeps_location_memories(location_config, location_worker):
    task, _, button = location_worker
    button('A', W1)
    button('C', W2)
    button('B', M1)
    button('C', M2)
    before = dict(settings(location_config)['remembered'])
    task.manual_update(ManualRefresh('weather', {'name': 'Comic'}))
    assert task.screen_selection.displaced
    button('B', M2)
    assert not task.screen_selection.displaced
    assert settings(location_config)['remembered'] == before
    assert task.display_manager.display_image.call_count == 6
    assert not task.request_button('B')
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=5)) is None


def test_web_saved_location_does_not_replace_button_location_memory(location_config, location_worker):
    task, _, button = location_worker
    button('A', W1)
    button('C', W2)
    playlist, instance = task.screen_selection.resolve(W3)
    task.manual_update(PlaylistRefresh(playlist, instance, force=True))
    assert task.screen_selection.displaced
    assert settings(location_config)['selected'] == W2
    assert settings(location_config)['remembered'] == {'weather': W2}
    button('A', W2)
    assert not task.screen_selection.displaced
    assert task.display_manager.display_image.call_count == 4


@pytest.mark.parametrize('phase', ['generation', 'display'])
def test_failed_web_update_keeps_group_memory_and_duplicate_suppression(location_config, location_worker, phase):
    task, plugin, button = location_worker
    button('A', W1)
    button('C', W2)
    if phase == 'generation':
        plugin.generate_image.side_effect = RuntimeError('mock web failure')
    else:
        task.display_manager.display_image.side_effect = RuntimeError('mock web failure')
    with pytest.raises(RuntimeError):
        task.manual_update(ManualRefresh('metar', {'name': 'Comic'}))
    assert not task.screen_selection.displaced
    assert settings(location_config)['remembered'] == {'weather': W2}
    assert task.screen_selection.display_uncertain is (phase == 'display')
    task.stop()
    assert task.request_button('A') is (phase == 'display')


def test_c_context_ignores_web_plugin_and_advances_selected_group(location_config, location_worker):
    task, plugin, button = location_worker
    button('B', M1)
    entered, release = threading.Event(), threading.Event()
    original = plugin.generate_image.side_effect
    def generate(data, cfg):
        if data['name'] == 'Comic':
            entered.set()
            assert release.wait(3)
        return original(data, cfg)
    plugin.generate_image.side_effect = generate
    errors = []
    def web_update():
        try:
            task.manual_update(ManualRefresh('weather', {'name': 'Comic'}))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=web_update)
    thread.start()
    try:
        assert entered.wait(3)
        assert task.request_button('C')
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    wait_until(lambda: location_config.refresh_info.plugin_instance == M2['instance']
               and task.screen_selection._active is None)
    assert settings(location_config)['selected'] == M2
    assert not task.screen_selection.displaced
    assert task.display_manager.display_image.call_count == 3


@pytest.mark.parametrize('phase', ['generation', 'display'])
def test_failed_location_preserves_committed_selection_without_automatic_retry(location_config, location_worker,
                                                                        monkeypatch, phase):
    task, plugin, button = location_worker
    button('A', W1)
    failed = threading.Event()
    monkeypatch.setattr('refresh_task.logger.exception', lambda *args: failed.set())
    if phase == 'generation':
        plugin.generate_image.side_effect = RuntimeError('mock network failure')
    else:
        task.display_manager.display_image.side_effect = RuntimeError('mock display failure')
    assert task.request_button('C')
    assert failed.wait(3)
    wait_until(lambda: task.screen_selection._active is None)
    assert settings(location_config)['selected'] == W1
    assert settings(location_config)['remembered']['weather'] == W1
    assert location_config.refresh_info.plugin_instance == W1['instance']
    assert task._scheduled_action(datetime.now(timezone.utc) + timedelta(days=5)) is None
    assert task.screen_selection.display_uncertain is (phase == 'display')
    task.stop()
    assert task.request_button('C')  # Deliberate retry, not automatic replay.
