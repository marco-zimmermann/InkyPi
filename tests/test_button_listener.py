import threading
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from button_listener import ButtonDebouncer, ButtonListener, EventSequenceTracker


@pytest.fixture
def debouncer():
    debounce = ButtonDebouncer(ButtonListener.PINS)
    debounce.initialize({pin: True for pin in ButtonListener.PINS})
    return debounce


def test_press_requires_stable_low(debouncer):
    debouncer.edge(5, False, 1.0)
    assert debouncer.settle(1.074) == []
    assert debouncer.settle(1.076) == ['A']
    assert debouncer.settle(2.0) == []


def test_bounce_and_release_required(debouncer):
    for timestamp, high in [(1.0, False), (1.02, True), (1.03, False)]:
        debouncer.edge(5, high, timestamp)
    assert debouncer.settle(1.08) == []
    assert debouncer.settle(1.11) == ['A']
    # A brief release bounce must not rearm a held button.
    debouncer.edge(5, True, 1.2)
    debouncer.edge(5, False, 1.22)
    assert debouncer.settle(1.3) == []
    debouncer.edge(5, True, 1.4)
    assert debouncer.settle(1.48) == []
    debouncer.edge(5, False, 1.5)
    assert debouncer.settle(1.58) == ['A']


def test_short_press_is_filtered(debouncer):
    debouncer.edge(5, False, 1.0)
    debouncer.edge(5, True, 1.02)
    assert debouncer.settle(1.1) == []


def test_rapid_distinct_presses(debouncer):
    for index in range(5):
        now = index * 0.2 + 1
        debouncer.edge(5, False, now)
        assert debouncer.settle(now + 0.08) == ['A']
        debouncer.edge(5, True, now + 0.1)
        assert debouncer.settle(now + 0.18) == []


def test_simultaneous_buttons_independent(debouncer):
    for pin in ButtonListener.PINS:
        debouncer.edge(pin, False, 1.0)
    assert debouncer.settle(1.08) == ['A', 'B', 'C', 'D']


def test_start_held_requires_release(debouncer):
    debouncer.initialize({pin: False for pin in ButtonListener.PINS})
    debouncer.edge(5, False, 1.0)
    assert debouncer.settle(1.1) == []
    debouncer.edge(5, True, 1.2)
    debouncer.settle(1.3)
    debouncer.edge(5, False, 1.4)
    assert debouncer.settle(1.5) == ['A']


class FakeRequest:
    def __init__(self):
        self.events = deque()
        self.ready = threading.Event()
        self.released = threading.Event()
        self.failure = None
        self.line_sequences = {}

    def get_values(self, pins):
        return ['high'] * len(pins)

    def wait_edge_events(self, timeout):
        if self.failure:
            raise self.failure
        return self.ready.wait(timeout)

    def read_edge_events(self, max_events):
        events = [self.events.popleft() for _ in range(min(max_events, len(self.events)))]
        if not self.events:
            self.ready.clear()
        return events

    def release(self):
        self.released.set()

    def edge(self, pin, high, sequence):
        self.line_sequences[pin] = self.line_sequences.get(pin, 0) + 1
        self.events.append(SimpleNamespace(line_offset=pin, event_type='rise' if high else 'fall',
                                          global_seqno=sequence, line_seqno=self.line_sequences[pin],
                                          timestamp_ns=time.monotonic_ns()))
        self.ready.set()


def gpio_module(request):
    return SimpleNamespace(
        line=SimpleNamespace(Direction=SimpleNamespace(INPUT='input'), Edge=SimpleNamespace(BOTH='both'),
                             Bias=SimpleNamespace(PULL_UP='pull-up'), Clock=SimpleNamespace(MONOTONIC='monotonic'),
                             Value=SimpleNamespace(ACTIVE='high')),
        EdgeEvent=SimpleNamespace(Type=SimpleNamespace(RISING_EDGE='rise')),
        LineSettings=Mock(), request_lines=Mock(return_value=request))


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, 'listener did not reach expected state'
        threading.Event().wait(0.005)


def test_gpio_api_and_shutdown_restart():
    request = FakeRequest()
    gpio = gpio_module(request)
    controller = Mock(enabled=True)
    pressed = threading.Event()
    controller.request_button.side_effect = lambda button: pressed.set()
    listener = ButtonListener(controller, gpio)
    listener.start()
    listener.start()
    try:
        wait_until(lambda: gpio.request_lines.call_count == 1)
        request.edge(6, False, 1)
        assert pressed.wait(2)
        controller.request_button.assert_called_once_with('B')
        settings = gpio.LineSettings.call_args.kwargs
        assert settings == dict(direction='input', edge_detection='both', bias='pull-up', event_clock='monotonic')
    finally:
        listener.stop()
    assert request.released.is_set()
    assert not listener.thread.is_alive()
    request2 = FakeRequest()
    gpio.request_lines.return_value = request2
    listener.start()
    try:
        wait_until(lambda: gpio.request_lines.call_count == 2)
    finally:
        listener.stop()
    assert request2.released.is_set()


