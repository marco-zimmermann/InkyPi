#!/usr/bin/env python3
"""Offline fake bookings only; never reads a secret or fetches a subscription."""
import argparse
from datetime import datetime,timedelta
from pathlib import Path
from plugins.flight_duties.calendar_data import schedule_for
from plugins.flight_duties.rendering import render_dashboard

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'mock_display_output/flight-duties')
    args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
    now=datetime.fromisoformat('2026-10-26T10:15:00+01:00')
    raw=(ROOT/'tests/fixtures/flight_duties/synthetic.ics').read_bytes()
    empty=b'BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR'
    tomorrow=raw.replace(b'20261026',b'20261027').replace(b'20261025',b'20261026')
    evening=now.replace(hour=18,minute=0)
    cases=[('live',raw,now,'live'),('cached',raw,now,'cached'),
           ('unavailable',raw,now,'unavailable'),
           ('tomorrow',tomorrow,evening,'live'),
           ('tomorrow-cached',tomorrow,evening,'cached'),
           ('today-empty',empty,now,'live'),('tomorrow-empty',empty,evening,'live')]
    for name,data,at,source in cases:
        report=dict(schedule=schedule_for(data,at) if source!='unavailable' else None,source=source,
                    fetched_at=(at-timedelta(minutes=45) if source=='cached' else
                                at if source=='live' else None),
                    error='Network unavailable' if source!='live' else None)
        path=args.output_dir/(name+'.png')
        with render_dashboard(report,at,{'title':'TEST: Daily Flight Duties'}) as image:image.save(path)
        print(path.resolve())

    with render_dashboard(dict(schedule=schedule_for(raw,now),source='live',fetched_at=now),
                          now,{'title':'TEST: Daily Flight Duties','max_bookings':7}) as image:
        image.save(args.output_dir/'legacy-six.png')


if __name__=='__main__':main()
