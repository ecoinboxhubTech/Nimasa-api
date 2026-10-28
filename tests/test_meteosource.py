"""Meteosource adapter tests. Pure unit tests with a mocked transport: no database, no network, no real key."""
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.services.adapters import AdapterError, MeteosourceAdapter, get_adapter

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
LOC = SimpleNamespace(id="lekki", lat=6.43, lng=3.54)
WIND = SimpleNamespace(key="wind_speed", unit="km/h", has_forecast=True)
WAVES = SimpleNamespace(key="wave_height", unit="m", has_forecast=True)


def settings(key="k-test", tier="free"):
    return SimpleNamespace(meteosource_api_key=key, meteosource_tier=tier, ingest_timeout_seconds=5)


def adapter(handler, **kw):
    return MeteosourceAdapter(settings(**kw), client=httpx.Client(transport=httpx.MockTransport(handler)))


def ok(data):
    return lambda request: httpx.Response(200, json={"hourly": {"data": data}})


def test_maps_wind_to_kmh_with_utc_times_and_forecast_label():
    a = adapter(ok([{"date": "2026-10-28T11:00:00", "wind": {"speed": 5.0}}, {"date": "2026-10-28T13:00:00", "wind": {"speed": 10.0}}]))
    rows = a.fetch([LOC], [WIND, WAVES], NOW)
    assert [(r.dataset_key, r.value, r.kind) for r in rows] == [("wind_speed", 18.0, "model_estimate"), ("wind_speed", 36.0, "forecast")]
    assert all(r.observed_at.tzinfo is not None and r.unit == "km/h" and r.raw_reference == "meteosource:wind.speed" for r in rows)


def test_unmapped_marine_datasets_stay_unavailable():
    rows = adapter(ok([{"date": "2026-10-28T11:00:00", "wind": {"speed": 5.0}}])).fetch([LOC], [WAVES], NOW)
    assert rows == []


def test_missing_values_are_skipped_not_invented():
    rows = adapter(ok([{"date": "2026-10-28T11:00:00", "wind": {}}, {"date": "2026-10-28T10:00:00"}])).fetch([LOC], [WIND], NOW)
    assert rows == []


def test_request_uses_tier_utc_and_hourly_section():
    seen = {}

    def handler(request):
        seen["url"] = request.url
        return httpx.Response(200, json={"hourly": {"data": []}})

    adapter(handler, tier="flexi").fetch([LOC], [WIND], NOW)
    assert seen["url"].path == "/api/v1/flexi/point" and seen["url"].params["timezone"] == "UTC"
    assert seen["url"].params["sections"] == "hourly"


@pytest.mark.parametrize("status,category,retryable", [
    (401, "UPSTREAM_AUTH_ERROR", False), (403, "UPSTREAM_AUTH_ERROR", False), (402, "UPSTREAM_PLAN_OR_QUOTA", False),
    (429, "UPSTREAM_RATE_LIMITED", True), (500, "UPSTREAM_HTTP_ERROR", True)])
def test_http_errors_are_categorised_and_only_safe_ones_retry(status, category, retryable):
    with pytest.raises(AdapterError) as e:
        adapter(lambda r: httpx.Response(status)).fetch([LOC], [WIND], NOW)
    assert e.value.category == category and e.value.retryable is retryable


def test_timeout_and_network_failure_hide_the_key():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def down(request):
        raise httpx.ConnectError("boom k-test", request=request)

    with pytest.raises(AdapterError) as e:
        adapter(timeout).fetch([LOC], [WIND], NOW)
    assert e.value.category == "UPSTREAM_TIMEOUT"
    with pytest.raises(AdapterError) as e:
        adapter(down).fetch([LOC], [WIND], NOW)
    assert e.value.category == "UPSTREAM_NETWORK_ERROR" and "k-test" not in str(e.value)


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json"), httpx.Response(200, json={}), httpx.Response(200, json={"hourly": {"data": "x"}}),
    httpx.Response(200, json={"hourly": {"data": [{"wind": {"speed": 1}}]}}),
    httpx.Response(200, json={"hourly": {"data": [{"date": "2026-10-28T11:00:00", "wind": {"speed": "fast"}}]}})])
def test_malformed_payloads_are_rejected(response):
    with pytest.raises(AdapterError) as e:
        adapter(lambda r: response).fetch([LOC], [WIND], NOW)
    assert e.value.category == "UPSTREAM_BAD_RESPONSE"


def test_missing_key_bad_tier_and_bad_coordinates_fail_fast():
    with pytest.raises(AdapterError) as e:
        adapter(ok([]), key="").fetch([LOC], [WIND], NOW)
    assert e.value.category == "AUTH_NOT_CONFIGURED" and not e.value.retryable
    with pytest.raises(AdapterError) as e:
        MeteosourceAdapter(settings(tier="free/../x"))
    assert e.value.category == "CONFIG_ERROR"
    with pytest.raises(AdapterError) as e:
        adapter(ok([])).fetch([SimpleNamespace(id="x", lat=91, lng=0)], [WIND], NOW)
    assert e.value.category == "CONFIG_ERROR"


def test_get_adapter_knows_meteosource():
    assert isinstance(get_adapter("meteosource", settings()), MeteosourceAdapter)
