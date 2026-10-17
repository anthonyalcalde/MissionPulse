# MissionPulse

MissionPulse simulates a rocket flight, streams its telemetry through FastAPI, stores raw time-series records in PostgreSQL/TimescaleDB (including Tiger Data), and displays live telemetry and time-bucketed historical statistics in a React dashboard.

## Requirements

- Windows 10/11 with Python 3.12 (3.11+ should work) and Node.js 22 LTS.
- npm 10.9.2 (declared package-manager baseline; npm 10.9.x through 11 is supported).
- Docker Desktop with Linux containers for the reproducible local TimescaleDB service, or a provisioned Tiger Data/PostgreSQL database with TimescaleDB enabled.

The frontend's Vite 6/Vitest 4 engine ranges support Node 20, 22, or 24+, not odd-numbered Node 21/23. On Windows, install Node 22 LTS (for example with nvm-windows), then run `npm install --global npm@10.9.2`. Check `node --version` and `npm --version`; use `npm ci` for a reproducible frontend install. `.npmrc` preserves the lockfile's legacy peer resolution setting.

## Configure the database

From the repository root:

```powershell
Copy-Item .env.example .env
```

The example configures the local Compose database. Change its development password before using it outside a private local machine. Keep `.env` private; it is ignored by Git. For Tiger Data, set `DATABASE_URL` to the connection string from the Tiger Data project (including the provider's required TLS options). The URL is read by the backend and must never be placed in frontend variables.

Start local TimescaleDB:

```powershell
docker compose up -d
```

The service listens only on `localhost:5432` and persists data in the `missionpulse-timescale` named volume. Schema initialization is explicit and repeatable; it preserves rows and sets up the TimescaleDB extension, telemetry hypertable, documented phase constraint, and indexes:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt -r backend\requirements-dev.txt
python -m backend.database init
python -m backend.database check
```

If an existing table contains legacy `pressure` values, initialization migrates them only when every row has an explicit `pressure_unit` of `Pa` or `kPa`. Unitless or inconsistent legacy data is refused without reinterpretation; back up and audit such data, then explicitly normalize it before retrying. The new canonical storage column is `pressure_pa`. Initialization also stops if legacy timestamps are not `TIMESTAMPTZ`, undocumented phases exist, or duplicate `(mission_id, timestamp)` records prevent safe idempotent writes.

## Start the services

Use separate PowerShell windows from the repository root.

**Backend API:**

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

`GET http://localhost:8000/` is a process health check. `GET http://localhost:8000/health/ready` also checks PostgreSQL, TimescaleDB, and the telemetry schema. The database CLI has the corresponding `check` command.

**Frontend:**

```powershell
npm ci
Copy-Item .env.frontend.example .env.local
npm run dev
```

Open the Vite URL (normally `http://localhost:5173`) and enter a mission ID. `VITE_API_BASE_URL` in the ignored `.env.local` sets the API origin; keep it separate from the root `.env` containing the backend database credentials. The WebSocket uses that same host and switches to `ws`/`wss` as appropriate.

**Simulator and ingestion:**

```powershell
.\.venv\Scripts\Activate.ps1
python -m simulator.demo --mission-id mission_demo --api-url http://localhost:8000 --speed 1
```

The runner generates records with the actual simulator and posts sequentially to `POST /api/telemetry`. Transient network/server failures are retried with exponential delay; a permanent rejection or exhausted retry limit exits visibly rather than dropping the record and continuing. `--fast` removes simulation pacing but still waits for each accepted POST. `--retries N` controls retries. Without `--api-url`, the simulator remains independently usable and emits JSONL to stdout:

```powershell
python -m simulator.demo --mission-id mission_jsonl --fast
```

Open the dashboard and load `mission_demo` before starting the simulator to watch the live WebSocket. After telemetry is stored, the frontend loads raw history and backend-computed bucket averages/maxima. The API also exposes:

```text
GET /api/missions/{mission_id}/telemetry?limit=500&offset=0
GET /api/missions/{mission_id}/stats?interval=5%20seconds
WS  /ws/telemetry?mission_id={mission_id}
```

The supported statistics intervals are `1 second`, `5 seconds`, `1 minute`, and `1 hour`. These are time-bucket aggregations, not LTTB downsampling or mission-wide statistics. The WebSocket sends a raw telemetry object per message and responds to client text `ping` with `pong`.

## Telemetry contract

Simulator, API, database, and frontend use timestamps in UTC and the documented phases `preflight`, `ascent`, `coast`, `apogee`, `descent`, and `landed`. Pressure is generated in Pa, accepted and stored in Pa, and returned with `pressure_unit: "Pa"`; the dashboard converts to kPa only when this explicit unit is present. Other measurements use altitude m, velocity m/s, acceleration m/s², and temperature °C. Pressure is in Pa; database storage uses `pressure_pa`.

## Checks

```powershell
python -m pytest backend\tests simulator\tests -q
npm test
npm run build
```

The backend unit/API tests use an in-memory fake cursor, not PostgreSQL. `backend/tests/test_database_integration.py` runs persistence and aggregation against an isolated TimescaleDB database only when `TEST_DATABASE_URL` is set; it refuses to reuse `DATABASE_URL`. Simulator-to-API tests exercise the real simulator and HTTP client against the API with the database mocked. For a real local persistence check, use the Compose TimescaleDB service and follow the service startup steps above. Testing against local Compose verifies PostgreSQL/TimescaleDB, not Tiger Data; Tiger Data integration is verified only when configured with and tested against that service.
