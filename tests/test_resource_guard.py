"""Resource evidence, migration and worker faults; fake cgroups/rendering only."""
import copy
import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from PIL import Image

from config import Config
from refresh_task import RefreshTask, ManualRefresh
from resource_guard import (CgroupResourceMonitor, watch_resources, resource_checkpoint,
                            report_resource_failure)
from screen_selection import ScreenSelectionController
from utils import image_utils as screenshots
from tests.test_active_screen_schedule import auto_config, clock, controller, due, finished, state, worker
from tests.test_location_navigation import location_config, settings, wait_until, W1, W2, W3, M1, M2
from tests.test_screenshot import fake_browser

MIB = 2**20


@pytest.fixture
def cgroup(tmp_path):
    membership = tmp_path / 'membership'
    membership.write_text('0::/test.service\n')
    path = tmp_path / 'test.service'
    path.mkdir()
    def make(current=100, peak=150, maximum=200, **events):
        for name, value in {'memory.current': int(current*MIB), 'memory.peak': int(peak*MIB),
                            'memory.max': 'max' if maximum is None else int(maximum*MIB)}.items():
            (path/name).write_text(str(value))
        values = dict(low=0, high=0, max=0, oom=0, oom_kill=0, oom_group_kill=0)
        values.update(events)
        (path/'memory.events').write_text('\n'.join(f'{k} {v}' for k,v in values.items()))
        return CgroupResourceMonitor(membership=membership, root=tmp_path)
    return make, path


@pytest.mark.parametrize('current,peak,reason', [(100,150,None), (185,185,None),
    (190,190,'memory_critical'), (100,195,None)])
def test_peak_and_current_thresholds_are_read_only(cgroup, current, peak, reason):
    make,path=cgroup
    monitor=make(current=current,peak=peak)
    before={p.name:p.read_bytes() for p in path.iterdir()}
    result=monitor.check()
    assert (result[0] if result else None)==reason
    if result: assert result[1]['memory_peak']==int(peak*MIB)
    assert {p.name:p.read_bytes() for p in path.iterdir()}==before


@pytest.mark.parametrize('event,reason', [('max','memory_limit'),('oom','oom_evidence'),
    ('oom_kill','oom_evidence'),('oom_group_kill','oom_evidence')])
def test_kernel_counter_baseline_then_new_evidence(cgroup, event, reason):
    make,_=cgroup
    monitor=make(**{event:349})
    assert monitor.check() is None
    assert monitor.check() is None
    make(**{event:350})
    result=monitor.check()
    assert result[0]==reason and result[1][event]==350
    assert result[1][event+'_delta']==1


def test_warning_is_bounded_and_does_not_pause(cgroup, caplog):
    monitor=cgroup[0](current=185,peak=185)
    assert monitor.check() is None and monitor.check() is None
    assert sum('90%' in r.message for r in caplog.records)==1


def test_unlimited_memory_still_detects_oom(cgroup):
    make,_=cgroup;monitor=make(maximum=None,oom_kill=1)
    assert monitor.check() is None
    make(maximum=None,oom_kill=2)
    assert monitor.check()[0]=='oom_evidence'


@pytest.mark.parametrize('failure',['missing','malformed','root','traversal'])
def test_unavailable_accounting_does_not_claim_a_measurement(tmp_path, failure, caplog):
    membership=tmp_path/'membership'
    membership.write_text({'missing':'0::/missing','malformed':'bad',
                          'root':'0::/','traversal':'0::/../elsewhere'}[failure])
    monitor=CgroupResourceMonitor(membership=membership,root=tmp_path)
    assert monitor.check() is None and monitor.check() is None
    assert sum('unavailable' in r.message for r in caplog.records)==1


def test_corrupt_counter_or_lost_monitor_is_unknown(cgroup, caplog):
    make,path=cgroup;monitor=make()
    (path/'memory.events').write_text('max invalid')
    assert monitor.check() is None
    assert 'unavailable' in caplog.text


