"""Platform features added beyond the original MVP: RBAC, MFA, reset, API keys, vessels, tides and admin CRUD."""
import pytest
from fastapi.testclient import TestClient

from app import totp
from app.db import utcnow
from tests.conftest import ADMIN, make_client

EMAIL, PASSWORD = "owner@blueintel.test", "correct-horse-battery"


@pytest.fixture
def app_client(monkeypatch):
    app = make_client(monkeypatch, ADMIN_BOOTSTRAP_EMAIL=EMAIL, ADMIN_BOOTSTRAP_PASSWORD=PASSWORD)
    with TestClient(app) as c:
        yield c


def login(c, email=EMAIL, password=PASSWORD, totp_code=None):
    body = {"email": email, "password": password}
    if totp_code:
        body["totp_code"] = totp_code
    return c.post("/api/v1/admin/auth/login", json=body)


def bearer(c, email=EMAIL, password=PASSWORD):
    return {"Authorization": f"Bearer {login(c, email, password).json()['access_token']}"}


def test_roles_enforce_read_only_viewers(app_client):
    h = bearer(app_client)
    made = app_client.post("/api/v1/admin/staff", headers=h,
                           json={"email": "viewer@blueintel.test", "password": "read-only-password", "role": "viewer"})
    assert made.status_code == 201 and made.json()["role"] == "viewer"
    vh = bearer(app_client, "viewer@blueintel.test", "read-only-password")
    assert app_client.get("/api/v1/admin/feeds", headers=vh).status_code == 200
    assert app_client.patch("/api/v1/admin/datasets/wave_height", headers=vh,
                            json={"stale_after_minutes": 240}).status_code == 403
    assert app_client.post("/api/v1/admin/staff", headers=vh,
                           json={"email": "x@blueintel.test", "password": "long-enough-password"}).status_code == 403


def test_mfa_setup_enable_and_required_login(app_client):
    h = bearer(app_client)
    setup = app_client.post("/api/v1/admin/auth/mfa/setup", headers=h)
    assert setup.status_code == 200 and setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    secret = setup.json()["secret"]
    assert app_client.post("/api/v1/admin/auth/mfa/enable", headers=h, json={"code": "000000"}).status_code == 400
    assert app_client.post("/api/v1/admin/auth/mfa/enable", headers=h,
                           json={"code": totp.now(secret)}).status_code == 200
    assert login(app_client).status_code == 401
    assert login(app_client, totp_code="000000").status_code == 401
    ok = login(app_client, totp_code=totp.now(secret))
    assert ok.status_code == 200 and ok.json()["user"]["mfa_enabled"] is True


def test_password_reset_is_single_use_and_revokes_sessions(app_client):
    h = bearer(app_client)
    requested = app_client.post("/api/v1/admin/auth/password-reset/request", json={"email": EMAIL})
    assert requested.status_code == 202
    token = requested.json().get("reset_token")
    assert token, "non-production without SMTP returns a local reset token"
    assert app_client.post("/api/v1/admin/auth/password-reset/confirm",
                           json={"token": token, "new_password": "a-brand-new-password"}).status_code == 200
    assert app_client.get("/api/v1/admin/feeds", headers=h).status_code == 401
    assert login(app_client).status_code == 401
    assert login(app_client, password="a-brand-new-password").status_code == 200
    assert app_client.post("/api/v1/admin/auth/password-reset/confirm",
                           json={"token": token, "new_password": "another-new-password"}).status_code == 400


def test_public_sandbox_key_quota_and_marine_endpoints(app_client):
    created = app_client.post("/api/v1/developer/keys", json={"name": "notebook"})
    assert created.status_code == 201
    raw = created.json()["key"]
    key_h = {"X-API-Key": raw}
    assert app_client.get("/api/v1/vessels", headers=key_h).status_code == 200
    assert len(app_client.get("/api/v1/vessels", headers=key_h).json()) == 6
    assert len(app_client.get("/api/v1/bathymetry", headers=key_h).json()) == 81
    tide = app_client.get("/api/v1/tides", params={"location_id": "lekki", "hours": 24}, headers=key_h)
    assert tide.status_code == 200 and tide.json()["simulated"] is True and len(tide.json()["points"]) == 25
    usage = app_client.get("/api/v1/developer/usage", headers=key_h).json()
    assert usage["requests"] >= 3 and usage["remaining"] >= 0
    assert app_client.get("/api/v1/vessels", headers={"X-API-Key": "bl_not-real"}).status_code == 401


def test_admin_crud_for_locations_rules_keys_and_jobs(app_client):
    h = bearer(app_client)
    loc = app_client.post("/api/v1/admin/locations", headers=h,
                          json={"id": "tarkwa-bay", "name": "Tarkwa Bay", "area": "Lagos Harbour mouth",
                                "lat": 6.40, "lng": 3.39, "zone": "coastal"})
    assert loc.status_code == 201 and loc.json()["zone"] == "coastal"
    rule = app_client.post("/api/v1/admin/alert-rules", headers=h,
                           json={"id": "tarkwa-wave", "name": "Tarkwa wave watch", "dataset": "wave_height",
                                 "operator": ">=", "threshold": 1.8, "unit": "m", "category": "coastal",
                                 "zones": ["coastal"], "suppression_hours": 3, "validity_hours": 3})
    assert rule.status_code == 201 and rule.json()["zones"] == ["coastal"]
    assert app_client.patch("/api/v1/admin/alert-rules/tarkwa-wave", headers=h,
                            json={"threshold": 2.1}).json()["threshold"] == 2.1
    assert "tarkwa-wave" in {r["id"] for r in app_client.get("/api/v1/alert-rules").json()}
    key = app_client.post("/api/v1/admin/api-keys", headers=h, json={"name": "ops", "quota_per_day": 100})
    assert key.status_code == 201 and key.json()["key"].startswith("bl_")
    assert app_client.patch(f"/api/v1/admin/api-keys/{key.json()['id']}", headers=h,
                            json={"active": False}).json()["active"] is False
    job = app_client.post("/api/v1/admin/jobs", headers=h, json={"kind": "quality_scan"})
    assert job.status_code == 201
    assert app_client.post("/api/v1/admin/jobs/run", headers=h).json()["processed"] >= 1
    assert app_client.get("/api/v1/admin/quality", headers=h).json()["datasets"] == 5
    assert app_client.delete("/api/v1/admin/locations/tarkwa-bay", headers=h).status_code == 204
    assert app_client.delete("/api/v1/admin/alert-rules/tarkwa-wave", headers=h).status_code == 204
