import threading
import time
import os
import logging
import psutil
import pytz
from datetime import datetime, timezone
from plugins.plugin_registry import get_plugin_instance
from utils.image_utils import compute_image_hash, cancel_screenshots, reset_screenshot_shutdown
from model import RefreshInfo, PlaylistManager
from PIL import Image
from screen_selection import ScreenSelectionController

logger = logging.getLogger(__name__)

class RefreshTask:
    """Handles the logic for refreshing the display using a background thread."""

    def __init__(self, device_config, display_manager):
        self.device_config = device_config
        self.display_manager = display_manager

        self.thread = None
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.running = False
        self.manual_update_request = None
        self._manual_lock = threading.Lock()
        self._wake_requested = False
        self.screen_selection = ScreenSelectionController(device_config, self.signal_config_change)

    def start(self):
        """Starts the background thread for refreshing the display."""
        with self.condition:
            if not self.thread or not self.thread.is_alive():
                logger.info("Starting refresh task")
                self.thread = threading.Thread(target=self._run, daemon=True)
                reset_screenshot_shutdown()
                self.running = True
                self.thread.start()

    def stop(self):
        """Stops the refresh task by notifying the background thread to exit."""
        with self.condition:
            self.running = False
            self.condition.notify_all()  # Wake the thread to let it exit
        cancel_screenshots()
        if self.thread:
            logger.info("Stopping refresh task")
            self.thread.join()

    def _run(self):
        """One render worker; no condition lock is held during rendering or IO."""
        while True:
            ticket = None
            action = None
            previous_time = None
            latest_refresh = None
            try:
                with self.condition:
                    delay = self.device_config.get_config("plugin_cycle_interval_seconds", default=3600)
                    if self.screen_selection.enabled:
                        delay = min(delay, 60)
                    self.condition.wait_for(
                        lambda: not self.running or self._wake_requested or self.manual_update_request is not None or self.screen_selection.has_pending,
                        timeout=delay)
                    if not self.running:
                        if self.manual_update_request:
                            _, event, result = self.manual_update_request
                            result["exception"] = RuntimeError("Refresh worker stopped")
                            event.set()
                            self.manual_update_request = None
                        return
                    self._wake_requested = False
                    ticket, self.manual_update_request = self.manual_update_request, None

                current_dt = self._get_current_datetime()
                latest_refresh = self.device_config.get_refresh_info()
                if ticket:
                    action = ticket[0]
                else:
                    action = self._scheduled_action(current_dt)
                if action is None:
                    continue
                reference = None
                if isinstance(action, PlaylistRefresh):
                    previous_time = action.plugin_instance.latest_refresh_time
                    reference = dict(playlist=action.playlist.name,
                                     plugin_id=action.plugin_instance.plugin_id,
                                     instance=action.plugin_instance.name)
                self.screen_selection.begin_refresh(reference, web=ticket is not None)
                plugin_config = self.device_config.get_plugin(action.get_plugin_id())
                if plugin_config is None:
                    raise ValueError("Plugin configuration no longer exists")
                plugin = get_plugin_instance(plugin_config)
                image = action.execute(plugin, self.device_config, current_dt)
                if not self.running:
                    raise RuntimeError("Refresh cancelled during application shutdown")
                image_hash = compute_image_hash(image)
                info = action.get_refresh_info()
                info.update(refresh_time=current_dt.isoformat(), image_hash=image_hash)
                image_changed = image_hash != latest_refresh.image_hash
                if image_changed or getattr(action, "restore_selected", False):
                    self.display_manager.display_image(image, image_settings=plugin.config.get("image_settings", []))
                if isinstance(action, PlaylistRefresh) and action.generated:
                    action.plugin_instance.latest_refresh_time = current_dt.isoformat()
                self.device_config.refresh_info = RefreshInfo(**info)
                # The section transaction also commits refresh metadata/model state.
                if not self.screen_selection.record_display_success(
                        reference, web=ticket is not None, image_changed=image_changed):
                    self.device_config.write_config()
            except Exception as exc:
                # A failed physical refresh must not postpone the next scheduled retry.
                if isinstance(action, PlaylistRefresh):
                    action.plugin_instance.latest_refresh_time = previous_time
                if latest_refresh is not None:
                    self.device_config.refresh_info = latest_refresh
                logger.exception("Exception during refresh")
                if ticket:
                    ticket[2]["exception"] = exc
            finally:
                self.screen_selection.end_refresh()
                if ticket:
                    ticket[1].set()

    def _scheduled_action(self, current_dt):
        if self.screen_selection.enabled:
            pending = self.screen_selection.consume_pending()
            if pending and pending[2]:
                action = PlaylistRefresh(pending[0], pending[1], force=True)
                action.restore_selected = pending[3]
                return action
            # Consume each selection once, including failed attempts. Never fall
            # through to either selected-instance or ordinary playlist scheduling.
            if self.screen_selection.manual_only:
                return None
            selected = self.screen_selection.selected()
            if selected and self._selection_due(selected[1], current_dt):
                return PlaylistRefresh(*selected, force=True)
            return None
        # Drain stale requests when mode is disabled; ordinary playlists retain priority.
        self.screen_selection.consume_pending()
        if self.device_config.get_config("log_system_stats"):
            self.log_system_stats()
        playlist, instance = self._determine_next_plugin(
            self.device_config.get_playlist_manager(), self.device_config.get_refresh_info(), current_dt)
        return PlaylistRefresh(playlist, instance) if instance else None

    @staticmethod
    def _selection_due(instance, now):
        try:
            latest = instance.get_latest_refresh_dt()
            if latest is None:
                return True
            refresh = instance.refresh
            if "interval" in refresh:
                interval = refresh["interval"]
                return isinstance(interval, (int, float)) and interval > 0 and (now - latest).total_seconds() >= interval
            if "scheduled" in refresh:
                scheduled = datetime.strptime(refresh["scheduled"], "%H:%M").time()
                due = now.replace(hour=scheduled.hour, minute=scheduled.minute, second=0, microsecond=0)
                return latest < due <= now
        except (ValueError, TypeError, AttributeError):
            logger.warning("Invalid refresh schedule for selected instance %s", instance.name)
        return False

    def request_button(self, button):
        """Future GPIO adapters only publish a selection; they never touch the display."""
        return self.screen_selection.request_button(button)

    def manual_update(self, refresh_action):
        """Keep synchronous web API compatibility with one bounded manual mailbox."""
        with self._manual_lock:
            event, result = threading.Event(), {}
            with self.condition:
                if not self.running:
                    logger.warning("Background refresh task is not running")
                    return
                self.manual_update_request = (refresh_action, event, result)
                self.condition.notify_all()
            event.wait()
            if "exception" in result:
                raise result["exception"]

    def signal_config_change(self):
        with self.condition:
            self._wake_requested = True
            self.condition.notify_all()

    def _get_current_datetime(self):
        """Retrieves the current datetime based on the device's configured timezone."""
        tz_str = self.device_config.get_config("timezone", default="UTC")
        return datetime.now(pytz.timezone(tz_str))

    def _determine_next_plugin(self, playlist_manager, latest_refresh_info, current_dt):
        """Determines the next plugin to refresh based on the active playlist, plugin cycle interval, and current time."""
        playlist = playlist_manager.determine_active_playlist(current_dt)
        if not playlist:
            playlist_manager.active_playlist = None
            logger.info(f"No active playlist determined.")
            return None, None

        playlist_manager.active_playlist = playlist.name
        if not playlist.plugins:
            logger.info(f"Active playlist '{playlist.name}' has no plugins.")
            return None, None

        latest_refresh_dt = latest_refresh_info.get_refresh_datetime()
        plugin_cycle_interval = self.device_config.get_config("plugin_cycle_interval_seconds", default=3600)
        should_refresh = PlaylistManager.should_refresh(latest_refresh_dt, plugin_cycle_interval, current_dt)

        if not should_refresh:
            latest_refresh_str = latest_refresh_dt.strftime('%Y-%m-%d %H:%M:%S') if latest_refresh_dt else "None"
            logger.info(f"Not time to update display. | latest_update: {latest_refresh_str} | plugin_cycle_interval: {plugin_cycle_interval}")
            return None, None

        plugin = playlist.get_next_plugin()
        logger.info(f"Determined next plugin. | active_playlist: {playlist.name} | plugin_instance: {plugin.name}")

        return playlist, plugin
    
    def log_system_stats(self):
        metrics = {
            'cpu_percent': psutil.cpu_percent(interval=1),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage('/').percent,
            'load_avg_1_5_15': os.getloadavg(),
            'swap_percent': psutil.swap_memory().percent,
            'net_io': {
                'bytes_sent': psutil.net_io_counters().bytes_sent,
                'bytes_recv': psutil.net_io_counters().bytes_recv
            }
        }

        logger.info(f"System Stats: {metrics}")

