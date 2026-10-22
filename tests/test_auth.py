import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.models import AdminSession
from tests.conftest import ADMIN, make_client

EMAIL, PASSWORD = "boss@blueintel.test", "correct-horse-battery"


@pytest.fixture
def staff_client(monkeypatch):
    app = make_client(monkeypatch, ADMIN_BOOTSTRAP_EMAIL=EMAIL.upper(), ADMIN_BOOTSTRAP_PASSWORD=PASSWORD,
                      ADMIN_MAX_FAILED_LOGINS="3")
    with TestClient(app) as c:
        yield c


def login(c, email=EMAIL, password=PASSWORD):
    return c.post("/api/v1/admin/auth/login", json={"email": email, "password": password})


def bearer(c) -> dict:
    return {"Authorization": f"Bearer {login(c).json()['access_token']}"}


def test_public_endpoints_need_no_login(staff_client):
    for path in ("/api/v1/health", "/api/v1/sources", "/api/v1/locations", "/api/v1/alerts"):
        assert staff_client.get(path).status_code == 200


def test_admin_routes_reject_anonymous_callers(staff_client):
    assert staff_client.get("/api/v1/admin/feeds").status_code == 401
    assert staff_client.get("/api/v1/admin/staff").status_code == 401
    assert staff_client.get("/api/v1/admin/feeds", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401


def test_login_and_use_session(staff_client):
    r = login(staff_client)
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"
    assert "password" not in r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert staff_client.get("/api/v1/admin/feeds", headers=h).status_code == 200
    assert staff_client.get("/api/v1/admin/auth/me", headers=h).json()["email"] == EMAIL.lower()  # bootstrap email normalised


def test_bad_credentials_are_indistinguishable(staff_client):
    wrong_pw = login(staff_client, password="wrong-password-123")
    no_user = login(staff_client, email="nobody@blueintel.test")
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json()


def test_lockout_after_repeated_failures_even_with_right_password(staff_client):
    for _ in range(3):
        assert login(staff_client, password="wrong-password-123").status_code == 401
    assert login(staff_client).status_code == 401  # locked
    with dbmod.SessionLocal() as s:
        from app.models import AuditLog
        assert "auth.locked" in {a.action for a in s.query(AuditLog).all()}


def test_logout_revokes_the_session(staff_client):
    h = bearer(staff_client)
    assert staff_client.post("/api/v1/admin/auth/logout", headers=h).status_code == 200
    assert staff_client.get("/api/v1/admin/feeds", headers=h).status_code == 401


def test_expired_session_is_rejected(staff_client):
    h = bearer(staff_client)
    with dbmod.SessionLocal() as s:
        from app.db import utcnow
        from datetime import timedelta
        for sess in s.query(AdminSession).all():
            sess.expires_at = utcnow() - timedelta(minutes=1)
        s.commit()
    assert staff_client.get("/api/v1/admin/feeds", headers=h).status_code == 401


def test_staff_management_and_audit(staff_client):
    h = bearer(staff_client)
    weak = staff_client.post("/api/v1/admin/staff", headers=h, json={"email": "a@blueintel.test", "password": "short"})
    assert weak.status_code == 422
    ok = staff_client.post("/api/v1/admin/staff", headers=h, json={"email": "Ops@BlueIntel.test", "password": "another-long-password"})
    assert ok.status_code == 201 and ok.json()["email"] == "ops@blueintel.test" and "password" not in ok.text
    assert staff_client.post("/api/v1/admin/staff", headers=h, json={"email": "ops@blueintel.test", "password": "another-long-password"}).status_code == 409
    ops_h = bearer_for(staff_client, "ops@blueintel.test", "another-long-password")
    assert staff_client.get("/api/v1/admin/feeds", headers=ops_h).status_code == 200

    # deactivation signs the person out immediately and blocks new logins
    ops_id = ok.json()["id"]
    assert staff_client.patch(f"/api/v1/admin/staff/{ops_id}", headers=h, json={"active": False}).status_code == 200
    assert staff_client.get("/api/v1/admin/feeds", headers=ops_h).status_code == 401
    assert login(staff_client, "ops@blueintel.test", "another-long-password").status_code == 401

    me_id = staff_client.get("/api/v1/admin/auth/me", headers=h).json()["id"]
    assert staff_client.patch(f"/api/v1/admin/staff/{me_id}", headers=h, json={"active": False}).status_code == 409

    log = staff_client.get("/api/v1/admin/audit-log", headers=h).json()
    assert {"auth.login", "staff.create", "staff.update"} <= {a["action"] for a in log}
    assert all(a["actor"] == EMAIL.lower() or a["actor"] == "ops@blueintel.test" for a in log)


def bearer_for(c, email, password) -> dict:
    return {"Authorization": f"Bearer {login(c, email, password).json()['access_token']}"}


def test_password_change_signs_out_other_sessions(staff_client):
    h1, h2 = bearer(staff_client), bearer(staff_client)
    new = "a-brand-new-password"
    assert staff_client.post("/api/v1/admin/auth/change-password", headers=h1,
                             json={"current_password": "wrong-one-here", "new_password": new}).status_code == 403
    assert staff_client.post("/api/v1/admin/auth/change-password", headers=h1,
                             json={"current_password": PASSWORD, "new_password": new}).status_code == 200
    assert staff_client.get("/api/v1/admin/feeds", headers=h1).status_code == 200
    assert staff_client.get("/api/v1/admin/feeds", headers=h2).status_code == 401
    assert login(staff_client).status_code == 401 and login(staff_client, password=new).status_code == 200


def test_service_key_works_but_cannot_manage_staff(monkeypatch):
    app = make_client(monkeypatch, ADMIN_BOOTSTRAP_EMAIL=EMAIL, ADMIN_BOOTSTRAP_PASSWORD=PASSWORD)
    with TestClient(app) as c:
        assert c.get("/api/v1/admin/feeds", headers=ADMIN).status_code == 200
        r = c.get("/api/v1/admin/staff", headers=ADMIN)
        assert r.status_code == 403 and r.json()["error"]["code"] == "STAFF_LOGIN_REQUIRED"


def test_bootstrap_only_runs_when_no_admins_exist_and_rejects_weak_password(monkeypatch):
    with pytest.raises(RuntimeError):
        with TestClient(make_client(monkeypatch, ADMIN_BOOTSTRAP_EMAIL=EMAIL, ADMIN_BOOTSTRAP_PASSWORD="short")):
            pass


def test_login_is_rate_limited(monkeypatch):
    app = make_client(monkeypatch, LOGIN_RATE_LIMIT_PER_MINUTE="2", ADMIN_BOOTSTRAP_EMAIL=EMAIL, ADMIN_BOOTSTRAP_PASSWORD=PASSWORD)
    with TestClient(app) as c:
        codes = [login(c, password="wrong-password-123").status_code for _ in range(3)]
    assert codes == [401, 401, 429]
