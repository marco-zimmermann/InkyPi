"""Daily Zurich agenda. The existing worker owns all physical presentation."""
from datetime import datetime, timezone
from pathlib import Path
from plugins.base_plugin.base_plugin import BasePlugin
from .data import read_secret, make_session, load_schedule, PrivateSourceError
from .rendering import render_dashboard


class FlightDuties(BasePlugin):
    def generate_image(self, settings, device_config):
        dimensions=tuple(device_config.get_resolution())
        if device_config.get_config('orientation')=='vertical':dimensions=dimensions[::-1]
        if dimensions!=(800,480):raise ValueError('Flight Duties requires 800 x 480 landscape')
        now=datetime.now(timezone.utc)
        try:url=read_secret(device_config)
        except PrivateSourceError:
            result=dict(schedule=None,source='unavailable',fetched_at=None,
                        error='Configure a private calendar secret')
        else:
            with make_session() as session:
                # Calendar text contains personal data: keep it outside Flask's
                # public static tree, adjacent to private device configuration.
                cache=Path(device_config.config_file).resolve().parent/'.flight-duties-cache'
                result=load_schedule(session,url,cache,now,
                                     clock=lambda: datetime.now(timezone.utc))
        image=render_dashboard(result,result.get('evaluated_at',now),settings)
        image.info['inkypi_data_status']=('live' if result['source']=='live' else
                                        'cached_stale' if result['source']=='cached' else 'unavailable')
        image.info['inkypi_live_data_success']=result['source']=='live'
        return image
