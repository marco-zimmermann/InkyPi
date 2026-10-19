"""Active-screen cadence with fake clocks, temporary JSON, and mocked IO only."""
import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from config import Config
from model import RefreshInfo
from refresh_task import RefreshTask, ManualRefresh
from screen_selection import ScreenSelectionController
from tests.test_location_navigation import location_config, settings, wait_until, W1, W2, W3, M1, M2
from utils.image_utils import compute_image_hash


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.wall = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)

    def monotonic(self):
        return self.seconds

    def utcnow(self):
        return self.wall

    def advance(self, seconds):
        self.seconds += seconds
        self.wall += timedelta(seconds=seconds)


@pytest.fixture
def auto_config(location_config):
    cfg = location_config
    def enable(n):
        n.update(manual_only=False, selected=W1, remembered={'weather': W1},
                 displaced=False, display_uncertain=False)
        for group in n['groups'].values():
            group['interval_seconds'] = 900
            group['auto_refresh_enabled'] = True
    cfg.update_section('button_navigation', enable)
    cfg.refresh_info = RefreshInfo(refresh_time=None, refresh_type='Playlist', playlist='Default',
        plugin_id='weather', plugin_instance=W1['instance'],
        image_hash=compute_image_hash(Image.new('RGB', (8, 8), 'red')))
    cfg.write_config()
    return cfg


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def worker(auto_config, clock, monkeypatch):
    plugin = Mock(config={})
    plugin.generate_image.side_effect = lambda data, cfg: Image.new('RGB', (8, 8),
        {W1['instance']:'green', W2['instance']:'blue', W3['instance']:'white',
         M1['instance']:'yellow', M2['instance']:'black'}[data['name']])
    monkeypatch.setattr('refresh_task.get_plugin_instance', lambda _: plugin)
    task = RefreshTask(auto_config, Mock(), monotonic=clock.monotonic, utcnow=clock.utcnow)
    task.start()
    try:
        yield task, plugin
    finally:
        task.stop()


def state(cfg, plugin='weather'):
    return settings(cfg).get('refresh_state', {}).get(plugin, {})


def due(task, clock):
    clock.advance(900)
    task.signal_config_change()


def finished(task, cfg, plugin='weather', outcome=None):
    wait_until(lambda: state(cfg, plugin).get('attempt_completed_at') is not None
        and task.screen_selection._active is None
        and (outcome is None or state(cfg, plugin).get('outcome') == outcome))


def controller(cfg, clock):
    return ScreenSelectionController(cfg, Mock(), monotonic=clock.monotonic, utcnow=clock.utcnow)


def test_independent_intervals_only_selected_location(auto_config, clock):
    c = controller(auto_config, clock)
    assert c.automatic_target() is None
    clock.advance(899)
    assert c.automatic_target() is None
    clock.advance(1)
    assert c.automatic_target()[1].name == W1['instance']
    settings(auto_config)['selected'] = M1
    settings(auto_config)['groups']['metar']['interval_seconds'] = 1800
    assert c.automatic_target() is None
    clock.advance(900)
    assert c.automatic_target()[1].name == M1['instance']
    settings(auto_config)['selected'] = W2
    assert c.automatic_target()[1].name == W2['instance']
    assert auto_config.get_playlist_manager().get_playlist('Default').current_plugin_index is None


@pytest.mark.parametrize('plugin,target', [('weather', W1), ('metar', M1)])
def test_default_intervals_are_900(auto_config, clock, plugin, target):
    settings(auto_config)['selected'] = target
    settings(auto_config)['groups'][plugin].pop('interval_seconds')
    c = controller(auto_config, clock)
    clock.advance(899)
    assert c.automatic_target() is None
    clock.advance(1)
    assert c.automatic_target()[1].name == target['instance']


@pytest.mark.parametrize('value', [None, 0, -1, True, '900', float('inf'), float('nan'), [], {}, 10**1000])
def test_invalid_interval_pauses_without_writes(auto_config, clock, value):
    settings(auto_config)['groups']['weather']['interval_seconds'] = value
    before = Path(auto_config.config_file).read_bytes()
    c = controller(auto_config, clock)
    clock.advance(100000)
    assert c.automatic_target() is None
    assert Path(auto_config.config_file).read_bytes() == before


