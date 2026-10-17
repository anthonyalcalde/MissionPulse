import { describe, expect, it } from "vitest";
import {
  appendTelemetry,
  isFlightPhase,
  parseHistoryResponse,
  parseStatsResponse,
  parseTelemetry,
  pressureInKilopascals,
  timestampToEpoch,
} from "./telemetry";
import type { Telemetry } from "../types";

const reading = (elapsed: number, extra: Partial<Telemetry> = {}): Telemetry => ({
  mission_id: "mission_001",
  timestamp: new Date(Date.UTC(2026, 9, 16, 21, 0, elapsed)).toISOString(),
  elapsed_seconds: elapsed,
  altitude: 10,
  velocity: 20,
  acceleration: 9.8,
  temperature: 20,
  pressure: 98_000,
  flight_phase: "ascent",
  ...extra,
});

describe("telemetry validation", () => {
  it("converts pressure only when the backend explicitly identifies Pa", () => {
    expect(pressureInKilopascals(101_325, "Pa")).toBe(101.325);
    expect(pressureInKilopascals(101.325, "kPa")).toBeNull();
    expect(pressureInKilopascals(101_325, null)).toBeNull();
  });

  it("interprets backend timestamps without an explicit zone as UTC", () => {
    expect(timestampToEpoch("2026-10-16T21:00:00")).toBe(Date.parse("2026-10-16T21:00:00Z"));
  });

  it("accepts the shared telemetry record shape", () => {
    expect(parseTelemetry(reading(1))).toMatchObject({
      mission_id: "mission_001",
      elapsed_seconds: 1,
      flight_phase: "ascent",
    });
    expect(parseTelemetry({ ...reading(1), pressure_unit: "Pa" }).pressure_unit).toBe("Pa");
  });

  it("accepts nullable fields supported by the FastAPI response models", () => {
    expect(
      parseTelemetry({
        ...reading(1),
        altitude: null,
        pressure: null,
        flight_phase: null,
      }),
    ).toMatchObject({ altitude: null, pressure: null, flight_phase: null });
  });

  it("recognizes documented phases without rejecting unknown backend labels", () => {
    expect(isFlightPhase("apogee")).toBe(true);
    expect(isFlightPhase("boost")).toBe(false);
    expect(isFlightPhase(null)).toBe(false);
  });

  it.each([
    [null, "JSON object"],
    [{ ...reading(1), altitude: Number.NaN }, "altitude"],
    [{ ...reading(1), timestamp: "not a date" }, "timestamp"],
    [{ ...reading(1), mission_id: "" }, "mission_id"],
    [{ ...reading(1), pressure_unit: 12 }, "pressure_unit"],
  ])("rejects malformed telemetry", (input, message) => {
    expect(() => parseTelemetry(input)).toThrow(message);
  });

  it("validates historical records and their response metadata", () => {
    expect(
      parseHistoryResponse({
        mission_id: "mission_001",
        total_records: 1,
        limit: 500,
        offset: 0,
        data: [reading(2)],
      }).data,
    ).toHaveLength(1);
    expect(() =>
      parseHistoryResponse({
        mission_id: "mission_001",
        total_records: 1,
        limit: 500,
        offset: 0,
        data: [{ ...reading(2), pressure: "high" }],
      }),
    ).toThrow("pressure");
  });

  it("validates bucketed-statistics response and nullable metrics", () => {
    const stats = parseStatsResponse({
      mission_id: "mission_001",
      interval: "5 seconds",
      total_buckets: 1,
      data: [{
        bucket: "2026-10-16T21:00:00",
        timestamp: "2026-10-16T21:00:00",
        avg_altitude: 10,
        max_altitude: 12,
        avg_velocity: 20,
        max_velocity: 22,
        avg_acceleration: 9.8,
        max_acceleration: 9.8,
        avg_temperature: null,
        max_temperature: null,
        sample_count: 4,
      }],
    });
    expect(stats.data[0].avg_temperature).toBeNull();
    expect(() => parseStatsResponse({ mission_id: "x", interval: "1 second", total_buckets: 1, data: [{}] }))
      .toThrow("invalid bucket");
    expect(() => parseStatsResponse({
      mission_id: "x",
      interval: "1 second",
      total_buckets: 1,
      data: [{
        bucket: "not-a-date",
        timestamp: "not-a-date",
        sample_count: 1,
        avg_altitude: null,
        max_altitude: null,
        avg_velocity: null,
        max_velocity: null,
        avg_acceleration: null,
        max_acceleration: null,
        avg_temperature: null,
        max_temperature: null,
      }],
    })).toThrow("invalid bucket");
  });
});

describe("telemetry buffer", () => {
  it("orders readings, replaces duplicate timestamps, and caps retained records", () => {
    const original = [reading(2), reading(1)];
    const updated = reading(2, { altitude: 25 });
    const result = appendTelemetry(original, reading(3), 2);
    expect(result.map((item) => item.elapsed_seconds)).toEqual([2, 3]);
    expect(appendTelemetry(result, updated, 2)[0].altitude).toBe(25);
    expect(original).toHaveLength(2);
    expect(() => appendTelemetry([], reading(1), 0)).toThrow("positive integer");
  });

  it("deduplicates equivalent timestamps with different UTC notation", () => {
    const utcReading = reading(1);
    const noZoneReading = { ...utcReading, timestamp: utcReading.timestamp.slice(0, -1), altitude: 99 };
    expect(appendTelemetry([utcReading], noZoneReading)).toHaveLength(1);
    expect(appendTelemetry([utcReading], noZoneReading)[0].altitude).toBe(99);
  });
});
