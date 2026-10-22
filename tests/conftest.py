import os

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app import db as dbmod
from app.config import get_settings
from app.main import create_app
from app.routers.insights import llm_dep
from app.security import limiter
from app.services import ingestion
from app.services.llm import LLMError

ADMIN_KEY = "test-admin-key-0123456789abcdef"


class FakeLLM:
    """Stands in for Groq so tests never touch the network."""

    model = "fake-model"

    def __init__(self, reply="Wave height is 1.7 m [E1].", error: LLMError | None = None):
        self.reply, self.error, self.calls = reply, error, []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return self.reply


def _test_database_url() -> str:
    """Tests need their own PostgreSQL database (TEST_DATABASE_URL in the environment or .env).

    Every test starts by DROPPING all tables, so this refuses to run unless the database name ends in '_test' and
    differs from DATABASE_URL: it can never wipe a real database."""
    url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(".env").get("TEST_DATABASE_URL") or ""
    if not url:
        pytest.exit("Set TEST_DATABASE_URL to a PostgreSQL database named '*_test' (see README, 'Running the tests').", returncode=2)
    url = dbmod.normalize_database_url(url)
    name = make_url(url).database or ""
    real = os.environ.get("DATABASE_URL") or dotenv_values(".env").get("DATABASE_URL") or ""
    if not name.endswith("_test") or (real and dbmod.normalize_database_url(real) == url):
        pytest.exit(f"Refusing to run: test database '{name}' must be named '*_test' and differ from DATABASE_URL.", returncode=2)
    return url


def _reset_database(url: str) -> None:
    from app import models  # noqa: F401  (registers every table on Base.metadata)
    engine = create_engine(url)
    dbmod.Base.metadata.drop_all(engine)
    engine.dispose()


def make_client(monkeypatch, **env):
    url = _test_database_url()
    _reset_database(url)
    base = {"DATABASE_URL": url, "SCHEDULER_ENABLED": "false", "ADMIN_API_KEY": ADMIN_KEY,
            "INGEST_BACKOFF_SECONDS": "0", "GROQ_API_KEY": "",
            # Blank out anything a developer's real .env might set, so tests never depend on local settings.
            "ADMIN_BOOTSTRAP_EMAIL": "", "ADMIN_BOOTSTRAP_PASSWORD": "", "APP_ENV": "development", "AI_ENABLED": "true"}
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    limiter.reset()
    return create_app()


@pytest.fixture
def client(monkeypatch):
    app = make_client(monkeypatch)
    with TestClient(app) as c:
        with dbmod.SessionLocal() as s:
            ingestion.run_all(s, get_settings())
        c.app_ref = app
        yield c
    get_settings.cache_clear()


@pytest.fixture
def stale_client(monkeypatch):
    app = make_client(monkeypatch, DEMO_LAG_HOURS="8")
    with TestClient(app) as c:
        with dbmod.SessionLocal() as s:
            ingestion.run_all(s, get_settings())
        yield c
    get_settings.cache_clear()


@pytest.fixture
def db_session(client):
    with dbmod.SessionLocal() as s:
        yield s


ADMIN = {"X-API-Key": ADMIN_KEY}
__all__ = ["FakeLLM", "llm_dep", "ADMIN"]
