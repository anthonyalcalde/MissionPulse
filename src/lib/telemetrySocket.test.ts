import { afterEach, describe, expect, it, vi } from "vitest";
import { isTelemetryStale } from "../hooks/useTelemetryStream";
import {
  PING_INTERVAL_MS,
  STABLE_CONNECTION_MS,
  TelemetrySocketClient,
  parseWebSocketFrame,
} from "./telemetrySocket";
import type { ConnectionState, Telemetry } from "../types";

const reading: Telemetry = {
  mission_id: "mission_001",
  timestamp: "2026-10-16T21:00:00Z",
  elapsed_seconds: 1,
  altitude: 2,
  velocity: 3,
  acceleration: 4,
  temperature: 20,
  pressure: 101_325,
  pressure_unit: "Pa",
  flight_phase: "ascent",
};

class FakeSocket extends EventTarget {
  readyState = 0;
  sent: string[] = [];
  closeCalls = 0;
  throwOnSend = false;

  send(value: string) {
    if (this.throwOnSend) throw new Error("send failed");
    this.sent.push(value);
  }

  open() {
    this.readyState = 1;
    this.dispatchEvent(new Event("open"));
  }

  message(data: unknown) {
    this.dispatchEvent(new MessageEvent("message", { data }));
  }

  close() {
    this.closeCalls += 1;
    this.readyState = 3;
    this.dispatchEvent(new Event("close"));
  }
}

function makeClient(createSocket: (url: string) => WebSocket) {
  const statuses: ConnectionState[] = [];
  const readings: Telemetry[] = [];
  const errors: string[] = [];
  const invalid: string[] = [];
  const client = new TelemetrySocketClient("ws://mission.test/ws", {
    onStatus: (status) => statuses.push(status),
    onTelemetry: (value) => readings.push(value),
    onError: (message) => errors.push(message),
    onInvalidFrame: (message) => invalid.push(message),
  }, createSocket);
  return { client, statuses, readings, errors, invalid };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("WebSocket frame handling", () => {
  it("ignores pong and parses a raw telemetry object", () => {
    expect(parseWebSocketFrame(" pong ")).toEqual({ kind: "pong" });
    expect(parseWebSocketFrame(JSON.stringify(reading))).toEqual({ kind: "telemetry", reading });
  });

  it("reports malformed JSON, invalid telemetry, and non-text frames", () => {
    expect(parseWebSocketFrame("{").kind).toBe("invalid");
    expect(parseWebSocketFrame(JSON.stringify({ ...reading, altitude: "high" })).kind).toBe("invalid");
    expect(parseWebSocketFrame(new ArrayBuffer(8))).toMatchObject({ kind: "invalid" });
  });

  it("checks known flight phases and safely identifies unknown phases", () => {
    expect(parseWebSocketFrame(JSON.stringify(reading))).toMatchObject({ kind: "telemetry" });
    expect(parseWebSocketFrame(JSON.stringify({ ...reading, flight_phase: "boost" }))).toMatchObject({
      kind: "telemetry",
      reading: { flight_phase: "boost" },
    });
  });
});

describe("WebSocket lifecycle", () => {
  it("creates one connection, ignores pong, reports malformed frames, and safely pings", () => {
    vi.useFakeTimers();
    const socket = new FakeSocket();
    const factory = vi.fn(() => socket as unknown as WebSocket);
    const result = makeClient(factory);
    result.client.start();
    result.client.start();
    expect(factory).toHaveBeenCalledTimes(1);

    socket.open();
    socket.message("pong");
    socket.message(JSON.stringify(reading));
    socket.message("{");
    socket.message(new ArrayBuffer(4));
    expect(result.readings).toEqual([reading]);
    expect(result.invalid).toHaveLength(2);

    vi.advanceTimersByTime(PING_INTERVAL_MS);
    expect(socket.sent).toEqual(["ping"]);
    result.client.stop();
    expect(socket.closeCalls).toBe(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("reports failed ping sends and closes the socket for reconnection", () => {
    vi.useFakeTimers();
    const sockets: FakeSocket[] = [];
    const result = makeClient(() => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    result.client.start();
    sockets[0].open();
    sockets[0].throwOnSend = true;
    vi.advanceTimersByTime(PING_INTERVAL_MS);
    expect(result.errors).toContain("Telemetry ping failed: send failed");
    expect(sockets[0].closeCalls).toBe(1);
    expect(result.statuses.at(-1)).toBe("reconnecting");
    result.client.stop();
  });

  it("backs off across short-lived opens and resets only after a stable connection", () => {
    vi.useFakeTimers();
    const sockets: FakeSocket[] = [];
    const result = makeClient(() => {
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
    result.client.start();
    sockets[0].close();
    vi.advanceTimersByTime(999);
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(2);

    sockets[1].open();
    vi.advanceTimersByTime(5_000);
    sockets[1].close();
    vi.advanceTimersByTime(1_999);
    expect(sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(3);

    sockets[2].open();
    vi.advanceTimersByTime(STABLE_CONNECTION_MS);
    sockets[2].close();
    vi.advanceTimersByTime(999);
    expect(sockets).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(4);
    result.client.stop();
  });

  it("reports stale state only after a live reading ages or its socket disconnects", () => {
    expect(isTelemetryStale("connected", null, 20_000)).toBe(false);
    expect(isTelemetryStale("connected", 10_001, 20_000)).toBe(false);
    expect(isTelemetryStale("connected", 10_000, 20_001)).toBe(true);
    expect(isTelemetryStale("reconnecting", 19_500, 20_000)).toBe(true);
    expect(isTelemetryStale("disconnected", null, 20_000)).toBe(false);
  });
});
