"""Database engine/session. Timestamps are stored and returned as timezone-aware UTC (SRS 11.2)."""
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import DateTime, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Always bind/return aware UTC datetimes, even if the driver returns naive values."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def normalize_database_url(url: str) -> str:
    """Hosted providers (Aiven, Heroku, ...) hand out 'postgres://' or 'postgresql://' URIs; SQLAlchemy needs a driver name."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def init_engine(url: str) -> Engine:
    global engine, SessionLocal
    url = normalize_database_url(url)
    if not url.startswith("postgresql"):
        raise ValueError("DATABASE_URL must be a PostgreSQL URL (e.g. your Aiven Service URI). Other databases are not supported.")
    # pool_pre_ping + pool_recycle: managed Postgres closes idle connections, so stale ones are replaced instead of erroring.
    engine = create_engine(url, pool_pre_ping=True, pool_recycle=300)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine


def get_db() -> Iterator[Session]:
    assert SessionLocal is not None, "Database not initialised"
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
