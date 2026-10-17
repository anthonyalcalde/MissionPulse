import type {
  FlightPhase,
  MetricBucket,
  MissionStatsResponse,
  Telemetry,
  TelemetryHistoryResponse,
} from "../types";
import { FLIGHT_PHASES } from "../types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function timestampToEpoch(timestamp: string): number {
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(timestamp) ? timestamp : `${timestamp}Z`;
  return Date.parse(normalized);
}

function parseNullableNumber(value: unknown, field: string): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`Telemetry message has an invalid ${field}.`);
  }
  return value;
}

export function parseTelemetry(value: unknown): Telemetry {
  if (!isRecord(value)) throw new Error("Telemetry message must be a JSON object.");
  if (typeof value.mission_id !== "string" || !value.mission_id.trim()) {
    throw new Error("Telemetry message has no valid mission_id.");
  }
  if (typeof value.timestamp !== "string" || !Number.isFinite(timestampToEpoch(value.timestamp))) {
    throw new Error("Telemetry message has no valid timestamp.");
  }
  const elapsedSeconds = parseNullableNumber(value.elapsed_seconds, "elapsed_seconds");
  const altitude = parseNullableNumber(value.altitude, "altitude");
  const velocity = parseNullableNumber(value.velocity, "velocity");
  const acceleration = parseNullableNumber(value.acceleration, "acceleration");
  const temperature = parseNullableNumber(value.temperature, "temperature");
  const pressure = parseNullableNumber(value.pressure, "pressure");
  if (value.flight_phase !== null && (typeof value.flight_phase !== "string" || !value.flight_phase.trim())) {
    throw new Error("Telemetry message has no valid flight_phase.");
  }
  if (
    value.pressure_unit !== undefined &&
    value.pressure_unit !== null &&
    (typeof value.pressure_unit !== "string" || !value.pressure_unit.trim())
  ) {
    throw new Error("Telemetry message has an invalid pressure_unit.");
  }
  return {
    mission_id: value.mission_id,
    timestamp: value.timestamp,
    elapsed_seconds: elapsedSeconds,
    altitude,
    velocity,
    acceleration,
    temperature,
    pressure,
    pressure_unit: typeof value.pressure_unit === "string" ? value.pressure_unit : null,
    flight_phase: value.flight_phase,
  };
}

export function isFlightPhase(value: string | null): value is FlightPhase {
  return value !== null && (FLIGHT_PHASES as readonly string[]).includes(value);
}

export function pressureInKilopascals(
  pressure: number | null,
  unit: string | null | undefined,
): number | null {
  return pressure !== null && unit === "Pa" ? pressure / 1_000 : null;
}

export function parseHistoryResponse(value: unknown): TelemetryHistoryResponse {
  if (!isRecord(value) || !Array.isArray(value.data)) {
    throw new Error("Telemetry history response has an invalid shape.");
  }
  if (
    typeof value.mission_id !== "string" ||
    !Number.isInteger(value.total_records) ||
    !Number.isInteger(value.limit) ||
    !Number.isInteger(value.offset)
  ) {
    throw new Error("Telemetry history response is missing pagination metadata.");
  }
  return {
    mission_id: value.mission_id,
    total_records: value.total_records as number,
    limit: value.limit as number,
    offset: value.offset as number,
    data: value.data.map(parseTelemetry),
  };
}

export function parseStatsResponse(value: unknown): MissionStatsResponse {
  if (!isRecord(value) || !Array.isArray(value.data)) {
    throw new Error("Mission statistics response has an invalid shape.");
  }
  if (
    typeof value.mission_id !== "string" ||
    typeof value.interval !== "string" ||
    !Number.isInteger(value.total_buckets)
  ) {
    throw new Error("Mission statistics response is missing metadata.");
  }
  const data: MetricBucket[] = value.data.map((item) => {
    if (
      !isRecord(item) ||
      typeof item.timestamp !== "string" ||
      !Number.isFinite(timestampToEpoch(item.timestamp)) ||
      typeof item.bucket !== "string" ||
      !Number.isFinite(timestampToEpoch(item.bucket)) ||
      !Number.isInteger(item.sample_count)
    ) {
      throw new Error("Mission statistics response contains an invalid bucket.");
    }
    const metrics = [
      "avg_altitude",
      "max_altitude",
      "avg_velocity",
      "max_velocity",
      "avg_acceleration",
      "max_acceleration",
      "avg_temperature",
      "max_temperature",
    ] as const;
    for (const metric of metrics) {
      if (
        item[metric] !== null &&
        (typeof item[metric] !== "number" || !Number.isFinite(item[metric]))
      ) {
        throw new Error(`Mission statistics bucket has an invalid ${metric}.`);
      }
    }
    return item as unknown as MetricBucket;
  });
  return {
    mission_id: value.mission_id,
    interval: value.interval,
    total_buckets: value.total_buckets as number,
    data,
  };
}

export function appendTelemetry(
  buffer: Telemetry[],
  reading: Telemetry,
  capacity = 500,
): Telemetry[] {
  if (!Number.isInteger(capacity) || capacity < 1) {
    throw new Error("Telemetry buffer capacity must be a positive integer.");
  }
  const timestamp = timestampToEpoch(reading.timestamp);
  const next = [
    ...buffer.filter((item) => timestampToEpoch(item.timestamp) !== timestamp),
    reading,
  ].sort((left, right) => timestampToEpoch(left.timestamp) - timestampToEpoch(right.timestamp));
  return next.slice(-capacity);
}
