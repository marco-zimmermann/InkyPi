"""Synthetic aviation reports, mocked network, no hardware/display operations."""
import ast
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from PIL import Image, ImageDraw
from plugins.metar.decoding import decode, category, select_report, validate_icao, observation_time, SM_KM
from plugins.metar.data import (load_metar, load_airport, fetch_metar, airport_metadata,
                               write_cache, csv_lines, make_session, METADATA_TTL)
from plugins.metar.rendering import render_dashboard, fit_text, font, BLACK, WHITE, RED, BLUE, GREEN, YELLOW

FIXTURES=json.loads(Path('tests/fixtures/metar/examples.json').read_text())
NOW=datetime.fromisoformat(FIXTURES['now'])
AIRPORT=json.loads(Path('src/plugins/metar/airport_seed.json').read_text())['LSZL']['airport']


def report(groups,icao='LSZL'):
    return {'icaoId':icao,'obsTime':NOW.timestamp()-900,'rawOb':f'METAR {icao} 171200Z '+groups}


def test_metar_web_icon_is_a_valid_transparent_png():
    info=json.loads(Path('src/plugins/metar/plugin-info.json').read_text())
    icon=Path('src/plugins')/info['id']/'icon.png'
    with Image.open(icon) as image:
        image.load()
        assert image.format=='PNG' and image.size==(512,512) and image.mode=='RGBA'
        assert image.getchannel('A').getextrema()==(0,255)
        assert image.getpixel((0,0))[3]==0


def test_metar_web_icon_resolves_through_existing_asset_handler():
    """Exercise the actual handler body without Flask/app/hardware dependencies."""
    import os
    source=Path('src').resolve()
    module=ast.parse((source/'blueprints/plugin.py').read_text())
    handler=next(node for node in module.body if isinstance(node,ast.FunctionDef) and node.name=='image')
    assert handler.decorator_list[0].args[0].value=='/images/<plugin_id>/<path:filename>'
    handler.decorator_list=[]
    send=Mock(side_effect=lambda directory,filename:(Path(directory)/filename).read_bytes())
    logger=Mock()
    namespace={'os':os,'resolve_path':lambda name:str(source/name),
               'send_from_directory':send,'logger':logger}
    exec(compile(ast.Module(body=[handler],type_ignores=[]),'plugin-image-handler','exec'),namespace)
    response=namespace['image']('metar','icon.png')
    assert response==(source/'plugins/metar/icon.png').read_bytes()
    assert response.startswith(b'\x89PNG\r\n\x1a\n')
    send.assert_called_once_with(str(source/'plugins/metar'),'icon.png')
    logger.error.assert_not_called()


@pytest.mark.parametrize('key,expected', [('vfr','VFR'),('ifr','IFR')])
def test_lszl_fixture(key,expected):
    item=FIXTURES[key]
    assert observation_time(item)==NOW-timedelta(minutes=15)
    assert decode(item)['category']==expected


@pytest.mark.parametrize('ceiling,expected',[(499,'LIFR'),(500,'IFR'),(999,'IFR'),(1000,'MVFR'),(3000,'MVFR'),(3001,'VFR')])
def test_ceiling_boundaries(ceiling,expected):
    assert category(10,ceiling,True)==expected


@pytest.mark.parametrize('sm,expected',[(.99,'LIFR'),(1,'IFR'),(2.99,'IFR'),(3,'MVFR'),(5,'MVFR'),(5.01,'VFR')])
def test_visibility_boundaries(sm,expected):
    assert category(sm*SM_KM,None,True)==expected


@pytest.mark.parametrize('groups,km,bound,expected',[
    ('CAVOK',10,'>=','VFR'),('9999NDV SCT040',10,'>=','VFR'),
    ('0000 BKN001',.05,'<','LIFR'),('1500 OVC010',1.5,'=','LIFR'),
    ('1 1/2SM BKN008',1.5*SM_KM,'=','IFR'),('M1/4SM VV002',.25*SM_KM,'<','LIFR'),
    ('P6SM CLR',6*SM_KM,'>','VFR'),('P2SM CLR',2*SM_KM,'>','UNKNOWN')])
