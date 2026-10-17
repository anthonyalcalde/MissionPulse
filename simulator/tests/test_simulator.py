import json
import math
import unittest
from datetime import datetime, timezone
from simulator import FlightSimulator, SimulationConfig

ORIGIN = datetime(2026, 10, 17, tzinfo=timezone.utc)
def simulator(**kwargs):
    return FlightSimulator(start_time=ORIGIN, **kwargs)

class FlightTests(unittest.TestCase):
    def test_contract_and_complete_flight(self):
        sim = simulator()
        phases = set()
        maximum = previous = 0
        for _ in range(6000):
            r = sim.step()
            self.assertEqual(set(r), {'mission_id', 'timestamp', 'elapsed_seconds', 'altitude', 'velocity', 'acceleration', 'temperature', 'pressure', 'flight_phase'})
            self.assertTrue(r['timestamp'].endswith('Z'))
            self.assertGreater(r['elapsed_seconds'], previous)
            previous = r['elapsed_seconds']
            self.assertGreaterEqual(r['altitude'], 0)
            self.assertGreater(r['pressure'], 0)
            json.dumps(r, allow_nan=False)
            phases.add(r['flight_phase'])
            maximum = max(maximum, r['altitude'])
            if r['flight_phase'] == 'landed': break
        self.assertEqual(phases, {'preflight', 'ascent', 'coast', 'apogee', 'descent', 'landed'})
        self.assertGreater(maximum, 100)
        self.assertEqual(r['velocity'], 0)

    def test_analytic_no_drag_powered_flight(self):
        sim = simulator(config=SimulationConfig(preflight_seconds=0, drag_area=0))
        r = sim.step(1)
        a = sim.config.thrust / sim.config.mass - sim.config.gravity
        self.assertAlmostEqual(r['velocity'], a, places=8)
        self.assertAlmostEqual(r['altitude'], a / 2, places=8)

    def test_controls(self):
        sim = simulator()
        r = sim.step()
        sim.pause()
        self.assertEqual(sim.step(), r)
        sim.resume()
        self.assertGreater(sim.step()['elapsed_seconds'], r['elapsed_seconds'])
        sim.stop()
        t = sim.elapsed_seconds
        sim.step()
        self.assertEqual(sim.elapsed_seconds, t)
        sim.reset()
        self.assertEqual(sim.step(), r)

    def test_determinism_and_speed(self):
        c = SimulationConfig(noise_fraction=0.01)
        a, b = simulator(config=c), simulator(config=c)
        b.set_speed(10)
        for _ in range(50): self.assertEqual(a.step(), b.step())
        a.reset()
        self.assertEqual(a.step(), simulator(config=c).step())

    def test_anomaly_is_sensor_only_and_expires(self):
        a, b = simulator(), simulator()
        a.inject_anomaly('temperature', 60, 0.25)
        for i in range(4):
            ra, rb = a.step(), b.step()
            self.assertEqual(ra['altitude'], rb['altitude'])
            self.assertAlmostEqual(ra['temperature'] - rb['temperature'], 60 if i < 2 else 0)

    def test_timestep_convergence(self):
        a = simulator(config=SimulationConfig(integration_step=0.01))
        b = simulator(config=SimulationConfig(integration_step=0.005))
        for _ in range(100):
            a.step()
            b.step()
        self.assertLess(abs(a.altitude - b.altitude), 1)

    def test_invalid_inputs(self):
        for dt in (0, -1, math.nan, math.inf, 61):
            with self.assertRaises(ValueError): simulator().step(dt)
        with self.assertRaises(ValueError): FlightSimulator(start_time=datetime(2026, 1, 1))
        with self.assertRaises(ValueError): SimulationConfig(mass=0)
        with self.assertRaises(ValueError): simulator().inject_anomaly('unknown')

if __name__ == '__main__': unittest.main()
