"""Synthetic active-screen scheduling; no calendar requests or hardware IO."""
import copy
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock

import pytest
from PIL import Image

from config import Config
from model import PluginInstance
from refresh_task import RefreshTask
from screen_selection import ScreenSelectionController
from tests.test_active_screen_schedule import auto_config,clock,controller,state,finished
from tests.test_location_navigation import location_config,settings,wait_until,W1,M1
from tests.test_flight_duties import URL,RAW,NOW,one,response_session
from plugins.flight_duties.data import load_schedule,write_cache,cache_path
from plugins.flight_duties.calendar_data import ZURICH

D=dict(playlist='Default',plugin_id='flight_duties',instance='Flight Duties')


@pytest.fixture
def duties_config(auto_config):
    cfg=auto_config
    cfg.plugins_list.append({'id':'flight_duties'})
    cfg.playlist_manager.get_playlist('Default').plugins.append(
        PluginInstance('flight_duties','Flight Duties',{'name':'Flight Duties'},{}))
    settings(cfg)['groups']['flight_duties']=dict(instances=[D],interval_seconds=900,auto_refresh_enabled=True)
    settings(cfg)['assignments']['D']=D
    settings(cfg)['selected']=D
    cfg.write_config()
    return cfg


@pytest.fixture
def duties_worker(duties_config,clock,monkeypatch):
    plugin=Mock(config={})
    def generate(*args):
        image=Image.new('RGB',(8,8),'blue')
        image.info.update(inkypi_data_status='live',inkypi_live_data_success=True)
        return image
    plugin.generate_image.side_effect=generate
    monkeypatch.setattr('refresh_task.get_plugin_instance',lambda _:plugin)
    task=RefreshTask(duties_config,Mock(),monotonic=clock.monotonic,utcnow=clock.utcnow)
    task.start()
    try:yield task,plugin
    finally:task.stop()


def test_flight_duties_default_interval_and_selected_only(duties_config,clock):
    cfg=duties_config;n=settings(cfg);n['groups']['flight_duties'].pop('interval_seconds')
    c=controller(cfg,clock);clock.advance(899);assert c.automatic_target() is None
    clock.advance(1);assert c.automatic_target()[1].plugin_id=='flight_duties'
    for target in [W1,M1]:
        n['selected']=target
        assert c.automatic_target()[1].plugin_id==target['plugin_id']
    n['selected']=D
    assert not c.request_button('C') # scheduling support does not add contextual D cycling


@pytest.mark.parametrize('gate',['manual_only','displaced','display_uncertain','resource_pause','disabled','missing_permission','pending'])
def test_flight_duties_safety_gates(duties_config,clock,gate):
    n=settings(duties_config)
    if gate in ['manual_only','displaced','display_uncertain']:n[gate]=True
    elif gate=='resource_pause':n[gate]={'reason':'memory_critical'}
    elif gate=='disabled':n['groups']['flight_duties']['auto_refresh_enabled']=False
    elif gate=='missing_permission':n['groups']['flight_duties'].pop('auto_refresh_enabled')
    c=controller(duties_config,clock)
    if gate=='pending':assert c.request_button('A')
    clock.advance(900);assert c.automatic_target() is None


@pytest.mark.parametrize('status,live',[('live',True),('cached_stale',False),('unavailable',False)])
def test_journal_distinguishes_live_from_cached_and_unavailable(duties_config,clock,duties_worker,status,live):
    task,plugin=duties_worker;before=copy.deepcopy(settings(duties_config).get('remembered'))
    def render(*args):
        image=Image.new('RGB',(8,8),'blue');image.info.update(inkypi_data_status=status,inkypi_live_data_success=live);return image
    plugin.generate_image.side_effect=render
    clock.advance(900);task.signal_config_change();finished(task,duties_config,'flight_duties','presented')
    timing=state(duties_config,'flight_duties')
    assert timing['data_status']==status and bool(timing.get('live_data_success_at')) is live
    assert timing['presentation_success_at'] and timing['attempt_completed_at']
    assert settings(duties_config)['remembered']==before and settings(duties_config)['selected']==D
    assert task.display_manager.display_image.call_count==1 and task._scheduled_action(clock.utcnow()) is None
    clock.advance(899);assert task._scheduled_action(clock.utcnow()) is None
    clock.advance(1);assert task._scheduled_action(clock.utcnow()).plugin_instance.plugin_id=='flight_duties'


