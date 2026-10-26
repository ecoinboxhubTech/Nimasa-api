"""Public read API, versioned under /api/v1 (SRS 12.2, 12.3). No login needed for the MVP (SRS 14.2)."""
import csv
import io
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.errors import ApiError
from app.models import AlertRule, Dataset, DataSource, Feedback, IngestionRun, Location
from app.schemas import (AlertCategory, AlertOut, DatasetOut, FeedbackIn, GapOut, Indicator, LocationOut, PointOut, SeriesOut, SourceOut,
                         SourceRef, SummaryOut)
from app.security import rate_limit
from app.services.alerts import events_with_status
from app.services.data import age_minutes, feed_status, find_gaps, get_series, source_for
from app.services.export import FORMATS, render_series
from app.zones import category_of, zone_of

router = APIRouter(prefix="/api/v1")


def _loc(db: Session, location_id: str) -> Location:
    loc = db.get(Location, location_id)
    if not loc:
        raise ApiError(404, "LOCATION_NOT_FOUND", f"Location '{location_id}' is not supported. See GET /api/v1/locations.")
    return loc


MAX_BULK_LOCATIONS = 20


def _locs(db: Session, ids: str | None) -> list[Location]:
    """Resolve a comma-separated id list (or all locations). Unknown ids are an error, never silently dropped."""
    if not ids:
        return list(db.scalars(select(Location).order_by(Location.name)).all())
    wanted = list(dict.fromkeys(i.strip() for i in ids.split(",") if i.strip()))
    if not wanted or len(wanted) > MAX_BULK_LOCATIONS:
        raise ApiError(422, "VALIDATION_ERROR", f"Provide between 1 and {MAX_BULK_LOCATIONS} location ids.")
    return [_loc(db, i) for i in wanted]


def _ref(src: DataSource | None) -> SourceRef | None:
    return SourceRef(id=src.id, name=src.name, attribution=src.attribution) if src else None


@router.get("/health", tags=["health"])
def health(db: Session = Depends(get_db)):
    """Application health and upstream feed health are reported SEPARATELY (FR-021)."""
    now = utcnow()
    try:
        db.execute(text("SELECT 1"))
        app_ok = True
    except Exception:
        app_ok = False
    feeds = []
    degraded = False
    for src in db.scalars(select(DataSource).where(DataSource.enabled.is_(True))).all():
        last_ok = db.scalars(select(IngestionRun).where(IngestionRun.source_id == src.id, IngestionRun.status.in_(["success", "partial"]))
                             .order_by(IngestionRun.started_at.desc()).limit(1)).first()
        last = db.scalars(select(IngestionRun).where(IngestionRun.source_id == src.id).order_by(IngestionRun.started_at.desc()).limit(1)).first()
        stale = []
        for ds in db.scalars(select(Dataset).where(Dataset.source_id == src.id)).all():
            statuses = [feed_status(db, ds, loc.id, now)[0] for loc in db.scalars(select(Location)).all()]
            if any(s != "fresh" for s in statuses):
                stale.append(ds.key)
        bad = bool(stale) or (last is not None and last.status == "failed")
        degraded |= bad
        feeds.append({"source_id": src.id, "status": "degraded" if bad else "healthy",
                      "last_successful_sync": last_ok.completed_at if last_ok else None,
                      "last_run_status": last.status if last else None, "stale_datasets": stale})
    return {"status": "ok" if app_ok else "error", "app": {"database": "ok" if app_ok else "unreachable"},
            "feeds": {"status": "unknown" if not feeds else ("degraded" if degraded else "healthy"), "sources": feeds}, "time": now}


@router.get("/sources", response_model=list[SourceOut], tags=["metadata"])
def sources(db: Session = Depends(get_db)):
    return db.scalars(select(DataSource).order_by(DataSource.id)).all()


@router.get("/locations", response_model=list[LocationOut], tags=["metadata"])
def locations(db: Session = Depends(get_db)):
    return db.scalars(select(Location).order_by(Location.name)).all()


@router.get("/datasets", response_model=list[DatasetOut], tags=["metadata"])
def datasets(db: Session = Depends(get_db)):
    return db.scalars(select(Dataset).order_by(Dataset.key)).all()


