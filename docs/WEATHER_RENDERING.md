# Weather rendering

Weather defaults to Pillow for the fixed 800 × 480 landscape dashboard. Existing
saved instances require no new setting. Provider requests, parsing, rounding,
units, timezone handling, icon selection and the shared serialized refresh and
DisplayManager pipeline are unchanged. Pillow returns a caller-owned RGB image;
it does not quantize to the METAR palette or touch hardware.

The optional saved setting `renderer` accepts `pillow` (default) or `chromium`.
The Weather settings form exposes this choice. Chromium remains an explicit
fallback with the original HTML/CSS, assets, Chart.js and launch flags. There is
**no automatic browser fallback** after Pillow errors. Unsupported resolutions,
vertical layouts, image backgrounds or unsupported Pillow style options fail
explicitly; choose Chromium deliberately for those layouts.

The production model and renderer are `plugins/weather/dashboard.py` and
`plugins/weather/pillow_rendering.py`. Offline previews, fixtures and regression tests import these production modules
directly; unused prototype shims have been removed. Rendering time is supplied explicitly;
weather values are not recalculated by the renderer. Wind arrows use the existing
parser's eight compass arrows, drawn with vector strokes because Jost lacks those
glyphs. Only referenced original assets are decoded; fonts are cached per render;
only the chart uses a bounded 2× antialiasing workspace.

Pillow differs visibly in font spacing, chart interpolation, gradients and icon
sizes. This is not pixel-equivalent browser emulation. E-Ink color conversion
continues to be owned by DisplayManager. Review the physical panel before enabling
unattended use. No navigation, remembered-location, scheduler or resource-pause
semantics change. A failed generation preserves the last committed display;
physical failures retain display uncertainty through the existing worker.

Deploy into a new independent release, preserving configuration, all saved
instances, cached reports, images and journal timestamps. Keep manual_only=true,
both group auto_refresh_enabled=false and the existing memory_critical pause.
The path-only persistent service switch requires approval; retain the previous
release and exact drop-in for rollback. Activation must not render or fetch.
Select remembered Little Salmon with one user-operated Button A press afterward.
Do not clear a resource pause merely because isolated benchmarks used less memory.

## Verified PiFrame rollout — 2026-10-19

The isolated three-pair benchmark measured median cgroup peaks of 152.3 MiB for
Chromium and 50.5 MiB for Pillow, with median renderer times 35.3 s and 4.6 s.
Pillow used no swap. Fresh cgroups do not imply cold file caches; a lower single
Pillow peak reflected file-cache charging variation. These isolated numbers are
not the full service's footprint or a guarantee for other inputs.

A user-confirmed Little Salmon physical render passed at 105.9 MiB service memory
peak, approximately 6.1 s generation and 35.3 s total. After deliberate recovery of
the backed-up historical resource pause, one automatic cycle passed at 102.4 MiB,
6.4 s generation and 35.6 s total, with no swap, memory-limit or OOM events. The
running configuration now enables both groups at 900 seconds with manual_only=false;
resource protection remains enabled. The disabled settings above describe safe
preparation/activation, not the current operating state. No visual defects were
reported on the physical display, including the wind arrow.

See [offline tests and previews](WEATHER_TESTING.md) and
[deployment status](PIFRAME_SETUP.md#phase-2-status--2026-10-19). Runtime state,
release backups and generated benchmark reports are local operational artifacts;
they are excluded from Git. Keep previous releases and latest display/journal
state when rolling back; an older release's metadata can be stale after presentation.
