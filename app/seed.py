"""Initial reference data. Only inserts rows that do not exist, so admin changes are never overwritten."""
import math
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utcnow
from app.models import (AdminUser, AlertRule, BathymetryPoint, Dataset, DataSource, Location, Vessel,
                        VesselPosition)
from app.passwords import MIN_PASSWORD_LENGTH, hash_password

SOURCES = [
    dict(
        id="demo", name="Orizon Nimasa sample generator", provider="Orizon Nimasa (internal, demo only)",
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
    dict(
        id="meteosource", name="Meteosource Weather API", provider="Meteosource",
        licence_status="not_reviewed", licence_url="https://www.meteosource.com/terms-of-service",
        attribution="Weather data by Meteosource (to be confirmed)", documentation_url="https://www.meteosource.com/documentation",
        refresh_interval_minutes=60,
        note="CANDIDATE source (model data). Only wind speed is mapped; marine variables are not assumed. Verify plan, "
             "marine fields, quota and licence, set METEOSOURCE_API_KEY, then SOURCE_LICENCE_REVIEWED=true.",
    ),
    dict(
        id="demo-ais", name="Demo AIS replay", provider="Orizon Nimasa (internal, demo only)",
        licence_status="not_applicable", attribution="Synthetic vessel tracks for interface demonstration",
        refresh_interval_minutes=5, note="Fictional AIS replay. A live AIS provider must be configured before operational use.",
    ),
    dict(
        id="demo-bathymetry", name="Demo Lagos shelf bathymetry", provider="Orizon Nimasa (internal, demo only)",
        licence_status="not_applicable", attribution="Synthetic depth grid for interface demonstration",
        refresh_interval_minutes=1440, note="Fictional depth grid. Replace with an approved GEBCO/NIMASA survey before navigation use.",
    ),
]

# Illustrative marine locations on the Lagos coast: placeholder pilot area until the product owner confirms one (PRD 18).
LOCATIONS = [
    ("badagry", "Badagry", "Western Lagos coast", 6.38, 2.88, "coastal"),
    ("apapa", "Lagos Harbour (Apapa)", "Lagos Harbour approaches", 6.40, 3.34, "coastal"),
    ("bar-beach", "Bar Beach (Victoria Island)", "Central Lagos coast", 6.41, 3.41, "coastal"),
    ("lekki", "Lekki", "Eastern Lagos coast", 6.43, 3.54, "coastal"),
    ("eleko", "Eleko", "Ibeju-Lekki coast", 6.43, 3.80, "coastal"),
    ("lagos-lagoon", "Lagos Lagoon", "Inland waterway (lagoon)", 6.47, 3.45, "waterway"),
    ("badagry-creek", "Badagry Creek", "Inland waterway (creek)", 6.43, 2.95, "waterway"),
    ("lagos-offshore", "Lagos Offshore Approach", "Offshore shipping approaches", 6.20, 3.35, "offshore"),
]

DATASETS = [
    dict(key="wave_height", label="Wave height", description="Significant wave height", unit="m",
         refresh_minutes=60, stale_after_minutes=180, has_forecast=True, plausible_min=0.0, plausible_max=30.0),
    dict(key="wind_speed", label="Wind speed", description="Mean 10 m wind speed", unit="km/h",
         refresh_minutes=60, stale_after_minutes=120, has_forecast=True, plausible_min=0.0, plausible_max=300.0),
    dict(key="sst", label="Sea surface temperature", description="Sea surface temperature", unit="°C",
         refresh_minutes=360, stale_after_minutes=720, has_forecast=False, plausible_min=0.0, plausible_max=40.0),
    dict(key="visibility", label="Visibility", description="Horizontal visibility", unit="km",
         refresh_minutes=60, stale_after_minutes=180, has_forecast=True, plausible_min=0.0, plausible_max=100.0),
    dict(key="current_speed", label="Current speed", description="Ocean or water current speed", unit="km/h",
         refresh_minutes=60, stale_after_minutes=180, has_forecast=True, plausible_min=0.0, plausible_max=30.0),
]

# Illustrative thresholds only. Real thresholds need review by a qualified marine/meteorological specialist (PRD 8, 18).
RULES = [
    dict(id="wave-high", name="High wave height advisory", dataset_key="wave_height", operator=">=", threshold=2.0,
         unit="m", suppression_hours=6, validity_hours=6, category="coastal", zones=["coastal", "offshore"]),
    dict(id="wind-strong", name="Strong wind advisory", dataset_key="wind_speed", operator=">=", threshold=28.0,
         unit="km/h", suppression_hours=3, validity_hours=3, category="coastal", zones=None),
    dict(id="nav-ocean-waves", name="Ocean navigation: rough sea state", dataset_key="wave_height", operator=">=",
         threshold=2.5, unit="m", suppression_hours=6, validity_hours=6, category="ocean_navigation",
         zones=["coastal", "offshore"]),
    dict(id="nav-ocean-wind", name="Ocean navigation: strong wind", dataset_key="wind_speed", operator=">=",
         threshold=40.0, unit="km/h", suppression_hours=3, validity_hours=3, category="ocean_navigation",
         zones=["coastal", "offshore"]),
    dict(id="nav-ocean-visibility", name="Ocean navigation: reduced visibility", dataset_key="visibility", operator="<=",
         threshold=2.0, unit="km", suppression_hours=3, validity_hours=3, category="ocean_navigation",
         zones=["coastal", "offshore"]),
    dict(id="nav-ocean-current", name="Ocean navigation: strong current", dataset_key="current_speed", operator=">=",
         threshold=5.5, unit="km/h", suppression_hours=6, validity_hours=6, category="ocean_navigation",
         zones=["coastal", "offshore"]),
    dict(id="nav-waterway-wind", name="Waterway navigation: small-craft wind", dataset_key="wind_speed", operator=">=",
         threshold=22.0, unit="km/h", suppression_hours=3, validity_hours=3, category="waterway_navigation",
         zones=["waterway"]),
    dict(id="nav-waterway-visibility", name="Waterway navigation: reduced visibility", dataset_key="visibility",
         operator="<=", threshold=1.0, unit="km", suppression_hours=3, validity_hours=3,
         category="waterway_navigation", zones=["waterway"]),
    dict(id="nav-waterway-current", name="Waterway navigation: strong current", dataset_key="current_speed", operator=">=",
         threshold=4.0, unit="km/h", suppression_hours=6, validity_hours=6, category="waterway_navigation",
         zones=["waterway"]),
]

VESSELS = [
    ("657123450", "MV Lagos Star", "Nigeria", "cargo", "5NLS1"),
    ("657123451", "MT Niger Spirit", "Nigeria", "tanker", "5NNS2"),
    ("657123452", "FV Atlantic Grace", "Nigeria", "fishing", "5NAG3"),
    ("657123453", "MV Apapa Trader", "Nigeria", "cargo", "5NAT4"),
    ("657123454", "PB Lekki Watch", "Nigeria", "patrol", "5NLW5"),
    ("657123455", "SV Gulf Horizon", "Panama", "sailing", "3FGH6"),
]


def _seed_vessels(db: Session, now) -> None:
    for i, (mmsi, name, flag, kind, call_sign) in enumerate(VESSELS):
        if db.get(Vessel, mmsi):
            continue
        db.add(Vessel(mmsi=mmsi, name=name, flag=flag, vessel_type=kind, call_sign=call_sign))
        for step in range(24):
            t = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23 - step)
            phase = step / 4 + i
            lat = 6.20 + 0.035 * i + 0.10 * math.sin(phase)
            lng = 3.15 + 0.08 * i + 0.18 * math.cos(phase * 0.8)
            db.add(VesselPosition(
                mmsi=mmsi, observed_at=t, lat=round(lat, 5), lng=round(lng, 5),
                sog=round(7 + 3 * math.sin(phase), 1), cog=round((phase * 40) % 360, 1),
                heading=round((phase * 40) % 360, 1), nav_status="under_way", source="demo-ais"))


