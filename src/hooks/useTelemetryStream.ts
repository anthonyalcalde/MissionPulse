import { useEffect, useRef, useState } from "react";
import { createWebSocketUrl } from "../lib/api";
import { TelemetrySocketClient } from "../lib/telemetrySocket";
import { appendTelemetry } from "../lib/telemetry";
import type { ConnectionState, Telemetry } from "../types";

const BUFFER_CAPACITY = 500;

interface StreamState {
  missionId: string;
  status: ConnectionState;
  readings: Telemetry[];
  lastMessageAt: number | null;
  error: string | null;
  invalidMessages: number;
}

export function isTelemetryStale(
  status: ConnectionState,
  lastMessageAt: number | null,
  now: number,
  staleAfterMs = 10_000,
): boolean {
  return lastMessageAt !== null &&
    (status !== "connected" || now - lastMessageAt > staleAfterMs);
}

export function useTelemetryStream(missionId: string, enabled: boolean) {
  const [state, setState] = useState<StreamState>({
    missionId: "",
    status: "disconnected",
    readings: [],
    lastMessageAt: null,
    error: null,
    invalidMessages: 0,
  });
  const [retryKey, setRetryKey] = useState(0);
  const clientRef = useRef<TelemetrySocketClient | null>(null);

  useEffect(() => {
    if (!enabled || !missionId.trim()) {
      setState((current) => ({
        ...current,
        missionId,
        readings: current.missionId === missionId ? current.readings : [],
        lastMessageAt: current.missionId === missionId ? current.lastMessageAt : null,
        invalidMessages: current.missionId === missionId ? current.invalidMessages : 0,
        status: "disconnected",
        error: null,
      }));
      return;
    }

    setState((current) => ({
      ...current,
      missionId,
      status: retryKey > 0 ? "reconnecting" : "connecting",
      readings: current.missionId === missionId ? current.readings : [],
      error: null,
      lastMessageAt: current.missionId === missionId ? current.lastMessageAt : null,
      invalidMessages: current.missionId === missionId ? current.invalidMessages : 0,
    }));

    const client = new TelemetrySocketClient(createWebSocketUrl(missionId), {
      onStatus: (status) => {
        setState((current) => ({ ...current, status }));
      },
      onTelemetry: (reading) => {
        if (reading.mission_id !== missionId) return;
        setState((current) => ({
          ...current,
          readings: appendTelemetry(current.readings, reading, BUFFER_CAPACITY),
          lastMessageAt: Date.now(),
          error: null,
        }));
      },
      onError: (error) => {
        setState((current) => ({ ...current, error }));
      },
      onInvalidFrame: (error) => {
        setState((current) => ({
          ...current,
          error,
          invalidMessages: current.invalidMessages + 1,
        }));
      },
    });
    clientRef.current = client;
    client.start();

    return () => {
      client.stop();
      if (clientRef.current === client) clientRef.current = null;
    };
  }, [enabled, missionId, retryKey]);

  const disconnect = () => {
    clientRef.current?.stop();
    clientRef.current = null;
    setState((current) => ({ ...current, status: "disconnected", error: null }));
  };

  const reconnect = () => {
    clientRef.current?.stop();
    clientRef.current = null;
    setState((current) => ({ ...current, readings: [], lastMessageAt: null, error: null }));
    setRetryKey((value) => value + 1);
  };

  const isCurrentMission = state.missionId === missionId;
  return {
    ...state,
    status: isCurrentMission ? state.status : enabled ? "connecting" : "disconnected",
    readings: isCurrentMission ? state.readings : [],
    lastMessageAt: isCurrentMission ? state.lastMessageAt : null,
    error: isCurrentMission ? state.error : null,
    invalidMessages: isCurrentMission ? state.invalidMessages : 0,
    disconnect,
    reconnect,
  };
}