def test_notifications_are_scoped_to_the_worker_thread():
    check, failure=Mock(),Mock()
    report_resource_failure('chromium_timeout')
    with watch_resources(check,failure):
        resource_checkpoint()
        report_resource_failure('chromium_exit',returncode=-9)
        t=threading.Thread(target=lambda:report_resource_failure('chromium_timeout'))
        t.start();t.join(2)
    resource_checkpoint()
    report_resource_failure('chromium_timeout')
    check.assert_called_once_with()
    failure.assert_called_once_with('chromium_exit',{'returncode':-9})


@pytest.mark.parametrize('value',[None,False,0,1,'true',{},[]])
def test_only_literal_true_enables_a_group(auto_config, clock, value):
    group=settings(auto_config)['groups']['weather']
    group['auto_refresh_enabled']=value
    c=controller(auto_config,clock);clock.advance(100000)
    assert c.automatic_target() is None
    c.begin_attempt(W1)
    assert state(auto_config)=={}
    assert c.request_button('C') and c.consume_pending()[1].name==W2['instance']


def test_missing_enablement_safe_migration_keeps_instances_and_navigation(location_config, clock):
    cfg=location_config;n=settings(cfg)
    n.update(selected=W1,manual_only=False)
    before=copy.deepcopy(cfg.get_playlist_manager().to_dict())
    c=controller(cfg,clock);clock.advance(100000)
    assert c.automatic_target() is None
    assert c.request_button('B')
    action=c.consume_pending()
    assert action[1].name==M1['instance']
    assert cfg.get_playlist_manager().to_dict()==before
    assert all('auto_refresh_enabled' not in g for g in n['groups'].values())


@pytest.mark.parametrize('weather,metar',[(False,False),(False,True),(True,False),(True,True)])
@pytest.mark.parametrize('target',[W1,M1])
def test_independent_opt_in_is_selected_only(auto_config, clock, weather, metar, target):
    n=settings(auto_config);n['selected']=target
    n['groups']['weather']['auto_refresh_enabled']=weather
    n['groups']['metar']['auto_refresh_enabled']=metar
    c=controller(auto_config,clock);clock.advance(900)
    result=c.automatic_target()
    assert bool(result)==(weather if target==W1 else metar)
    if result: assert result[1].name==target['instance']
    n['manual_only']=True
    assert c.automatic_target() is None


def test_metar_only_keeps_weather_cycle_and_separate_memories(auto_config, clock, worker):
    task,plugin=worker
    settings(auto_config)['groups']['weather']['auto_refresh_enabled']=False
    assert task.request_button('C')
    wait_until(lambda:settings(auto_config)['selected']==W2)
    due(task,clock)
    assert task._scheduled_action(clock.utcnow()) is None
    assert task.request_button('B')
    finished(task,auto_config,'metar','presented')
    assert settings(auto_config)['remembered']=={'weather':W2,'metar':M1}
    clock.advance(900)
    assert task._scheduled_action(clock.utcnow()).plugin_instance.name==M1['instance']
    assert task.request_button('A')
    wait_until(lambda:settings(auto_config)['selected']==W2)
    assert task._scheduled_action(clock.utcnow()) is None


def test_pause_persists_first_reason_and_only_sanitized_details(auto_config, clock):
    c=controller(auto_config,clock)
    before=copy.deepcopy(settings(auto_config))
    c.pause_automatic('memory_limit',{'max':349,'memory_current':195*MIB,
        'password':'secret','url':'https://secret.invalid','returncode':'not numeric'})
    pause=copy.deepcopy(settings(auto_config)['resource_pause'])
    assert pause['reason']=='memory_limit'
    assert pause['details']=={'max':349,'memory_current':195*MIB}
    c.pause_automatic('chromium_timeout')
    assert settings(auto_config)['resource_pause']==pause
    other=copy.deepcopy(settings(auto_config));other.pop('resource_pause')
    assert other==before
    restarted=controller(Config(),clock);clock.advance(100000)
    assert restarted.resource_paused and restarted.automatic_target() is None
    assert restarted.request_button('C')


