# Physical button navigation

Optional in-process button navigation uses the existing InkyPi refresh worker,
plugins, and display driver. It is disabled by default. This feature was developed
from the Phase 1 baseline `307dc0497961cfd62f0276f3d0026422f8316d02`.

## Configuration and saved instances

Add a `button_navigation` section to the device configuration only when enabling
this feature. Existing installations need no configuration changes. Example for
an already saved Weather instance:

```json
{
  "button_navigation": {
    "enabled": true,
    "manual_only": true,
    "assignments": {
      "A": {"playlist": "Default", "plugin_id": "weather", "instance": "Weather"}
    },
    "selected": null,
    "displaced": false
  }
}
```

Only JSON boolean `true` enables either mode; `enabled`, `manual_only`, and
`displaced` default to false. B/C/D are unassigned when absent or null.
`startup=false` suppresses the independent startup splash. It does not suppress
normal scheduled updates; use `manual_only=true` for one-attempt integration tests.

Stock saved plugin instances have no UUID. References use playlist name, plugin ID,
and instance name. Renaming/deleting an instance or disabling its plugin makes the
reference invalid. Invalid requests are ignored; an invalid selected instance
pauses button scheduling rather than falling back to ordinary playlists.

`RefreshTask.screen_selection.assign_button(button, reference)` validates and
persists assignments. Passing `None` removes an assignment. Use this API outside
GPIO callbacks. External JSON changes require an application restart. Assignment
UI controls and additional aviation plugins are future work.

## Selection, rendering, and persistence

`RefreshTask.request_button("A")` validates the assignment, posts a bounded
selection request, and wakes the worker. It performs no rendering or configuration
write. The GPIO listener calls the controller's `request_button()` directly.
During a refresh there is at most one pending selection; the latest accepted
request wins. An active physical refresh finishes before another begins. Pending
requests are transient and are not replayed after a crash.

The worker persists the selected reference before generation and uses the existing
`PlaylistRefresh`, image hashing, and DisplayManager pipeline. Configuration
transactions retain unrelated settings and atomically replace a flushed temporary
JSON file, preserving its mode. Failed selection persistence restores in-memory
selection. Serialization is within one process; cross-process writers and direct
mutation of returned configuration/model objects are not synchronized.

Condition/controller locks are not held during generation, display IO, or
persistence. Input may briefly wait for configuration synchronization; this is
not a hard real-time input system. Web manual updates remain serialized through
the same worker and do not change the selected button reference.

With button mode enabled and `manual_only=false`, the selected saved instance's
interval or daily scheduled time controls updates, independent of playlist time
windows. Due checks occur at most every 60 seconds, or the shorter plugin-cycle
interval, after active work finishes. A selection change forces generation;
unchanged image hashes normally skip physical display IO. Failed refreshes retain
the selection and its prior successful timestamp, allowing later scheduled retry.
A restart restores selection, but an overdue normal-mode instance may refresh on
the next due check. Disabled button mode retains ordinary playlist scheduling.

With `manual_only=true`, each newly consumed selection gets one render attempt.
No periodic selected-screen update, automatic failure retry, or playlist fallback
occurs. Repeated requests for the selected, undisplaced screen are ignored.
Initial selection failure therefore requires selecting another instance before
returning; a displaced-screen restoration can be deliberately retried after failure.
Changing manual-only mode does not enqueue a refresh, but returning to normal mode
allows overdue updates on subsequent checks. Disabling button mode restores
playlists even if manual-only remains true.

## Web updates and restoration

After a successful web update that changes the image and does not identify the
selected saved instance, the worker persists `displaced=true` with refresh
metadata. Failed updates and identical-image updates do not mark displacement.
Identifying the selected instance requires the full composite reference, not
just its plugin ID.

While displaced, pressing the selected button queues a restoration despite its
unchanged reference. Consumption preserves this intent and forces physical display
IO even if cached metadata claims the generated image hash matches. Displacement
clears only after successful restoration; pending/active duplicate requests are
suppressed. Manual-only mode never retries automatically after failure.

A request arriving during a web render waits for that render to finish. If the web
update failed or did not displace the screen, an unchanged selection produces no
extra refresh. Other pending selections retain latest-wins behavior.

The displaced marker survives restart without an automatic manual-only refresh.
Disabled button mode does not update displacement bookkeeping. Out-of-band display
operations and power loss during physical refresh cannot be tracked reliably.
Deleting a saved instance does not cancel an already active physical operation.

## GPIO listener and lifecycle

The verified Pi Zero 2 W mapping on `/dev/gpiochip0` is:

| Button | BCM GPIO |
|---|---:|
| A | 5 |
| B | 6 |
| C | 16 |
| D | 24 |

The listener imports gpiod only when enabled, using its v2 API and consumer name
`inkypi-buttons`. It requests input lines with pull-up bias, both-edge detection,
monotonic event timestamps, and a 64-event buffer. Falling edges begin presses;
rising edges recognize releases. Stable low for 75 ms accepts one press, and
stable high for 75 ms rearms that button. Short taps are filtered; buttons held at
initialization must first be released. Batched events retain timestamp order.

The listener starts after the startup splash path. Disabling mode releases lines
on its next approximately 20 ms poll. Re-enabling resets input state. Missing
gpiod, busy lines, permission/read errors, and per-line sequence gaps release
resources and latch failure until mode is disabled/re-enabled or the app restarts.
The web app and worker continue. No additional systemd service is required.

