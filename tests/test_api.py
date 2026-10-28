from datetime import datetime, timedelta, timezone

from tests.conftest import ADMIN


def test_health_separates_app_and_feeds(client):
    r = client.get("/api/v1/health").json()
    assert r["status"] == "ok" and r["app"]["database"] == "ok"
    assert r["feeds"]["status"] == "healthy"
    assert r["feeds"]["sources"][0]["last_successful_sync"] is not None


def test_metadata_endpoints(client):
    assert {s["id"] for s in client.get("/api/v1/sources").json()} == {"demo", "open_meteo", "meteosource"}
    assert len(client.get("/api/v1/locations").json()) == 8
    assert {d["key"] for d in client.get("/api/v1/datasets").json()} == {"wave_height", "wind_speed", "sst", "visibility", "current_speed"}


def test_summary_has_unit_source_time_and_type_on_every_value(client):
    r = client.get("/api/v1/locations/lekki/summary").json()
    assert len(r["indicators"]) == 5
    for i in r["indicators"]:
        assert i["unit"] and i["observed_at"] and i["source"]["name"] and i["data_type"] in ("observation", "model_estimate")
        assert i["status"] == "fresh"
    assert r["warnings"] == []


def test_series_points_labelled_and_gaps_reported(client):
    r = client.get("/api/v1/locations/eleko/series", params={"dataset": "wind_speed", "start": (datetime.now(timezone.utc) - timedelta(days=6)).isoformat(), "end": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()}).json()
    kinds = {p["data_type"] for p in r["points"]}
    assert "observation" in kinds and "forecast" in kinds
    assert r["gaps"], "missing hours must be reported as gaps"
    times = [p["time"] for p in r["points"]]
    assert times == sorted(times) and len(times) == len(set(times))


def test_series_validation_and_limits(client):
    assert client.get("/api/v1/locations/nowhere/summary").status_code == 404
    assert client.get("/api/v1/locations/lekki/series", params={"dataset": "nope"}).status_code == 404
    assert client.get("/api/v1/locations/lekki/series").status_code == 422  # dataset required
    big = client.get("/api/v1/locations/lekki/series", params={"dataset": "sst", "start": "2026-01-01T00:00:00Z", "end": "2026-06-01T00:00:00Z"})
    assert big.status_code == 422 and big.json()["error"]["code"] == "RANGE_TOO_LARGE"
    naive = client.get("/api/v1/locations/lekki/series", params={"dataset": "sst", "start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00"})
    assert naive.status_code == 422


def test_error_shape_is_consistent_and_hides_internals(client):
    e = client.get("/api/v1/locations/nowhere/summary").json()["error"]
    assert set(e) == {"code", "message"} and "Traceback" not in e["message"]


def test_stale_feed_is_flagged_and_alerts_not_active(stale_client):
    s = stale_client.get("/api/v1/locations/lekki/summary").json()
    status = {i["dataset"]: i["status"] for i in s["indicators"]}
    assert status["wave_height"] == "stale" and status["wind_speed"] == "stale" and status["sst"] == "fresh"
    assert any("stale" in w for w in s["warnings"])
    assert s["active_alerts"] == 0
    assert all(a["status"] != "active" for a in stale_client.get("/api/v1/alerts", params={"include_expired": True}).json())
    assert stale_client.get("/api/v1/health").json()["feeds"]["status"] == "degraded"


def test_alert_fields_are_traceable(client):
    alerts = client.get("/api/v1/alerts", params={"include_expired": True}).json()
    assert alerts, "the demo data should trigger at least one wave advisory"
    a = alerts[0]
    for k in ("rule_id", "value", "threshold", "unit", "data_type", "source_id", "source_timestamp", "issued_at", "valid_until", "status", "condition"):
        assert a[k] not in (None, "")
    assert a["value"] >= a["threshold"]


def test_no_duplicate_alerts_within_suppression_window(client):
    alerts = client.get("/api/v1/alerts", params={"include_expired": True}).json()
    by_key = {}
    for a in alerts:
        by_key.setdefault((a["rule_id"], a["location_id"]), []).append(datetime.fromisoformat(a["source_timestamp"].replace("Z", "+00:00")))
    rules = {r["id"]: r["suppression_hours"] for r in client.get("/api/v1/alert-rules").json()}
    for (rule, _), times in by_key.items():
        times.sort()
        for a, b in zip(times, times[1:]):
            assert b - a >= timedelta(hours=rules[rule])


def test_feedback_validation_and_minimisation(client):
    assert client.post("/api/v1/feedback", json={}).status_code == 422
    assert client.post("/api/v1/feedback", json={"comments": "x" * 1001}).status_code == 422
    assert client.post("/api/v1/feedback", json={"found_information": "yes_easily", "comments": "clear"}).status_code == 201


