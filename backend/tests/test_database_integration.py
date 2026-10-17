import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.database import get_db_cursor, initialize_schema

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set TEST_DATABASE_URL to a dedicated disposable TimescaleDB test database.",
)


def test_timescale_schema_ingestion_history_stats_and_cleanup(monkeypatch):
    assert TEST_DATABASE_URL
    configured_url = os.getenv("DATABASE_URL")
    if configured_url and configured_url == TEST_DATABASE_URL:
        pytest.fail("TEST_DATABASE_URL must not point at DATABASE_URL; use a dedicated test database.")

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    initialize_schema()
    mission_id = f"pytest_{uuid4().hex}"
    client = TestClient(main.app)
    reading = {
        "mission_id": mission_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": 1.0,
        "altitude": 25.0,
        "velocity": 3.0,
        "acceleration": 9.8,
        "temperature": 18.0,
        "pressure": 101_325.0,
        "flight_phase": "ascent",
    }
    try:
        with client.websocket_connect(f"/ws/telemetry?mission_id={mission_id}") as websocket:
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"
            response = client.post("/api/telemetry", json=reading)
            assert response.status_code == 201
            live = websocket.receive_json()
        assert live["pressure_unit"] == "Pa"
        assert client.get(f"/api/missions/{mission_id}/telemetry").json()["data"][0]["pressure"] == 101_325.0
        stats = client.get(f"/api/missions/{mission_id}/stats?interval=1%20second").json()
        assert stats["total_buckets"] == 1
        assert stats["data"][0]["max_altitude"] == 25.0
    finally:
        with get_db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM telemetry WHERE mission_id = %s;", (mission_id,))
        client.close()
