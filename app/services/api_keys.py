"""Developer API keys: generation, lookup, quota metering and usage summaries."""
import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utcnow
from app.errors import ApiError
from app.models import ApiKey, ApiUsage


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_key(db: Session, settings: Settings, name: str, owner_email: str | None = None,
               quota_per_day: int | None = None, environment: str = "test") -> tuple[ApiKey, str]:
    if environment not in {"test", "production"}:
        raise ValueError(f"Unsupported API key environment: {environment}")
    raw = f"bl_{'live' if environment == 'production' else 'test'}_" + secrets.token_urlsafe(32)
    key = ApiKey(
        name=name.strip(),
        prefix=raw[:16],
        key_digest=digest(raw),
        owner_email=owner_email.strip().lower() if owner_email else None,
        environment=environment,
        quota_per_day=quota_per_day or settings.developer_key_quota_per_day,
        rate_limit_per_minute=settings.developer_key_rate_per_minute,
    )
    db.add(key)
    db.commit()
    return key, raw


def resolve(db: Session, raw: str | None) -> ApiKey | None:
    if not raw:
        return None
    key = db.scalars(select(ApiKey).where(ApiKey.key_digest == digest(raw))).first()
    if key is None or not key.active or key.revoked_at is not None:
        return None
    return key


def usage_today(db: Session, key: ApiKey) -> ApiUsage:
    day = datetime.now(timezone.utc).date().isoformat()
    return db.scalars(select(ApiUsage).where(ApiUsage.api_key_id == key.id, ApiUsage.day == day)).first() or ApiUsage(
        api_key_id=key.id, day=day, requests=0, errors=0)


def meter(db: Session, key: ApiKey, path: str) -> None:
    """Atomically count a request and enforce the daily quota. Safe with several workers."""
    day = datetime.now(timezone.utc).date().isoformat()
    stmt = insert(ApiUsage).values(api_key_id=key.id, day=day, requests=1, errors=0, last_path=path[:300])
    stmt = stmt.on_conflict_do_update(
        index_elements=["api_key_id", "day"],
        set_={"requests": ApiUsage.__table__.c.requests + 1, "last_path": path[:300]},
    ).returning(ApiUsage.requests)
    requests = db.execute(stmt).scalar_one()
    if requests > key.quota_per_day:
        db.rollback()
        raise ApiError(429, "API_QUOTA_EXCEEDED", f"Daily sandbox quota of {key.quota_per_day} requests reached.")
    key.last_used_at = utcnow()
    db.commit()
