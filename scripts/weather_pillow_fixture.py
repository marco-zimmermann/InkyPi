"""Offline adapter for the recorded synthetic A/B fixture, not live weather.

Calls only the existing provider parser, never Weather.generate_image(), provider
HTTP methods, configuration loaders, browser rendering or display code.
"""
import copy
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytz

from plugins.weather.dashboard import from_parsed

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / 'tests/fixtures/weather/synthetic_dashboard.json'


def load_fixture(path=FIXTURE):
    from plugins.weather.weather import Weather
    source = json.loads(Path(path).read_text())
    if not source['settings']['customTitle'].startswith('TEST:'):
        raise ValueError('Only clearly labelled synthetic fixture input is supported')
    now = datetime.fromisoformat(source['fixed_now'])
    if now.utcoffset() is None:
        raise ValueError('Fixture time must be timezone-aware')
    tz = pytz.timezone(source['timezone'])
    now = now.astimezone(tz)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    plugin = Weather({'id': 'weather'})
    settings = copy.deepcopy(source['settings'])
    # Preserve the exact parser, including its existing rounding, timezone,
    # icon selection, AQI descriptions and Astral moon phase calculation.
    with patch('plugins.weather.weather.datetime', FrozenDatetime):
        parsed = plugin.parse_open_meteo_data(copy.deepcopy(source['weather']),
                    copy.deepcopy(source['aqi']), tz, settings['units'],
                    source['time_format'], float(settings['latitude']))
    parsed['title'] = settings['customTitle']
    return from_parsed(parsed, rendered_at=now), settings, parsed