def test_rate_limit(monkeypatch):
    from fastapi.testclient import TestClient
    from tests.conftest import make_client
    app = make_client(monkeypatch, RATE_LIMIT_PER_MINUTE="2")
    with TestClient(app) as c:
        codes = [c.post("/api/v1/feedback", json={"comments": "hi"}).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


def test_csv_export_has_time_unit_type_and_source(client):
    r = client.get("/api/v1/locations/lekki/series.csv", params={"dataset": "sst"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0] == "time_utc,location_id,dataset,value,unit,data_type,source_id,source_name,attribution"
    assert len(lines) > 1 and all(row.split(",")[4] and row.split(",")[6] for row in lines[1:])
    assert client.get("/api/v1/locations/lekki/series.csv", params={"dataset": "nope"}).status_code == 404


def test_public_data_endpoints_are_rate_limited(monkeypatch):
    from fastapi.testclient import TestClient
    from tests.conftest import make_client
    app = make_client(monkeypatch, RATE_LIMIT_PER_MINUTE="2")
    with TestClient(app) as c:
        codes = [c.get("/api/v1/locations/lekki/summary").status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        assert c.get("/api/v1/health").status_code == 200  # monitors must not be throttled


def test_hsts_only_in_production(monkeypatch):
    from fastapi.testclient import TestClient
    from tests.conftest import make_client
    with TestClient(make_client(monkeypatch, APP_ENV="production")) as c:
        assert "max-age" in c.get("/api/v1/health").headers["strict-transport-security"]


# ---- admin ----
def test_admin_can_change_freshness_threshold_audited_and_bounded(client):
    path = "/api/v1/admin/datasets/wave_height"
    assert client.patch(path, json={"stale_after_minutes": 300}).status_code == 401
    assert client.patch(path, headers=ADMIN, json={"stale_after_minutes": 1}).status_code == 422  # below refresh interval
    assert client.patch("/api/v1/admin/datasets/nope", headers=ADMIN, json={"stale_after_minutes": 300}).status_code == 404
    ok = client.patch(path, headers=ADMIN, json={"stale_after_minutes": 300})
    assert ok.status_code == 200 and ok.json()["stale_after_minutes"] == 300
    assert next(d for d in client.get("/api/v1/datasets").json() if d["key"] == "wave_height")["stale_after_minutes"] == 300
    assert client.get("/api/v1/admin/audit-log", headers=ADMIN).json()[0]["action"] == "dataset.update"


def test_admin_requires_credentials(client):
    assert client.get("/api/v1/admin/feeds").status_code == 401
    assert client.get("/api/v1/admin/feeds", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/admin/feeds", headers=ADMIN).status_code == 200
    assert client.get("/api/v1/admin/feeds", headers={"Authorization": f"Bearer {ADMIN['X-API-Key']}"}).status_code == 200


def test_admin_disabled_without_key(monkeypatch):
    from fastapi.testclient import TestClient
    from tests.conftest import make_client
    app = make_client(monkeypatch, ADMIN_API_KEY="")
    with TestClient(app) as c:
        assert c.get("/api/v1/admin/feeds", headers=ADMIN).status_code == 503


def test_unauthorised_rule_change_is_rejected_and_authorised_one_is_audited(client):
    assert client.patch("/api/v1/admin/alert-rules/wave-high", json={"threshold": 9}).status_code == 401
    assert client.get("/api/v1/alert-rules").json()[0]["condition"].startswith(">= 2")
    ok = client.patch("/api/v1/admin/alert-rules/wave-high", headers=ADMIN, json={"threshold": 2.5})
    assert ok.status_code == 200 and ok.json()["threshold"] == 2.5
    log = client.get("/api/v1/admin/audit-log", headers=ADMIN).json()
    assert log[0]["action"] == "alert_rule.update" and log[0]["before"]["threshold"] == 2.0 and log[0]["after"]["threshold"] == 2.5
    assert client.patch("/api/v1/admin/alert-rules/wave-high", headers=ADMIN, json={"threshold": 2.5, "suppression_hours": -1}).status_code == 422
    assert client.patch("/api/v1/admin/alert-rules/missing", headers=ADMIN, json={"threshold": 1}).status_code == 404


def test_admin_can_disable_a_source_and_trigger_ingestion(client):
    runs = client.post("/api/v1/admin/ingestion/run", headers=ADMIN).json()
    assert runs[0]["status"] == "success"
    assert client.patch("/api/v1/admin/sources/demo", headers=ADMIN, json={"enabled": False}).json()["enabled"] is False
    assert client.post("/api/v1/admin/ingestion/run", headers=ADMIN).json() == []
    actions = [a["action"] for a in client.get("/api/v1/admin/audit-log", headers=ADMIN).json()]
    assert "source.update" in actions and "ingestion.run" in actions


def test_admin_feeds_and_runs_have_no_secrets(client):
    body = client.get("/api/v1/admin/feeds", headers=ADMIN).text + client.get("/api/v1/admin/ingestion-runs", headers=ADMIN).text
    assert ADMIN["X-API-Key"] not in body


def test_hosted_postgres_urls_are_normalised_for_sqlalchemy():
    from app.db import normalize_database_url as n
    assert n("postgres://u:p@h.aivencloud.com:1/db?sslmode=require") == "postgresql+psycopg://u:p@h.aivencloud.com:1/db?sslmode=require"
    assert n("postgresql://u:p@h/db").startswith("postgresql+psycopg://")
    assert n("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_bulk_summaries_cover_every_location(client):
    r = client.get("/api/v1/summaries").json()
    assert len(r) == 8 and all(len(s["indicators"]) == 5 for s in r)
    two = client.get("/api/v1/summaries", params={"locations": "lekki,eleko"}).json()
    assert [s["location"]["id"] for s in two] == ["lekki", "eleko"]


def test_bulk_endpoints_reject_unknown_and_oversized_location_lists(client):
    assert client.get("/api/v1/summaries", params={"locations": "lekki,nowhere"}).status_code == 404
    too_many = ",".join(f"loc{i}" for i in range(21))
    assert client.get("/api/v1/series", params={"locations": too_many, "dataset": "sst"}).status_code == 422


def test_bulk_series_matches_single_location_series(client):
    bulk = client.get("/api/v1/series", params={"locations": "lekki,eleko", "dataset": "sst"}).json()
    single = client.get("/api/v1/locations/eleko/series", params={"dataset": "sst"}).json()
    assert [s["location_id"] for s in bulk] == ["lekki", "eleko"]
    assert bulk[1]["points"] == single["points"]


def test_alerts_status_filter(client):
    everything = client.get("/api/v1/alerts", params={"include_expired": True}).json()
    for status in ("active", "expired", "suppressed_stale"):
        got = client.get("/api/v1/alerts", params={"status": status}).json()
        assert all(a["status"] == status for a in got)
        assert len(got) == sum(a["status"] == status for a in everything)
    assert client.get("/api/v1/alerts", params={"status": "bogus"}).status_code == 422


def test_datasets_expose_source_id(client):
    assert all(d["source_id"] for d in client.get("/api/v1/datasets").json())


def test_admin_lists_alert_rules(client):
    assert client.get("/api/v1/admin/alert-rules").status_code == 401
    rules = client.get("/api/v1/admin/alert-rules", headers=ADMIN).json()
    assert rules and {"id", "operator", "threshold", "enabled", "review_status"} <= set(rules[0])


def test_chart_exports_png_jpg_pdf(client):
    magic = {"png": (b"\x89PNG", "image/png"), "jpg": (b"\xff\xd8", "image/jpeg"), "pdf": (b"%PDF", "application/pdf")}
    for fmt, (head, ctype) in magic.items():
        r = client.get(f"/api/v1/locations/lekki/series.{fmt}", params={"dataset": "wave_height"})
        assert r.status_code == 200 and r.content.startswith(head) and r.headers["content-type"].startswith(ctype)
        assert f"lekki_wave_height.{fmt}" in r.headers["content-disposition"]
    assert client.get("/api/v1/locations/lekki/series.png", params={"dataset": "nope"}).status_code == 404
    assert client.get("/api/v1/locations/nowhere/series.pdf", params={"dataset": "sst"}).status_code == 404
    assert client.get("/api/v1/locations/lekki/series.gif", params={"dataset": "sst"}).status_code == 422


def test_favicon_and_branding(client):
    ico = client.get("/favicon.ico")
    assert ico.status_code == 200 and ico.content[:4] == b"\x00\x00\x01\x00"
    assert client.get("/static/favicon-32x32.png").content.startswith(b"\x89PNG")
    assert "/static/favicon-32x32.png" in client.get("/docs").text
    assert client.get("/").json()["name"] == "Orizon Nimasa API"


def test_navigation_zones_and_rules(client):
    zones = {l["id"]: l["zone"] for l in client.get("/api/v1/locations").json()}
    assert zones["lagos-lagoon"] == "waterway" and zones["lagos-offshore"] == "offshore" and zones["lekki"] == "coastal"
    rules = {r["id"]: r["category"] for r in client.get("/api/v1/alert-rules").json()}
    assert rules["nav-ocean-visibility"] == "ocean_navigation" and rules["nav-waterway-current"] == "waterway_navigation"
    assert rules["wave-high"] == "coastal"
    everything = client.get("/api/v1/alerts", params={"include_expired": True}).json()
    assert all(a["category"] and a["zone"] for a in everything)
    for cat, allowed in (("ocean_navigation", {"coastal", "offshore"}), ("waterway_navigation", {"waterway"})):
        got = client.get("/api/v1/alerts", params={"category": cat, "include_expired": True}).json()
        assert all(a["category"] == cat and a["zone"] in allowed for a in got)
    assert client.get("/api/v1/alerts", params={"category": "bogus"}).status_code == 422