def _summary(db: Session, loc: Location, now: datetime) -> SummaryOut:
    indicators, warnings = [], []
    for ds in db.scalars(select(Dataset).order_by(Dataset.key)).all():
        status, obs = feed_status(db, ds, loc.id, now)
        if status == "stale":
            warnings.append(f"{ds.label} data is stale (last source update {age_minutes(obs.observed_at, now)} min ago). It is not shown as current.")
        elif status == "empty":
            warnings.append(f"No {ds.label.lower()} data is available for {loc.name}.")
        indicators.append(Indicator(
            dataset=ds.key, label=ds.label, unit=ds.unit, value=obs.value if obs else None,
            data_type=obs.kind if obs else None, observed_at=obs.observed_at if obs else None,
            age_minutes=age_minutes(obs.observed_at, now) if obs else None, status=status,
            source=_ref(source_for(db, obs, ds)) if obs else None))
    active = [1 for _, s in events_with_status(db, now, loc.id) if s == "active"]
    return SummaryOut(location=loc, generated_at=now, indicators=indicators, active_alerts=len(active), warnings=warnings)


@router.get("/locations/{location_id}/summary", response_model=SummaryOut, tags=["data"], dependencies=[Depends(rate_limit("public"))])
def summary(location_id: str, db: Session = Depends(get_db)):
    return _summary(db, _loc(db, location_id), utcnow())


@router.get("/summaries", response_model=list[SummaryOut], tags=["data"], dependencies=[Depends(rate_limit("public"))])
def summaries(locations: str | None = Query(None, description="Comma-separated location ids; default = every location"),
              db: Session = Depends(get_db)):
    """Bulk summary for the map and landing page: one request instead of one per location."""
    now = utcnow()
    return [_summary(db, loc, now) for loc in _locs(db, locations)]


def _series_query(db: Session, settings: Settings, location_id: str, dataset: str, start: datetime | None, end: datetime | None):
    """Shared by the JSON and CSV endpoints so both apply identical validation and bounds (SRS 12.2)."""
    loc, now = _loc(db, location_id), utcnow()
    ds = db.get(Dataset, dataset)
    if not ds:
        raise ApiError(404, "DATASET_NOT_FOUND", f"Dataset '{dataset}' does not exist. See GET /api/v1/datasets.")
    start = start or now - timedelta(hours=48)
    end = end or now + timedelta(hours=48)
    if start.tzinfo is None or end.tzinfo is None:
        raise ApiError(422, "VALIDATION_ERROR", "start and end must include a timezone, e.g. 2026-10-20T00:00:00Z.")
    if end <= start:
        raise ApiError(422, "VALIDATION_ERROR", "end must be after start.")
    if end - start > timedelta(days=settings.max_series_days):
        raise ApiError(422, "RANGE_TOO_LARGE", f"Time range is limited to {settings.max_series_days} days.")
    return loc, ds, now, get_series(db, ds.key, loc.id, start, end)


@router.get("/locations/{location_id}/series.csv", tags=["data"], dependencies=[Depends(rate_limit("public"))])
def series_csv(
    location_id: str, dataset: str = Query(..., description="Dataset key, e.g. wave_height"),
    start: datetime | None = Query(None), end: datetime | None = Query(None),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    """CSV export of chart data (FR-011): every row carries timestamp, unit, data type and source."""
    loc, ds, now, rows = _series_query(db, settings, location_id, dataset, start, end)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "location_id", "dataset", "value", "unit", "data_type", "source_id", "source_name", "attribution"])
    for r in rows:
        src = source_for(db, r, ds)
        w.writerow([r.observed_at.isoformat().replace("+00:00", "Z"), loc.id, ds.key, f"{r.value:g}", r.unit, r.kind,
                    src.id, src.name, src.attribution])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{loc.id}_{ds.key}.csv"'})


@router.get("/locations/{location_id}/series.{fmt}", tags=["data"], dependencies=[Depends(rate_limit("public"))],
            responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "application/pdf": {}}}})
