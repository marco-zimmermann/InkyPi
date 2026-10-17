"""Conservative display decoding of observation groups, not flight-rule advice."""
import math
import re
from datetime import datetime, timezone
from fractions import Fraction

SM_KM = 1.609344
WX = {'DZ':'drizzle','RA':'rain','SN':'snow','SG':'snow grains','IC':'ice crystals',
      'PL':'ice pellets','GR':'hail','GS':'small hail','UP':'unknown precipitation',
      'BR':'mist','FG':'fog','FU':'smoke','VA':'volcanic ash','DU':'dust','SA':'sand',
      'HZ':'haze','PY':'spray','PO':'dust whirls','SQ':'squalls','FC':'funnel cloud',
      'SS':'sandstorm','DS':'duststorm'}
DESCRIPTORS = {'MI':'shallow','PR':'partial','BC':'patches','DR':'drifting','BL':'blowing',
               'SH':'showers','TS':'thunderstorm','FZ':'freezing'}


def validate_icao(value):
    value = str(value).strip().upper()
    if not re.fullmatch('[A-Z]{4}', value):
        raise ValueError('ICAO must contain exactly four letters')
    return value


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def observation_time(record):
    value = number(record.get('obsTime'))
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def select_report(payload, icao):
    """Never substitute another station or use receipt time as observation time."""
    if not isinstance(payload, list):
        raise ValueError('METAR response must be an array')
    candidates = []
    for row in payload:
        if not isinstance(row, dict) or row.get('icaoId') != icao:
            continue
        raw = row.get('rawOb')
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 8192:
            continue
        # Optional METAR/SPECI and COR prefixes can precede the station.
        tokens = raw.split()
        while tokens and tokens[0] in ('METAR','SPECI','COR'):
            tokens.pop(0)
        if not tokens or tokens[0] != icao:
            continue
        observed = observation_time(row)
        if observed is not None:
            candidates.append((observed, 'COR' in raw.split(), row))
    return max(candidates, key=lambda item: item[:2])[2] if candidates else None


def visibility(tokens):
    if 'CAVOK' in tokens:
        return {'km':10.0,'bound':'>='}
    for i, token in enumerate(tokens):
        if re.fullmatch(r'\d{4}(?:NDV)?', token):
            value = int(token[:4])
            return {'km':10.0 if value == 9999 else (0.05 if value == 0 else value / 1000),
                    'bound':'>=' if value == 9999 else '<' if value == 0 else '='}
        match = re.fullmatch(r'([MP]?)(\d+(?:/\d+)?)SM', token)
        if match:
            try:
                value = float(Fraction(match[2]))
                if '/' in match[2] and i and re.fullmatch(r'\d',tokens[i-1]):
                    value += int(tokens[i-1])
                return {'km':value*SM_KM,'bound':{'M':'<','P':'>','':'='}[match[1]]}
            except (ValueError, ZeroDivisionError):
                pass
    return None


def category(visibility_km, ceiling_ft, sky_known, bound='='):
    """NWS category thresholds; unknown components cannot imply VFR.

    A known LIFR component establishes LIFR even if the other is unknown.
    Lower/upper visibility bounds spanning categories remain UNKNOWN.
    """
    def vis_rank(km):
        sm = km / SM_KM
        return 0 if sm < 1 else 1 if sm < 3 else 2 if sm <= 5 else 3
    v = None if visibility_km is None else vis_rank(visibility_km)
    if visibility_km is not None:
        if bound == '<':
            v = 0 if visibility_km <= SM_KM else None
        elif bound in ('>','>='):
            v = 3 if visibility_km >= 5*SM_KM and (bound == '>' or visibility_km > 5*SM_KM) else None
    h = (3 if sky_known else None) if ceiling_ft is None else (0 if ceiling_ft < 500 else 1 if ceiling_ft < 1000 else 2 if ceiling_ft <= 3000 else 3)
    if not sky_known and h != 0:
        h = None
    if v == 0 or h == 0:
        return 'LIFR'
    if v is None or h is None:
        return 'UNKNOWN'
    return ('LIFR','IFR','MVFR','VFR')[min(v,h)]


