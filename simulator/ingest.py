"""HTTP ingestion client for posting simulator readings to MissionPulse."""

import json
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class IngestionError(RuntimeError):
    """Raised when a telemetry record cannot be accepted by the API."""


def post_reading(
    api_url: str,
    reading: dict,
    retries: int = 5,
    retry_delay: float = 0.5,
    request: Callable = urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if retries < 0:
        raise ValueError("retries must be nonnegative")
    if retry_delay < 0:
        raise ValueError("retry_delay must be nonnegative")

    url = f"{api_url.rstrip('/')}/api/telemetry"
    body = json.dumps(reading, allow_nan=False).encode("utf-8")
    request_data = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with request(request_data, timeout=10) as response:
                if 200 <= response.status < 300:
                    return
                raise IngestionError(f"Ingestion API returned HTTP {response.status}.")
        except HTTPError as error:
            if error.code not in RETRYABLE_STATUSES or attempt == retries:
                raise IngestionError(f"Ingestion API rejected telemetry with HTTP {error.code}.") from error
            delay = retry_delay * 2**attempt
        except (URLError, TimeoutError, ConnectionError, OSError) as error:
            if attempt == retries:
                raise IngestionError(
                    f"Could not deliver telemetry after {retries + 1} attempts."
                ) from error
            delay = retry_delay * 2**attempt
        if delay:
            sleep(delay)
