"""Pure Python vertical flight model. No networking or database dependencies."""
from datetime import datetime, timezone
import math
import random
from .config import SimulationConfig
from .telemetry import METRICS, make_reading

class FlightSimulator:
    def __init__(self, mission_id='mission_001', config=None, start_time=None):
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError('mission_id must be a nonempty string')
        self.mission_id = mission_id
        self.config = config or SimulationConfig()
        origin = start_time or datetime.now(timezone.utc)
        if origin.tzinfo is None or origin.utcoffset() is None:
            raise ValueError('start_time must include a timezone')
        self.start_time = origin.astimezone(timezone.utc)
        self.speed = self.config.speed
        self.reset()

    def reset(self, mission_id=None, start_time=None):
        """Rewind with the same seed/origin. Use a new mission ID for another DB run."""
        if mission_id is not None:
            if not isinstance(mission_id, str) or not mission_id.strip():
                raise ValueError('mission_id must be a nonempty string')
            self.mission_id = mission_id
        if start_time is not None:
            if start_time.tzinfo is None or start_time.utcoffset() is None:
                raise ValueError('start_time must include a timezone')
            self.start_time = start_time.astimezone(timezone.utc)
        self.elapsed_seconds = 0.0
        self.altitude = self.velocity = self.acceleration = 0.0
        self.flight_phase = 'preflight'
        self.status = 'ready'
        self._descending = self._landed = False
        self._rng = random.Random(self.config.seed)
        self._faults = []
        self._last = None

    def start(self):
        if self.status in ('ready', 'paused'):
            self.status = 'running'
        elif self.status == 'stopped':
            raise RuntimeError('reset before restarting a stopped simulator')

    def pause(self):
        if self.status == 'running':
            self.status = 'paused'

    def resume(self):
        if self.status == 'paused':
            self.status = 'running'

    def stop(self):
        self.status = 'stopped'

    def set_speed(self, speed):
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError('speed must be finite and positive')
        self.speed = speed  # Runner uses dt / speed for wall-clock pacing.

    def inject_anomaly(self, metric='temperature', magnitude=60.0, duration=2.0):
        """Add a sensor bias for duration simulated seconds; does not alter physics."""
        if metric not in METRICS:
            raise ValueError(f'metric must be one of {METRICS}')
        if not math.isfinite(magnitude) or not math.isfinite(duration) or duration <= 0:
            raise ValueError('magnitude must be finite and duration positive')
        self._faults.append((metric, magnitude, self.elapsed_seconds + duration))

    def _advance(self, h):
        c = self.config
        if self._landed:
            self.elapsed_seconds += h
            self.flight_phase = 'landed'
            return
        if self.elapsed_seconds < c.preflight_seconds - 1e-10:
            self.elapsed_seconds += h
            return
        powered = self.elapsed_seconds < c.preflight_seconds + c.burn_seconds - 1e-10
        area = c.parachute_drag_area if self._descending else c.drag_area
        density = c.sea_level_density * math.exp(-self.altitude / c.scale_height)
        drag = 0.5 * density * area * self.velocity * abs(self.velocity)
        self.acceleration = ((c.thrust if powered else 0.0) - drag) / c.mass - c.gravity
        old_v = self.velocity
        self.velocity += self.acceleration * h
        self.altitude += (old_v + self.velocity) * 0.5 * h
        self.elapsed_seconds += h
        if not self._descending and old_v > 0 and self.velocity <= 0:
            self._descending = True
            self.flight_phase = 'apogee'
        elif self._descending:
            self.flight_phase = 'descent'
        else:
            self.flight_phase = 'ascent' if powered else 'coast'
        if self.altitude <= 0 and self._descending:
            self.altitude = self.velocity = self.acceleration = 0.0
            self._landed = True
            self.flight_phase = 'landed'

    def step(self, dt=0.1):
        """Advance dt simulated seconds. First step auto-starts. Paused steps freeze."""
        if not math.isfinite(dt) or dt <= 0 or dt > 60:
            raise ValueError('dt must be finite and in (0, 60] seconds')
        if self.status in ('paused', 'stopped'):
            return dict(self._last) if self._last is not None else self._reading()
        self.start()
        target = self.elapsed_seconds + dt
        saw_apogee = False
        while self.elapsed_seconds < target - 1e-10:
            h = min(self.config.integration_step, target - self.elapsed_seconds)
            for boundary in (self.config.preflight_seconds,
                             self.config.preflight_seconds + self.config.burn_seconds):
                if self.elapsed_seconds < boundary - 1e-10:
                    h = min(h, boundary - self.elapsed_seconds)
            self._advance(h)
            saw_apogee |= self.flight_phase == 'apogee'
        self.elapsed_seconds = target
        if saw_apogee and not self._landed:
            self.flight_phase = 'apogee'  # Expose crossing even between output samples.
        self._last = self._reading()
        return dict(self._last)

    def _reading(self):
        c = self.config
        values = dict(altitude=self.altitude, velocity=self.velocity,
                      acceleration=self.acceleration,
                      temperature=c.sea_level_temperature - 0.0065 * self.altitude,
                      pressure=c.sea_level_pressure * math.exp(-self.altitude / c.scale_height))
        if c.noise_fraction:
            values = {k: v + self._rng.gauss(0, max(abs(v), 1) * c.noise_fraction)
                      for k, v in values.items()}
        self._faults = [f for f in self._faults if self.elapsed_seconds < f[2]]
        for metric, magnitude, _ in self._faults:
            values[metric] += magnitude
        return make_reading(self.mission_id, self.start_time, self.elapsed_seconds,
                            self.flight_phase, values)