def weather(tokens):
    descriptions, codes = [], []
    for token in tokens:
        m = re.fullmatch(r'([+-]?)(VC)?(MI|PR|BC|DR|BL|SH|TS|FZ)?((?:(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS))*)',token)
        if not m or not (m[3] or m[4]):
            continue
        phenomena = [m[4][i:i+2] for i in range(0,len(m[4]),2)]
        codes.extend(([m[3]] if m[3] else [])+phenomena)
        words = ([{'-':'light','+':'heavy'}[m[1]]] if m[1] else [])
        if m[2]: words.append('vicinity')
        if m[3]: words.append(DESCRIPTORS[m[3]])
        words.extend(WX[x] for x in phenomena)
        descriptions.append(' '.join(words))
    return descriptions, codes


def decode(record):
    raw = record.get('rawOb','') if record else ''
    tokens = raw.rstrip('=').split()
    # Remarks and trend forecasts must not alter the current observation.
    stop = next((i for i,x in enumerate(tokens) if x in ('RMK','TEMPO','BECMG','NOSIG')),len(tokens))
    tokens = tokens[:stop]
    nil = 'NIL' in tokens
    result = dict(raw=raw, flags=[x for x in ('AUTO','COR','NIL') if x in tokens],
                  wind=None,variable=None,visibility=None,layers=[],ceiling=None,
                  sky_known=False,temperature=None,dewpoint=None,qnh=None,
                  weather=[],symbol='unknown',category='UNKNOWN')
    if nil:
        return result
    for token in tokens:
        m = re.fullmatch(r'(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)',token)
        if m and (m[1]=='VRB' or int(m[1])<=360):
            factor = 1 if m[4]=='KT' else 1.943844
            result['wind'] = {'direction':m[1],'speed':round(int(m[2])*factor,1),
                              'gust':round(int(m[3])*factor,1) if m[3] else None}
        if re.fullmatch(r'\d{3}V\d{3}',token) and all(int(x)<=360 for x in token.split('V')):
            result['variable'] = token
        m = re.fullmatch(r'(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(CB|TCU)?',token)
        if m: result['layers'].append({'cover':m[1],'height':None if m[2]=='///' else int(m[2])*100,'type':m[3]})
        m = re.fullmatch(r'(M?\d{2}|//)/(M?\d{2}|//)',token)
        if m:
            for key,value in zip(('temperature','dewpoint'),m.groups()):
                result[key] = None if value=='//' else int(value.replace('M','-'))
        if re.fullmatch(r'Q\d{4}',token):result['qnh']=int(token[1:])
        elif re.fullmatch(r'A\d{4}',token):result['qnh']=round(int(token[1:])/100*33.8638866667,1)
    result['visibility']=visibility(tokens)
    layers = result['layers']
    significant = [x for x in layers if x['cover'] in ('BKN','OVC','VV')]
    unknown = any(x['height'] is None for x in significant) or any('///' in x for x in tokens if x.startswith(('FEW','SCT','BKN','OVC','VV')))
    result['sky_known'] = not unknown and (bool(layers) or any(x in tokens for x in ('CAVOK','SKC','CLR','NSC','NCD')))
    known_heights=[x['height'] for x in significant if x['height'] is not None]
    if known_heights:result['ceiling']=min(known_heights)
    result['weather'],codes=weather(tokens)
    if 'TS' in codes:result['symbol']='thunderstorm'
    elif any(x in codes for x in ('SN','SG','IC','PL','GR','GS')):result['symbol']='snow'
    elif 'SH' in codes:result['symbol']='showers'
    elif any(x in codes for x in ('RA','DZ','UP')):result['symbol']='rain'
    elif 'FG' in codes:result['symbol']='fog'
    elif any(x in codes for x in ('BR','HZ','FU','DU','SA','VA','DS','SS')):result['symbol']='mist'
    elif result['sky_known']:
        result['symbol']='overcast' if significant else 'partly_cloudy' if layers else 'clear' if any(x in tokens for x in ('CAVOK','SKC','CLR')) else 'unknown'
    v=result['visibility']
    result['category']=category(v['km'] if v else None,result['ceiling'],result['sky_known'],v['bound'] if v else '=')
    return result
