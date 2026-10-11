"""GPIO-independent selection of existing saved plugin instances.

Only the refresh worker consumes selections and persists state. Event callbacks
publish into a bounded mailbox; they never render or write files. Configuration
synchronization can briefly delay callbacks; this is not a hard real-time API.
"""
import logging
import threading

logger = logging.getLogger(__name__)


class ScreenSelectionController:
    BUTTONS = ("A", "B", "C", "D")

    def __init__(self, device_config, wake):
        self.device_config = device_config
        self._wake = wake
        self._lock = threading.Lock()
        self._pending = None
        self._consuming = None
        self._active = None
        self._web_inflight = False

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

    def begin_refresh(self, reference, web=False):
        """Worker-only bookkeeping; no rendering or persistence under this lock."""
        with self._lock:
            self._active = reference
            self._web_inflight = web

    def end_refresh(self):
        with self._lock:
            self._active = None
            self._web_inflight = False

    def record_display_success(self, reference, web, image_changed):
        """Persist displacement only after successful display IO (or dedup)."""
        if not self.enabled or not self._settings().get("selected"):
            return
        def edit(settings):
            selected = settings.get("selected")
            if reference == selected:
                settings["displaced"] = False
            elif web and image_changed:
                settings["displaced"] = True
        self.device_config.update_section("button_navigation", edit)
        return True

    @property
    def has_pending(self):
        with self._lock:
            return self._pending is not None

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
        assignments = self._settings().get("assignments", {})
        reference = assignments.get(button) if isinstance(assignments, dict) else None
        return self.request_selection(reference) if button in self.BUTTONS else False

    def request_selection(self, reference):
        if not self.enabled or not self.resolve(reference):
            return False
        reference = {k: reference[k] for k in ("playlist", "plugin_id", "instance")}
        with self._lock:
            if self._pending == reference:
                return False
            if self._pending is None:
                if self._consuming == reference or (self._active == reference and self._consuming is None):
                    return False
                if (self._consuming is None and self._settings().get("selected") == reference
                        and not self.displaced and not self._web_inflight):
                    return False
            self._pending = reference
        self._wake()
        return True

    def consume_pending(self):
        """Worker-only: persist a valid request without replacing unrelated settings."""
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
            changed = []
            restore = []
            def edit(settings):
                changed.append(settings.get("selected") != reference)
                restore.append(settings.get("displaced") is True)
                settings["selected"] = reference
            self.device_config.update_section("button_navigation", edit)
            if changed[0] or restore[0]:
                self.begin_refresh(reference)
            return (*resolved, changed[0] or restore[0], restore[0])
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
