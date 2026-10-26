# Daily Flight Duties

Displays a read-only FlightLogger iCalendar subscription, not the FlightLogger API.

Plugin ID: `flight_duties`; class: `FlightDuties`; display: 800 × 480 landscape,
RGB Pillow. No browser, display-driver or scheduling code is imported by its data
model/renderer. Existing plugin discovery, settings, static icon and saved-instance
mechanisms work without a registry/configuration migration. Save an instance in the
normal web interface when deploying; no instance is created by installing the code.

## Private subscription secret

No real feed is required for tests or previews. The plugin never stores the URL
in saved instance settings. At runtime, `Config.load_env_key` reads either:

- `FLIGHTLOGGER_ICAL_URL`: the private HTTPS subscription URL; or
- `FLIGHTLOGGER_ICAL_URL_FILE`: path to a file containing just that URL. The file
  must be a regular, non-symlink file with no group/other permissions (mode 0600).

Use the existing environment/.env mechanism. Never commit the URL or token, enter
it in the dashboard title, paste it in a command argument, or include it in logs.
For local use, create a file outside Git with hidden terminal input:

```sh
umask 077
python3 - <<'PY'
import getpass
from pathlib import Path
path = Path.home() / '.config/inkypi/flightlogger-calendar-url'
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
path.write_text(getpass.getpass('Private HTTPS calendar URL (hidden): ').strip()+'\n')
path.chmod(0o600)
PY
export FLIGHTLOGGER_ICAL_URL_FILE="$HOME/.config/inkypi/flightlogger-calendar-url"
```

Configure a deployed service's environment separately, with explicit operational
approval. Missing/invalid secrets produce an UNAVAILABLE page without fetching or
reusing an unidentified account's cache. Participant details can appear on the
physical dashboard; keep the screen in an appropriate private location.

## Button D navigation

Create a saved `flight_duties` instance named `Flight Duties` in playlist `Default`.
Merge this reference into `button_navigation.assignments`, preserving A/B and the
contextual C action:

```json
"D": {"playlist": "Default", "plugin_id": "flight_duties", "instance": "Flight Duties"}
```

Button D (GPIO24) selects that instance through the existing serialized worker;
selection commits only after successful presentation. Weather/METAR memories are
independent and unchanged. C has no location cycle while Flight Duties is selected.
No real credential belongs in the saved instance or button configuration.

## Display and calendar semantics

- The planning day uses Europe/Zurich, including DST: before 18:00 show today;
  from 18:00 show tomorrow. TODAY/TOMORROW and the selected date are prominent.
  UTC and supplied timezone-aware times
  are converted; floating times are explicitly interpreted as Zurich school time.
  Unknown timezone identifiers fail conservatively.
- A featured next/upcoming duty uses a 42 px start time and prominent aircraft,
  followed by a cleaner list: four to six bookings total (default five). Legacy
  seven-booking settings remain valid and render six. NEXT/IN PROGRESS labels and
  a bordered card communicate focus without relying on color. Overflow counts
  reveal hidden bookings; missing participant fields do not add filler text.
  Overnight bookings overlapping the selected day use `<` (previous day) and `+`
  (following day) markers relative to that displayed date.
- The compact date reads e.g. TUE 27 OCT 2026. Blue accents identify the selected
  planning day, featured card and reminders. Tomorrow's feature says FIRST DUTY
  TOMORROW; today's upcoming feature says NEXT DUTY. Counts read “5 of 7 bookings
  shown”, and the footer gives last-sync/stale information without status clutter.
- Text is primarily black on white. RGB output is snapped without dithering to the
  installed E673 driver's nominal six colors: black, white, yellow, red, blue and
  green. Blue is restrained; green only marks explicit confirmation; red is reserved
  for stale/unavailable information or actual errors. No driver/global enhancement
  settings change. Tests cover the current neutral enhancements and driver palette
  blend at inky_saturation=0.5; custom enhancement settings can alter conversion.
