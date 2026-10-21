import pytest
from fastapi.testclient import TestClient

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


def make_client(monkeypatch, tmp_path, **env):
    base = {"DATABASE_URL": f"sqlite:///{tmp_path}/t.db", "SCHEDULER_ENABLED": "false", "ADMIN_API_KEY": ADMIN_KEY,
            "INGEST_BACKOFF_SECONDS": "0", "GROQ_API_KEY": ""}
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    limiter.reset()
    return create_app()


@pytest.fixture
def client(monkeypatch, tmp_path):
    app = make_client(monkeypatch, tmp_path)
    with TestClient(app) as c:
        with dbmod.SessionLocal() as s:
            ingestion.run_all(s, get_settings())
        c.app_ref = app
        yield c
    get_settings.cache_clear()


@pytest.fixture
def stale_client(monkeypatch, tmp_path):
    app = make_client(monkeypatch, tmp_path, DEMO_LAG_HOURS="8")
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