@pytest.mark.parametrize('value',[{},False,'corrupt',[]])
def test_malformed_pause_fails_closed(auto_config, clock, value):
    settings(auto_config)['resource_pause']=value
    c=controller(auto_config,clock);clock.advance(100000)
    assert c.resource_paused and c.automatic_target() is None


@pytest.mark.parametrize('phase',['generation','display'])
def test_critical_pressure_does_not_interrupt_presentation_or_clear_itself(auto_config, clock, monkeypatch, phase):
    hit=threading.Event();calls=[]
    monitor=Mock();monitor.check.side_effect=lambda:('memory_critical',{'memory_peak':191*MIB}) if hit.is_set() else None
    plugin=Mock(config={})
    def generate(data,cfg):
        calls.append(data['name'])
        if phase=='generation':hit.set()
        return Image.new('RGB',(8,8),'green')
    plugin.generate_image.side_effect=generate
    monkeypatch.setattr('refresh_task.get_plugin_instance',lambda _:plugin)
    task=RefreshTask(auto_config,Mock(),monotonic=clock.monotonic,utcnow=clock.utcnow,resource_monitor=monitor)
    def display(*args,**kwargs):
        assert task.condition.acquire(timeout=0.1);task.condition.release()
        assert settings(auto_config)['display_uncertain'] is True
        if phase=='display':hit.set()
    task.display_manager.display_image.side_effect=display
    task.start()
    try:
        due(task,clock);finished(task,auto_config,outcome='presented')
        assert task.display_manager.display_image.call_count==1
        assert settings(auto_config)['display_uncertain'] is False
        pause=copy.deepcopy(settings(auto_config)['resource_pause'])
        assert pause['reason']=='memory_critical'
        clock.advance(100000)
        assert task._scheduled_action(clock.utcnow()) is None
        assert task.request_button('C')
        wait_until(lambda:settings(auto_config)['selected']==W2)
        assert settings(auto_config)['resource_pause']==pause
        assert calls==[W1['instance'],W2['instance']]
    finally:task.stop()


@pytest.mark.parametrize('exception',[requests.ReadTimeout('temporary'),requests.ConnectTimeout('temporary'),
    requests.HTTPError('503'),RuntimeError('provider overloaded')])
def test_network_failure_keeps_interval_without_resource_pause(auto_config, clock, worker, exception):
    task,plugin=worker;plugin.generate_image.side_effect=exception
    due(task,clock);finished(task,auto_config,outcome='failed')
    assert not task.screen_selection.resource_paused
    assert 'resource_pause' not in settings(auto_config)
    assert task._scheduled_action(clock.utcnow()) is None
    clock.advance(900)
    assert task._scheduled_action(clock.utcnow()).plugin_instance.name==W1['instance']
    assert plugin.generate_image.call_count==1


@pytest.mark.parametrize('failure,reason',[('timeout','chromium_timeout'),('exit','chromium_exit'),
    ('invalid','chromium_output_failure'),('cleanup','chromium_cleanup_failure')])
def test_real_screenshot_failure_contract_pauses_and_cleans(auto_config, clock, worker, monkeypatch, tmp_path, failure, reason):
    task,plugin=worker
    screenshots.reset_screenshot_shutdown()
    monkeypatch.setattr(screenshots.tempfile,'tempdir',str(tmp_path))
    monkeypatch.setattr(screenshots,'_find_chromium_binary',lambda:'/fake/chromium')
    process,_,_=fake_browser(monkeypatch,result=1 if failure=='exit' else 0,
                             mode='invalid' if failure=='invalid' else 'success')
    if failure=='timeout':process.communicate.side_effect=subprocess.TimeoutExpired('fake',0.01)
    if failure=='cleanup':monkeypatch.setattr(screenshots,'_cleanup_screenshot_process',Mock(side_effect=RuntimeError('stalled cleanup')))
    def generate(*args):
        result=screenshots.take_screenshot('synthetic-local-html',(8,6),process_timeout_seconds=0.015)
        if result is None:raise RuntimeError('Screenshot failed')
        return result
    plugin.generate_image.side_effect=generate
    due(task,clock);finished(task,auto_config,outcome='failed')
    pause=settings(auto_config)['resource_pause']
    assert pause['reason']==reason
    assert controller(Config(),clock).resource_paused
    task.display_manager.display_image.assert_not_called()
    assert not list(tmp_path.glob('inkypi-*'))
    assert settings(auto_config).get('display_uncertain',False) is False
    screenshots.reset_screenshot_shutdown()