def test_visibility_encodings(groups,km,bound,expected):
    d=decode(report(groups));assert d['visibility']['km']==pytest.approx(km)
    assert d['visibility']['bound']==bound and d['category']==expected


@pytest.mark.parametrize('groups,expected',[
 ('9999 FEW010 SCT030','VFR'),('9999 BKN010 OVC020','MVFR'),
 ('9999 VV003','LIFR'),('9999 VV///','UNKNOWN'),('9999 BKN///','UNKNOWN'),
 ('9999','UNKNOWN'),('CLR','UNKNOWN'),('NIL','UNKNOWN'),('','UNKNOWN'),
 ('1/2SM','LIFR'),('9999 NCD','VFR')])
def test_missing_clouds_not_clear(groups,expected):
    assert decode(report(groups))['category']==expected


def test_north_american_negative_temperature_altimeter():
    d=decode(report('18012G20KT 1 1/2SM -RA BR BKN008 OVC020 M02/M04 A2992 RMK AO2 0000 BKN001','KJFK'))
    assert d['category']=='IFR' and d['temperature']==-2 and d['dewpoint']==-4
    assert d['qnh']==pytest.approx(1013.2,abs=.1)
    assert d['wind']=={'direction':'180','speed':12,'gust':20}
    assert d['symbol']=='rain' and d['ceiling']==800
    assert d['weather']==['light rain','mist']


@pytest.mark.parametrize('group,direction,speed,gust',[
 ('VRB03KT','VRB',3,None),('00000KT','000',0,None),
 ('24012G22KT','240',12,22),('18005MPS','180',9.7,None)])
def test_winds(group,direction,speed,gust):
    d=decode(report(group+' CAVOK 12/10 Q1012'))
    assert d['wind']==dict(direction=direction,speed=speed,gust=gust)


def test_variable_wind_correction_and_trend_are_separate():
    d=decode(report('COR AUTO 24012KT 180V280 CAVOK 10/08 Q1014 TEMPO 0800 FG BKN001'))
    assert d['flags']==['AUTO','COR'] and d['variable']=='180V280'
    assert d['category']=='VFR' and d['symbol']=='clear'
    assert decode(report('9999 FEW025TCU SCT040CB'))['layers'][1]['type']=='CB'


@pytest.mark.parametrize('groups,icon',[
 ('CAVOK','clear'),('9999 FEW020','partly_cloudy'),('9999 OVC040','overcast'),
 ('9999 -RA BKN040','rain'),('9999 SHRA BKN040','showers'),
 ('9999 +TSRAGR BKN040','thunderstorm'),('9999 FZRA OVC040','rain'),
 ('9999 -SN OVC040','snow'),('0800 BCFG VV002','fog'),('4000 BR OVC010','mist'),
 ('4000 HZ SCT040','mist'),('//// ////','unknown')])
def test_weather_symbols(groups,icon):
    assert decode(report(groups))['symbol']==icon


def test_malformed_and_nil_dont_create_values():
    d=decode(report('AUTO 99999KT XSM BKN/// XX/XX Q////'))
    assert d['wind'] is d['temperature'] is d['qnh'] is d['visibility'] is None
    assert d['category']=='UNKNOWN'
    d=decode(report('NIL CAVOK 15/10 Q1010'))
    assert d['category']=='UNKNOWN' and d['temperature'] is None
    d=decode(dict(report('CAVOK'),fltCat='LIFR'))
    assert d['category']=='VFR'


def test_select_station_latest_correction_not_api_defaults():
    first=report('CAVOK');corrected=dict(first,rawOb=first['rawOb'].replace('METAR ','METAR COR '))
    assert select_report([first,report('CAVOK','LSZA'),corrected],'LSZL')==corrected
    assert select_report([dict(first,rawOb=first['rawOb'].replace('LSZL','LSZA'))],'LSZL') is None
    assert select_report([dict(first,obsTime=None)],'LSZL') is None
    with pytest.raises(ValueError):select_report({},'LSZL')


