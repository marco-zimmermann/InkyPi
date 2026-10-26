"""Private iCalendar data model. No HTTP, application config, logs or hardware."""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
import re
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

ZURICH = ZoneInfo('Europe/Zurich')
MAX_BYTES = 1024 * 1024
MAX_EVENTS = 2000
MAX_OCCURRENCES = 512


class InvalidCalendar(ValueError):
    """Deliberately excludes parser messages that may contain private content."""


@dataclass(frozen=True)
class Duty:
    uid: str = field(repr=False)
    start: datetime
    end: datetime
    activity: str = field(repr=False)
    aircraft: str = field(repr=False)
    participants: str = field(repr=False)
    status: str = 'UNKNOWN'
    all_day: bool = False


@dataclass(frozen=True)
class Schedule:
    day: date
    duties: tuple = field(repr=False)
    reminders: tuple = field(repr=False)
    cancelled: int = 0


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() in ('br', 'p', 'div', 'li'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag.lower() in ('p', 'div', 'li'): self.parts.append('\n')
    def handle_data(self, data):
        self.parts.append(data)


def clean(value, limit=1200):
    # Links are never shown: descriptions can contain private booking URLs.
    parser = PlainText()
    parser.feed(str(value or '')[:16000])
    text = unescape(''.join(parser.parts))
    text = re.sub(r'https?://\S+', '[private link]', text, flags=re.I)
    text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)
    return '\n'.join(' '.join(line.split()) for line in text.splitlines())[:limit].strip()


def details(event):
    summary = clean(event.get('SUMMARY')) or 'Untitled duty'
    description = clean(event.get('DESCRIPTION'))
    people = []
    for line in (summary+'\n'+description).splitlines():
        match = re.match(r'(?i)^(students?|instructors?|customer|pilot|participants?)\s*:\s*(.+)$', line)
        if match:
            people.append(match.group(1).capitalize()+': '+match.group(2))
    attendees = event.get('ATTENDEE', [])
    if not isinstance(attendees, list): attendees = [attendees]
    for attendee in attendees:
        name = clean(attendee.params.get('CN', ''))
        if name: people.append(name)  # Never display mailto addresses.
    location = clean(event.get('LOCATION'), 140)
    # FlightLogger places registrations in LOCATION; keep other resources usable.
    explicit = re.search(r'(?im)^(?:aircraft|registration)\s*:\s*([^\n]+)', description)
    aircraft = location or (explicit.group(1).strip() if explicit else 'Not specified')
    return summary.replace('\n', ' '), aircraft, ' · '.join(dict.fromkeys(people))[:600]


def _aware(prop):
    value = prop.dt
    if not isinstance(value, (date, datetime)): raise InvalidCalendar('Invalid date')
    if isinstance(value, datetime) and value.tzinfo is None:
        if prop.params.get('TZID'): raise InvalidCalendar('Unknown timezone')
        value = value.replace(tzinfo=ZURICH)  # Floating school time is Zurich time.
    return value


