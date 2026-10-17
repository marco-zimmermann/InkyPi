"""Bounded aviation-data I/O and independent per-station caches."""
import csv
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import requests
from urllib3.exceptions import ReadTimeoutError
from .decoding import validate_icao, select_report, observation_time, number

log = logging.getLogger(__name__)
AWC = 'https://aviationweather.gov/api/data/metar'
AIRPORTS = 'https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/'
USER_AGENT = 'PiFrame-InkyPi-METAR/1.0 (local e-ink dashboard)'
TIMEOUT = (3, 8)
STALE_SECONDS = 90 * 60  # Display heuristic, not a regulatory expiration.
METADATA_TTL = 30 * 86400


def make_session():
    session = requests.Session()  # Default adapters have zero automatic retries.
    session.headers['User-Agent'] = USER_AGENT
    return session


def _exception_chain(error):
    """Inspect bounded exception metadata, never untrusted exception messages."""
    chain, pending, seen = [], [error], set()
    while pending and len(chain) < 6:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend(value for value in
                       (current.__cause__, current.__context__, *current.args[:6])
                       if isinstance(value, BaseException))
    return chain


def _request_failure(error, phase):
    """Allowlisted diagnostics omit URLs, credentials, bodies and raw messages."""
    chain = _exception_chain(error)
    if not isinstance(error, Exception):
        outcome, detail = 'interrupted', 'request interrupted'
    elif isinstance(error, requests.ConnectTimeout):
        outcome, detail = 'connect_timeout', 'connection timeout=%ss' % TIMEOUT[0]
    elif isinstance(error, requests.ReadTimeout) or any(isinstance(e, ReadTimeoutError) for e in chain):
        outcome, detail = 'read_timeout', 'read inactivity timeout=%ss' % TIMEOUT[1]
    elif isinstance(error, requests.HTTPError):
        outcome, detail = 'http_error', 'HTTP status rejected'
    elif isinstance(error, requests.exceptions.SSLError):
        outcome, detail = 'tls_error', 'TLS connection/verification failure'
    elif isinstance(error, requests.exceptions.ProxyError):
        outcome, detail = 'proxy_error', 'proxy connection failure'
    elif isinstance(error, requests.Timeout):
        outcome, detail = 'timeout', 'request timeout'
    elif isinstance(error, requests.RequestException):
        outcome, detail = 'transport_error', 'request transport failure'
    elif phase == 'decode_json':
        outcome, detail = 'decoding_failure', 'response JSON/text decoding failed'
        if isinstance(error, json.JSONDecodeError):
            detail += ' line=%d column=%d position=%d' % (error.lineno, error.colno, error.pos)
        elif isinstance(error, UnicodeDecodeError):
            detail += ' byte_start=%d byte_end=%d' % (error.start, error.end)
    elif phase == 'read_body':
        outcome, detail = 'invalid_response', 'response body rejected (64 KiB limit)'
    elif phase == 'select_report':
        outcome, detail = 'invalid_response', 'report schema/selection failed'
    else:
        outcome, detail = 'request_error', 'request processing failed'
    # Types and numeric OS/TLS codes remain useful without copying messages that
    # can embed URLs, proxy credentials, response text or user configuration.
    detail += '; causes=' + '/'.join(type(e).__name__ for e in chain)
    for item in chain:
        for attribute in ('errno', 'verify_code'):
            value = getattr(item, attribute, None)
            if isinstance(value, int):
                detail += '; %s=%d' % (attribute, value)
    return outcome, detail


def fetch_metar(session, icao):
    started = time.monotonic()
    status, received, error = None, 0, None
    phase, outcome, detail = 'request', 'interrupted', 'request did not complete'
    try:
        with session.get(AWC, params={'ids':icao,'format':'json'},timeout=TIMEOUT,stream=True) as response:
            status = response.status_code
            phase = 'http_status'
            if status == 204:
                outcome, detail = 'no_report', 'HTTP no content'
                return None
            response.raise_for_status()
            phase = 'read_body'
            data = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                data.extend(chunk)
                received = len(data)
                if received > 65536:
                    raise ValueError('METAR response exceeds 64 KiB')
            phase = 'decode_json'
            payload = json.loads(data)
            phase = 'select_report'
            report = select_report(payload,icao)
            if report is not None:
                outcome, detail = 'success', 'matching observation received'
            elif not payload:
                outcome, detail = 'no_report', 'empty observation list'
            else:
                outcome, detail = 'invalid_response', 'no matching valid observation'
            return report
    except BaseException as exc:
        error = exc
        exception_response = getattr(exc, 'response', None)
        if status is None and exception_response is not None:
            status = getattr(exception_response, 'status_code', None)
        outcome, detail = _request_failure(exc, phase)
        raise  # Preserve the original exception and existing fallback behavior.
    finally:
        elapsed = time.monotonic() - started
        station = icao if isinstance(icao, str) and re.fullmatch('[A-Z]{4}', icao) else '[invalid]'
        level = logging.INFO if outcome in ('success', 'no_report') else logging.WARNING
        log.log(level,
                'METAR HTTP station=%s outcome=%s status=%s elapsed_seconds=%.3f '
                'phase=%s bytes=%d exception=%s details=%s',
                station, outcome, status if isinstance(status, int) else '-', elapsed,
                phase, received, type(error).__name__ if error else '-', detail)


def read_cache(path, icao):
    try:
        if path.stat().st_size > 65536:return None
        value = json.loads(path.read_text())
        return value if isinstance(value,dict) and value.get('icao')==icao else None
    except (OSError, ValueError, TypeError):
        return None


