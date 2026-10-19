"""Offline Weather model/renderer tests; no browser, network or hardware operations."""
import copy
import hashlib
import importlib.util
import json
import math
from dataclasses import asdict, replace, FrozenInstanceError
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image
from plugins.weather.dashboard import from_parsed
from plugins.weather.pillow_rendering import (
    render_dashboard, Painter, curve_points, rain_annotations, ICON_DIR)

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('weather_pillow_fixture',REPO/'scripts/weather_pillow_fixture.py')
fixture_module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    deny=Mock(side_effect=AssertionError('Prototype must remain offline/browser-free'))
    monkeypatch.setattr('requests.sessions.Session.request',deny)
    monkeypatch.setattr('subprocess.Popen',deny)
    monkeypatch.setattr('socket.socket.connect',deny)
    yield deny
    deny.assert_not_called()


@pytest.fixture
def fixture():
    return fixture_module.load_fixture()


def test_existing_parser_output_and_explicit_time(fixture):
    model,settings,parsed=fixture
    assert model.title=='TEST: Little Salmon'
    assert model.current_temperature=='7' and model.feels_like=='5'
    assert model.temperature_unit=='°C' and model.last_refresh_time=='2026-10-18 12:00'
    assert model.current_date=='Sunday, October 18'
    assert Path(model.current_day_icon).name=='51d.png'
    assert [(p.label,p.measurement,p.unit,p.arrow) for p in model.data_points]==[
        ('Sunrise','08:00','',''),('Sunset','18:00','',''),('Wind',3.5,'m/s','↗'),
        ('Humidity',82,'%',''),('Pressure',1014,'hPa',''),('UV Index',3.0,'',''),
        ('Visibility','≥10.0','km',''),('Air Quality',17,'Good','')]
    for name in ('current_temperature','feels_like','current_day_icon','temperature_unit','units','current_date'):
        assert getattr(model,name)==parsed[name]
    assert [asdict(p) for p in model.data_points]==[dict(p,arrow=p.get('arrow','')) for p in parsed['data_points']]
    assert [asdict(p) for p in model.forecast]==parsed['forecast']
    assert [asdict(p) for p in model.hourly_forecast]==parsed['hourly_forecast']
    assert [p.day for p in model.forecast[1:]]==['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
    assert [(p.high,p.low,p.moon_phase_pct) for p in model.forecast[1:]]==[
        (8,2,'54'),(7,0,'63'),(6,-1,'71'),(4,-3,'80'),(8,1,'87'),(9,3,'93'),(5,0,'97')]
    assert len(model.hourly_forecast)==24
    assert model.hourly_forecast[0].time=='12:00'
    assert model.hourly_forecast[-1].time=='11:00'


def test_model_is_snapshot(fixture):
    model,_,parsed=fixture
    parsed['data_points'][2]['measurement']=999
    parsed['forecast'].clear()
    assert model.data_points[2].measurement==3.5 and len(model.forecast)==8
    with pytest.raises(FrozenInstanceError):model.title='changed'


def test_aware_time_required_and_12h_format(fixture):
    _,_,parsed=fixture
    with pytest.raises(ValueError):from_parsed(parsed,rendered_at=datetime(2026,10,18,12))
    parsed['time_format']='12h'
    m=from_parsed(parsed,rendered_at=datetime.fromisoformat('2026-10-18T12:00:00+02:00'))
    assert m.last_refresh_time=='2026-10-18 12:00 PM'


def test_rgb_deterministic_and_inputs_unchanged(fixture):
    model,settings,_=fixture
    before=(asdict(model),copy.deepcopy(settings))
    with render_dashboard(model,settings) as a, render_dashboard(model,settings) as b:
        assert a.size==(800,480) and a.mode=='RGB'
        assert a.tobytes()==b.tobytes()
        assert len(a.getcolors(800*480))>6
    assert (asdict(model),settings)==before


@pytest.mark.parametrize('title',['Lugano','TEST: '+('Long location name / '*50),'東京 — São Tomé — Zürich'])
def test_long_titles_remain_inside_field(fixture,title):
    model,settings,_=fixture
    with render_dashboard(replace(model,title=title),settings) as image:assert image.size==(800,480)
    with Image.new('RGB',(300,60),'white') as image:
        Painter(image,'black').text((20,10,150,40),title,28,True,minimum=16)
        with image.convert('L') as grey, Image.eval(grey,lambda x:255-x) as mask:
            ink=mask.getbbox()
        assert ink is None or (ink[0]>=20 and ink[1]>=10 and ink[2]<=150 and ink[3]<=40)


def test_too_narrow_text_never_bleeds():
    with Image.new('RGB',(100,50),'white') as image:
        p=Painter(image,'black')
        p.text((10,10,12,30),'a very long string',20)
        p.text((20,10,19,30),'negative width',20)
        assert image.getextrema()==((255,255),)*3


@pytest.mark.parametrize('which',['metrics','current','daily','hourly','all'])
def test_missing_presentation_data_not_invented(fixture,which):
    model,settings,parsed=fixture
    if which in ('metrics','all'):parsed['data_points']=[]
    if which in ('current','all'):
        for key in ['current_day_icon','current_temperature','feels_like']:parsed.pop(key,None)
    if which in ('daily','all'):parsed['forecast']=[]
    if which in ('hourly','all'):parsed['hourly_forecast']=[]
    m=from_parsed(parsed,rendered_at=model.rendered_at)
    with render_dashboard(m,settings) as image:assert image.mode=='RGB'
    if which in ('current','all'):assert m.current_temperature is None


def test_provider_failure_not_replaced_with_fake_conditions(fixture):
    from plugins.weather.weather import Weather
    import pytz
    with pytest.raises((TypeError,KeyError,ValueError,IndexError)):
        Weather({'id':'weather'}).parse_open_meteo_data({}, {},pytz.UTC,'metric','24h',46)
    m=from_parsed({},rendered_at=fixture[0].rendered_at)
    assert m.current_temperature is None and m.data_points==() and m.forecast==()


@pytest.mark.parametrize('temperature',[0,-30,42])
def test_constant_zero_negative_temperatures(fixture,temperature):
    model,settings,_=fixture
    hours=tuple(replace(h,temperature=temperature) for h in model.hourly_forecast)
    with render_dashboard(replace(model,hourly_forecast=hours,current_temperature=str(temperature)),settings) as im:
        assert im.size==(800,480)


@pytest.mark.parametrize('probability,rain',[(0,0),(1,120.5),(-1,-2),(2,1000000),(None,None),(float('nan'),float('inf'))])
def test_rain_extremes_bounded(fixture,probability,rain):
    model,settings,_=fixture
    hours=tuple(replace(h,precipitation=probability,rain=rain) for h in model.hourly_forecast)
    with render_dashboard(replace(model,hourly_forecast=hours),settings) as im:assert im.size==(800,480)
    assert hours[0].rain is rain or hours[0].rain==rain


def test_annotation_collisions_and_plot_bounds(fixture):
    hours=[replace(h,rain=1.23) for h in fixture[0].hourly_forecast]
    labels=rain_annotations(hours,[10+10*i for i in range(24)],[5]*24,70,(0,0,250,70))
    assert 0<len(labels)<24
    for i,(a,_,_,_) in enumerate(labels):
        assert 0<=a[0]<a[2]<=250 and 0<=a[1]<a[3]<=70
        for b,*_ in labels[i+1:]:
            assert a[2]<=b[0] or b[2]<=a[0] or a[3]<=b[1] or b[3]<=a[1]


def test_rain_threshold_and_imperial(fixture):
    hour=fixture[0].hourly_forecast[0]
    assert rain_annotations([replace(hour,rain=.09)],[50],[20],80,(0,0,100,80))==[]
    labels=rain_annotations([replace(hour,rain=.004)],[50],[20],80,(0,0,100,80),imperial=True)
    assert labels[0][2]=='in'


@pytest.mark.parametrize('points',[
    [(0,0),(10,10),(20,-10),(30,0)],[(0,0),(10,0)],[(0,-30),(10,-20),(20,-25)],[(0,0)]])
def test_curve_interpolates_without_overshoot(points):
    curve=curve_points(points)
    assert curve[0]==points[0] and curve[-1]==points[-1]
    for point in points:assert point in curve
    assert all(math.isfinite(y) and min(p[1] for p in points)<=y<=max(p[1] for p in points) for x,y in curve)


@pytest.mark.parametrize('count',[3,5,7])
def test_forecast_lengths(fixture,count,monkeypatch):
    model,settings,_=fixture;settings['forecastDays']=str(count)
    calls=[];original=Painter.icon
    def track(self,path,box):calls.append(path);return original(self,path,box)
    monkeypatch.setattr(Painter,'icon',track)
    with render_dashboard(model,settings):pass
    assert calls[-2*count:]==[icon for day in model.forecast[1:count+1] for icon in (day.icon,day.moon_phase_icon)]


@pytest.mark.parametrize('phase',['newmoon','waxingcrescent','firstquarter','waxinggibbous','fullmoon','waninggibbous','lastquarter','waningcrescent'])
def test_all_moon_assets(fixture,phase):
    model,settings,_=fixture
    days=tuple(replace(d,moon_phase_icon=str(ICON_DIR/(phase+'.png'))) for d in model.forecast)
    with render_dashboard(replace(model,forecast=days),settings):pass


def test_missing_sunrise_sunset(fixture):
    model,settings,_=fixture
    metrics=tuple(p for p in model.data_points if p.label not in ('Sunrise','Sunset'))
    with render_dashboard(replace(model,data_points=metrics),settings):pass
    assert not any(m.label in ('Sunrise','Sunset') for m in metrics)


@pytest.mark.parametrize('key',['displayMetrics','displayGraph','displayRain','displayForecast','moonPhase','displayRefreshTime'])
def test_display_flags(fixture,key):
    model,settings,_=fixture;settings[key]='false'
    with render_dashboard(model,settings):pass


@pytest.mark.parametrize('step',[1,2,4,6,12])
def test_optional_graph_icons(fixture,step):
    model,settings,_=fixture;settings.update(displayGraphIcons='true',graphIconStep=str(step))
    with render_dashboard(model,settings):pass


@pytest.mark.parametrize('frame',['None','Corner','Top and Bottom','Rectangle'])
def test_frame_variants_and_margins(fixture,frame):
    model,settings,_=fixture;settings.update(selectedFrame=frame,leftMargin='10',topMargin='8',rightMargin='10',bottomMargin='8')
    with render_dashboard(model,settings):pass


def test_referenced_assets_only_and_bounded_chart(fixture,monkeypatch):
    model,settings,_=fixture
    opens=[];sizes=[];original_open=Image.open;original_new=Image.new
    def opened(path,*a,**k):opens.append(Path(path).name);return original_open(path,*a,**k)
    def created(mode,size,*a,**k):sizes.append(size);return original_new(mode,size,*a,**k)
    monkeypatch.setattr(Image,'open',opened);monkeypatch.setattr(Image,'new',created)
    with render_dashboard(model,settings):pass
    expected=[model.current_day_icon]+[m.icon for m in model.data_points]+[x for d in model.forecast[1:8] for x in (d.icon,d.moon_phase_icon)]
    assert opens==[Path(x).name for x in expected]
    assert all(w<=1600 and h<=480 for w,h in sizes)
    assert (1532,194) in sizes


def test_rejects_external_assets_and_unsupported_layout(fixture):
    model,settings,_=fixture
    with pytest.raises(ValueError):render_dashboard(model,settings,(480,800))
    with pytest.raises(ValueError):render_dashboard(model,dict(settings,backgroundOption='image'))
    with pytest.raises(ValueError):render_dashboard(replace(model,current_day_icon='https://example.invalid/icon.png'),settings)


def test_missing_hourly_samples_make_curve_gaps(fixture):
    model,settings,_=fixture
    hours=tuple(replace(h,temperature=None if i%3 else h.temperature) for i,h in enumerate(model.hourly_forecast))
    with render_dashboard(replace(model,hourly_forecast=hours),settings):pass


def test_long_metric_values_cannot_spill_into_neighbors(fixture):
    model,settings,_=fixture
    metrics=tuple(replace(m,measurement='LONG VALUE '*20,unit='long unit '*12) for m in model.data_points)
    with render_dashboard(replace(model,data_points=metrics),settings):pass


@pytest.mark.parametrize('units,label',[('metric','°C'),('imperial','°F'),('standard','K')])
def test_units_pass_through_without_renderer_conversions(fixture,units,label):
    model,settings,parsed=fixture
    parsed.update(units=units,temperature_unit=label,current_temperature='-5')
    m=from_parsed(parsed,rendered_at=model.rendered_at)
    assert m.current_temperature=='-5' and m.temperature_unit==label
    assert [asdict(h) for h in m.hourly_forecast]==parsed['hourly_forecast']
    with render_dashboard(m,settings):pass


def test_existing_openweathermap_parser_can_feed_same_model(fixture):
    from plugins.weather.weather import Weather
    import pytz
    now=fixture[0].rendered_at
    stamp=now.timestamp()
    current=dict(dt=stamp,temp=-2.6,feels_like=-5.2,weather=[{'icon':'10n'}],
                 sunrise=stamp-4*3600,sunset=stamp+6*3600,wind_deg=0,wind_speed=3.25,
                 humidity=90,pressure=1000,uvi=1.2,visibility=2000)
    raw=dict(current=current,daily=[dict(dt=stamp+i*86400,weather=[{'icon':'10d'}],
                 temp={'max':2,'min':-4},moon_phase=.25,sunrise=stamp-4*3600+i*86400,
                 sunset=stamp+6*3600+i*86400) for i in range(8)],
             hourly=[dict(dt=stamp+i*3600,temp=-2.6,pop=.5,rain={'1h':.2},weather=[{'icon':'10d'}]) for i in range(24)])
    parsed=Weather({'id':'weather'}).parse_weather_data(raw,{'list':[{'main':{'aqi':2}}]},pytz.UTC,'metric','24h',46)
    m=from_parsed(parsed,rendered_at=now)
    assert m.current_temperature=='-3' and m.feels_like=='-5'
    assert Path(m.current_day_icon).name=='10n.png'
    assert m.data_points[-1].measurement==2 and m.data_points[-1].unit=='Fair'
    assert [asdict(h) for h in m.hourly_forecast]==parsed['hourly_forecast']
    with render_dashboard(m,fixture[1]):pass


def test_original_chromium_templates_fonts_and_assets_unchanged():
    expected=json.loads((REPO/'tests/fixtures/weather/original-assets-sha256.json').read_text())
    for relative,digest in expected.items():
        if relative in ('src/plugins/weather/weather.py', 'src/plugins/weather/settings.html'):
            continue  # Renderer dispatch and settings selector are intentional integration changes.
        assert hashlib.sha256((REPO/relative).read_bytes()).hexdigest()==digest


@pytest.mark.parametrize('margin',[0,30])
def test_margin_extremes_preserve_insets(fixture,margin):
    model,settings,_=fixture
    for key in ['leftMargin','topMargin','rightMargin','bottomMargin']:settings[key]=str(margin)
    with render_dashboard(model,settings) as image:
        assert image.size==(800,480)
        if margin:
            for box in [(0,0,800,margin),(0,480-margin,800,480),(0,0,margin,480),(800-margin,0,800,480)]:
                with image.crop(box) as strip:assert strip.getextrema()==((255,255),)*3
