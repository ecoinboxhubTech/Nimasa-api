"""Application factory: wires config, DB, routers, error handling and the ingestion scheduler."""
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.config import Settings, get_settings
from app.errors import install_error_handlers
from app.models import Observation  # noqa: F401  (ensures models are registered)
from app.routers import admin, insights, public
from app.seed import seed
from app.services import ingestion

log = logging.getLogger("blueintel")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"time": self.formatTime(record), "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def _scheduled_ingest(settings: Settings) -> None:
    assert db.SessionLocal is not None
    with db.SessionLocal() as session:
        ingestion.run_all(session, settings)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.is_production and settings.admin_api_key and len(settings.admin_api_key) < 24:
        raise RuntimeError("ADMIN_API_KEY must be at least 24 characters in production.")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app import models  # noqa: F401
        engine = db.init_engine(settings.database_url)
        db.Base.metadata.create_all(engine)
        with db.SessionLocal() as session:
            seed(session, settings)
        scheduler = None
        if settings.scheduler_enabled:
            from apscheduler.schedulers.background import BackgroundScheduler
            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.add_job(_scheduled_ingest, "interval", minutes=settings.ingest_interval_minutes, args=[settings],
                              next_run_time=datetime.now(), max_instances=1, coalesce=True, id="ingest")
            scheduler.start()
            log.info("ingestion scheduler started (every %d min)", settings.ingest_interval_minutes)
        yield
        if scheduler:
            scheduler.shutdown(wait=False)
        engine.dispose()

    app = FastAPI(title="BlueIntel Nigeria API", version="0.1.0", lifespan=lifespan,
                  description="Ocean and coastal data API with source-grounded AI insights (PRD/SRS v1.0).")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_methods=["GET", "POST", "PATCH"],
                       allow_headers=["Content-Type", "X-API-Key", "Authorization"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        response.headers["X-Content-Type-Options"] = "nosniff"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"  # NFR-007
        response.headers["Cache-Control"] = "no-store" if "/admin" in request.url.path else "public, max-age=30"
        return response

    install_error_handlers(app)
    app.include_router(public.router)
    app.include_router(insights.router)
    app.include_router(admin.router)

    @app.get("/", include_in_schema=False)
    def root():
        return {"name": "BlueIntel Nigeria API", "docs": "/docs", "health": "/api/v1/health"}

    return app


app = create_app()
