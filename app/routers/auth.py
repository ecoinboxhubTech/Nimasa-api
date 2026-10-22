"""Administrator sign-in. The public never logs in; only authorised staff do (PRD 14.2)."""
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.errors import ApiError
from app.models import AdminSession, AdminUser
from app.passwords import DUMMY_HASH, hash_password, new_token, token_digest, verify_password
from app.schemas import LoginIn, PasswordChangeIn
from app.security import audit, bearer_token, rate_limit, require_staff

router = APIRouter(prefix="/api/v1/admin/auth", tags=["admin-auth"])

BAD_LOGIN = ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password, or the account is temporarily locked.")


def user_out(u: AdminUser) -> dict:
    return {"id": u.id, "email": u.email, "active": u.active, "created_at": u.created_at, "last_login_at": u.last_login_at}


@router.post("/login", dependencies=[Depends(rate_limit("login", "login_rate_limit_per_minute"))])
def login(body: LoginIn, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Returns a bearer session token. Failures are deliberately indistinguishable (no account enumeration)."""
    now = utcnow()
    user = db.scalars(select(AdminUser).where(AdminUser.email == body.email.strip().lower())).first()
    if user is None:
        verify_password(body.password, DUMMY_HASH)  # equalise timing
        raise BAD_LOGIN
    if not user.active or (user.locked_until and user.locked_until > now):
        verify_password(body.password, DUMMY_HASH)
        raise BAD_LOGIN
    if not verify_password(body.password, user.password_hash):
        user.failed_attempts += 1
        if user.failed_attempts >= settings.admin_max_failed_logins:
            user.locked_until = now + timedelta(minutes=settings.admin_lockout_minutes)
            user.failed_attempts = 0
            audit(db, user.email, "auth.locked", user.email, None, {"locked_until": user.locked_until.isoformat()})
        db.commit()
        raise BAD_LOGIN
    token = new_token()
    expires = now + timedelta(hours=settings.admin_session_hours)
    user.failed_attempts, user.locked_until, user.last_login_at = 0, None, now
    db.add(AdminSession(token_digest=token_digest(token), user_id=user.id, expires_at=expires))
    audit(db, user.email, "auth.login", user.email, None, None)
    db.commit()
    return {"access_token": token, "token_type": "bearer", "expires_at": expires, "user": user_out(user)}


@router.post("/logout")
def logout(token: str = Depends(bearer_token), user: AdminUser = Depends(require_staff), db: Session = Depends(get_db)):
    db.execute(update(AdminSession).where(AdminSession.token_digest == token_digest(token)).values(revoked=True))
    audit(db, user.email, "auth.logout", user.email, None, None)
    db.commit()
    return {"status": "signed_out"}


@router.get("/me")
def me(user: AdminUser = Depends(require_staff)):
    return user_out(user)


@router.post("/change-password")
def change_password(body: PasswordChangeIn, token: str = Depends(bearer_token),
                    user: AdminUser = Depends(require_staff), db: Session = Depends(get_db)):
    """Changes your own password and signs out every other session."""
    if not verify_password(body.current_password, user.password_hash):
        raise ApiError(403, "WRONG_PASSWORD", "Current password is incorrect.")
    keep = token_digest(token)
    user.password_hash = hash_password(body.new_password)
    db.execute(update(AdminSession).where(AdminSession.user_id == user.id, AdminSession.token_digest != keep).values(revoked=True))
    audit(db, user.email, "auth.password_change", user.email, None, None)
    db.commit()
    return {"status": "password_changed"}
