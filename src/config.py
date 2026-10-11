import os
import json
import logging
import threading
import tempfile
import stat
from copy import deepcopy
from functools import wraps
from dotenv import load_dotenv
from model import PlaylistManager, RefreshInfo

logger = logging.getLogger(__name__)

def serialized_config(method):
    """Serialize short configuration reads/writes, never rendering."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._config_lock:
            return method(self, *args, **kwargs)
    return wrapped


class Config:
    # Base path for the project directory
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

    # File paths relative to the script's directory
    config_file = os.path.join(BASE_DIR, "config", "device.json")

    # File path for storing the current image being displayed
    current_image_file = os.path.join(BASE_DIR, "static", "images", "current_image.png")

    # Directory path for storing plugin instance images
    plugin_image_dir = os.path.join(BASE_DIR, "static", "images", "plugins")

    def __init__(self):
        self._config_lock = threading.RLock()
        self.config = self.read_config()
        self.plugins_list = self.read_plugins_list()
        self.playlist_manager = self.load_playlist_manager()
        self.refresh_info = self.load_refresh_info()

    def read_config(self):
        """Reads the device config JSON file and returns it as a dictionary."""
        logger.debug(f"Reading device config from {self.config_file}")
        with open(self.config_file) as f:
            config = json.load(f)

        logger.debug("Loaded config:\n%s", json.dumps(config, indent=3))

        return config

    def read_plugins_list(self):
        """Reads the plugin-info.json config JSON from each plugin folder. Excludes the base plugin."""
        # Iterate over all plugin folders
        plugins_list = []
        for plugin in sorted(os.listdir(os.path.join(self.BASE_DIR, "plugins"))):
            plugin_path = os.path.join(self.BASE_DIR, "plugins", plugin)
            if os.path.isdir(plugin_path) and plugin != "__pycache__":
                # Check if the plugin-info.json file exists
                plugin_info_file = os.path.join(plugin_path, "plugin-info.json")
                if os.path.isfile(plugin_info_file):
                    logger.debug(f"Reading plugin info from {plugin_info_file}")
                    with open(plugin_info_file) as f:
                        plugin_info = json.load(f)
                    plugins_list.append(plugin_info)

        return plugins_list

    @serialized_config
    def write_config(self):
        """Updates the cached config from the model objects and writes to the config file."""
        logger.debug(f"Writing device config to {self.config_file}")
        self.update_value("playlist_config", self.playlist_manager.to_dict())
        self.update_value("refresh_info", self.refresh_info.to_dict())
        # Replace within the same filesystem: interruption never leaves partial JSON.
        payload = json.dumps(self.config, indent=4)
        mode = stat.S_IMODE(os.stat(self.config_file).st_mode)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(self.config_file),
                                             prefix=".device-", delete=False) as outfile:
                temporary = outfile.name
                os.fchmod(outfile.fileno(), mode)
                outfile.write(payload)
                outfile.flush()
                os.fsync(outfile.fileno())
            os.replace(temporary, self.config_file)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    @serialized_config
    def get_config(self, key=None, default={}):
        """Gets the value of a specific configuration key or returns the entire config if none provided."""
        if key is not None:
            return self.config.get(key, default)
        return self.config

    def get_plugins(self):
        """Returns the list of plugin configurations, sorted by custom order if set."""
        plugin_order = self.config.get('plugin_order', [])

        if not plugin_order:
            return self.plugins_list

        # Create a dict for quick lookup
        plugins_dict = {p['id']: p for p in self.plugins_list}

        # Build ordered list
        ordered = []
        for plugin_id in plugin_order:
            if plugin_id in plugins_dict:
                ordered.append(plugins_dict.pop(plugin_id))

        # Append any remaining plugins not in the order (new plugins)
        ordered.extend(plugins_dict.values())

        return ordered

    def set_plugin_order(self, order):
        """Sets the custom plugin display order."""
        self.update_value('plugin_order', order, write=True)

    def get_plugin(self, plugin_id):
        """Finds and returns a plugin config by its ID."""
        return next((plugin for plugin in self.plugins_list if plugin['id'] == plugin_id), None)

    def get_resolution(self):
        """Returns the display resolution as a tuple (width, height) from the configuration."""
        resolution = self.get_config("resolution")
        width, height = resolution
        return (int(width), int(height))

    @serialized_config
    def update_config(self, config):
        """Updates the config with the new values provided and writes to the config file."""
        self.config.update(config)
        self.write_config()

    @serialized_config
    def update_value(self, key, value, write=False):
        """Updates a specific key in the configuration with a new value and optionally writes it to the config file."""
        self.config[key] = value
        if write:
            self.write_config()

    @serialized_config
    def update_section(self, key, transform):
        """Atomically edit one section while retaining concurrent unrelated edits."""
        value = self.config.get(key, {})
        value = deepcopy(value) if isinstance(value, dict) else {}
        transform(value)
        old = self.config.get(key)
        existed = key in self.config
        self.config[key] = value
        try:
            self.write_config()
        except Exception:
            if existed:
                self.config[key] = old
            else:
                self.config.pop(key, None)
            raise

    def load_env_key(self, key):
        """Loads an environment variable using dotenv and returns its value."""
        load_dotenv(override=True)
        return os.getenv(key)

    def load_playlist_manager(self):
        """Loads the playlist manager object from the config."""
        playlist_manager = PlaylistManager.from_dict(self.get_config("playlist_config"))
        if not playlist_manager.playlists:
            playlist_manager.add_default_playlist()
        return playlist_manager

    def load_refresh_info(self):
        """Loads the refresh information from the config."""
        return RefreshInfo.from_dict(self.get_config("refresh_info"))

    def get_playlist_manager(self):
        """Returns the playlist manager."""
        return self.playlist_manager

    def get_refresh_info(self):
        """Returns the refresh information."""
        return self.refresh_info
