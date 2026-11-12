"""Small in-place upgrades for databases created before a column existed.

`Base.metadata.create_all()` creates missing tables but never alters existing ones. These idempotent PostgreSQL
statements keep pilots upgradeable without a maintenance window. Alembic is the long-term migration path; this runs
only for additive, backwards-compatible changes.
"""
from sqlalchemy import text
from sqlalchemy.engine import Engine


STATEMENTS = [
    "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'admin'",
    "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS mfa_secret VARCHAR(64)",
    "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS environment VARCHAR(20) NOT NULL DEFAULT 'test'",
    "ALTER TABLE locations ADD COLUMN IF NOT EXISTS zone VARCHAR(20) NOT NULL DEFAULT 'coastal'",
    "UPDATE locations SET zone = 'waterway' WHERE id IN ('lagos-lagoon', 'badagry-creek')",
    "UPDATE locations SET zone = 'offshore' WHERE id = 'lagos-offshore'",
    "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS category VARCHAR(40) NOT NULL DEFAULT 'coastal'",
    "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS zones JSONB",
    "UPDATE alert_rules SET category = 'ocean_navigation' WHERE id LIKE 'nav-ocean-%'",
    "UPDATE alert_rules SET category = 'waterway_navigation' WHERE id LIKE 'nav-waterway-%'",
    "UPDATE alert_rules SET zones = '[\"coastal\", \"offshore\"]'::jsonb WHERE id IN ('wave-high', 'nav-ocean-waves', 'nav-ocean-wind', 'nav-ocean-visibility', 'nav-ocean-current')",
    "UPDATE alert_rules SET zones = '[\"waterway\"]'::jsonb WHERE id LIKE 'nav-waterway-%'",
]


def upgrade(engine: Engine) -> None:
    with engine.begin() as conn:
        for statement in STATEMENTS:
            conn.execute(text(statement))
