"""Protected operations API (FR-021..024, NFR-006). Every change is audit-logged."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.errors import ApiError
from app.models import (AdminSession, AdminUser, AlertEvent, AlertRule, ApiKey, AuditLog, DataSource, Dataset,
                        Feedback, IngestionRun, Job, Location, Notification, NotificationTarget, Observation)
from app.passwords import hash_password
from app.routers.auth import user_out
from app.schemas import (AlertRuleCreate, AlertRulePatch, ApiKeyCreateIn, ApiKeyPatch, DatasetPatch, JobCreateIn,
                         LocationCreate, LocationPatch, NotificationTargetIn, NotificationTargetPatch, SourcePatch,
                         StaffCreate, StaffPatch)
from app.security import audit, require_admin, require_editor, require_staff, require_staff_admin
from app.services import api_keys, ingestion, jobs, notifications, quality
from app.services.data import age_minutes, feed_status

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _run_dict(r: IngestionRun) -> dict:
    return {"run_id": r.run_uid, "source_id": r.source_id, "started_at": r.started_at, "completed_at": r.completed_at,
            "status": r.status, "records": r.record_count, "rejected": r.rejected_count, "error_category": r.error_category,
            "error_summary": r.error_summary}


def _loc_dict(loc: Location) -> dict:
    return {"id": loc.id, "name": loc.name, "area": loc.area, "lat": loc.lat, "lng": loc.lng, "zone": loc.zone}


def _rule_dict(r: AlertRule) -> dict:
    return {"id": r.id, "name": r.name, "category": r.category, "dataset": r.dataset_key, "operator": r.operator,
            "threshold": r.threshold, "unit": r.unit, "zones": r.zones, "suppression_hours": r.suppression_hours,
            "validity_hours": r.validity_hours, "enabled": r.enabled, "review_status": r.review_status}


def _key_dict(k: ApiKey) -> dict:
    return {"id": k.id, "name": k.name, "prefix": k.prefix, "owner_email": k.owner_email, "active": k.active,
            "environment": k.environment, "scopes": k.scopes, "quota_per_day": k.quota_per_day, "rate_limit_per_minute": k.rate_limit_per_minute,
            "created_at": k.created_at, "last_used_at": k.last_used_at, "revoked_at": k.revoked_at}


def _target_dict(t: NotificationTarget) -> dict:
    return {"id": t.id, "name": t.name, "channel": t.channel, "address": t.address, "event_types": t.event_types,
            "active": t.active, "created_at": t.created_at}


def _job_dict(j: Job) -> dict:
    return {"id": j.id, "kind": j.kind, "status": j.status, "payload": j.payload, "scheduled_at": j.scheduled_at,
            "started_at": j.started_at, "completed_at": j.completed_at, "attempts": j.attempts, "error": j.error,
            "created_at": j.created_at}


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
            actor: str = Depends(require_editor)):
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
def patch_source(source_id: str, body: SourcePatch, db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings), actor: str = Depends(require_editor)):
    """Enable/disable a source: the documented way to switch off a faulty feed (SRS 16)."""
    src = db.get(DataSource, source_id)
    if not src:
        raise ApiError(404, "SOURCE_NOT_FOUND", f"Source '{source_id}' does not exist.")
    if body.enabled and src.licence_status != "not_applicable" and not settings.source_licence_reviewed:
        raise ApiError(409, "SOURCE_REVIEW_REQUIRED",
                       "This provider is a candidate. Complete licence, coverage and credential review before enabling it.")
    before = {"enabled": src.enabled}
    src.enabled = body.enabled
    audit(db, actor, "source.update", source_id, before, {"enabled": src.enabled})
    db.commit()
    return {"id": src.id, "enabled": src.enabled}


@router.patch("/datasets/{dataset_key}")
def patch_dataset(dataset_key: str, body: DatasetPatch, db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings), actor: str = Depends(require_editor)):
    """Change a dataset's freshness threshold or selected source. Changes are audited."""
    ds = db.get(Dataset, dataset_key)
    if not ds:
        raise ApiError(404, "DATASET_NOT_FOUND", f"Dataset '{dataset_key}' does not exist.")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise ApiError(422, "VALIDATION_ERROR", "No changes supplied.")
    if body.stale_after_minutes is not None and body.stale_after_minutes < ds.refresh_minutes:
        raise ApiError(422, "VALIDATION_ERROR",
                       f"stale_after_minutes must be at least the dataset refresh interval ({ds.refresh_minutes} min).")
    if body.source_id is not None:
        source = db.get(DataSource, body.source_id)
        if source is None:
            raise ApiError(404, "SOURCE_NOT_FOUND", f"Source '{body.source_id}' does not exist.")
        if not source.enabled:
            raise ApiError(409, "SOURCE_DISABLED", "Enable the source before assigning it to a dataset.")
        if source.licence_status != "not_applicable" and not settings.source_licence_reviewed:
            raise ApiError(409, "SOURCE_REVIEW_REQUIRED",
                           "This provider is a candidate. Complete licence, coverage and credential review before assigning it.")
    before = {"stale_after_minutes": ds.stale_after_minutes, "source_id": ds.source_id}
    if body.stale_after_minutes is not None:
        ds.stale_after_minutes = body.stale_after_minutes
    if body.source_id is not None:
        ds.source_id = body.source_id
    after = {"stale_after_minutes": ds.stale_after_minutes, "source_id": ds.source_id}
    audit(db, actor, "dataset.update", dataset_key, before, after)
    db.commit()
    return {"key": ds.key, "stale_after_minutes": ds.stale_after_minutes, "source_id": ds.source_id}


