"""Original vector symbols and Pillow layout; no browser or display imports."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from .decoding import decode, observation_time

FONT_DIR=Path(__file__).resolve().parents[2]/'static/fonts'
BLACK=(0,0,0);WHITE=(255,255,255);RED=(255,0,0);BLUE=(0,0,255);GREEN=(0,255,0);YELLOW=(255,255,0)
COLORS={'VFR':GREEN,'MVFR':BLUE,'IFR':RED,'LIFR':RED,'UNKNOWN':BLACK}


def font(size,bold=False):
    return ImageFont.truetype(str(FONT_DIR/('Jost-SemiBold.ttf' if bold else 'Jost.ttf')),size)


def fit_text(draw,text,width,size=22,minimum=16,bold=False):
    text=str(text)
    minimum=min(minimum,size)
    for n in range(size,minimum-1,-1):
        f=font(n,bold)
        if draw.textlength(text,font=f)<=width:return text,f
    f=font(minimum,bold)
    while text and draw.textlength(text+'…',font=f)>width:text=text[:-1]
    return text+'…',f


def wrap_text(draw,text,width,f):
    lines=[]
    for paragraph in str(text).splitlines() or ['']:
        line=''
        for word in paragraph.split():
            if line and draw.textlength(line+' '+word,font=f)>width:
                lines.append(line);line=''
            if draw.textlength(word,font=f)>width:
                # Preserve tokens: leave overflow for explicit truncation handling.
                if line:lines.append(line);line=''
                lines.append(word)
            else:line=(line+' '+word).strip()
        if line:lines.append(line)
    return lines


def symbol(image, kind, box=(20, 110, 240, 275)):
    """Original bold vector artwork, scaled as one coherent weather symbol."""
    icon = Image.new('RGB', (240, 180), WHITE)
    draw = ImageDraw.Draw(icon)
    if kind == 'unknown':
        draw.ellipse((55, 20, 185, 150), outline=BLACK, width=6)
        draw.text((120, 34), '?', font=font(90, True), fill=BLACK, anchor='mt')
    else:
        if kind in ('clear', 'partly_cloudy', 'showers'):
            cx, cy = (120, 88) if kind == 'clear' else (75, 57)
            radius = 49 if kind == 'clear' else 32
            draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius),
                         fill=YELLOW, outline=BLACK, width=5)
            for dx, dy in ((0,-1),(0,1),(-1,0),(1,0),(-.707,-.707),(.707,-.707),(-.707,.707),(.707,.707)):
                draw.line((cx+dx*(radius+12), cy+dy*(radius+12),
                           cx+dx*(radius+26), cy+dy*(radius+26)), fill=BLACK, width=5)
        if kind != 'clear':
            cloud = Image.new('L', icon.size, 0)
            cloud_draw = ImageDraw.Draw(cloud)
            cloud_draw.ellipse((38, 57, 117, 128), fill=255)
            cloud_draw.ellipse((80, 29, 174, 127), fill=255)
            cloud_draw.ellipse((139, 65, 208, 128), fill=255)
            cloud_draw.rectangle((77, 83, 174, 127), fill=255)
            icon.paste(BLACK, mask=cloud.filter(ImageFilter.MaxFilter(11)))
            icon.paste(WHITE, mask=cloud)
            if kind in ('rain', 'showers', 'thunderstorm'):
                for x in (72, 119, 166):
                    draw.line((x, 143, x-12, 166), fill=BLACK, width=8)
                    draw.line((x, 143, x-12, 166), fill=BLUE, width=4)
            if kind == 'thunderstorm':
                draw.polygon([(127,88),(101,129),(120,129),(104,169),(153,117),(130,117)],
                             fill=YELLOW, outline=BLACK, width=3)
            if kind == 'snow':
                for x in (70, 120, 170):
                    for a,b in (((-9,0),(9,0)),((0,-9),(0,9)),((-7,-7),(7,7)),((-7,7),(7,-7))):
                        draw.line((x+a[0],153+a[1],x+b[0],153+b[1]),fill=BLACK,width=3)
            if kind in ('fog', 'mist'):
                for y in (140, 153, 166):
                    draw.line((34,y,211,y),fill=BLACK,width=4 if kind=='fog' else 3)
    left, top, right, bottom = box
    image.paste(icon.resize((right-left, bottom-top), Image.Resampling.LANCZOS), (left, top))


def render_dashboard(icao,state,airport,now,example=False):
    image=Image.new('RGB',(800,480),WHITE)
    draw=ImageDraw.Draw(image)
    record=state.get('report')
    decoded=decode(record)
    obs=observation_time(record) if record else None

    def text(x,y,value,width,size=22,bold=False,fill=BLACK):
        value,f=fit_text(draw,value,width,size,bold=bold)
        # Top anchoring makes the allocated vertical space independent of glyph bearings.
        draw.text((x,y),value,font=f,fill=fill,anchor='lt')

    title=airport.get('name') or (record or {}).get('name') or 'Airport name unavailable'
    text(20,10,icao,165,58,bold=True)
    text(198,14,title,350,27)
    elevation=airport.get('elevation_ft')
    text(200,49,'ELEV '+(f'{elevation:g} ft' if elevation is not None else 'unavailable'),340,20)
    category=decoded['category']
    color=COLORS[category]
    draw.rounded_rectangle((578,12,780,66),radius=5,fill=color,outline=BLACK,width=2)
    text(592,23,category,176,34,bold=True,fill=WHITE if color in (BLACK,BLUE,RED) else BLACK)
    if example:
        status='EXAMPLE FIXTURE — NOT LIVE'
    elif record is None or 'NIL' in decoded['flags']:
        status='UNAVAILABLE — no usable observation'
    else:
        status=(('CACHED / STALE' if state.get('stale') else 'CACHED — fetch failed')
                if state.get('cached') else ('STALE' if state.get('stale') else 'CURRENT'))
    observed=obs.strftime('%d %b %H:%MZ') if obs else 'unknown'
    age=max(0,int((now-obs).total_seconds()/60)) if obs else None
    text(20,81,status,390,18,fill=RED if state.get('stale') or state.get('cached') else BLACK)
    text(418,78,'OBS '+observed+(f' · {age} min' if age is not None else ''),362,22,bold=True)
    draw.line((20,105,780,105),fill=BLACK,width=2)

    symbol(image,decoded['symbol'])
    description=' / '.join(decoded['weather']) or {
        'clear':'CAVOK' if 'CAVOK' in decoded['raw'].split() else 'Clear',
        'partly_cloudy':'Partly cloudy','overcast':'Overcast'
    }.get(decoded['symbol'],'Conditions unknown')
    text(20,285,description,220,22)
    draw.line((251,115,251,305),fill=BLACK,width=1)

    # Wind occupies its own column: direction, speed, gusts, and range never merge.
    text(269,116,'WIND',160,16)
    wind=decoded['wind']
    if wind:
        calm=wind['speed']==0
        direction='CALM' if calm else wind['direction']+('°' if wind['direction']!='VRB' else '')
        text(269,143,direction,160,43,bold=True)
        text(269,191,f"{wind['speed']:g} kt",160,36,bold=True)
        if wind['gust'] is not None:
            text(269,239,f"Gust {wind['gust']:g} kt",160,22)
        if decoded['variable']:
            low,high=decoded['variable'].split('V')
            text(269,273,f'{low}°–{high}°',160,20)
    else:
        text(269,148,'Unavailable',160,26)

    def metric(x,y,label,value,width=157,size=42,detail=None):
        text(x,y,label,width,16)
        text(x,y+27,value,width,size,bold=True)
        if detail:
            text(x,y+73,detail,width,18)

    visibility=decoded['visibility']
    if visibility:
        prefix={'=':'','>=':'≥','>':'>','<':'<'}[visibility['bound']]
        visibility_value=f"{prefix}{visibility['km']:g}"
    else:
        visibility_value='Unknown'
    metric(448,116,'VISIBILITY',visibility_value,detail='km' if visibility else None)
    ceiling=decoded['ceiling']
    ceiling_value=f'{ceiling:g}' if ceiling is not None else 'None' if decoded['sky_known'] else 'Unknown'
    metric(623,116,'CEILING',ceiling_value,detail='ft AGL' if ceiling is not None else 'reported' if decoded['sky_known'] else None)
    temperature=decoded['temperature']
    dew=decoded['dewpoint']
    metric(448,222,'TEMPERATURE',f'{temperature:g}°C' if temperature is not None else 'Unknown',
           size=38,detail='Dew '+(f'{dew:g}°C' if dew is not None else 'unknown'))
    qnh=decoded['qnh']
    metric(623,222,'QNH',f'{qnh:g}' if qnh is not None else 'Unknown',size=38,detail='hPa' if qnh is not None else None)

    layers='   '.join(layer['cover']+(' ???' if layer['height'] is None else ' '+str(layer['height']))+
                      ((' '+layer['type']) if layer['type'] else '') for layer in decoded['layers'])
    if not layers:
        layers='No significant cloud' if decoded['sky_known'] else 'Cloud information unavailable'
    text(20,320,layers+(' ft AGL' if decoded['layers'] else '')+
         ('  ·  '+' / '.join(decoded['flags']) if decoded['flags'] else ''),760,19)
    draw.line((20,344,780,344),fill=BLACK,width=1)

    # Provenance stays next to the aerodrome table, rather than implying AIP authority.
    text(20,355,'OurAirports · community reference',374,19)
    metadata_status=airport.get('status','metadata unavailable')
    text(20,383,airport.get('as_of','unknown')+' · '+metadata_status,374,16,
         fill=RED if 'STALE' in metadata_status else BLACK)
    text(20,410,'Not an official AIP  ·  METAR: AWC',374,16)

    table_x=419
    text(table_x,352,'RUNWAY',190,15)
    text(624,352,'LENGTH',156,15)
    draw.line((table_x,371,780,371),fill=BLACK,width=1)
    runways=airport.get('runways',[])
    if not runways:
        text(table_x,383,'Runway information unavailable',361,20)
    else:
        visible=runways[:3] if len(runways)<=3 else runways[:2]
        for i,row in enumerate(visible):
            y=377+i*19
            text(table_x,y,row['ident'],190,19)
            text(624,y,f"{row['length_m']} m" if row.get('length_m') is not None else 'Unknown',156,19)
        if len(runways)>3:
            text(table_x,415,f'+ {len(runways)-2} more runways',361,18)
    draw.line((20,437,780,437),fill=BLACK,width=1)

    raw=decoded['raw'] or 'METAR unavailable — '+(state.get('error') or 'No report')
    footer_font=font(16)
    lines=wrap_text(draw,raw,760,footer_font)
    if len(lines)>2 or any(draw.textlength(line,font=footer_font)>760 for line in lines):
        text(20,442,'RAW TOO LONG — FULL REPORT NOT SHOWN',760,15,fill=RED)
        lines=lines[:1]
        top=460
    else:
        top=442
    for i,line in enumerate(lines):
        text(20,top+i*18,line,760,16)
    palette=Image.new('P',(1,1))
    palette.putpalette(list(BLACK+WHITE+RED+BLUE+GREEN+YELLOW)+[0]*750)
    return image.quantize(palette=palette,dither=Image.Dither.NONE).convert('RGB')