def write_cache(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.metar-',delete=False) as out:
            temporary=out.name;json.dump(data,out);out.flush();os.fsync(out.fileno())
        os.replace(temporary,path)
    finally:
        if temporary and os.path.exists(temporary):os.unlink(temporary)


def load_metar(session, cache_root, icao, now):
    """One fetch attempt per generation; fallback never changes observation time."""
    icao = validate_icao(icao)
    path = Path(cache_root)/'reports'/(icao+'.json')
    cached = read_cache(path,icao)
    error = None
    try:
        report = fetch_metar(session,icao)
        if report is None:error='No current report returned'
        else:
            observed=observation_time(report)
            if observed.timestamp() > now.timestamp()+300:
                log.warning('METAR report station=%s outcome=invalid_response exception=ValueError '
                            'details=observation timestamp exceeds allowed future skew', icao)
                raise ValueError('Observation timestamp is in the future')
            try:write_cache(path,{'icao':icao,'fetched_at':now.timestamp(),'report':report})
            except OSError:log.warning('METAR cache write failed for %s',icao)
            return {'report':report,'cached':False,'stale':(now-observed).total_seconds()>STALE_SECONDS,'error':None}
    except (requests.RequestException,ValueError,TypeError) as exc:
        error='METAR fetch failed: '+type(exc).__name__
    report = None
    if cached:
        try:report=select_report([cached.get('report')],icao)
        except (ValueError,TypeError):pass
    if report and observation_time(report).timestamp()<=now.timestamp()+300:
        return {'report':report,'cached':True,'stale':(now-observation_time(report)).total_seconds()>STALE_SECONDS,'error':error}
    return {'report':None,'cached':False,'stale':True,'error':error}


def csv_lines(response):
    """Stream datasets; bound total transfer and individual physical lines."""
    total=0;buffer=b'';deadline=time.monotonic()+30
    for chunk in response.iter_content(chunk_size=8192):
        total+=len(chunk)
        if total>32*1024*1024 or time.monotonic()>deadline:
            raise ValueError('Airport metadata transfer budget exceeded')
        buffer+=chunk
        while b'\n' in buffer:
            line,buffer=buffer.split(b'\n',1)
            if len(line)>131072:raise ValueError('Airport CSV line too long')
            yield line.decode('utf-8-sig')+'\n'
        if len(buffer)>131072:raise ValueError('Airport CSV line too long')
    if buffer:yield buffer.decode('utf-8-sig')


def airport_metadata(airport, runways, icao):
    """Keep only known runway identifiers/lengths; do not infer reciprocal ends."""
    usable=[]
    for row in runways:
        if row.get('airport_ident') != icao or row.get('closed') != '0':continue
        feet=number(row.get('length_ft'))
        ends=[row.get('le_ident','').strip(),row.get('he_ident','').strip()]
        usable.append({'ident':' / '.join(x for x in ends if x) or 'ID unavailable',
                       'length_m':round(feet*0.3048) if feet and feet>0 else None,
                       'surface':row.get('surface') or None})
    return {'name':airport.get('name') or None,'elevation_ft':number(airport.get('elevation_ft')),
            'runways':usable}


def fetch_airport(session, icao):
    found=None;runways=[]
    for filename in ('airports.csv','runways.csv'):
        with session.get(AIRPORTS+filename,timeout=TIMEOUT,stream=True) as response:
            response.raise_for_status()
            for row in csv.DictReader(csv_lines(response)):
                if filename=='airports.csv' and row.get('ident')==icao:found=row
                if filename=='runways.csv' and row.get('airport_ident')==icao:runways.append(row)
        if filename=='airports.csv' and found is None:return None
    return airport_metadata(found,runways,icao)


def load_airport(session, cache_root, icao, now):
    path=Path(cache_root)/'airports'/(icao+'.json');cached=read_cache(path,icao)
    if cached is None:
        seed=Path(__file__).with_name('airport_seed.json')
        try:
            seeds=json.loads(seed.read_text());cached=seeds.get(icao)
        except (OSError,ValueError):pass
    if cached:
        airport=cached.get('airport')
        valid=isinstance(airport,dict) and isinstance(airport.get('runways'),list)
        if valid:
            valid=all(isinstance(row,dict) and isinstance(row.get('ident'),str)
                      and (row.get('length_m') is None or number(row['length_m']) is not None)
                      for row in airport['runways'])
            valid=valid and (airport.get('elevation_ft') is None or number(airport['elevation_ft']) is not None)
        if not valid or number(cached.get('fetched_at')) is None or not isinstance(cached.get('as_of'),str):
            cached=None
    age = now.timestamp()-number(cached['fetched_at']) if cached else float('inf')
    if cached and 0<=age<METADATA_TTL:
        return dict(cached['airport'],status='cached metadata',as_of=cached.get('as_of','unknown'))
    try:
        metadata=fetch_airport(session,icao)
        if metadata:
            value={'icao':icao,'airport':metadata,'fetched_at':now.timestamp(),'as_of':now.strftime('%Y-%m-%d'),'source':AIRPORTS}
            try:write_cache(path,value)
            except OSError:log.warning('Airport cache write failed for %s',icao)
            return dict(metadata,status='metadata updated',as_of=value['as_of'])
    except (requests.RequestException,ValueError,csv.Error,UnicodeError):
        pass
    if cached:return dict(cached['airport'],status='STALE metadata',as_of=cached.get('as_of','unknown'))
    return {'name':None,'elevation_ft':None,'runways':[],'status':'metadata unavailable','as_of':'unknown'}