def test_memory_allocation_failure_is_not_a_network_retry(auto_config, clock, worker):
    task,plugin=worker;plugin.generate_image.side_effect=MemoryError('allocation failed')
    due(task,clock);finished(task,auto_config,outcome='failed')
    assert settings(auto_config)['resource_pause']['reason']=='memory_allocation'
    assert settings(auto_config)['selected']==W1
    assert task._scheduled_action(clock.utcnow()) is None


@pytest.mark.parametrize('fatal',[SystemExit('unexpected'),KeyboardInterrupt()])
def test_unexpected_worker_exception_persists_pause_without_restart_loop(auto_config, clock, worker, fatal):
    task,plugin=worker;plugin.generate_image.side_effect=fatal
    due(task,clock)
    wait_until(lambda:not task.thread.is_alive())
    assert not task.running
    assert settings(auto_config)['resource_pause']['reason']=='worker_terminated'
    assert not controller(Config(),clock).automatic_target()
    assert settings(auto_config)['selected']==W1
    task.display_manager.display_image.assert_not_called()
    with pytest.raises(RuntimeError,match='operator recovery'):
        task.manual_update(ManualRefresh('weather',{'name':W2['instance']}))


def test_unexpected_worker_return_is_detected_but_orderly_stop_is_not(auto_config, clock, monkeypatch):
    task=RefreshTask(auto_config,Mock(),monotonic=clock.monotonic,utcnow=clock.utcnow)
    monkeypatch.setattr(task,'_run',lambda:None)
    task.start();task.thread.join(2)
    assert settings(auto_config)['resource_pause']['reason']=='worker_terminated'


def test_orderly_shutdown_leaves_no_resource_pause(auto_config, clock, worker):
    task,_=worker;task.stop()
    assert 'resource_pause' not in settings(auto_config)


def test_interrupted_attempt_requires_explicit_operator_acknowledgement(auto_config, clock):
    c=controller(auto_config,clock);c.begin_attempt(W1)
    original=copy.deepcopy(state(auto_config))
    restarted=RefreshTask(Config(),Mock(),monotonic=clock.monotonic,utcnow=clock.utcnow)
    restarted.start()
    try:
        wait_until(lambda:restarted.screen_selection.resource_paused and
                   restarted.device_config.get_config('button_navigation').get('resource_pause') is not None)
        assert state(restarted.device_config)==original
        clock.advance(100000)
        assert restarted._scheduled_action(clock.utcnow()) is None
    finally:restarted.stop()
    # Offline, deliberate operator recovery retains all actual success times.
    recovery=Config()
    def acknowledge(n):
        n.pop('resource_pause',None)
        for attempt in n.get('refresh_state',{}).values():
            if attempt.get('outcome')=='running':
                attempt.update(outcome='interrupted',interruption_acknowledged_at=clock.utcnow().isoformat())
    recovery.update_section('button_navigation',acknowledge)
    recovered=controller(Config(),clock)
    assert not recovered.resource_paused
    clock.advance(899);assert recovered.automatic_target() is None
    clock.advance(1);assert recovered.automatic_target()[1].name==W1['instance']
    assert state(recovery)['attempt_completed_at'] is None
    assert 'presentation_success_at' not in state(recovery)


