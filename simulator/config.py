"""SI configuration for a deliberately simplified educational rocket."""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class SimulationConfig:
    mass: float = 10.0
    thrust: float = 300.0
    burn_seconds: float = 8.0
    preflight_seconds: float = 2.0
    gravity: float = 9.80665
    drag_area: float = 0.015  # Cd * frontal area, m²
    parachute_drag_area: float = 1.5
    sea_level_density: float = 1.225
    sea_level_pressure: float = 101325.0
    sea_level_temperature: float = 20.0
    scale_height: float = 8500.0
    integration_step: float = 0.01
    sample_interval: float = 0.1
    speed: float = 1.0
    seed: int = 42
    noise_fraction: float = 0.0

    def __post_init__(self):
        for name, value in vars(self).items():
            if name != 'seed' and not math.isfinite(value):
                raise ValueError(f'{name} must be finite')
        for name in ('mass', 'thrust', 'burn_seconds', 'gravity', 'sea_level_density',
                     'sea_level_pressure', 'scale_height', 'integration_step', 'sample_interval', 'speed'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')
        if self.thrust <= self.mass * self.gravity:
            raise ValueError('thrust must exceed weight for liftoff')
        for name in ('drag_area', 'parachute_drag_area', 'preflight_seconds', 'noise_fraction'):
            if getattr(self, name) < 0:
                raise ValueError(f'{name} cannot be negative')
