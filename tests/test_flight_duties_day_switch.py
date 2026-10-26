"""Synthetic school planning-day policy and dashboard hierarchy."""
from datetime import datetime,timedelta
from unittest.mock import Mock
import pytest
from PIL import ImageDraw
from plugins.flight_duties.calendar_data import display_day,schedule_for,ZURICH
from plugins.flight_duties.data import load_schedule,write_cache,cache_path
from plugins.flight_duties.rendering import render_dashboard
from tests.test_flight_duties import NOW,RAW,URL,response_session,one,report


@pytest.mark.parametrize('at,expected',[('2026-10-26T17:59:59+01:00','2026-10-26'),('2026-10-26T18:00:00+01:00','2026-10-27'),('2026-10-26T23:59:59+01:00','2026-10-27'),('2026-10-27T00:00:00+01:00','2026-10-27'),('2026-03-29T15:59:59+00:00','2026-03-29'),('2026-03-29T16:00:00+00:00','2026-03-30'),('2026-10-25T16:59:59+00:00','2026-10-25'),('2026-10-25T17:00:00+00:00','2026-10-26'),('2026-12-31T18:00:00+01:00','2027-01-01'),('2028-02-28T18:00:00+01:00','2028-02-29')])
def test_zurich_1800_boundary_dst_and_month_year(at,expected):
    assert display_day(datetime.fromisoformat(at)).isoformat()==expected


def test_naive_policy_time_rejected():
    with pytest.raises(ValueError):display_day(datetime(2026,10,26,18))


@pytest.mark.parametrize('source',['live','cached'])
def test_fetch_crossing_1800_selects_tomorrow_and_retains_cache_provenance(tmp_path,source):
    before=NOW.replace(hour=17,minute=59,second=59);after=before+timedelta(seconds=2)
    tomorrow=RAW.replace(b'20261026',b'20261027').replace(b'20261025',b'20261026')
    data=tomorrow
    # Tomorrow-only fixture makes today's accidental expansion observably empty.
    if source=='cached':
        write_cache(cache_path(tmp_path,URL),data,before)
        session=Mock();session.get.side_effect=RuntimeError('synthetic offline')
    else:session=response_session(data)
    result=load_schedule(session,URL,tmp_path,before,clock=lambda:after)
    assert result['source']==source and result['schedule'].day==after.date()+timedelta(days=1)
    assert len(result['schedule'].duties)==7 and result['evaluated_at']==after
    assert result['fetched_at']==(before if source=='cached' else after)


def test_same_planning_day_survives_midnight():
    evening=NOW.replace(hour=23,minute=59,second=59);midnight=evening+timedelta(seconds=2)
    raw=one('20261027T110000','20261027T120000')
    before=schedule_for(raw,evening);after=schedule_for(raw,midnight)
    assert before.day==after.day and before.duties==after.duties


@pytest.fixture
def labels(monkeypatch):
    captured=[];original=ImageDraw.ImageDraw.text
    def text(self,xy,value,*args,**kwargs):
        captured.append((xy,value,kwargs.get('font').size));return original(self,xy,value,*args,**kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',text)
    return captured


@pytest.mark.parametrize('hour,word',[(17,'today'),(18,'tomorrow'),(23,'tomorrow')])
@pytest.mark.parametrize('source',['live','cached'])
def test_relative_day_header_and_unambiguous_empty_state(hour,word,source,labels):
    now=NOW.replace(hour=hour);raw=b'BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR'
    result=dict(schedule=schedule_for(raw,now),source=source,fetched_at=NOW,error=None)
    with render_dashboard(result,now) as image:assert image.size==(800,480) and image.mode=='RGB'
    values=[value for _,value,_ in labels]
    assert word.upper() in values and 'No duties '+word in values
    if source=='cached':assert 'CACHED' in values and any('STALE' in v for v in values)


def test_featured_time_aircraft_hierarchy_reminders_and_overflow(labels):
    with render_dashboard(report(),NOW,{'max_bookings':5}):pass
    assert any(v=='11:00' and size==42 for _,v,size in labels)
    assert any(v.startswith('NEXT DUTY') for _,v,_ in labels)
    assert any(v.startswith('HB-') and size==29 for _,v,size in labels)
    assert any('5 shown' in v and '2 more' in v for _,v,_ in labels)
    assert next(xy[1] for xy,v,_ in labels if v=='REMINDERS')>=423
    assert not any('not separately supplied' in v for _,v,_ in labels)


def test_wrong_day_input_fails_closed_instead_of_relabelling_duties(labels):
    with render_dashboard(report(),NOW.replace(hour=18)):pass
    values=[v for _,v,_ in labels]
    assert 'TOMORROW' in values and 'Schedule unavailable' in values
    assert not any(v.startswith('HB-') for v in values)


@pytest.mark.parametrize('limit',[4,5,6,7,None,'invalid'])
def test_design_limits_long_text_and_determinism(limit):
    options={'max_bookings':limit,'title':'TEST: '+('Long title '*100)}
    with render_dashboard(report(),NOW,options) as a,render_dashboard(report(),NOW,options) as b:
        assert a.mode=='RGB' and a.size==(800,480) and a.tobytes()==b.tobytes()


def test_legacy_six_rows_retain_status_and_participant_lines(labels):
    with render_dashboard(report(),NOW,{'max_bookings':7}):pass
    assert len([v for xy,v,size in labels if xy[1]>=217 and v in ('Status unknown','Tentative','Confirmed')])==5
    assert len([v for xy,v,size in labels if 217<=xy[1]<415 and size==13 and v])>=3
