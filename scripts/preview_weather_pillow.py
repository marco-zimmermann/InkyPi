#!/usr/bin/env python3
"""Offline synthetic Pillow preview; no Chromium, live fetching or display IO.

PYTHONPATH=src:scripts python scripts/preview_weather_pillow.py --output-dir ...
  [--baseline path/to/original-frozen-output.png]
"""
import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance

from plugins.weather.pillow_rendering import render_dashboard
from weather_pillow_fixture import load_fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    dashboard, settings, _ = load_fixture()
    image = render_dashboard(dashboard, settings)
    output = args.output_dir / 'weather-pillow-synthetic.png'
    try:
        image.save(output)
        model = asdict(dashboard)
        model['rendered_at'] = dashboard.rendered_at.isoformat()
        (args.output_dir/'dashboard-model.json').write_text(json.dumps(model,indent=2)+'\n')
        if args.baseline:
            with Image.open(args.baseline) as source:
                baseline=source.convert('RGB')
            try:
                if baseline.size != image.size:
                    raise ValueError('Baseline must be 800 x 480')
                baseline.save(args.output_dir/'weather-chromium-baseline.png')
                with Image.blend(baseline,image,.5) as overlay:
                    overlay.save(args.output_dir/'weather-overlay.png')
                with ImageChops.difference(baseline,image) as diff:
                    channels=diff.split()
                    with ImageChops.lighter(channels[0],channels[1]) as first:
                        with ImageChops.lighter(first,channels[2]) as union:
                            histogram=union.histogram()
                    comparison=dict(dimensions=list(image.size), changed_pixels=sum(histogram[1:]),
                                    bbox=diff.getbbox(),maximum_channel_difference=[v[1] for v in diff.getextrema()],
                                    baseline_sha256=hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
                                    note='Different rendering engines; pixel identity is not expected.')
                    for c in channels:c.close()
                    diff.save(args.output_dir/'weather-difference.png')
                    with ImageEnhance.Brightness(diff).enhance(3) as amplified:
                        amplified.save(args.output_dir/'weather-difference-3x.png')
                    (args.output_dir/'comparison.json').write_text(json.dumps(comparison,indent=2)+'\n')
                for name,box in [('conditions',(17,75,783,227)),('graph',(17,235,783,335)),('forecast',(17,348,783,464))]:
                    with Image.new('RGB',((box[2]-box[0])*2,(box[3]-box[1])*4),'white') as pair:
                        for row,im in enumerate([baseline,image]):
                            with im.crop(box) as crop:
                                with crop.resize((crop.width*2,crop.height*2),Image.Resampling.NEAREST) as large:
                                    pair.paste(large,(0,row*large.height))
                        pair.save(args.output_dir/(name+'-chromium-top-pillow-bottom-2x.png'))
            finally:
                baseline.close()
    finally:
        image.close()
    print(output.resolve())


if __name__=='__main__':
    main()
