"""Read-only resource evidence and worker-scoped screenshot notifications.

No polling thread, service control, hardware access, or network requests. The
refresh worker owns pause persistence; screenshot helpers only call its hooks.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
_watch = ContextVar("inkypi_resource_watch", default=None)


@contextmanager
def watch_resources(check, failure):
    token = _watch.set((check, failure))
    try:
        yield
    finally:
        _watch.reset(token)


def resource_checkpoint():
    watch = _watch.get()
    if watch is not None:
        watch[0]()


def report_resource_failure(reason, **details):
    watch = _watch.get()
    if watch is not None:
        watch[1](reason, details)


class CgroupResourceMonitor:
    """Observe our own non-root cgroup v2, including browser descendants.

    New kernel peaks catch spikes between checkpoints. The first valid sample
    establishes a lifetime-accounting baseline; historical counters/peaks are not
    new failures. Kernel accounting is never reset by this monitor.
    Unsupported/missing interfaces are unknown, not evidence of healthy memory.
    """

    def __init__(self, *, membership=Path("/proc/self/cgroup"),
                 root=Path("/sys/fs/cgroup")):
        self.path = None
        self._unavailable_logged = False
        self._warning_logged = False
        self._previous = None
        try:
            for line in membership.read_text().splitlines():
                hierarchy, controllers, name = line.split(":", 2)
                relative = Path(name.lstrip("/"))
                if (hierarchy == "0" and not controllers and name != "/"
                        and ".." not in relative.parts):
                    self.path = root / relative
                    break
        except (OSError, ValueError):
            pass

    def check(self):
        if self.path is None:
            self._unavailable()
            return None
        try:
            current = int((self.path / "memory.current").read_text())
            peak = int((self.path / "memory.peak").read_text())
            maximum = (self.path / "memory.max").read_text().strip()
            maximum = int(maximum) if maximum != "max" else None
            events = {k: int(v) for k, v in
                      (line.split() for line in (self.path / "memory.events").read_text().splitlines())}
            if not {"max", "oom", "oom_kill"}.issubset(events):
                raise ValueError("Incomplete cgroup accounting")
            if min(current, peak, *events.values()) < 0 or (maximum is not None and maximum <= 0):
                raise ValueError("Invalid cgroup accounting")
        except (OSError, ValueError):
            self._unavailable()
            return None
        details = dict(memory_current=current, memory_peak=peak)
        if maximum is not None:
            details["memory_max"] = maximum
        keys = ("max", "oom", "oom_kill", "oom_group_kill")
        previous = self._previous
        # A recreated/reset cgroup starts a new epoch. Never subtract unrelated
        # lifetime counters or mistake a decrease for a new failure.
        reset = previous is not None and (peak < previous["peak"] or any(
            events.get(key, 0) < previous["events"].get(key, 0) for key in keys))
        if reset:
            previous = None
        self._previous = dict(peak=peak, events=dict(events))
        deltas = {key: max(0, events.get(key, 0) - previous["events"].get(key, 0))
                  if previous is not None else 0 for key in keys}
        new_peak = previous is not None and peak > previous["peak"]
        details["memory_peak_baseline"] = previous["peak"] if previous else peak
        for key in keys:
            details[key] = events.get(key, 0)
            details[key + "_delta"] = deltas[key]
        if any(deltas[k] > 0 for k in ("oom", "oom_kill", "oom_group_kill")):
            return "oom_evidence", details
        if deltas["max"] > 0:
            return "memory_limit", details
        observed = max(current, peak if new_peak else current)
        if maximum is not None and observed >= maximum * 0.95:
            return "memory_critical", details
        if maximum is not None and observed > maximum * 0.90 and not self._warning_logged:
            self._warning_logged = True
            logger.warning("Cgroup memory exceeded 90% of memory.max; active display IO is not interrupted")
        return None

    def _unavailable(self):
        if not self._unavailable_logged:
            self._unavailable_logged = True
            logger.warning("Cgroup v2 memory monitoring unavailable; memory pressure cannot be certified")