@pytest.mark.parametrize('bad',['../../x','LSZ','LSZ12','<div>',''])
def test_invalid_icao(bad):
    with pytest.raises(ValueError):validate_icao(bad)


class Response:
    def __init__(self,payload=None,status=200,chunks=None):
        self.status_code=status;self.chunks=chunks if chunks is not None else [json.dumps(payload).encode()]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def raise_for_status(self):
        if self.status_code>=400:raise requests.HTTPError(str(self.status_code))
    def iter_content(self,chunk_size):yield from self.chunks


def session(payload,status=200):return Mock(get=Mock(return_value=Response(payload,status)))


def test_fetch_bounds_and_empty():
    s=session([report('CAVOK')]);assert fetch_metar(s,'LSZL')['icaoId']=='LSZL'
    assert s.get.call_args.kwargs['timeout']==(3,8) and s.get.call_args.kwargs['stream']
    assert fetch_metar(session(None,204),'LSZL') is None
    with pytest.raises(ValueError):fetch_metar(Mock(get=Mock(return_value=Response(chunks=[b'x'*65537]))),'LSZL')
    with make_session() as s:assert s.adapters['https://'].max_retries.total==0


def test_cache_isolation_stale_fallback_and_original_timestamp(tmp_path):
    item=report('CAVOK');good=load_metar(session([item]),tmp_path,'LSZL',NOW)
    assert not good['cached'] and not good['stale']
    failed=Mock(get=Mock(side_effect=requests.Timeout))
    cached=load_metar(failed,tmp_path,'LSZL',NOW+timedelta(hours=3))
    assert cached['cached'] and cached['stale'] and cached['report']['obsTime']==item['obsTime']
    assert load_metar(failed,tmp_path,'LSZA',NOW)['report'] is None
    assert failed.get.call_count==2
    assert load_metar(session(None,204),tmp_path,'LSZL',NOW)['cached']


def test_corrupt_cache_and_future_data(tmp_path):
    p=tmp_path/'reports/LSZL.json';p.parent.mkdir();p.write_text('{broken')
    result=load_metar(session([dict(report('CAVOK'),obsTime=NOW.timestamp()+3600)]),tmp_path,'LSZL',NOW)
    assert result['report'] is None
    assert p.read_text()=='{broken'


def test_metadata_known_runways_no_inventions():
    a={'name':'Example','elevation_ft':'650'}
    runways=[{'airport_ident':'LSZL','closed':'0','le_ident':'08','he_ident':'26','length_ft':'2625'},
             {'airport_ident':'LSZL','closed':'1','le_ident':'09','he_ident':'27','length_ft':'3000'},
             {'airport_ident':'LSZA','closed':'0','le_ident':'01'}]
    m=airport_metadata(a,runways,'LSZL')
    assert m['elevation_ft']==650 and m['runways']==[{'ident':'08 / 26','length_m':800,'surface':None}]
    assert airport_metadata({},[{'airport_ident':'LSZL','closed':'0'}],'LSZL')['runways'][0]['length_m'] is None
    assert len(AIRPORT['runways'])==3 and AIRPORT['runways'][1]['ident']=='08L / 26R'


def test_metadata_independent_cache_and_seed(tmp_path):
    failed=Mock(get=Mock(side_effect=requests.Timeout))
    seed=json.loads(Path('src/plugins/metar/airport_seed.json').read_text())['LSZL']
    now=datetime.fromtimestamp(seed['fetched_at'],timezone.utc)
    m=load_airport(failed,tmp_path,'LSZL',now)
    assert m['elevation_ft']==650 and 'cached' in m['status'];failed.get.assert_not_called()
    m=load_airport(failed,tmp_path,'LSZL',now+timedelta(seconds=METADATA_TTL+1))
    assert m['status']=='STALE metadata'
    assert load_airport(failed,tmp_path,'LSZA',now)['runways']==[]