@pytest.mark.parametrize('change', ['manual_only','displaced','display_uncertain','null','deleted','disabled_plugin','no_group'])
def test_safety_gates_never_fall_back_to_playlists(auto_config, clock, change):
    task = RefreshTask(auto_config, Mock(), monotonic=clock.monotonic, utcnow=clock.utcnow)
    n = settings(auto_config)
    if change in ('manual_only','displaced','display_uncertain'):
        n[change] = True
    elif change == 'null':
        n['selected'] = None
    elif change == 'deleted':
        n['selected'] = dict(W1, instance='deleted')
    elif change == 'disabled_plugin':
        auto_config.plugins_list[0]['disabled'] = True
    else:
        n['groups'] = {}
    clock.advance(100000)
    assert task._scheduled_action(clock.utcnow()) is None
    assert not task.screen_selection.has_pending


def test_disabled_mode_still_uses_ordinary_playlists(auto_config, clock):
    settings(auto_config).update(enabled=False, manual_only=True)
    task = RefreshTask(auto_config, Mock(), monotonic=clock.monotonic, utcnow=clock.utcnow)
    assert task._scheduled_action(clock.utcnow()).plugin_instance.name == W1['instance']
    assert 'refresh_state' not in settings(auto_config)


@pytest.mark.parametrize('recovered', ['recent','overdue','running','missing','malformed','future'])
def test_restart_utc_recovery_and_grace(auto_config, clock, recovered):
    n = settings(auto_config)
    completed = clock.utcnow() - timedelta(seconds=100 if recovered=='recent' else 5000)
    if recovered=='future':
        completed = clock.utcnow() + timedelta(days=1)
    n['refresh_state'] = {'weather': {'attempt_completed_at': completed.isoformat(), 'outcome':'failed'}}
    if recovered=='running':
        n['refresh_state']['weather'].update(attempt_completed_at=None, outcome='running')
    if recovered=='missing':
        n.pop('refresh_state')
    if recovered=='malformed':
        n['refresh_state']['weather']['attempt_completed_at'] = 'invalid'
    c = controller(auto_config, clock)
    assert c.automatic_target() is None
    if recovered == 'running':
        c.recover_interrupted_attempt()
        assert Config().get_config('button_navigation')['resource_pause']['reason'] == 'interrupted_attempt'
        clock.advance(100000)
        assert c.automatic_target() is None
        return
    delay = 800 if recovered=='recent' else 60 if recovered=='overdue' else 900
    clock.advance(delay-1)
    assert c.automatic_target() is None
    clock.advance(1)
    assert c.automatic_target()[1].name == W1['instance']
    assert not c.has_pending


def test_clock_adjustments_do_not_move_runtime_deadline(auto_config, clock):
    c = controller(auto_config, clock)
    assert c.automatic_target() is None
    clock.wall += timedelta(days=40)
    assert c.automatic_target() is None
    clock.seconds = 899
    clock.wall -= timedelta(days=80)
    assert c.automatic_target() is None
    clock.seconds = 900
    assert c.automatic_target() is not None


@pytest.mark.parametrize('identical', [False, True])
def test_completion_based_cadence_dedup_and_atomic_success(auto_config, clock, worker, identical):
    task, plugin = worker
    before = copy.deepcopy(settings(auto_config)['remembered'])
    def generate(*args):
        assert state(auto_config)['outcome']=='running'
        assert Path(auto_config.config_file).read_text().find('attempt_started_at') >= 0
        assert task.condition.acquire(timeout=0.1)
        task.condition.release()
        clock.advance(35)
        return Image.new('RGB', (8, 8), 'red' if identical else 'green')
    plugin.generate_image.side_effect = generate
    def display(*args, **kwargs):
        assert settings(auto_config)['display_uncertain'] is True
        clock.advance(30)
    task.display_manager.display_image.side_effect = display
    due(task, clock)
    finished(task, auto_config, outcome='unchanged' if identical else 'presented')
    assert plugin.generate_image.call_count == 1
    assert task.display_manager.display_image.call_count == (0 if identical else 1)
    timing = state(auto_config)
    assert timing['attempt_completed_at']==clock.utcnow().isoformat()
    assert timing['presentation_success_at']==timing['live_data_success_at']==timing['attempt_completed_at']
    assert settings(auto_config)['remembered']==before
    clock.advance(899)
    assert task._scheduled_action(clock.utcnow()) is None
    clock.advance(1)
    assert task._scheduled_action(clock.utcnow()).plugin_instance.name==W1['instance']
    assert Config().get_config('button_navigation')['refresh_state']['weather']==timing