class RefreshAction:
    """Base class for a refresh action. Subclasses should override the methods below."""
    
    def refresh(self, plugin, device_config, current_dt):
        """Perform a refresh operation and return the updated image."""
        raise NotImplementedError("Subclasses must implement the refresh method.")
    
    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        raise NotImplementedError("Subclasses must implement the get_refresh_info method.")
    
    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        raise NotImplementedError("Subclasses must implement the get_plugin_id method.")

class ManualRefresh(RefreshAction):
    """Performs a manual refresh based on a plugin's ID and its associated settings.
    
    Attributes:
        plugin_id (str): The ID of the plugin to refresh.
        plugin_settings (dict): The settings for the manual refresh.
    """

    def __init__(self, plugin_id: str, plugin_settings: dict):
        self.plugin_id = plugin_id
        self.plugin_settings = plugin_settings

    def execute(self, plugin, device_config, current_dt: datetime):
        """Performs a manual refresh using the stored plugin ID and settings."""
        return plugin.generate_image(self.plugin_settings, device_config)

    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        return {"refresh_type": "Manual Update", "plugin_id": self.plugin_id}

    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        return self.plugin_id

class PlaylistRefresh(RefreshAction):
    """Performs a refresh using a plugin instance within a playlist context.

    Attributes:
        playlist: The playlist object associated with the refresh.
        plugin_instance: The plugin instance to refresh.
    """

    def __init__(self, playlist, plugin_instance, force=False):
        self.playlist = playlist
        self.plugin_instance = plugin_instance
        self.force = force
        self.generated = False

    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        return {
            "refresh_type": "Playlist",
            "playlist": self.playlist.name,
            "plugin_id": self.plugin_instance.plugin_id,
            "plugin_instance": self.plugin_instance.name
        }

    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        return self.plugin_instance.plugin_id

    def execute(self, plugin, device_config, current_dt: datetime):
        """Performs a refresh for the specified plugin instance within its playlist context."""
        self.generated = False
        # Determine the file path for the plugin's image
        plugin_image_path = os.path.join(device_config.plugin_image_dir, self.plugin_instance.get_image_path())

        # Check if a refresh is needed based on the plugin instance's criteria
        if self.plugin_instance.should_refresh(current_dt) or self.force:
            logger.info(f"Refreshing plugin instance. | plugin_instance: '{self.plugin_instance.name}'") 
            # Generate a new image
            image = plugin.generate_image(self.plugin_instance.settings, device_config)
            image.save(plugin_image_path)
            self.generated = True
        else:
            logger.info(f"Not time to refresh plugin instance, using latest image. | plugin_instance: {self.plugin_instance.name}.")
            # Load the existing image from disk
            with Image.open(plugin_image_path) as img:
                image = img.copy()

        return image
