"""Synthetic calendar only. No private secrets, real network, browser or hardware."""
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from PIL import Image

from plugins.flight_duties.calendar_data import (schedule_for, InvalidCalendar, ZURICH,
                                                  parse_calendar, clean)
from plugins.flight_duties.data import (cache_path, load_schedule, write_cache, read_cache,
                                       read_secret, validate_url, PrivateSourceError, make_session)
from plugins.flight_duties.rendering import render_dashboard, visible_duties

NOW=datetime.fromisoformat('2026-10-26T10:15:00+01:00')
URL='https://calendar.example.invalid/calendar.ics?id=SYNTHETIC_SECRET'
RAW=Path('tests/fixtures/flight_duties/synthetic.ics').read_bytes()


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    monkeypatch.setattr('requests.sessions.Session.request',Mock(side_effect=AssertionError('Network forbidden')))
    monkeypatch.setattr('subprocess.Popen',Mock(side_effect=AssertionError('No browser or service access')))


def event(body):
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n'+body+
            '\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode()


def one(start='20261026T110000',end='20261026T120000',extra=''):
    return event('UID:fake@example.invalid\r\nDTSTART;TZID=Europe/Zurich:'+start+
                 '\r\nDTEND;TZID=Europe/Zurich:'+end+'\r\nSUMMARY:Fake lesson\r\n'+extra)


def response_session(raw=RAW,status=200):
    response=Mock(status_code=status)
    response.iter_content.return_value=iter([raw])
    response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
    session=Mock();session.get.return_value=response
    return session


def report(source='live',raw=RAW):
    return dict(schedule=schedule_for(raw,NOW),source=source,fetched_at=NOW,error=None)


def test_fields_folded_escaped_unknown_status_and_reminders():
    s=schedule_for(RAW,NOW)
    assert len(s.duties)==7 and len(s.reminders)==1 and s.cancelled==1
    assert s.day==NOW.date()
    assert s.duties[0].start.strftime('%H:%M')=='07:30'
    assert s.duties[1].start.strftime('%H:%M')=='09:00' # UTC converted to Zurich
    assert s.duties[0].activity=='Training flight, circuits'
    assert s.duties[1].activity=='Navigation exercise; cross-country'
    assert 'fordeterministic' not in s.duties[2].activity
    assert 'deterministic folded-line' in s.duties[2].activity
    assert s.duties[0].aircraft=='HB-AAA'
    assert s.duties[0].participants=='Student: Alex Example · Instructor: Taylor Demo'
    assert s.duties[3].participants=='Alex Example'
    assert s.duties[0].status=='UNKNOWN'
    assert s.duties[1].status=='TENTATIVE' and s.duties[3].status=='CONFIRMED'
    assert s.reminders[0].end-s.reminders[0].start==timedelta(days=1)


@pytest.mark.parametrize('day,offset', [('20260328',1),('20260329',2),('20261024',2),('20261025',1)])
def test_dst_local_timezone(day,offset):
    now=datetime.strptime(day,'%Y%m%d').replace(tzinfo=ZURICH)
    s=schedule_for(one(day+'T110000',day+'T120000'),now)
    assert s.duties[0].start.hour==11 and s.duties[0].start.utcoffset()==timedelta(hours=offset)


def test_overnight_overlap_and_half_open_day():
    s=schedule_for(one('20261025T230000','20261026T013000'),NOW)
    assert len(s.duties)==1
    assert not schedule_for(one('20261025T230000','20261026T000000'),NOW).duties
    assert not schedule_for(one('20261027T000000','20261027T010000'),NOW).duties


def test_floating_time_is_explicitly_zurich():
    data=one().replace(b';TZID=Europe/Zurich',b'')
    assert schedule_for(data,NOW).duties[0].start.hour==11