def test_disabled_does_not_import_or_open_gpio(monkeypatch):
    importer = Mock(side_effect=AssertionError('GPIO import forbidden'))
    monkeypatch.setattr('button_listener.importlib.import_module', importer)
    controller = Mock(enabled=False)
    listener = ButtonListener(controller)
    listener.start()
    listener.stop()
    importer.assert_not_called()


def test_runtime_disable_releases_and_reenable_reopens():
    request = FakeRequest()
    gpio = gpio_module(request)
    controller = Mock(enabled=True)
    listener = ButtonListener(controller, gpio)
    listener.start()
    try:
        wait_until(lambda: gpio.request_lines.call_count == 1)
        controller.enabled = False
        assert request.released.wait(2)
        request2 = FakeRequest()
        gpio.request_lines.return_value = request2
        controller.enabled = True
        wait_until(lambda: gpio.request_lines.call_count == 2)
    finally:
        listener.stop()
    assert request2.released.is_set()


@pytest.mark.parametrize('error', [PermissionError('denied'), OSError('busy'), ImportError('missing')])
def test_initialization_failure_latches_and_recovers(error):
    request = FakeRequest()
    gpio = gpio_module(request)
    gpio.request_lines.side_effect = error
    controller = Mock(enabled=True)
    listener = ButtonListener(controller, gpio)
    listener.start()
    try:
        wait_until(lambda: gpio.request_lines.call_count == 1)
        threading.Event().wait(0.06)
        assert gpio.request_lines.call_count == 1
        assert listener.thread.is_alive()
        controller.enabled = False
        threading.Event().wait(0.06)
        gpio.request_lines.side_effect = None
        controller.enabled = True
        wait_until(lambda: gpio.request_lines.call_count == 2)
    finally:
        listener.stop()
    assert request.released.is_set()


def test_event_loss_releases_lines_without_selection():
    request = FakeRequest()
    request.edge(5, False, 1)
    request.line_sequences[5] = 2
    request.edge(5, False, 3)
    gpio = gpio_module(request)
    controller = Mock(enabled=True)
    listener = ButtonListener(controller, gpio)
    listener.start()
    try:
        assert request.released.wait(2)
        controller.request_button.assert_not_called()
    finally:
        listener.stop()


def test_partial_initialization_releases_request():
    request = FakeRequest()
    request.get_values = Mock(side_effect=OSError('read failed'))
    gpio = gpio_module(request)
    listener = ButtonListener(Mock(enabled=True), gpio)
    listener.start()
    try:
        assert request.released.wait(2)
    finally:
        listener.stop()


def test_delayed_batch_preserves_press_and_release(debouncer):
    assert debouncer.edge(5, False, 1.0) == []
    assert debouncer.edge(5, True, 1.1) == ['A']
    assert debouncer.edge(5, False, 1.2) == []
    assert debouncer.edge(5, True, 1.3) == ['A']


def test_close_presses_follow_event_order(debouncer):
    debouncer.edge(24, False, 1.0)
    debouncer.edge(5, False, 1.01)
    assert debouncer.settle(1.1) == ['D', 'A']


@pytest.mark.parametrize('startup,terminate', [(False, False), (True, False), (False, True)])
def test_application_lifecycle_uses_finally(startup, terminate):
    # Execute only the main lifecycle AST, with every application dependency mocked.
    import ast
    from pathlib import Path
    tree = ast.parse(Path('src/inkypi.py').read_text())
    main = next(node for node in tree.body if isinstance(node, ast.If) and
                isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and
                node.test.left.id == '__name__')
    calls = []
    listener = Mock()
    listener.start.side_effect = lambda: calls.append('listener-start')
    listener.stop.side_effect = lambda: calls.append('listener-stop')
    task = Mock()
    task.start.side_effect = lambda: calls.append('worker-start')
    task.stop.side_effect = lambda: calls.append('worker-stop')
    display = Mock()
    display.display_image.side_effect = lambda *args: calls.append('splash')
    config = Mock()
    config.get_config.return_value = startup
    signal = Mock(SIGTERM=15)
    def serve(*args, **kwargs):
        calls.append('serve')
        if terminate:
            signal.signal.call_args.args[1](15, None)
    namespace = dict(ButtonListener=Mock(return_value=listener), refresh_task=task,
                     signal=signal, device_config=config, display_manager=display,
                     generate_startup_image=Mock(), logger=Mock(), app=SimpleNamespace(),
                     random=Mock(), DEV_MODE=False, PORT=80, serve=serve)
    code = compile(ast.Module(body=main.body, type_ignores=[]), '<mock lifecycle>', 'exec')
    if terminate:
        with pytest.raises(SystemExit):
            exec(code, namespace)
    else:
        exec(code, namespace)
    expected = ['worker-start'] + (['splash'] if startup else []) + ['listener-start', 'serve', 'listener-stop', 'worker-stop']
    assert calls == expected


