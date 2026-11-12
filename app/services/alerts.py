"""Alert rules (FR-012..015). Pure trigger logic + a DB-backed evaluator.
Status is DERIVED at read time so a stale feed can never present an old alert as current."""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AlertEvent, AlertRule, Dataset, Location, Observation
from app.services.data import NON_FORECAST, feed_status, source_for
from app.zones import rule_applies_to_location

OPS = {">=": lambda v, t: v >= t, "<=": lambda v, t: v <= t}


@dataclass
class Point:
    t: datetime
    value: float | None
    kind: str
    obs_id: int | None = None


def find_triggers(
    operator: str, threshold: float, suppression_hours: float, points: list[Point], now: datetime,
    last_trigger_at: datetime | None = None, known_times: set[datetime] | None = None,
) -> list[Point]:
    """Return points that should create a NEW alert.
    - Forecast, missing, non-finite and future points never trigger.
    - Comparison is inclusive.
    - A repeat inside the suppression window is dropped.
    - Points that already produced a stored event (known_times) are skipped but still start a suppression window."""
    compare = OPS[operator]
    known = known_times or set()
    window = timedelta(hours=suppression_hours)
    out: list[Point] = []
    last = last_trigger_at
    for p in sorted(points, key=lambda x: x.t):
        if p.kind == "forecast" or p.t > now:
            continue
        if p.value is None or not math.isfinite(p.value) or not compare(p.value, threshold):
            continue
        if p.t in known:
            last = p.t
            continue
        if last is not None and p.t - last < window:
            continue
        out.append(p)
        last = p.t
    return out


def derive_status(valid_until: datetime, freshness: str, now: datetime) -> str:
    if now > valid_until:
        return "expired"
    if freshness != "fresh":
        return "suppressed_stale"  # FR-015: not an actionable current alert
    return "active"


def evaluate_all(db: Session, now: datetime, lookback_hours: int = 72) -> int:
    """Evaluate every enabled rule for every location against validated stored data. Returns new event count."""
    created = 0
    locations = db.scalars(select(Location)).all()
    for rule in db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))).all():
        dataset = db.get(Dataset, rule.dataset_key)
        if dataset is None:
            continue
        since = now - timedelta(hours=lookback_hours)
        for loc in locations:
            if not rule_applies_to_location(rule, loc):
                continue
            rows = db.scalars(
                select(Observation).where(
                    Observation.dataset_key == rule.dataset_key, Observation.location_id == loc.id,
                    Observation.kind.in_(NON_FORECAST), Observation.observed_at >= since, Observation.observed_at <= now,
                    Observation.quality_status == "valid",
                ).order_by(Observation.observed_at)
            ).all()
            if not rows:
                continue
            prior = db.scalars(
                select(AlertEvent.triggered_at).where(
                    AlertEvent.rule_id == rule.id, AlertEvent.location_id == loc.id, AlertEvent.triggered_at < since
                ).order_by(AlertEvent.triggered_at.desc()).limit(1)
            ).first()
            known = set(db.scalars(
                select(AlertEvent.triggered_at).where(
                    AlertEvent.rule_id == rule.id, AlertEvent.location_id == loc.id, AlertEvent.triggered_at >= since)
            ).all())
            by_id = {r.id: r for r in rows}
            pts = [Point(r.observed_at, r.value, r.kind, r.id) for r in rows]
            for p in find_triggers(rule.operator, rule.threshold, rule.suppression_hours, pts, now, prior, known):
                obs = by_id[p.obs_id]
                db.add(AlertEvent(
                    rule_id=rule.id, location_id=loc.id, dataset_key=rule.dataset_key, observation_id=obs.id,
                    triggered_at=p.t, issued_at=now, valid_until=p.t + timedelta(hours=rule.validity_hours),
                    value=obs.value, threshold=rule.threshold, operator=rule.operator, unit=rule.unit,
                    data_kind=obs.kind, source_id=source_for(db, obs, dataset).id,
                    message=f"{rule.name}: {obs.value:g} {rule.unit} {'reached' if rule.operator == '>=' else 'fell to'} "
                            f"the {rule.threshold:g} {rule.unit} threshold ({obs.kind.replace('_', ' ')}).",
                ))
                created += 1
    db.commit()
    return created


def events_with_status(db: Session, now: datetime, location_id: str | None = None, include_expired: bool = False):
    """Yield (event, status) with status derived from the CURRENT freshness of the underlying feed."""
    q = select(AlertEvent).order_by(AlertEvent.triggered_at.desc())
    if location_id:
        q = q.where(AlertEvent.location_id == location_id)
    cache: dict[tuple[str, str], str] = {}
    out = []
    for ev in db.scalars(q).all():
        key = (ev.dataset_key, ev.location_id)
        if key not in cache:
            cache[key] = feed_status(db, db.get(Dataset, ev.dataset_key), ev.location_id, now)[0]
        status = derive_status(ev.valid_until, cache[key], now)
        if status == "expired" and not include_expired:
            continue
        out.append((ev, status))
    return out