def parse_calendar(data):
    """Validate the snapshot before caching; normalize broken date-only reminders."""
    try:
        if not isinstance(data, bytes) or len(data) > MAX_BYTES:
            raise InvalidCalendar('Calendar size limit')
        if not data.lstrip(b'\xef\xbb\xbf\r\n ').startswith(b'BEGIN:VCALENDAR'):
            raise InvalidCalendar('Not a calendar')
        calendar = icalendar.Calendar.from_ical(data)
        if calendar.name != 'VCALENDAR' or str(calendar.get('VERSION', '')) != '2.0' or calendar.errors:
            raise InvalidCalendar('Invalid calendar')
        # A METHOD:CANCEL message is a delta, not a complete subscription snapshot.
        if str(calendar.get('METHOD', '')).upper() not in ('', 'PUBLISH'):
            raise InvalidCalendar('Partial calendar message unsupported')
        events = calendar.walk('VEVENT')
        if len(events) > MAX_EVENTS: raise InvalidCalendar('Event limit')
        for event in events:
            if event.errors or not event.get('UID') or 'DTSTART' not in event:
                raise InvalidCalendar('Incomplete event')
            start = _aware(event['DTSTART'])
            if isinstance(start, datetime): event['DTSTART'].dt = start
            if 'RECURRENCE-ID' in event:
                event['RECURRENCE-ID'].dt = _aware(event['RECURRENCE-ID'])
            if 'DTEND' not in event:
                if 'DURATION' in event:
                    end = start + event.decoded('DURATION')
                elif not isinstance(start, datetime): end = start + timedelta(days=1)
                else: raise InvalidCalendar('Timed event has no end')
                event.add('DTEND', end)
            end = _aware(event['DTEND'])
            if isinstance(start, datetime) != isinstance(end, datetime):
                raise InvalidCalendar('Mismatched date types')
            if not isinstance(start, datetime) and end == start: end += timedelta(days=1)
            if end <= start: raise InvalidCalendar('Nonpositive duration')
            event['DTEND'].dt = end
            rules = event.get('RRULE', [])
            if not isinstance(rules, list): rules = [rules]
            for rule in rules:
                if str(rule.get('FREQ', [''])[0]) not in ('DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'):
                    raise InvalidCalendar('Unsupported dense recurrence')
                if any(len(rule.get(k, [])) > bound for k, bound in [('BYHOUR',4),('BYMINUTE',1),('BYSECOND',1)]):
                    raise InvalidCalendar('Dense recurrence limit')
        return calendar
    except InvalidCalendar:
        raise
    except Exception:
        raise InvalidCalendar('Calendar parsing failed') from None


def display_day(now):
    """School planning day: next local calendar day from 18:00 Zurich time."""
    if now.tzinfo is None: raise ValueError('Aware time required')
    local = now.astimezone(ZURICH)
    return local.date() + timedelta(days=local.hour >= 18)


def schedule_for(data, now):
    """Bounded expansion of the selected planning day, including overlaps."""
    if now.tzinfo is None: raise ValueError('Aware time required')
    today = display_day(now)
    lower = datetime.combine(today, time.min, ZURICH)
    upper = datetime.combine(today+timedelta(days=1), time.min, ZURICH)
    calendar = parse_calendar(data)
    duties, reminders, seen, cancelled = [], [], set(), 0
    try:
        # after() is lazy and includes events overlapping its earliest-end bound.
        for count, event in enumerate(recurring_ical_events.of(calendar).after(lower)):
            if count >= MAX_OCCURRENCES: raise InvalidCalendar('Occurrence limit')
            start, end = event['DTSTART'].dt, event['DTEND'].dt
            all_day = not isinstance(start, datetime)
            if all_day:
                start = datetime.combine(start, time.min, ZURICH)
                end = datetime.combine(end, time.min, ZURICH)
            elif start.tzinfo is None or end.tzinfo is None:
                start, end = start.replace(tzinfo=ZURICH), end.replace(tzinfo=ZURICH)
            start, end = start.astimezone(ZURICH), end.astimezone(ZURICH)
            if start >= upper: break
            if end <= lower: continue
            key = (str(event['UID']), start)
            if key in seen: continue
            seen.add(key)
            status = str(event.get('STATUS', '')).upper()
            if status == 'CANCELLED':
                cancelled += 1
                continue
            if status not in ('CONFIRMED', 'TENTATIVE'): status = 'UNKNOWN'
            activity, aircraft, participants = details(event)
            item = Duty(key[0], start, end, activity, aircraft, participants, status, all_day)
            (reminders if all_day else duties).append(item)
    except InvalidCalendar:
        raise
    except Exception:
        raise InvalidCalendar('Recurrence expansion failed') from None
    return Schedule(today, tuple(sorted(duties, key=lambda d:(d.start,d.end))),
                    tuple(sorted(reminders, key=lambda d:d.start)), cancelled)
