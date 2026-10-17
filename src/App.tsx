import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { API_BASE_URL, fetchHistory, fetchStats } from "./lib/api";
import { isTelemetryStale, useTelemetryStream } from "./hooks/useTelemetryStream";
import { isFlightPhase, pressureInKilopascals, timestampToEpoch } from "./lib/telemetry";
import type { MetricBucket, Telemetry } from "./types";

const RECENT_MISSIONS_KEY = "missionpulse.recentMissions";
const STATS_INTERVALS = ["1 second", "5 seconds", "1 minute", "1 hour"];

function getRecentMissionIds(): string[] {
  try {
    const recent = JSON.parse(sessionStorage.getItem(RECENT_MISSIONS_KEY) ?? "[]") as unknown;
    return Array.isArray(recent)
      ? recent.filter((value): value is string => typeof value === "string" && value.trim().length > 0).slice(0, 5)
      : [];
  } catch {
    return [];
  }
}

function getInitialMissionId(): string {
  return getRecentMissionIds()[0] ?? "mission_001";
}

function rememberMission(missionId: string): string[] {
  try {
    const recent = [missionId, ...getRecentMissionIds().filter((item) => item !== missionId)].slice(0, 5);
    sessionStorage.setItem(RECENT_MISSIONS_KEY, JSON.stringify(recent));
    return recent;
  } catch {
    throw new Error("Could not save the recent mission list in this browser session.");
  }
}

function formatNumber(value: number | null | undefined, digits = 1): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString(undefined, { maximumFractionDigits: digits })
    : "—";
}

function formatTime(value: string | number | null): string {
  if (value === null) return "—";
  const date = new Date(typeof value === "number" ? value : timestampToEpoch(value));
  return Number.isFinite(date.getTime())
    ? date.toLocaleTimeString([], { timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })
    : "—";
}

function formatDateTime(value: string | number | null): string {
  if (value === null) return "—";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString([], {
        timeZone: "UTC",
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      })
    : "—";
}

function spansUtcDates(start: number | undefined, end: number | undefined): boolean {
  if (start === undefined || end === undefined) return false;
  return new Date(start).toISOString().slice(0, 10) !== new Date(end).toISOString().slice(0, 10);
}

function phaseLabel(phase: string | null | undefined): string {
  if (!phase) return "AWAITING DATA";
  return isFlightPhase(phase)
    ? phase.replaceAll("_", " ").toUpperCase()
    : `UNKNOWN PHASE · ${phase.slice(0, 32).toUpperCase()}`;
}

function MetricTile({
  label,
  value,
  unit,
  accent,
  detail,
}: {
  label: string;
  value: string;
  unit: string;
  accent: string;
  detail: string;
}) {
  return (
    <article className={`metric-tile ${accent}`}>
      <div className="metric-heading">
        <span>{label}</span>
        <span className="metric-dot" />
      </div>
      <div className="metric-value">{value}<span className="metric-unit">{unit}</span></div>
      <div className="metric-detail">{detail}</div>
    </article>
  );
}

function ChartTooltip({
  active,
  payload,
  label,
  includeDate = false,
}: {
  active?: boolean;
  payload?: Array<{ name: string; value: number; color: string }>;
  label?: number | string;
  includeDate?: boolean;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      <div className="tooltip-time">{includeDate
        ? formatDateTime(typeof label === "number" ? label : String(label ?? ""))
        : formatTime(typeof label === "number" ? label : String(label ?? ""))} UTC</div>
      {payload.map((item) => (
        <div className="tooltip-row" key={item.name}>
          <span style={{ color: item.color }}>●</span>
          <span>{item.name}</span>
          <strong>{formatNumber(item.value, 2)}</strong>
        </div>
      ))}
    </div>
  );
}