def test_listener_remains_responsive_during_refresh(tmp_path, monkeypatch):
    from model import Playlist, PlaylistManager, PluginInstance, RefreshInfo
    from refresh_task import RefreshTask
    instances = [PluginInstance('test', n, {'name': n}, {'interval': 3600}) for n in 'ABCD']
    manager = PlaylistManager([Playlist('Default', '00:00', '24:00', [i.to_dict() for i in instances])])
    refs = {n: dict(playlist='Default', plugin_id='test', instance=n) for n in 'ABCD'}
    settings = {'button_navigation': {'enabled': True, 'assignments': refs}, 'plugin_cycle_interval_seconds': 3600}
    config = Mock(plugin_image_dir=str(tmp_path))
    config.get_config.side_effect = lambda key, default=None: settings.get(key, default)
    config.get_playlist_manager.return_value = manager
    config.get_plugin.return_value = {'id': 'test'}
    config.refresh_info = RefreshInfo.from_dict({})
    config.get_refresh_info.side_effect = lambda: config.refresh_info
    config.update_section.side_effect = lambda key, transform: transform(settings[key])
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    names = []
    from PIL import Image
    plugin = Mock(config={})
    def generate(values, cfg):
        names.append(values['name'])
        return Image.new('RGB', (8, 8), 'red' if values['name'] == 'A' else 'blue')
    plugin.generate_image.side_effect = generate
    monkeypatch.setattr('refresh_task.get_plugin_instance', lambda _: plugin)
    def display(*args, **kwargs):
        if len(names) == 1:
            entered.set()
            assert release.wait(3)
        else:
            finished.set()
    task = RefreshTask(config, Mock(display_image=display))
    request = FakeRequest()
    listener = ButtonListener(task.screen_selection, gpio_module(request))
    task.start()
    listener.start()
    try:
        request.edge(5, False, 1)
        assert entered.wait(2)
        for seq, pin in enumerate([6, 16, 24], 2):
            request.edge(pin, False, seq)
        wait_until(lambda: task.screen_selection._pending == refs['D'])
        assert not release.is_set()
        release.set()
        assert finished.wait(2)
    finally:
        release.set()
        listener.stop()
        task.stop()
    assert names == ['A', 'D']


def gpio_event(pin, global_sequence, line_sequence, high=False, timestamp=1.0):
    return SimpleNamespace(line_offset=pin, global_seqno=global_sequence,
                           line_seqno=line_sequence, event_type='rise' if high else 'fall',
                           timestamp_ns=int(timestamp * 1_000_000_000))


def test_global_and_line_sequence_interleaving():
    tracker = EventSequenceTracker()
    for event in [gpio_event(5, 1, 1), gpio_event(6, 2, 1),
                  gpio_event(5, 3, 2, True), gpio_event(24, 4, 1),
                  gpio_event(6, 5, 2, True)]:
        tracker.observe(event)
    assert tracker.global_sequence == 5
    assert tracker.line_sequences == {5: 2, 6: 2, 24: 1}


def test_observed_first_sequence_five_is_not_a_valid_start():
    tracker = EventSequenceTracker()
    with pytest.raises(RuntimeError, match='line expected=1 received=5'):
        tracker.observe(gpio_event(5, 5, 5))


def test_observed_five_to_seven_gap_remains_an_error():
    tracker = EventSequenceTracker()
    for index in range(1, 6):
        tracker.observe(gpio_event(5, index, index))
    with pytest.raises(RuntimeError, match='line expected=6 received=7'):
        tracker.observe(gpio_event(5, 7, 7))


def test_line_sequence_gap_detected_independently():
    tracker = EventSequenceTracker()
    tracker.observe(gpio_event(5, 1, 1))
    with pytest.raises(RuntimeError, match='line expected=2 received=3'):
        tracker.observe(gpio_event(5, 2, 3, True))


def test_sequence_counter_rollover():
    tracker = EventSequenceTracker()
    tracker.global_sequence = 0xffffffff
    tracker.line_sequences[5] = 0xffffffff
    tracker.observe(gpio_event(5, 0, 0))
    tracker.observe(gpio_event(5, 1, 1))


def test_initial_loss_stops_without_accepting_press():
    request = FakeRequest()
    request.line_sequences[5] = 4
    request.edge(5, False, 5)
    controller = Mock(enabled=True)
    listener = ButtonListener(controller, gpio_module(request))
    listener.start()
    try:
        assert request.released.wait(2)
        controller.request_button.assert_not_called()
    finally:
        listener.stop()
    assert not listener.thread.is_alive()


