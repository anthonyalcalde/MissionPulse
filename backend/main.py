"""MissionPulse Backend API.

FastAPI application providing telemetry ingestion, time-series analytics,
and historical data retrieval backed by Tiger Cloud TimescaleDB.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import psycopg2
from psycopg2 import errors as pg_errors

try:
    from backend.database import get_db_cursor
except ImportError:
    from database import get_db_cursor

# Configure logger
logger = logging.getLogger("missionpulse.api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="MissionPulse Telemetry API",
    description="Simulated rocket telemetry ingestion and analytics powered by TimescaleDB.",
    version="1.0.0",
)

# Enable CORS for frontend dashboard access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# Pydantic Models
# ============================================================================


class TelemetryPayload(BaseModel):
    """Pydantic model for incoming telemetry data validation."""

    mission_id: str = Field(..., description="Unique mission identifier", min_length=1)
    timestamp: datetime = Field(..., description="Telemetry timestamp (ISO 8601 format)")
    elapsed_seconds: Optional[float] = Field(None, description="Elapsed flight time in seconds")
    altitude: Optional[float] = Field(None, description="Altitude in meters")
    velocity: Optional[float] = Field(None, description="Velocity in m/s")
    acceleration: Optional[float] = Field(None, description="Acceleration in m/s^2")
    temperature: Optional[float] = Field(None, description="Temperature in Celsius")
    pressure: Optional[float] = Field(None, description="Atmospheric pressure in Pa or kPa")
    flight_phase: Optional[str] = Field(None, description="Flight phase description (e.g. Boost, Coast, Apogee)")


class MetricBucket(BaseModel):
    """Aggregated telemetry metrics for a single time bucket."""

    bucket: str = Field(..., description="Timestamp bucket start (ISO 8601)")
    timestamp: str = Field(..., description="Alias for bucket timestamp for easy chart plotting")
    avg_altitude: Optional[float] = Field(None, description="Average altitude in meters")
    max_altitude: Optional[float] = Field(None, description="Maximum altitude in meters")
    avg_velocity: Optional[float] = Field(None, description="Average velocity in m/s")
    max_velocity: Optional[float] = Field(None, description="Maximum velocity in m/s")
    avg_acceleration: Optional[float] = Field(None, description="Average acceleration in m/s^2")
    avg_temperature: Optional[float] = Field(None, description="Average temperature in Celsius")
    sample_count: int = Field(..., description="Number of telemetry samples in this bucket")


class MissionStatsResponse(BaseModel):
    """Response schema for time-series mission telemetry analytics."""

    mission_id: str = Field(..., description="Mission identifier")
    interval: str = Field(..., description="Aggregation interval bucket width")
    total_buckets: int = Field(..., description="Total number of intervals returned")
    data: List[MetricBucket] = Field(default_factory=list, description="Array of time-bucketed metrics")


class TelemetryRecord(BaseModel):
    """Historical telemetry record data model."""

    timestamp: str = Field(..., description="Timestamp in ISO 8601 format")
    mission_id: str = Field(..., description="Mission identifier")
    elapsed_seconds: Optional[float] = Field(None, description="Elapsed flight time in seconds")
    altitude: Optional[float] = Field(None, description="Altitude in meters")
    velocity: Optional[float] = Field(None, description="Velocity in m/s")
    acceleration: Optional[float] = Field(None, description="Acceleration in m/s^2")
    temperature: Optional[float] = Field(None, description="Temperature in Celsius")
    pressure: Optional[float] = Field(None, description="Atmospheric pressure in Pa or kPa")
    flight_phase: Optional[str] = Field(None, description="Flight phase description")


class HistoricalTelemetryResponse(BaseModel):
    """Response schema for historical raw telemetry retrieval."""

    mission_id: str = Field(..., description="Mission identifier")
    total_records: int = Field(..., description="Total records returned in this batch")
    limit: int = Field(..., description="Applied query limit")
    offset: int = Field(..., description="Applied query offset")
    data: List[TelemetryRecord] = Field(default_factory=list, description="Chronological telemetry records")


# ============================================================================
# API Endpoints
# ============================================================================


@app.get("/", summary="Health Check")
def health_check() -> Dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "MissionPulse API"}


@app.post(
    "/api/telemetry",
    status_code=status.HTTP_201_CREATED,
    summary="Ingest Telemetry Data",
    response_description="Confirmation of stored telemetry record",
)
def ingest_telemetry(payload: TelemetryPayload) -> Dict[str, Any]:
    """Ingest a single telemetry record and store it into the TimescaleDB hypertable.

    - Validates payload structure and types with Pydantic.
    - Executes a parameterized SQL INSERT.
    - Safely handles errors without leaking credentials.
    """
    insert_sql = """
        INSERT INTO telemetry (
            timestamp,
            mission_id,
            elapsed_seconds,
            altitude,
            velocity,
            acceleration,
            temperature,
            pressure,
            flight_phase
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    params = (
        payload.timestamp,
        payload.mission_id,
        payload.elapsed_seconds,
        payload.altitude,
        payload.velocity,
        payload.acceleration,
        payload.temperature,
        payload.pressure,
        payload.flight_phase,
    )

    try:
        with get_db_cursor(commit=True) as cur:
            cur.execute(insert_sql, params)
    except psycopg2.Error as db_err:
        logger.error("Database error during telemetry insert: %s", db_err.pgerror or str(db_err))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error occurred while storing telemetry.",
        )
    except Exception as err:
        logger.error("Unexpected error during telemetry insert: %s", str(err))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while storing telemetry.",
        )

    return {
        "status": "success",
        "message": "Telemetry record stored successfully.",
        "data": {
            "mission_id": payload.mission_id,
            "timestamp": payload.timestamp.isoformat(),
        },
    }