function App() {
  const [missionInput, setMissionInput] = useState(getInitialMissionId);
  const [activeMissionId, setActiveMissionId] = useState(getInitialMissionId);
  const [recentMissions, setRecentMissions] = useState(getRecentMissionIds);
  const [streamEnabled, setStreamEnabled] = useState(true);
  const [interval, setInterval] = useState("5 seconds");
  const [historical, setHistorical] = useState<Telemetry[]>([]);
  const [buckets, setBuckets] = useState<MetricBucket[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [statsLoading, setStatsLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [clock, setClock] = useState(Date.now());
  const stream = useTelemetryStream(activeMissionId, streamEnabled);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setHistoryLoading(true);
    setStatsLoading(true);
    setHistoryError(null);
    setStatsError(null);
    setHistorical([]);
    setBuckets([]);

    fetchHistory(activeMissionId, controller.signal)
      .then((response) => setHistorical(response.data))
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setHistoryError(error instanceof Error ? error.message : "Could not load telemetry history.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setHistoryLoading(false);
      });
    fetchStats(activeMissionId, interval, controller.signal)
      .then((response) => setBuckets(response.data))
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setStatsError(error instanceof Error ? error.message : "Could not load time-bucketed statistics.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setStatsLoading(false);
      });

    return () => controller.abort();
  }, [activeMissionId, interval, refreshKey]);

  const chartData = useMemo(() => {
    const combined = [...historical];
    for (const reading of stream.readings) {
      const index = combined.findIndex((item) => item.timestamp === reading.timestamp);
      if (index >= 0) combined[index] = reading;
      else combined.push(reading);
    }
    return combined
      .sort((left, right) => timestampToEpoch(left.timestamp) - timestampToEpoch(right.timestamp))
      .slice(-500)
      .map((reading) => ({ ...reading, time: timestampToEpoch(reading.timestamp) }));
  }, [historical, stream.readings]);

  const latestLive = stream.readings.at(-1) ?? null;
  const latest = latestLive ?? historical.at(-1) ?? null;
  const isStale = latestLive !== null &&
    isTelemetryStale(stream.status, stream.lastMessageAt, clock);
  const statusLabel = stream.status === "connected"
    ? isStale ? "STALE TELEMETRY" : latestLive ? "LIVE DATA" : "CONNECTED · AWAITING DATA"
    : stream.status === "reconnecting"
      ? latestLive ? "RECONNECTING · STALE DATA" : "RECONNECTING"
      : stream.status === "connecting"
        ? latestLive ? "CONNECTING · STALE DATA" : "CONNECTING"
        : stream.status === "error"
          ? latestLive ? "ERROR · STALE DATA" : "CONNECTION ERROR"
          : latestLive ? "DISCONNECTED · STALE DATA" : "DISCONNECTED";
  const statusClass = stream.status === "connected" && latestLive !== null && !isStale
    ? "online"
    : stream.status === "connecting" || stream.status === "reconnecting" || isStale
      ? "pending"
      : stream.status === "connected"
        ? "pending"
        : "offline";
  const peakAltitude = chartData.reduce<number | null>(
    (max, item) =>
      item.altitude !== null && (max === null || item.altitude > max) ? item.altitude : max,
    null,
  );
  const latestBucket = buckets.at(-1);
  const chartStart = chartData[0]?.time;
  const chartEnd = chartData.at(-1)?.time;
  const showChartDates = spansUtcDates(chartStart, chartEnd);
  const statsChartData = useMemo(
    () => buckets.map((bucket) => ({ ...bucket, time: timestampToEpoch(bucket.timestamp) })),
    [buckets],
  );
  const statsStart = statsChartData[0]?.time;
  const statsEnd = statsChartData.at(-1)?.time;
  const showStatsDates = spansUtcDates(statsStart, statsEnd);

  function connectMission(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const missionId = missionInput.trim();
    if (!missionId) {
      setHistoryError("Enter a mission ID to connect.");
      return;
    }
    try {
      setRecentMissions(rememberMission(missionId));
      if (missionId !== activeMissionId) {
        setHistorical([]);
        setBuckets([]);
        setHistoryLoading(true);
        setStatsLoading(true);
        setHistoryError(null);
        setStatsError(null);
      }
      setHistoryError(null);
      setMissionInput(missionId);
      setStreamEnabled(true);
      if (missionId === activeMissionId && stream.status !== "disconnected") {
        stream.reconnect();
      }
      setActiveMissionId(missionId);
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : "Could not save this mission.");
    }
  }

  function disconnectMission() {
    setStreamEnabled(false);
    stream.disconnect();
  }

  function refreshData() {
    setRefreshKey((value) => value + 1);
    if (streamEnabled && stream.status === "disconnected") stream.reconnect();
  }

  return (
    <main className="console-shell">
      <header className="topbar">
        <div className="brand" aria-label="MissionPulse">
          <span className="brand-mark"><span /></span>
          <span className="brand-name">MISSION<span>PULSE</span></span>
        </div>
        <div className="topbar-center">FLIGHT OPERATIONS CONSOLE <span className="topbar-divider">/</span> TELEMETRY</div>
        <div className="utc-clock"><span>UTC</span> {new Date(clock).toISOString().slice(11, 19)}</div>
      </header>

      <section className="mission-bar">
        <div className="mission-identity">
          <div className="eyebrow">MISSION CONTROL <span>·</span> FLIGHT TELEMETRY</div>
          <h1>{activeMissionId}</h1>
          <div className="mission-subtitle">REAL-TIME VEHICLE MONITORING <span>•</span> TIGER DATA TIME-SERIES</div>
        </div>
        <form className="mission-connect" onSubmit={connectMission}>
          <label htmlFor="mission-id">MISSION ID</label>
          <div className="connect-controls">
            <input
              id="mission-id"
              value={missionInput}
              onChange={(event) => setMissionInput(event.target.value)}
              maxLength={200}
              placeholder="Enter mission ID"
              aria-label="Mission ID"
              list="recent-mission-ids"
            />
            <datalist id="recent-mission-ids">
              {recentMissions.map((mission) => <option key={mission} value={mission} />)}
            </datalist>
            <button className="button-primary" type="submit">LOAD MISSION <span>↗</span></button>
          </div>
        </form>
      </section>

      <section className="status-strip" aria-live="polite">
        <div className="status-group">
          <span className={`status-indicator ${statusClass}`} />
          <span className={`status-text ${statusClass}`}>{statusLabel}</span>
          <span className="status-divider" />
          <span className="status-caption">
            {latestLive ? `LAST PACKET ${formatTime(latestLive.timestamp)} UTC` : "NO LIVE PACKETS RECEIVED"}
          </span>
        </div>
        <div className="status-actions">
          <span className="api-endpoint">{API_BASE_URL}</span>
          {streamEnabled && stream.status !== "disconnected" ? (
            <button className="text-button" onClick={disconnectMission}>DISCONNECT</button>
          ) : (
            <button
              className="text-button"
              onClick={() => {
                setStreamEnabled(true);
                stream.reconnect();
              }}
            >
              RECONNECT
            </button>
          )}
          <button className="icon-button" onClick={refreshData} aria-label="Refresh history and statistics" title="Refresh data">↻</button>
        </div>
      </section>

      {stream.error && <div className="notice notice-warn" role="status">{stream.error}</div>}

      <section className="metrics-grid" aria-label="Latest telemetry">
        <MetricTile
          label="ALTITUDE"
          value={formatNumber(latest?.altitude)}
          unit="m"
          accent="cyan"
          detail={peakAltitude === null ? "DISPLAYED DATA PEAK · —" : `DISPLAYED DATA PEAK · ${formatNumber(peakAltitude)} m`}
        />
        <MetricTile
          label="VELOCITY"
          value={formatNumber(latest?.velocity)}
          unit="m/s"
          accent="blue"
          detail={
            latest && latest.velocity !== null
              ? `${latest.velocity >= 0 ? "↑" : "↓"} VERTICAL VELOCITY`
              : "AWAITING TELEMETRY"
          }
        />
        <MetricTile
          label="ACCELERATION"
          value={formatNumber(latest?.acceleration, 2)}
          unit="m/s²"
          accent="violet"
          detail={latest?.acceleration === null || !latest ? "AWAITING TELEMETRY" : "VERTICAL ACCELERATION"}
        />
        <MetricTile
          label="TEMPERATURE"
          value={formatNumber(latest?.temperature)}
          unit="°C"
          accent="amber"
          detail={
            pressureInKilopascals(latest?.pressure ?? null, latest?.pressure_unit) !== null
              ? `PRESSURE · ${formatNumber(pressureInKilopascals(latest?.pressure ?? null, latest?.pressure_unit), 2)} kPa`
              : `PRESSURE · ${formatNumber(latest?.pressure, 1)} RAW · UNIT UNCONFIRMED`
          }
        />
      </section>

      <section className="flight-overview">
        <div className="phase-block">
          <span className="section-kicker">FLIGHT PHASE</span>
          <strong className={`phase-name phase-${latest?.flight_phase && isFlightPhase(latest.flight_phase) ? latest.flight_phase : "waiting"}`}>
            {phaseLabel(latest?.flight_phase)}
          </strong>
          <span className="phase-note">
            {latest?.flight_phase ? "PHASE FROM LATEST TELEMETRY" : "WAITING FOR REPORTED PHASE"}
          </span>
        </div>
        <div className="overview-item">
          <span className="section-kicker">ELAPSED MISSION TIME</span>
          <strong className="mono-value">
            {latest?.elapsed_seconds !== null && latest?.elapsed_seconds !== undefined
              ? `${formatNumber(latest.elapsed_seconds, 1)} s`
              : "—"}
          </strong>
          <span className="phase-note">SECONDS SINCE MISSION START</span>
        </div>
        <div className="overview-item">
          <span className="section-kicker">TELEMETRY BUFFER</span>
          <strong className="mono-value">{chartData.length.toString().padStart(3, "0")} <small>/ 500</small></strong>
          <span className="phase-note">{latestLive ? "HISTORICAL + LIVE SAMPLES" : "HISTORICAL SAMPLES"}</span>
        </div>
        <div className="overview-item">
          <span className="section-kicker">LATEST PACKET</span>
          <strong className="mono-value">{latest ? formatTime(latest.timestamp) : "—"}</strong>
          <span className="phase-note">UTC TIMESTAMP</span>
        </div>
      </section>

      <section className="primary-panel panel">
        <div className="panel-heading">
          <div>
            <div className="section-kicker">FLIGHT TELEMETRY <span className="live-tag"><i />{latestLive ? isStale ? "STALE STREAM DATA" : "LIVE STREAM" : "HISTORICAL"}</span></div>
            <h2>Vehicle altitude &amp; velocity</h2>
          </div>
          <div className="chart-legend">
            <span><i className="legend-altitude" /> ALTITUDE <small>m</small></span>
            <span><i className="legend-velocity" /> VELOCITY <small>m/s</small></span>
          </div>
        </div>
        {chartData.length ? (
          <div className="chart-wrap">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={chartData}
                margin={{ top: 12, right: 24, left: 18, bottom: 18 }}
                accessibilityLayer
                role="img"
                aria-label="Raw telemetry chart showing vehicle altitude in meters and vertical velocity in meters per second against UTC timestamp."
              >
                <CartesianGrid stroke="#1b2a37" strokeDasharray="2 5" vertical={false} />
                <XAxis
                  dataKey="time"
                  type="number"
                  domain={chartStart === chartEnd ? ["dataMin - 1000", "dataMax + 1000"] : ["dataMin", "dataMax"]}
                  tickFormatter={(value: number) => showChartDates ? formatDateTime(value) : formatTime(value)}
                  tick={{ fill: "#657789", fontSize: 10, fontFamily: "monospace" }}
                  axisLine={{ stroke: "#253442" }}
                  tickLine={false}
                  minTickGap={44}
                  label={{ value: "Timestamp (UTC)", position: "insideBottom", offset: -12, fill: "#718292", fontSize: 9 }}
                />
                <YAxis
                  yAxisId="altitude"
                  tick={{ fill: "#657789", fontSize: 10, fontFamily: "monospace" }}
                  axisLine={false}
                  tickLine={false}
                  width={48}
                  label={{ value: "Altitude (m)", angle: -90, position: "insideLeft", fill: "#718292", fontSize: 9 }}
                />
                <YAxis
                  yAxisId="velocity"
                  orientation="right"
                  tick={{ fill: "#657789", fontSize: 10, fontFamily: "monospace" }}
                  axisLine={false}
                  tickLine={false}
                  width={42}
                  label={{ value: "Velocity (m/s)", angle: 90, position: "insideRight", fill: "#718292", fontSize: 9 }}
                />
                <Tooltip content={<ChartTooltip includeDate={showChartDates} />} />
                <Line yAxisId="altitude" type="monotone" dataKey="altitude" name="Altitude (m)" stroke="#50d5d0" strokeWidth={2} dot={false} activeDot={{ r: 4, fill: "#50d5d0", stroke: "#0b1219", strokeWidth: 2 }} isAnimationActive={false} />
                <Line yAxisId="velocity" type="monotone" dataKey="velocity" name="Velocity (m/s)" stroke="#7e9af7" strokeWidth={1.7} dot={false} activeDot={{ r: 4, fill: "#7e9af7", stroke: "#0b1219", strokeWidth: 2 }} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="empty-chart">
            <div className="empty-mark">⌁</div>
            <strong>{historyLoading ? "LOADING MISSION TELEMETRY" : "NO TELEMETRY DATA"}</strong>
            <span>{historyError ?? "Historical records and live packets will appear here when available."}</span>
          </div>
        )}
        <div className="chart-footer">
          <span><i className="footer-live-dot" /> {latestLive ? "LIVE POINTS APPENDED AS RECEIVED" : "NO SYNTHETIC DATA DISPLAYED"}</span>
          <span>{chartData.length
            ? showChartDates
              ? `${formatDateTime(chartData[0].time)} — ${formatDateTime(chartData.at(-1)?.time ?? null)} UTC`
              : `${formatTime(chartData[0].time)} — ${formatTime(chartData.at(-1)?.time ?? null)} UTC`
            : "—"}</span>
        </div>
        {chartData.length > 0 && (
          <details className="chart-data-table">
            <summary>View latest raw telemetry values</summary>
            <div className="table-scroll">
              <table>
                <caption>Latest 20 raw telemetry records, in timestamp order</caption>
                <thead><tr><th>Timestamp (UTC)</th><th>Phase</th><th>Altitude (m)</th><th>Velocity (m/s)</th><th>Acceleration (m/s²)</th><th>Temperature (°C)</th><th>Pressure ({latest?.pressure_unit ?? "unit unconfirmed"})</th></tr></thead>
                <tbody>{chartData.slice(-20).map((reading) => (
                  <tr key={reading.timestamp}>
                    <td>{formatDateTime(reading.timestamp)}</td>
                    <td>{phaseLabel(reading.flight_phase)}</td>
                    <td>{formatNumber(reading.altitude)}</td>
                    <td>{formatNumber(reading.velocity)}</td>
                    <td>{formatNumber(reading.acceleration, 2)}</td>
                    <td>{formatNumber(reading.temperature)}</td>
                    <td>{reading.pressure_unit === "Pa" && reading.pressure !== null
                      ? formatNumber(reading.pressure, 0)
                      : `${formatNumber(reading.pressure, 1)} (unit unconfirmed)`}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </details>
        )}
      </section>

      <section className="analysis-layout">
        <article className="panel analysis-panel">
          <div className="panel-heading compact">
            <div>
              <div className="section-kicker">TIME-SERIES ANALYTICS <span className="source-tag">API AGGREGATION</span></div>
              <h2>Bucketed mission statistics</h2>
            </div>
            <label className="interval-select">
              <span>BUCKET</span>
              <select value={interval} onChange={(event) => setInterval(event.target.value)}>
                {STATS_INTERVALS.map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
            </label>
          </div>
          {buckets.length ? (
            <>
              <div className="stats-chart-grid">
                {[
                  { label: "ALTITUDE", unit: "m", average: "avg_altitude", maximum: "max_altitude", color: "#50d5d0", maxColor: "#b2e9e6" },
                  { label: "VELOCITY", unit: "m/s", average: "avg_velocity", maximum: "max_velocity", color: "#7e9af7", maxColor: "#bdc8ff" },
                  { label: "ACCELERATION", unit: "m/s²", average: "avg_acceleration", maximum: "max_acceleration", color: "#b493e5", maxColor: "#d8c5f2" },
                  { label: "TEMPERATURE", unit: "°C", average: "avg_temperature", maximum: "max_temperature", color: "#e3b86c", maxColor: "#f5ddad" },
                ].map((metric) => (
                <div className="stats-chart-card" key={metric.label}>
                  <div className="stats-chart-title">{metric.label} <span>{metric.unit} · SERVER BUCKETS</span></div>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={statsChartData}
                    margin={{ top: 8, right: 12, left: 8, bottom: 18 }}
                    accessibilityLayer
                    role="img"
                    aria-label={`${metric.label} bucketed average${metric.maximum ? " and maximum" : ""} in ${metric.unit}, by UTC timestamp.`}
                  >
                    <CartesianGrid stroke="#1b2a37" strokeDasharray="2 5" vertical={false} />
                    <XAxis
                      dataKey="time"
                      type="number"
                      domain={statsStart === statsEnd ? ["dataMin - 1000", "dataMax + 1000"] : ["dataMin", "dataMax"]}
                      tickFormatter={(value: number) => showStatsDates ? formatDateTime(value) : formatTime(value)}
                      tick={{ fill: "#657789", fontSize: 9, fontFamily: "monospace" }}
                      axisLine={{ stroke: "#253442" }}
                      tickLine={false}
                      minTickGap={40}
                      label={{ value: "Timestamp (UTC)", position: "insideBottom", offset: -12, fill: "#718292", fontSize: 8 }}
                    />
                    <YAxis
                      tick={{ fill: "#657789", fontSize: 9 }}
                      axisLine={false}
                      tickLine={false}
                      width={42}
                      label={{ value: metric.unit, angle: -90, position: "insideLeft", fill: "#718292", fontSize: 8 }}
                    />
                    <Tooltip content={<ChartTooltip includeDate={showStatsDates} />} />
                    <Line type="monotone" dataKey={metric.average} name={`Average ${metric.label.toLowerCase()} (${metric.unit})`} stroke={metric.color} strokeWidth={1.8} dot={false} isAnimationActive={false} />
                    {metric.maximum && <Line type="monotone" dataKey={metric.maximum} name={`Maximum ${metric.label.toLowerCase()} (${metric.unit})`} stroke={metric.maxColor} strokeWidth={1.2} strokeDasharray="4 4" dot={false} isAnimationActive={false} />}
                  </LineChart>
                </ResponsiveContainer>
                </div>
                ))}
              </div>
              <div className="stats-summary">
                <div><span>BUCKETS RETURNED</span><strong>{buckets.length}</strong></div>
                <div><span>SAMPLES IN LATEST BUCKET</span><strong>{latestBucket?.sample_count ?? "—"}</strong></div>
                <div><span>LATEST BUCKET MAX ALT.</span><strong>{formatNumber(latestBucket?.max_altitude)} <small>m</small></strong></div>
              </div>
              <p className="footnote">Server-computed time_bucket averages and maximums. Not downsampled; no mission-wide totals implied.</p>
              <details className="chart-data-table stats-data-table">
                <summary>View bucketed statistics table</summary>
                <div className="table-scroll">
                  <table>
                    <caption>Latest 100 backend time buckets, in bucket order</caption>
                    <thead><tr><th>Bucket (UTC)</th><th>Samples</th><th>Avg altitude (m)</th><th>Max altitude (m)</th><th>Avg velocity (m/s)</th><th>Max velocity (m/s)</th><th>Avg acceleration (m/s²)</th><th>Max acceleration (m/s²)</th><th>Avg temperature (°C)</th><th>Max temperature (°C)</th></tr></thead>
                    <tbody>{buckets.slice(-100).map((bucket) => (
                      <tr key={bucket.bucket}>
                        <td>{formatDateTime(bucket.timestamp)}</td><td>{bucket.sample_count}</td>
                        <td>{formatNumber(bucket.avg_altitude)}</td><td>{formatNumber(bucket.max_altitude)}</td>
                        <td>{formatNumber(bucket.avg_velocity)}</td><td>{formatNumber(bucket.max_velocity)}</td>
                        <td>{formatNumber(bucket.avg_acceleration, 2)}</td><td>{formatNumber(bucket.max_acceleration, 2)}</td>
                        <td>{formatNumber(bucket.avg_temperature)}</td><td>{formatNumber(bucket.max_temperature)}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              </details>
            </>
          ) : (
            <div className="compact-empty">
              <strong>{statsLoading ? "QUERYING TIME BUCKETS" : "NO AGGREGATED DATA"}</strong>
              <span>{statsError ?? "Statistics will appear when the API returns buckets for this mission."}</span>
            </div>
          )}
        </article>

        <aside className="panel signal-panel">
          <div className="panel-heading compact">
            <div>
              <div className="section-kicker">FLIGHT DATA STATUS</div>
              <h2>Signal integrity</h2>
            </div>
            <span className={`integrity-badge ${statusClass}`}><i />{statusClass === "online" ? "NOMINAL" : statusClass === "pending" ? "STANDBY" : "OFFLINE"}</span>
          </div>
          <div className="signal-list">
            <div className="signal-row"><span>WebSocket</span><strong className={statusClass}>{stream.status.toUpperCase()}</strong></div>
            <div className="signal-row"><span>Last received</span><strong>{latestLive ? `${formatTime(latestLive.timestamp)} UTC` : "No live packets"}</strong></div>
            <div className="signal-row"><span>Malformed packets</span><strong>{stream.invalidMessages}</strong></div>
            <div className="signal-row"><span>History query</span><strong className={historyError ? "offline" : historyLoading ? "pending" : "online"}>{historyError ? "ERROR" : historyLoading ? "LOADING" : `${historical.length} RECORDS`}</strong></div>
            <div className="signal-row"><span>Stats query</span><strong className={statsError ? "offline" : statsLoading ? "pending" : "online"}>{statsError ? "ERROR" : statsLoading ? "LOADING" : `${buckets.length} BUCKETS`}</strong></div>
          </div>
          {(historyError || statsError) && (
            <div className="signal-errors">
              {historyError && <p><b>HISTORY:</b> {historyError}</p>}
              {statsError && <p><b>STATS:</b> {statsError}</p>}
            </div>
          )}
        </aside>
      </section>

      <footer className="console-footer">
        <span>MISSIONPULSE <b>FLIGHT SYSTEMS</b></span>
        <span>PRESSURE IN Pa · DASHBOARD CONVERTS TO kPa <i /> PHASE REPORTED BY VEHICLE STREAM</span>
        <span>LIVE + HISTORICAL <b>·</b> {chartStart && chartEnd ? `${formatTime(chartStart)}–${formatTime(chartEnd)} UTC` : "NO DATA"}</span>
      </footer>
    </main>
  );
}

export default App;
