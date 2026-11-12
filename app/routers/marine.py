"""Public marine-data endpoints for the BlueLens sandbox: AIS replay, bathymetry, tides and quality."""
import math
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, utcnow
from app.errors import ApiError
from app.models import BathymetryPoint, Location, Vessel, VesselPosition
from app.schemas import (BathymetryOut, QualityOut, TideOut, TidePointOut, VesselLiveOut, VesselOut,
                         VesselPositionOut, VesselTrackOut)
from app.security import developer_key
from app.services import quality

router = APIRouter(prefix="/api/v1", tags=["marine-sandbox"])


def _bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    try:
        min_lng, min_lat, max_lng, max_lat = (float(x) for x in value.split(","))
    except ValueError:
        raise ApiError(422, "VALIDATION_ERROR", "bbox must be minLng,minLat,maxLng,maxLat")
    if min_lng >= max_lng or min_lat >= max_lat:
        raise ApiError(422, "VALIDATION_ERROR", "bbox minimums must be less than maximums")
    return min_lng, min_lat, max_lng, max_lat


@router.get("/vessels", response_model=list[VesselLiveOut], tags=["vessels"], dependencies=[Depends(developer_key)])
def vessels(bbox: str | None = Query(None, description="minLng,minLat,maxLng,maxLat"), since: datetime | None = None,
            limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)):
    """Latest AIS position per vessel. The bundled source is an explicitly labelled demo replay."""
    box = _bbox(bbox)
    cutoff = since or utcnow() - timedelta(hours=24)
    out = []
    for vessel in db.scalars(select(Vessel).where(Vessel.active.is_(True)).limit(limit)).all():
        pos = db.scalars(select(VesselPosition).where(VesselPosition.mmsi == vessel.mmsi,
                                                      VesselPosition.observed_at >= cutoff)
                         .order_by(VesselPosition.observed_at.desc()).limit(1)).first()
        if pos is None:
            continue
        if box and not (box[0] <= pos.lng <= box[2] and box[1] <= pos.lat <= box[3]):
            continue
        out.append({"mmsi": vessel.mmsi, "name": vessel.name, "flag": vessel.flag, "vessel_type": vessel.vessel_type,
                    "call_sign": vessel.call_sign, "active": vessel.active, "lat": pos.lat, "lng": pos.lng,
                    "sog": pos.sog, "cog": pos.cog, "observed_at": pos.observed_at, "nav_status": pos.nav_status})
    return out


@router.get("/vessels/{mmsi}/track", response_model=VesselTrackOut, tags=["vessels"], dependencies=[Depends(developer_key)])
def vessel_track(mmsi: str, hours: int = Query(48, ge=1, le=14 * 24), db: Session = Depends(get_db)):
    vessel = db.get(Vessel, mmsi)
    if vessel is None:
        raise ApiError(404, "VESSEL_NOT_FOUND", f"Vessel '{mmsi}' is not in the replay catalogue.")
    rows = db.scalars(select(VesselPosition).where(VesselPosition.mmsi == mmsi,
                                                    VesselPosition.observed_at >= utcnow() - timedelta(hours=hours))
                      .order_by(VesselPosition.observed_at)).all()
    return {"vessel": vessel, "points": rows}


@router.get("/bathymetry", response_model=list[BathymetryOut], tags=["bathymetry"], dependencies=[Depends(developer_key)])
def bathymetry(bbox: str | None = Query(None, description="minLng,minLat,maxLng,maxLat"),
               limit: int = Query(500, ge=1, le=5000), db: Session = Depends(get_db)):
    box = _bbox(bbox)
    q = select(BathymetryPoint).order_by(BathymetryPoint.lat, BathymetryPoint.lng).limit(limit)
    if box:
        q = q.where(BathymetryPoint.lng >= box[0], BathymetryPoint.lat >= box[1],
                    BathymetryPoint.lng <= box[2], BathymetryPoint.lat <= box[3])
    return db.scalars(q).all()


@router.get("/tides", response_model=TideOut, tags=["tides"], dependencies=[Depends(developer_key)])
def tides(location_id: str = Query("lekki", max_length=50), hours: int = Query(48, ge=1, le=14 * 24),
          db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Harmonic demo tide curve. Clearly marked simulated: no operational tide provider is configured."""
    loc = db.get(Location, location_id)
    if loc is None:
        raise ApiError(404, "LOCATION_NOT_FOUND", f"Location '{location_id}' is not supported.")
    start = utcnow().replace(minute=0, second=0, microsecond=0)
    phase = (abs(hash(location_id)) % 360) / 57.2958
    points, high_low = [], []
    prev = None
    for i in range(hours + 1):
        t = start + timedelta(hours=i)
        h = t.timestamp() / 3600
        height = round(0.55 * math.sin(2 * math.pi * h / 12.42 + phase)
                       + 0.18 * math.sin(2 * math.pi * h / 24 + phase / 2) + 0.08 * math.sin(h / 7), 2)
        points.append(TidePointOut(time=t, height_m=height, type="prediction", source=settings.tide_provider))
        if prev is not None and i > 1:
            p_height = points[-2].height_m
            if prev[0] > p_height and prev[0] > height:
                high_low.append({"time": prev[1], "height_m": prev[0], "type": "high"})
            elif prev[0] < p_height and prev[0] < height:
                high_low.append({"time": prev[1], "height_m": prev[0], "type": "low"})
        prev = (height, t)
    return {"location_id": loc.id, "location": loc.name, "simulated": True, "source": settings.tide_provider,
            "points": points, "high_low": high_low[-8:]}


@router.get("/data-quality", response_model=QualityOut, tags=["quality"], dependencies=[Depends(developer_key)])
def data_quality(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return quality.summary(db, settings)
