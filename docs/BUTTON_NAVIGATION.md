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
    "displaced": false,
    "display_uncertain": false
  }
}
```

Only JSON boolean `true` enables either mode; `enabled`, `manual_only`, and
`displaced` default to false. `display_uncertain` also defaults to false; it is a
worker-maintained presentation-safety marker, not a request to redraw. B/C/D are unassigned when absent or null.
`startup=false` suppresses the independent startup splash. It does not suppress
normal scheduled updates; use `manual_only=true` for one-attempt integration tests.

Stock saved plugin instances have no UUID. References use playlist name, plugin ID,
and instance name. Renaming/deleting an instance or disabling its plugin makes the
reference invalid. Invalid requests are ignored; an invalid selected instance
pauses button scheduling rather than falling back to ordinary playlists.

`RefreshTask.screen_selection.assign_button(button, reference)` validates and
persists assignments. Passing `None` removes an assignment. Use this API outside
GPIO callbacks. External JSON changes require an application restart. Assignment
UI controls remain future work. The [Aviation METAR plugin](METAR_PLUGIN.md)
provides the verified Button B screen.

## Contextual location cycling

Ordered location groups and the contextual C action are optional. They use existing
saved instances without changing plugin settings, caches, intervals, or playlists.
Add the following fields inside `button_navigation` to opt in, while retaining
`enabled=true`, `manual_only=true`, the A/B assignments above, and `startup=false`:

```json
{
  "actions": {
    "C": "cycle_active_plugin"
  },
  "groups": {
    "weather": {
      "instances": [
        {"playlist": "Default", "plugin_id": "weather", "instance": "Weather"},
        {"playlist": "Default", "plugin_id": "weather", "instance": "Other Weather Location"}
      ]
    },
    "metar": {
      "instances": [
        {"playlist": "Default", "plugin_id": "metar", "instance": "LSZL METAR"},
        {"playlist": "Default", "plugin_id": "metar", "instance": "Other Airport METAR"}
      ]
    }
  }
}
```

The two `Other ...` names are illustrative: create and configure those saved
instances separately before referencing them. No locations or airports are
hardcoded or automatically created. Each group member must match the group's
plugin ID. Duplicate references, invalid/missing instances, and disabled plugins
are skipped. Group order determines cycling, including across playlists; their
time windows do not determine button selection.

- A/B return to the remembered location for their assigned plugin, when it has a
  valid Weather or METAR group. Legacy assignments to other plugins stay literal.
- C advances within the current selection's group and wraps at the end. With no
  selected/pending context, an unsupported plugin, or fewer than two valid group
  members, it does nothing, including when the screen is displaced.
- C's explicit action takes precedence over a literal C assignment. Without the
  recognized action, C retains its ordinary assignment behavior. D is unchanged.
- Remembered references must still resolve and belong to the configured group.
  Otherwise A/B use a valid selected location in that group, then their original
  assignment, then the first valid group member. Removing an A/B assignment leaves
  that button unassigned. With no valid group, the original assignment is retained.
- If the current selection was deleted or omitted from a group containing at
  least two valid members, C starts at the first valid member.

Requested and in-flight references are volatile. Consuming a request never writes
`selected` or either location memory. After rendering and physical display return
successfully (or a valid known-image deduplication), the worker commits `selected`,
`button_navigation.remembered.weather` or `.metar`, successful refresh metadata,
and the instance timestamp in one atomic configuration replacement. Memories use
full references, not indexes. A successfully presented reference can subsequently
become invalid if its saved instance is removed; normal resolution/fallback rules
still apply. Only valid group members update group memory.

During work, C advances from the latest pending target, then the consuming/active
**button-selection** target, then committed selection. A web render never supplies
this context. Rapid presses replace one pending reference. Skipped intermediate
locations are not rendered or remembered. Cycling back to the active target cancels
intermediate pending work, without queuing another attempt if that active attempt
fails. Returning to the committed location while a different target is active is
accepted: it restores that location after success, or becomes a no-op if generation
failed before any display operation. A/B recognize their plugin's pending/in-flight
target before consulting independent committed memories. Completion never deletes
a newer pending request. GPIO callbacks do not persist state or render images.

Generation failure leaves committed selection, memories, refresh metadata and
`displaced` unchanged. It consumes the attempt, with no manual-only automatic retry.
For Contra → C/Lugano → provider failure, Contra remains selected/remembered;
A is a no-op and a later deliberate C press may try Lugano again. Initial selection
failure leaves selection null, so the assigned A/B can be deliberately retried.
A successful cached, stale or UNAVAILABLE METAR page is a presentation success;
an exception preventing image generation is a failure.

Web updates keep the existing selected-screen context and location memories.
Successful web displacement is restored by A/B; C chooses the next location.
Older configurations require no automatic redraw or queue migration. Existing
pre-fix selected/memory inconsistencies must be explicitly reconciled from known
successful operations; never blindly replace selection from web display metadata.

Location cycling uses the same worker and GPIO listener. Keep `manual_only=true`
for manual operation; automatic selected-screen refresh is described below. The presentation-state release has passed isolated Python 3.13.5
checks and physical Lugano/LSZL/LSZA navigation and independent-location-memory tests.
Both groups were subsequently enabled on PiFrame after Pillow validation; see
the current rollout results below.

## Selection, rendering, and persistence

`RefreshTask.request_button("A")` validates the assignment, posts a bounded
selection request, and wakes the worker. It performs no rendering or configuration
write. The GPIO listener calls the controller's `request_button()` directly.
During a refresh there is at most one pending selection; the latest accepted
request wins. An active physical refresh finishes before another begins. Pending
requests are transient and are not replayed after a crash.

The worker keeps selection intent volatile until presentation succeeds and uses the
existing `PlaylistRefresh`, image hashing, and DisplayManager pipeline. Configuration
transactions retain unrelated settings and atomically replace a flushed temporary
JSON file, preserving its mode. Failed selection persistence restores in-memory
selection. Serialization is within one process; cross-process writers and direct
mutation of returned configuration/model objects are not synchronized.

Condition/controller locks are not held during generation, display IO, or
persistence. Input may briefly wait for configuration synchronization; this is
not a hard real-time input system. Web manual updates remain serialized through
the same worker and do not change the selected button reference.

With button mode enabled and `manual_only=false`, a Weather/METAR group explicitly
enabled for automatic refresh controls the active screen's cadence. Group intervals replace the earlier selected
instance's interval/daily-time scheduling. Instance settings are not edited.
Disabled button mode retains ordinary playlist scheduling and its existing rules.

With `manual_only=true`, each newly consumed selection gets one render attempt.
No periodic selected-screen update, automatic failure retry, or playlist fallback
occurs. Repeated requests for the committed, undisplaced screen are ignored while
display state is known. Failed targets can be deliberately requested again after
completion; displaced or uncertain-screen restorations can also be deliberately
retried. Unconsumed requests are discarded on shutdown, never replayed on restart.
Changing manual-only mode does not enqueue a refresh, but returning to automatic
mode allows due updates on subsequent checks. Disabling button mode restores
playlists even if manual-only remains true.

## Automatic refresh of the active screen

Automatic refresh requires `enabled=true`, `manual_only=false`, and JSON
boolean `auto_refresh_enabled=true` on the selected plugin's group. Missing or
non-boolean enablement defaults to **off**, including older configurations that
already have `manual_only=false`. Migration never silently enables either plugin.
Keep `startup=false` to suppress the separate startup splash. Example fields inside
`button_navigation` for a METAR-only rollout, retaining the saved references and
A/B/C mappings above (example only; do not apply to a running service):

```json
{
  "manual_only": false,
  "startup_grace_seconds": 60,
  "groups": {
    "weather": {
      "auto_refresh_enabled": false,
      "interval_seconds": 900,
      "instances": [
        {"playlist": "Default", "plugin_id": "weather", "instance": "Weather"},
        {"playlist": "Default", "plugin_id": "weather", "instance": "Other Weather Location"}
      ]
    },
    "metar": {
      "auto_refresh_enabled": true,
      "interval_seconds": 900,
      "instances": [
        {"playlist": "Default", "plugin_id": "metar", "instance": "LSZL METAR"},
        {"playlist": "Default", "plugin_id": "metar", "instance": "Other Airport METAR"}
      ]
    }
  }
}
```

An explicitly enabled supported group defaults to 900 seconds if `interval_seconds` is
absent. An absent/malformed group or invalid interval pauses that plugin's automatic
refresh: intervals must be positive finite numbers, not booleans or numeric strings.
Only Weather and METAR are supported by this scheduler. Other button assignments
remain usable manually. No instances are created and no plugin settings are changed.
Turning a group's automatic refresh off does not remove its instances from C cycling,
change A/B defaults or memories, disable deliberate button/web rendering, or alter
the other plugin's interval. Both groups can be enabled independently. A Weather
selection during a METAR-only rollout stays manual until another METAR selection
successfully commits. No inactive group runs in the background.

The worker considers only the committed selected reference. It never rotates
playlists, switches plugins/locations, polls an inactive plugin, or fetches outside
generation. Selection must resolve; null selection, displacement or uncertainty
pauses automatic work. `manual_only=true` overrides every automatic deadline.
Disabling button mode restores ordinary playlists, rather than this scheduler.

The existing worker checks deadlines at most every 60 seconds (or the shorter
plugin-cycle interval), so an attempt can start up to one check late. There is no
catch-up queue. A pending web ticket or latest button request takes priority; input
arriving during the durable start write supersedes automatic work before generation.
Once generation has started it finishes serially, with at most one latest button
selection waiting. C uses the committed/pending/button-in-flight navigation context,
never a scheduled or web target. No condition/controller lock is held during
generation, display IO or timing persistence.

Next attempts are paced from completion, including failed, cancelled, superseded,
and identical-image attempts. Buttons and saved-instance web updates in automatic
mode also restart their plugin's cadence. A failed new navigation target does not
become selected and is never retried by the scheduler; the old committed screen
remains the only automatic candidate. The two plugins maintain separate deadlines.

### Durable timing and restart recovery

The worker owns `button_navigation.refresh_state.weather` and `.metar`. Each is a
bounded last-attempt journal, not a queue or per-airport history:

- `attempt_reference`, UTC `attempt_started_at`, `attempt_completed_at`, and
  `outcome` (`running`, `presented`, `unchanged`, `failed`, `cancelled`, `superseded`).
- `presentation_reference` and `presentation_success_at` record the last known
  successful presentation or valid hash deduplication.
- `live_data_reference` and `live_data_success_at` separately record the last
  successfully presented live retrieval. METAR `data_status` is `live`, `stale`,
  `cached`, `cached_stale` or `unavailable`; a newly fetched but old report can be
  `stale` while still constituting a live retrieval. Cached/UNAVAILABLE presentations
  never advance live-data success. Missing result annotations are conservative.

Start timing is atomically persisted before generation. A successful completion
commits timing with presentation metadata/navigation in the same existing atomic
configuration transaction. Failure/cancellation records completion separately,
retaining the previous success timestamps and committed display state. Manual-only,
non-opted-in, resource-paused and unsupported/ungrouped plugin operation do not start
new journal attempts. A pause discovered during an existing attempt does not discard
its completion or success evidence.

Running deadlines use monotonic time. UTC completion timestamps are consulted once
per plugin after restart. The startup grace defaults to 60 seconds; a positive
finite `startup_grace_seconds` overrides it, and invalid values use 60. Even overdue
work waits for grace. Recent completion waits the remaining interval. Missing,
invalid or future timing waits a full interval conservatively. An unfinished
`running` attempt instead requires operator acknowledgement through the persistent
pause below; pending or failed targets are not replayed. Runtime wall-clock jumps do not move deadlines.
Restart recovery depends on the system's UTC clock and cannot infer elapsed time
perfectly when that clock is wrong.

If the durable start write fails, no generation begins. If start/completion journal
persistence fails, automatic work pauses for that process lifetime; deliberate input
remains separate and may attempt persistence again. A persisted `running` marker
causes a conservative persistent pause after a crash. Persistent storage failure can
defeat durability entirely; startup grace remains, and operator inspection is needed.
Any uncertain physical result continues to block automatic refresh independently.

### Persistent resource-failure pause

`button_navigation.resource_pause` is a worker-owned record containing `reason`,
UTC `recorded_at`, the relevant saved `reference` when known, and allowlisted numeric
`details`. Its presence blocks automatic refresh for **both** groups. Missing/null
means no persisted pause; malformed values fail closed. The first reason is retained.
Neither successful manual navigation, normal generation, reboot, nor turning
`manual_only` on/off clears it. A/B/C and deliberate web work remain usable while
the worker is alive, and successful presentation still commits normally.

Reliable local triggers are:

- Current memory or a newly increased kernel peak at least **95% of this cgroup's finite
  `memory.max`**: 190 MiB under the unchanged 200 MiB limit. Above 90% logs one
  warning, without pausing or interrupting display IO.
- New increments of `memory.events.max`, `oom`, `oom_kill` or `oom_group_kill` in this
  service's cgroup, including its browser descendants. The first valid sample is
  a baseline: pre-existing `max=349` is neither a new event nor 349 failed renders.
- Chromium deadline, nonzero exit, unavailable/unlaunchable browser, invalid/missing
  screenshot, or stalled/failed group cleanup. A browser exit is not labelled OOM
  without kernel evidence. Existing 60-second deadline, SIGTERM/SIGKILL cleanup,
  temporary-directory cleanup and successful screenshot return values remain intact.
- A caught `MemoryError`, unexpected worker return/uncaught termination, or an
  unfinished durable `running` attempt discovered at startup. Interruption has an
  **unknown cause**; it does not prove an OOM.

Network/provider exceptions and METAR cached/stale/UNAVAILABLE output alone do not
set a resource pause. Their existing completion-based interval and separate
live-data/presentation timestamps remain unchanged. Normal screenshot cancellation
during orderly shutdown does not pause, unless an independent cleanup failure occurs.

The same worker reads its own non-root cgroup-v2 accounting at startup, worker
checks, generation/display boundaries, and existing Chromium wait checkpoints.
There is no additional render/polling thread, process, network poll, or systemd
change. Hooks are scoped to this worker thread. A critical signal closes future
automatic admission, but a valid attempt already executing may finish and commit.
No warning/critical threshold terminates active physical display IO. Kernel peak
and event evidence during display is examined when the driver returns.

Kernel counters and peaks are lifetime accounting, not per-attempt measurements.
The first valid snapshot establishes the reference high-water mark and counters;
current memory can still trigger immediately. Later snapshots compare increments
and rising peaks. A counter/peak decrease establishes a new accounting epoch instead
of manufacturing a negative or fresh event. Missing/unreadable samples preserve the
last valid baseline; no kernel accounting files are reset or written.

If a historical peak is already high, a new brief spike below that old peak cannot
be distinguished from history using `memory.peak`. Sampled current memory and new
limit/OOM counter increments still protect, but this is not continuous visibility.
Events before the first valid snapshot cannot be dated or attributed as new; crash
recovery separately uses persistent pause/attempt state and does not clear it.

The in-memory latch is set before persistence is attempted. If writing the pause
fails, automatic admission stays closed for that object; completion transactions
retain the pending pause, and attempt finalization makes one bounded write retry.
No retry queue is created. Persistent storage failure can still prevent durability.

Python cannot guarantee a pause write before SIGKILL, OOM, power loss or storage
failure. The pre-generation attempt marker allows a surviving `running` record to
pause recovery, but a kill before that marker, startup failure, or total disk
failure can escape this protection. There is no claim of kernel OOM prevention or
systemd restart-loop containment. An unexpectedly dead worker is logged and rejects
web refresh work; its HTTP server may still answer health requests. If the driver
or native code hangs forever, this worker cannot persist a new pause until control
returns. Detached browser descendants can still escape process-group cleanup.

If cgroup v2/peak/events cannot be read (including non-Linux development systems),
one warning reports memory monitoring unavailable. No pressure value is invented;
browser, allocation and interrupted-attempt protection remains. This fallback is
not evidence that unattended memory use is safe. No limits, swap or Chromium flags
are changed by these guards.

### Deliberate operator recovery

Do not automatically clear pauses or restart repeatedly. Preserve logs, cgroup
events, the pause record and attempt journal; establish the cause and safe memory
conditions first. The following is an **offline recovery recipe for a separately
approved maintenance window**, using the release's existing Python environment and
`Config.update_section` API after the service is completely stopped. Back up the
then-current configuration, verify the isolated release paths and use its existing
activation/rollback procedure. Config is cached; external file edits alone do not
update a running worker.

```python
from datetime import datetime, timezone
from config import Config  # imported from the intended isolated release's src

