"""GPIO-independent selection of existing saved plugin instances.

Only the refresh worker consumes selections and persists state. Event callbacks
publish into a bounded mailbox; they never render or write files. Configuration
synchronization can briefly delay callbacks; this is not a hard real-time API.
"""
import logging
import math
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ScreenSelectionController:
    BUTTONS = ("A", "B", "C", "D")
    LOCATION_PLUGINS = ("weather", "metar")

    def __init__(self, device_config, wake, *, monotonic=None, utcnow=None):
        self.device_config = device_config
        self._wake = wake
        self._lock = threading.Lock()
        self._pending = None
        self._consuming = None
        self._active = None
        self._selection_active = None
        self._web_inflight = False
        self._monotonic = monotonic or time.monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._attempt = None
        self._resource_pause_latched = False
        self._resource_pause_record = None
        self.reset_scheduler()

    def reset_scheduler(self):
        """Startup never replays interrupted work or immediately renders a screen."""
        grace = self._settings().get("startup_grace_seconds", 60)
        if not self._seconds(grace):
            grace = 60
        self._started_mono = self._monotonic()
        self._startup_until = self._started_mono + grace
        self._completion_mono = {}
        self._scheduler_failed = False
        self._interrupted_plugins = [plugin for plugin in self.LOCATION_PLUGINS
                                     if self._timing(self._settings(), plugin).get("outcome") == "running"]

    @staticmethod
    def _seconds(value):
        try:
            return (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value > 0)
        except OverflowError:
            return False

    def _interval(self, settings, plugin_id):
        groups = settings.get("groups", {})
        group = groups.get(plugin_id) if isinstance(groups, dict) else None
        if plugin_id not in self.LOCATION_PLUGINS or not isinstance(group, dict):
            return None
        interval = group.get("interval_seconds", 900)
        return interval if self._seconds(interval) else None

    @staticmethod
    def _timing(settings, plugin_id):
        timings = settings.get("refresh_state", {})
        value = timings.get(plugin_id, {}) if isinstance(timings, dict) else {}
        return dict(value) if isinstance(value, dict) else {}

    def _utc_stamp(self):
        return self._utcnow().astimezone(timezone.utc).isoformat()

    @staticmethod
    def _auto_enabled(settings, plugin_id):
        groups = settings.get("groups", {})
        group = groups.get(plugin_id) if isinstance(groups, dict) else None
        return isinstance(group, dict) and group.get("auto_refresh_enabled") is True

    @property
    def resource_paused(self):
        # Only absence/null represents no persisted pause. Malformed values fail
        # closed until an operator removes them through the configuration API.
        return (self._resource_pause_latched or bool(self._interrupted_plugins)
                or self._settings().get("resource_pause") is not None)

    def pause_automatic(self, reason, details=None):
        """Latch first evidence; never stop active rendering/display or clear flags.

        A failed write still closes admission for this object. No guarantee is
        possible when storage fails persistently or the kernel kills Python.
        """
        if not self.enabled:
            return
        self._resource_pause_latched = True
        if self._settings().get("resource_pause") is not None:
            return
        allowed = {"memory_current", "memory_peak", "memory_max", "max", "oom",
                   "oom_kill", "oom_group_kill", "returncode", "memory_peak_baseline",
                   "max_delta", "oom_delta", "oom_kill_delta", "oom_group_kill_delta"}
        evidence = {key: value for key, value in (details or {}).items()
                    if key in allowed and type(value) is int}
        # Call sites supply fixed codes, never exception text, commands or URLs.
        codes = {"memory_critical", "memory_limit", "oom_evidence", "memory_allocation",
                 "chromium_timeout", "chromium_exit", "chromium_launch_failure",
                 "chromium_output_failure", "chromium_cleanup_failure", "chromium_unavailable",
                 "worker_terminated", "interrupted_attempt"}
        reason = reason if reason in codes else "worker_terminated"
        if self._resource_pause_record is None:
            pause = dict(reason=reason, recorded_at=self._utc_stamp(), details=evidence)
            reference = self._reference(self._active) or self._reference(self._settings().get("selected"))
            if reference is not None:
                pause["reference"] = dict(reference)
            self._resource_pause_record = pause
        try:
            self.device_config.update_section("button_navigation",
                                              self._retain_resource_pause)
        except Exception:
            logger.exception("Could not persist automatic resource pause; automatic work latched off in memory")
        else:
            logger.error("Automatic refresh resource pause persisted: %s", self._resource_pause_record["reason"])

    def _retain_resource_pause(self, settings):
        if self._resource_pause_record is not None and settings.get("resource_pause") is None:
            settings["resource_pause"] = deepcopy(self._resource_pause_record)

    def recover_interrupted_attempt(self):
        """Startup only: an unfinished durable attempt has an unknown cause."""
        if self._interrupted_plugins:
            self.pause_automatic("interrupted_attempt")

    def automatic_target(self):
        """Worker-only due check; no queues, network, file writes, or wall-clock polling."""
        settings = self._settings()
        if (settings.get("enabled") is not True or settings.get("manual_only") is True
                or settings.get("displaced") is True or settings.get("display_uncertain") is True
                or self.resource_paused or self._scheduler_failed or self.has_pending):
            return None
        reference = self._reference(settings.get("selected"))
        if reference is None:
            return None
        plugin_id = reference["plugin_id"]
        interval = self._interval(settings, plugin_id)
        resolved = self.resolve(reference)
        if interval is None or resolved is None or not self._auto_enabled(settings, plugin_id):
            return None
        now = self._monotonic()
        if plugin_id not in self._completion_mono:
            # UTC is used only once per plugin on restart. Missing, malformed,
            # future, or acknowledged-interruption timing waits a full interval.
            anchor = self._started_mono
            state = self._timing(settings, plugin_id)
            try:
                completed = datetime.fromisoformat(state["attempt_completed_at"])
                if completed.tzinfo is not None and state.get("outcome") != "running":
                    elapsed = min(interval, max(0, (self._utcnow() - completed).total_seconds()))
                    anchor = now - elapsed
            except (KeyError, ValueError, TypeError, OverflowError):
                pass
            self._completion_mono[plugin_id] = anchor
        if now < self._startup_until or now - self._completion_mono[plugin_id] < interval:
            return None
        return resolved

    def begin_attempt(self, reference):
        """Durably mark opt-in automatic-mode work before generation, including buttons.

        Manual-only/legacy operation retains its existing writes and semantics.
        The journal is per plugin, with full references; it never selects a target.
        """
        self._attempt = None
        settings = self._settings()
        reference = self._reference(reference)
        if (settings.get("enabled") is not True or settings.get("manual_only") is True
                or reference is None or self.resource_paused
                or not self._auto_enabled(settings, reference["plugin_id"])
                or self._interval(settings, reference["plugin_id"]) is None):
            return
        plugin_id = reference["plugin_id"]
        self._attempt = dict(reference=reference, plugin_id=plugin_id)
        started = self._utc_stamp()
        def edit(settings):
            state = self._timing(settings, plugin_id)
            state.update(attempt_reference=dict(reference), attempt_started_at=started,
                         attempt_completed_at=None, outcome="running", data_status=None)
            self._put_timing(settings, plugin_id, state)
        try:
            self.device_config.update_section("button_navigation", edit)
        except Exception:
            self._scheduler_failed = True
            self._completion_mono[plugin_id] = self._monotonic()
            self._attempt = None
            raise

    @staticmethod
    def _put_timing(settings, plugin_id, state):
        if not isinstance(settings.get("refresh_state"), dict):
            settings["refresh_state"] = {}
        settings["refresh_state"][plugin_id] = state

    def _complete_timing(self, settings, outcome, data_status=None, live_data=False):
        self._retain_resource_pause(settings)
        attempt = self._attempt
        state = self._timing(settings, attempt["plugin_id"])
        completed = self._utc_stamp()
        state.update(attempt_completed_at=completed, outcome=outcome, data_status=data_status)
        if outcome in ("presented", "unchanged"):
            state.update(presentation_success_at=completed,
                         presentation_reference=dict(attempt["reference"]))
            if live_data:
                state["live_data_success_at"] = completed
                state["live_data_reference"] = dict(attempt["reference"])
        self._put_timing(settings, attempt["plugin_id"], state)

    def finish_attempt(self, outcome="failed"):
        """Failure/cancellation completes cadence once; storage failure pauses auto work."""
        if self._attempt is None:
            # One bounded retry at the end of deliberate/manual-only work too;
            # a transient write failure must not vanish after a later success.
            if self._resource_pause_record is not None and self._settings().get("resource_pause") is None:
                self.device_config.update_section("button_navigation", self._retain_resource_pause)
            return
        attempt = self._attempt
        self._completion_mono[attempt["plugin_id"]] = self._monotonic()
        try:
            self.device_config.update_section("button_navigation",
                lambda settings: self._complete_timing(settings, outcome))
        except Exception:
            self._scheduler_failed = True
            raise
        finally:
            self._completion_mono[attempt["plugin_id"]] = self._monotonic()
            self._attempt = None

    def _settings(self):
        settings = self.device_config.get_config("button_navigation", default={})
        return settings if isinstance(settings, dict) else {}

    @property
    def enabled(self):
        return self._settings().get("enabled") is True

    @property
    def manual_only(self):
        """Suppress scheduled selection updates; button requests still use the worker."""
        return self._settings().get("manual_only") is True

    @property
    def displaced(self):
        return self._settings().get("displaced") is True

    @property
    def display_uncertain(self):
        return self._settings().get("display_uncertain") is True

    def begin_refresh(self, reference, web=False, selection=False):
        """Worker-only bookkeeping; no rendering or persistence under this lock."""
        with self._lock:
            self._active = reference
            self._web_inflight = web
            if selection:
                self._selection_active = reference

    def end_refresh(self):
        with self._lock:
            self._active = None
            self._web_inflight = False
            self._selection_active = None

    def mark_display_uncertain(self):
        """Worker-only durable marker. Failure must abort before display IO."""
        self.device_config.update_section(
            "button_navigation", lambda settings: settings.update(display_uncertain=True))

    def record_display_success(self, reference, web, image_changed, selection=False,
                               refresh_info=None, instance=None, refreshed_at=None,
                               physical_display=True, data_status=None, live_data=False):
        """Commit navigation and refresh metadata only after known presentation."""
        def edit(settings):
            self._retain_resource_pause(settings)
            if self._attempt is not None:
                self._complete_timing(settings, "presented" if physical_display else "unchanged",
                                      data_status, live_data)
            if settings.get("enabled") is not True:
                if "display_uncertain" in settings:
                    settings["display_uncertain"] = False
                return
            if selection and self._reference(reference):
                settings["selected"] = dict(reference)
                if reference in self._group_references(settings, reference["plugin_id"]):
                    remembered = settings.get("remembered")
                    if not isinstance(remembered, dict):
                        remembered = settings["remembered"] = {}
                    remembered[reference["plugin_id"]] = dict(reference)
            selected = settings.get("selected")
            if selected and reference == selected:
                settings["displaced"] = False
            elif selected and web and image_changed:
                settings["displaced"] = True
            settings["display_uncertain"] = False
        navigation_edit = edit if self.enabled or self.display_uncertain else None
        if refresh_info is not None:
            self.device_config.commit_refresh(refresh_info, instance, refreshed_at, navigation_edit)
        elif navigation_edit:
            self.device_config.update_section("button_navigation", navigation_edit)
        if self._attempt is not None:
            self._completion_mono[self._attempt["plugin_id"]] = self._monotonic()
            self._attempt = None
        return True

    @property
    def has_pending(self):
        with self._lock:
            return self._pending is not None

    def discard_pending(self):
        """Shutdown discards unconsumed intent; it is never replayed on restart."""
        with self._lock:
            self._pending = None

    def resolve(self, reference):
        """Use the application's existing playlist/plugin/name identity."""
        if not isinstance(reference, dict):
            return None
        fields = ("playlist", "plugin_id", "instance")
        if not all(isinstance(reference.get(k), str) and reference[k] for k in fields):
            return None
        playlist = self.device_config.get_playlist_manager().get_playlist(reference["playlist"])
        instance = playlist.find_plugin(reference["plugin_id"], reference["instance"]) if playlist else None
        plugin = self.device_config.get_plugin(reference["plugin_id"])
        if not instance or not plugin or plugin.get("disabled", False):
            return None
        return playlist, instance

    def assign_button(self, button, reference):
        """Configuration API; call outside GPIO callbacks (writes device.json)."""
        if button not in self.BUTTONS:
            raise ValueError("Button must be A, B, C, or D")
        if reference is not None and not self.resolve(reference):
            raise ValueError("Assignment must reference an existing enabled plugin instance")
        def edit(settings):
            assignments = settings.get("assignments")
            if not isinstance(assignments, dict):
                assignments = settings["assignments"] = {}
            if reference is None:
                assignments.pop(button, None)
            else:
                assignments[button] = {k: reference[k] for k in ("playlist", "plugin_id", "instance")}
        self.device_config.update_section("button_navigation", edit)

    def request_button(self, button):
        """Bounded callback API. Latest accepted request replaces the pending one."""
        if button not in self.BUTTONS:
            return False
        # Choose the next location and replace the mailbox in one transaction:
        # concurrent C presses must advance from each other's pending target.
        with self._lock:
            settings = self._settings()
            actions = settings.get("actions", {})
            if button == "C" and isinstance(actions, dict) and actions.get("C") == "cycle_active_plugin":
                reference = self._cycle_target(settings)
            else:
                assignments = settings.get("assignments", {})
                reference = assignments.get(button) if isinstance(assignments, dict) else None
                if button in ("A", "B"):
                    reference = self._remembered_target(settings, reference)
            accepted = self._queue_selection(reference, settings)
        if accepted:
            self._wake()
        return accepted

    @staticmethod
    def _reference(reference):
        """Canonical identity; ignore unrelated caller-owned dictionary fields."""
        fields = ("playlist", "plugin_id", "instance")
        if not isinstance(reference, dict) or not all(
                isinstance(reference.get(key), str) and reference[key] for key in fields):
            return None
        return {key: reference[key] for key in fields}

    def _group_references(self, settings, plugin_id):
        groups = settings.get("groups", {})
        group = groups.get(plugin_id) if isinstance(groups, dict) else None
        if plugin_id not in self.LOCATION_PLUGINS or not isinstance(group, dict):
            return []
        instances = group.get("instances", [])
        if not isinstance(instances, list):
            return []
        references = []
        for item in instances:
            reference = self._reference(item)
            if (reference and reference["plugin_id"] == plugin_id
                    and reference not in references and self.resolve(reference)):
                references.append(reference)
        return references

    def _remembered_target(self, settings, assignment):
        """Opt in per assigned plugin; legacy/unassigned buttons stay unchanged."""
        assignment = self._reference(assignment)
        if assignment is None:
            return None
        references = self._group_references(settings, assignment["plugin_id"])
        if not references:
            return assignment
        remembered = settings.get("remembered", {})
        remembered = remembered.get(assignment["plugin_id"]) if isinstance(remembered, dict) else None
        for candidate in (self._pending, self._consuming, self._selection_active,
                          remembered, settings.get("selected")):
            candidate = self._reference(candidate)
            if candidate in references:
                return candidate
        return assignment if self.resolve(assignment) else references[0]

    def _cycle_target(self, settings):
        # A web render's _active reference must not change the selected context.
        current = self._reference(self._pending or self._consuming or
                                  self._selection_active or settings.get("selected"))
        if current is None:
            return None
        references = self._group_references(settings, current["plugin_id"])
        if len(references) <= 1:
            return None
        if current not in references:
            return references[0]
        return references[(references.index(current) + 1) % len(references)]

    def _queue_selection(self, reference, settings):
        """Caller holds _lock; callbacks never render, persist, or wake under it."""
        reference = self._reference(reference)
        if settings.get("enabled") is not True or not self.resolve(reference):
            return False
        if self._pending == reference:
            return False
        if self._pending is not None and reference == self._selection_active:
            # A full cycle cancels superseded pending work, not a new attempt of
            # the active target if that attempt subsequently fails.
            self._pending = None
            return True
        if self._pending is None:
            if self._consuming == reference or (self._active == reference
                    and self._consuming is None and not self._web_inflight):
                return False
            if (self._consuming is None and settings.get("selected") == reference
                    and self._selection_active is None
                    and settings.get("displaced") is not True
                    and settings.get("display_uncertain") is not True and not self._web_inflight):
                return False
        self._pending = reference
        return True

    def request_selection(self, reference):
        with self._lock:
            accepted = self._queue_selection(reference, self._settings())
        if accepted:
            self._wake()
        return accepted

    def consume_pending(self):
        """Worker-only: move a valid target into volatile in-flight state."""
        with self._lock:
            reference, self._pending = self._pending, None
            self._consuming = reference
        try:
            if not self.enabled or reference is None:
                return None
            resolved = self.resolve(reference)
            if not resolved:
                logger.warning("Pending screen instance was removed or disabled: %s", reference)
                return None
            settings = self._settings()
            changed = settings.get("selected") != reference
            restore = (settings.get("display_uncertain") is True or
                       (not changed and settings.get("displaced") is True))
            if changed or restore:
                self.begin_refresh(reference, selection=True)
            return (*resolved, changed or restore, restore)
        finally:
            with self._lock:
                self._consuming = None

    def selected(self):
        if not self.enabled:
            return None
        reference = self._settings().get("selected")
        resolved = self.resolve(reference)
        if reference is not None and not resolved:
            logger.warning("Selected screen instance was removed or disabled: %s", reference)
        return resolved
