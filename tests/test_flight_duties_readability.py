"""Synthetic labels and the pure E673 six-color image-conversion path."""
from datetime import timedelta
from unittest.mock import Mock
import pytest
from PIL import Image,ImageDraw
from plugins.flight_duties.calendar_data import schedule_for
from plugins.flight_duties.rendering import render_dashboard,SPECTRA_COLORS,BLUE,GREEN,RED
from tests.test_flight_duties import NOW,RAW,report
from utils.image_utils import change_orientation,resize_image,apply_image_enhancement

# Installed Pimoroni inky_e673.py tables, nominal order: K/W/Y/R/B/G.
SATURATED=((0,0,0),(161,164,165),(208,190,71),(156,72,75),(61,59,94),(58,91,70))


@pytest.fixture
def text_calls(monkeypatch):
    calls=[];original=ImageDraw.ImageDraw.text
    def text(self,xy,value,*args,**kwargs):
        calls.append((xy,value,kwargs.get('fill')));return original(self,xy,value,*args,**kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw,'text',text)
    return calls


def tomorrow_report():
    now=NOW.replace(hour=18,minute=0)
    raw=RAW.replace(b'20261026',b'20261027').replace(b'20261025',b'20261026')
    return dict(schedule=schedule_for(raw,now),source='live',fetched_at=now),now


def test_today_and_tomorrow_labels_and_compact_date(text_calls):
    with render_dashboard(report(),NOW):pass
    assert any(v=='NEXT DUTY' for _,v,_ in text_calls)
    text_calls.clear();result,now=tomorrow_report()
    with render_dashboard(result,now):pass
    assert any(v=='TOMORROW' and color==BLUE for _,v,color in text_calls)
    assert any(v=='TUE 27 OCT 2026' for _,v,_ in text_calls)
    assert any(v=='FIRST DUTY TOMORROW' for _,v,_ in text_calls)
    assert not any('Status unknown' in v or v=='NEXT DUTY' for _,v,_ in text_calls)
    assert any(v=='5 of 7 bookings shown' for _,v,_ in text_calls)
    assert any(v.startswith('Last sync') for _,v,_ in text_calls)


@pytest.mark.parametrize('status',['UNKNOWN','TENTATIVE','CONFIRMED'])
def test_only_explicit_confirmation_gets_green(status,text_calls):
    raw=RAW.replace(b'STATUS:CONFIRMED',b'STATUS:'+status.encode())
    result=report(raw=raw)
    with render_dashboard(result,NOW) as image:
        colors=set(image.getdata())
        assert (GREEN in colors)==(status=='CONFIRMED')
        assert RED not in colors
    values=[v for _,v,_ in text_calls]
    assert 'Status unknown' not in values
    assert ('Confirmed' in values)==(status=='CONFIRMED')
    assert all(color==(0,0,0) for _,v,color in text_calls if v in ('Confirmed','Tentative'))


@pytest.mark.parametrize('source,error',[('live',None),('live','Cache not saved'),('cached','Network unavailable'),('unavailable','Network unavailable')])
def test_red_only_for_real_warning_and_no_background_color(source,error):
    result=report(source);result['error']=error
    if source=='unavailable':result['schedule']=None
    with render_dashboard(result,NOW) as image:
        colors=dict((color,count) for count,color in image.getcolors(7))
        assert set(colors)<=set(SPECTRA_COLORS)
        assert (RED in colors)==bool(error or source!='live')
        assert colors[(255,255,255)]>800*480*.85


@pytest.mark.parametrize('source',['live','cached'])
def test_exact_source_colors_survive_current_display_conversion(source):
    result=report(source)
    with render_dashboard(result,NOW) as original:
        image=change_orientation(original,'horizontal')
        resized=resize_image(image,(800,480));image.close()
        converted=apply_image_enhancement(resized,dict(brightness=1,contrast=1,saturation=1,sharpness=1))
        palette=Image.new('P',(1,1))
        palette.putpalette([int((a+b)/2) for raw,sat in zip(SPECTRA_COLORS,SATURATED) for a,b in zip(raw,sat)])
        try:
            with converted.quantize(6,palette=palette,dither=Image.Dither.FLOYDSTEINBERG) as indices:
                expected=bytes(SPECTRA_COLORS.index(c) for c in original.getdata())
                assert indices.tobytes()==expected # no color leakage or error-diffusion speckles
        finally:palette.close();resized.close()


def test_featured_confirmation_is_explicit_and_black_text(text_calls):
    from tests.test_flight_duties import one
    result=report(raw=one(extra='STATUS:CONFIRMED'))
    with render_dashboard(result,NOW) as image:assert GREEN in set(image.getdata())
    assert any(v=='Confirmed' and color==(0,0,0) for _,v,color in text_calls)
