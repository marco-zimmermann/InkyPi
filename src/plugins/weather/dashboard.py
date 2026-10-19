"""Snapshot the existing Weather parser output without recalculating weather.

Missing fields remain missing; presentation may say N/A but never invent zeroes.
The caller owns acquisition and supplies an explicit timezone-aware render time.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class Metric:
    label: str
    measurement: Any
    unit: str
    icon: Optional[str]
    arrow: str = ''


@dataclass(frozen=True)
class Forecast:
    day: str
    high: Any
    low: Any
    icon: Optional[str]
    moon_phase_pct: Any
    moon_phase_icon: Optional[str]


@dataclass(frozen=True)
class Hour:
    time: str
    temperature: Any
    precipitation: Any
    rain: Any
    icon: Optional[str]


@dataclass(frozen=True)
class Dashboard:
    title: str
    current_date: str
    current_day_icon: Optional[str]
    current_temperature: Any
    feels_like: Any
    temperature_unit: str
    units: str
    time_format: str
    rendered_at: datetime
    last_refresh_time: str
    data_points: Tuple[Metric, ...]
    forecast: Tuple[Forecast, ...]
    hourly_forecast: Tuple[Hour, ...]


def from_parsed(parsed, *, rendered_at):
    """Adapt Weather.parse_* output; values, rounding and order are untouched."""
    if rendered_at.tzinfo is None or rendered_at.utcoffset() is None:
        raise ValueError('rendered_at must be timezone-aware')
    time_format = parsed.get('time_format', '24h')
    refresh = rendered_at.strftime('%Y-%m-%d %H:%M' if time_format == '24h'
                                   else '%Y-%m-%d %I:%M %p')
    return Dashboard(
        title=str(parsed.get('title') or ''),
        current_date=str(parsed.get('current_date') or ''),
        current_day_icon=parsed.get('current_day_icon'),
        current_temperature=parsed.get('current_temperature'),
        feels_like=parsed.get('feels_like'),
        temperature_unit=parsed.get('temperature_unit', ''),
        units=parsed.get('units', 'metric'), time_format=time_format,
        rendered_at=rendered_at, last_refresh_time=refresh,
        data_points=tuple(Metric(str(p.get('label') or ''), p.get('measurement'),
                                 p.get('unit') or '', p.get('icon'), p.get('arrow') or '')
                          for p in parsed.get('data_points', []) or []),
        forecast=tuple(Forecast(str(p.get('day') or ''), p.get('high'), p.get('low'),
                                p.get('icon'), p.get('moon_phase_pct'), p.get('moon_phase_icon'))
                       for p in parsed.get('forecast', []) or []),
        hourly_forecast=tuple(Hour(str(p.get('time') or ''), p.get('temperature'),
                                   p.get('precipitation'), p.get('rain'), p.get('icon'))
                             for p in (parsed.get('hourly_forecast', []) or [])[:24]),
    )
