"""MissionPulse Telemetry Simulation Runner.

Continuously runs the educational FlightSimulator physics model and streams
generated telemetry readings to the FastAPI backend ingestion endpoint
(POST /api/telemetry) over HTTP.

Successful ingestions are stored in Tiger Cloud TimescaleDB and broadcast
in real time to connected WebSocket dashboard clients.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

# Ensure repository root is in sys.path so simulator can be imported
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from simulator import FlightSimulator, SimulationConfig
from simulator.telemetry import METRICS


class TelemetrySender:
    """Sends telemetry readings to the FastAPI backend over HTTP."""

    def __init__(
        self,
        endpoint_url: str = "http://127.0.0.1:8000/api/telemetry",
        timeout: float = 5.0,
        http_client: Optional[Callable[[Dict[str, Any]], Tuple[bool, Optional[str]]]] = None,
    ) -> None:
        url = endpoint_url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"http://{url}"
        if not url.endswith("/api/telemetry"):
            url = f"{url.rstrip('/')}/api/telemetry"
        self.endpoint_url = url
        self.timeout = timeout
        self._http_client = http_client

    def send(self, reading: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Post a single telemetry reading as JSON.

        Returns:
            Tuple of (success: bool, error_message: Optional[str]).
        """
        if self._http_client is not None:
            return self._http_client(reading)

        try:
            payload_bytes = json.dumps(reading, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError) as err:
            return False, f"JSON serialization error: {err}"

        req = urllib.request.Request(
            self.endpoint_url,
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "MissionPulse-SimulationRunner/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status in (200, 201):
                    return True, None
                return False, f"Unexpected HTTP status {resp.status}"
        except urllib.error.HTTPError as err:
            try:
                body = err.read().decode("utf-8")
            except Exception:
                body = ""
            return False, f"HTTP {err.code} ({err.reason}): {body.strip()}"
        except urllib.error.URLError as err:
            return False, f"Connection failure: {err.reason}"
        except TimeoutError:
            return False, f"Request timed out after {self.timeout}s"
        except Exception as err:
            return False, f"Network error ({type(err).__name__}): {err}"


class SimulationRunner:
    """Orchestrates flight simulation and telemetry ingestion."""

    def __init__(
        self,
        mission_id: Optional[str] = None,
        api_base_url: str = "http://127.0.0.1:8000",
        dt: float = 0.1,
        speed: float = 1.0,
        fast: bool = False,
        seed: int = 42,
        noise_fraction: float = 0.0,
        start_time: Optional[datetime] = None,
        anomaly_at: Optional[float] = None,
        anomaly_metric: str = "temperature",
        anomaly_magnitude: float = 60.0,
        anomaly_duration: float = 2.0,
        max_duration: float = 600.0,
        timeout: float = 5.0,
        max_consecutive_errors: int = 10,
        sender: Optional[TelemetrySender] = None,
        verbose: bool = True,
    ) -> None:
        self.origin = start_time or datetime.now(timezone.utc)
        if self.origin.tzinfo is None or self.origin.utcoffset() is None:
            raise ValueError("start_time must be timezone-aware")
        self.origin = self.origin.astimezone(timezone.utc)

        self.mission_id = (
            mission_id.strip()
            if (mission_id and mission_id.strip())
            else f"mission_{self.origin.strftime('%Y%m%dT%H%M%S%f')}"
        )
        self.dt = dt
        self.speed = speed
        self.fast = fast
        self.seed = seed
        self.noise_fraction = noise_fraction
        self.anomaly_at = anomaly_at
        self.anomaly_metric = anomaly_metric
        self.anomaly_magnitude = anomaly_magnitude
        self.anomaly_duration = anomaly_duration
        self.max_duration = max_duration
        self.max_consecutive_errors = max_consecutive_errors
        self.verbose = verbose

        self.config = SimulationConfig(
            sample_interval=dt,
            speed=speed,
            seed=seed,
            noise_fraction=noise_fraction,
        )
        self.sim = FlightSimulator(
            mission_id=self.mission_id,
            config=self.config,
            start_time=self.origin,
        )
        self.sender = sender or TelemetrySender(endpoint_url=api_base_url, timeout=timeout)

    def run(self) -> Dict[str, Any]:
        """Execute the simulation loop until rocket landing or max duration.

        Returns:
            Dict containing flight statistics, counts, and error summaries.
        """
        if self.verbose:
            self._print_header()

        injected = False
        total_readings = 0
        successful_readings = 0
        failed_readings = 0
        consecutive_errors = 0
        errors: List[Dict[str, Any]] = []
        max_altitude = 0.0
        max_velocity = 0.0
        previous_phase = None
        interrupted = False
        last_progress_print = 0.0

        try:
            while not self.sim._landed and self.sim.elapsed_seconds < self.max_duration:
                deadline = time.monotonic() + (self.dt / self.sim.speed)

                # Inject planned sensor anomaly
                if (
                    self.anomaly_at is not None
                    and not injected
                    and self.sim.elapsed_seconds >= self.anomaly_at - 1e-9
                ):
                    self.sim.inject_anomaly(
                        metric=self.anomaly_metric,
                        magnitude=self.anomaly_magnitude,
                        duration=self.anomaly_duration,
                    )
                    injected = True
                    if self.verbose:
                        print(
                            f"\n[ANOMALY INJECTED] t={self.sim.elapsed_seconds:.2f}s | "
                            f"{self.anomaly_metric} bias: {self.anomaly_magnitude:+0.1f} "
                            f"for {self.anomaly_duration:.1f}s",
                            flush=True,
                        )

                # Step simulation
                reading = self.sim.step(self.dt)
                total_readings += 1

                alt = reading.get("altitude", 0.0)
                vel = reading.get("velocity", 0.0)
                phase = reading.get("flight_phase", "unknown")
                if alt > max_altitude:
                    max_altitude = alt
                if vel > max_velocity:
                    max_velocity = vel

                # Print phase transition
                if self.verbose and phase != previous_phase:
                    previous_phase = phase
                    print(
                        f"\n[PHASE] t={self.sim.elapsed_seconds:5.1f}s | "
                        f"{phase:<9} | alt={alt:7.1f}m | vel={vel:+6.1f}m/s",
                        flush=True,
                    )

                # Send reading to ingestion API
                success, err_msg = self.sender.send(reading)
                if success:
                    successful_readings += 1
                    consecutive_errors = 0
                else:
                    failed_readings += 1
                    consecutive_errors += 1
                    errors.append(
                        {
                            "elapsed_seconds": reading.get("elapsed_seconds"),
                            "phase": phase,
                            "error": err_msg,
                        }
                    )
                    if self.verbose:
                        print(
                            f"\n[INGESTION ERROR] t={self.sim.elapsed_seconds:.1f}s: {err_msg}",
                            file=sys.stderr,
                            flush=True,
                        )
                    if consecutive_errors >= self.max_consecutive_errors:
                        raise RuntimeError(
                            f"Aborting mission: Exceeded {self.max_consecutive_errors} "
                            f"consecutive HTTP ingestion errors. Last error: {err_msg}"
                        )

                # Terminal progress update (approx. once per simulated second)
                if self.verbose and (self.sim.elapsed_seconds - last_progress_print >= 1.0 or self.sim._landed):
                    last_progress_print = self.sim.elapsed_seconds
                    print(
                        f"\r >> [{phase:<8}] t={self.sim.elapsed_seconds:5.1f}s | "
                        f"alt={alt:7.1f}m | vel={vel:+6.1f}m/s | "
                        f"sent={total_readings} (ok={successful_readings}, err={failed_readings})",
                        end="",
                        flush=True,
                    )

                # Wall-clock pacing
                if not self.fast and not self.sim._landed:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)

        except KeyboardInterrupt:
            interrupted = True
            self.sim.stop()
            if self.verbose:
                print("\n[INTERRUPTED] Simulation paused and aborted by user (Ctrl+C).", flush=True)

        summary = {
            "mission_id": self.mission_id,
            "flight_completed": self.sim._landed,
            "interrupted": interrupted,
            "total_readings": total_readings,
            "successful_readings": successful_readings,
            "failed_readings": failed_readings,
            "elapsed_seconds": round(self.sim.elapsed_seconds, 2),
            "max_altitude": round(max_altitude, 2),
            "max_velocity": round(max_velocity, 2),
            "final_phase": self.sim.flight_phase,
            "errors": errors,
        }

        if self.verbose:
            self._print_summary(summary)

        return summary

    def _print_header(self) -> None:
        print("=" * 72)
        print("MissionPulse Telemetry Simulation Runner")
        print("=" * 72)
        print(f"Mission ID   : {self.mission_id}")
        print(f"Target URL   : {self.sender.endpoint_url}")
        print(f"Sample dt    : {self.dt:.2f}s")
        print(f"Speed Ratio  : {'FAST (no delay)' if self.fast else f'{self.speed:.1f}x'}")
        print(f"Noise / Seed : {self.noise_fraction * 100:.1f}% / {self.seed}")
        if self.anomaly_at is not None:
            print(
                f"Planned Fault: {self.anomaly_metric} bias ({self.anomaly_magnitude:+0.1f}) "
                f"for {self.anomaly_duration:.1f}s at t={self.anomaly_at:.1f}s"
            )
        else:
            print("Planned Fault: None")
        print("=" * 72, flush=True)

    def _print_summary(self, summary: Dict[str, Any]) -> None:
        print("\n" + "=" * 72)
        status_label = (
            "INTERRUPTED"
            if summary["interrupted"]
            else ("SUCCESS" if summary["flight_completed"] and summary["failed_readings"] == 0 else "FAILED")
        )
        print(f"Simulation Summary: [{status_label}]")
        print("=" * 72)
        print(f"Mission ID         : {summary['mission_id']}")
        print(f"Final Flight Phase : {summary['final_phase']}")
        print(f"Simulated Duration : {summary['elapsed_seconds']:.1f}s")
        print(f"Maximum Altitude   : {summary['max_altitude']:.1f} m")
        print(f"Maximum Velocity   : {summary['max_velocity']:.1f} m/s")
        print(f"Total Readings Sent: {summary['total_readings']}")
        print(f"Successful Ingests : {summary['successful_readings']}")
        print(f"Failed Ingests     : {summary['failed_readings']}")
        if summary["errors"]:
            print(f"Total Errors Logged: {len(summary['errors'])}")
            print(f"First Error Detail : {summary['errors'][0]['error']}")
        print("=" * 72, flush=True)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MissionPulse Rocket Telemetry Simulation & Ingestion Runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mission-id",
        type=str,
        default=None,
        help="Unique mission identifier (defaults to mission_<UTC_TIMESTAMP>)",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default="http://127.0.0.1:8000",
        help="FastAPI base URL or full endpoint URL",
    )
    parser.add_argument(
        "--dt",
        type=float,
        default=0.1,
        help="Simulation sample interval in seconds",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Simulation speed multiplier (e.g. 5.0 for 5x real-time)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Run without any wall-clock delay (as fast as HTTP permits)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for simulation repeatability",
    )
    parser.add_argument(
        "--noise",
        type=float,
        default=0.0,
        help="Sensor noise fraction (e.g. 0.01 for 1%% Gaussian noise)",
    )
    parser.add_argument(
        "--start-time",
        type=str,
        default=None,
        help="Timezone-aware ISO 8601 start time (e.g. 2026-10-17T12:00:00Z)",
    )
    parser.add_argument(
        "--anomaly-at",
        type=float,
        default=None,
        help="Elapsed simulated seconds at which to inject an anomaly",
    )
    parser.add_argument(
        "--anomaly-metric",
        type=str,
        choices=METRICS,
        default="temperature",
        help="Metric to bias during anomaly injection",
    )
    parser.add_argument(
        "--anomaly-magnitude",
        type=float,
        default=60.0,
        help="Magnitude of sensor bias to inject",
    )
    parser.add_argument(
        "--anomaly-duration",
        type=float,
        default=2.0,
        help="Duration of anomaly in simulated seconds",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP request timeout in seconds",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=600.0,
        help="Safety cutoff for flight simulation in seconds",
    )
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=10,
        help="Maximum consecutive ingestion failures before aborting",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    start_time = None
    if args.start_time:
        try:
            start_time = datetime.fromisoformat(args.start_time.replace("Z", "+00:00"))
        except ValueError as err:
            print(f"Error parsing --start-time: {err}", file=sys.stderr)
            return 2

    runner = SimulationRunner(
        mission_id=args.mission_id,
        api_base_url=args.api_url,
        dt=args.dt,
        speed=args.speed,
        fast=args.fast,
        seed=args.seed,
        noise_fraction=args.noise,
        start_time=start_time,
        anomaly_at=args.anomaly_at,
        anomaly_metric=args.anomaly_metric,
        anomaly_magnitude=args.anomaly_magnitude,
        anomaly_duration=args.anomaly_duration,
        max_duration=args.max_duration,
        timeout=args.timeout,
        max_consecutive_errors=args.max_consecutive_errors,
        verbose=True,
    )

    summary = runner.run()

    if summary["interrupted"]:
        return 130
    if summary["failed_readings"] > 0 or not summary["flight_completed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
