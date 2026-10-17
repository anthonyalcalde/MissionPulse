import json
import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from backend import main
from simulator import FlightSimulator
from simulator.ingest import post_reading


class MemoryCursor:
    def __init__(self, records):
        self.records = records
        self.rows = []
        self.one = None

    def execute(self, query, params=None):
        params = params or ()
        if "INSERT INTO telemetry" in query:
            timestamp, mission_id, elapsed, altitude, velocity, acceleration, temperature, pressure, phase = params
            self.records[(mission_id, timestamp)] = {
                "timestamp": timestamp,
                "mission_id": mission_id,
                "elapsed_seconds": elapsed,
                "altitude": altitude,
                "velocity": velocity,
                "acceleration": acceleration,
                "temperature": temperature,
                "pressure_pa": pressure,
                "flight_phase": phase,
            }
            return
        if "time_bucket(" in query:
            interval, mission_id, *filters = params
            seconds = {"1 second": 1, "5 seconds": 5, "1 minute": 60, "1 hour": 3600}[interval]
            start_time = filters[0] if filters else None
            end_time = filters[1] if len(filters) > 1 else None
            buckets = {}
            for record in self.records.values():
                stamp = record["timestamp"]
                if record["mission_id"] != mission_id:
                    continue
                if start_time and stamp < start_time or end_time and stamp > end_time:
                    continue
                epoch = stamp.timestamp()
                bucket_time = datetime.fromtimestamp(math.floor(epoch / seconds) * seconds, timezone.utc)
                buckets.setdefault(bucket_time, []).append(record)
            self.rows = []
            for bucket, values in sorted(buckets.items()):
                nums = lambda key: [value[key] for value in values if value[key] is not None]
                altitude = nums("altitude")
                velocity = nums("velocity")
                acceleration = nums("acceleration")
                temperature = nums("temperature")
                self.rows.append({
                    "bucket": bucket,
                    "avg_altitude": sum(altitude) / len(altitude) if altitude else None,
                    "max_altitude": max(altitude) if altitude else None,
                    "avg_velocity": sum(velocity) / len(velocity) if velocity else None,
                    "max_velocity": max(velocity) if velocity else None,
                    "avg_acceleration": sum(acceleration) / len(acceleration) if acceleration else None,
                    "max_acceleration": max(acceleration) if acceleration else None,
                    "avg_temperature": sum(temperature) / len(temperature) if temperature else None,
                    "max_temperature": max(temperature) if temperature else None,
                    "sample_count": len(values),
                })
            return
        if "FROM telemetry" in query:
            mission_id = params[0]
            remaining = list(params[1:])
            start_time = end_time = None
            if "timestamp >= %s" in query:
                start_time = remaining.pop(0)
            if "timestamp <= %s" in query:
                end_time = remaining.pop(0)
            limit, offset = remaining
            values = [
                row for row in self.records.values()
                if row["mission_id"] == mission_id
                and (start_time is None or row["timestamp"] >= start_time)
                and (end_time is None or row["timestamp"] <= end_time)
            ]
            values.sort(key=lambda row: row["timestamp"])
            self.rows = values[offset:offset + limit]

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


@pytest.fixture
def api(monkeypatch):
    records = {}

    @contextmanager
    def cursor(commit=False, dict_cursor=False):
        yield MemoryCursor(records)

    monkeypatch.setattr(main, "get_db_cursor", cursor)
    client = TestClient(main.app)
    yield client, records
    client.close()


def payload(mission_id="mission_test", timestamp="2026-10-16T21:00:00Z", **updates):
    record = {
        "mission_id": mission_id,
        "timestamp": timestamp,
        "elapsed_seconds": 1.5,
        "altitude": 300.0,
        "velocity": 40.0,
        "acceleration": 9.8,
        "temperature": 18.0,
        "pressure": 101_325.0,
        "flight_phase": "ascent",
    }
    record.update(updates)
    return record


