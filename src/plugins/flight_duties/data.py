"""Secret-backed HTTPS subscription and private atomic snapshot cache.

No URL, exception detail, UID, event text or response body is ever logged.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from .calendar_data import InvalidCalendar, MAX_BYTES, schedule_for

logger = logging.getLogger(__name__)
SECRET = 'FLIGHTLOGGER_ICAL_URL'
MAX_CACHE_AGE = timedelta(hours=24)


class PrivateSourceError(ValueError):
    pass


def validate_url(url):
    try:
        value = url.strip()
        p = urlsplit(value)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError()
        if len(value) > 8192 or any(c.isspace() for c in value): raise ValueError()
        return value
    except Exception:
        raise PrivateSourceError('Invalid HTTPS calendar secret') from None


def read_secret(device_config):
    """Environment/.env value first; optional private file path via environment."""
    try:
        value = device_config.load_env_key(SECRET)
        if value: return validate_url(value)
        filename = device_config.load_env_key(SECRET+'_FILE')
        if filename:
            path = Path(filename).expanduser()
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_size > 8192:
                raise PrivateSourceError('Calendar secret file must be private')
            return validate_url(path.read_text(encoding='utf-8'))
    except PrivateSourceError:
        raise
    except Exception:
        raise PrivateSourceError('Calendar secret unavailable') from None
    raise PrivateSourceError('Calendar secret not configured')


@contextmanager
def private_http_logs():
    # urllib3 DEBUG can otherwise expose the subscription query string. Suppress
    # this thread's transport diagnostics, without changing global log levels.
    owner = threading.get_ident()
    class Redact(logging.Filter):
        def filter(self, record):
            if record.thread == owner:
                record.msg = 'Private calendar transport diagnostic suppressed'
                record.args = ()
                record.exc_info = None
                record.exc_text = None
            return True
    guard = Redact()
    logs = [logging.getLogger(n) for n in ('urllib3.connectionpool', 'urllib3.util.retry',
            'requests.packages.urllib3.connectionpool')]
    for log in logs: log.addFilter(guard)
    try: yield
    finally:
        for log in logs: log.removeFilter(guard)


def make_session():
    session = requests.Session()
    session.trust_env = False  # No implicit .netrc credentials or proxy overrides.
    session.mount('https://', HTTPAdapter(max_retries=0))
    session.headers.update({'User-Agent':'InkyPi-FlightDuties/1.0','Accept':'text/calendar'})
    return session


def fetch(session, url):
    with private_http_logs():
        with session.get(url, timeout=(3,8), stream=True, allow_redirects=False) as response:
            # Redirects are not followed: never forward a private subscription.
            if response.status_code != 200:
                raise PrivateSourceError('HTTP '+str(int(response.status_code)))
            chunks, size, started = [], 0, time.monotonic()
            for chunk in response.iter_content(32768):
                size += len(chunk)
                if size > MAX_BYTES or time.monotonic()-started > 20:
                    raise PrivateSourceError('Calendar transfer limit')
                chunks.append(chunk)
            return b''.join(chunks)


def cache_path(root, url):
    # Different subscriptions never share a cache, even if the same saved instance
    # is reconfigured. The URL/token itself is not stored in cache metadata.
    return Path(root)/('snapshot-'+hashlib.sha256(url.encode()).hexdigest()+'.json')


def write_cache(path, data, now):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink(): raise OSError('Unsafe cache path')
    path.parent.chmod(0o700)
    payload = json.dumps({'version':1,'fetched_at':now.isoformat(),
                          'ics':data.decode('utf-8-sig')}, ensure_ascii=False).encode('utf-8')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.snapshot-', delete=False) as f:
            temporary = Path(f.name)
            os.fchmod(f.fileno(),0o600)
            f.write(payload);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
        fd = os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary is not None and temporary.exists():temporary.unlink()


def read_cache(path, now):
    try:
        path = Path(path);st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_size > MAX_BYTES*6:
            return None
        item=json.loads(path.read_text(encoding='utf-8'))
        fetched=datetime.fromisoformat(item['fetched_at'])
        if fetched.tzinfo is None or not timedelta(0)<=now-fetched<=MAX_CACHE_AGE or item['version']!=1:
            return None
        data=item['ics'].encode('utf-8')
        return data,fetched
    except (OSError,ValueError,TypeError,KeyError):
        return None


def load_schedule(session, url, root, now, *, clock=None):
    # Resolve the displayed local day after network IO, including midnight/DST.
    clock = clock or (lambda: now)
    url=validate_url(url)
    path=cache_path(root,url)
    failure=None
    try:
        data=fetch(session,url)
        evaluated_at=clock()
        schedule=schedule_for(data,evaluated_at)  # Invalid responses never replace the cache.
    except Exception as error:
        # Never log str(error), traceback, URL or response contents.
        failure=('Invalid calendar' if isinstance(error,InvalidCalendar) else
                 str(error) if isinstance(error,PrivateSourceError) and str(error).startswith('HTTP ') else
                 'Calendar request failed')
        logger.warning('Flight Duties fetch failed; attempting private cached snapshot')
    else:
        warning=None
        try:write_cache(path,data,evaluated_at)
        except (OSError,UnicodeError,ValueError):
            warning='Cache not saved'
            logger.warning('Flight Duties private cache write failed')
        return dict(schedule=schedule,source='live',fetched_at=evaluated_at,error=warning,
                    evaluated_at=evaluated_at)
    evaluated_at=clock()
    cached=read_cache(path,evaluated_at)
    if cached:
        data,fetched=cached
        try:
            return dict(schedule=schedule_for(data,evaluated_at),source='cached',
                        fetched_at=fetched,error=failure,evaluated_at=evaluated_at)
        except InvalidCalendar:pass
    return dict(schedule=None,source='unavailable',fetched_at=None,error=failure,
                evaluated_at=evaluated_at)
