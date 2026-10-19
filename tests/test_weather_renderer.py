"""Weather renderer dispatch: offline, real Pillow and mocked providers/browser."""
import copy
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image, ImageChops

from plugins.weather.weather import Weather
from plugins.weather.pillow_rendering import Painter


@pytest.fixture
def prepared(monkeypatch):
    from scripts.weather_pillow_fixture import load_fixture
    model, settings, parsed = load_fixture()
    plugin = Weather({'id': 'weather'})
    for name in ('get_open_meteo_data', 'get_open_meteo_air_quality',
                 'get_weather_data', 'get_air_quality'):
        monkeypatch.setattr(plugin, name, Mock(return_value={}))
    for name in ('parse_open_meteo_data', 'parse_weather_data'):
        monkeypatch.setattr(plugin, name, Mock(side_effect=lambda *a: copy.deepcopy(parsed)))
    monkeypatch.setattr(plugin, 'parse_timezone', Mock(return_value=model.rendered_at.tzinfo))
    monkeypatch.setattr(plugin, 'get_location', Mock(return_value='Test location'))
    monkeypatch.setattr('requests.sessions.Session.request', Mock(side_effect=AssertionError('No live weather')))
    monkeypatch.setattr('subprocess.Popen', Mock(side_effect=AssertionError('No real browser')))
    device = Mock()
    device.get_config.side_effect = lambda key, **kw: {
        'timezone': 'Europe/Zurich', 'time_format': '24h', 'orientation': 'horizontal'}[key]
    device.get_resolution.return_value = (800, 480)
    device.load_env_key.return_value = 'synthetic-not-a-secret'
    return plugin, settings, device, parsed


@pytest.mark.parametrize('provider', ['OpenMeteo', 'OpenWeatherMap'])
@pytest.mark.parametrize('units', ['metric', 'imperial', 'standard'])
def test_default_pillow_uses_existing_provider_and_saved_settings(prepared, monkeypatch, provider, units):
    plugin, settings, device, parsed = prepared
    settings.update(weatherProvider=provider, units=units)
    before = copy.deepcopy(settings)
    browser = Mock(side_effect=AssertionError('No automatic Chromium'))
    monkeypatch.setattr(plugin, 'render_image', browser)
    with plugin.generate_image(settings, device) as image:
        assert image.size == (800, 480) and image.mode == 'RGB'
    assert settings == before
    parser = plugin.parse_open_meteo_data if provider == 'OpenMeteo' else plugin.parse_weather_data
    parser.assert_called_once()
    browser.assert_not_called()


def test_explicit_chromium_fallback_only(prepared, monkeypatch):
    plugin, settings, device, _ = prepared
    settings['renderer'] = 'chromium'
    image = Image.new('RGB', (800, 480))
    browser = Mock(return_value=image)
    monkeypatch.setattr(plugin, 'render_image', browser)
    monkeypatch.setattr('plugins.weather.pillow_rendering.render_dashboard', Mock(side_effect=AssertionError('Wrong renderer')))
    assert plugin.generate_image(settings, device) is image
    dimensions, template, css, params = browser.call_args.args
    assert (dimensions, template, css) == ((800, 480), 'weather.html', 'weather.css')
    assert params['plugin_settings'] is settings
    assert params['last_refresh_time']
    image.close()


@pytest.mark.parametrize('failure', [RuntimeError('Pillow failed'), MemoryError('allocation failed')])
def test_pillow_failure_propagates_without_browser_retry(prepared, monkeypatch, failure):
    plugin, settings, device, _ = prepared
    monkeypatch.setattr('plugins.weather.pillow_rendering.render_dashboard', Mock(side_effect=failure))
    browser = Mock()
    monkeypatch.setattr(plugin, 'render_image', browser)
    with pytest.raises(type(failure), match=str(failure)):
        plugin.generate_image(settings, device)
    browser.assert_not_called()


def test_provider_failure_never_renders(prepared, monkeypatch):
    plugin, settings, device, _ = prepared
    plugin.get_open_meteo_data.side_effect = RuntimeError('provider offline')
    pillow = Mock()
    monkeypatch.setattr('plugins.weather.pillow_rendering.render_dashboard', pillow)
    with pytest.raises(RuntimeError, match='request failure'):
        plugin.generate_image(settings, device)
    pillow.assert_not_called()


@pytest.mark.parametrize('dimensions,orientation', [((480, 800), 'horizontal'), ((800, 480), 'vertical')])
def test_unsupported_layout_does_not_silently_launch_browser(prepared, monkeypatch, dimensions, orientation):
    plugin, settings, device, _ = prepared
    device.get_resolution.return_value = dimensions
    device.get_config.side_effect = lambda key, **kw: {
        'timezone': 'Europe/Zurich', 'time_format': '24h', 'orientation': orientation}[key]
    browser = Mock()
    monkeypatch.setattr(plugin, 'render_image', browser)
    with pytest.raises(ValueError, match='800 x 480'):
        plugin.generate_image(settings, device)
    browser.assert_not_called()


@pytest.mark.parametrize('arrow', list('↑↗→↘↓↙←↖'))
def test_wind_arrow_is_visible_and_bounded_without_font_glyphs(arrow):
    with Image.new('RGB', (40, 40), 'white') as image:
        Painter(image, 'black').arrow(arrow, (10, 10, 26, 26))
        difference = ImageChops.difference(image, Image.new('RGB', image.size, 'white'))
        box = difference.getbbox()
        assert box is not None
        assert box[0] >= 10 and box[1] >= 10 and box[2] <= 26 and box[3] <= 26
        # Tip points in the direction produced by the existing Weather parser.
        x, y = {'↑': (18,12), '↗': (22,14), '→': (24,18), '↘': (22,22),
                '↓': (18,24), '↙': (14,22), '←': (12,18), '↖': (14,14)}[arrow]
        with image.crop((x-1,y-1,x+2,y+2)) as tip:
            assert tip.getextrema() != ((255,255),)*3


def test_dashboard_actually_draws_parser_wind_arrow(monkeypatch):
    from scripts.weather_pillow_fixture import load_fixture
    from plugins.weather.pillow_rendering import render_dashboard
    model, settings, _ = load_fixture()
    draw = Painter.arrow
    calls = []
    def capture(self, arrow, box):
        calls.append((arrow, box))
        return draw(self, arrow, box)
    monkeypatch.setattr(Painter, 'arrow', capture)
    with render_dashboard(model, settings):
        pass
    assert [a for a, _ in calls] == ['↗']
