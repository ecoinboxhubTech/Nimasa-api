"""Durable notification outbox for alert events.

Delivery is optional: without SMTP or a webhook endpoint, notifications remain visible in the admin console with a
clear `skipped` status. Nothing is silently reported as sent.
"""
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utcnow
from app.models import AlertEvent, Notification, NotificationTarget

log = logging.getLogger("blueintel.notifications")


def enqueue(db: Session, event_type: str, subject: str, body: str, target: NotificationTarget | None = None,
            channel: str | None = None, destination: str | None = None) -> Notification:
    n = Notification(
        target_id=target.id if target else None,
        event_type=event_type,
        channel=channel or (target.channel if target else "in_app"),
        destination=destination or (target.address if target else "admin-console"),
        subject=subject[:300],
        body=body,
        status="queued",
    )
    db.add(n)
    return n


def _send_email(settings: Settings, destination: str, subject: str, body: str) -> None:
    if not settings.smtp_host:
        raise RuntimeError("SMTP is not configured")
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = destination
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.webhook_timeout_seconds) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)


def dispatch_one(db: Session, settings: Settings, n: Notification) -> Notification:
    n.attempts += 1
    try:
        if n.channel == "email":
            _send_email(settings, n.destination, n.subject, n.body)
        elif n.channel == "webhook":
            r = httpx.post(n.destination, json={"event": n.event_type, "subject": n.subject, "body": n.body},
                           timeout=settings.webhook_timeout_seconds)
            r.raise_for_status()
        elif n.channel == "sms":
            raise RuntimeError("SMS gateway is not configured")
        else:
            n.status = "skipped"
            n.error = "No delivery channel configured; visible in the admin console only."
            db.commit()
            return n
        n.status, n.error, n.sent_at = "sent", None, utcnow()
    except Exception as e:  # message only; it never contains provider credentials
        n.status, n.error, n.sent_at = "failed", str(e)[:500], None
        log.warning("notification id=%s channel=%s failed: %s", n.id, n.channel, type(e).__name__)
    db.commit()
    return n


def dispatch_pending(db: Session, settings: Settings, limit: int = 50) -> int:
    rows = db.scalars(select(Notification).where(Notification.status == "queued").order_by(Notification.id).limit(limit)).all()
    for n in rows:
        dispatch_one(db, settings, n)
    return len(rows)


def notify_new_alerts(db: Session, settings: Settings, since: datetime) -> int:
    """Queue one digest notification per active target for alerts created since the last ingestion step."""
    targets = db.scalars(select(NotificationTarget).where(NotificationTarget.active.is_(True))).all()
    events = db.scalars(select(AlertEvent).where(AlertEvent.issued_at >= since).order_by(AlertEvent.issued_at.desc())).all()
    if not targets or not events:
        return 0
    lines = [f"{e.triggered_at.isoformat()} {e.location_id}: {e.message}" for e in events[:25]]
    subject = f"Orizon Nimasa: {len(events)} new alert{'s' if len(events) != 1 else ''}"
    body = "\n".join(lines) + ("\n\nOpen the operations console for full evidence." if len(lines) == 25 else "")
    queued = 0
    for target in targets:
        if "alert.created" not in target.event_types:
            continue
        enqueue(db, "alert.created", subject, body, target)
        queued += 1
    db.commit()
    if queued and settings.notifications_enabled:
        dispatch_pending(db, settings)
    return queued
