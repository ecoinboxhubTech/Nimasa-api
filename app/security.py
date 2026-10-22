"""Admin authentication, rate limiting and audit helper (NFR-006, FR-023, 14.1)."""
import hmac
import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.errors import ApiError
from app.models import AdminSession, AdminUser, AuditLog
from app.passwords import token_digest


# Declared as security schemes so Swagger UI (/docs) shows an Authorize button and sends the credential.
bearer_scheme = HTTPBearer(auto_error=False, description="Session token from POST /api/v1/admin/auth/login")
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False, description="Optional service key")


def bearer_token(creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> str:
    return creds.credentials if creds else ""


def admin_principal(
    x_api_key: str | None = Depends(api_key_scheme),
    bearer: str = Depends(bearer_token),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> tuple[str, AdminUser | None]:
    """Who is calling an admin route: (actor name, staff account or None for the service key).

    Accepted credentials: a staff session token (from POST /admin/auth/login) as 'Authorization: Bearer',
    or the optional ADMIN_API_KEY service key as 'X-API-Key' / bearer."""
    supplied = x_api_key or bearer
    if not supplied:
        if not settings.admin_api_key and db.scalars(select(AdminUser.id).limit(1)).first() is None:
            raise ApiError(503, "ADMIN_DISABLED", "Admin access is not configured (no administrator accounts or service key).")
        raise ApiError(401, "UNAUTHORISED", "Sign in required.")
    if settings.admin_api_key and hmac.compare_digest(supplied.encode(), settings.admin_api_key.encode()):
        return "service-key", None
    session = db.scalars(select(AdminSession).where(AdminSession.token_digest == token_digest(supplied))).first()
    if session and not session.revoked and session.expires_at > utcnow() and session.user.active:
        return session.user.email, session.user
    if not settings.admin_api_key and db.scalars(select(AdminUser.id).limit(1)).first() is None:
        raise ApiError(503, "ADMIN_DISABLED", "Admin access is not configured (no administrator accounts or service key).")
    raise ApiError(401, "UNAUTHORISED", "Missing, invalid or expired credentials.")


def require_admin(principal: tuple[str, AdminUser | None] = Depends(admin_principal)) -> str:
    """Any authenticated administrator (staff login or service key). Returns the actor name for audit logs."""
    return principal[0]


def require_staff(principal: tuple[str, AdminUser | None] = Depends(admin_principal)) -> AdminUser:
    """A signed-in staff member. Staff management never works with the shared service key."""
    if principal[1] is None:
        raise ApiError(403, "STAFF_LOGIN_REQUIRED", "This action requires an administrator login, not the service key.")
    return principal[1]


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


__all__ = ["bearer_token", "require_admin", "require_staff", "admin_principal", "audit", "rate_limit", "limiter", "get_db"]
