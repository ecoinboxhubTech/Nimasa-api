"""Source adapters: the only code that knows a provider's format. They return neutral RawRecords.
Adapters never write to the database and never validate; ingestion.py does that (SRS 9.2)."""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from app.config import Settings
from app.models import Dataset, Location

HOUR = timedelta(hours=1)
PAST_HOURS = 168
FORECAST_HOURS = 72


@dataclass
class RawRecord:
    dataset_key: str
    location_id: str
    observed_at: datetime
    value: float | None
    unit: str
    kind: str  # observation | forecast | model_estimate
    raw_reference: str | None = None


class AdapterError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


class SourceAdapter(Protocol):
    def fetch(self, locations: list[Location], datasets: list[Dataset], now: datetime) -> list[RawRecord]: ...


# --------------------------------------------------------------------------------------
# Demo adapter: synthetic, deterministic values. Mirrors the demo generator in the front-end.
# --------------------------------------------------------------------------------------
_BASE = {
    "badagry": (1.35, 15.0, 27.9),
    "apapa": (1.0, 13.0, 28.2),
    "bar-beach": (1.5, 16.0, 28.0),
    "lekki": (1.75, 17.0, 28.1),
    "eleko": (1.6, 18.0, 27.8),
}


def _hash(s: str) -> int:
    h = 0
    for c in s:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return h


def _rnd(seed: int, i: int) -> float:
    x = math.sin(seed * 12.9898 + i * 78.233) * 43758.5453
    return x - math.floor(x)


def _demo_value(ds: str, loc: str, t: datetime) -> float:
    h = t.timestamp() / 3600
    hour_utc = h % 24
    seed = _hash(loc + ds)
    n = _rnd(seed, math.floor(h)) - 0.5
    wave, wind, sst = _BASE.get(loc, (1.2, 15.0, 28.0))
    if ds == "wave_height":
        return max(0.3, wave + 0.35 * math.sin(h / 9.3 + (seed % 7)) + 0.28 * math.sin(h / 57) + 0.16 * n)
    if ds == "wind_speed":
        return max(2.0, wind + 6 * math.sin((hour_utc - 8) / 24 * 2 * math.pi) + 4 * math.sin(h / 41) + 5 * n)
    if ds == "visibility":  # km; periodic fog/haze episodes so reduced-visibility alerts can be exercised
        fog = 40 * max(0.0, math.sin(h / 61 + (seed % 3)) - 0.85)
        return min(30.0, max(0.3, 8.0 + 2.0 * math.sin(h / 23) + 1.5 * math.sin(h / 7) + 1.5 * n - fog))
    if ds == "current_speed":  # km/h
        return max(0.1, 2.2 + 1.6 * math.sin(h / 6.2 + (seed % 7)) + 1.2 * math.sin(h / 49) + 0.5 * n)
    return sst + 0.35 * math.sin((hour_utc - 9) / 24 * 2 * math.pi) + 0.2 * math.sin(h / 90) + 0.1 * n


class DemoAdapter:
    def __init__(self, settings: Settings):
        self.lag = timedelta(hours=settings.demo_lag_hours)

    def fetch(self, locations, datasets, now):
        last = now.replace(minute=0, second=0, microsecond=0) - self.lag
        out: list[RawRecord] = []
        for loc in locations:
            for ds in datasets:
                kind = "observation" if ds.key == "wind_speed" else "model_estimate"
                seed = _hash(loc.id + ds.key + "gap")
                for i in range(-(PAST_HOURS - 1), 1):
                    if _rnd(seed, i) < 0.025 and i != 0:  # some hours are simply absent: gaps stay gaps
                        continue
                    if ds.key == "wind_speed" and loc.id == "eleko" and -60 <= i <= -56:
                        continue
                    t = last + i * HOUR
                    out.append(RawRecord(ds.key, loc.id, t, round(_demo_value(ds.key, loc.id, t), 2), ds.unit, kind, "demo"))
                if ds.has_forecast:
                    for i in range(1, FORECAST_HOURS + 1):
                        t = last + i * HOUR
                        out.append(RawRecord(ds.key, loc.id, t, round(_demo_value(ds.key, loc.id, t), 2), ds.unit, "forecast", "demo"))
        return out


# --------------------------------------------------------------------------------------
# Open-Meteo adapter (CANDIDATE source). Disabled until SOURCE_LICENCE_REVIEWED=true.
# Values are MODEL data (never labelled "observation"). Not exercised by the automated tests, because
# they must not call external services; verify against the live API before relying on it.
# --------------------------------------------------------------------------------------
class OpenMeteoAdapter:
    MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
    WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.timeout = settings.ingest_timeout_seconds
        self.client = client

    def _get(self, client: httpx.Client, url: str, params: dict) -> dict:
        try:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.TimeoutException as e:
            raise AdapterError("UPSTREAM_TIMEOUT", "Provider request timed out") from e
        except httpx.HTTPStatusError as e:
            raise AdapterError("UPSTREAM_HTTP_ERROR", f"Provider returned HTTP {e.response.status_code}") from e
        except (httpx.HTTPError, ValueError) as e:
            raise AdapterError("UPSTREAM_BAD_RESPONSE", "Provider response could not be read") from e

    @staticmethod
    def _rows(payload: dict, var: str):
        hourly = payload.get("hourly") or {}
        times, values = hourly.get("time"), hourly.get(var)
        if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
            raise AdapterError("UPSTREAM_BAD_RESPONSE", f"Missing or malformed hourly series '{var}'")
        for t, v in zip(times, values):
            yield datetime.fromisoformat(t).replace(tzinfo=timezone.utc), v

    def fetch(self, locations, datasets, now):
        keys = {d.key: d for d in datasets}
        own = self.client or httpx.Client(timeout=self.timeout)
        out: list[RawRecord] = []
        try:
            for loc in locations:
                common = dict(latitude=loc.lat, longitude=loc.lng, timezone="UTC", past_days=7, forecast_days=3)
                marine = self._get(own, self.MARINE_URL, {**common, "hourly": "wave_height,sea_surface_temperature,ocean_current_velocity"})
                weather = self._get(own, self.WEATHER_URL, {**common, "hourly": "wind_speed_10m,visibility", "wind_speed_unit": "kmh"})
                # (dataset, payload, provider variable, divisor to convert the provider unit to the dataset unit)
                plan = [("wave_height", marine, "wave_height", 1), ("sst", marine, "sea_surface_temperature", 1),
                        ("current_speed", marine, "ocean_current_velocity", 1),  # km/h
                        ("wind_speed", weather, "wind_speed_10m", 1), ("visibility", weather, "visibility", 1000)]  # m -> km
                for key, payload, var, divisor in plan:
                    if key not in keys:
                        continue
                    for t, v in self._rows(payload, var):
                        if v is None:
                            continue  # missing stays missing
                        v = v / divisor
                        kind = "forecast" if t > now else "model_estimate"
                        if kind == "forecast" and not keys[key].has_forecast:
                            continue
                        out.append(RawRecord(key, loc.id, t, float(v), keys[key].unit, kind, f"open-meteo:{var}"))
        finally:
            if self.client is None:
                own.close()
        return out


def get_adapter(source_id: str, settings: Settings) -> SourceAdapter:
    if source_id == "demo":
        return DemoAdapter(settings)
    if source_id == "open_meteo":
        return OpenMeteoAdapter(settings)
    raise AdapterError("UNKNOWN_SOURCE", f"No adapter for source '{source_id}'")