def test_interleaved_stable_cycles_in_delayed_batch(monkeypatch):
    request = FakeRequest()
    base = time.monotonic() - 2
    stream = [(5, False, 1, 1), (6, False, 2, 1), (5, True, 3, 2),
              (6, True, 4, 2), (5, False, 5, 3), (5, True, 6, 4),
              (16, False, 7, 1), (16, True, 8, 2),
              (24, False, 9, 1), (24, True, 10, 2)]
    for index, (pin, high, global_sequence, line_sequence) in enumerate(stream):
        request.events.append(gpio_event(pin, global_sequence, line_sequence,
                                         high, base + index * 0.1))
    request.ready.set()
    probe = ButtonDebouncer(ButtonListener.PINS)
    monkeypatch.setattr('button_listener.ButtonDebouncer', lambda pins: probe)
    controller = Mock(enabled=True)
    listener = ButtonListener(controller, gpio_module(request))
    listener.start()
    try:
        wait_until(lambda: controller.request_button.call_count == 5)
        wait_until(lambda: all(probe.armed.values()))
    finally:
        listener.stop()
    assert [call.args[0] for call in controller.request_button.call_args_list] == ['A', 'B', 'A', 'C', 'D']
    assert all(probe.levels.values())
    assert request.released.is_set()


def test_read_buffer_boundary_does_not_drop_or_duplicate(monkeypatch):
    # Force 80 events across the listener's 64-event read boundary.
    request = FakeRequest()
    base = time.monotonic() - 20
    for index in range(80):
        request.events.append(gpio_event(5, index + 1, index + 1,
                                         high=bool(index % 2), timestamp=base + index * 0.1))
    request.ready.set()
    controller = Mock(enabled=True)
    listener = ButtonListener(controller, gpio_module(request))
    listener.start()
    try:
        wait_until(lambda: controller.request_button.call_count == 40)
    finally:
        listener.stop()
    assert controller.request_button.call_count == 40
    assert request.released.is_set()


def test_exact_global_anomaly_sequence_preserves_debounce(caplog):
    tracker = EventSequenceTracker()
    debounce = ButtonDebouncer({5: 'A'})
    debounce.initialize({5: True})
    accepted = []
    # Exact kernel timestamps and counters from the combined diagnostic.
    stream = [(1, 1, False, 3415015628052),
              (4, 2, False, 3416330559291),
              (4, 3, False, 3416330605489),
              (5, 4, True, 3416330652520)]
    for global_sequence, line_sequence, high, ns in stream:
        event = gpio_event(5, global_sequence, line_sequence, high)
        event.timestamp_ns = ns
        tracker.observe(event)
        accepted.extend(debounce.edge(5, high, ns / 1_000_000_000))
    accepted.extend(debounce.settle(stream[-1][3] / 1_000_000_000 + 0.08))
    assert accepted == ['A']
    assert debounce.levels[5] and debounce.armed[5]
    assert tracker.global_anomalies == 2
    assert 'per-line sequence valid' in caplog.text


def test_global_anomalies_do_not_mask_interleaved_line_loss():
    tracker = EventSequenceTracker()
    for event in [gpio_event(5, 1, 1), gpio_event(6, 4, 1),
                  gpio_event(5, 4, 2, True), gpio_event(6, 5, 2, True)]:
        tracker.observe(event)
    with pytest.raises(RuntimeError, match='line expected=3 received=4'):
        tracker.observe(gpio_event(6, 6, 4))


def test_listener_continues_through_exact_global_anomaly(monkeypatch):
    request = FakeRequest()
    base = time.monotonic() - 2
    for global_sequence, line_sequence, high, delta in [
        (1, 1, False, 0), (4, 2, False, 1.3),
        (4, 3, False, 1.3001), (5, 4, True, 1.3002)]:
        request.events.append(gpio_event(5, global_sequence, line_sequence, high, base + delta))
    request.ready.set()
    probe = ButtonDebouncer(ButtonListener.PINS)
    monkeypatch.setattr('button_listener.ButtonDebouncer', lambda pins: probe)
    controller = Mock(enabled=True)
    listener = ButtonListener(controller, gpio_module(request))
    listener.start()
    try:
        wait_until(lambda: controller.request_button.call_count == 1 and probe.armed[5])
        assert listener.thread.is_alive()
        assert not request.released.is_set()
    finally:
        listener.stop()
    controller.request_button.assert_called_once_with('A')
    assert request.released.is_set()


def test_global_warning_volume_is_bounded(caplog):
    tracker = EventSequenceTracker()
    for index in range(1, 21):
        tracker.observe(gpio_event(5, 1, index))
    assert tracker.global_anomalies == 19
    assert len([r for r in caplog.records if r.levelname == 'WARNING']) == 5