- LOCATION supplies the aircraft/resource; an explicit Aircraft/Registration label
  in DESCRIPTION is a fallback. The original summary remains intact in the parsed
  data and private cache.
  Featured and list activity titles hide only leading syllabus/year tokens such as
  `2025/10` and lesson codes such as `E.1.11`, allowing spaces, hyphens, en dashes
  or colons between them. Year/month tokens may have a trailing letter.
  A leading year and FlightLogger marker before the lesson code are also
  hidden. Intervening booking details (which can contain a student name) move to
  the participant line and are retained alongside structured participants; their
  role is not guessed. Similar text later in a title is preserved. Explicit
  Student/Instructor/Customer/Pilot/Participant text
  and ATTENDEE display names are included where available. Unstructured names can
  remain embedded in the activity; the feed is not a structured personnel API.
- Only explicit STATUS:CONFIRMED is labelled Confirmed with a small green marker.
  Explicit TENTATIVE is labelled; absent/unknown status is left unlabelled, never
  inferred as confirmed (the data model still retains UNKNOWN). CANCELLED
  occurrences are excluded and
  counted, including overridden recurring instances.
- All-day reminders are separate. Date-only DTSTART=DTEND is normalized to one day,
  as required by the inspected export's expiry reminders; timed nonpositive or
  missing ends are rejected rather than inventing a flight duration.
- A valid empty planning day shows “No duties today” or “No duties tomorrow”.
  Cached empty days retain clear CACHED/STALE indicators; unavailable data is never
  silently treated as an empty schedule. Reminders occupy a separate bottom band.

Parsing uses existing project dependencies `icalendar==6.3.2` and
`recurring-ical-events==3.8.0` (no requirements changes). Folded lines, escaped text,
VTIMEZONE, RRULE, RDATE, EXDATE and RECURRENCE-ID overrides are handled by these
libraries. Expansion is lazy, restricted to the selected planning day and capped at 512
occurrences; input is limited to 1 MiB / 2,000 VEVENTs. Normal daily/weekly/monthly/
yearly rules are supported. Dense sub-daily or multi-minute/second rule combinations
are rejected, not partially displayed. Invalid components/series fail the snapshot.
Cancellations must be present in the subscription snapshot with usable timestamps;
METHOD:CANCEL/REQUEST messages are deltas, not supported full-feed replacements.
A disappearing UID is removed on a subsequent valid snapshot, not labelled cancelled
without evidence. No calendar content is executed and embedded URLs are not followed
or rendered.

## Fetch and cache

A generation makes one streamed, TLS-verified HTTPS GET, connect/read timeouts 3/8 s,
no automatic retries, redirects, implicit proxy environment or .netrc credentials.
A 20-second transfer budget is checked between chunks, with read-timeout granularity;
this is not a hard DNS/whole-operation deadline. urllib3 transport logs for the
request thread are suppressed to prevent DEBUG query-string leaks. Plugin logs
contain no URL, UID, event text, response body or exception details.

Cache: `<device-config-directory>/.flight-duties-cache/snapshot-<source-hash>.json`.
It is outside the public static/image tree, and the local source-tree cache path is
ignored by Git.
The directory is 0700; files and atomic temporary files are 0600. The last fully
parsed snapshot and real retrieval timestamp are flushed/replaced atomically. Source
hashing separates subscriptions; raw calendar text is private and includes personal
information, so do not export this cache to reports or Git. A cache write failure
still permits displaying valid live data with a visible warning.

On request/parse/HTTP failure, a valid cache up to 24 hours old is re-expanded for
**the selected planning day**, labelled STALE/CACHED with its real last-sync
timestamp and failure state.
Older/future-dated/corrupt/public-permission caches are rejected. Without a valid
cache, display UNAVAILABLE. A successful empty calendar replaces an old snapshot;
network failure never does. There is no polling thread, retry loop, ETag assumption
or background network activity outside generation.

## Selected-screen automatic refresh

Flight Duties uses the same serialized worker, attempt journal and resource guard
as Weather/METAR. Add an explicit opt-in group without changing Button D:

```json
"flight_duties": {
  "instances": [{"playlist": "Default", "plugin_id": "flight_duties", "instance": "Flight Duties"}],
  "interval_seconds": 900,
  "auto_refresh_enabled": true
}
```