def test_recurring_exception_cancellation_and_exdate():
    data=b'''BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:fake-series@example.invalid
DTSTART;TZID=Europe/Zurich:20261025T110000
DTEND;TZID=Europe/Zurich:20261025T120000
RRULE:FREQ=DAILY;COUNT=4
EXDATE;TZID=Europe/Zurich:20261028T110000
SUMMARY:Synthetic recurring training
END:VEVENT
BEGIN:VEVENT
UID:fake-series@example.invalid
RECURRENCE-ID;TZID=Europe/Zurich:20261026T110000
DTSTART;TZID=Europe/Zurich:20261026T130000
DTEND;TZID=Europe/Zurich:20261026T140000
SUMMARY:Synthetic moved instance
END:VEVENT
BEGIN:VEVENT
UID:fake-series@example.invalid
RECURRENCE-ID;TZID=Europe/Zurich:20261027T110000
DTSTART;TZID=Europe/Zurich:20261027T110000
DTEND;TZID=Europe/Zurich:20261027T120000
STATUS:CANCELLED
SUMMARY:Synthetic cancelled instance
END:VEVENT
END:VCALENDAR'''
    s=schedule_for(data,NOW);assert len(s.duties)==1 and s.duties[0].start.hour==13
    tomorrow=schedule_for(data,NOW+timedelta(days=1));assert not tomorrow.duties and tomorrow.cancelled==1
    assert not schedule_for(data,NOW+timedelta(days=2)).duties


def test_rdate_and_duration():
    data=event('UID:fake\r\nDTSTART:20261025T090000Z\r\nDURATION:PT1H\r\nRDATE:20261026T090000Z\r\nSUMMARY:Synthetic extra occurrence')
    s=schedule_for(data,NOW);assert len(s.duties)==1 and s.duties[0].start.hour==10


@pytest.mark.parametrize('data', [b'<html>Not a calendar</html>',b'BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VEVENT\nEND:VCALENDAR',
    one(end='20261026T100000'),one(extra='RRULE:FREQ=SECONDLY;COUNT=999999'),
    one().replace(b'Europe/Zurich',b'Unknown/Timezone'),
    one().replace(b'BEGIN:VCALENDAR',b'BEGIN:VCALENDAR\nMETHOD:CANCEL')])
def test_invalid_or_impractical_calendar_is_not_empty_success(data):
    with pytest.raises(InvalidCalendar):schedule_for(data,NOW)


def test_links_html_and_control_characters_not_displayed():
    value=clean('<b>Student:</b> Alex Example<br>Info: https://private.invalid/?id=FAKE\n\x00')
    assert 'Student: Alex Example' in value and 'FAKE' not in value and 'private.invalid' not in value and '\x00' not in value


def test_successful_empty_calendar_and_other_day():
    raw=b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR'
    assert not schedule_for(raw,NOW).duties
    assert not schedule_for(one('20261027T110000','20261027T120000'),NOW).duties


def test_private_atomic_cache_and_live_fetch(tmp_path):
    session=response_session();r=load_schedule(session,URL,tmp_path/'cache',NOW)
    assert r['source']=='live' and len(r['schedule'].duties)==7
    path=cache_path(tmp_path/'cache',URL)
    assert path.stat().st_mode & 0o777==0o600
    assert path.parent.stat().st_mode & 0o777==0o700
    assert read_cache(path,NOW)[0]==RAW
    assert URL not in path.read_text() and 'SYNTHETIC_SECRET' not in path.read_text()
    assert not list(path.parent.glob('.snapshot-*'))
    assert session.get.call_args.kwargs==dict(timeout=(3,8),stream=True,allow_redirects=False)


@pytest.mark.parametrize('failure',[requests.ReadTimeout('private url and personal text'),requests.ConnectTimeout('private url'),RuntimeError('secret personal data')])
def test_network_failure_cached_data_and_redacted_logs(tmp_path,caplog,failure):
    path=cache_path(tmp_path,URL);write_cache(path,RAW,NOW)
    session=Mock();session.get.side_effect=failure
    with caplog.at_level(logging.DEBUG):r=load_schedule(session,URL,tmp_path,NOW+timedelta(minutes=5))
    assert r['source']=='cached' and r['fetched_at']==NOW
    assert r['error']=='Calendar request failed'
    assert 'personal' not in caplog.text and URL not in caplog.text and 'SYNTHETIC_SECRET' not in caplog.text
    assert read_cache(path,NOW)[0]==RAW


@pytest.mark.parametrize('status',[302,403,429,500])
def test_http_failure_no_retry_or_redirect_and_no_empty_success(tmp_path,status):
    session=response_session(status=status);r=load_schedule(session,URL,tmp_path,NOW)
    assert r['source']=='unavailable' and r['schedule'] is None and r['error']=='HTTP '+str(status)
    session.get.assert_called_once()


