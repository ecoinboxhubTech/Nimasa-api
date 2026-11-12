"""Durable job queue with cross-worker claiming (PostgreSQL FOR UPDATE SKIP LOCKED)."""
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utcnow
from app.models import Job

log = logging.getLogger("blueintel.jobs")


def enqueue(db: Session, kind: str, payload: dict | None = None, scheduled_at: datetime | None = None) -> Job:
    job = Job(kind=kind, payload=payload, scheduled_at=scheduled_at or utcnow())
    db.add(job)
    db.commit()
    return job


def _claim(db: Session) -> Job | None:
    job = db.scalars(
        select(Job)
        .where(Job.status == "queued", Job.scheduled_at <= utcnow())
        .order_by(Job.scheduled_at, Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()
    if job is None:
        return None
    job.status, job.started_at, job.attempts = "running", utcnow(), job.attempts + 1
    db.commit()
    return job


def _execute(db: Session, settings: Settings, job: Job) -> None:
    if job.kind == "ingestion":
        from app.services import ingestion
        ingestion.run_all(db, settings, (job.payload or {}).get("source"))
    elif job.kind == "notification_dispatch":
        from app.services import notifications
        notifications.dispatch_pending(db, settings)
    elif job.kind == "retention":
        from app.services import ingestion
        ingestion.purge_old(db, settings, utcnow())
    elif job.kind == "quality_scan":
        from app.services.quality import summary
        job.payload = {**(job.payload or {}), "result": summary(db, settings).model_dump(mode="json")}
    else:
        raise ValueError(f"Unknown job kind '{job.kind}'")


def run_pending(db: Session, settings: Settings, limit: int = 10) -> int:
    count = 0
    for _ in range(limit):
        job = _claim(db)
        if job is None:
            break
        try:
            _execute(db, settings, job)
            job.status, job.completed_at, job.error = "success", utcnow(), None
        except Exception as e:
            job.status, job.completed_at = "failed", utcnow()
            job.error = f"{type(e).__name__}: {str(e)[:400]}"
            log.warning("job id=%s kind=%s failed", job.id, job.kind)
        db.commit()
        count += 1
    return count
