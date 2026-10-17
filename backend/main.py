"""MissionPulse Backend API.

FastAPI application providing telemetry ingestion, time-series analytics,
historical data retrieval, and real-time WebSocket broadcasting backed by
Tiger Cloud TimescaleDB.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
import psycopg2
from psycopg2 import errors as pg_errors

try:
    from backend.database import DatabaseConfigurationError, check_database_ready, get_db_cursor
except ImportError:
    from database import DatabaseConfigurationError, check_database_ready, get_db_cursor

# Configure logger
logger = logging.getLogger("missionpulse.api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="MissionPulse Telemetry API",
    description="Simulated rocket telemetry ingestion, analytics, and live streaming powered by TimescaleDB.",
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
# WebSocket Connection Manager
# ============================================================================


class ConnectionManager:
    """Manages active WebSocket client connections for real-time telemetry streaming."""

    def __init__(self) -> None:
        # Maps active WebSocket connection to optional mission_id filter
        self.active_connections: Dict[WebSocket, Optional[str]] = {}

    async def connect(self, websocket: WebSocket, mission_id: Optional[str] = None) -> None:
        """Accept new WebSocket connection and track optional mission filter."""
        await websocket.accept()
        self.active_connections[websocket] = mission_id
        logger.info(
            "WebSocket client connected (mission_filter=%s). Active connections: %d",
            mission_id,
            len(self.active_connections),
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket client."""
        if websocket in self.active_connections:
            del self.active_connections[websocket]
            logger.info("WebSocket client disconnected. Active connections: %d", len(self.active_connections))

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """Broadcast telemetry message to all connected clients respecting filters."""
        disconnected_clients: List[WebSocket] = []
        for ws, filter_mission_id in list(self.active_connections.items()):
            # If no filter is set or if mission_id matches, send to client
            if filter_mission_id is None or filter_mission_id == message.get("mission_id"):
                try:
                    await ws.send_json(message)
                except Exception as err:
                    logger.warning("Error transmitting to WebSocket client, removing: %s", err)
                    disconnected_clients.append(ws)

        for ws in disconnected_clients:
            self.disconnect(ws)


manager = ConnectionManager()


# ============================================================================
# Pydantic Models
# ============================================================================


class TelemetryPayload(BaseModel):
    """Incoming pressure values are always pascals."""

    mission_id: str = Field(..., description="Unique mission identifier", min_length=1)
    timestamp: datetime = Field(..., description="Telemetry timestamp (ISO 8601 format)")
    elapsed_seconds: Optional[float] = Field(None, allow_inf_nan=False, description="Elapsed flight time in seconds")
    altitude: Optional[float] = Field(None, allow_inf_nan=False, description="Altitude in meters")
    velocity: Optional[float] = Field(None, allow_inf_nan=False, description="Velocity in m/s")
    acceleration: Optional[float] = Field(None, allow_inf_nan=False, description="Acceleration in m/s^2")
    temperature: Optional[float] = Field(None, allow_inf_nan=False, description="Temperature in Celsius")
    pressure: Optional[float] = Field(
        None, allow_inf_nan=False, description="Atmospheric pressure in pascals (Pa)"
    )
    pressure_unit: Literal["Pa"] = Field("Pa", description="Pressure unit; only Pa is accepted")
    flight_phase: Optional[Literal["preflight", "ascent", "coast", "apogee", "descent", "landed"]] = Field(
        None, description="Documented lowercase flight phase"
    )

    @field_validator("timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone (UTC recommended)")
        return value.astimezone(timezone.utc)


class MetricBucket(BaseModel):
    """Aggregated telemetry metrics for a single time bucket."""

    bucket: str = Field(..., description="Timestamp bucket start (ISO 8601)")
    timestamp: str = Field(..., description="Alias for bucket timestamp for easy chart plotting")
    avg_altitude: Optional[float] = Field(None, description="Average altitude in meters")
    max_altitude: Optional[float] = Field(None, description="Maximum altitude in meters")
    avg_velocity: Optional[float] = Field(None, description="Average velocity in m/s")
    max_velocity: Optional[float] = Field(None, description="Maximum velocity in m/s")
    avg_acceleration: Optional[float] = Field(None, description="Average acceleration in m/s^2")
    max_acceleration: Optional[float] = Field(None, description="Maximum acceleration in m/s^2")
    avg_temperature: Optional[float] = Field(None, description="Average temperature in Celsius")
    max_temperature: Optional[float] = Field(None, description="Maximum temperature in Celsius")
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
    pressure: Optional[float] = Field(None, description="Atmospheric pressure in pascals (Pa)")
    pressure_unit: Literal["Pa"] = Field("Pa", description="Pressure unit")
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
    """Liveness check; does not require the database."""
    return {"status": "ok", "service": "MissionPulse API"}


@app.get("/health/ready", summary="Readiness Check")
@app.get("/health", summary="Readiness Check", include_in_schema=False)
def readiness_check() -> Dict[str, str]:
    try:
        check_database_ready()
    except DatabaseConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    return {"status": "ready", "database": "connected", "timescale": "available"}


