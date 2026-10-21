"""Read-side helpers shared by routers, alerts and AI: freshness policy, latest values, series and gaps.
There is ONE freshness policy, used everywhere (NFR-004)."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Dataset, DataSource, IngestionRun, Observation

NON_FORECAST = ("observation", "model_estimate", "derived")


def get_freshness(source_ts: datetime | None, stale_after_minutes: int, now: datetime) -> str:
    """'fresh' | 'stale' | 'empty'. Exactly at the limit is still fresh."""
    if source_ts is None:
        return "empty"
    return "stale" if (now - source_ts).total_seconds() / 60 > stale_after_minutes else "fresh"


def age_minutes(ts: datetime | None, now: datetime) -> int | None:
    return None if ts is None else max(0, round((now - ts).total_seconds() / 60))


def latest_valid(db: Session, dataset_key: str, location_id: str, now: datetime) -> Observation | None:
    return db.scalars(
        select(Observation)
        .where(Observation.dataset_key == dataset_key, Observation.location_id == location_id,
               Observation.kind.in_(NON_FORECAST), Observation.observed_at <= now, Observation.quality_status == "valid")
        .order_by(Observation.observed_at.desc()).limit(1)
    ).first()


def feed_status(db: Session, dataset: Dataset, location_id: str, now: datetime) -> tuple[str, Observation | None]:
    obs = latest_valid(db, dataset.key, location_id, now)
    return get_freshness(obs.observed_at if obs else None, dataset.stale_after_minutes, now), obs


def get_series(db: Session, dataset_key: str, location_id: str, start: datetime, end: datetime) -> list[Observation]:
    """Points in [start, end]. Where an observed value and a forecast share a timestamp, the non-forecast wins."""
    rows = db.scalars(
        select(Observation)
        .where(Observation.dataset_key == dataset_key, Observation.location_id == location_id,
               Observation.observed_at >= start, Observation.observed_at <= end, Observation.quality_status == "valid")
        .order_by(Observation.observed_at, Observation.kind)
    ).all()
    by_time: dict[datetime, Observation] = {}
    for r in rows:
        cur = by_time.get(r.observed_at)
        if cur is None or (cur.kind == "forecast" and r.kind != "forecast"):
            by_time[r.observed_at] = r
    return [by_time[t] for t in sorted(by_time)]


@dataclass
class Gap:
    start: datetime
    end: datetime


def find_gaps(times: list[datetime], expected_minutes: int) -> list[Gap]:
    """A gap is a hole between consecutive points larger than 1.5x the expected interval. Nothing is interpolated."""
    limit = timedelta(minutes=expected_minutes * 1.5)
    return [Gap(a, b) for a, b in zip(times, times[1:]) if b - a > limit]


def source_for(db: Session, obs: Observation | None, dataset: Dataset) -> DataSource:
    """Provenance comes from the ingestion run that produced the record, not from current configuration."""
    if obs is not None:
        run = obs.run or db.get(IngestionRun, obs.run_id)
        src = db.get(DataSource, run.source_id)
        if src:
            return src
    return dataset.source