Per-line event discontinuities are fatal, including loss before the first event.
Global-only counter anomalies with valid per-line counters are diagnostic: warn
for the first five per request, then log at DEBUG. Physical multi-line tests observed
repeated/skipped global counters with continuous per-line events. The bindings copy
scalar fields into independent events; buffer/object reuse was not demonstrated.
The kernel's hard-IRQ versus threaded-event sequencing is a plausible explanation,
not proof of the installed kernel's exact execution path. See the
[libgpiod counters](https://libgpiod.readthedocs.io/en/master/core_edge_event.html),
[Python binding source](https://pypi.org/project/gpiod/2.5.0/), and
[Raspberry Pi kernel implementation](https://github.com/raspberrypi/linux/blob/rpi-6.18.y/drivers/gpio/gpiolib-cdev.c).
A lost final event may remain undetectable until another event exposes a line gap.

Application shutdown stops/joins the listener and then the refresh worker.
SIGTERM, normal server exit, and KeyboardInterrupt use this cleanup path. The
worker's wait predicate includes shutdown, preventing the stock lost-notification
shutdown race. A physical refresh already underway is allowed to finish.

## Chromium deadlines and cleanup

Screenshots have a 60-second Python process deadline, separate from Chromium's
existing page timeout. Both screenshot helpers accept the keyword
`process_timeout_seconds` for a different positive finite bound. Existing flags,
successful pixels, and return conventions remain unchanged: independent PIL image
on success, logged failure and `None` on error/cancellation.

Chromium runs in a private POSIX session. Cleanup sends SIGTERM to its process
group, allows up to two seconds, then sends SIGKILL if members remain. Pipe
collection has a further two-second bound; if pipes remain open, they are closed
and reaping the leader has a final two-second bound. HTML/screenshot temporary
directories are removed on all exit paths. Chromium-created profiles remain its
responsibility; no profile flags were introduced. Deliberately detached descendants
can escape process-group cleanup.

Worker shutdown cancels screenshots and rejects new calls before joining; start
resets the admission gate. The worker checks shutdown after generation, preventing
an interrupted render from advancing to display IO. Existing network calls and
in-progress physical operations retain their timeout behavior. This hardening
does not reduce normal Chromium memory peaks.

## Verified results

The complete local suite passes **137 tests** using mocks for hardware/rendering.
Run in a development environment with the test dependencies available:

```sh
PYTHONPATH=src:. python -m pytest -q
git diff --check
```

Coverage includes selection/persistence, invalid references, latest-wins requests,
manual-only scheduling, web displacement/restoration, rendering failures,
disabled-mode playlists, debounce, event sequences, listener lifecycle, atomic
configuration writes, screenshot deadlines, graceful/forced termination, cleanup,
and shutdown without a display call.

PiFrame validation used Python 3.13.5, Inky 2.4.0, and gpiod 2.5.0 (bundled
libgpiod 2.3), with the E673 variant 22 display at 800 × 480. Individual and combined
input-only tests verified all four buttons, stable releases, and no duplicate or
missed deliberate presses. End-to-end testing assigned only A to Weather: a comic
web update displaced Weather; A restored it once; repeated A caused no refresh.
Manual-only scheduling/retries stayed suppressed. The user confirmed visible output.

A real 160 × 100 static Chromium screenshot passed on Python 3.13.5, as did
controlled subprocess timeout/cancellation tests. The approved Weather measurement
recorded a kernel cgroup peak of **178.8 MiB** under **MemoryMax=200 MiB** and
**CPUQuota=40%**, zero limit-hit/OOM events or restarts, approximately 33.7 seconds
of generation and 28 seconds of physical refresh. Chromium was observed for
31.2 seconds and exited before display IO. No screenshot/profile directories remained.
Weather generation reached a sampled swap peak of 105.5 MiB; post-refresh retained
file cache and swap do not establish a leak. Sampling targeted 500 ms with occasional
1.18-second gaps; kernel peak records account for intervening memory spikes.

## Isolated deployment and rollback

Build checksummed source snapshots into new release directories, preserving the
running release. Copy configuration, saved instances, assets, and runtime images
into independent paths; compare live state before activation and stop if it changed.
Carry selected/displaced state forward without queuing input. Keep manual-only true,
startup false, B/C/D unassigned, and playlists paused for controlled integration tests.

Use the existing virtual environment and a temporary systemd drop-in overriding
only ExecStart/WorkingDirectory. Point launcher environment and writable paths into
the new release. Verify the unit, limits, privileged process cwd, and isolated
configuration. Stop the old service and confirm complete process/GPIO release
before starting another. On failure, stop the candidate and restore the previous
drop-in and release without copying new state into the rollback configuration.
The original production checkout, boot configuration, dependencies, and boot-enable
state remain unchanged. A `/run` drop-in disappears at reboot.

Before later approved rendering tests, observe memory.current, memory.peak,
memory.stat, memory.events, memory.swap.current, process RSS/PSS, and journal phases.
Cgroup peak is cumulative; sampled phase maxima can miss short spikes. RSS sums
count shared pages repeatedly and must not be treated as cgroup usage. Measure swap
separately. Do not reset peak counters or trigger rendering from monitoring tools.

Generated configuration, images, release archives, deployment scripts, and measurement
logs are runtime artifacts; they are not part of this source commit. Further button
assignments, scheduling changes, deployments, and physical tests require their own
scope and validation.
