import { parseTelemetry } from "./telemetry";
import type { ConnectionState, Telemetry } from "../types";

export const PING_INTERVAL_MS = 25_000;
export const STABLE_CONNECTION_MS = 30_000;
const INITIAL_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;
const SOCKET_OPEN = 1;

type SocketFactory = (url: string) => WebSocket;

export type ParsedFrame =
  | { kind: "pong" }
  | { kind: "telemetry"; reading: Telemetry }
  | { kind: "invalid"; error: string };

export function parseWebSocketFrame(data: unknown): ParsedFrame {
  if (typeof data !== "string") {
    return { kind: "invalid", error: "Ignored unexpected non-text WebSocket frame." };
  }
  if (data.trim().toLowerCase() === "pong") return { kind: "pong" };
  try {
    return { kind: "telemetry", reading: parseTelemetry(JSON.parse(data) as unknown) };
  } catch (error) {
    return {
      kind: "invalid",
      error: error instanceof Error ? `Ignored malformed telemetry: ${error.message}` : "Ignored malformed telemetry.",
    };
  }
}

interface TelemetrySocketCallbacks {
  onStatus(status: ConnectionState): void;
  onTelemetry(reading: Telemetry): void;
  onError(message: string): void;
  onInvalidFrame(message: string): void;
}

export class TelemetrySocketClient {
  private socket: WebSocket | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private stableTimer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private started = false;
  private stopped = false;

  constructor(
    private readonly url: string,
    private readonly callbacks: TelemetrySocketCallbacks,
    private readonly createSocket: SocketFactory = (socketUrl) => new WebSocket(socketUrl),
  ) {}

  start(): void {
    if (this.started || this.stopped) return;
    this.started = true;
    this.connect();
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.clearReconnectTimer();
    this.clearConnectionTimers();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      this.detachHandlers(socket);
      socket.close();
    }
    this.callbacks.onStatus("disconnected");
  }

  private connect(): void {
    if (this.stopped || this.socket) return;
    this.callbacks.onStatus(this.failures ? "reconnecting" : "connecting");
    let socket: WebSocket;
    try {
      socket = this.createSocket(this.url);
    } catch (error) {
      this.callbacks.onError(error instanceof Error ? error.message : "Could not open telemetry connection.");
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    const onOpen: EventListener = () => {
      if (this.stopped || this.socket !== socket) return;
      if (this.pingTimer !== null) return;
      this.callbacks.onStatus("connected");
      this.stableTimer = setTimeout(() => {
        this.stableTimer = null;
        if (!this.stopped && this.socket === socket) this.failures = 0;
      }, STABLE_CONNECTION_MS);
      this.pingTimer = setInterval(() => {
        if (socket.readyState !== SOCKET_OPEN) return;
        try {
          socket.send("ping");
        } catch (error) {
          this.callbacks.onError(
            error instanceof Error ? `Telemetry ping failed: ${error.message}` : "Telemetry ping failed.",
          );
          try {
            socket.close();
          } catch {
            this.finishSocket(socket);
          }
        }
      }, PING_INTERVAL_MS);
    };
    const onMessage: EventListener = (event) => {
      if (this.stopped || this.socket !== socket) return;
      const frame = parseWebSocketFrame((event as MessageEvent<unknown>).data);
      if (frame.kind === "pong") return;
      if (frame.kind === "invalid") {
        this.callbacks.onInvalidFrame(frame.error);
        return;
      }
      this.callbacks.onTelemetry(frame.reading);
    };
    const onError: EventListener = () => {
      if (!this.stopped && this.socket === socket) {
        this.callbacks.onError("Telemetry connection failed; retrying.");
      }
    };
    const onClose: EventListener = () => this.finishSocket(socket);
    socket.addEventListener("open", onOpen);
    socket.addEventListener("message", onMessage);
    socket.addEventListener("error", onError);
    socket.addEventListener("close", onClose);
    this.handlers.set(socket, {
      open: onOpen,
      message: onMessage,
      error: onError,
      close: onClose,
    });
  }

  private finishSocket(socket: WebSocket): void {
    if (this.socket !== socket) return;
    this.clearConnectionTimers();
    this.detachHandlers(socket);
    this.socket = null;
    if (!this.stopped) this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) return;
    this.failures += 1;
    const delay = Math.min(
      INITIAL_RECONNECT_DELAY_MS * 2 ** (this.failures - 1),
      MAX_RECONNECT_DELAY_MS,
    );
    this.callbacks.onStatus("reconnecting");
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private clearConnectionTimers(): void {
    if (this.pingTimer !== null) clearInterval(this.pingTimer);
    if (this.stableTimer !== null) clearTimeout(this.stableTimer);
    this.pingTimer = null;
    this.stableTimer = null;
  }

  private detachHandlers(socket: WebSocket): void {
    const handlers = this.handlers.get(socket);
    if (!handlers) return;
    socket.removeEventListener("open", handlers.open);
    socket.removeEventListener("message", handlers.message);
    socket.removeEventListener("error", handlers.error);
    socket.removeEventListener("close", handlers.close);
    this.handlers.delete(socket);
  }

  private handlers = new WeakMap<WebSocket, {
    open: EventListener;
    message: EventListener;
    error: EventListener;
    close: EventListener;
  }>();
}