@app.get(
    "/api/missions/{mission_id}/stats",
    response_model=MissionStatsResponse,
    summary="Get Mission Telemetry Analytics",
    response_description="Time-bucketed mission statistics ready for chart graphing",
)
def get_mission_stats(
    mission_id: str,
    interval: str = Query(
        "5 seconds",
        description="TimescaleDB time_bucket interval width (e.g. '1 second', '5 seconds', '1 minute', '1 hour')",
    ),
    start_time: Optional[datetime] = Query(None, description="Optional filter: start timestamp (ISO 8601)"),
    end_time: Optional[datetime] = Query(None, description="Optional filter: end timestamp (ISO 8601)"),
) -> MissionStatsResponse:
    """Retrieve aggregated time-series telemetry stats for a specific mission.

    Uses TimescaleDB's `time_bucket()` function to aggregate metrics across
    configurable time intervals.

    Returns:
    - Average and maximum altitude
    - Average and maximum velocity
    - Average acceleration
    - Average temperature
    """
    if not interval or not interval.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Interval parameter cannot be empty. Examples of valid intervals: '1 second', '5 seconds', '1 minute'.",
        )

    conditions = ["mission_id = %s"]
    params: List[Any] = [interval, mission_id]

    if start_time:
        conditions.append("timestamp >= %s")
        params.append(start_time)
    if end_time:
        conditions.append("timestamp <= %s")
        params.append(end_time)

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT 
            time_bucket(%s::interval, timestamp) AS bucket,
            ROUND(AVG(altitude)::numeric, 2) AS avg_altitude,
            ROUND(MAX(altitude)::numeric, 2) AS max_altitude,
            ROUND(AVG(velocity)::numeric, 2) AS avg_velocity,
            ROUND(MAX(velocity)::numeric, 2) AS max_velocity,
            ROUND(AVG(acceleration)::numeric, 2) AS avg_acceleration,
            ROUND(AVG(temperature)::numeric, 2) AS avg_temperature,
            COUNT(*) AS sample_count
        FROM telemetry
        WHERE {where_clause}
        GROUP BY bucket
        ORDER BY bucket ASC;
    """

    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
    except (pg_errors.InvalidDatetimeFormat, pg_errors.DataError, pg_errors.InvalidParameterValue) as fmt_err:
        logger.warning("Invalid interval parameter '%s': %s", interval, str(fmt_err))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid interval format: '{interval}'. Interval must be a valid positive duration (e.g. '1 second', '5 seconds', '1 minute', '1 hour').",
        )
    except psycopg2.Error as db_err:
        logger.error("Database error while querying mission stats: %s", db_err.pgerror or str(db_err))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error occurred while fetching mission statistics.",
        )
    except Exception as err:
        logger.error("Unexpected error while querying mission stats: %s", str(err))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching mission statistics.",
        )

    buckets: List[MetricBucket] = []
    for row in rows:
        bucket_iso = row["bucket"].isoformat()
        buckets.append(
            MetricBucket(
                bucket=bucket_iso,
                timestamp=bucket_iso,
                avg_altitude=float(row["avg_altitude"]) if row["avg_altitude"] is not None else None,
                max_altitude=float(row["max_altitude"]) if row["max_altitude"] is not None else None,
                avg_velocity=float(row["avg_velocity"]) if row["avg_velocity"] is not None else None,
                max_velocity=float(row["max_velocity"]) if row["max_velocity"] is not None else None,
                avg_acceleration=float(row["avg_acceleration"]) if row["avg_acceleration"] is not None else None,
                avg_temperature=float(row["avg_temperature"]) if row["avg_temperature"] is not None else None,
                sample_count=int(row["sample_count"]),
            )
        )

    return MissionStatsResponse(
        mission_id=mission_id,
        interval=interval,
        total_buckets=len(buckets),
        data=buckets,
    )


@app.get(
    "/api/missions/{mission_id}/telemetry",
    response_model=HistoricalTelemetryResponse,
    summary="Get Historical Mission Telemetry",
    response_description="Chronological raw telemetry records for a mission",
)
def get_historical_telemetry(
    mission_id: str,
    start_time: Optional[datetime] = Query(
        None, description="Filter records starting from timestamp (inclusive, ISO 8601)"
    ),
    end_time: Optional[datetime] = Query(
        None, description="Filter records up to timestamp (inclusive, ISO 8601)"
    ),
    limit: int = Query(
        500, ge=1, le=5000, description="Maximum number of records to return (1 to 5000, default 500)"
    ),
    offset: int = Query(0, ge=0, description="Number of records to skip for pagination (default 0)"),
) -> HistoricalTelemetryResponse:
    """Retrieve historical telemetry records for a specific mission in chronological order.

    Supports optional time range slicing (start_time and end_time) and pagination.
    Safely limits output to a maximum of 5000 records per query.
    """
    conditions = ["mission_id = %s"]
    params: List[Any] = [mission_id]

    if start_time:
        conditions.append("timestamp >= %s")
        params.append(start_time)
    if end_time:
        conditions.append("timestamp <= %s")
        params.append(end_time)

    params.append(limit)
    params.append(offset)

    where_clause = " AND ".join(conditions)

    query = f"""
        SELECT 
            timestamp,
            mission_id,
            elapsed_seconds,
            altitude,
            velocity,
            acceleration,
            temperature,
            pressure,
            flight_phase
        FROM telemetry
        WHERE {where_clause}
        ORDER BY timestamp ASC
        LIMIT %s OFFSET %s;
    """

    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
    except psycopg2.Error as db_err:
        logger.error("Database error while retrieving historical telemetry: %s", db_err.pgerror or str(db_err))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error occurred while retrieving historical telemetry.",
        )
    except Exception as err:
        logger.error("Unexpected error while retrieving historical telemetry: %s", str(err))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while retrieving historical telemetry.",
        )

    records: List[TelemetryRecord] = []
    for row in rows:
        records.append(
            TelemetryRecord(
                timestamp=row["timestamp"].isoformat(),
                mission_id=row["mission_id"],
                elapsed_seconds=float(row["elapsed_seconds"]) if row["elapsed_seconds"] is not None else None,
                altitude=float(row["altitude"]) if row["altitude"] is not None else None,
                velocity=float(row["velocity"]) if row["velocity"] is not None else None,
                acceleration=float(row["acceleration"]) if row["acceleration"] is not None else None,
                temperature=float(row["temperature"]) if row["temperature"] is not None else None,
                pressure=float(row["pressure"]) if row["pressure"] is not None else None,
                flight_phase=row["flight_phase"],
            )
        )

    return HistoricalTelemetryResponse(
        mission_id=mission_id,
        total_records=len(records),
        limit=limit,
        offset=offset,
        data=records,
    )
