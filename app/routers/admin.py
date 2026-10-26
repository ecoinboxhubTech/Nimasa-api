"""Protected operations API (FR-021..024, NFR-006). Every change is audit-logged."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.errors import ApiError
from app.models import (AdminSession, AdminUser, AlertRule, AuditLog, DataSource, Dataset, Feedback, IngestionRun,
                        Location)
from app.passwords import hash_password
from app.routers.auth import user_out
from app.schemas import AlertRulePatch, DatasetPatch, SourcePatch, StaffCreate, StaffPatch
from app.security import audit, require_admin, require_staff
from app.services import ingestion
from app.services.data import age_minutes, feed_status

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _run_dict(r: IngestionRun) -> dict:
    return {"run_id": r.run_uid, "source_id": r.source_id, "started_at": r.started_at, "completed_at": r.completed_at,
            "status": r.status, "records": r.record_count, "rejected": r.rejected_count, "error_category": r.error_category,
            "error_summary": r.error_summary}


@router.get("/feeds")
def feeds(db: Session = Depends(get_db)):
    """Freshness of every dataset at every location, plus recent ingestion runs."""
    now, rows = utcnow(), []
    for loc in db.scalars(select(Location)).all():
        for ds in db.scalars(select(Dataset)).all():
            status, obs = feed_status(db, ds, loc.id, now)
            rows.append({"location_id": loc.id, "dataset": ds.key, "status": status, "limit_minutes": ds.stale_after_minutes,
                         "source_timestamp": obs.observed_at if obs else None, "age_minutes": age_minutes(obs.observed_at, now) if obs else None})
    runs = db.scalars(select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(20)).all()
    return {"feeds": rows, "recent_runs": [_run_dict(r) for r in runs]}


@router.get("/ingestion-runs")
def runs(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return [_run_dict(r) for r in db.scalars(select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(limit)).all()]


@router.post("/ingestion/run")
def run_now(source: str | None = None, db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
            actor: str = Depends(require_admin)):
    if source and not db.get(DataSource, source):
        raise ApiError(404, "SOURCE_NOT_FOUND", f"Source '{source}' does not exist.")
    try:
        result = ingestion.run_all(db, settings, source)
    except ingestion.IngestionBusy:
        raise ApiError(409, "INGESTION_IN_PROGRESS", "An ingestion run is already in progress. Try again shortly.")
    audit(db, actor, "ingestion.run", source or "all-enabled", None, {"runs": [r.run_uid for r in result]})
    db.commit()
    return [_run_dict(r) for r in result]


@router.patch("/sources/{source_id}")
def patch_source(source_id: str, body: SourcePatch, db: Session = Depends(get_db), actor: str = Depends(require_admin)):
    """Enable/disable a source: the documented way to switch off a faulty feed (SRS 16)."""
    src = db.get(DataSource, source_id)
    if not src:
        raise ApiError(404, "SOURCE_NOT_FOUND", f"Source '{source_id}' does not exist.")
    before = {"enabled": src.enabled}
    src.enabled = body.enabled
    audit(db, actor, "source.update", source_id, before, {"enabled": src.enabled})
    db.commit()
    return {"id": src.id, "enabled": src.enabled}


@router.patch("/datasets/{dataset_key}")
def patch_dataset(dataset_key: str, body: DatasetPatch, db: Session = Depends(get_db), actor: str = Depends(require_admin)):
    """Change a dataset's freshness threshold (FR-024). Applies to API, alerts and AI at once (NFR-004). Audited."""
    ds = db.get(Dataset, dataset_key)
    if not ds:
        raise ApiError(404, "DATASET_NOT_FOUND", f"Dataset '{dataset_key}' does not exist.")
    if body.stale_after_minutes < ds.refresh_minutes:
        raise ApiError(422, "VALIDATION_ERROR",
                       f"stale_after_minutes must be at least the dataset refresh interval ({ds.refresh_minutes} min).")
    before = {"stale_after_minutes": ds.stale_after_minutes}
    ds.stale_after_minutes = body.stale_after_minutes
    audit(db, actor, "dataset.update", dataset_key, before, {"stale_after_minutes": ds.stale_after_minutes})
    db.commit()
    return {"key": ds.key, "stale_after_minutes": ds.stale_after_minutes}