@pytest.mark.parametrize('plugin,target', [('weather',W1), ('metar',M1)])
def test_generation_failure_is_paced_and_preserves_presentation(auto_config, clock, worker, plugin, target):
    task, renderer = worker
    settings(auto_config)['selected'] = target
    before = copy.deepcopy(auto_config.refresh_info.to_dict())
    oldnav = copy.deepcopy(settings(auto_config))
    renderer.generate_image.side_effect = RuntimeError('mock network/provider exception')
    due(task, clock)
    finished(task, auto_config, plugin, 'failed')
    assert state(auto_config,plugin).get('presentation_success_at') is None
    assert auto_config.refresh_info.to_dict()==before
    for key in ('selected','remembered','displaced','display_uncertain'):
        assert settings(auto_config)[key]==oldnav[key]
    task.display_manager.display_image.assert_not_called()
    assert not task.screen_selection.has_pending
    for _ in range(4):
        assert task._scheduled_action(clock.utcnow()) is None
    task.stop()
    restarted = RefreshTask(Config(), Mock(), monotonic=clock.monotonic, utcnow=clock.utcnow)
    assert restarted._scheduled_action(clock.utcnow()) is None
    clock.advance(899)
    assert restarted._scheduled_action(clock.utcnow()) is None
    clock.advance(1)
    assert restarted._scheduled_action(clock.utcnow()).plugin_instance.name==target['instance']


@pytest.mark.parametrize('status,live', [('live',True),('stale',True),('cached',False),('cached_stale',False),('unavailable',False), (None,False)])
def test_metar_presentation_is_not_always_live_retrieval(auto_config, clock, worker, status, live):
    task, plugin = worker
    settings(auto_config)['selected'] = M1
    image = Image.new('RGB', (8, 8), 'yellow')
    image.info.update(inkypi_data_status=status, inkypi_live_data_success=live)
    plugin.generate_image.side_effect = lambda *args: image
    due(task, clock)
    finished(task, auto_config, 'metar', 'presented')
    timing = state(auto_config, 'metar')
    assert timing['presentation_success_at'] is not None
    assert bool(timing.get('live_data_success_at')) is live
    assert timing['data_status']==status
    assert task._scheduled_action(clock.utcnow()) is None


def test_manual_button_attempt_also_restarts_automatic_cadence(auto_config, clock, worker):
    task, plugin = worker
    assert task.request_button('C')
    finished(task, auto_config, outcome='presented')
    assert settings(auto_config)['selected']==W2
    assert state(auto_config)['attempt_reference']==W2
    clock.advance(899)
    assert task._scheduled_action(clock.utcnow()) is None
    clock.advance(1)
    assert task._scheduled_action(clock.utcnow()).plugin_instance.name==W2['instance']


@pytest.mark.parametrize('phase',['generation','display'])
def test_inflight_scheduled_work_keeps_latest_pending_c(auto_config, clock, worker, phase):
    task, plugin = worker
    entered, release = threading.Event(), threading.Event()
    original = plugin.generate_image.side_effect
    names=[]
    def generate(data, cfg):
        names.append(data['name'])
        if len(names)==1 and phase=='generation':
            entered.set()
            assert release.wait(3)
        return original(data,cfg)
    plugin.generate_image.side_effect=generate
    active=[]
    def display(*args,**kwargs):
        assert not active
        active.append(True)
        try:
            if len(names)==1 and phase=='display':
                entered.set()
                assert release.wait(3)
        finally:
            active.pop()
    task.display_manager.display_image.side_effect=display
    due(task, clock)
    try:
        assert entered.wait(3)
        assert task.request_button('C')  # W2
        assert task.request_button('C')  # W3 replaces W2
        assert settings(auto_config)['selected']==W1
    finally:
        release.set()
    wait_until(lambda: settings(auto_config)['selected']==W3 and task.screen_selection._active is None)
    assert names==[W1['instance'],W3['instance']]
    assert task.display_manager.display_image.call_count==2
    assert settings(auto_config)['remembered']['weather']==W3


@pytest.mark.parametrize('incoming', ['button','web'])
def test_requests_during_attempt_write_preempt_auto_before_generation(auto_config, clock, worker, monkeypatch, incoming):
    task, plugin=worker
    update=auto_config.update_section
    once=[];event=threading.Event()
    def editing(key, transform):
        update(key, transform)
        if not once and state(auto_config).get('outcome')=='running':
            once.append(True)
            if incoming=='button':
                assert task.request_button('C')
            else:
                with task.condition:
                    task.manual_update_request=(ManualRefresh('weather',{'name':W3['instance']}),event,{})
                    task.condition.notify_all()
    monkeypatch.setattr(auto_config,'update_section',editing)
    due(task, clock)
    if incoming=='button':
        wait_until(lambda: settings(auto_config)['selected']==W2 and task.screen_selection._active is None)
        assert plugin.generate_image.call_args.args[0]['name']==W2['instance']
    else:
        assert event.wait(3)
        assert plugin.generate_image.call_args.args[0]['name']==W3['instance']
        assert settings(auto_config)['selected']==W1
        assert settings(auto_config)['displaced'] is True
    assert plugin.generate_image.call_count==1