def series_chart(
    location_id: str, fmt: Literal["png", "jpg", "jpeg", "pdf"], dataset: str = Query(..., description="Dataset key, e.g. wave_height"),
    start: datetime | None = Query(None), end: datetime | None = Query(None),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    """Chart export as PNG, JPG or PDF. Same data, bounds and validation as the JSON and CSV endpoints."""
    loc, ds, now, rows = _series_query(db, settings, location_id, dataset, start, end)
    status, latest = feed_status(db, ds, loc.id, now)
    src = source_for(db, latest, ds)
    media_type, mpl_format = FORMATS[fmt]
    body = render_series(
        mpl_format, title=f"{ds.label} - {loc.name}", unit=ds.unit,
        points=[(r.observed_at, r.value, r.kind) for r in rows], expected_minutes=ds.refresh_minutes,
        source_line=f"Source: {src.name}. {src.attribution}", status=status, generated_at=now)
    return Response(body, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{loc.id}_{ds.key}.{"jpg" if fmt == "jpeg" else fmt}"'})


def _series_out(db: Session, settings: Settings, location_id: str, dataset: str,
                start: datetime | None, end: datetime | None) -> SeriesOut:
    loc, ds, now, rows = _series_query(db, settings, location_id, dataset, start, end)
    status, latest = feed_status(db, ds, loc.id, now)
    past = [r.observed_at for r in rows if r.kind != "forecast"]
    fut = [r.observed_at for r in rows if r.kind == "forecast"]
    gaps = find_gaps(past, ds.refresh_minutes) + find_gaps(fut, ds.refresh_minutes)
    return SeriesOut(
        location_id=loc.id, dataset=ds.key, label=ds.label, unit=ds.unit, status=status,
        source_timestamp=latest.observed_at if latest else None, expected_interval_minutes=ds.refresh_minutes,
        source=_ref(source_for(db, latest, ds)),
        points=[PointOut(time=r.observed_at, value=r.value, data_type=r.kind) for r in rows],
        gaps=[GapOut(start=g.start, end=g.end) for g in gaps])


@router.get("/locations/{location_id}/series", response_model=SeriesOut, tags=["data"], dependencies=[Depends(rate_limit("public"))])
def series(
    location_id: str, dataset: str = Query(..., description="Dataset key, e.g. wave_height"),
    start: datetime | None = Query(None, description="ISO 8601; default = 48 h ago"),
    end: datetime | None = Query(None, description="ISO 8601; default = now + 48 h (forecast)"),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    return _series_out(db, settings, location_id, dataset, start, end)


@router.get("/series", response_model=list[SeriesOut], tags=["data"], dependencies=[Depends(rate_limit("public"))])
def series_bulk(
    locations: str = Query(..., description="Comma-separated location ids (max 20)"),
    dataset: str = Query(..., description="Dataset key, e.g. wave_height"),
    start: datetime | None = Query(None), end: datetime | None = Query(None),
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
):
    """Same bounds and validation as the single-location endpoint, for several locations in one request."""
    return [_series_out(db, settings, loc.id, dataset, start, end) for loc in _locs(db, locations)]


@router.get("/alerts", response_model=list[AlertOut], tags=["alerts"], dependencies=[Depends(rate_limit("public"))])
def alerts(location_id: str | None = None, include_expired: bool = False,
           status: Literal["active", "expired", "suppressed_stale"] | None = Query(None, description="Filter by derived status"),
           category: AlertCategory | None = Query(None, description="coastal, ocean_navigation or waterway_navigation"),
           db: Session = Depends(get_db)):
    if location_id:
        _loc(db, location_id)
    out = []
    for e, derived in events_with_status(db, utcnow(), location_id, include_expired or status == "expired"):
        if status and derived != status:
            continue
        if category and category_of(e.rule_id) != category:
            continue
        out.append(AlertOut(
            id=e.id, rule_id=e.rule_id, rule_name=e.rule.name, category=category_of(e.rule_id), location_id=e.location_id,
            zone=zone_of(e.location_id), dataset=e.dataset_key,
            condition=f"{e.dataset_key} {e.operator} {e.threshold:g} {e.unit}", value=e.value, threshold=e.threshold, unit=e.unit,
            data_type=e.data_kind, source_id=e.source_id, source_timestamp=e.triggered_at, issued_at=e.issued_at,
            valid_until=e.valid_until, status=derived, message=e.message))
    return out


@router.get("/alert-rules", tags=["alerts"])
def alert_rules(db: Session = Depends(get_db)):
    return [{"id": r.id, "name": r.name, "category": category_of(r.id), "dataset": r.dataset_key, "condition": f"{r.operator} {r.threshold:g} {r.unit}",
             "suppression_hours": r.suppression_hours, "validity_hours": r.validity_hours, "enabled": r.enabled,
             "review_status": r.review_status} for r in db.scalars(select(AlertRule)).all()]


@router.post("/feedback", status_code=201, tags=["feedback"], dependencies=[Depends(rate_limit("feedback"))])
def feedback(body: FeedbackIn, db: Session = Depends(get_db)):
    """Optional pilot feedback. No name, phone or email is collected (data minimisation)."""
    if not any([body.found_information, body.source_clarity, (body.comments or "").strip()]):
        raise ApiError(422, "VALIDATION_ERROR", "Please answer at least one question.")
    db.add(Feedback(found_information=body.found_information, source_clarity=body.source_clarity,
                    comments=(body.comments or "").strip() or None))
    db.commit()
    return {"status": "received"}