def test_transport_debug_cannot_log_url(tmp_path,caplog):
    session=response_session()
    response=session.get.return_value
    def get(*a,**kw):
        logging.getLogger('urllib3.connectionpool').debug('GET %s',URL)
        return response
    session.get.side_effect=get
    with caplog.at_level(logging.DEBUG):load_schedule(session,URL,tmp_path,NOW)
    assert URL not in caplog.text and 'SYNTHETIC_SECRET' not in caplog.text
    assert 'suppressed' in caplog.text


def test_invalid_response_preserves_last_good_snapshot(tmp_path):
    path=cache_path(tmp_path,URL);write_cache(path,RAW,NOW);before=path.read_bytes()
    result=load_schedule(response_session(b'bad response'),URL,tmp_path,NOW+timedelta(minutes=3))
    assert result['source']=='cached' and result['error']=='Invalid calendar' and path.read_bytes()==before


def test_cache_old_corrupt_future_and_public_permissions(tmp_path):
    path=cache_path(tmp_path,URL)
    for when in (NOW-timedelta(hours=25),NOW+timedelta(minutes=1)):
        write_cache(path,RAW,when);assert read_cache(path,NOW) is None
    write_cache(path,RAW,NOW);path.chmod(0o644);assert read_cache(path,NOW) is None
    path.write_text('broken');path.chmod(0o600);assert read_cache(path,NOW) is None
    assert cache_path(tmp_path,URL)!=cache_path(tmp_path,URL+'-other-account')


def test_cached_snapshot_recomputed_for_today(tmp_path):
    path=cache_path(tmp_path,URL);write_cache(path,RAW,NOW)
    session=Mock();session.get.side_effect=requests.ConnectionError()
    r=load_schedule(session,URL,tmp_path,NOW+timedelta(hours=14))
    assert r['source']=='cached' and not r['schedule'].duties and r['schedule'].day==NOW.date()+timedelta(days=1)


def test_live_data_survives_cache_write_failure(tmp_path,monkeypatch):
    monkeypatch.setattr('plugins.flight_duties.data.write_cache',Mock(side_effect=OSError('private path')))
    r=load_schedule(response_session(),URL,tmp_path,NOW)
    assert r['source']=='live' and r['error']=='Cache not saved'


def test_secret_environment_and_private_file(tmp_path):
    device=Mock();device.load_env_key.side_effect=lambda name: URL if name=='FLIGHTLOGGER_ICAL_URL' else None
    assert read_secret(device)==URL
    path=tmp_path/'url';path.write_text(URL);path.chmod(0o600)
    device.load_env_key.side_effect=lambda name:str(path) if name.endswith('_FILE') else None
    assert read_secret(device)==URL
    path.chmod(0o644)
    with pytest.raises(PrivateSourceError):read_secret(device)


@pytest.mark.parametrize('url',['http://private.invalid/feed','https://name:pass@private.invalid/feed','https://private.invalid/#secret','not a url'])
def test_invalid_secret_never_printed(url):
    with pytest.raises(PrivateSourceError) as e:validate_url(url)
    assert url not in str(e.value)


def test_session_has_no_retries():
    with make_session() as s:
        assert s.get_adapter('https://').max_retries.total==0 and not s.trust_env


@pytest.mark.parametrize('source',['live','cached','unavailable'])
def test_renderer_rgb_deterministic_states_and_inputs(source):
    r=report(source);r['schedule']=None if source=='unavailable' else r['schedule']
    with render_dashboard(r,NOW) as a,render_dashboard(r,NOW) as b:
        assert a.size==(800,480) and a.mode=='RGB' and a.tobytes()==b.tobytes()
    assert r['source']==source


def test_next_duty_visible_overflow_and_empty_state_text(monkeypatch):
    s=schedule_for(RAW,NOW);shown,start,focus=visible_duties(s.duties,NOW+timedelta(hours=7),5)
    assert any(d.start>=NOW+timedelta(hours=7) for d in shown) and len(shown)==5 and start>0
    captured=[]
    from PIL import ImageDraw
    original=ImageDraw.ImageDraw.text
    def text(self,xy,value,*a,**kw):captured.append(value);return original(self,xy,value,*a,**kw)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',text)
    empty=report(raw=b'BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR')
    with render_dashboard(empty,NOW):pass
    assert 'No duties today' in captured
    captured.clear()
    with render_dashboard(dict(schedule=None,source='unavailable',error='No calendar'),NOW):pass
    assert 'No duties today' not in captured and 'Schedule unavailable' in captured