def acknowledge(nav):
    nav["manual_only"] = True  # verify safely before any later automatic opt-in
    nav.pop("resource_pause", None)
    for attempt in nav.get("refresh_state", {}).values():
        if isinstance(attempt, dict) and attempt.get("outcome") == "running":
            attempt["outcome"] = "interrupted"
            attempt["interruption_acknowledged_at"] = datetime.now(timezone.utc).isoformat()

Config().update_section("button_navigation", acknowledge)
```

Acknowledgement invents no attempt-completion, presentation-success or live-data
success timestamp. It preserves assignments, group enablement/intervals, memories,
selected reference, displacement, uncertainty, saved settings and caches. Do not
clear `display_uncertain` without separate successful presentation evidence. Verify
JSON diff/permissions, then start in manual-only mode and check health. A fresh
process is required to clear the in-memory latch. Current critical memory or fresh
pressure evidence will pause it again; old lifetime counts alone will not. Re-enable only the intended group in a separately approved
operation; acknowledged incomplete timing waits a full interval after initialization.

### Exact-image deduplication

Identical rendered pixels skip physical display IO when the current display is
known. Attempt and presentation timing still advance, with the full interval before
the next attempt. Uncertainty/displacement restoration retains the existing explicit
redraw rules. This compares the whole rendered image only: Weather graph/timestamp
changes and METAR age/CURRENT/CACHED/STALE status changes may require a redraw even
when core conditions are unchanged. Age labels remain static between generations.
There is no image-region masking or semantic deduplication, and no claim that a
changed report always changes pixels. Network activity still occurs once per due
generation; hashing is not a substitute for conservative fetching/cache behavior.

## Web updates and restoration

After a successful web update that changes the image and does not identify the
selected saved instance, the worker persists `displaced=true` with refresh
metadata. Failed updates and identical-image updates do not mark displacement.
Identifying the selected instance requires the full composite reference, not
just its plugin ID.

While displaced, pressing the selected button queues a restoration despite its
unchanged reference. Consumption preserves this intent and forces physical display
IO even if cached metadata claims the generated image hash matches. Switching to
another target already successfully displayed through the web may instead deduplicate
the same known image while committing the new button context. Displacement
clears only after successful restoration; pending/active duplicate requests are
suppressed. Manual-only mode never retries automatically after failure.

A request arriving during a web render waits for that render to finish. If the web
update failed or did not displace the screen, an unchanged selection produces no
extra refresh unless display execution failed and left uncertainty. Other pending
selections retain latest-wins behavior.

The displaced marker survives restart without an automatic manual-only refresh.
Disabled button mode does not update selection/memory/displacement bookkeeping.
Deleting a saved instance does not cancel an already active physical operation.

## Display uncertainty and persistence failures

Immediately before calling the existing DisplayManager, the worker persists
`button_navigation.display_uncertain=true`. Both the JSON payload and its parent
directory are fsynced. If that write fails, display IO is not entered. The flag
also protects ordinary playlist/web display operations when button mode is off,
without changing their selection or scheduling behavior.

After a normal display return, the worker commits navigation/refresh metadata and
clears uncertainty together. On persistence failure it restores cached configuration
and model state and reasserts uncertainty; a failure after rename is treated as
potentially committed until that conservative write succeeds. A generation failure
never marks uncertainty. Failure after entering DisplayManager conservatively does,
even if failure happened while transforming/saving the image before driver IO.

A deliberate restoration bypasses hash deduplication while uncertain. Neither
uncertainty nor restart enqueues work or enables scheduling. The marker survives
process interruption; without presentation success, committed state stays unchanged.
Shutdown cancels before display when possible, discards unconsumed requests and
waits for a physical operation already entered: a normal return is committed,
whereas an interrupted/failed return retains uncertainty. The driver is not forcibly
interrupted and no locks are held over rendering or display IO.

`current_image.png` and plugin images can be saved before hardware completion;
they are not proof of the panel's contents. Successful hash deduplication records
logical presentation, not another physical display call. A normal driver return
also cannot prove optical correctness. Out-of-band hardware access is untracked.
No software can guarantee durability when storage/fsync or the filesystem itself
fails persistently; failure to reassert the marker is logged and requires operator
inspection, not an automatic refresh.

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

The navigation/Chromium foundation contains **137 tests** using mocks for
hardware/rendering. The committed METAR baseline passed **240 tests**; the complete
suite before the presentation-state correction passed **294 tests**. The
presentation-state correction passed **324 tests** locally and 151 isolated Python
3.13.5 cases; its staged release passed 153 guarded checks. Physical tests confirmed
Lugano/LSZL/LSZA navigation and independent Weather/METAR memories, with no manual-only
automatic retries. The scheduler baseline passed **387 local tests** and isolated
Python 3.13.5 validation. One controlled automatic METAR attempt and one controlled
automatic Contra Weather attempt subsequently passed on PiFrame, each after about
900 seconds; automatic mode was restored to manual-only after each test. The latest
Weather attempt used 36.6 seconds generation and 28.1 seconds display, with a
163.2 MiB kernel memory peak, 102.3 MiB swap peak and no limit/OOM events. Earlier
Weather tests reached 191.9 MiB and historical service lifetimes reached the 200 MiB
boundary, so a single successful attempt does not prove unattended safety.

The per-group opt-in and persistent resource guards were first validated offline.
They passed **326 guarded Python 3.13.5 cases on PiFrame**, using synthetic cgroups,
fake Chromium/display operations, simulated clocks and sanitized deployed-format
configuration. No live service/configuration/hardware/network access was permitted
inside the test process; before/after service/runtime fingerprints were unchanged.
Historical-counter baseline handling is covered, including resets and unreadable
samples. This is mock compatibility validation, not unattended hardware validation.
That resource-guard baseline passed **458 local
tests**, including 71 added resource/enablement cases and updated interruption
expectations. The three existing Pillow `getdata()` deprecation warnings remain.
The complete local suite includes scheduler tests using
fake monotonic/UTC clocks, durable attempt recovery, preemption, failure pacing,
deduplication and live-versus-fallback METAR results. Other cases cover
group validation/order, separate remembered locations, concurrent and rapid C
requests during generation/display, atomic persistence failure, restart recovery,
web restoration, manual-only suppression, and disabled-mode playlist behavior.
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
missed deliberate presses. Initial end-to-end testing assigned A to Weather: a comic
web update displaced Weather; A restored it once; repeated A caused no refresh.
Manual-only scheduling/retries stayed suppressed. The user confirmed visible output.
The subsequent A → B → A METAR regression and reboot persistence are recorded in
[the METAR baseline](METAR_PLUGIN.md#verified-baseline-2026-10-17).

A real 160 × 100 static Chromium screenshot passed on Python 3.13.5, as did
controlled subprocess timeout/cancellation tests. The approved Weather measurement
recorded a kernel cgroup peak of **178.8 MiB** under **MemoryMax=200 MiB** and
**CPUQuota=40%**, zero limit-hit/OOM events or restarts, approximately 33.7 seconds
of generation and 28 seconds of physical refresh. Chromium was observed for
31.2 seconds and exited before display IO. No screenshot/profile directories remained.
Weather generation reached a sampled swap peak of 105.5 MiB; post-refresh retained
file cache and swap do not establish a leak. Sampling targeted 500 ms with occasional
1.18-second gaps; kernel peak records account for intervening memory spikes.

### Current Pillow Weather rollout — 2026-10-19

The integrated suite passes **546 local tests**, and **88 focused Python 3.13.5
Weather/renderer tests** passed in an isolated Pi copy. Pillow is the 800 × 480
Weather default, with explicit Chromium fallback and no automatic browser retry.
A physical Little Salmon test passed at 105.9 MiB service memory peak; the first
automatic Little Salmon cycle then passed at 102.4 MiB, zero swap and zero limit/OOM
events. It generated once and displayed once, with durable attempt/presentation/live
data success and no overlapping work or immediate retry.

Both groups are now enabled at 900 seconds with manual_only=false. The historical
resource pause was recovered deliberately after backup; protection remains enabled.
The overdue journal made the first attempt eligible after the 60-second startup
grace (observed 60.4 seconds), not after a full initial 900 seconds. Subsequent
attempts are completion-relative. Exact-image hashing includes refresh timestamps,
so unchanged underlying weather can still produce a physical update.

See [current deployment status](PIFRAME_SETUP.md#phase-2-status--2026-10-19) and
[Weather rendering](WEATHER_RENDERING.md). Earlier Chromium measurements above are
historical; neither a single passing test nor these lower Pillow peaks guarantees
unattended safety for every provider response.

## Isolated deployment and rollback

Build checksummed source snapshots into new release directories, preserving the
running release. Copy configuration, saved instances, assets, and runtime images
into independent paths; compare live state before activation and stop if it changed.
Carry selected/displaced state forward without queuing input. Keep manual-only true,
startup false, the intended validated assignments, and playlists paused for controlled
integration tests. The deployed Phase 2 configuration assigns A/B to remembered
Weather/METAR locations, C to contextual cycling and D unassigned. Production
automatic enablement requires a separate deliberate operation after validation.

Use the existing virtual environment and a systemd drop-in overriding only
ExecStart/WorkingDirectory. A persistent drop-in under
`/etc/systemd/system/inkypi.service.d/` retains the release paths across reboot;
`/run/systemd/system/` is suitable only for temporary activation. Point launcher
environment and writable paths into the new release. Verify the unit, limits, privileged process cwd, and isolated
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