@pytest.mark.parametrize('failure',['replace','directory_fsync'])
def test_pause_storage_failure_latches_and_crash_marker_survives(auto_config, clock, monkeypatch, failure):
    c=controller(auto_config,clock);c.begin_attempt(W1)
    sync=os.fsync
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError('directory sync failed')
        return sync(fd)
    with monkeypatch.context() as patch:
        if failure=='replace':patch.setattr('config.os.replace',Mock(side_effect=OSError('rename failed')))
        else:patch.setattr('config.os.fsync',fsync)
        c.pause_automatic('memory_limit',{'max':1})
        assert c.resource_paused
        assert 'resource_pause' not in settings(auto_config)
        clock.advance(100000);assert c.automatic_target() is None
    restarted=controller(Config(),clock)
    assert restarted.resource_paused
    restarted.recover_interrupted_attempt()
    assert Config().get_config('button_navigation')['resource_pause']['reason'] in ('interrupted_attempt','memory_limit')
    assert not list(Path(auto_config.config_file).parent.glob('.device-*'))


def test_disabled_mode_keeps_playlists_and_does_not_probe_or_write_pause(auto_config, clock):
    settings(auto_config).update(enabled=False,resource_pause={'reason':'memory_limit'})
    monitor=Mock();monitor.check.side_effect=AssertionError('legacy mode must not probe')
    task=RefreshTask(auto_config,Mock(),monotonic=clock.monotonic,utcnow=clock.utcnow,resource_monitor=monitor)
    before=copy.deepcopy(settings(auto_config))
    task._check_resources();task.screen_selection.pause_automatic('chromium_timeout')
    assert task._scheduled_action(clock.utcnow()).plugin_instance.name==W1['instance']
    assert settings(auto_config)==before
    monitor.check.assert_not_called()


def test_later_success_retains_pause_after_initial_write_failure(auto_config, clock, worker, monkeypatch):
    task,plugin=worker
    original=auto_config.update_section
    once=[]
    def update(key,transform):
        # Fail only the first resource-pause transaction, after attempt admission.
        value=copy.deepcopy(auto_config.get_config(key));transform(value)
        if 'resource_pause' in value and not once:
            once.append(True)
            raise OSError('transient pause write failure')
        return original(key,transform)
    monkeypatch.setattr(auto_config,'update_section',update)
    def generate(*args):
        task.screen_selection.pause_automatic('memory_limit',{'max':1})
        assert task.screen_selection.resource_paused
        assert 'resource_pause' not in settings(auto_config)
        return Image.new('RGB',(8,8),'green')
    plugin.generate_image.side_effect=generate
    due(task,clock);finished(task,auto_config,outcome='presented')
    assert once==[True]
    assert Config().get_config('button_navigation')['resource_pause']['reason']=='memory_limit'
    assert settings(auto_config)['display_uncertain'] is False
    clock.advance(100000);assert task._scheduled_action(clock.utcnow()) is None


def test_failed_physical_display_keeps_uncertainty_and_resource_pause_separate(auto_config, clock, worker):
    task,plugin=worker
    def display(*args,**kwargs):
        task.screen_selection.pause_automatic('memory_limit',{'max':1})
        raise RuntimeError('physical IO failed')
    task.display_manager.display_image.side_effect=display
    due(task,clock);finished(task,auto_config,outcome='failed')
    assert settings(auto_config)['display_uncertain'] is True
    pause=copy.deepcopy(settings(auto_config)['resource_pause'])
    # Successful deliberate restoration clears uncertainty, never the pause.
    task.display_manager.display_image.side_effect=None
    assert task.request_button('A')
    wait_until(lambda:not settings(auto_config)['display_uncertain'])
    assert settings(auto_config)['resource_pause']==pause
    assert task._scheduled_action(clock.utcnow()) is None


def test_critical_pre_admission_evidence_prevents_generation(auto_config, clock, monkeypatch):
    plugin=Mock(config={});monkeypatch.setattr('refresh_task.get_plugin_instance',lambda _:plugin)
    monitor=Mock(check=Mock(return_value=('memory_limit',{'max':1})))
    task=RefreshTask(auto_config,Mock(),monotonic=clock.monotonic,utcnow=clock.utcnow,resource_monitor=monitor)
    task.start()
    try:
        wait_until(lambda:'resource_pause' in settings(auto_config))
        due(task,clock)
        assert task._scheduled_action(clock.utcnow()) is None
        plugin.generate_image.assert_not_called()
        assert state(auto_config)=={}
    finally:task.stop()


