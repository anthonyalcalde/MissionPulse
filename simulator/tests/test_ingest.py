import json
from urllib.error import HTTPError, URLError

import pytest

from simulator.ingest import IngestionError, post_reading


class Response:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def reading():
    return {
        "mission_id": "mission_test",
        "timestamp": "2026-10-16T21:00:00Z",
        "elapsed_seconds": 1.25,
        "altitude": 2.0,
        "velocity": 3.0,
        "acceleration": 4.0,
        "temperature": 20.0,
        "pressure": 101325.0,
        "flight_phase": "preflight",
    }


def test_posts_actual_json_schema_to_ingestion_route():
    requests = []

    def request(value, timeout):
        requests.append((value, timeout))
        return Response()

    post_reading("http://localhost:8000/", reading(), request=request)
    sent = requests[0][0]
    assert sent.full_url == "http://localhost:8000/api/telemetry"
    assert sent.get_header("Content-type") == "application/json"
    assert json.loads(sent.data) == reading()
    assert requests[0][1] == 10


def test_retries_transient_network_failure_without_advancing_silently():
    attempts = []
    delays = []

    def request(value, timeout):
        attempts.append(value)
        if len(attempts) == 1:
            raise URLError("temporary failure")
        return Response()

    post_reading("http://localhost:8000", reading(), retries=2, retry_delay=0.25,
                 request=request, sleep=delays.append)
    assert len(attempts) == 2
    assert delays == [0.25]


def test_retries_transient_server_errors_and_stops_on_permanent_rejection():
    delays = []

    def request(value, timeout):
        raise HTTPError(value.full_url, 503, "unavailable", {}, None)

    with pytest.raises(IngestionError, match="HTTP 503"):
        post_reading("http://localhost:8000", reading(), retries=2, request=request, sleep=delays.append)
    assert delays == [0.5, 1.0]

    def reject(value, timeout):
        raise HTTPError(value.full_url, 422, "invalid", {}, None)

    with pytest.raises(IngestionError, match="HTTP 422"):
        post_reading("http://localhost:8000", reading(), retries=4, request=reject, sleep=delays.append)
    assert delays == [0.5, 1.0]


def test_gives_up_explicitly_after_network_retry_limit():
    attempts = []

    def request(value, timeout):
        attempts.append(value)
        raise URLError("offline")

    with pytest.raises(IngestionError, match="after 3 attempts"):
        post_reading("http://localhost:8000", reading(), retries=2, request=request, sleep=lambda _: None)
    assert len(attempts) == 3
