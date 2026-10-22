"""Connectivity check for the configured database (Aiven PostgreSQL).

Run:  python -m app.dbcheck
Prints the database type, server version and whether TLS is in use. The password is never printed."""
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app import db
from app.config import get_settings


def main() -> int:
    url = db.normalize_database_url(get_settings().database_url)
    shown = make_url(url).render_as_string(hide_password=True)
    print(f"Connecting to {shown}")
    try:
        engine = db.init_engine(url)
        with engine.connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar()
            ssl = conn.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")).scalar()
            name = conn.execute(text("SELECT current_database()")).scalar()
            print(f"OK  PostgreSQL {version}, database '{name}', TLS: {'yes' if ssl else 'NO (add ?sslmode=require)'}")
    except Exception as e:  # report the reason without echoing credentials
        print(f"FAILED  {type(e).__name__}: {str(e).splitlines()[0][:300]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
