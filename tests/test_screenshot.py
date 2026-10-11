"""Screenshot lifecycle tests: fake Chromium, real temporary files, no display."""
import os
import signal
import subprocess
import threading
from unittest.mock import Mock

import pytest
from PIL import Image
from utils import image_utils as screenshots


@pytest.fixture(autouse=True)
def isolated_screenshots(monkeypatch, tmp_path):
    screenshots.reset_screenshot_shutdown()
    monkeypatch.setattr(screenshots.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(screenshots, '_find_chromium_binary', lambda: '/fake/chromium')
    yield
    screenshots.reset_screenshot_shutdown()
    assert list(tmp_path.iterdir()) == []


def fake_browser(monkeypatch, result=0, mode='success'):
    process = Mock(pid=987654, returncode=result)
    def launch(command, **kwargs):
        path = next(x.split('=', 1)[1] for x in command if x.startswith('--screenshot='))
        if mode == 'success':
            Image.new('RGB', (8, 6), 'red').save(path)
        elif mode == 'invalid':
            open(path, 'w').write('invalid PNG')
        return process
    popen = Mock(side_effect=launch)
    monkeypatch.setattr(screenshots.subprocess, 'Popen', popen)
    kill = Mock(side_effect=ProcessLookupError)
    monkeypatch.setattr(screenshots.os, 'killpg', kill)
    return process, popen, kill


def test_success_preserves_pixels_and_existing_chromium_timeout(monkeypatch):
    process, popen, _ = fake_browser(monkeypatch)
    image = screenshots.take_screenshot('https://example.test', (8, 6), timeout_ms=40000)
    assert image.size == (8, 6)
    assert image.getpixel((0, 0)) == (255, 0, 0)
    command = popen.call_args.args[0]
    assert '--timeout=40000' in command
    assert '--renderer-process-limit=1' in command
    assert popen.call_args.kwargs['start_new_session'] is True
    assert process.communicate.call_args_list[0].kwargs['timeout'] <= 0.2


@pytest.mark.parametrize('result,mode', [(1, 'success'), (0, 'missing'), (0, 'invalid')])
def test_failure_returns_none_and_cleans_files(monkeypatch, result, mode):
    process, _, kill = fake_browser(monkeypatch, result, mode)
    assert screenshots.take_screenshot('target', (8, 6)) is None
    assert kill.call_args_list[0].args == (process.pid, signal.SIGTERM)


def test_launch_failure_cleans_directory(monkeypatch):
    monkeypatch.setattr(screenshots.subprocess, 'Popen', Mock(side_effect=OSError('cannot launch')))
    assert screenshots.take_screenshot('target', (8, 6)) is None


def test_timeout_terminates_group(monkeypatch):
    process, _, kill = fake_browser(monkeypatch)
    process.communicate.side_effect = [subprocess.TimeoutExpired('browser', 0.2), (b'', b''), (b'', b'')]
    ticks = iter([0, 0, 2, 2, 2])
    monkeypatch.setattr(screenshots.time, 'monotonic', lambda: next(ticks))
    assert screenshots.take_screenshot('target', (8, 6), process_timeout_seconds=1) is None
    assert kill.call_args_list[0].args == (process.pid, signal.SIGTERM)


def test_shutdown_during_render_discards_successful_png(monkeypatch):
    process, _, kill = fake_browser(monkeypatch)
    def interrupted(**kwargs):
        screenshots.cancel_screenshots()
        return b'', b''
    process.communicate.side_effect = interrupted
    assert screenshots.take_screenshot('target', (8, 6)) is None
    assert kill.call_args_list[0].args[1] == signal.SIGTERM
    # Shutdown gate also prevents a subsequent launch.
    assert screenshots.take_screenshot('target', (8, 6)) is None


def test_cleanup_graceful_group_exit(monkeypatch):
    process = Mock(pid=987654)
    signals = Mock()
    monkeypatch.setattr(screenshots, '_signal_screenshot_group', signals)
    monkeypatch.setattr(screenshots, '_screenshot_group_exists', lambda _: False)
    screenshots._cleanup_screenshot_process(process)
    assert signals.call_args_list == [((process, signal.SIGTERM),)]
    process.communicate.assert_called_with(timeout=2)


def test_cleanup_stalled_group_escalates_and_reaps(monkeypatch):
    process = Mock(pid=987654)
    process.communicate.side_effect = [subprocess.TimeoutExpired('browser', 2), (b'', b'')]
    signals = Mock()
    monkeypatch.setattr(screenshots, '_signal_screenshot_group', signals)
    monkeypatch.setattr(screenshots, '_screenshot_group_exists', lambda _: True)
    ticks = iter([0, 3])
    monkeypatch.setattr(screenshots.time, 'monotonic', lambda: next(ticks))
    screenshots._cleanup_screenshot_process(process)
    assert [call.args[1] for call in signals.call_args_list] == [signal.SIGTERM, signal.SIGKILL]
    assert process.communicate.call_count == 2


def test_cleanup_exited_leader_with_live_descendant(monkeypatch):
    process = Mock(pid=987654, returncode=0)
    signals = Mock()
    monkeypatch.setattr(screenshots, '_signal_screenshot_group', signals)
    monkeypatch.setattr(screenshots, '_screenshot_group_exists', lambda _: True)
    ticks = iter([0, 3])
    monkeypatch.setattr(screenshots.time, 'monotonic', lambda: next(ticks))
    screenshots._cleanup_screenshot_process(process)
    assert signals.call_args_list[-1].args[1] == signal.SIGKILL


@pytest.mark.parametrize('failure', [False, True])
def test_html_cleanup_success_and_exception(monkeypatch, failure):
    paths = []
    def take(path, *args, **kwargs):
        paths.append(path)
        assert open(path).read() == '<html>hello</html>'
        if failure:
            raise RuntimeError('mock screenshot failure')
        return Image.new('RGB', (8, 6))
    monkeypatch.setattr(screenshots, 'take_screenshot', take)
    result = screenshots.take_screenshot_html('<html>hello</html>', (8, 6))
    assert (result is None) == failure
    assert not os.path.exists(paths[0])


def test_html_cancellation_propagates_and_cleans(monkeypatch):
    monkeypatch.setattr(screenshots, 'take_screenshot', Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        screenshots.take_screenshot_html('html', (8, 6))


@pytest.mark.parametrize('bound', [0, -1, float('inf'), float('nan')])
def test_invalid_deadline_rejected_before_launch(monkeypatch, bound):
    launch = Mock()
    monkeypatch.setattr(screenshots.subprocess, 'Popen', launch)
    assert screenshots.take_screenshot('target', (8, 6), process_timeout_seconds=bound) is None
    launch.assert_not_called()


def test_pipe_cleanup_remains_bounded_when_descendant_escapes(monkeypatch):
    process = Mock(pid=987654)
    process.communicate.side_effect = subprocess.TimeoutExpired('browser', 2)
    monkeypatch.setattr(screenshots, '_signal_screenshot_group', Mock())
    monkeypatch.setattr(screenshots, '_screenshot_group_exists', lambda _: False)
    screenshots._cleanup_screenshot_process(process)
    process.stdout.close.assert_called_once()
    process.stderr.close.assert_called_once()
    process.wait.assert_called_once_with(timeout=2)


def test_keyboard_interrupt_releases_browser_and_temp_files(monkeypatch):
    process, _, kill = fake_browser(monkeypatch)
    process.communicate.side_effect = [KeyboardInterrupt(), (b'', b''), (b'', b'')]
    with pytest.raises(KeyboardInterrupt):
        screenshots.take_screenshot('target', (8, 6))
    assert kill.call_args_list[0].args[1] == signal.SIGTERM
