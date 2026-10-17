# Aviation METAR plugin

`metar` is a normal InkyPi plugin (`Metar` class). Plugin discovery reads its
`plugin-info.json`; settings expose one ICAO field, default LSZL. The plugin returns
an 800 × 480 RGB Pillow image through `generate_image(settings, device_config)`.
It does not use Chromium, GPIO, a display controller, or a separate scheduler.
This plugin supports METAR only; TAF retrieval and presentation are not implemented.
It adds no dependencies and does not assign buttons automatically. The verified
PiFrame runtime saves an instance named `LSZL METAR` in playlist `Default`, with
settings `{"icao": "LSZL"}`. Button B references that instance through the existing
controller; Button A remains assigned to `Default/weather/Weather`, and C/D are
unassigned. These runtime assignments and configuration are not source defaults.

With button navigation enabled and `manual_only=true`, a newly selected screen gets
one refresh attempt, with no periodic update, automatic failure retry, or playlist
fallback. Repeated B is suppressed while METAR is selected and undisplaced. A
successful web update displaying another image marks the selection displaced;
pressing B then restores METAR through the same worker. Selection/displacement
persist across restart without an automatic manual-only refresh. On-screen age
and stale indicators describe generation time and do not advance until another
render. Disabled button mode retains ordinary playlist behavior. See
[button navigation](BUTTON_NAVIGATION.md) for configuration and lifecycle details.

## Sources

