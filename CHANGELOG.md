# Changelog

## 2026-10-19 — PiFrame Phase 2

- Add contextual Button C location cycling and independent remembered Weather and
  METAR locations for Buttons A/B, using saved-instance references and latest-wins
  requests through the existing serialized worker.
- Commit navigation/display metadata only after successful presentation; retain
  durable display uncertainty across interrupted or failed physical operations.
- Add selected-screen automatic refresh with per-plugin opt-in, independent
  intervals, startup grace and durable completion-based attempt/success timing.
- Add persistent resource-failure pauses with historical cgroup counter baselines,
  Chromium failure detection and deliberate operator recovery.
- Make Pillow the default fixed 800 × 480 Weather renderer, including vector wind
  arrows; retain original assets/provider parsing and explicit Chromium fallback
  without automatic fallback on Pillow failure.
- Add deterministic Weather fixtures, offline previews and regression coverage.

Automatic scheduling remains opt-in for existing/new configurations; PiFrame's
verified deployment explicitly enables both groups at 900 seconds. See
[deployment history](docs/PIFRAME_SETUP.md) for measured results and limits.
