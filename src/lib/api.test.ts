import { afterEach, describe, expect, it, vi } from "vitest";
import { API_BASE_URL, createWebSocketUrl, fetchHistory, fetchStats } from "./api";

const telemetry = {
  mission_id: "mission 001",
  timestamp: "2026-10-16T21:00:00Z",
  elapsed_seconds: 1,
  altitude: 2,
  velocity: 3,
  acceleration: 4,
  temperature: 20,
  pressure: 101_325,
  flight_phase: "preflight",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("MissionPulse REST client", () => {
  it("loads and validates chronological history from the implemented endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      mission_id: telemetry.mission_id,
      total_records: 1,
      limit: 500,
      offset: 0,
      data: [telemetry],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchHistory(telemetry.mission_id);

    expect(result.data).toHaveLength(1);
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `${API_BASE_URL}/api/missions/mission%20001/telemetry?limit=500`,
    );
  });

  it("loads time-bucket statistics from the implemented endpoint", async () => {
    const body = {
      mission_id: telemetry.mission_id,
      interval: "5 seconds",
      total_buckets: 1,
      data: [{
        bucket: "2026-10-16T21:00:00Z",
        timestamp: "2026-10-16T21:00:00Z",
        avg_altitude: 2,
        max_altitude: 3,
        avg_velocity: 4,
        max_velocity: 5,
        avg_acceleration: 6,
        max_acceleration: 7,
        avg_temperature: 20,
        max_temperature: 21,
        sample_count: 2,
      }],
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    expect((await fetchStats(telemetry.mission_id, "5 seconds")).data).toHaveLength(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/stats?interval=5+seconds");
  });

  it("surfaces API detail on failed HTTP responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Mission not found" }), { status: 404 }),
    ));
    await expect(fetchHistory(telemetry.mission_id)).rejects.toThrow("Mission not found");
  });

  it("rejects invalid mission IDs before making a request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchHistory("   ")).rejects.toThrow("Mission ID");
    await expect(fetchStats(" mission_001", "5 seconds")).rejects.toThrow("Mission ID");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects malformed response JSON and mismatched mission IDs", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(new Response("not-json", { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        mission_id: "another-mission",
        total_records: 0,
        limit: 500,
        offset: 0,
        data: [],
      }), { status: 200 })));
    await expect(fetchHistory(telemetry.mission_id)).rejects.toThrow("invalid JSON");
    await expect(fetchHistory(telemetry.mission_id)).rejects.toThrow("different mission");
  });

  it("creates an encoded WebSocket URL using the backend route", () => {
    expect(createWebSocketUrl(telemetry.mission_id)).toBe(
      "ws://localhost:8000/ws/telemetry?mission_id=mission+001",
    );
  });
});