@router.get("/alert-rules")
def list_rules(db: Session = Depends(get_db)):
    return [_rule_dict(r) for r in db.scalars(select(AlertRule).order_by(AlertRule.id)).all()]


@router.post("/alert-rules", status_code=201)
def create_rule(body: AlertRuleCreate, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    if db.get(AlertRule, body.id):
        raise ApiError(409, "RULE_EXISTS", f"Alert rule '{body.id}' already exists.")
    ds = db.get(Dataset, body.dataset)
    if not ds:
        raise ApiError(422, "DATASET_NOT_FOUND", f"Dataset '{body.dataset}' does not exist.")
    rule = AlertRule(id=body.id, name=body.name, dataset_key=body.dataset, operator=body.operator,
                     threshold=body.threshold, unit=body.unit or ds.unit, category=body.category, zones=body.zones,
                     suppression_hours=body.suppression_hours, validity_hours=body.validity_hours,
                     enabled=body.enabled, review_status=body.review_status)
    db.add(rule)
    audit(db, actor, "alert_rule.create", rule.id, None, _rule_dict(rule))
    db.commit()
    return _rule_dict(rule)


@router.patch("/alert-rules/{rule_id}")
def patch_rule(rule_id: str, body: AlertRulePatch, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise ApiError(404, "RULE_NOT_FOUND", f"Alert rule '{rule_id}' does not exist.")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise ApiError(422, "VALIDATION_ERROR", "No changes supplied.")
    fields = ["name", "threshold", "operator", "enabled", "suppression_hours", "validity_hours", "review_status"]
    before = {k: getattr(rule, k) for k in fields}
    for k, v in changes.items():
        setattr(rule, k, v)
    audit(db, actor, "alert_rule.update", rule_id, before, {k: getattr(rule, k) for k in fields})
    db.commit()
    return _rule_dict(rule)


@router.delete("/alert-rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise ApiError(404, "RULE_NOT_FOUND", f"Alert rule '{rule_id}' does not exist.")
    db.execute(delete(AlertEvent).where(AlertEvent.rule_id == rule_id))
    before = _rule_dict(rule)
    db.delete(rule)
    audit(db, actor, "alert_rule.delete", rule_id, before, None)
    db.commit()


@router.get("/locations")
def admin_locations(db: Session = Depends(get_db)):
    return [_loc_dict(loc) for loc in db.scalars(select(Location).order_by(Location.name)).all()]


@router.post("/locations", status_code=201)
def create_location(body: LocationCreate, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    if db.get(Location, body.id):
        raise ApiError(409, "LOCATION_EXISTS", f"Location '{body.id}' already exists.")
    loc = Location(id=body.id, name=body.name, area=body.area, lat=body.lat, lng=body.lng, zone_value=body.zone)
    db.add(loc)
    audit(db, actor, "location.create", loc.id, None, _loc_dict(loc))
    db.commit()
    return _loc_dict(loc)


@router.patch("/locations/{location_id}")
def patch_location(location_id: str, body: LocationPatch, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    loc = db.get(Location, location_id)
    if not loc:
        raise ApiError(404, "LOCATION_NOT_FOUND", f"Location '{location_id}' does not exist.")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise ApiError(422, "VALIDATION_ERROR", "No changes supplied.")
    before = _loc_dict(loc)
    if "zone" in changes:
        loc.zone_value = changes.pop("zone")
    for k, v in changes.items():
        setattr(loc, k, v)
    audit(db, actor, "location.update", location_id, before, _loc_dict(loc))
    db.commit()
    return _loc_dict(loc)


@router.delete("/locations/{location_id}", status_code=204)
def delete_location(location_id: str, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    loc = db.get(Location, location_id)
    if not loc:
        raise ApiError(404, "LOCATION_NOT_FOUND", f"Location '{location_id}' does not exist.")
    if db.scalars(select(Observation.id).where(Observation.location_id == location_id).limit(1)).first():
        raise ApiError(409, "LOCATION_HAS_DATA", "Disable coverage by removing data first; this location has observations.")
    before = _loc_dict(loc)
    db.delete(loc)
    audit(db, actor, "location.delete", location_id, before, None)
    db.commit()


@router.get("/audit-log")
def audit_log(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return [{"at": a.at, "actor": a.actor, "action": a.action, "target": a.target, "before": a.before, "after": a.after}
            for a in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()]


@router.get("/staff")
def list_staff(db: Session = Depends(get_db), _: AdminUser = Depends(require_staff)):
    return [user_out(u) for u in db.scalars(select(AdminUser).order_by(AdminUser.email)).all()]


@router.post("/staff", status_code=201)
def create_staff(body: StaffCreate, db: Session = Depends(get_db), me: AdminUser = Depends(require_staff_admin)):
    email = body.email.strip().lower()
    if db.scalars(select(AdminUser).where(AdminUser.email == email)).first():
        raise ApiError(409, "STAFF_EXISTS", "An administrator with that email already exists.")
    user = AdminUser(email=email, password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    audit(db, me.email, "staff.create", email, None, {"role": body.role, "active": True})
    db.commit()
    return user_out(user)


@router.patch("/staff/{staff_id}")
def patch_staff(staff_id: int, body: StaffPatch, db: Session = Depends(get_db), me: AdminUser = Depends(require_staff_admin)):
    """Deactivate/reactivate staff, change role or reset a password. Each change signs that person out everywhere."""
    user = db.get(AdminUser, staff_id)
    if not user:
        raise ApiError(404, "STAFF_NOT_FOUND", f"Administrator {staff_id} does not exist.")
    if body.active is None and body.password is None and body.role is None:
        raise ApiError(422, "VALIDATION_ERROR", "No changes supplied.")
    if body.active is False and user.id == me.id:
        raise ApiError(409, "CANNOT_DEACTIVATE_SELF", "You cannot deactivate your own account.")
    if body.role is not None and user.id == me.id and body.role != "admin":
        raise ApiError(409, "CANNOT_DEMOTE_SELF", "You cannot remove your own administrator role.")
    changes = {}
    if body.active is not None:
        changes["active"] = body.active
        user.active = body.active
    if body.role is not None:
        changes["role"] = body.role
        user.role = body.role
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


@router.get("/quality")
def data_quality(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return quality.summary(db, settings)


@router.get("/api-keys")
def list_api_keys(db: Session = Depends(get_db)):
    return [_key_dict(k) for k in db.scalars(select(ApiKey).order_by(ApiKey.id.desc())).all()]


@router.post("/api-keys", status_code=201)
def create_api_key(body: ApiKeyCreateIn, db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                   actor: str = Depends(require_editor)):
    key, raw = api_keys.create_key(db, settings, body.name, body.owner_email, body.quota_per_day, body.environment)
    audit(db, actor, "api_key.create", key.prefix, None, {"name": key.name, "environment": key.environment, "quota_per_day": key.quota_per_day})
    db.commit()
    return {**_key_dict(key), "key": raw, "warning": "Store this key now. It is shown once and cannot be recovered."}


@router.patch("/api-keys/{key_id}")
def patch_api_key(key_id: int, body: ApiKeyPatch, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    key = db.get(ApiKey, key_id)
    if not key:
        raise ApiError(404, "API_KEY_NOT_FOUND", "API key does not exist.")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise ApiError(422, "VALIDATION_ERROR", "No changes supplied.")
    before = _key_dict(key)
    for k, v in changes.items():
        setattr(key, k, v)
    if body.active is False:
        key.revoked_at = utcnow()
    audit(db, actor, "api_key.update", key.prefix, before, _key_dict(key))
    db.commit()
    return _key_dict(key)


@router.post("/api-keys/{key_id}/rotate", status_code=201)
def rotate_api_key(key_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                   actor: str = Depends(require_editor)):
    old = db.get(ApiKey, key_id)
    if not old:
        raise ApiError(404, "API_KEY_NOT_FOUND", "API key does not exist.")
    old.active, old.revoked_at = False, utcnow()
    key, raw = api_keys.create_key(db, settings, old.name, old.owner_email, old.quota_per_day, old.environment)
    audit(db, actor, "api_key.rotate", old.prefix, {"active": True}, {"new_prefix": key.prefix})
    db.commit()
    return {**_key_dict(key), "key": raw, "warning": "Store this key now. It is shown once."}


@router.get("/notification-targets")
def list_targets(db: Session = Depends(get_db)):
    return [_target_dict(t) for t in db.scalars(select(NotificationTarget).order_by(NotificationTarget.id)).all()]


@router.post("/notification-targets", status_code=201)
def create_target(body: NotificationTargetIn, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    target = NotificationTarget(**body.model_dump())
    db.add(target)
    audit(db, actor, "notification_target.create", target.name, None, {"channel": target.channel})
    db.commit()
    return _target_dict(target)


@router.patch("/notification-targets/{target_id}")
def patch_target(target_id: int, body: NotificationTargetPatch, db: Session = Depends(get_db),
                 actor: str = Depends(require_editor)):
    target = db.get(NotificationTarget, target_id)
    if not target:
        raise ApiError(404, "NOTIFICATION_TARGET_NOT_FOUND", "Notification target does not exist.")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for k, v in changes.items():
        setattr(target, k, v)
    audit(db, actor, "notification_target.update", target.name, None, changes)
    db.commit()
    return _target_dict(target)


@router.delete("/notification-targets/{target_id}", status_code=204)
def delete_target(target_id: int, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    target = db.get(NotificationTarget, target_id)
    if not target:
        raise ApiError(404, "NOTIFICATION_TARGET_NOT_FOUND", "Notification target does not exist.")
    db.delete(target)
    audit(db, actor, "notification_target.delete", str(target_id), None, None)
    db.commit()


@router.get("/notifications")
def list_notifications(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return [{"id": n.id, "target_id": n.target_id, "event_type": n.event_type, "channel": n.channel,
             "destination": n.destination, "subject": n.subject, "body": n.body, "status": n.status,
             "attempts": n.attempts, "created_at": n.created_at, "sent_at": n.sent_at, "error": n.error}
            for n in db.scalars(select(Notification).order_by(Notification.id.desc()).limit(limit)).all()]


@router.post("/notifications/dispatch")
def dispatch_notifications(db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                           actor: str = Depends(require_editor)):
    count = notifications.dispatch_pending(db, settings)
    audit(db, actor, "notification.dispatch", "pending", None, {"processed": count})
    db.commit()
    return {"processed": count}


@router.post("/notification-targets/{target_id}/test")
def test_target(target_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings),
                actor: str = Depends(require_editor)):
    target = db.get(NotificationTarget, target_id)
    if not target:
        raise ApiError(404, "NOTIFICATION_TARGET_NOT_FOUND", "Notification target does not exist.")
    n = notifications.enqueue(db, "test", "Orizon Nimasa test notification",
                              "This is a test from the operations console. No action is required.", target)
    db.commit()
    notifications.dispatch_one(db, settings, n)
    audit(db, actor, "notification.test", target.name, None, {"status": n.status})
    db.commit()
    return {"id": n.id, "status": n.status, "error": n.error}


@router.get("/jobs")
def list_jobs(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return [_job_dict(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(limit)).all()]


@router.post("/jobs", status_code=201)
def create_job(body: JobCreateIn, db: Session = Depends(get_db), actor: str = Depends(require_editor)):
    job = jobs.enqueue(db, body.kind, body.payload)
    audit(db, actor, "job.create", body.kind, None, {"job_id": job.id})
    db.commit()
    return _job_dict(job)


@router.post("/jobs/run")
def run_jobs(limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings), actor: str = Depends(require_editor)):
    count = jobs.run_pending(db, settings, limit)
    audit(db, actor, "job.run", "pending", None, {"processed": count})
    db.commit()
    return {"processed": count}
