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
        image = render_dashboard(icao,report,airport,now)
        # Presentation success and live retrieval are distinct. These private
        # Pillow annotations do not change pixels, cache behavior, or requests.
        image.info['inkypi_live_data_success'] = report.get('report') is not None and not report.get('cached')
        image.info['inkypi_data_status'] = (
            'unavailable' if report.get('report') is None else
            'cached_stale' if report.get('cached') and report.get('stale') else
            'cached' if report.get('cached') else 'stale' if report.get('stale') else 'live')
        return image
