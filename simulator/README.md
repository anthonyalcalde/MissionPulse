# MissionPulse simulator

Python 3.10+; the flight model uses only the standard library. Run commands from the MissionPulse repository root.

## Run from the repository root

```bash
python -m simulator.demo --speed 5 --anomaly-at 15
python -m simulator.demo --fast --mission-id mission_demo --start-time 2026-10-17T14:00:00Z > mission.jsonl
python -m simulator.demo --mission-id mission_live --api-url http://localhost:8000 --speed 1
python -m pytest simulator/tests -q
```

The first command prints JSON readings in real time at 5x speed; the second writes a reproducible JSONL flight without delays. The third sends readings sequentially to `POST /api/telemetry`; it requires the backend and database to be ready. Add `--fast` for an unpaced API demo (POSTs remain sequential), or `--retries N` to change the transient-error retry limit. Failed delivery exits with an error; the runner does not continue after dropping a record. Stop with Ctrl+C. Default output interval is 0.1 simulated seconds. Use `--dt` to change it. Each JSON line is one reading, not a JSON array. Messages about injection and ingestion progress go to stderr.

## Python interface

```python
from simulator import FlightSimulator, SimulationConfig

sim = FlightSimulator(mission_id='mission_001')
reading = sim.step(dt=0.1)
sim.pause()
sim.resume()
sim.set_speed(5)
sim.inject_anomaly(metric='temperature', magnitude=60, duration=2)
sim.stop()
sim.reset(mission_id='mission_002')
```

First `step()` auto-starts. `start()` can also explicitly start. Pause/stop freeze elapsed time; do not ingest repeated frozen readings. Stopping requires reset before restarting. Reset rewinds the RNG and time origin; provide a fresh mission ID and optionally a timezone-aware `start_time` for a new database mission. Reset without arguments is an exact replay. Speed changes runner pacing (`sleep = dt / speed`), not flight equations or timestamps. The default `step()` interval is 0.1; the demo explicitly passes the configured sample interval.

All mutable simulator methods should be called by one owner/task. It is not thread-safe. The repository demo runner owns pacing and optional sequential HTTP ingestion. Timestamp = origin + simulated elapsed time; at accelerated speed it can be ahead of wall time. Pauses do not create gaps in simulated time.

## Physics you can explain to judges

Positive altitude and velocity point upward. Net acceleration is:

`a = (thrust - 0.5 * density * CdA * v * abs(v)) / mass - gravity`

Thrust acts for 8 seconds after a 2-second preflight. Drag opposes velocity, so it acts upward during descent. Density and pressure decrease exponentially with altitude. Temperature decreases 0.0065 degrees Celsius per meter. At the first upward-to-downward velocity crossing, a larger drag area represents parachute deployment.

Internally, steps are split into at most 0.01 seconds and at preflight/burnout boundaries. Each substep updates velocity from current acceleration, then updates altitude using average old/new velocity. This is exact for constant acceleration, but only approximate for velocity-dependent drag. The convergence test compares 0.01 and 0.005 second integration. Ground contact clamps height, velocity and acceleration to zero; the impact impulse is not modeled.

Phases: `preflight`, `ascent` (powered), `coast` (unpowered upward motion), `apogee`, `descent`, `landed`. Apogee is exposed on the output interval containing the velocity crossing, even if the true crossing occurs between readings. Very large output intervals can skip short phases; 0.1 seconds is recommended. Acceleration is vertical coordinate acceleration, not an accelerometer's proper acceleration; resting readings are zero. Elapsed time includes preflight.

Fixed mass, constant thrust/gravity, no wind, no rotation, no fuel consumption, no heating model, and instantaneous parachute deployment: this is an educational synthetic flight, not flight-control software or a validated rocket design tool.

## Noise and anomalies

Default noise is zero for a clean demo. Set `SimulationConfig(noise_fraction=0.01, seed=42)` for seeded Gaussian sensor noise. Repeatability requires the same start time, seed, configuration, call sequence, and output intervals. Noise can produce negative sensor altitude; true physical altitude stays nonnegative.

`inject_anomaly` adds a configurable bias to one measured field for simulated seconds. It does not alter flight dynamics. Bias expires before the reading at its end time. Multiple active biases add. It can intentionally yield impossible sensor values. The dictionary keeps exactly the agreed telemetry keys, with no extra anomaly flag. Detecting and displaying alerts remains the backend/frontend owners' responsibility.

## Backend integration

Each returned dictionary supplies the request fields expected by `TelemetryPayload`: mission ID, UTC ISO 8601 timestamp, elapsed seconds, altitude (m), velocity (m/s), acceleration (m/s²), temperature (Celsius), pressure (**Pa**), lowercase flight phase. The optional HTTP bridge is in `simulator/ingest.py`; the simulator does not connect to the database directly.

The demo runner posts each reading to `POST /api/telemetry`; the backend commits each record before WebSocket broadcast. Retrying a record is safe for the same `(mission_id, timestamp)` because the API uses an idempotent upsert. The runner has no start/pause/resume/reset HTTP controls; do not imply those controls exist.

The repository API implements ingestion, historical telemetry, time-bucket statistics, and a live telemetry WebSocket. Mission listing, dedicated downsampling, and simulation-control routes are not implemented. Statistics are TimescaleDB `time_bucket` aggregations, not LTTB downsampling. The API contract fixes pressure to Pa and enforces the documented lowercase flight phases.

`python -m unittest discover -s simulator/tests -v` runs the standard-library physics tests. The pytest suite also covers the HTTP bridge's request schema, retry behavior, and explicit failures. API delivery tests in `backend/tests` mock database access; they do not establish live database or Tiger Data connectivity.