def test_health_and_database_readiness(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(
        main,
        "check_database_ready",
        lambda: (_ for _ in ()).throw(main.DatabaseConfigurationError("TimescaleDB unavailable.")),
    )
    assert client.get("/").json()["status"] == "ok"
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "TimescaleDB unavailable."


def test_ingestion_history_and_bucket_stats_share_schema(api):
    client, records = api
    first = client.post("/api/telemetry", json=payload())
    second = client.post(
        "/api/telemetry",
        json=payload(timestamp="2026-10-16T21:00:01Z", elapsed_seconds=2.5, altitude=400, pressure=99_000),
    )
    assert first.status_code == second.status_code == 201
    assert len(records) == 2
    assert first.json()["data"]["mission_id"] == "mission_test"

    history = client.get("/api/missions/mission_test/telemetry?limit=1")
    assert history.status_code == 200
    assert history.json()["total_records"] == 1
    assert history.json()["data"][0]["pressure"] == 101_325.0
    assert history.json()["data"][0]["pressure_unit"] == "Pa"
    assert history.json()["data"][0]["timestamp"].endswith("Z")

    stats = client.get("/api/missions/mission_test/stats?interval=5%20seconds")
    assert stats.status_code == 200
    result = stats.json()
    assert result["total_buckets"] == 1
    assert result["data"][0]["sample_count"] == 2
    assert result["data"][0]["avg_altitude"] == 350.0
    assert result["data"][0]["max_altitude"] == 400.0
    assert result["data"][0]["max_acceleration"] == 9.8
    assert result["data"][0]["max_temperature"] == 18.0


def test_ingestion_is_idempotent_for_same_mission_timestamp(api):
    client, records = api
    reading = payload()
    assert client.post("/api/telemetry", json=reading).status_code == 201
    reading["altitude"] = 350
    assert client.post("/api/telemetry", json=reading).status_code == 201
    assert len(records) == 1
    assert next(iter(records.values()))["altitude"] == 350


@pytest.mark.parametrize(
    "updates",
    [
        {"flight_phase": "boost"},
        {"pressure_unit": "kPa"},
        {"timestamp": "2026-10-16T21:00:00"},
        {"altitude": "NaN"},
    ],
)
def test_ingestion_rejects_invalid_phase_pressure_timestamp_and_numbers(api, updates):
    client, _ = api
    assert client.post("/api/telemetry", json=payload(**updates)).status_code == 422


def test_unknown_mission_returns_empty_history_and_stats(api):
    client, _ = api
    history = client.get("/api/missions/not_found/telemetry")
    stats = client.get("/api/missions/not_found/stats?interval=1%20second")
    assert history.json()["data"] == []
    assert history.json()["total_records"] == 0
    assert stats.json()["data"] == []


def test_invalid_stats_interval_rejected(api):
    client, _ = api
    assert client.get("/api/missions/mission_test/stats?interval=0%20seconds").status_code == 422


def test_websocket_ping_receives_pong(api):
    client, _ = api
    with client.websocket_connect("/ws/telemetry?mission_id=mission_test") as websocket:
        websocket.send_text("ping")
        assert websocket.receive_text() == "pong"


def test_successful_ingestion_broadcasts_only_after_database_commit(api, monkeypatch):
    client, _ = api
    messages = []
    committed = []

    @contextmanager
    def cursor(commit=False, dict_cursor=False):
        cursor = MemoryCursor({})
        yield cursor
        if commit:
            committed.append(True)

    async def broadcast(message):
        assert committed
        messages.append(message)

    monkeypatch.setattr(main, "get_db_cursor", cursor)
    monkeypatch.setattr(main.manager, "broadcast", broadcast)
    response = client.post("/api/telemetry", json=payload())
    assert response.status_code == 201
    assert messages[0]["mission_id"] == "mission_test"
    assert messages[0]["pressure"] == 101_325.0
    assert messages[0]["pressure_unit"] == "Pa"


def test_real_simulator_record_streams_through_ingestion_endpoint(api):
    client, records = api
    simulator = FlightSimulator(mission_id="simulator_api_test")
    reading = simulator.step()

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def request(http_request, timeout):
        response = client.post(
            http_request.full_url,
            content=http_request.data,
            headers=dict(http_request.header_items()),
        )
        assert response.status_code == 201
        Response.status = response.status_code
        return Response()

    post_reading(
        "http://missionpulse.test",
        reading,
        retries=0,
        request=request,
    )
    assert ("simulator_api_test", datetime.fromisoformat(reading["timestamp"].replace("Z", "+00:00"))) in records
    saved = next(iter(records.values()))
    assert saved["pressure_pa"] == 101_325.0
    assert saved["flight_phase"] == "preflight"
