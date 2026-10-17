"""PostgreSQL/TimescaleDB access and explicit, repeatable schema setup."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent

for _candidate in (_BACKEND_DIR / ".env", _REPO_ROOT / ".env", Path.cwd() / ".env"):
    if _candidate.is_file():
        load_dotenv(dotenv_path=_candidate)
        break
else:
    load_dotenv()


class DatabaseConfigurationError(RuntimeError):
    """Raised when the database cannot be used by the application."""


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise DatabaseConfigurationError(
            "DATABASE_URL is not configured. Set it in the environment or a local .env file."
        )
    return database_url


def get_connection():
    try:
        return psycopg2.connect(get_database_url(), connect_timeout=5)
    except DatabaseConfigurationError:
        raise
    except psycopg2.Error as error:
        raise DatabaseConfigurationError("Could not connect to the configured PostgreSQL database.") from error


@contextmanager
def get_db_connection():
    connection = get_connection()
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def get_db_cursor(commit: bool = False, dict_cursor: bool = False) -> Iterator:
    cursor_factory = RealDictCursor if dict_cursor else None
    with get_db_connection() as connection:
        cursor = connection.cursor(cursor_factory=cursor_factory)
        try:
            yield cursor
            if commit:
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()


def check_database_ready() -> None:
    """Check database connectivity, TimescaleDB, and initialized telemetry schema."""
    try:
        with get_db_cursor() as cursor:
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';")
            extension = cursor.fetchone()
            if not extension:
                raise DatabaseConfigurationError(
                    "TimescaleDB extension is not installed. Run the database initializer on a TimescaleDB database."
                )
            cursor.execute("SELECT time_bucket('1 second'::interval, NOW());")
            cursor.execute("SELECT to_regclass('public.telemetry') IS NOT NULL;")
            table_exists = cursor.fetchone()[0]
            if not table_exists:
                raise DatabaseConfigurationError(
                    "Telemetry schema is not initialized. Run `python -m backend.database init`."
                )
            columns = _columns(cursor)
            missing = {"timestamp", "mission_id", "pressure_pa", "flight_phase"} - columns
            if missing:
                raise DatabaseConfigurationError(
                    "Telemetry schema is missing required columns: " + ", ".join(sorted(missing))
                )
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM timescaledb_information.hypertables
                    WHERE hypertable_schema = 'public' AND hypertable_name = 'telemetry'
                );
                """
            )
            if not cursor.fetchone()[0]:
                raise DatabaseConfigurationError(
                    "The telemetry table is not a TimescaleDB hypertable. "
                    "Run `python -m backend.database init`."
                )
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'public.telemetry'::regclass
                      AND conname = 'telemetry_flight_phase_contract_check'
                );
                """
            )
            if not cursor.fetchone()[0]:
                raise DatabaseConfigurationError(
                    "The telemetry flight-phase constraint is missing. "
                    "Run `python -m backend.database init`."
                )
    except DatabaseConfigurationError:
        raise
    except psycopg2.Error as error:
        raise DatabaseConfigurationError(
            "Database readiness check failed. Verify credentials, connectivity, and TimescaleDB availability."
        ) from error


def _columns(cursor) -> set[str]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'telemetry';
        """
    )
    return {row[0] for row in cursor.fetchall()}


