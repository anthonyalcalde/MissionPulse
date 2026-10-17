import {
  parseHistoryResponse,
  parseStatsResponse,
} from "./telemetry";
import type { MissionStatsResponse, TelemetryHistoryResponse } from "../types";

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
export const API_BASE_URL = configuredBaseUrl.replace(/\/+$/, "");

function validateMissionId(missionId: string): string {
  if (!missionId.trim() || missionId !== missionId.trim()) {
    throw new Error("Mission ID must be nonempty and must not have surrounding whitespace.");
  }
  return missionId;
}

async function getJson(url: string, signal?: AbortSignal): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(url, { signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new Error(`Could not reach the MissionPulse API at ${API_BASE_URL}.`);
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status}).`;
    try {
      const body: unknown = await response.json();
      if (
        typeof body === "object" &&
        body !== null &&
        "detail" in body &&
        typeof body.detail === "string"
      ) {
        detail = body.detail;
      }
    } catch {
      // Keep the HTTP status message when the server response is not JSON.
    }
    throw new Error(detail);
  }
  try {
    return await response.json();
  } catch {
    throw new Error("The MissionPulse API returned invalid JSON.");
  }
}

export async function fetchHistory(
  missionId: string,
  signal?: AbortSignal,
): Promise<TelemetryHistoryResponse> {
  validateMissionId(missionId);
  const query = new URLSearchParams({ limit: "500" });
  const response = parseHistoryResponse(
    await getJson(
      `${API_BASE_URL}/api/missions/${encodeURIComponent(missionId)}/telemetry?${query}`,
      signal,
    ),
  );
  if (response.mission_id !== missionId) {
    throw new Error("The history response belongs to a different mission.");
  }
  return response;
}

export async function fetchStats(
  missionId: string,
  interval: string,
  signal?: AbortSignal,
): Promise<MissionStatsResponse> {
  validateMissionId(missionId);
  const query = new URLSearchParams({ interval });
  const response = parseStatsResponse(
    await getJson(
      `${API_BASE_URL}/api/missions/${encodeURIComponent(missionId)}/stats?${query}`,
      signal,
    ),
  );
  if (response.mission_id !== missionId) {
    throw new Error("The statistics response belongs to a different mission.");
  }
  return response;
}

export function createWebSocketUrl(missionId: string): string {
  validateMissionId(missionId);
  const url = new URL(API_BASE_URL);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/ws/telemetry`;
  url.search = new URLSearchParams({ mission_id: missionId }).toString();
  return url.toString();
}
