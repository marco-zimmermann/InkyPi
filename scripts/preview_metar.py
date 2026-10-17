#!/usr/bin/env python3
"""Render clearly labelled synthetic fixtures locally; no network or app startup.

Run: PYTHONPATH=src python scripts/preview_metar.py --output-dir /path/to/previews
"""
import argparse
import json
from datetime import datetime
from pathlib import Path
from plugins.metar.rendering import render_dashboard


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    repo=Path(__file__).resolve().parents[1]
    fixtures=json.loads((repo/'tests/fixtures/metar/examples.json').read_text())
    airport=json.loads((repo/'src/plugins/metar/airport_seed.json').read_text())['LSZL']
    args.output_dir.mkdir(parents=True,exist_ok=True)
    for name in ('vfr','ifr'):
        state={'report':fixtures[name],'cached':False,'stale':False,'error':None}
        metadata=dict(airport['airport'],as_of=airport['as_of'],status='reference metadata')
        image=render_dashboard('LSZL',state,metadata,datetime.fromisoformat(fixtures['now']),example=True)
        path=args.output_dir/('metar-lszl-'+name+'-example.png')
        image.save(path);image.close();print(path.resolve())


if __name__=='__main__':main()
