"""Admin authentication, rate limiting and audit helper (NFR-006, FR-023, 14.1)."""
import hmac
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.errors import ApiError
from app.models import AuditLog


def require_admin(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    """Admin routes need ADMIN_API_KEY via 'X-API-Key' or 'Authorization: Bearer'. Returns the actor name."""
    if not settings.admin_api_key:
        raise ApiError(503, "ADMIN_DISABLED", "Admin API is disabled (no ADMIN_API_KEY configured).")
    supplied = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else "")
    if not supplied or not hmac.compare_digest(supplied.encode(), settings.admin_api_key.encode()):
        raise ApiError(401, "UNAUTHORISED", "Missing or invalid admin credentials.")
    return "admin"


def audit(db: Session, actor: str, action: str, target: str, before: dict | None, after: dict | None) -> None:
    db.add(AuditLog(actor=actor, action=action, target=target, before=before, after=after))


class RateLimiter:
    """Small in-memory sliding-window limiter per client IP. For multiple instances use a shared store (e.g. Redis)."""

    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.lock = Lock()

    def check(self, bucket: str, client: str, limit: int, window: float = 60.0) -> None:
        now = time.monotonic()
        with self.lock:
            q = self.hits[(bucket, client)]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                raise ApiError(429, "RATE_LIMITED", "Too many requests. Please wait a moment and try again.")
            q.append(now)

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


limiter = RateLimiter()


def rate_limit(bucket: str, attr: str = "rate_limit_per_minute"):
    def dep(request: Request, settings: Settings = Depends(get_settings)) -> None:
        client = request.client.host if request.client else "unknown"
        limiter.check(bucket, client, getattr(settings, attr))
    return dep


__all__ = ["require_admin", "audit", "rate_limit", "limiter", "get_db"]