def test_normal_shutdown_cancellation_is_not_a_chromium_resource_failure(auto_config, clock, worker, monkeypatch, tmp_path):
    task,plugin=worker
    screenshots.reset_screenshot_shutdown()
    monkeypatch.setattr(screenshots.tempfile,'tempdir',str(tmp_path))
    monkeypatch.setattr(screenshots,'_find_chromium_binary',lambda:'/fake/chromium')
    process,_,_=fake_browser(monkeypatch)
    def communicate(**kwargs):
        screenshots.cancel_screenshots()
        return b'',b''
    process.communicate.side_effect=communicate
    def generate(*args):
        if screenshots.take_screenshot('local-fixture',(8,6)) is None:
            raise RuntimeError('cancelled screenshot')
    plugin.generate_image.side_effect=generate
    due(task,clock);finished(task,auto_config,outcome='failed')
    assert 'resource_pause' not in settings(auto_config)
    task.display_manager.display_image.assert_not_called()
    assert not list(tmp_path.glob('inkypi-*'))
    screenshots.reset_screenshot_shutdown()


def test_rapid_pending_c_requests_survive_resource_pause_without_overlap(auto_config, clock, monkeypatch):
    entered,release,hit=threading.Event(),threading.Event(),threading.Event()
    names=[];active=[]
    monitor=Mock();monitor.check.side_effect=lambda:('memory_limit',{'max':1}) if hit.is_set() else None
    plugin=Mock(config={})
    def generate(data,cfg):
        names.append(data['name'])
        return Image.new('RGB',(8,8),'green' if len(names)==1 else 'blue')
    plugin.generate_image.side_effect=generate
    monkeypatch.setattr('refresh_task.get_plugin_instance',lambda _:plugin)
    task=RefreshTask(auto_config,Mock(),monotonic=clock.monotonic,utcnow=clock.utcnow,resource_monitor=monitor)
    def display(*args,**kwargs):
        assert not active
        active.append(True)
        try:
            if len(names)==1:
                entered.set();assert release.wait(3);hit.set()
        finally:active.pop()
    task.display_manager.display_image.side_effect=display
    task.start()
    try:
        due(task,clock);assert entered.wait(3)
        assert task.request_button('C') and task.request_button('C')
        assert settings(auto_config)['selected']==W1
        release.set()
        wait_until(lambda:settings(auto_config)['selected']==W3 and task.screen_selection._active is None)
        assert names==[W1['instance'],W3['instance']]
        assert task.display_manager.display_image.call_count==2
        assert settings(auto_config)['remembered']['weather']==W3
        assert settings(auto_config)['resource_pause']['reason']=='memory_limit'
        clock.advance(100000);assert task._scheduled_action(clock.utcnow()) is None
    finally:release.set();task.stop()


def test_pending_web_ticket_is_released_when_worker_dies(auto_config, clock, monkeypatch):
    entered,release=threading.Event(),threading.Event();errors=[]
    plugin=Mock(config={})
    def generate(*args):
        entered.set();assert release.wait(3)
        raise SystemExit('unexpected native wrapper exit')
    plugin.generate_image.side_effect=generate
    monkeypatch.setattr('refresh_task.get_plugin_instance',lambda _:plugin)
    task=RefreshTask(auto_config,Mock(),monotonic=clock.monotonic,utcnow=clock.utcnow)
    def web():
        try:task.manual_update(ManualRefresh('weather',{'name':W2['instance']}))
        except RuntimeError as error:errors.append(str(error))
    task.start();thread=None
    try:
        due(task,clock);assert entered.wait(3)
        thread=threading.Thread(target=web);thread.start()
        wait_until(lambda:task.manual_update_request is not None)
        release.set();thread.join(3)
        assert not thread.is_alive() and errors==['Refresh worker terminated']
        wait_until(lambda:not task.thread.is_alive())
        assert settings(auto_config)['resource_pause']['reason']=='worker_terminated'
        task.display_manager.display_image.assert_not_called()
    finally:
        release.set();task.stop()
        if thread is not None:thread.join(3)


