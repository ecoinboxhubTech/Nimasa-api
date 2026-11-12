"""API contracts. Timestamps are ISO 8601 UTC; every value carries unit, data type, source and freshness (FR-008)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DataType = Literal["observation", "forecast", "model_estimate", "derived"]
Freshness = Literal["fresh", "stale", "empty"]
AlertCategory = Literal["coastal", "ocean_navigation", "waterway_navigation"]
StaffRole = Literal["admin", "operator", "viewer"]


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
    zone: Literal["coastal", "waterway", "offshore"] = "coastal"


class DatasetOut(ORM):
    key: str
    label: str
    description: str = ""
    unit: str
    source_id: str
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
    category: AlertCategory
    location_id: str
    zone: Literal["coastal", "waterway", "offshore"]
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
    name: str | None = Field(default=None, max_length=200)
    threshold: float | None = None
    operator: Literal[">=", "<="] | None = None
    enabled: bool | None = None
    suppression_hours: float | None = Field(default=None, gt=0, le=168)
    validity_hours: float | None = Field(default=None, gt=0, le=168)
    review_status: str | None = Field(default=None, max_length=200)


class AlertRuleCreate(BaseModel):
    id: str = Field(min_length=2, max_length=50, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=2, max_length=200)
    dataset: str = Field(min_length=1, max_length=50)
    operator: Literal[">=", "<="]
    threshold: float
    unit: str = Field(min_length=1, max_length=20)
    category: AlertCategory = "coastal"
    zones: list[Literal["coastal", "waterway", "offshore"]] | None = None
    suppression_hours: float = Field(gt=0, le=168)
    validity_hours: float = Field(gt=0, le=168)
    enabled: bool = True
    review_status: str = Field(default="Illustrative threshold. Domain review pending", max_length=200)


class LocationCreate(BaseModel):
    id: str = Field(min_length=2, max_length=50, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=2, max_length=200)
    area: str = Field(min_length=2, max_length=200)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    zone: Literal["coastal", "waterway", "offshore"] = "coastal"


class LocationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    area: str | None = Field(default=None, min_length=2, max_length=200)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    zone: Literal["coastal", "waterway", "offshore"] | None = None


class SourcePatch(BaseModel):
    enabled: bool


# Staff sign in with an email address or a staff ID such as STF001. Stored lower-case.
EMAIL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@+-]{2,253}$"


class LoginIn(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=1, max_length=200)
    totp_code: str | None = Field(default=None, min_length=6, max_length=8)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=12, max_length=200)


class PasswordResetRequestIn(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)


class PasswordResetConfirmIn(BaseModel):
    token: str = Field(min_length=20, max_length=500)
    new_password: str = Field(min_length=12, max_length=200)


class MfaEnableIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class MfaDisableIn(BaseModel):
    password: str = Field(max_length=200)


class StaffCreate(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=12, max_length=200)
    role: StaffRole = "operator"


class StaffPatch(BaseModel):
    active: bool | None = None
    role: StaffRole | None = None
    password: str | None = Field(default=None, min_length=12, max_length=200)  # admin password reset


class DatasetPatch(BaseModel):
    """Freshness threshold per dataset (FR-024). Must be at least one source refresh interval, so fresh data is never flagged stale."""
    stale_after_minutes: int | None = Field(default=None, gt=0, le=60 * 24 * 14)
    source_id: str | None = Field(default=None, min_length=1, max_length=80)

# ---------------------------------------------------------------------------
# Developer sandbox, notifications, jobs, vessels, bathymetry and tides
# ---------------------------------------------------------------------------
class ApiKeyCreateIn(BaseModel):
    name: str = Field(default="Sandbox key", min_length=2, max_length=120)
    owner_email: str | None = Field(default=None, max_length=254)
    quota_per_day: int | None = Field(default=None, ge=10, le=1_000_000)
    environment: Literal["test", "production"] = "test"


class ApiKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    owner_email: str | None = None
    environment: Literal["test", "production"] = "test"
    active: bool
    scopes: str
    quota_per_day: int
    rate_limit_per_minute: int
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreatedOut(ApiKeyOut):
    key: str
    warning: str = "Store this key now. It is shown once and cannot be recovered."


class ApiUsageOut(BaseModel):
    api_key_id: int
    day: str
    requests: int
    quota: int
    remaining: int
    last_path: str | None = None


class NotificationTargetIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    channel: Literal["email", "sms", "webhook"]
    address: str = Field(min_length=3, max_length=500)
    event_types: str = Field(default="alert.created", max_length=200)
    active: bool = True


class NotificationTargetOut(ORM):
    id: int
    name: str
    channel: str
    address: str
    event_types: str
    active: bool
    created_at: datetime


class NotificationOut(ORM):
    id: int
    target_id: int | None = None
    event_type: str
    channel: str
    destination: str
    subject: str
    body: str
    status: str
    attempts: int
    created_at: datetime
    sent_at: datetime | None = None
    error: str | None = None


class JobCreateIn(BaseModel):
    kind: Literal["ingestion", "notification_dispatch", "retention", "quality_scan"]
    payload: dict | None = None


class JobOut(ORM):
    id: int
    kind: str
    status: str
    payload: dict | None = None
    scheduled_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempts: int
    error: str | None = None
    created_at: datetime


class VesselOut(ORM):
    mmsi: str
    name: str
    flag: str
    vessel_type: str
    call_sign: str | None = None
    active: bool


class VesselPositionOut(BaseModel):
    mmsi: str
    observed_at: datetime
    lat: float
    lng: float
    sog: float
    cog: float
    heading: float | None = None
    nav_status: str
    source: str


class VesselTrackOut(BaseModel):
    vessel: VesselOut
    points: list[VesselPositionOut]


class BathymetryOut(BaseModel):
    lat: float
    lng: float
    depth_m: float
    survey_name: str
    source: str


class TidePointOut(BaseModel):
    time: datetime
    height_m: float
    type: Literal["prediction"]
    source: str


class TideOut(BaseModel):
    location_id: str
    location: str
    datum: str = "Chart datum (demo harmonic model)"
    source: str = "harmonic-demo"
    simulated: bool = True
    points: list[TidePointOut]
    high_low: list[dict]


class QualityOut(BaseModel):
    generated_at: datetime
    locations: int
    datasets: int
    observations: int
    rejected_last_7d: int
    active_alerts: int
    stale_feeds: int
    empty_feeds: int
    gap_count_48h: int
    completeness_pct: float
    notes: list[str]

class ApiKeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    active: bool | None = None
    quota_per_day: int | None = Field(default=None, ge=10, le=1_000_000)


class NotificationTargetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    address: str | None = Field(default=None, min_length=3, max_length=500)
    event_types: str | None = Field(default=None, max_length=200)
    active: bool | None = None

class VesselLiveOut(VesselOut):
    lat: float | None = None
    lng: float | None = None
    sog: float | None = None
    cog: float | None = None
    observed_at: datetime | None = None
    nav_status: str | None = None