@pytest.mark.parametrize('failure', ['start','completion','failure_completion'])
def test_persistence_failure_cannot_cause_retry_storm(auto_config, clock, worker, monkeypatch, failure):
    task, plugin=worker
    update=auto_config.update_section
    commit=auto_config.commit_refresh
    failed=threading.Event()
    def editing(key, transform):
        if failure=='start' or failure=='failure_completion' and state(auto_config).get('outcome')=='running':
            failed.set()
            raise OSError('mock scheduler storage failure')
        return update(key,transform)
    def committing(*args,**kwargs):
        failed.set()
        raise OSError('mock completion storage failure')
    if failure=='completion':
        monkeypatch.setattr(auto_config,'commit_refresh',committing)
    else:
        monkeypatch.setattr(auto_config,'update_section',editing)
    if failure=='failure_completion':
        plugin.generate_image.side_effect=RuntimeError('provider unavailable')
    due(task,clock)
    assert failed.wait(3)
    wait_until(lambda: task.screen_selection._active is None)
    if failure=='start':
        plugin.generate_image.assert_not_called()
    if failure=='completion':
        assert settings(auto_config)['display_uncertain'] is True
    clock.advance(100000)
    assert task._scheduled_action(clock.utcnow()) is None
    assert settings(auto_config)['selected']==W1


def test_cancelled_generation_has_a_completion_deadline_and_no_display(auto_config, clock, worker):
    task, plugin=worker
    entered,release=threading.Event(),threading.Event()
    def generate(*args):
        entered.set()
        assert release.wait(3)
        return Image.new('RGB',(8,8),'blue')
    plugin.generate_image.side_effect=generate
    due(task,clock)
    assert entered.wait(3)
    stopper=threading.Thread(target=task.stop);stopper.start()
    try:
        wait_until(lambda: not task.running)
    finally:
        release.set();stopper.join(3)
    assert not stopper.is_alive()
    assert state(auto_config)['outcome']=='cancelled'
    assert state(auto_config)['attempt_completed_at'] is not None
    task.display_manager.display_image.assert_not_called()
    assert settings(auto_config)['selected']==W1


def test_fallback_keeps_prior_live_success_and_unchanged_attempt_is_paced(auto_config, clock, worker):
    task,plugin=worker
    settings(auto_config)['selected']=M1
    image=Image.new('RGB',(8,8),'yellow')
    image.info.update(inkypi_data_status='live',inkypi_live_data_success=True)
    plugin.generate_image.side_effect=lambda *a:image
    due(task,clock)
    finished(task,auto_config,'metar','presented')
    first=copy.deepcopy(state(auto_config,'metar'))
    image.info.update(inkypi_data_status='cached_stale',inkypi_live_data_success=False)
    due(task,clock)
    wait_until(lambda: plugin.generate_image.call_count==2)
    finished(task,auto_config,'metar','unchanged')
    after=state(auto_config,'metar')
    assert after['live_data_success_at']==first['live_data_success_at']
    assert after['presentation_success_at']!=first['presentation_success_at']
    assert after['data_status']=='cached_stale'
    assert task.display_manager.display_image.call_count==1
    assert task._scheduled_action(clock.utcnow()) is None


def test_physical_failure_pauses_until_deliberate_restoration(auto_config, clock, worker):
    task,plugin=worker
    task.display_manager.display_image.side_effect=RuntimeError('mock SPI error')
    due(task,clock)
    finished(task,auto_config,outcome='failed')
    assert settings(auto_config)['display_uncertain'] is True
    clock.advance(100000)
    assert task._scheduled_action(clock.utcnow()) is None
    task.display_manager.display_image.side_effect=None
    assert task.request_button('A')
    wait_until(lambda: task.display_manager.display_image.call_count==2)
    finished(task,auto_config,outcome='presented')
    assert settings(auto_config)['display_uncertain'] is False
    assert settings(auto_config)['selected']==W1


