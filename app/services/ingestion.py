"""Scheduled ingestion: fetch -> validate -> store -> evaluate alerts (FR-006, FR-007, FR-022).
Every run is logged with run id, source, time, outcome and an error category. Secrets are never logged."""
import logging
import math
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utcnow
from app.models import AlertEvent, Dataset, DataSource, IngestionRun, Location, Observation
from app.services import alerts
from app.services.adapters import AdapterError, RawRecord, get_adapter

log = logging.getLogger("blueintel.ingestion")
VALID_KINDS = {"observation", "forecast", "model_estimate", "derived"}
MAX_FUTURE = timedelta(days=7)
INGEST_LOCK_KEY = 0x426C5565  # arbitrary app-wide PostgreSQL advisory lock id for "an ingestion cycle is running"


class IngestionBusy(Exception):
    """Another process (scheduler on another instance, or a manual run) is already ingesting."""


@contextmanager
def ingestion_lock(db: Session):
    """Cross-process mutex so several web workers/instances never ingest at once.

    Session-level advisory locks belong to one connection, and run_source commits (returning its connection to the
    pool), so the lock is held on its own dedicated connection and released explicitly. If the process dies, the
    server drops the lock when the connection closes."""
    with db.get_bind().connect() as conn:
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": INGEST_LOCK_KEY}).scalar():
            raise IngestionBusy()
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": INGEST_LOCK_KEY})
            conn.commit()


def validate_record(rec: RawRecord, datasets: dict[str, Dataset], location_ids: set[str], now: datetime) -> str | None:
    """Return a rejection reason, or None if the record is publishable."""
    ds = datasets.get(rec.dataset_key)
    if ds is None:
        return "unknown_dataset"
    if rec.location_id not in location_ids:
        return "unknown_location"
    if rec.kind not in VALID_KINDS:
        return "invalid_kind"
    if not isinstance(rec.observed_at, datetime) or rec.observed_at.tzinfo is None:
        return "invalid_timestamp"
    if rec.observed_at - now > MAX_FUTURE:
        return "timestamp_too_far_in_future"
    if rec.value is None or isinstance(rec.value, bool) or not isinstance(rec.value, (int, float)) or not math.isfinite(rec.value):
        return "missing_or_invalid_value"
    if rec.unit != ds.unit:
        return "unit_mismatch"  # units are preserved; only documented, tested conversions are allowed
    if not (ds.plausible_min <= rec.value <= ds.plausible_max):
        return "impossible_value"
    return None


def _store(db: Session, run: IngestionRun, records: list[RawRecord]) -> int:
    """Insert new points; refresh changed forecasts; never overwrite past observations. Duplicates are detected."""
    if not records:
        return 0
    lo, hi = min(r.observed_at for r in records), max(r.observed_at for r in records)
    existing = {
        (o.dataset_key, o.location_id, o.observed_at, o.kind): o
        for o in db.scalars(select(Observation).where(Observation.observed_at >= lo, Observation.observed_at <= hi))
    }
    written = 0
    seen: set = set()
    for r in records:
        key = (r.dataset_key, r.location_id, r.observed_at, r.kind)
        if key in seen:
            continue  # duplicate inside the same batch
        seen.add(key)
        cur = existing.get(key)
        if cur is None:
            db.add(Observation(dataset_key=r.dataset_key, location_id=r.location_id, observed_at=r.observed_at,
                               value=float(r.value), unit=r.unit, kind=r.kind, run_id=run.id, raw_reference=r.raw_reference))
            written += 1
        elif r.kind == "forecast" and cur.value != r.value:
            cur.value, cur.run_id = float(r.value), run.id
            written += 1
    return written


def run_source(db: Session, settings: Settings, source: DataSource, now: datetime | None = None) -> IngestionRun:
    now = now or utcnow()
    run = IngestionRun(run_uid=str(uuid.uuid4()), source_id=source.id, started_at=now, status="running")
    db.add(run)
    db.commit()
    rejected: Counter[str] = Counter()
    try:
        if source.id != "demo" and not settings.source_licence_reviewed:
            raise AdapterError("LICENCE_NOT_REVIEWED", "Source licence has not been marked as reviewed (SOURCE_LICENCE_REVIEWED)")
        locations = db.scalars(select(Location)).all()
        datasets = db.scalars(select(Dataset)).all()
        adapter = get_adapter(source.id, settings)

        raw: list[RawRecord] | None = None
        for attempt in range(1, settings.ingest_max_retries + 1):
            try:
                raw = adapter.fetch(locations, datasets, now)
                break
            except AdapterError as e:
                if e.category == "UNKNOWN_SOURCE" or not e.retryable or attempt == settings.ingest_max_retries:
                    raise
                time.sleep(settings.ingest_backoff_seconds * 2 ** (attempt - 1))  # bounded exponential backoff
        assert raw is not None

        ds_map = {d.key: d for d in datasets}
        loc_ids = {loc.id for loc in locations}
        valid: list[RawRecord] = []
        for rec in raw:
            reason = validate_record(rec, ds_map, loc_ids, now)
            if reason:
                rejected[reason] += 1
            else:
                valid.append(rec)

        run.record_count = _store(db, run, valid)
        run.rejected_count = sum(rejected.values())
        if not valid:
            run.status, run.error_category = "failed", "NO_VALID_RECORDS"
            run.error_summary = f"All {len(raw)} records were rejected: {dict(rejected)}"
        elif rejected:
            run.status = "partial"
            run.error_summary = f"Rejected records by reason: {dict(rejected)}"
        else:
            run.status = "success"
        run.completed_at = utcnow()
        db.commit()
        new_alerts = alerts.evaluate_all(db, now, settings.alert_lookback_hours)
        purge_old(db, settings, now)
        log.info("ingestion run=%s source=%s status=%s stored=%d rejected=%d new_alerts=%d",
                 run.run_uid, source.id, run.status, run.record_count, run.rejected_count, new_alerts)
    except AdapterError as e:
        db.rollback()
        run = db.get(IngestionRun, run.id)
        run.status, run.error_category, run.error_summary, run.completed_at = "failed", e.category, str(e), utcnow()
        db.commit()
        log.warning("ingestion run=%s source=%s status=failed category=%s", run.run_uid, source.id, e.category)
    except Exception:  # never let a feed bug take the app down; details go to logs, a generic category is stored
        db.rollback()
        run = db.get(IngestionRun, run.id)
        run.status, run.error_category, run.error_summary, run.completed_at = "failed", "INTERNAL_ERROR", "Unexpected error. See server logs.", utcnow()
        db.commit()
        log.exception("ingestion run=%s source=%s unexpected error", run.run_uid, source.id)
    return run


def run_all(db: Session, settings: Settings, only_source: str | None = None) -> list[IngestionRun]:
    q = select(DataSource).where(DataSource.enabled.is_(True))
    if only_source:
        q = q.where(DataSource.id == only_source)
    with ingestion_lock(db):
        return [run_source(db, settings, s) for s in db.scalars(q).all()]


def purge_old(db: Session, settings: Settings, now: datetime) -> None:
    """Retention: do not keep source data longer than necessary (SRS 11.3)."""
    cutoff = now - timedelta(days=settings.retention_days)
    old = select(Observation.id).where(Observation.observed_at < cutoff)
    # Alert events keep their own copy of value, source and time, so only the link to the purged row is dropped.
    db.execute(update(AlertEvent).where(AlertEvent.observation_id.in_(old)).values(observation_id=None))
    db.execute(delete(Observation).where(Observation.observed_at < cutoff))
    db.commit()
