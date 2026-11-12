"""All configuration comes from environment variables / .env (see .env.example). No secrets in code."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173"
    database_url: str  # PostgreSQL only (Aiven Service URI). Required: there is no local fallback.

    admin_api_key: str = ""  # optional service key for automation; staff sign in with accounts instead
    admin_bootstrap_email: str = ""  # creates the first administrator when none exist
    admin_bootstrap_password: str = ""
    admin_bootstrap_role: str = "admin"
    admin_session_hours: int = 8
    admin_max_failed_logins: int = 5
    admin_lockout_minutes: int = 15
    login_rate_limit_per_minute: int = 10
    password_reset_hours: int = 1
    public_base_url: str = "http://localhost:5173"

    developer_key_quota_per_day: int = 1000
    developer_key_rate_per_minute: int = 60
    developer_keys_per_hour: int = 5

    jobs_enabled: bool = True
    job_poll_seconds: int = 30

    notifications_enabled: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "alerts@orizon-nimasa.local"
    smtp_use_tls: bool = True
    webhook_timeout_seconds: float = 10.0

    ais_provider: str = "demo"  # provider-ready id; live AIS credentials are not bundled
    bathymetry_provider: str = "demo"
    tide_provider: str = "harmonic-demo"

    active_source: str = "demo"
    source_licence_reviewed: bool = False
    scheduler_enabled: bool = True
    ingest_interval_minutes: int = 60
    ingest_max_retries: int = 3
    ingest_backoff_seconds: float = 2.0
    ingest_timeout_seconds: float = 20.0
    retention_days: int = 90
    demo_lag_hours: int = 0

    meteosource_api_key: str = ""  # backend only; never sent to the browser or logged
    meteosource_tier: str = "free"  # the endpoint tier of your Meteosource plan, e.g. free

    ai_enabled: bool = True
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: float = 20.0

    rate_limit_per_minute: int = 30
    insights_rate_limit_per_minute: int = 6

    max_series_days: int = 14  # bounded time ranges (SRS 12.2)
    alert_lookback_hours: int = 72

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
