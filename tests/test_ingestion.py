from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app import db as dbmod
from app.config import get_settings
from app.models import DataSource, Dataset, IngestionRun, Location, Observation
from app.services import ingestion
from app.services.adapters import AdapterError, RawRecord
from app.services.ingestion import validate_record

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def rec(**kw):
    d = dict(dataset_key="wave_height", location_id="lekki", observed_at=NOW, value=1.5, unit="m", kind="model_estimate")
    d.update(kw)
    return RawRecord(**d)


def ctx(db_session):
    ds = {d.key: d for d in db_session.scalars(select(Dataset))}
    return ds, {loc.id for loc in db_session.scalars(select(Location))}


def test_validation_rules(db_session):
    ds, locs = ctx(db_session)
    check = lambda r: validate_record(r, ds, locs, NOW)
    assert check(rec()) is None
    assert check(rec(dataset_key="nope")) == "unknown_dataset"
    assert check(rec(location_id="mars")) == "unknown_location"
    assert check(rec(kind="guess")) == "invalid_kind"
    assert check(rec(observed_at=datetime(2026, 10, 20, 12, 0))) == "invalid_timestamp"
    assert check(rec(observed_at=NOW + timedelta(days=30))) == "timestamp_too_far_in_future"
    assert check(rec(value=None)) == "missing_or_invalid_value"
    assert check(rec(value=float("inf"))) == "missing_or_invalid_value"
    assert check(rec(unit="ft")) == "unit_mismatch"
    assert check(rec(value=99.0)) == "impossible_value"
    assert check(rec(value=-1.0)) == "impossible_value"


def test_demo_ingest_stores_data_and_is_idempotent(db_session):
    count = db_session.scalar(select(func.count()).select_from(Observation))
    assert count > 1000
    runs = ingestion.run_all(db_session, get_settings())
    assert runs[0].status == "success"
    assert db_session.scalar(select(func.count()).select_from(Observation)) <= count + 50  # no duplicate explosion


def test_every_record_traces_to_a_run(db_session):
    orphan = db_session.scalar(select(func.count()).select_from(Observation).where(Observation.run_id.is_(None)))
    assert orphan == 0


def test_gaps_stay_gaps(db_session):
    from app.services.data import find_gaps, get_series
    now = datetime.now(timezone.utc)
    pts = [p.observed_at for p in get_series(db_session, "wind_speed", "eleko", now - timedelta(days=7), now) if p.kind != "forecast"]
    assert find_gaps(pts, 60)  # the demo feed has a deliberate multi-hour hole at Eleko


def test_failed_adapter_logs_failure_and_does_not_crash(db_session, monkeypatch):
    class Boom:
        def fetch(self, *a):
            raise AdapterError("UPSTREAM_TIMEOUT", "timeout")
    monkeypatch.setattr(ingestion, "get_adapter", lambda *a: Boom())
    run = ingestion.run_source(db_session, get_settings(), db_session.get(DataSource, "demo"))
    assert run.status == "failed" and run.error_category == "UPSTREAM_TIMEOUT"


def test_retries_then_succeeds(db_session, monkeypatch):
    calls = {"n": 0}

    class Flaky:
        def fetch(self, locations, datasets, now):
            calls["n"] += 1
            if calls["n"] < 3:
                raise AdapterError("UPSTREAM_TIMEOUT", "timeout")
            return [rec(observed_at=now.replace(minute=0, second=0, microsecond=0))]
    monkeypatch.setattr(ingestion, "get_adapter", lambda *a: Flaky())
    run = ingestion.run_source(db_session, get_settings(), db_session.get(DataSource, "demo"))
    assert calls["n"] == 3 and run.status == "success"


def test_invalid_records_are_rejected_and_counted(db_session, monkeypatch):
    class Mixed:
        def fetch(self, locations, datasets, now):
            t = now.replace(minute=0, second=0, microsecond=0)
            return [rec(observed_at=t - timedelta(days=20)), rec(observed_at=t - timedelta(hours=1), value=500.0), rec(observed_at=t - timedelta(hours=2), value=None)]
    monkeypatch.setattr(ingestion, "get_adapter", lambda *a: Mixed())
    run = ingestion.run_source(db_session, get_settings(), db_session.get(DataSource, "demo"))
    assert run.status == "partial" and run.rejected_count == 2 and run.record_count == 1
    assert "impossible_value" in run.error_summary


def test_real_source_blocked_until_licence_reviewed(db_session):
    src = db_session.get(DataSource, "open_meteo")
    run = ingestion.run_source(db_session, get_settings(), src)
    assert run.status == "failed" and run.error_category == "LICENCE_NOT_REVIEWED"


def test_retention_purge(db_session):
    from app.services.ingestion import purge_old
    s = get_settings()
    far = datetime.now(timezone.utc) + timedelta(days=s.retention_days + 30)
    purge_old(db_session, s, far)
    assert db_session.scalar(select(func.count()).select_from(Observation).where(Observation.observed_at < far - timedelta(days=s.retention_days))) == 0
