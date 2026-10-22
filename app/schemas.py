"""API contracts. Timestamps are ISO 8601 UTC; every value carries unit, data type, source and freshness (FR-008)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DataType = Literal["observation", "forecast", "model_estimate", "derived"]
Freshness = Literal["fresh", "stale", "empty"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SourceRef(BaseModel):
    id: str
    name: str
    attribution: str


class SourceOut(ORM):
    id: str
    name: str
    provider: str
    licence_status: str
    licence_url: str | None = None
    attribution: str
    documentation_url: str | None = None
    refresh_interval_minutes: int
    enabled: bool
    note: str


class LocationOut(ORM):
    id: str
    name: str
    area: str
    lat: float
    lng: float


class DatasetOut(ORM):
    key: str
    label: str
    unit: str
    refresh_minutes: int
    stale_after_minutes: int
    has_forecast: bool


class Indicator(BaseModel):
    dataset: str
    label: str
    unit: str
    value: float | None
    data_type: DataType | None
    observed_at: datetime | None
    age_minutes: int | None
    status: Freshness
    source: SourceRef | None


class SummaryOut(BaseModel):
    location: LocationOut
    generated_at: datetime
    timezone_note: str = "All timestamps are UTC (ISO 8601). Display in WAT = UTC+1."
    indicators: list[Indicator]
    active_alerts: int
    warnings: list[str]


class PointOut(BaseModel):
    time: datetime
    value: float
    data_type: DataType


class GapOut(BaseModel):
    start: datetime
    end: datetime


class SeriesOut(BaseModel):
    location_id: str
    dataset: str
    label: str
    unit: str
    status: Freshness
    source_timestamp: datetime | None
    expected_interval_minutes: int
    source: SourceRef | None
    points: list[PointOut]
    gaps: list[GapOut]
    note: str = "Missing values are shown as gaps. Nothing is interpolated or estimated."


class AlertOut(BaseModel):
    id: int
    rule_id: str
    rule_name: str
    location_id: str
    dataset: str
    condition: str
    value: float
    threshold: float
    unit: str
    data_type: DataType
    source_id: str
    source_timestamp: datetime
    issued_at: datetime
    valid_until: datetime
    status: Literal["active", "expired", "suppressed_stale"]
    message: str


class FeedbackIn(BaseModel):
    found_information: Literal["yes_easily", "yes_with_difficulty", "no"] | None = None
    source_clarity: Literal["yes", "partly", "no"] | None = None
    comments: str | None = Field(default=None, max_length=1000)


class InsightQuery(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    location_id: str | None = Field(default=None, max_length=50)


class Citation(BaseModel):
    id: str
    location: str
    dataset: str
    value: float | None
    unit: str
    data_type: str
    observed_at: datetime | None
    freshness: str
    source: str


class InsightOut(BaseModel):
    answer_type: Literal["grounded", "refused_safety", "refused_unsupported", "no_evidence", "ungrounded_blocked"]
    answer: str
    citations: list[Citation]
    limitations: list[str]
    model: str | None
    disclaimer: str


class AlertRulePatch(BaseModel):
    threshold: float | None = None
    enabled: bool | None = None
    suppression_hours: float | None = Field(default=None, gt=0, le=168)
    validity_hours: float | None = Field(default=None, gt=0, le=168)
    review_status: str | None = Field(default=None, max_length=200)


class SourcePatch(BaseModel):
    enabled: bool


EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class LoginIn(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=1, max_length=200)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=12, max_length=200)


class StaffCreate(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=12, max_length=200)


class StaffPatch(BaseModel):
    active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=200)  # admin password reset


class DatasetPatch(BaseModel):
    """Freshness threshold per dataset (FR-024). Must be at least one source refresh interval, so fresh data is never flagged stale."""
    stale_after_minutes: int = Field(gt=0, le=60 * 24 * 14)
