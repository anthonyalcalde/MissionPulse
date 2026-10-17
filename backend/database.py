"""Database connection module for MissionPulse backend.

Handles connection to Tiger Cloud (TimescaleDB / PostgreSQL)
using psycopg2 and python-dotenv.
"""

import os
import sys
from pathlib import Path
from contextlib import contextmanager
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Search for .env in backend/ directory, project root, and current working directory
_BACKEND_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_DIR.parent

_env_candidates = [
    _BACKEND_DIR / ".env",
    _REPO_ROOT / ".env",
    Path.cwd() / ".env",
]

for _candidate in _env_candidates:
    if _candidate.is_file():
        load_dotenv(dotenv_path=_candidate)
        break
else:
    load_dotenv()


def get_database_url() -> str:
    """Retrieve DATABASE_URL from environment variables.

    Raises:
        ValueError: If DATABASE_URL is not set.
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError(
            "DATABASE_URL is not set in environment or local .env file. "
            "Please configure DATABASE_URL in a local .env file (see .env.example)."
        )
    return database_url


def get_connection():
    """Establish and return a new psycopg2 database connection.

    Never logs or exposes credentials.
    """
    database_url = get_database_url()
    return psycopg2.connect(database_url)


@contextmanager
def get_db_connection():
    """Context manager to obtain a database connection and ensure closure."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_db_cursor(commit: bool = False, dict_cursor: bool = False):
    """Context manager providing a database cursor.

    Args:
        commit: If True, commits transaction on success.
        dict_cursor: If True, returns rows as dictionaries.
    """
    cursor_factory = RealDictCursor if dict_cursor else None
    with get_db_connection() as conn:
        cursor = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cursor
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()


def test_connection() -> bool:
    """Test the database connection by executing 'SELECT 1;'.

    Returns:
        bool: True if connection and test query succeed, False otherwise.
    """
    print("Initiating database connection test...")
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT 1;")
            result = cur.fetchone()
            if result and result[0] == 1:
                print("Database connection test PASSED: Successfully connected and executed 'SELECT 1'.")
                return True
            else:
                print(f"Database connection test FAILED: Unexpected query result: {result}")
                return False
    except ValueError as err:
        print(f"Configuration Error: {err}")
        return False
    except psycopg2.Error as err:
        print(f"Database Connection Error: {err}")
        return False
    except Exception as err:
        print(f"Unexpected Error during connection test: {err}")
        return False


if __name__ == "__main__":
    success = test_connection()
    sys.exit(0 if success else 1)
