"""Pure fixed-layout RGB renderer. No network, clock, subprocess or display IO.

Only the chart uses a 2x workspace. Original icons are opened on demand; all
fonts are bounded to this single render. Used by the Weather plugin and standalone offline previews. This is intentionally
not a CSS/Chart.js compatibility engine.
"""
import math
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

FONT_DIR = Path(__file__).resolve().parents[2] / 'static/fonts'
ICON_DIR = Path(__file__).resolve().parents[2] / 'plugins/weather/icons'


def enabled(settings, name):
    return settings.get(name) in (True, 'true')


def numeric(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError, OverflowError):
        return None


def shown(value):
    return 'N/A' if value is None or value == '' else str(value)


class Painter:
    def __init__(self, image, color):
        self.image = image
        self.draw = ImageDraw.Draw(image)
        self.color = color
        self.fonts = {}

    def font(self, size, bold=False):
        size = max(8, min(76, int(size)))
        key = (size, bool(bold))
        if key not in self.fonts:
            self.fonts[key] = ImageFont.truetype(
                str(FONT_DIR / ('Jost-SemiBold.ttf' if bold else 'Jost.ttf')), size)
        return self.fonts[key]

    def text(self, box, value, size=16, bold=False, align='center', minimum=9):
        """Fit both width and ink height; clip nothing silently."""
        left, top, right, bottom = box
        text = shown(value).replace('\n', ' ')
        width, height = right-left, bottom-top
        if width <= 0 or height <= 0:
            return
        for n in range(size, min(size, minimum)-1, -1):
            font = self.font(n, bold)
            bounds = self.draw.textbbox((0, 0), text, font=font)
            if bounds[2]-bounds[0] <= width and bounds[3]-bounds[1] <= height:
                break
        else:
            # Explicit ellipsis for overlong fields, never spill into neighbors.
            while text and self.draw.textlength(text+'…', font=font) > width:
                text = text[:-1]
            text += '…'
            bounds = self.draw.textbbox((0, 0), text, font=font)
            if bounds[2]-bounds[0] > width or bounds[3]-bounds[1] > height:
                return
        x = left if align == 'left' else right-(bounds[2]-bounds[0]) if align == 'right' else (left+right-bounds[2]+bounds[0])/2
        y = (top+bottom-bounds[3]+bounds[1])/2
        self.draw.text((round(x-bounds[0]), round(y-bounds[1])), text,
                       font=font, fill=self.color)

    def arrow(self, arrow, box):
        """Draw the parser's eight-way wind arrow without font glyph fallback."""
        vectors = {'↑': (0, -1), '↗': (1, -1), '→': (1, 0), '↘': (1, 1),
                   '↓': (0, 1), '↙': (-1, 1), '←': (-1, 0), '↖': (-1, -1)}
        if arrow not in vectors:
            return
        left, top, right, bottom = box
        radius = min(right-left, bottom-top)/2-2
        if radius < 2:
            return
        dx, dy = vectors[arrow]
        norm = math.hypot(dx, dy)
        dx, dy = dx/norm, dy/norm
        cx, cy = (left+right)/2, (top+bottom)/2
        tip = (cx+dx*radius, cy+dy*radius)
        tail = (cx-dx*radius, cy-dy*radius)
        self.draw.line((tail, tip), fill=self.color, width=2)
        back = (tip[0]-dx*4, tip[1]-dy*4)
        self.draw.polygon((tip, (back[0]-dy*3, back[1]+dx*3),
                          (back[0]+dy*3, back[1]-dx*3)), fill=self.color)

    def icon(self, path, box):
        if not path:
            return
        # Parsed icons are local assets only. No URL/background fetching.
        target = Path(path).resolve()
        if target.parent != ICON_DIR.resolve() or target.suffix.lower() != '.png':
            raise ValueError('Only original Weather PNG assets are supported')
        left, top, right, bottom = (round(n) for n in box)
        with Image.open(target) as source:
            with source.convert('RGBA') as rgba:
                rgba.thumbnail((max(1, right-left), max(1, bottom-top)), Image.Resampling.LANCZOS)
                self.image.paste(rgba, (left+(right-left-rgba.width)//2,
                                       top+(bottom-top-rgba.height)//2), rgba)


def curve_points(points, samples=12):
    """Bounded, shape-preserving cubic Hermite curve; no temperature overshoot.

    Deliberately approximates the dashboard curve rather than claiming Chart.js
    control-point or rasterization identity. Missing-data segments are separate.
    """
    if len(points) < 2:
        return points
    slopes = [(b[1]-a[1])/(b[0]-a[0]) for a, b in zip(points, points[1:])]
    tangents = [slopes[0]] + [0 if a*b <= 0 else 2*a*b/(a+b)
                              for a, b in zip(slopes, slopes[1:])] + [slopes[-1]]
    out = []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        dx = b[0]-a[0]
        for step in range(samples):
            t = step/samples
            y = ((2*t**3-3*t*t+1)*a[1] + (t**3-2*t*t+t)*dx*tangents[i]
                 + (-2*t**3+3*t*t)*b[1] + (t**3-t*t)*dx*tangents[i+1])
            out.append((a[0]+t*dx, max(min(a[1], b[1]), min(max(a[1], b[1]), y))))
    return out + [points[-1]]


def rain_annotations(hours, xs, bar_tops, baseline, limits, *, imperial=False, measure=None):
    """Plan labels greedily in time order, skipping collisions rather than overlap."""
    threshold, unit = (.0035, 'in') if imperial else (.09, 'mm')
    occupied, labels = [], []
    measure = measure or (lambda text: len(text)*6)
    left, top, right, bottom = limits
    for i, hour in enumerate(hours):
        rain = numeric(hour.rain)
        if rain is None or rain <= threshold:
            continue
        value = f'{rain:.2f}'
        width = max(measure(value), measure(unit))+3
        if width > right-left:
            continue
        x = max(left, min(right-width, xs[i]-width/2))
        preferred = bar_tops[i]+4 if baseline-bar_tops[i] >= 25 else bar_tops[i]-22
        for y in (preferred, preferred-20, preferred+20):
            y = max(top, min(bottom-20, y))
            box = (x, y, x+width, y+20)
            if not any(box[0] < b[2]+2 and box[2]+2 > b[0] and
                       box[1] < b[3]+2 and box[3]+2 > b[1] for b in occupied):
                occupied.append(box)
                labels.append((box, value, unit, i))
                break
    return labels


def draw_chart(painter, dashboard, settings, bounds, background):
    """All chart layers share one bounded 2x RGB image, with disposable masks."""
    left, top, right, bottom = map(int, bounds)
    width, height = right-left, bottom-top
    scale = 2
    with Image.new('RGB', (width*scale, height*scale), background) as chart:
        d = ImageDraw.Draw(chart)
        p = Painter(chart, painter.color)
        plot = (28, 2, width-36, height-(47 if enabled(settings, 'displayGraphIcons') else 23))
        x0, y0, x1, base = plot
        hours = dashboard.hourly_forecast
        if not hours:
            p.text((0, 0, width*scale, height*scale), 'Hourly forecast unavailable', 24)
        else:
            step = (x1-x0)/len(hours)
            xs = [x0+(i+.5)*step for i in range(len(hours))]
            temps = [numeric(h.temperature) for h in hours]
            valid = [v for v in temps if v is not None]
            low, high = (min(valid), max(valid)) if valid else (0, 1)
            span = high-low or 2
            lower = low if high != low else low-1
            ys = [base-(v-lower)/span*(base-y0) if v is not None else None for v in temps]
            prob = [numeric(h.precipitation) for h in hours]
            bar_tops = [base-max(0, min(1, v))*(base-y0) if v is not None else base for v in prob]
            # Probability bars are independent of precipitation amounts.
            with Image.new('L', chart.size, 0) as mask:
                md = ImageDraw.Draw(mask)
                for i, y in enumerate(bar_tops):
                    if y < base:
                        md.rectangle((round((xs[i]-step/2)*2), round(y*2),
                                      round((xs[i]+step/2)*2), base*2), fill=255)
                _gradient(chart, mask, (26, 111, 176), .8, 0, y0*2, base*2)
            for i, y in enumerate(bar_tops):
                if y < base:
                    d.line(((xs[i]-step/2)*2, y*2, (xs[i]+step/2)*2, y*2), fill=(26,111,176), width=4)
            segments, segment = [], []
            for x, y in zip(xs, ys):
                if y is None:
                    if segment: segments.append(segment)
                    segment = []
                else:
                    segment.append((x, y))
            if segment: segments.append(segment)
            with Image.new('L', chart.size, 0) as mask:
                md = ImageDraw.Draw(mask)
                for points in segments:
                    smooth = curve_points(points)
                    polygon = [(x*2, y*2) for x, y in smooth] + [(points[-1][0]*2, base*2), (points[0][0]*2, base*2)]
                    if len(smooth) > 1: md.polygon(polygon, fill=255)
                _gradient(chart, mask, (252,204,5), .95, .01, y0*2, (base+10)*2)
            for points in segments:
                if len(points) > 1:
                    d.line([(round(x*2), round(y*2)) for x, y in curve_points(points)], fill=(243,135,58), width=4, joint='curve')
                else:
                    x,y=points[0]; d.ellipse((x*2-2,y*2-2,x*2+2,y*2+2),fill=(243,135,58))
            d.line((x0*2, y0*2, x0*2, base*2), fill='#dedede', width=1)
            d.line((x0*2, base*2, x1*2, base*2), fill='#dedede', width=1)
            degree = '' if dashboard.units == 'standard' else '°'
            for value, y in [(high, y0), (low, base-10)]:
                p.text((0,y*2,(x0-5)*2,(y+12)*2), f'{value:g}{degree}' if valid else 'N/A', 22, align='right')
            p.text(((x1+6)*2,0,width*2,24), '100%', 22)
            p.text(((x1+6)*2,(base-10)*2,width*2,(base+2)*2), '0%', 22)
            if enabled(settings, 'displayRain'):
                measure=lambda s: p.draw.textlength(s,font=p.font(21))/2
                for box,value,unit,_ in rain_annotations(hours,xs,bar_tops,base,plot,
                                                         imperial=dashboard.units=='imperial',measure=measure):
                    x,y,r,b=box
                    p.text((x*2,y*2,r*2,(y+11)*2),value,21)
                    p.text((x*2,(y+10)*2,r*2,b*2),unit,19)
            # Deterministic tick skipping; full original hourly labels remain in model.
            stride = max(1, math.ceil(len(hours)/12))
            for i in range(0,len(hours),stride):
                p.text(((xs[i]-step)*2,(base+10)*2,(xs[i]+step)*2,(base+23)*2),hours[i].time,22)
            if enabled(settings,'displayGraphIcons'):
                icon_step = max(1,int(settings.get('graphIconStep') or 2))
                for i in range(0,len(hours),icon_step):
                    p.icon(hours[i].icon, ((xs[i]-11)*2,(base+25)*2,(xs[i]+11)*2,(base+47)*2))
        with chart.resize((width,height),Image.Resampling.LANCZOS) as small:
            painter.image.paste(small,(left,top))


def _gradient(image, mask, color, start_alpha, end_alpha, top, bottom):
    """Scanline gradient compositing, no per-pixel Python lists or float arrays."""
    with Image.new('RGBA',(1,image.height)) as strip:
        sd=ImageDraw.Draw(strip)
        for y in range(image.height):
            fraction=max(0,min(1,(y-top)/max(1,bottom-top)))
            sd.point((0,y),fill=(*color,round(255*(start_alpha+(end_alpha-start_alpha)*fraction))))
        with strip.resize(image.size) as layer:
            # Multiply the polygon/bar mask by gradient alpha.
            from PIL import ImageChops
            with layer.getchannel('A') as alpha:
                with ImageChops.multiply(alpha,mask) as combined:
                    image.paste(layer,(0,0),combined)


def render_dashboard(dashboard, settings, dimensions=(800,480)):
    """Return caller-owned RGB image; unsupported layouts fail explicitly."""
    if tuple(dimensions)!=(800,480):
        raise ValueError('Pillow Weather supports only 800 x 480 landscape')
    if settings.get('backgroundOption','color')!='color':
        raise ValueError('Pillow Weather does not support background images')
    background=ImageColor.getrgb(settings.get('backgroundColor') or '#ffffff')
    color=ImageColor.getrgb(settings.get('textColor') or '#000000')
    image=Image.new('RGB',dimensions,background)
    try:
        p=Painter(image,color)
        margins=[int(settings.get(k) or settings.get('margin') or 5) for k in ('leftMargin','topMargin','rightMargin','bottomMargin')]
        if any(v<0 or v>30 for v in margins):
            raise ValueError('Pillow Weather margins must be between 0 and 30 pixels')
        # Lay out once in the canonical 5 px-margin coordinate system. Optional
        # margins remap this native-resolution content below; no full-frame 2x
        # surface or partial reflow that could overlap the fixed bands.
        ml=mt=mr=mb=5
        frame=settings.get('selectedFrame') or 'None'
        if frame=='Rectangle': p.draw.rectangle((ml,mt,799-mr,479-mb),outline=color,width=5)
        elif frame=='Top and Bottom':
            for y in (mt,479-mb):p.draw.line((ml,y,799-mr,y),fill=color,width=5)
        elif frame=='Corner':
            p.draw.line((ml,mt+48,ml,mt,ml+80,mt),fill=color,width=5)
            p.draw.line((799-mr-80,479-mb,799-mr,479-mb,799-mr,479-mb-48),fill=color,width=5)
        elif frame!='None': raise ValueError('Unsupported frame')
        left,right=ml+12,800-mr-12
        # Content is inset using the same 12 px padding at 800 px as base CSS.
        p.text((left,mt+18,right,mt+49),dashboard.title,28,True,minimum=16)
        p.text((left,mt+47,right,mt+68),dashboard.current_date,19)
        if enabled(settings,'displayRefreshTime'):
            p.text((right-280,mt+13,right,mt+24),'Last refresh: '+dashboard.last_refresh_time,10,True,align='right')
        metrics=enabled(settings,'displayMetrics')
        split=400 if metrics else 600
        p.icon(dashboard.current_day_icon,(left+8,76,left+200,226))
        temp_right=split-17
        p.text((left+220,104,temp_right-35,171),dashboard.current_temperature,72,minimum=25)
        p.text((temp_right-38,109,temp_right,135),dashboard.temperature_unit,21)
        suffix='' if dashboard.units=='standard' else '°'
        p.text((left+200,165,temp_right,187),'Feels Like '+shown(dashboard.feels_like)+suffix,15)
        today=dashboard.forecast[0] if dashboard.forecast else None
        p.text((left+200,188,temp_right,209),
               shown(today.high if today else None)+suffix+' / '+shown(today.low if today else None)+suffix,17)
        if metrics:
            for i,m in enumerate(dashboard.data_points[:8]):
                x=400+(i%2)*(right-400+8)/2
                y=75+(i//2)*37.5
                w=(right-400-8)/2
                p.icon(m.icon,(x,y,x+47,y+35))
                p.text((x+47,y,x+w,y+14),m.label,11)
                # Fit measured mixed-size runs into the value box.
                value=shown(m.measurement)
                unit=m.unit.strip()
                arrow_width=16 if m.arrow else 0
                value_right=x+w-arrow_width
                size=16
                while size>10 and p.draw.textlength(value,font=p.font(size,True))+p.draw.textlength(unit,font=p.font(10))+4>w-47-arrow_width:
                    size-=1
                vw=p.draw.textlength(value,font=p.font(size,True));uw=p.draw.textlength(unit,font=p.font(10)) if unit else 0
                start=x+47+max(0,(w-47-arrow_width-vw-uw-3)/2)
                p.text((start,y+13,min(value_right,start+vw+1),y+33),value,size,True,align='left')
                if unit:p.text((min(value_right,start+vw+3),y+17,value_right,y+31),unit,10,align='left')
                if m.arrow:p.arrow(m.arrow,(value_right,y+15,x+w,y+31))
        graph_icons=enabled(settings,'displayGraphIcons')
        if enabled(settings,'displayGraph'):
            draw_chart(p,dashboard,settings,(left,237,right,350 if graph_icons else 334),background)
        if enabled(settings,'displayForecast'):
            count=int(settings.get('forecastDays') or 7)
            if count not in (3,5,7):raise ValueError('forecastDays must be 3, 5 or 7')
            cards=dashboard.forecast[1:count+1]
            top=355 if graph_icons else 349
            bottom=480-mb-12
            width=(right-left-12*(count-1))/count
            moon=enabled(settings,'moonPhase')
            for i,day in enumerate(cards):
                x=left+i*(width+12);r=x+width
                p.draw.rounded_rectangle((round(x),top,round(r),bottom),radius=9,outline=color,width=1)
                p.text((x+4,top+4,r-4,top+24),day.day,17,True)
                p.icon(day.icon,(x+12,top+17,r-12,top+61))
                p.text((x+3,top+65,r-3,top+84),shown(day.high)+suffix+' / '+shown(day.low)+suffix,15)
                if moon:
                    p.draw.line((x+7,top+83,r-7,top+83),fill='#aaaaaa',width=1)
                    p.icon(day.moon_phase_icon,(x+18,top+87,x+36,bottom-3))
                    p.text((x+39,top+87,r-7,bottom-3),shown(day.moon_phase_pct)+' %',16,align='right')
            if not cards:p.text((left,top,right,bottom),'Daily forecast unavailable',18)
        if margins != [5,5,5,5]:
            ml,mt,mr,mb=margins
            with image.crop((5,5,795,475)) as content:
                with content.resize((800-ml-mr,480-mt-mb),Image.Resampling.LANCZOS) as resized:
                    result=Image.new('RGB',dimensions,background)
                    result.paste(resized,(ml,mt))
            image.close()
            return result
        return image
    except BaseException:
        image.close()
        raise