This is an entry in `button_navigation.groups`. Global `manual_only=true`, resource
pause, displaced selection and uncertain display state all suppress automatic work.
Only the committed selected instance refreshes; selecting Weather or METAR stops
calendar scheduling and resumes that group's existing cadence. Contextual Button C
continues to cycle only Weather/METAR, and their remembered locations are unchanged.
There is no calendar polling thread, concurrent renderer or background inactive fetch.

The next attempt is due 900 seconds after completion, including failures and exact
image matches, with up to 60 seconds worker polling delay. A missing journal waits a
full interval after worker initialization; valid UTC completion timestamps survive
restarts, subject to the existing 60-second startup grace. Interrupted attempts
persist a resource pause. Cached/UNAVAILABLE output can be successfully presented
but cannot advance the live-data-success timestamp. Failed generation retains the
last successful display and never immediately retries. Existing resource-pause
recovery remains deliberate; calendar requests cannot implicitly clear a pause.

After network IO the agenda and header share an explicitly sampled Europe/Zurich
planning date. Before 18:00 the date is today; from 18:00 it is tomorrow. The
header and empty state explicitly identify that day. Fresh and cached calendars
are expanded for that date, including DST, month/year transitions and overnight
overlaps. A mismatched date supplied to the renderer fails closed instead of
relabeling bookings. Neither 18:00 nor midnight starts an extra request: the panel
changes on the next eligible 15-minute refresh (plus worker/render/display time).
At midnight the planning date remains the same, while TOMORROW becomes TODAY.
A cached snapshot remains visibly stale and retains
its actual retrieval timestamp. Exact-image hashes suppress identical display IO;
changing date, next-duty emphasis or last-sync text may legitimately change pixels.
Normal disabled-button playlists retain their existing behavior.

## Offline validation and deployment readiness

```sh
PYTHONPATH=src:. python -m pytest -q tests/test_flight_duties*.py
PYTHONPATH=src python scripts/preview_flight_duties.py
```

The preview script only uses explicitly synthetic bookings and fixed render time;
it never reads a secret or fetches a feed. Outputs are live/cached/unavailable PNGs
under ignored `mock_display_output/flight-duties/`. Never present them as current
bookings. Parser/cache/layout and plugin-discovery tests mock all network and browser
calls. Tests include DST, midnight overlap, recurrence exceptions/cancellations,
status uncertainty, secret-file permissions, cache-write failures, privacy logging,
overflow, deterministic RGB output and monochrome conversion.

## Verified PiFrame deployment

On 2026-10-26 the isolated release `flight-duties-design-02b0bbd15633` passed
Python 3.13.5 validation and physical Button D / automatic-refresh testing. Button D
opens Flight Duties; all three groups are enabled at 900 seconds with
`manual_only=false` and `startup=false`. Only the selected group refreshes.
A/B/C, saved Weather/METAR instances, independent location memories and resource
protection were preserved. Current selection is mutable runtime state.

The redesigned TOMORROW screen was physically confirmed readable. Its first natural
scheduled cycle generated in 2.3 s and completed physical display in 28.7 s, with a
92.4 MiB kernel cgroup peak, zero swap and no limit/OOM events. These are individual
measurements, not an unattended reliability guarantee. MemoryMax remains 200 MiB
and CPUQuota 40%. An earlier working release and its exact drop-in are retained
for rollback; no runtime configuration or release package is shipped in Git.

Keep private source/cache data and real agenda PNGs out of Git and reports. Future
service changes still require operational approval; never clear resource protection
or display uncertainty simply because a cached page rendered successfully.

The later `flight-duties-titles-5b79fecf6c69` release includes the six-color
readability improvements and UI-only title cleanup. A physical B → D test confirmed
clean activity titles, preserved student names on the participant line and no text
overlap. The Flight Duties attempt generated in 2.6 s and completed in 31.5 s,
including 28.6 s of physical display work. Service cgroup peak memory was 96.4 MiB;
sampled swap peaked at 15.7 MiB, with no memory-limit/OOM events or unexpected
restarts. Scheduling, saved settings, private source/cache data and resource limits
were preserved. Earlier releases and their drop-ins remain available for rollback.
