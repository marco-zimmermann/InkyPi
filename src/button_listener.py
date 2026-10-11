"""Optional gpiod v2 input adapter; never renders or imports display drivers."""
import importlib
import logging
import threading
import time

logger = logging.getLogger(__name__)


class ButtonDebouncer:
    """Accept stable low once, then require stable high before another press."""

    def __init__(self, pins, debounce_seconds=0.075):
        self.pins = pins
        self.delay = debounce_seconds
        self.levels = {}
        self.candidates = {}
        self.armed = {}

    def initialize(self, levels):
        self.levels = dict(levels)
        self.candidates = {pin: (level, None) for pin, level in levels.items()}
        # A button held when the listener starts is not a new press.
        self.armed = {pin: bool(level) for pin, level in levels.items()}

    def edge(self, pin, high, timestamp):
        # Event timestamps preserve stable presses even if a batch is read late.
        presses = self.settle(timestamp)
        if pin in self.pins:
            self.candidates[pin] = (high, timestamp)
        return presses

    def settle(self, now):
        presses = []
        candidates = sorted(self.candidates.items(),
                            key=lambda item: item[1][1] if item[1][1] is not None else float("inf"))
        for pin, (high, timestamp) in candidates:
            if timestamp is None or now - timestamp < self.delay:
                continue
            self.candidates[pin] = (high, None)
            self.levels[pin] = high
            if high:
                self.armed[pin] = True
            elif self.armed[pin]:
                self.armed[pin] = False
                presses.append(self.pins[pin])
        return presses


class EventSequenceTracker:
    """Per-line loss is fatal; anomalous request counters remain diagnostic.

    Pi multi-line IRQ counters can skip/repeat while emitted line counters stay
    contiguous. Never use the global counter alone to discard valid input.
    Kernel counters are unsigned 32-bit and reset for each new request.
    """

    def __init__(self):
        self.global_sequence = 0
        self.line_sequences = {}
        self.global_anomalies = 0

    def observe(self, event):
        expected_global = (self.global_sequence + 1) & 0xffffffff
        expected_line = (self.line_sequences.get(event.line_offset, 0) + 1) & 0xffffffff
        if event.line_seqno != expected_line:
            raise RuntimeError(
                f'GPIO event sequence gap: GPIO{event.line_offset} '
                f'global expected={expected_global} received={event.global_seqno}; '
                f'line expected={expected_line} received={event.line_seqno}; '
                f'type={event.event_type} timestamp_ns={event.timestamp_ns}; '
                'refusing uncertain presses')
        if event.global_seqno != expected_global:
            self.global_anomalies += 1
            # Keep a faulty/bouncing counter from flooding the journal.
            log = logger.warning if self.global_anomalies <= 5 else logger.debug
            log('GPIO global counter anomaly #%s: GPIO%s expected=%s received=%s '
                'line=%s type=%s timestamp_ns=%s; per-line sequence valid',
                self.global_anomalies, event.line_offset, expected_global,
                event.global_seqno, event.line_seqno, event.event_type, event.timestamp_ns)
        self.global_sequence = event.global_seqno
        self.line_sequences[event.line_offset] = event.line_seqno


class ButtonListener:
    """One small input thread, with a bounded kernel event buffer.

    Both edges are requested so release is debounced as well as falling presses.
    gpiod is imported only when button mode is enabled. Initialization/runtime
    failures release lines and latch until mode is disabled and enabled again.
    """

    PINS = {5: 'A', 6: 'B', 16: 'C', 24: 'D'}
    POLL_SECONDS = 0.02

    def __init__(self, controller, gpio_module=None, clock=time.monotonic):
        self.controller = controller
        self._gpio = gpio_module
        self._clock = clock
        self._stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self.thread = None

    def start(self):
        with self._lifecycle_lock:
            if self.thread and self.thread.is_alive():
                return
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, name='inkypi-buttons', daemon=True)
            self.thread.start()

    def stop(self):
        with self._lifecycle_lock:
            self._stop.set()
            thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join()

    def _open(self):
        gpio = self._gpio or importlib.import_module('gpiod')
        # Fixed BCM mapping for the verified Pi Zero 2 W / Inky Impression.
        settings = gpio.LineSettings(direction=gpio.line.Direction.INPUT,
                                     edge_detection=gpio.line.Edge.BOTH,
                                     bias=gpio.line.Bias.PULL_UP,
                                     event_clock=gpio.line.Clock.MONOTONIC)
        request = gpio.request_lines('/dev/gpiochip0', consumer='inkypi-buttons',
                                     config={tuple(self.PINS): settings}, event_buffer_size=64)
        try:
            values = request.get_values(list(self.PINS))
            debouncer = ButtonDebouncer(self.PINS)
            # active_low is false: INACTIVE is electrical low, ACTIVE is high.
            debouncer.initialize({pin: value == gpio.line.Value.ACTIVE
                                  for pin, value in zip(self.PINS, values)})
            return gpio, request, debouncer
        except Exception:
            self._release(request)
            raise

    @staticmethod
    def _release(request):
        try:
            request.release()
        except Exception:
            logger.exception('Failed to release GPIO request')

    def _run(self):
        request = None
        failed = False
        sequence = None
        try:
            while not self._stop.is_set():
                try:
                    if not self.controller.enabled:
                        if request is not None:
                            self._release(request)
                            request = None
                        failed = False
                        sequence = None
                        self._stop.wait(self.POLL_SECONDS)
                        continue
                    if failed:
                        self._stop.wait(self.POLL_SECONDS)
                        continue
                    if request is None:
                        gpio, request, debouncer = self._open()
                        sequence = EventSequenceTracker()
                    presses = []
                    if request.wait_edge_events(timeout=self.POLL_SECONDS):
                        events = request.read_edge_events(max_events=64)
                        for event in events:
                            logger.debug('GPIO%s type=%s global=%s line=%s timestamp_ns=%s',
                                         event.line_offset, event.event_type, event.global_seqno,
                                         event.line_seqno, event.timestamp_ns)
                            sequence.observe(event)
                            presses.extend(debouncer.edge(
                                event.line_offset,
                                event.event_type == gpio.EdgeEvent.Type.RISING_EDGE,
                                event.timestamp_ns / 1_000_000_000))
                    # Do not settle at wall time until queued edges are drained.
                    if not request.wait_edge_events(timeout=0):
                        presses.extend(debouncer.settle(self._clock()))
                    if self._stop.is_set() or not self.controller.enabled:
                        continue
                    for button in presses:
                        self.controller.request_button(button)
                except Exception:
                    logger.exception('Button listener unavailable; toggle button mode or restart to retry')
                    failed = True
                    if request is not None:
                        self._release(request)
                        request = None
        finally:
            if request is not None:
                self._release(request)
