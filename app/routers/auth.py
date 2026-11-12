"""Administrator sign-in, MFA, password reset. The public never logs in; only authorised staff do (PRD 14.2)."""
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import totp
from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.errors import ApiError
from app.models import AdminSession, AdminUser, PasswordResetToken
from app.passwords import DUMMY_HASH, hash_password, new_token, token_digest, verify_password
from app.schemas import (LoginIn, MfaDisableIn, MfaEnableIn, PasswordChangeIn, PasswordResetConfirmIn,
                         PasswordResetRequestIn)
from app.security import audit, bearer_token, rate_limit, require_staff
from app.services import notifications

router = APIRouter(prefix="/api/v1/admin/auth", tags=["admin-auth"])

BAD_LOGIN = ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password, or the account is temporarily locked.")


def user_out(u: AdminUser) -> dict:
    return {"id": u.id, "email": u.email, "role": u.role, "active": u.active, "mfa_enabled": u.mfa_enabled,
            "created_at": u.created_at, "last_login_at": u.last_login_at}


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
    if user.mfa_enabled:
        if not body.totp_code:
            raise ApiError(401, "MFA_REQUIRED", "A six-digit authenticator code is required.")
        if not user.mfa_secret or not totp.verify(user.mfa_secret, body.totp_code):
            audit(db, user.email, "auth.mfa_failed", user.email, None, None)
            db.commit()
            raise ApiError(401, "MFA_INVALID", "The authenticator code is invalid or has expired.")
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


@router.post("/password-reset/request", status_code=202,
             dependencies=[Depends(rate_limit("password-reset", "login_rate_limit_per_minute"))])
def request_password_reset(body: PasswordResetRequestIn, db: Session = Depends(get_db),
                           settings: Settings = Depends(get_settings)):
    """Always returns 202 so the endpoint cannot be used to enumerate accounts."""
    email = body.email.strip().lower()
    user = db.scalars(select(AdminUser).where(AdminUser.email == email)).first()
    raw = None
    if user and user.active:
        raw = new_token()
        db.add(PasswordResetToken(user_id=user.id, token_digest=token_digest(raw),
                                  expires_at=utcnow() + timedelta(hours=settings.password_reset_hours)))
        link = f"{settings.public_base_url.rstrip('/')}/admin/login?reset={raw}"
        notifications.enqueue(db, "auth.password_reset", "Reset your Orizon Nimasa password",
                              f"Use this single-use link within {settings.password_reset_hours} hour(s): {link}",
                              channel="email" if settings.smtp_host else "in_app", destination=email)
        db.commit()
        notifications.dispatch_pending(db, settings, limit=5)
    out = {"status": "requested", "message": "If the account exists, a reset link has been sent."}
    if raw and not settings.is_production and not settings.smtp_host:
        out["reset_token"] = raw  # local/demo fallback only; never exposed in production
    return out


@router.post("/password-reset/confirm")
def confirm_password_reset(body: PasswordResetConfirmIn, db: Session = Depends(get_db)):
    token = db.scalars(select(PasswordResetToken).where(PasswordResetToken.token_digest == token_digest(body.token))).first()
    if token is None or token.used or token.expires_at <= utcnow():
        raise ApiError(400, "RESET_TOKEN_INVALID", "This reset link is invalid or has expired.")
    user = db.get(AdminUser, token.user_id)
    if user is None or not user.active:
        raise ApiError(400, "RESET_TOKEN_INVALID", "This reset link is invalid or has expired.")
    user.password_hash = hash_password(body.new_password)
    user.failed_attempts, user.locked_until = 0, None
    token.used = True
    db.execute(update(AdminSession).where(AdminSession.user_id == user.id).values(revoked=True))
    audit(db, user.email, "auth.password_reset", user.email, None, None)
    db.commit()
    return {"status": "password_reset"}


@router.post("/mfa/setup")
def mfa_setup(user: AdminUser = Depends(require_staff), db: Session = Depends(get_db)):
    if user.mfa_enabled:
        raise ApiError(409, "MFA_ALREADY_ENABLED", "MFA is already enabled for this account.")
    secret = totp.new_secret()
    user.mfa_secret = secret
    db.commit()
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, user.email)}


@router.post("/mfa/enable")
def mfa_enable(body: MfaEnableIn, user: AdminUser = Depends(require_staff), db: Session = Depends(get_db)):
    if not user.mfa_secret or not totp.verify(user.mfa_secret, body.code):
        raise ApiError(400, "MFA_INVALID", "The authenticator code is invalid or has expired.")
    user.mfa_enabled = True
    audit(db, user.email, "auth.mfa_enabled", user.email, None, None)
    db.commit()
    return {"status": "mfa_enabled"}


@router.post("/mfa/disable")
def mfa_disable(body: MfaDisableIn, user: AdminUser = Depends(require_staff), db: Session = Depends(get_db)):
    if not verify_password(body.password, user.password_hash):
        raise ApiError(403, "WRONG_PASSWORD", "Password is incorrect.")
    user.mfa_enabled, user.mfa_secret = False, None
    audit(db, user.email, "auth.mfa_disabled", user.email, None, None)
    db.commit()
    return {"status": "mfa_disabled"}
