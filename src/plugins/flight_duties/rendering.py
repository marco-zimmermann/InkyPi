"""Fixed 800x480 RGB Pillow agenda, no network, config, browser or display IO."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from .calendar_data import ZURICH, clean, display_day

FONT_DIR=Path(__file__).resolve().parents[2]/'static/fonts'
BLACK='#171717';BLUE='#164e85';WHITE='#ffffff';RED='#a02020'


def visible_duties(duties,now,limit):
    """Keep the next duty visible when earlier bookings exceed the page capacity."""
    next_index=next((i for i,d in enumerate(duties) if d.start>=now),None)
    active=next((i for i,d in enumerate(duties) if d.start<=now<d.end),None)
    focus=next_index if next_index is not None else active
    first=max(0,focus-limit+2) if focus is not None and focus>=limit else 0
    first=min(first,max(0,len(duties)-limit))
    return duties[first:first+limit],first,focus


def render_dashboard(result,now,settings=None):
    if now.tzinfo is None:raise ValueError('Aware render time required')
    settings=settings or {};local=now.astimezone(ZURICH);day=display_day(now)
    relative='tomorrow' if day!=local.date() else 'today'
    try:limit=int(settings.get('max_bookings',5))
    except (TypeError,ValueError):limit=5
    limit=min(limit,6) if limit in (4,5,6,7) else 5 # legacy seven-row instances stay valid
    image=Image.new('RGB',(800,480),WHITE);draw=ImageDraw.Draw(image);fonts={}
    def font(size,bold=False):
        key=(size,bold)
        if key not in fonts:fonts[key]=ImageFont.truetype(str(FONT_DIR/('Jost-SemiBold.ttf' if bold else 'Jost.ttf')),size)
        return fonts[key]
    def label(value,box,size=18,bold=False,color=BLACK):
        left,top,right,bottom=box;value=clean(value,1600).replace('\n',' ');f=font(size,bold)
        if draw.textlength(value,font=f)>right-left:
            low,high=0,len(value)
            while low<high:
                middle=(low+high+1)//2
                if draw.textlength(value[:middle]+'…',font=f)<=right-left:low=middle
                else:high=middle-1
            value=value[:low]+'…'
        bounds=draw.textbbox((0,0),value,font=f)
        if bounds[3]-bounds[1]<=bottom-top:
            draw.text((left,top+(bottom-top-bounds[3]+bounds[1])/2-bounds[1]),value,font=f,fill=color)
    def times(duty):
        return (('<' if duty.start.date()<day else '')+duty.start.strftime('%H:%M'),
                duty.end.strftime('%H:%M')+('+' if duty.end.date()>day else ''))
    def status(duty):
        return {'UNKNOWN':'Status unknown','CONFIRMED':'Confirmed','TENTATIVE':'Tentative'}.get(duty.status,'Status unknown')
    try:
        source=result.get('source','unavailable');schedule=result.get('schedule');error=result.get('error')
        if schedule is not None and schedule.day!=day:
            schedule=None;source='unavailable';error='Schedule day changed; refresh required'
        label(settings.get('title') or 'Flight Duties',(20,10,610,33),17,True)
        label(relative.upper(),(20,35,223,77),32,True)
        label(day.strftime('%a, %d %B %Y'),(231,40,630,74),23)
        badge={'live':'LIVE','cached':'CACHED','unavailable':'OFFLINE'}.get(source,'OFFLINE')
        draw.rounded_rectangle((660,12,780,39),radius=4,outline=BLACK,width=1)
        label(badge,(673,14,775,37),14,True,color=RED if source!='live' else BLACK)
        fetched=result.get('fetched_at')
        synced=fetched.astimezone(ZURICH).strftime('%H:%M') if fetched else 'never'
        label('Sync '+synced,(661,46,780,70),15)
        draw.line((20,83,780,83),fill=BLACK,width=2)
        shown_count=0
        if schedule is None or not schedule.duties:
            draw.rounded_rectangle((20,100,780,409),radius=8,outline=BLACK,width=1)
            # Simple calendar motif, rendered entirely with Pillow.
            draw.rounded_rectangle((56,173,126,245),radius=6,outline=BLACK,width=3)
            draw.line((56,193,126,193),fill=BLACK,width=3)
            for x in (72,110):draw.line((x,166,x,183),fill=BLACK,width=4)
            for x in (73,94,113):draw.ellipse((x-2,211,x+2,215),fill=BLUE)
            text='Schedule unavailable' if schedule is None else 'No duties '+relative
            label(text,(160,173,755,224),32,True)
            detail=(error or 'No valid private calendar cached') if schedule is None else day.strftime('%A, %d %B')
            label(detail,(162,233,755,263),20)
            if source=='cached':label('Cached snapshot; duties may have changed.',(162,278,755,308),17)
        else:
            duties=schedule.duties;_,_,focus=visible_duties(duties,local,limit)
            featured=duties[focus] if focus is not None else None
            draw.rounded_rectangle((20,94,780,190),radius=7,outline=BLACK,width=2)
            draw.rectangle((21,101,27,183),fill=BLUE)
            if featured:
                start,end=times(featured)
                next_label='NEXT DUTY' if featured.start>=local else 'IN PROGRESS'
                label(next_label+' · '+status(featured),(36,101,298,122),13,True,color=BLUE)
                label(start,(36,125,184,168),42,True)
                label('– '+end,(187,135,297,168),23)
                label('LOCAL TIME',(38,170,295,184),11)
                label(featured.aircraft,(316,100,762,132),29,True)
                label(featured.activity,(316,135,762,158),20,True)
                label(featured.participants,(316,163,762,184),15)
            else:
                label('DAY COMPLETE',(36,101,750,121),13,True,color=BLUE)
                label('No upcoming duties',(36,128,750,164),30,True)
                label(str(len(duties))+' timed duties '+relative,(38,168,750,184),13)
            remaining=[(i,d) for i,d in enumerate(duties) if i!=focus]
            capacity=limit-1 if featured else min(limit,5)
            first=min(max(0,(focus or 0)-1),max(0,len(remaining)-capacity))
            shown=remaining[first:first+capacity];shown_count=len(shown)+int(featured is not None)
            label('OTHER DUTIES' if featured else 'DUTIES',(22,195,410,214),12,True)
            count=str(len(duties))+' total'
            if shown_count<len(duties):count+=' · '+str(shown_count)+' shown · '+str(len(duties)-shown_count)+' more'
            label(count,(478,195,780,214),13)
            height=198/max(4,len(shown))
            for row,(_,duty) in enumerate(shown):
                top=217+row*height;bottom=top+height-1;start,end=times(duty)
                draw.line((22,bottom,778,bottom),fill='#888888',width=1)
                label(start,(29,top+2,107,top+30),24,True)
                label('– '+end,(109,top+6,191,top+30),18)
                label(duty.aircraft,(207,top+1,348,top+25),21,True)
                label(status(duty),(208,top+25,347,bottom-1),11)
                label(duty.activity,(365,top+1,776,top+23),19,True)
                label(duty.participants,(365,top+23,776,bottom-1),13)
        draw.line((20,423,780,423),fill=BLACK,width=2)
        label('REMINDERS',(20,429,136,451),13,True,color=BLUE)
        if schedule:
            reminders=' · '.join(d.activity for d in schedule.reminders[:2])
            if len(schedule.reminders)>2:reminders+=' · +'+str(len(schedule.reminders)-2)+' more'
            label(reminders or 'None for this day',(149,428,780,452),16)
        else:label('Unavailable',(149,428,780,452),16)
        footer='Europe/Zurich · updated '+(fetched.astimezone(ZURICH).strftime('%d %b %H:%M') if fetched else 'never')
        if source=='cached':footer='STALE · CACHED · last sync '+(fetched.astimezone(ZURICH).strftime('%d %b %H:%M') if fetched else 'never')
        elif source=='unavailable':footer='UNAVAILABLE · no duties inferred from a failed request'
        if error and source!='unavailable':footer+=' · '+error
        if schedule and schedule.cancelled:footer+=' · '+str(schedule.cancelled)+' cancelled excluded'
        label(footer,(20,458,780,478),13,bold=source!='live',color=RED if source!='live' else BLACK)
        return image
    except BaseException:
        image.close();raise
