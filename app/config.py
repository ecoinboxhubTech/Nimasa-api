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
    admin_session_hours: int = 8
    admin_max_failed_logins: int = 5
    admin_lockout_minutes: int = 15
    login_rate_limit_per_minute: int = 10

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
