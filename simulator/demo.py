"""JSONL demo. HTTP ingestion belongs to the backend teammate's runner."""
import argparse
from datetime import datetime, timezone
import json
import sys
import time
from . import FlightSimulator, SimulationConfig
from .ingest import post_reading

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mission-id', default=None)
    parser.add_argument('--dt', type=float, default=0.1)
    parser.add_argument('--speed', type=float, default=1)
    parser.add_argument('--fast', action='store_true', help='No wall-clock delay')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--start-time', help='Timezone-aware ISO 8601 time for reproducibility')
    parser.add_argument('--anomaly-at', type=float, default=None)
    parser.add_argument('--api-url', help='POST readings to this MissionPulse API origin instead of stdout JSONL')
    parser.add_argument('--retries', type=int, default=5, help='Maximum retry attempts per reading (default: 5)')
    args = parser.parse_args()
    if args.retries < 0:
        parser.error('--retries must be nonnegative')
    if args.api_url and args.fast:
        print('Fast ingestion mode enabled; requests remain sequential and retry before advancing.', file=sys.stderr)
    origin = datetime.fromisoformat(args.start_time.replace('Z', '+00:00')) if args.start_time else datetime.now(timezone.utc)
    sim = FlightSimulator(args.mission_id or 'mission_' + origin.strftime('%Y%m%dT%H%M%S%f'),
                          SimulationConfig(sample_interval=args.dt, speed=args.speed, seed=args.seed), origin)
    injected = False
    records_sent = 0
    try:
        while not sim._landed and sim.elapsed_seconds < 600:
            deadline = time.monotonic() + args.dt / sim.speed
            if args.anomaly_at is not None and not injected and sim.elapsed_seconds >= args.anomaly_at:
                sim.inject_anomaly()
                injected = True
                print('Injected temperature sensor bias', file=sys.stderr)
            reading = sim.step(args.dt)
            if args.api_url:
                post_reading(args.api_url, reading, retries=args.retries)
                records_sent += 1
                if records_sent % 100 == 0:
                    print(f'Ingested {records_sent} readings for {sim.mission_id}', file=sys.stderr)
            else:
                print(json.dumps(reading, allow_nan=False), flush=True)
            if not args.fast:
                time.sleep(max(0, deadline - time.monotonic()))
        if not sim._landed:
            raise SystemExit('Simulation exceeded 600 seconds without landing')
        if args.api_url:
            print(f'Ingested {records_sent} readings for {sim.mission_id}; flight landed.', file=sys.stderr)
    except KeyboardInterrupt:
        sim.stop()

if __name__ == '__main__':
    main()