def initialize_schema() -> None:
    """Create/migrate the telemetry hypertable without deleting existing rows.

    Legacy `pressure` values are migrated only when every stored row has an
    explicit `pressure_unit` of Pa or kPa. Unitless legacy values are refused.
    """
    try:
        with get_db_cursor(commit=True) as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry (
                    timestamp TIMESTAMPTZ NOT NULL,
                    mission_id TEXT NOT NULL,
                    elapsed_seconds DOUBLE PRECISION,
                    altitude DOUBLE PRECISION,
                    velocity DOUBLE PRECISION,
                    acceleration DOUBLE PRECISION,
                    temperature DOUBLE PRECISION,
                    pressure_pa DOUBLE PRECISION,
                    flight_phase TEXT CHECK (
                        flight_phase IS NULL OR
                        flight_phase IN ('preflight', 'ascent', 'coast', 'apogee', 'descent', 'landed')
                    )
                );
                """
            )
            columns = _columns(cursor)
            required = {
                "timestamp",
                "mission_id",
                "elapsed_seconds",
                "altitude",
                "velocity",
                "acceleration",
                "temperature",
                "flight_phase",
            }
            missing = required - columns
            if missing:
                raise DatabaseConfigurationError(
                    "Existing telemetry table is missing required columns: " + ", ".join(sorted(missing))
                )
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM telemetry
                    WHERE flight_phase IS NOT NULL
                      AND flight_phase NOT IN ('preflight', 'ascent', 'coast', 'apogee', 'descent', 'landed')
                );
                """
            )
            if cursor.fetchone()[0]:
                raise DatabaseConfigurationError(
                    "Legacy telemetry contains undocumented flight phases. Audit/migrate them explicitly "
                    "before enforcing the documented phase contract."
                )
            cursor.execute(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'public.telemetry'::regclass
                  AND conname = 'telemetry_flight_phase_contract_check';
                """
            )
            phase_constraint = cursor.fetchone()
            if phase_constraint and not all(
                f"'{phase}'" in phase_constraint[0]
                for phase in ("preflight", "ascent", "coast", "apogee", "descent", "landed")
            ):
                raise DatabaseConfigurationError(
                    "An existing telemetry flight-phase constraint does not match the documented phases."
                )
            if not phase_constraint:
                cursor.execute(
                    """
                    ALTER TABLE telemetry
                    ADD CONSTRAINT telemetry_flight_phase_contract_check
                    CHECK (
                        flight_phase IS NULL OR
                        flight_phase IN ('preflight', 'ascent', 'coast', 'apogee', 'descent', 'landed')
                    );
                    """
                )

            if "pressure_pa" not in columns:
                if "pressure" in columns:
                    cursor.execute("SELECT COUNT(*) FROM telemetry;")
                    row_count = cursor.fetchone()[0]
                    if row_count:
                        if "pressure_unit" not in columns:
                            raise DatabaseConfigurationError(
                                "Cannot migrate nonempty legacy pressure data: pressure_unit metadata is missing. "
                                "Audit/normalize legacy rows explicitly, then rerun initialization."
                            )
                        cursor.execute(
                            """
                            SELECT COUNT(*) FROM telemetry
                            WHERE pressure_unit IS NULL OR pressure_unit NOT IN ('Pa', 'kPa');
                            """
                        )
                        invalid_units = cursor.fetchone()[0]
                        if invalid_units:
                            raise DatabaseConfigurationError(
                                "Cannot migrate legacy pressure data: every row must declare pressure_unit as Pa or kPa."
                            )
                        cursor.execute("ALTER TABLE telemetry ADD COLUMN pressure_pa DOUBLE PRECISION;")
                        cursor.execute(
                            """
                            UPDATE telemetry
                            SET pressure_pa = CASE pressure_unit
                                WHEN 'Pa' THEN pressure
                                WHEN 'kPa' THEN pressure * 1000.0
                            END
                            WHERE pressure_pa IS NULL;
                            """
                        )
                    else:
                        cursor.execute("ALTER TABLE telemetry ADD COLUMN pressure_pa DOUBLE PRECISION;")
                else:
                    cursor.execute("ALTER TABLE telemetry ADD COLUMN pressure_pa DOUBLE PRECISION;")

            cursor.execute(
                """
                SELECT data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'telemetry'
                  AND column_name = 'timestamp';
                """
            )
            timestamp_type = cursor.fetchone()
            if not timestamp_type or timestamp_type[0] != "timestamp with time zone":
                raise DatabaseConfigurationError(
                    "telemetry.timestamp must be TIMESTAMPTZ; automatic timezone conversion is unsafe."
                )

            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM timescaledb_information.hypertables
                    WHERE hypertable_schema = 'public' AND hypertable_name = 'telemetry'
                );
                """
            )
            is_hypertable = cursor.fetchone()[0]
            if not is_hypertable:
                cursor.execute(
                    """
                    SELECT create_hypertable(
                        'public.telemetry', 'timestamp',
                        if_not_exists => TRUE, migrate_data => TRUE
                    );
                    """
                )

            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM telemetry
                    GROUP BY mission_id, timestamp HAVING COUNT(*) > 1
                );
                """
            )
            duplicates_exist = cursor.fetchone()[0]
            if duplicates_exist:
                raise DatabaseConfigurationError(
                    "Duplicate (mission_id, timestamp) rows prevent safe idempotent ingestion. "
                    "Resolve duplicate legacy records explicitly before initialization."
                )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS telemetry_mission_timestamp_uidx
                ON telemetry (mission_id, timestamp);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS telemetry_timestamp_mission_idx
                ON telemetry (timestamp DESC, mission_id);
                """
            )
    except DatabaseConfigurationError:
        raise
    except psycopg2.Error as error:
        raise DatabaseConfigurationError(
            "Schema initialization failed. Confirm TimescaleDB is installed, the database user can create "
            "extensions/tables/indexes, and the legacy telemetry table matches the documented schema."
        ) from error


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "init"
    if action == "init":
        initialize_schema()
        print("MissionPulse TimescaleDB telemetry schema is initialized.")
        return 0
    if action == "check":
        check_database_ready()
        print("MissionPulse database and telemetry schema are ready.")
        return 0
    print("Usage: python -m backend.database [init|check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DatabaseConfigurationError as error:
        print(f"Database setup error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