@app.post(
    "/api/telemetry",
    status_code=status.HTTP_201_CREATED,
    summary="Ingest Telemetry Data",
    response_description="Confirmation of stored telemetry record",
)
async def ingest_telemetry(payload: TelemetryPayload) -> Dict[str, Any]:
    """Ingest a single telemetry record and store it into the TimescaleDB hypertable.

    - Validates payload structure and types with Pydantic.
    - Executes a parameterized SQL INSERT.
    - Broadcasts the new telemetry point to all active WebSocket clients.
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
            pressure_pa,
            flight_phase
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (mission_id, timestamp) DO UPDATE SET
                elapsed_seconds = EXCLUDED.elapsed_seconds,
                altitude = EXCLUDED.altitude,
                velocity = EXCLUDED.velocity,
                acceleration = EXCLUDED.acceleration,
                temperature = EXCLUDED.temperature,
                pressure_pa = EXCLUDED.pressure_pa,
                flight_phase = EXCLUDED.flight_phase;
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
    except (DatabaseConfigurationError, psycopg2.Error) as db_err:
        logger.error("Database error during telemetry insert: %s", type(db_err).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
            if isinstance(db_err, DatabaseConfigurationError)
            else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database unavailable while storing telemetry."
            if isinstance(db_err, DatabaseConfigurationError)
            else "Database error occurred while storing telemetry.",
        ) from db_err

    # Consistent JSON message matching existing telemetry schema
    telemetry_message = {
        "timestamp": payload.timestamp.isoformat(),
        "mission_id": payload.mission_id,
        "elapsed_seconds": payload.elapsed_seconds,
        "altitude": payload.altitude,
        "velocity": payload.velocity,
        "acceleration": payload.acceleration,
        "temperature": payload.temperature,
        "pressure": payload.pressure,
        "pressure_unit": "Pa",
        "flight_phase": payload.flight_phase,
    }

    # Broadcast to connected WebSocket clients
    await manager.broadcast(telemetry_message)

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
        pattern=r"^(1 second|5 seconds|1 minute|1 hour)$",
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
    if interval not in {"1 second", "5 seconds", "1 minute", "1 hour"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported interval. Choose 1 second, 5 seconds, 1 minute, or 1 hour.",
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
            ROUND(MAX(acceleration)::numeric, 2) AS max_acceleration,
            ROUND(AVG(temperature)::numeric, 2) AS avg_temperature,
            ROUND(MAX(temperature)::numeric, 2) AS max_temperature,
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
    except (DatabaseConfigurationError, psycopg2.Error) as db_err:
        logger.error("Database error while querying mission stats: %s", type(db_err).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
            if isinstance(db_err, DatabaseConfigurationError)
            else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database unavailable while fetching mission statistics."
            if isinstance(db_err, DatabaseConfigurationError)
            else "Database error occurred while fetching mission statistics.",
        ) from db_err

    buckets: List[MetricBucket] = []
    for row in rows:
        bucket_iso = row["bucket"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        buckets.append(
            MetricBucket(
                bucket=bucket_iso,
                timestamp=bucket_iso,
                avg_altitude=float(row["avg_altitude"]) if row["avg_altitude"] is not None else None,
                max_altitude=float(row["max_altitude"]) if row["max_altitude"] is not None else None,
                avg_velocity=float(row["avg_velocity"]) if row["avg_velocity"] is not None else None,
                max_velocity=float(row["max_velocity"]) if row["max_velocity"] is not None else None,
                avg_acceleration=float(row["avg_acceleration"]) if row["avg_acceleration"] is not None else None,
                max_acceleration=float(row["max_acceleration"]) if row["max_acceleration"] is not None else None,
                avg_temperature=float(row["avg_temperature"]) if row["avg_temperature"] is not None else None,
                max_temperature=float(row["max_temperature"]) if row["max_temperature"] is not None else None,
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
            pressure_pa,
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
    except (DatabaseConfigurationError, psycopg2.Error) as db_err:
        logger.error("Database error while retrieving historical telemetry: %s", type(db_err).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE
            if isinstance(db_err, DatabaseConfigurationError)
            else status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database unavailable while retrieving historical telemetry."
            if isinstance(db_err, DatabaseConfigurationError)
            else "Database error occurred while retrieving historical telemetry.",
        ) from db_err

    records: List[TelemetryRecord] = []
    for row in rows:
        records.append(
            TelemetryRecord(
                timestamp=row["timestamp"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                mission_id=row["mission_id"],
                elapsed_seconds=float(row["elapsed_seconds"]) if row["elapsed_seconds"] is not None else None,
                altitude=float(row["altitude"]) if row["altitude"] is not None else None,
                velocity=float(row["velocity"]) if row["velocity"] is not None else None,
                acceleration=float(row["acceleration"]) if row["acceleration"] is not None else None,
                temperature=float(row["temperature"]) if row["temperature"] is not None else None,
                pressure=float(row["pressure_pa"]) if row["pressure_pa"] is not None else None,
                pressure_unit="Pa",
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


# ============================================================================
# WebSocket Real-Time Endpoint
# ============================================================================


@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket, mission_id: Optional[str] = None):
    """Real-time telemetry streaming WebSocket endpoint.

    Allows dashboard clients to receive live telemetry updates as soon as
    they are successfully ingested into the database.

    Supports optional mission_id query parameter to stream updates for a
    specific mission, or streams all missions if omitted.
    """
    await manager.connect(websocket, mission_id=mission_id)
    try:
        while True:
            # Keep connection alive and accept client pings or control messages
            data = await websocket.receive_text()
            if data.strip().lower() == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as err:
        logger.debug("WebSocket client disconnected or encountered error: %s", err)
        manager.disconnect(websocket)
