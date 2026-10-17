"""Unit tests for MissionPulse simulation runner using mocked HTTP requests.

These tests verify runner orchestration, telemetry payload structure,
anomaly injection, error handling, and interruption handling without
requiring a running server or Tiger Cloud database credentials.
"""

from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch
from typing import Any, Dict, List

from backend.simulation_runner import SimulationRunner, TelemetrySender, parse_args


class TestSimulationRunner(unittest.TestCase):

    def test_telemetry_sender_url_normalization(self) -> None:
        """Verify TelemetrySender normalizes various base URLs to /api/telemetry."""
        cases = [
            ("http://127.0.0.1:8000", "http://127.0.0.1:8000/api/telemetry"),
            ("http://127.0.0.1:8000/", "http://127.0.0.1:8000/api/telemetry"),
            ("127.0.0.1:8000", "http://127.0.0.1:8000/api/telemetry"),
            ("http://localhost:8000/api/telemetry", "http://localhost:8000/api/telemetry"),
        ]
        for input_url, expected in cases:
            sender = TelemetrySender(endpoint_url=input_url)
            self.assertEqual(sender.endpoint_url, expected)

    def test_full_flight_mocked_success(self) -> None:
        """Verify full flight execution and schema compliance with mocked HTTP 201."""
        sent_readings: List[Dict[str, Any]] = []

        def mock_http_client(reading: Dict[str, Any]):
            sent_readings.append(reading)
            return True, None

        sender = TelemetrySender(http_client=mock_http_client)
        runner = SimulationRunner(
            mission_id="test_mission_001",
            dt=0.1,
            fast=True,
            sender=sender,
            verbose=False,
        )

        summary = runner.run()

        # Flight completion checks
        self.assertTrue(summary["flight_completed"])
        self.assertFalse(summary["interrupted"])
        self.assertEqual(summary["mission_id"], "test_mission_001")
        self.assertEqual(summary["final_phase"], "landed")
        self.assertGreater(summary["total_readings"], 50)
        self.assertEqual(summary["successful_readings"], summary["total_readings"])
        self.assertEqual(summary["failed_readings"], 0)
        self.assertEqual(len(summary["errors"]), 0)
        self.assertGreater(summary["max_altitude"], 100.0)

        # Verify every reading conforms to Pydantic TelemetryPayload schema
        expected_keys = {
            "mission_id",
            "timestamp",
            "elapsed_seconds",
            "altitude",
            "velocity",
            "acceleration",
            "temperature",
            "pressure",
            "flight_phase",
        }
        for r in sent_readings:
            self.assertEqual(set(r.keys()), expected_keys)
            self.assertEqual(r["mission_id"], "test_mission_001")
            self.assertTrue(r["timestamp"].endswith("Z"))
            self.assertIsInstance(r["elapsed_seconds"], (int, float))
            self.assertIsInstance(r["altitude"], (int, float))
            self.assertIsInstance(r["velocity"], (int, float))
            self.assertIsInstance(r["acceleration"], (int, float))
            self.assertIsInstance(r["temperature"], (int, float))
            self.assertIsInstance(r["pressure"], (int, float))
            self.assertIn(
                r["flight_phase"],
                {"preflight", "ascent", "coast", "apogee", "descent", "landed"},
            )

    def test_anomaly_injection_transmitted(self) -> None:
        """Verify anomaly bias is injected and transmitted in the HTTP payload."""
        sent_readings: List[Dict[str, Any]] = []

        def mock_http_client(reading: Dict[str, Any]):
            sent_readings.append(reading)
            return True, None

        sender = TelemetrySender(http_client=mock_http_client)
        runner = SimulationRunner(
            mission_id="test_anomaly_mission",
            dt=0.1,
            fast=True,
            anomaly_at=2.0,
            anomaly_metric="temperature",
            anomaly_magnitude=80.0,
            anomaly_duration=1.0,
            sender=sender,
            verbose=False,
        )

        summary = runner.run()
        self.assertTrue(summary["flight_completed"])

        # Check readings during the active fault window (t=2.1s to t=2.9s)
        faulted_readings = [
            r for r in sent_readings if 2.05 <= r["elapsed_seconds"] <= 2.95
        ]
        self.assertGreater(len(faulted_readings), 0)
        for r in faulted_readings:
            # Sea level base temperature is 20 C minus altitude lapse; with +80 bias it should be > 90 C
            self.assertGreater(r["temperature"], 90.0)

        # Readings after expiry (e.g. t >= 3.5s) should return to normal ambient temperatures
        post_fault = [
            r for r in sent_readings if 3.5 <= r["elapsed_seconds"] < 4.5
        ]
        self.assertGreater(len(post_fault), 0)
        for r in post_fault:
            self.assertLess(r["temperature"], 40.0)

    def test_http_failures_not_silently_discarded(self) -> None:
        """Verify HTTP failures are recorded and reflected in summary without silent drops."""
        step_count = 0

        def failing_http_client(reading: Dict[str, Any]):
            nonlocal step_count
            step_count += 1
            # Alternate success and failure
            if step_count % 2 == 0:
                return False, "HTTP 500 (Internal Server Error): DB timeout"
            return True, None

        sender = TelemetrySender(http_client=failing_http_client)
        runner = SimulationRunner(
            mission_id="test_fail_mission",
            dt=0.1,
            fast=True,
            max_consecutive_errors=1000,  # Don't abort early for this test
            sender=sender,
            verbose=False,
        )

        summary = runner.run()

        self.assertGreater(summary["failed_readings"], 0)
        self.assertGreater(summary["successful_readings"], 0)
        self.assertEqual(
            summary["total_readings"],
            summary["successful_readings"] + summary["failed_readings"],
        )
        self.assertGreater(len(summary["errors"]), 0)
        self.assertIn("DB timeout", summary["errors"][0]["error"])

    def test_consecutive_errors_abort(self) -> None:
        """Verify runner aborts when exceeding max consecutive ingestion errors."""

        def down_http_client(reading: Dict[str, Any]):
            return False, "Connection failure: [Errno 61] Connection refused"

        sender = TelemetrySender(http_client=down_http_client)
        runner = SimulationRunner(
            mission_id="test_abort_mission",
            dt=0.1,
            fast=True,
            max_consecutive_errors=5,
            sender=sender,
            verbose=False,
        )

        with self.assertRaises(RuntimeError) as ctx:
            runner.run()

        self.assertIn("Exceeded 5 consecutive HTTP ingestion errors", str(ctx.exception))

    def test_keyboard_interrupt_clean_handling(self) -> None:
        """Verify KeyboardInterrupt gracefully stops the simulator and marks summary."""

        calls = 0

        def interrupting_http_client(reading: Dict[str, Any]):
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise KeyboardInterrupt()
            return True, None

        sender = TelemetrySender(http_client=interrupting_http_client)
        runner = SimulationRunner(
            mission_id="test_sigint_mission",
            dt=0.1,
            fast=True,
            sender=sender,
            verbose=False,
        )

        summary = runner.run()

        self.assertTrue(summary["interrupted"])
        self.assertFalse(summary["flight_completed"])
        self.assertEqual(summary["total_readings"], 3)
        self.assertEqual(runner.sim.status, "stopped")

    def test_cli_argument_parsing(self) -> None:
        """Verify CLI flags are correctly parsed into configuration."""
        args = parse_args(
            [
                "--mission-id",
                "custom_rocket_42",
                "--speed",
                "5.5",
                "--dt",
                "0.05",
                "--fast",
                "--seed",
                "99",
                "--noise",
                "0.02",
                "--anomaly-at",
                "12.5",
                "--anomaly-metric",
                "velocity",
                "--anomaly-magnitude",
                "-20.0",
                "--anomaly-duration",
                "3.5",
            ]
        )
        self.assertEqual(args.mission_id, "custom_rocket_42")
        self.assertEqual(args.speed, 5.5)
        self.assertEqual(args.dt, 0.05)
        self.assertTrue(args.fast)
        self.assertEqual(args.seed, 99)
        self.assertEqual(args.noise, 0.02)
        self.assertEqual(args.anomaly_at, 12.5)
        self.assertEqual(args.anomaly_metric, "velocity")
        self.assertEqual(args.anomaly_magnitude, -20.0)
        self.assertEqual(args.anomaly_duration, 3.5)


if __name__ == "__main__":
    unittest.main()