- [AWC Data API](https://aviationweather.gov/data/api/) and
  [current OpenAPI schema](https://aviationweather.gov/data/schema/openapi.yaml),
  inspected 2026-10-17: `/api/data/metar?ids=LSZL&format=json`, `icaoId`, `rawOb`,
  and `obsTime` (Unix observation time). The API's `fltCat` is not trusted for
  classification. Decoding uses raw observation groups, not schema default values
  such as temperature=0. Records without a matching station or usable observation
  time are rejected. Receipt/fetch time never substitutes for observation time.
- [OurAirports datasets](https://github.com/davidmegginson/ourairports-data): airport
  name/elevation and open runway end identifiers/lengths. Public domain, community
  maintained, without an accuracy guarantee. It is not an authoritative AIP or
  runway availability/NOTAM source. Closed or unknown-status runways are excluded;
  missing identifiers/lengths stay explicitly unknown. Feet are converted to metres.
  `airport_seed.json` contains only actual LSZL rows retrieved 2026-10-17 with source
  and retrieval time; no runway values were invented. No other airports are seeded.
- [NWS flight categories](https://www.weather.gov/safety/fog-flying): worst of
  visibility and lowest BKN/OVC/VV ceiling, feet AGL. These display categories do
  not establish legal flight minima for local airspace or pilot privileges.

## Fetching and caches

A plugin-owned requests Session has no automatic retries and a descriptive User-Agent.
Each generation makes one METAR request with connect/read timeouts of 3/8 seconds,
streaming a maximum of 64 KiB. An absent report (204/empty array), malformed response,
HTTP error, or timeout can fall back to the last matching-ICAO successful report.
Cached fallback is always labelled CACHED; a report over 90 minutes old is marked
STALE. The 90-minute threshold is a display heuristic. NIL reports display unavailable
observation values. There is no decoded estimate or fallback to a neighbouring airport.

Caches are JSON under `device_config.plugin_image_dir/.metar-cache`, with separate
`reports/<ICAO>.json` and `airports/<ICAO>.json` paths and atomic writes. Cache failures
must not relabel stale weather as current. No cross-process cache writer is supported.
Airport metadata has a 30-day cache lifetime, including the bundled LSZL seed.
After expiration or for another station, CSVs are streamed and only the chosen airport
is retained. Each dataset transfer is bounded at 32 MiB, each physical line at 128 KiB,
and checked against a 30-second elapsed budget between chunks, in addition to socket
read timeouts. The two datasets can still take appreciable time on first use. A stale
metadata fallback remains labelled STALE; missing information is not fabricated.
A read timeout is not a strict whole-request wall-clock deadline. Slow streaming and
metadata transfers remain workload-dependent; the bundled LSZL metadata avoids
those transfers while its cache is valid.

### METAR request diagnostics

Each METAR request emits one summary: INFO for a successful observation or an
empty/no-content result, WARNING for a transport, HTTP, decoding or validation
failure. The summary includes station, outcome, HTTP status when available,
monotonic elapsed seconds, processing phase, received body bytes and exception
type. Phases distinguish opening the request/waiting for response headers,
checking status, streaming the body, decoding JSON and selecting a valid report.
Elapsed time covers the complete fetch/parse operation, not individual DNS,
TCP or TLS timings. Airport-metadata requests are separate from these summaries.

ConnectTimeout and ReadTimeout have distinct outcomes. A streamed-body read
timeout wrapped by Requests in ConnectionError is still identified as a read
timeout, while retaining the original exception type and propagation. Diagnostic
details use fixed explanations, a bounded exception-type chain, numeric OS/TLS
codes, and JSON/text error offsets. Raw exception messages, URLs, response bodies,
headers and configuration values are excluded. A future-dated observation rejected
after the HTTP operation gets a separate report-validation warning.

This logging does not change the User-Agent, 3/8-second timeouts, Requests address
selection, zero automatic retries, report selection, or cached/stale/unavailable
display states. It uses the existing Requests/urllib3 dependencies. Diagnostics
are covered by mocked requests; no live request is needed to run the tests.

## Decoding and presentation

`decoding.py` separates station selection, visibility, weather groups and category
calculation. It handles METAR/SPECI, AUTO/COR/NIL, CAVOK, 9999/NDV, statute-mile
fractions and M/P bounds, KT/MPS wind, calm/VRB/gust/direction ranges, cloud heights
and CB/TCU suffixes, negative temperature/dew point, Q and A altimeter settings.
RMK and trend groups cannot change current conditions. Aviation groups remain
unchanged in the raw footer. Missing cloud/visibility components cannot establish
VFR; a known LIFR component can establish the worst category. Visibility bounds
that span categories return UNKNOWN. FEW/SCT do not form a ceiling; unknown BKN/OVC/VV
heights remain uncertain. This is deliberately not a complete ICAO METAR validator.

Category thresholds: LIFR below 500 ft or below 1 SM; IFR 500–below 1,000 ft or
1–below 3 SM; MVFR 1,000–3,000 ft or 3–5 SM; otherwise VFR when both components
are known. CAVOK provides lower visibility/ceiling constraints, not unlimited vision.

`rendering.py` draws original geometric weather symbols; no third-party icon assets
are copied. Bundled Jost fonts, black/white plus red/blue/green/yellow, and explicit
text labels retain meaning in monochrome. Header, wind/visibility, temperature/dew
point/QNH, cloud/ceiling, runway strip, and raw report footer have fixed bounds.
Long labels use an explicit ellipsis. Reports exceeding the footer capacity show
RAW TOO LONG rather than silently presenting an apparently complete raw report.
No automatic pagination is implemented. More complex weather may share a generic
symbol; the present-weather text and raw report preserve the distinction.

## Local examples and tests

`tests/fixtures/metar/examples.json` contains **synthetic**, explicitly labelled
LSZL VFR and IFR reports at 2026-10-17 12:00 UTC. They are not live or historical
observations. Preview rendering performs no network calls or app initialization:

```sh
PYTHONPATH=src python scripts/preview_metar.py --output-dir /path/to/previews
PYTHONPATH=src:. python -m pytest -q
git diff --check
```

Tests use mocked requests and temporary caches. They cover boundaries, station
isolation, malformed/missing reports, weather symbols, runway conversion, cache
fallback, pixel dimensions/palette, bounded text, and excluded hardware/browser imports.
Generated preview PNGs belong outside the source tree. Tests do not fetch live
weather, initialize the application, acquire GPIO, or refresh a display.

## Refined local layout

The revised 800 × 480 layout retains the same synthetic VFR/IFR inputs and data
pipeline. The larger original weather symbol is balanced against a separate wind
column and a two-by-two visibility/ceiling/temperature/QNH grid. Wind direction,
speed, gusts, and variable-direction limits occupy separate lines. Ceiling has a
large numeric field with explicit ft AGL units; a known absence of a reported
ceiling reads None / reported, not unlimited. Observation UTC and category remain
prominent. All essential meanings use text and shapes as well as color.

A three-row runway/length table replaces the previous single line. More than three
runways show two rows and an explicit remaining count. The unchanged LSZL seed
contains 08C/26C (700 m), 08L/26R (800 m), 08R/26L (700 m), and elevation 650 ft,
from the documented OurAirports snapshot; metre lengths are rounded conversions
from source feet. The adjacent community-reference/as-of label explicitly states
that it is not an official AIP.

Trade-offs: raw METAR now has two footer lines at 16 px, with an explicit overflow
warning if necessary. Long airport names, descriptions, or cloud strings use
ellipsis rather than shrinking below their readability floor. Dew point and units
are smaller than the main numeric values; runway rows use 19 px text. No new
paging, data conversion, weather decoding, or navigation behavior was added.

## Verified baseline (2026-10-17)

The complete local suite passes **240 tests**, including the 137-test navigation
and Chromium foundation. METAR tests cover diagnostics, cache provenance, decoding,
layout, plugin discovery, and the icon route. Local validation uses Python 3.9 and
Pillow 11.3; its urllib3/LibreSSL warning comes from the Mac interpreter. Isolated
PiFrame validation passes **103 METAR tests** on Python 3.13.5 using installed
dependencies and mocks, including the 512 × 512 RGBA icon and route checks.
Synthetic VFR/IFR previews passed 800 × 480 rendering on the Pi's Pillow installation.

The source baseline is snapshot
`61479ddb212fd712d34d781f9818b2c52ffcc27df241bf6f77337e42a6cc28b3`.
Application code, tests, fixtures, icon, and preview script match the release
validated on PiFrame; documentation was subsequently updated to record these results.
The web interface resolves the icon at `/images/metar/icon.png` through its existing
plugin image route. The original windsock icon uses transparency and contains no
third-party artwork.

Live LSZL fetching, decoding, and physical presentation were verified on the Pi
Zero 2 W with the Inky Impression Spectra 7.3-inch E673 variant 22, Inky 2.4.0,
Python 3.13.5, and the existing virtual environment. A live test returned HTTP 200
in 1.141 seconds, generated in 3.219 seconds, and refreshed the panel in 28.724
seconds, with a kernel service-cgroup peak of about **92 MiB**. The report was:

```text
METAR LSZL 171950Z AUTO 00000KT 9000NDV SCT061 16/14 Q1016 RMK
```

Its observation time was 2026-10-17 19:50 UTC: calm wind, 9 km visibility,
SCT at 6,100 ft AGL without a reported ceiling, temperature/dew point 16/14°C,
QNH 1016 hPa, VFR. Later successful fetching returned the same observation;
only fetch time advanced. A successful HTTP response does not establish that
an airport has issued a newer report. Cache provenance preserves observation time.

The physical **A → B → A** regression produced exactly three worker generations
and three display calls, with user-confirmed readable Weather/METAR/Weather output.
The B step took about 33.2 seconds, including a 2.710-second HTTP request and
28.147-second physical refresh. No automatic retries, extra updates, OOM events,
or service restarts occurred over two subsequent scheduler intervals. MemoryMax
remained 200 MiB and CPUQuota 40%; the sequence's cumulative cgroup peak was about
171 MiB, including Chromium-based Weather rendering, rather than METAR alone.

A persistent systemd drop-in overriding only `ExecStart` and `WorkingDirectory`
survived a controlled reboot. The same release, assignments, Weather selection,
manual-only mode, startup=false, and limits were restored; no fetch or display
refresh occurred across two scheduler intervals. The user confirmed the physical
Weather image remained unchanged. The isolated runtime and caches remain outside
Git, and the original production checkout is preserved for rollback.

## Remaining limitations

- No TAF, automatic pagination, or complete ICAO grammar validation. Unknown or
  ambiguous groups remain unknown rather than inferred.
- OurAirports runway/elevation data are community reference information, not an
  official AIP, NOTAM feed, or assurance of runway availability. The LSZL seed is
  a dated source snapshot, not a runtime weather cache.
- Cache age thresholds are display heuristics; manual-only screens can grow old
  without another render. Neither a successful fetch nor the category badge
  establishes operational suitability or current local flight minima.
- The first physical request encountered ReadTimeout; later IPv4, IPv6 and Python
  requests succeeded. Its cause remains unconfirmed. The 3/8-second socket
  timeouts, zero retries, and sanitized diagnostics remain unchanged.
- Socket timeouts are not a total wall-clock deadline. Metadata refreshes for
  unseeded/expired airports can be slower, and memory use depends on report/layout
  and other plugins. Chromium-based Weather remains the larger measured workload.
