"""ORM models: the conceptual entities from SRS section 11.1 (plus Location, Feedback, AuditLog, InsightLog)."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow


class DataSource(Base):
    __tablename__ = "data_sources"
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(200))
    licence_status: Mapped[str] = mapped_column(String(100))  # SRS: store source terms + attribution
    licence_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attribution: Mapped[str] = mapped_column(String(500))
    documentation_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    refresh_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")


class Location(Base):
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    area: Mapped[str] = mapped_column(String(200))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)


class Dataset(Base):
    """An indicator (e.g. wave height). Freshness limit is per dataset, based on its real cadence (SRS 11.2)."""

    __tablename__ = "datasets"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"))
    refresh_minutes: Mapped[int] = mapped_column(Integer)
    stale_after_minutes: Mapped[int] = mapped_column(Integer)
    has_forecast: Mapped[bool] = mapped_column(Boolean, default=False)
    plausible_min: Mapped[float] = mapped_column(Float)
    plausible_max: Mapped[float] = mapped_column(Float)
    source: Mapped[DataSource] = relationship()


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_uid: Mapped[str] = mapped_column(String(36), unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # running | success | partial | failed
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    error_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # never contains secrets


class Observation(Base):
    """One validated data point. Every record is traceable to a source via its ingestion run (SRS 11.2)."""

    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("dataset_key", "location_id", "observed_at", "kind", name="uq_observation"),
        Index("ix_obs_lookup", "dataset_key", "location_id", "observed_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_key: Mapped[str] = mapped_column(ForeignKey("datasets.key"))
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.id"))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20))  # observation | forecast | model_estimate | derived
    quality_status: Mapped[str] = mapped_column(String(20), default="valid")
    run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.id"))
    raw_reference: Mapped[str | None] = mapped_column(String(300), nullable=True)
    run: Mapped[IngestionRun] = relationship()


class AlertRule(Base):
    __tablename__ = "alert_rules"
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    dataset_key: Mapped[str] = mapped_column(ForeignKey("datasets.key"))
    operator: Mapped[str] = mapped_column(String(3))  # >= or <=
    threshold: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(20))
    suppression_hours: Mapped[float] = mapped_column(Float)
    validity_hours: Mapped[float] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    review_status: Mapped[str] = mapped_column(String(200), default="Illustrative threshold. Domain review pending")


class AlertEvent(Base):
    """Traceable to the rule and the exact observation that caused it (FR-013). Status is derived at read time."""

    __tablename__ = "alert_events"
    __table_args__ = (UniqueConstraint("rule_id", "location_id", "triggered_at", name="uq_alert_event"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(ForeignKey("alert_rules.id"))
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.id"))
    dataset_key: Mapped[str] = mapped_column(String(50))
    observation_id: Mapped[int | None] = mapped_column(ForeignKey("observations.id"), nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(UTCDateTime)  # source timestamp of the triggering value
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    valid_until: Mapped[datetime] = mapped_column(UTCDateTime)
    value: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    operator: Mapped[str] = mapped_column(String(3))
    unit: Mapped[str] = mapped_column(String(20))
    data_kind: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text)
    rule: Mapped[AlertRule] = relationship()


class AdminUser(Base):
    """Authorised staff. The public never has accounts (PRD 14.2); only administrators sign in."""

    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    user: Mapped[AdminUser] = relationship()


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    found_information: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source_clarity: Mapped[str | None] = mapped_column(String(30), nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)  # no names/contacts collected (data minimisation)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(200))
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class InsightLog(Base):
    """Model/version + retrieval metadata for evaluation. The question text itself is NOT stored (hash only)."""

    __tablename__ = "insight_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    outcome: Mapped[str] = mapped_column(String(40))
    location_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    question_hash: Mapped[str] = mapped_column(String(64))
    question_length: Mapped[int] = mapped_column(Integer)