@pytest.mark.parametrize('failure,reason',[('launch','chromium_launch_failure'),('missing','chromium_unavailable')])
def test_browser_launch_preconditions_persist_pause(auto_config, clock, worker, monkeypatch, failure, reason):
    task,plugin=worker;screenshots.reset_screenshot_shutdown()
    monkeypatch.setattr(screenshots,'_find_chromium_binary',lambda:None if failure=='missing' else '/fake/chromium')
    launch=Mock(side_effect=OSError('cannot start browser'))
    monkeypatch.setattr(screenshots.subprocess,'Popen',launch)
    def generate(*args):
        assert screenshots.take_screenshot('local-fixture',(8,8)) is None
        raise RuntimeError('render failed')
    plugin.generate_image.side_effect=generate
    due(task,clock);finished(task,auto_config,outcome='failed')
    assert settings(auto_config)['resource_pause']['reason']==reason
    task.display_manager.display_image.assert_not_called()
    if failure=='missing':launch.assert_not_called()


def test_forced_browser_group_cleanup_reports_resource_failure(auto_config, clock, monkeypatch):
    import signal
    c=controller(auto_config,clock);process=Mock(pid=987654)
    process.communicate.side_effect=[subprocess.TimeoutExpired('fake',2),(b'',b'')]
    signals=Mock();monkeypatch.setattr(screenshots,'_signal_screenshot_group',signals)
    monkeypatch.setattr(screenshots,'_screenshot_group_exists',lambda _:True)
    ticks=iter([0,3]);monkeypatch.setattr(screenshots.time,'monotonic',lambda:next(ticks))
    with watch_resources(Mock(),c.pause_automatic):screenshots._cleanup_screenshot_process(process)
    assert [x.args[1] for x in signals.call_args_list]==[signal.SIGTERM,signal.SIGKILL]
    assert Config().get_config('button_navigation')['resource_pause']['reason']=='chromium_cleanup_failure'


def test_historical_high_peak_does_not_repause_but_rising_peak_does(cgroup):
    make,_=cgroup;monitor=make(peak=195)
    assert monitor.check() is None
    make(peak=195,current=120)
    assert monitor.check() is None
    make(peak=196,current=120)
    result=monitor.check()
    assert result[0]=='memory_critical'
    assert result[1]['memory_peak_baseline']==195*MIB


def test_new_brief_peak_is_detected_below_sampled_current_threshold(cgroup):
    make,_=cgroup;monitor=make(peak=150)
    assert monitor.check() is None
    make(peak=191,current=120)
    assert monitor.check()[0]=='memory_critical'


def test_current_pressure_detected_below_old_peak(cgroup):
    make,_=cgroup;monitor=make(peak=200,max=349)
    assert monitor.check() is None
    make(peak=200,current=190,max=349)
    result=monitor.check()
    assert result[0]=='memory_critical' and result[1]['max_delta']==0


def test_accounting_reset_rebaselines_without_false_counter_delta(cgroup):
    make,_=cgroup;monitor=make(peak=200,max=349,oom=2)
    assert monitor.check() is None
    make(peak=150,max=1,oom=0)
    assert monitor.check() is None
    make(peak=150,max=2,oom=0)
    result=monitor.check()
    assert result[0]=='memory_limit' and result[1]['max_delta']==1


def test_unreadable_sample_keeps_last_valid_baseline(cgroup, monkeypatch):
    make,path=cgroup;monitor=make(max=349)
    assert monitor.check() is None
    original=Path.read_text
    def denied(p,*args,**kwargs):
        if p==path/'memory.events':raise PermissionError('mock denied')
        return original(p,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'read_text',denied)
        assert monitor.check() is None
    make(max=350)
    result=monitor.check()
    assert result[0]=='memory_limit' and result[1]['max_delta']==1
