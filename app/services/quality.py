"""Data-quality summary used by the admin console and the quality job."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utcnow
from app.models import AlertEvent, Dataset, IngestionRun, Location, Observation
from app.schemas import QualityOut
from app.services.data import feed_status, find_gaps, get_series


def summary(db: Session, settings: Settings) -> QualityOut:
    now = utcnow()
    locations = db.scalars(select(Location)).all()
    datasets = db.scalars(select(Dataset)).all()
    observations = db.scalar(select(func.count()).select_from(Observation)) or 0
    rejected = db.scalar(select(func.sum(IngestionRun.rejected_count)).where(IngestionRun.started_at >= now - timedelta(days=7))) or 0
    active_alerts = db.scalar(select(func.count()).select_from(AlertEvent).where(AlertEvent.valid_until >= now)) or 0
    stale = empty = gaps = expected = actual = 0
    start, end = now - timedelta(hours=48), now
    for loc in locations:
        for ds in datasets:
            status, _ = feed_status(db, ds, loc.id, now)
            stale += status == "stale"
            empty += status == "empty"
            rows = get_series(db, ds.key, loc.id, start, end)
            actual += len(rows)
            expected += max(1, int((end - start).total_seconds() / 60 / ds.refresh_minutes) + 1)
            gaps += len(find_gaps([r.observed_at for r in rows if r.kind != "forecast"], ds.refresh_minutes))
    completeness = round(min(100.0, actual / expected * 100), 1) if expected else 0.0
    notes = []
    if stale:
        notes.append(f"{stale} feed(s) are stale and are never treated as current.")
    if empty:
        notes.append(f"{empty} feed(s) have no data yet.")
    notes.append("Gaps are counted from stored observations; values are never interpolated.")
    return QualityOut(
        generated_at=now, locations=len(locations), datasets=len(datasets), observations=observations,
        rejected_last_7d=int(rejected), active_alerts=int(active_alerts), stale_feeds=stale, empty_feeds=empty,
        gap_count_48h=gaps, completeness_pct=completeness, notes=notes,
    )