def _seed_bathymetry(db: Session) -> None:
    if db.scalars(select(BathymetryPoint.id).limit(1)).first():
        return
    for lat_i in range(9):
        for lng_i in range(9):
            lat = 6.10 + lat_i * 0.065
            lng = 2.80 + lng_i * 0.13
            offshore = max(0.0, 6.55 - lat) * 55
            depth = 4 + offshore + 7 * math.sin(lng_i * 0.9 + lat_i * 0.6)
            db.add(BathymetryPoint(lat=round(lat, 5), lng=round(lng, 5), depth_m=round(max(1.5, depth), 1)))


def seed(db: Session, settings: Settings) -> None:
    for s in SOURCES:
        if not db.get(DataSource, s["id"]):
            db.add(DataSource(enabled=(s["id"] == settings.active_source), **s))
    for lid, name, area, lat, lng, zone in LOCATIONS:
        existing = db.get(Location, lid)
        if existing is None:
            db.add(Location(id=lid, name=name, area=area, lat=lat, lng=lng, zone_value=zone))
        else:
            existing.zone_value = zone
    db.flush()
    for d in DATASETS:
        if db.get(Dataset, d["key"]) is None:
            db.add(Dataset(source_id=settings.active_source, **d))
    db.flush()  # datasets must exist before the rules that reference them (PostgreSQL enforces foreign keys)
    for r in RULES:
        if not db.get(AlertRule, r["id"]):
            db.add(AlertRule(**r))
    if settings.admin_bootstrap_email and settings.admin_bootstrap_password \
            and db.scalars(select(AdminUser.id).limit(1)).first() is None:
        if len(settings.admin_bootstrap_password) < MIN_PASSWORD_LENGTH:
            raise RuntimeError(f"ADMIN_BOOTSTRAP_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters.")
        db.add(AdminUser(email=settings.admin_bootstrap_email.strip().lower(),
                         password_hash=hash_password(settings.admin_bootstrap_password),
                         role=settings.admin_bootstrap_role))
    _seed_vessels(db, utcnow())
    _seed_bathymetry(db)
    db.commit()