@router.get("/alert-rules")
def list_rules(db: Session = Depends(get_db)):
    """Every rule with its editable fields, for the admin rules table."""
    return [{"id": r.id, "name": r.name, "dataset": r.dataset_key, "operator": r.operator, "threshold": r.threshold,
             "unit": r.unit, "suppression_hours": r.suppression_hours, "validity_hours": r.validity_hours,
             "enabled": r.enabled, "review_status": r.review_status}
            for r in db.scalars(select(AlertRule).order_by(AlertRule.id)).all()]


@router.patch("/alert-rules/{rule_id}")
def patch_rule(rule_id: str, body: AlertRulePatch, db: Session = Depends(get_db), actor: str = Depends(require_admin)):
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise ApiError(404, "RULE_NOT_FOUND", f"Alert rule '{rule_id}' does not exist.")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise ApiError(422, "VALIDATION_ERROR", "No changes supplied.")
    fields = ["threshold", "enabled", "suppression_hours", "validity_hours", "review_status"]
    before = {k: getattr(rule, k) for k in fields}
    for k, v in changes.items():
        setattr(rule, k, v)
    audit(db, actor, "alert_rule.update", rule_id, before, {k: getattr(rule, k) for k in fields})
    db.commit()
    return {"id": rule.id, **{k: getattr(rule, k) for k in fields}}


@router.get("/audit-log")
def audit_log(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return [{"at": a.at, "actor": a.actor, "action": a.action, "target": a.target, "before": a.before, "after": a.after}
            for a in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()]


@router.get("/staff")
def list_staff(db: Session = Depends(get_db), _: AdminUser = Depends(require_staff)):
    return [user_out(u) for u in db.scalars(select(AdminUser).order_by(AdminUser.email)).all()]


@router.post("/staff", status_code=201)
def create_staff(body: StaffCreate, db: Session = Depends(get_db), me: AdminUser = Depends(require_staff)):
    email = body.email.strip().lower()
    if db.scalars(select(AdminUser).where(AdminUser.email == email)).first():
        raise ApiError(409, "STAFF_EXISTS", "An administrator with that email already exists.")
    user = AdminUser(email=email, password_hash=hash_password(body.password))
    db.add(user)
    audit(db, me.email, "staff.create", email, None, {"active": True})
    db.commit()
    return user_out(user)


@router.patch("/staff/{staff_id}")
def patch_staff(staff_id: int, body: StaffPatch, db: Session = Depends(get_db), me: AdminUser = Depends(require_staff)):
    """Deactivate/reactivate staff or reset a password. Either change signs that person out everywhere."""
    user = db.get(AdminUser, staff_id)
    if not user:
        raise ApiError(404, "STAFF_NOT_FOUND", f"Administrator {staff_id} does not exist.")
    if body.active is None and body.password is None:
        raise ApiError(422, "VALIDATION_ERROR", "No changes supplied.")
    if body.active is False and user.id == me.id:
        raise ApiError(409, "CANNOT_DEACTIVATE_SELF", "You cannot deactivate your own account.")
    changes = {}
    if body.active is not None:
        changes["active"] = body.active
        user.active = body.active
    if body.password is not None:
        user.password_hash = hash_password(body.password)
        user.failed_attempts, user.locked_until = 0, None
        changes["password"] = "reset"
    db.execute(update(AdminSession).where(AdminSession.user_id == user.id).values(revoked=True))
    audit(db, me.email, "staff.update", user.email, None, changes)
    db.commit()
    return user_out(user)


@router.get("/feedback")
def list_feedback(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return [{"at": f.created_at, "found_information": f.found_information, "source_clarity": f.source_clarity, "comments": f.comments}
            for f in db.scalars(select(Feedback).order_by(Feedback.id.desc()).limit(limit)).all()]