def test_successful_web_displacement_pauses_and_button_restores(auto_config, clock, worker):
    task,plugin=worker
    task.manual_update(ManualRefresh('weather',{'name':W3['instance']}))
    assert settings(auto_config)['selected']==W1
    assert settings(auto_config)['displaced'] is True
    clock.advance(100000)
    assert task._scheduled_action(clock.utcnow()) is None
    assert task.request_button('A')
    wait_until(lambda: task.display_manager.display_image.call_count==2)
    finished(task,auto_config,outcome='presented')
    assert settings(auto_config)['displaced'] is False
    assert task._scheduled_action(clock.utcnow()) is None


def test_manual_only_toggle_never_enqueues_and_deliberate_buttons_remain_usable(auto_config, clock):
    c=controller(auto_config,clock)
    settings(auto_config)['manual_only']=True
    clock.advance(100000)
    assert c.automatic_target() is None and not c.has_pending
    assert c.request_button('C')
    assert c.consume_pending()[1].name==W2['instance']
    c.end_refresh()
    settings(auto_config)['manual_only']=False
    assert not c.has_pending  # Due work may run on the worker's next check, never in the setter.


@pytest.mark.parametrize('plugin,target', [('weather',W1),('metar',M1)])
def test_fresh_constructor_does_not_replay_pending_or_inflight_attempt(auto_config, clock, plugin, target):
    settings(auto_config)['selected']=target
    first=controller(auto_config,clock)
    first.begin_attempt(target)
    first.request_button('C')
    restarted=controller(Config(),clock)
    assert not restarted.has_pending
    assert restarted.automatic_target() is None
    clock.advance(899)
    assert restarted.automatic_target() is None
    clock.advance(1)
    assert restarted.automatic_target() is None
    restarted.recover_interrupted_attempt()
    assert Config().get_config('button_navigation')['resource_pause']['reason']=='interrupted_attempt'


def test_failed_navigation_target_is_never_the_next_automatic_target(auto_config, clock, worker):
    task,plugin=worker
    plugin.generate_image.side_effect=RuntimeError('mock new-location provider failure')
    assert task.request_button('C')
    finished(task,auto_config,outcome='failed')
    assert state(auto_config)['attempt_reference']==W2
    assert settings(auto_config)['selected']==W1
    assert settings(auto_config)['remembered']['weather']==W1
    clock.advance(900)
    assert task._scheduled_action(clock.utcnow()).plugin_instance.name==W1['instance']
    task.display_manager.display_image.assert_not_called()


def test_slow_failure_deadline_starts_at_completion(auto_config, clock, worker):
    task,plugin=worker
    def fail(*args):
        clock.advance(200)
        raise RuntimeError('slow provider failure')
    plugin.generate_image.side_effect=fail
    due(task,clock)
    finished(task,auto_config,outcome='failed')
    clock.advance(899)
    assert task._scheduled_action(clock.utcnow()) is None
    clock.advance(1)
    assert task._scheduled_action(clock.utcnow()) is not None


def test_manual_only_keeps_legacy_write_behavior_with_configured_groups(auto_config, clock, worker):
    task,plugin=worker
    settings(auto_config)['manual_only']=True
    assert task.request_button('C')
    wait_until(lambda: settings(auto_config)['selected']==W2 and task.screen_selection._active is None)
    assert 'refresh_state' not in settings(auto_config)
    clock.advance(100000)
    assert task._scheduled_action(clock.utcnow()) is None


@pytest.mark.parametrize('failure',['replace','directory_fsync'])
def test_atomic_start_failure_aborts_render_and_restart_recovers_conservatively(
        auto_config,clock,worker,monkeypatch,failure):
    import os
    import stat
    task,plugin=worker
    failed=threading.Event()
    original_fsync=os.fsync
    def fail_replace(*args):
        failed.set()
        raise OSError('mock atomic start replace failure')
    def fail_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            failed.set()
            raise OSError('mock post-rename directory sync failure')
        return original_fsync(fd)
    if failure=='replace':
        monkeypatch.setattr('config.os.replace',fail_replace)
    else:
        monkeypatch.setattr('config.os.fsync',fail_fsync)
    due(task,clock)
    assert failed.wait(3)
    wait_until(lambda: task.screen_selection._active is None)
    plugin.generate_image.assert_not_called()
    assert state(auto_config)=={}
    assert not list(Path(auto_config.config_file).parent.glob('.device-*'))
    restarted=controller(Config(),clock)
    assert restarted.automatic_target() is None
    clock.advance(899)
    assert restarted.automatic_target() is None
    clock.advance(1)
    assert (restarted.automatic_target() is not None) == (failure == 'replace')
    if failure == 'directory_fsync':
        restarted.recover_interrupted_attempt()
        assert Config().get_config('button_navigation')['resource_pause']['reason']=='interrupted_attempt'
