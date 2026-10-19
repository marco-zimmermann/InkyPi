# Offline Weather testing and previews

The production renderer is `src/plugins/weather/pillow_rendering.py`; the immutable
renderer-independent model is `src/plugins/weather/dashboard.py`. Tests and preview
scripts use these modules directly. There is no separate prototype implementation.

`tests/fixtures/weather/synthetic_dashboard.json` contains clearly labelled synthetic
Weather/AQI data and an explicit timezone-aware timestamp. The fixture adapter
`scripts/weather_pillow_fixture.py` calls the existing provider parser with a frozen
clock; it never fetches weather, opens a browser or touches hardware. Keep this
fixture and the original asset-hash manifest. The latter protects unchanged browser
templates, icons and fonts; intentional Weather dispatch/settings changes are covered
by the integration tests.

Run the suite using an existing environment with the project/test dependencies:

```sh
PYTHONPATH=src:. python -m pytest -q
```

Generate a deterministic standalone 800 × 480 RGB preview:

```sh
PYTHONPATH=src:scripts python scripts/preview_weather_pillow.py \
  --output-dir mock_display_output/weather-review
```

Optionally add `--baseline path/to/frozen-chromium.png` to compare an existing image
of the same fixture. The script never launches Chromium. It exports the model,
PNG, difference image, overlay and enlarged conditions/graph/forecast crops.
Different rasterizers need not produce identical pixels; compare numerical data,
labels, icon choice, units and layout. Keep generated files in ignored output paths.

Regression tests cover both provider model formats, numerical/unit/time parity,
forecast ordering, moon phases, polar/missing data, long titles, negative/constant
and zero temperatures, extreme rain probabilities/amounts, annotation collisions,
forecast lengths, frames/margins, referenced-only asset loading, bounded chart
workspace, all wind arrows, deterministic output and RGB dimensions. Dispatch tests
exercise default Pillow, explicit Chromium, unsupported layouts and propagated
Pillow/provider errors without browser fallback. Navigation/resource tests separately
cover persistence, latest-wins, uncertainty, pacing and critical-failure recovery.

Pi Python 3.13.5 with Pillow 12.3.0 passed 88 focused Weather/renderer tests. Local
FreeType/Pillow/RAQM versions can alter text rasterization; output must be stable
within one environment, while cross-platform pixel differences require inspection.
Fixtures must never be shown as current weather. Preview and mock tests cannot
certify physical six-color appearance or unattended memory safety.

The verified kernel-cgroup benchmark and panel results are summarized in
[WEATHER_RENDERING.md](WEATHER_RENDERING.md). Future measurements need fresh isolated
cgroups and identical inputs; process RSS is not a substitute for cgroup memory.
Retain the validated sandbox/controllers and reports under ignored
`mock_display_output/` for reproducibility. Resource-intensive Pi trials require
separate operational approval and are not part of the local test suite.