def test_registration_plugin_secret_failure_and_icon(monkeypatch,tmp_path):
    from plugins.flight_duties.flight_duties import FlightDuties
    info=json.loads(Path('src/plugins/flight_duties/plugin-info.json').read_text())
    assert info['class']=='FlightDuties' and info['id']=='flight_duties'
    device=Mock();device.get_resolution.return_value=(800,480);device.get_config.return_value='horizontal';device.load_env_key.return_value=None
    device.plugin_image_dir=str(tmp_path)
    with FlightDuties(info).generate_image({},device) as image:
        assert image.info['inkypi_data_status']=='unavailable' and not image.info['inkypi_live_data_success']
    with Image.open('src/plugins/flight_duties/icon.png') as icon:assert icon.size==(512,512) and icon.mode=='RGBA'


def test_cache_replacement_failure_keeps_good_file_and_cleans_temporary(tmp_path,monkeypatch):
    path=cache_path(tmp_path,URL);write_cache(path,RAW,NOW);before=path.read_bytes()
    monkeypatch.setattr('plugins.flight_duties.data.os.replace',Mock(side_effect=OSError('disk failure')))
    with pytest.raises(OSError):write_cache(path,one(),NOW+timedelta(minutes=5))
    assert path.read_bytes()==before and not list(tmp_path.glob('.snapshot-*'))


def test_unknown_status_missing_aircraft_and_explicit_participant():
    s=schedule_for(one(extra='STATUS:PENDING\r\nDESCRIPTION:Aircraft: HB-FAK\\nStudent: Alex Example'),NOW)
    assert s.duties[0].status=='UNKNOWN' and s.duties[0].aircraft=='HB-FAK'
    assert s.duties[0].participants=='Student: Alex Example'


def test_feed_and_occurrence_bounds(monkeypatch):
    from plugins.flight_duties.calendar_data import MAX_BYTES
    with pytest.raises(InvalidCalendar):schedule_for(b'x'*(MAX_BYTES+1),NOW)
    monkeypatch.setattr('plugins.flight_duties.calendar_data.MAX_OCCURRENCES',2)
    with pytest.raises(InvalidCalendar,match='Occurrence limit'):schedule_for(RAW,NOW)


def test_secret_symlink_is_rejected(tmp_path):
    target=tmp_path/'secret';target.write_text(URL);target.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(target)
    device=Mock();device.load_env_key.side_effect=lambda n:str(link) if n.endswith('_FILE') else None
    with pytest.raises(PrivateSourceError):read_secret(device)


def test_plugin_discovery_live_and_cached_annotations(tmp_path,monkeypatch):
    from plugins import plugin_registry
    info=json.loads(Path('src/plugins/flight_duties/plugin-info.json').read_text())
    monkeypatch.setattr(plugin_registry,'PLUGIN_CLASSES',{})
    plugin_registry.load_plugins([info]);plugin=plugin_registry.get_plugin_instance(info)
    device=Mock();device.get_resolution.return_value=(800,480);device.get_config.return_value='horizontal';device.plugin_image_dir=str(tmp_path/'static/images/plugins');device.config_file=str(tmp_path/'config/device.json')
    monkeypatch.setattr('plugins.flight_duties.flight_duties.read_secret',Mock(return_value=URL))
    for source,status in [('live','live'),('cached','cached_stale')]:
        load=Mock(return_value=report(source))
        monkeypatch.setattr('plugins.flight_duties.flight_duties.load_schedule',load)
        with plugin.generate_image({},device) as image:
            assert image.info['inkypi_data_status']==status
            assert image.info['inkypi_live_data_success']==(source=='live')
        assert load.call_args.args[2]==tmp_path/'config/.flight-duties-cache'
        assert 'static' not in load.call_args.args[2].parts


def test_title_overflow_five_rows_and_monochrome():
    with render_dashboard(report(),NOW,{'title':'TEST: '+('Long fake title '*30),'max_bookings':'5'}) as image:
        assert image.size==(800,480)
        with image.convert('1') as mono:assert mono.getextrema()==(0,255)