def test_csv_streaming_limits():
    assert list(csv_lines(Response(chunks=[b'a,b\n1',b',2\n'])))==['a,b\n','1,2\n']
    with pytest.raises(ValueError):list(csv_lines(Response(chunks=[b'x'*131073])))


@pytest.mark.parametrize('key',['vfr','ifr',None])
def test_render_palette_dimensions_and_missing_values(key):
    state={'report':FIXTURES[key] if key else None,'cached':False,'stale':key is None,'error':'No report'}
    image=render_dashboard('LSZL',state,dict(AIRPORT,status='cached metadata',as_of='2026-10-17'),NOW,example=True)
    assert image.size==(800,480) and image.mode=='RGB'
    assert set(image.getdata()) <= {BLACK,WHITE,RED,BLUE,GREEN,YELLOW}
    if key is None:assert decode(None)['symbol']=='unknown'


def test_text_fit_and_long_report_are_explicit(monkeypatch):
    draw=ImageDraw.Draw(Image.new('RGB',(800,480)))
    text,f=fit_text(draw,'Very long airport name '*20,100)
    assert draw.textlength(text,font=f)<=100 and text.endswith('…')
    calls=[];original=ImageDraw.ImageDraw.text
    def capture(self,xy,text,*args,**kwargs):
        calls.append((xy,text));return original(self,xy,text,*args,**kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',capture)
    state={'report':report('CAVOK RMK '+'LONG '*400),'cached':True,'stale':True}
    render_dashboard('LSZL',state,{'runways':[]},NOW)
    assert any('RAW TOO LONG' in text for _,text in calls)
    assert any('CACHED / STALE' in text for _,text in calls)
    assert any('Runway information unavailable' in text for _,text in calls)
    assert all(0<=xy[1]<480 for xy,_ in calls)


def test_no_browser_hardware_or_display_imports():
    for p in Path('src/plugins/metar').glob('*.py'):
        tree=ast.parse(p.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):names=[x.name for x in node.names]
            elif isinstance(node,ast.ImportFrom):names=[node.module or '']
            else:continue
            assert not any(n.startswith(('gpiod','inky','display','subprocess','selenium')) for n in names)
        assert 'take_screenshot' not in p.read_text()


def test_metadata_stream_fetch_and_disk_cache(tmp_path):
    airports=b'ident,name,elevation_ft\nLSZA,Other,900\nLSZL,Locarno Airport,650\n'
    runways=b'airport_ident,closed,le_ident,he_ident,length_ft,surface\nLSZA,0,01,19,1000,GRASS\nLSZL,0,08L,26R,2625,ASPH\n'
    s=Mock(get=Mock(side_effect=[Response(chunks=[airports]),Response(chunks=[runways])]))
    result=load_airport(s,tmp_path,'LSZA',NOW)
    assert result['name']=='Other' and result['runways'][0]['ident']=='01 / 19'
    assert s.get.call_count==2
    untouched=Mock(get=Mock(side_effect=AssertionError('Cache must avoid network')))
    assert load_airport(untouched,tmp_path,'LSZA',NOW)['runways']==result['runways']
    untouched.get.assert_not_called()


def test_corrupt_metadata_cache_is_not_rendered(tmp_path):
    write_cache(tmp_path/'airports/LSZA.json',{'icao':'LSZA','airport':{'runways':[None]},'fetched_at':NOW.timestamp(),'as_of':'2026-10-17'})
    failed=Mock(get=Mock(side_effect=requests.Timeout))
    metadata=load_airport(failed,tmp_path,'LSZA',NOW)
    assert metadata['runways']==[] and metadata['status']=='metadata unavailable'


def test_plugin_discovery_and_worker_entrypoint(tmp_path,monkeypatch):
    from plugins import plugin_registry
    from plugins.metar import metar as plugin_module
    info=json.loads(Path('src/plugins/metar/plugin-info.json').read_text())
    monkeypatch.setattr(plugin_registry,'PLUGIN_CLASSES',{})
    plugin_registry.load_plugins([info])
    instance=plugin_registry.get_plugin_instance(info)
    assert isinstance(instance,plugin_module.Metar)
    report_mock=Mock(return_value={'report':FIXTURES['vfr'],'cached':False,'stale':False})
    airport_mock=Mock(return_value=dict(AIRPORT,status='cached metadata',as_of='2026-10-17'))
    monkeypatch.setattr(plugin_module,'load_metar',report_mock)
    monkeypatch.setattr(plugin_module,'load_airport',airport_mock)
    image=instance.generate_image({},Mock(plugin_image_dir=str(tmp_path)))
    assert image.size==(800,480)
    assert report_mock.call_args.args[2]=='LSZL'
    assert report_mock.call_args.args[1]==tmp_path/'.metar-cache'
    assert instance.generate_settings_template()['settings_template']=='metar/settings.html'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('cached,stale,available,status,live', [
    (False,False,True,'live',True), (False,True,True,'stale',True),
    (True,False,True,'cached',False), (True,True,True,'cached_stale',False),
    (False,True,False,'unavailable',False)])
def test_plugin_reports_retrieval_quality_without_changing_pixels(
        tmp_path,monkeypatch,cached,stale,available,status,live):
    from plugins.metar import metar as module
    from PIL import Image
    from contextlib import nullcontext
    state={'report':FIXTURES['vfr'] if available else None,'cached':cached,'stale':stale}
    monkeypatch.setattr(module,'make_session',lambda: nullcontext(Mock()))
    monkeypatch.setattr(module,'load_metar',lambda *args:state)
    monkeypatch.setattr(module,'load_airport',lambda *args:{})
    expected=Image.new('RGB',(8,8),'white')
    monkeypatch.setattr(module,'render_dashboard',lambda *args:expected)
    image=module.Metar({'id':'metar'}).generate_image({'icao':'LSZL'},Mock(plugin_image_dir=str(tmp_path)))
    assert image is expected and image.tobytes()==Image.new('RGB',(8,8),'white').tobytes()
    assert image.info['inkypi_live_data_success'] is live
    assert image.info['inkypi_data_status']==status


def test_unknown_extra_ceiling_does_not_allow_optimistic_category():
    assert decode(report('9999 BKN007 OVC///'))['category']=='UNKNOWN'
    assert decode(report('9999 BKN002 OVC///'))['category']=='LIFR'


@pytest.mark.parametrize('key',['vfr','ifr',None])
def test_refined_layout_text_bounds_and_dedicated_fields(monkeypatch,key):
    captured=[]
    original=ImageDraw.ImageDraw.text
    def capture(self,xy,text,*args,**kwargs):
        box=self.textbbox(xy,text,font=kwargs.get('font'),anchor=kwargs.get('anchor'))
        captured.append((text,box,kwargs.get('font').size))
        return original(self,xy,text,*args,**kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',capture)
    state={'report':FIXTURES[key] if key else None,'cached':False,'stale':key is None}
    render_dashboard('LSZL',state,dict(AIRPORT,status='reference metadata',as_of='2026-10-17'),NOW,example=True)
    assert all(0<=box[0]<box[2]<=800 and 0<=box[1]<box[3]<=480
               for text,box,size in captured if text.strip())
    labels={text:(box,size) for text,box,size in captured}
    assert labels['CEILING'][0][0]>600
    assert 'OurAirports · community reference' in labels and 'RUNWAY' in labels and 'LENGTH' in labels
    assert '08C / 26C' in labels and '700 m' in labels and '800 m' in labels
    assert labels['08C / 26C'][0][1] < labels['08L / 26R'][0][1] < labels['08R / 26L'][0][1]
    if key=='ifr':
        assert labels['700'][1]>=40
        assert labels['240°'][1]>=40 and labels['12 kt'][1]>=34
        assert 'Gust 22 kt' in labels and '180°–280°' in labels
        # Numeric blocks and units never intrude into the cloud strip.
        assert labels['hPa'][0][3]<315 and labels['Dew 6°C'][0][3]<315
    if key=='vfr':assert labels['None'][1]>=40


def test_long_runway_table_is_explicit_and_bounded(monkeypatch):
    captured=[]
    original=ImageDraw.ImageDraw.text
    def capture(self,xy,text,*args,**kwargs):
        captured.append(text)
        return original(self,xy,text,*args,**kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',capture)
    airport=dict(AIRPORT,runways=AIRPORT['runways']*3)
    render_dashboard('LSZL',{'report':FIXTURES['vfr']},airport,NOW)
    assert '+ 7 more runways' in captured
    assert captured.count('08C / 26C')==1 and captured.count('08L / 26R')==1


@pytest.mark.parametrize('status,payload,outcome',[
    (200,[report('CAVOK')],'success'),(204,None,'no_report'),(200,[],'no_report'),
    (200,[report('CAVOK','LSZA')],'invalid_response'),
])
def test_http_diagnostic_success_and_no_report(caplog,monkeypatch,status,payload,outcome):
    from plugins.metar import data
    caplog.set_level('INFO',logger=data.__name__)
    times=iter([100.0,100.625]);monkeypatch.setattr(data.time,'monotonic',lambda:next(times))
    s=session(payload,status);result=fetch_metar(s,'LSZL')
    records=[r for r in caplog.records if r.name==data.__name__]
    assert len(records)==1
    message=records[0].getMessage()
    assert f'outcome={outcome}' in message and f'status={status}' in message
    assert 'elapsed_seconds=0.625' in message and 'exception=-' in message
    assert 'station=LSZL' in message
    assert 'CAVOK' not in message and 'rawOb' not in message
    assert records[0].levelname==('WARNING' if outcome=='invalid_response' else 'INFO')
    assert s.get.call_count==1
    assert s.get.call_args.kwargs=={'params':{'ids':'LSZL','format':'json'},'timeout':(3,8),'stream':True}
    assert (result is not None)==(outcome=='success')


@pytest.mark.parametrize('exception,outcome',[
    (requests.ConnectTimeout,'connect_timeout'),(requests.ReadTimeout,'read_timeout'),
    (requests.Timeout,'timeout'),(requests.ConnectionError,'transport_error'),
    (requests.exceptions.SSLError,'tls_error'),(requests.exceptions.ProxyError,'proxy_error'),
])
def test_http_exception_diagnostics_preserve_original_exception(caplog,monkeypatch,exception,outcome):
    from plugins.metar import data
    times=iter([20.0,28.125]);monkeypatch.setattr(data.time,'monotonic',lambda:next(times))
    secret='password=do-not-log; https://user:secret@proxy.invalid/path?token=private\nRAW-RESPONSE-TEXT'
    error=exception(secret);s=Mock(get=Mock(side_effect=error))
    with pytest.raises(exception) as caught:fetch_metar(s,'LSZL')
    assert caught.value is error and s.get.call_count==1
    records=[r for r in caplog.records if r.name==data.__name__]
    assert len(records)==1 and records[0].levelname=='WARNING'
    message=records[0].getMessage()
    assert f'outcome={outcome}' in message and f'exception={exception.__name__}' in message
    assert 'elapsed_seconds=8.125' in message and 'status=-' in message and 'phase=request' in message
    for sensitive in ('do-not-log','secret','proxy.invalid','token','RAW-RESPONSE-TEXT'):
        assert sensitive not in message
    assert '\n' not in message


@pytest.mark.parametrize('status',[403,429,503])
def test_http_failure_status_and_timing(caplog,monkeypatch,status):
    from plugins.metar import data
    times=iter([1.0,1.25]);monkeypatch.setattr(data.time,'monotonic',lambda:next(times))
    with pytest.raises(requests.HTTPError):fetch_metar(session({'error':'private response body'},status),'LSZL')
    message=caplog.records[-1].getMessage()
    assert 'outcome=http_error' in message and f'status={status}' in message
    assert 'phase=http_status' in message and 'elapsed_seconds=0.250' in message
    assert 'private response body' not in message and 'exception=HTTPError' in message


@pytest.mark.parametrize('chunks,outcome,phase,detail',[
    ([b'<html>private-error-body</html>'],'decoding_failure','decode_json','line=1 column=1'),
    ([b'\xff'],'decoding_failure','decode_json','byte_start=0'),
    ([b'{"secret":"private-error-body"}'],'invalid_response','select_report','schema/selection'),
    ([b'x'*65537],'invalid_response','read_body','64 KiB limit'),
])
def test_invalid_response_diagnostic_phases(caplog,monkeypatch,chunks,outcome,phase,detail):
    from plugins.metar import data
    times=iter([5.0,5.1]);monkeypatch.setattr(data.time,'monotonic',lambda:next(times))
    with pytest.raises((ValueError,TypeError)):
        fetch_metar(Mock(get=Mock(return_value=Response(chunks=chunks))),'LSZL')
    message=caplog.records[-1].getMessage()
    assert f'outcome={outcome}' in message and f'phase={phase}' in message and 'status=200' in message
    assert detail in message and 'private-error-body' not in message


def test_wrapped_read_timeout_after_partial_body(caplog,monkeypatch):
    from plugins.metar import data
    from urllib3.exceptions import ReadTimeoutError
    times=iter([1.0,9.0]);monkeypatch.setattr(data.time,'monotonic',lambda:next(times))
    response=Response()
    def chunks(chunk_size):
        yield b'[partial'
        raise requests.ConnectionError(ReadTimeoutError(None,'/private?token=secret','sensitive message'))
    response.iter_content=chunks
    with pytest.raises(requests.ConnectionError):fetch_metar(Mock(get=Mock(return_value=response)),'LSZL')
    message=caplog.records[-1].getMessage()
    assert 'outcome=read_timeout' in message and 'exception=ConnectionError' in message
    assert 'phase=read_body' in message and 'bytes=8' in message
    assert 'causes=ConnectionError/ReadTimeoutError' in message
    assert 'token' not in message and 'sensitive' not in message


def test_diagnostic_exception_chain_is_bounded_and_redacts_messages():
    from plugins.metar.data import _request_failure
    error=requests.ConnectionError('credential=private')
    error.__cause__=OSError(101,'secret-network-detail')
    error.__cause__.__context__=error
    outcome,detail=_request_failure(error,'request')
    assert outcome=='transport_error' and 'errno=101' in detail
    assert 'credential' not in detail and 'secret-network-detail' not in detail
    assert len(detail)<300


def test_http_error_response_available_before_context(caplog):
    response=Mock(status_code=502)
    error=requests.HTTPError('private',response=response)
    with pytest.raises(requests.HTTPError):fetch_metar(Mock(get=Mock(side_effect=error)),'LSZL')
    assert 'status=502' in caplog.records[-1].getMessage()


@pytest.mark.parametrize('error',[requests.ConnectTimeout('private'),requests.ReadTimeout('private'),requests.HTTPError('private')])
def test_diagnostics_preserve_unavailable_and_cached_states(tmp_path,error):
    bad=Mock(get=Mock(side_effect=error))
    unavailable=load_metar(bad,tmp_path,'LSZL',NOW)
    assert unavailable=={'report':None,'cached':False,'stale':True,'error':'METAR fetch failed: '+type(error).__name__}
    item=report('CAVOK');write_cache(tmp_path/'reports/LSZL.json',{'icao':'LSZL','report':item,'fetched_at':NOW.timestamp()})
    cached=load_metar(bad,tmp_path,'LSZL',NOW+timedelta(hours=2))
    assert cached['cached'] and cached['stale'] and cached['report']==item
    assert cached['error']=='METAR fetch failed: '+type(error).__name__
    assert bad.get.call_count==2


def test_post_fetch_future_report_validation_is_logged(tmp_path,caplog):
    item=dict(report('CAVOK'),obsTime=NOW.timestamp()+3600)
    result=load_metar(session([item]),tmp_path,'LSZL',NOW)
    assert result['report'] is None and result['error']=='METAR fetch failed: ValueError'
    assert any('outcome=invalid_response' in r.getMessage() and 'future skew' in r.getMessage() for r in caplog.records)
