import requests
from PIL import Image, ImageEnhance, ImageOps, ImageFilter
from io import BytesIO
import os
import logging
import hashlib
import tempfile
import subprocess
import shutil
import signal
import threading
import time
import math
from resource_guard import resource_checkpoint, report_resource_failure

# Separate from Chromium's --timeout (a page-loading option).
SCREENSHOT_TIMEOUT_SECONDS = 60
SCREENSHOT_TERMINATE_SECONDS = 2
_screenshot_shutdown = threading.Event()

def cancel_screenshots():
    """Cancel active screenshots and reject new ones during worker shutdown."""
    _screenshot_shutdown.set()

def reset_screenshot_shutdown():
    """Called before starting the application's single refresh worker."""
    _screenshot_shutdown.clear()

def _signal_screenshot_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass

def _screenshot_group_exists(process):
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False

def _cleanup_screenshot_process(process):
    """Reap the leader and bound cleanup of descendants in its private session."""
    _signal_screenshot_group(process, signal.SIGTERM)
    deadline = time.monotonic() + SCREENSHOT_TERMINATE_SECONDS
    try:
        process.communicate(timeout=SCREENSHOT_TERMINATE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    while _screenshot_group_exists(process) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _screenshot_group_exists(process):
        report_resource_failure("chromium_cleanup_failure")
        _signal_screenshot_group(process, signal.SIGKILL)
    try:
        process.communicate(timeout=SCREENSHOT_TERMINATE_SECONDS)
    except subprocess.TimeoutExpired:
        # Do not hang shutdown if an escaped child still holds a pipe open.
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        process.wait(timeout=SCREENSHOT_TERMINATE_SECONDS)


logger = logging.getLogger(__name__)

def get_image(image_url):
    response = requests.get(image_url, timeout=30)
    img = None
    if 200 <= response.status_code < 300 or response.status_code == 304:
        img = Image.open(BytesIO(response.content))
    else:
        logger.error(f"Received non-200 response from {image_url}: status_code: {response.status_code}")
    return img

def change_orientation(image, orientation, inverted=False):
    if orientation == 'horizontal':
        angle = 0
    elif orientation == 'vertical':
        angle = 90

    if inverted:
        angle = (angle + 180) % 360

    return image.rotate(angle, expand=1)

def resize_image(image, desired_size, image_settings=[]):
    img_width, img_height = image.size
    desired_width, desired_height = desired_size
    desired_width, desired_height = int(desired_width), int(desired_height)

    img_ratio = img_width / img_height
    desired_ratio = desired_width / desired_height

    keep_width = "keep-width" in image_settings

    x_offset, y_offset = 0,0
    new_width, new_height = img_width,img_height
    # Step 1: Determine crop dimensions
    desired_ratio = desired_width / desired_height
    if img_ratio > desired_ratio:
        # Image is wider than desired aspect ratio
        new_width = int(img_height * desired_ratio)
        if not keep_width:
            x_offset = (img_width - new_width) // 2
    else:
        # Image is taller than desired aspect ratio
        new_height = int(img_width / desired_ratio)
        if not keep_width:
            y_offset = (img_height - new_height) // 2

    # Step 2: Crop the image
    image = image.crop((x_offset, y_offset, x_offset + new_width, y_offset + new_height))

    # Step 3: Resize to the exact desired dimensions (if necessary)
    return image.resize((desired_width, desired_height), Image.LANCZOS)

def apply_image_enhancement(img, image_settings={}):
    # Convert image to RGB mode if necessary for enhancement operations
    # ImageEnhance requires RGB mode for operations like blend
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')

    # Each enhance() call blends over the full image, so skip factors of 1.0
    # (the no-op default) to avoid wasted full-frame passes on low-resource devices.
    enhancements = [
        (ImageEnhance.Brightness, image_settings.get("brightness", 1.0)),
        (ImageEnhance.Contrast, image_settings.get("contrast", 1.0)),
        (ImageEnhance.Color, image_settings.get("saturation", 1.0)),
        (ImageEnhance.Sharpness, image_settings.get("sharpness", 1.0)),
    ]
    for enhancer, factor in enhancements:
        if factor != 1.0:
            img = enhancer(img).enhance(factor)

    return img

def compute_image_hash(image):
    """Compute SHA-256 hash of an image."""
    image = image.convert("RGB")
    img_bytes = image.tobytes()
    return hashlib.sha256(img_bytes).hexdigest()

def take_screenshot_html(html_str, dimensions, timeout_ms=None, *, process_timeout_seconds=SCREENSHOT_TIMEOUT_SECONDS):
    try:
        with tempfile.TemporaryDirectory(prefix="inkypi-html-") as directory:
            path = os.path.join(directory, "page.html")
            with open(path, "w", encoding="utf-8") as html_file:
                html_file.write(html_str)
            return take_screenshot(path, dimensions, timeout_ms,
                                   process_timeout_seconds=process_timeout_seconds)
    except Exception as e:
        logger.error(f"Failed to take screenshot: {str(e)}")
        return None

def _find_chromium_binary():
    """Find the first available Chromium-based binary in system PATH."""
    candidates = ["chromium-headless-shell", "chromium", "chrome"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            logger.debug(f"Found browser binary: {candidate} at {path}")
            return candidate
    return None


def take_screenshot(target, dimensions, timeout_ms=None, *, process_timeout_seconds=SCREENSHOT_TIMEOUT_SECONDS):
    """Return an independent PIL image, or None on failure/cancellation.

    POSIX process sessions isolate Chromium cleanup from the application.
    The Python deadline includes page loading and screenshot generation.
    """
    process = None
    try:
        if not math.isfinite(process_timeout_seconds) or process_timeout_seconds <= 0:
            raise ValueError("Screenshot process timeout must be positive")
        if _screenshot_shutdown.is_set():
            return None
        browser = _find_chromium_binary()
        if not browser:
            logger.error("No Chromium-based browser found. Install chromium, chromium-headless-shell, or chrome.")
            report_resource_failure("chromium_unavailable")
            return None
        with tempfile.TemporaryDirectory(prefix="inkypi-screenshot-") as directory:
            img_file_path = os.path.join(directory, "screenshot.png")
            command = [
                browser,
                target,
                "--headless",
                f"--screenshot={img_file_path}",
                f"--window-size={dimensions[0]},{dimensions[1]}",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--use-gl=swiftshader",
                "--hide-scrollbars",
                "--in-process-gpu",
                "--js-flags=--jitless",
                "--disable-zero-copy",
                "--disable-gpu-memory-buffer-compositor-resources",
                "--disable-extensions",
                "--disable-plugins",
                "--mute-audio",
                "--renderer-process-limit=1",
                "--no-zygote",
                "--no-sandbox"
            ]
            if timeout_ms:
                command.append(f"--timeout={timeout_ms}")

            try:
                if _screenshot_shutdown.is_set():
                    return None
                try:
                    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               start_new_session=True)
                except OSError:
                    report_resource_failure("chromium_launch_failure")
                    raise
                deadline = time.monotonic() + process_timeout_seconds
                while True:
                    if _screenshot_shutdown.is_set():
                        raise RuntimeError("Screenshot cancelled during application shutdown")
                    resource_checkpoint()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        report_resource_failure("chromium_timeout")
                        raise subprocess.TimeoutExpired(command, process_timeout_seconds)
                    try:
                        process.communicate(timeout=min(remaining, 0.2))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if _screenshot_shutdown.is_set():
                    raise RuntimeError("Screenshot cancelled during application shutdown")
                if process.returncode != 0:
                    logger.error(f"Failed to take screenshot (return code: {process.returncode})")
                    report_resource_failure("chromium_exit", returncode=process.returncode)
                    return None
                try:
                    with Image.open(img_file_path) as img:
                        return img.copy()
                except (OSError, ValueError):
                    report_resource_failure("chromium_output_failure")
                    raise
            finally:
                # Also remove descendants left behind after a successful leader exit.
                if process is not None:
                    try:
                        _cleanup_screenshot_process(process)
                    except Exception:
                        report_resource_failure("chromium_cleanup_failure")
                        raise
    except Exception as e:
        if isinstance(e, MemoryError):
            report_resource_failure("memory_allocation")
        logger.error(f"Failed to take screenshot: {str(e)}")
        return None

def pad_image_blur(img: Image, dimensions: tuple[int, int]) -> Image:
    bkg = ImageOps.fit(img, dimensions)
    bkg = bkg.filter(ImageFilter.BoxBlur(8))
    img = ImageOps.contain(img, dimensions)

    img_size = img.size
    bkg.paste(img, ((dimensions[0] - img_size[0]) // 2, (dimensions[1] - img_size[1]) // 2))
    return bkg
