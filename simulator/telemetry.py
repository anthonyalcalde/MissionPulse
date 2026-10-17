"""Shared telemetry contract; pressure is always Pa, phase always lowercase."""
from typing import TypedDict
from datetime import datetime, timedelta

class Telemetry(TypedDict):
    mission_id: str
    timestamp: str
    elapsed_seconds: float
    altitude: float
    velocity: float
    acceleration: float
    temperature: float
    pressure: float
    flight_phase: str

METRICS = ('altitude', 'velocity', 'acceleration', 'temperature', 'pressure')

def make_reading(mission_id: str, origin: datetime, elapsed: float,
                 phase: str, values: dict) -> Telemetry:
    return dict(mission_id=mission_id,
                timestamp=(origin + timedelta(seconds=elapsed)).isoformat().replace('+00:00', 'Z'),
                elapsed_seconds=elapsed, flight_phase=phase, **values)
