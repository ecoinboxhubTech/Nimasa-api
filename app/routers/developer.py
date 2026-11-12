"""Self-service developer sandbox: key creation, quota visibility and copy-paste recipes."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.errors import ApiError
from app.models import ApiKey
from app.schemas import ApiKeyCreatedOut, ApiKeyCreateIn, ApiUsageOut
from app.security import audit, limiter, require_developer_key
from app.services import api_keys

router = APIRouter(prefix="/api/v1/developer", tags=["developer"])


def _key_out(k: ApiKey) -> dict:
    return {"id": k.id, "name": k.name, "prefix": k.prefix, "owner_email": k.owner_email, "active": k.active,
            "environment": k.environment, "scopes": k.scopes, "quota_per_day": k.quota_per_day, "rate_limit_per_minute": k.rate_limit_per_minute,
            "created_at": k.created_at, "last_used_at": k.last_used_at, "revoked_at": k.revoked_at}


@router.post("/keys", response_model=ApiKeyCreatedOut, status_code=201)
def create_sandbox_key(request: Request, body: ApiKeyCreateIn | None = None, db: Session = Depends(get_db),
                       settings: Settings = Depends(get_settings)):
    """Create a free sandbox key. Intentionally low-friction for the hackathon, with a per-IP hourly limit."""
    client = request.client.host if request.client else "unknown"
    limiter.check("developer-key-create", client, settings.developer_keys_per_hour, window=3600)
    body = body or ApiKeyCreateIn()
    if body.environment != "test":
        raise ApiError(403, "ADMIN_REQUIRED", "Production keys can only be created by an administrator.")
    key, raw = api_keys.create_key(db, settings, body.name, body.owner_email,
                                   min(body.quota_per_day or settings.developer_key_quota_per_day, 100_000),
                                   body.environment)
    return {**_key_out(key), "key": raw, "warning": "Store this key now. It is shown once and cannot be recovered."}


@router.delete("/keys/current", status_code=204)
def revoke_current_key(key: ApiKey = Depends(require_developer_key), db: Session = Depends(get_db)):
    key.active = False
    key.revoked_at = utcnow()
    audit(db, f"developer:{key.prefix}", "api_key.revoke", key.prefix, {"active": True}, {"active": False})
    db.commit()
    return None


@router.get("/usage", response_model=ApiUsageOut)
def usage(key: ApiKey = Depends(require_developer_key), db: Session = Depends(get_db)):
    row = api_keys.usage_today(db, key)
    return {"api_key_id": key.id, "day": row.day, "requests": row.requests, "quota": key.quota_per_day,
            "remaining": max(0, key.quota_per_day - row.requests), "last_path": row.last_path}


@router.get("/recipes")
def recipes():
    """Machine-readable quickstart used by the developer page."""
    return {
        "base_url": "/api/v1",
        "auth": {"header": "X-API-Key", "create": "POST /api/v1/developer/keys"},
        "endpoints": [
            {"method": "GET", "path": "/vessels", "example": "?bbox=2.8,6.1,3.95,6.6"},
            {"method": "GET", "path": "/vessels/657123450/track", "example": "?hours=24"},
            {"method": "GET", "path": "/bathymetry", "example": "?bbox=2.8,6.1,3.95,6.6"},
            {"method": "GET", "path": "/tides", "example": "?location_id=lekki&hours=48"},
            {"method": "GET", "path": "/series", "example": "?locations=lekki,eleko&dataset=wave_height"},
            {"method": "POST", "path": "/insights/query", "example": "{\"question\":\"wave height at Lekki?\"}"},
        ],
        "curl": "curl -H \"X-API-Key: bl_test_...\" http://localhost:8015/api/v1/vessels",
        "javascript": "const r = await fetch('/api/v1/tides?location_id=lekki', {headers: {'X-API-Key': key}});",
        "python": "import requests\\nr = requests.get('http://localhost:8015/api/v1/tides', params={'location_id': 'lekki'}, headers={'X-API-Key': key})\\nr.raise_for_status()\\nprint(r.json())",
        "note": "The demo AIS, bathymetry and tide sources are synthetic and explicitly labelled. Replace adapters before operational use.",
    }
