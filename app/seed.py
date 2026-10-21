"""Initial reference data. Only inserts rows that do not exist, so admin changes are never overwritten."""
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AlertRule, Dataset, DataSource, Location

SOURCES = [
    dict(
        id="demo", name="BlueIntel sample generator", provider="BlueIntel (internal, demo only)",
        licence_status="not_applicable", attribution="Synthetic sample data for interface demonstration",
        refresh_interval_minutes=60,
        note="Produces plausible but FICTIONAL values. Replace with an approved real source before any pilot.",
    ),
    dict(
        id="open_meteo", name="Open-Meteo Marine & Weather API", provider="Open-Meteo",
        licence_status="not_reviewed", licence_url="https://open-meteo.com/en/terms",
        attribution="Weather data by Open-Meteo.com (to be confirmed)", documentation_url="https://open-meteo.com/en/docs/marine-weather-api",
        refresh_interval_minutes=60,
        note="CANDIDATE source (model data). Check licence, commercial-use terms, limits and coverage, then set SOURCE_LICENCE_REVIEWED=true.",
    ),
]

# Illustrative marine locations on the Lagos coast: placeholder pilot area until the product owner confirms one (PRD 18).
LOCATIONS = [
    ("badagry", "Badagry", "Western Lagos coast", 6.38, 2.88),
    ("apapa", "Lagos Harbour (Apapa)", "Lagos Harbour approaches", 6.40, 3.34),
    ("bar-beach", "Bar Beach (Victoria Island)", "Central Lagos coast", 6.41, 3.41),
    ("lekki", "Lekki", "Eastern Lagos coast", 6.43, 3.54),
    ("eleko", "Eleko", "Ibeju-Lekki coast", 6.43, 3.80),
]

DATASETS = [
    dict(key="wave_height", label="Wave height", description="Significant wave height", unit="m",
         refresh_minutes=60, stale_after_minutes=180, has_forecast=True, plausible_min=0.0, plausible_max=30.0),
    dict(key="wind_speed", label="Wind speed", description="Mean 10 m wind speed", unit="km/h",
         refresh_minutes=60, stale_after_minutes=120, has_forecast=True, plausible_min=0.0, plausible_max=300.0),
    dict(key="sst", label="Sea surface temperature", description="Sea surface temperature", unit="°C",
         refresh_minutes=360, stale_after_minutes=720, has_forecast=False, plausible_min=0.0, plausible_max=40.0),
]

# Illustrative thresholds only. Real thresholds need review by a qualified marine/meteorological specialist (PRD 8, 18).
RULES = [
    dict(id="wave-high", name="High wave height advisory", dataset_key="wave_height", operator=">=", threshold=2.0,
         unit="m", suppression_hours=6, validity_hours=6),
    dict(id="wind-strong", name="Strong wind advisory", dataset_key="wind_speed", operator=">=", threshold=28.0,
         unit="km/h", suppression_hours=3, validity_hours=3),
]


def seed(db: Session, settings: Settings) -> None:
    for s in SOURCES:
        if not db.get(DataSource, s["id"]):
            db.add(DataSource(enabled=(s["id"] == settings.active_source), **s))
    for lid, name, area, lat, lng in LOCATIONS:
        if not db.get(Location, lid):
            db.add(Location(id=lid, name=name, area=area, lat=lat, lng=lng))
    db.flush()
    for d in DATASETS:
        existing = db.get(Dataset, d["key"])
        if existing:
            existing.source_id = settings.active_source  # active source is controlled by configuration (FR-024)
        else:
            db.add(Dataset(source_id=settings.active_source, **d))
    for r in RULES:
        if not db.get(AlertRule, r["id"]):
            db.add(AlertRule(**r))
    db.commit()