def test_failed_duties_generation_paced_across_restart(duties_config,clock,duties_worker):
    task,plugin=duties_worker;plugin.generate_image.side_effect=RuntimeError('synthetic network failure')
    clock.advance(900);task.signal_config_change();finished(task,duties_config,'flight_duties','failed')
    task.display_manager.display_image.assert_not_called()
    assert settings(duties_config)['selected']==D and not settings(duties_config).get('resource_pause')
    task.stop();reloaded=Config();reloaded.plugins_list.append({'id':'flight_duties'});restarted=controller(reloaded,clock)
    assert restarted.automatic_target() is None
    clock.advance(899);assert restarted.automatic_target() is None
    clock.advance(1);assert restarted.automatic_target()[1].plugin_id=='flight_duties'


def test_interrupted_duties_attempt_pauses_restart(duties_config,clock):
    c=controller(duties_config,clock);c.begin_attempt(D)
    recovered=controller(Config(),clock);recovered.recover_interrupted_attempt()
    clock.advance(900);assert recovered.automatic_target() is None
    assert settings(recovered.device_config)['resource_pause']['reason']=='interrupted_attempt'


def test_unchanged_duties_image_has_no_second_display(duties_config,clock,duties_worker):
    task,plugin=duties_worker
    clock.advance(900);task.signal_config_change();finished(task,duties_config,'flight_duties','presented')
    clock.advance(900);task.signal_config_change();finished(task,duties_config,'flight_duties','unchanged')
    assert plugin.generate_image.call_count==2 and task.display_manager.display_image.call_count==1
    assert state(duties_config,'flight_duties')['live_data_success_at']


@pytest.mark.parametrize('day',['20260329','20261025','20261027'])
@pytest.mark.parametrize('failure',[False,True])
def test_fetch_crossing_zurich_midnight_uses_new_day(tmp_path,day,failure):
    after=datetime.strptime(day,'%Y%m%d').replace(tzinfo=ZURICH)+timedelta(seconds=1)
    before=after-timedelta(seconds=2)
    raw=one(before.strftime('%Y%m%d')+'T110000',before.strftime('%Y%m%d')+'T120000')
    if failure:
        write_cache(cache_path(tmp_path,URL),raw,before)
        session=Mock();session.get.side_effect=RuntimeError('synthetic failure')
    else:session=response_session(raw)
    result=load_schedule(session,URL,tmp_path,before,clock=lambda:after)
    assert result['schedule'].day==after.date() and not result['schedule'].duties
    assert result['evaluated_at']==after and result['source']==('cached' if failure else 'live')
    if failure:assert result['fetched_at']==before


@pytest.mark.parametrize('instant,hour,offset',[('2026-03-29T00:30:00+00:00',1,1),('2026-03-29T01:30:00+00:00',3,2),('2026-10-25T00:30:00+00:00',2,2),('2026-10-25T01:30:00+00:00',2,1)])
def test_cadence_is_monotonic_across_dst(duties_config,clock,instant,hour,offset):
    clock.wall=datetime.fromisoformat(instant)
    assert clock.wall.astimezone(ZURICH).hour==hour
    assert clock.wall.astimezone(ZURICH).utcoffset()==timedelta(hours=offset)
    c=controller(duties_config,clock);clock.advance(900)
    assert c.automatic_target()[1].plugin_id=='flight_duties'


def test_plugin_renders_with_post_fetch_local_day(tmp_path,monkeypatch):
    from plugins.flight_duties.flight_duties import FlightDuties
    after=datetime(2026,10,27,0,0,1,tzinfo=ZURICH)
    result=load_schedule(response_session(RAW),URL,tmp_path,NOW,clock=lambda:after)
    monkeypatch.setattr('plugins.flight_duties.flight_duties.read_secret',lambda _:URL)
    monkeypatch.setattr('plugins.flight_duties.flight_duties.load_schedule',Mock(return_value=result))
    render=Mock(return_value=Image.new('RGB',(800,480)))
    monkeypatch.setattr('plugins.flight_duties.flight_duties.render_dashboard',render)
    cfg=Mock(config_file=str(tmp_path/'device.json'));cfg.get_resolution.return_value=(800,480);cfg.get_config.return_value='horizontal'
    with FlightDuties({'id':'flight_duties'}).generate_image({},cfg):pass
    assert render.call_args.args[1]==after
    assert render.call_args.args[0]['schedule'].day==after.date()
