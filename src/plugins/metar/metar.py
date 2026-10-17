"""METAR dashboard. All image/display scheduling remains with InkyPi."""
from datetime import datetime, timezone
from pathlib import Path
from plugins.base_plugin.base_plugin import BasePlugin
from .data import make_session, load_metar, load_airport
from .decoding import validate_icao
from .rendering import render_dashboard


class Metar(BasePlugin):
    def generate_image(self, settings, device_config):
        icao=validate_icao(settings.get('icao','LSZL'))
        cache=Path(device_config.plugin_image_dir)/'.metar-cache'
        now=datetime.now(timezone.utc)
        with make_session() as session:
            report=load_metar(session,cache,icao,now)
            airport=load_airport(session,cache,icao,now)
        return render_dashboard(icao,report,airport,now)
