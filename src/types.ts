export const FLIGHT_PHASES = [
  "preflight",
  "ascent",
  "coast",
  "apogee",
  "descent",
  "landed",
] as const;

export type FlightPhase = (typeof FLIGHT_PHASES)[number];

export interface Telemetry {
  mission_id: string;
  timestamp: string;
  elapsed_seconds: number | null;
  altitude: number | null;
  velocity: number | null;
  acceleration: number | null;
  temperature: number | null;
  pressure: number | null;
  pressure_unit?: string | null;
  flight_phase: string | null;
}

export interface TelemetryHistoryResponse {
  mission_id: string;
  total_records: number;
  limit: number;
  offset: number;
  data: Telemetry[];
}

export interface MetricBucket {
  bucket: string;
  timestamp: string;
  avg_altitude: number | null;
  max_altitude: number | null;
  avg_velocity: number | null;
  max_velocity: number | null;
  avg_acceleration: number | null;
  max_acceleration: number | null;
  avg_temperature: number | null;
  max_temperature: number | null;
  sample_count: number;
}

export interface MissionStatsResponse {
  mission_id: string;
  interval: string;
  total_buckets: number;
  data: MetricBucket[];
}

export type ConnectionState =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected"
  | "error";
