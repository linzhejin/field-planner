"""Weather lookups must be date-complete, traceable, and separate from field facts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v3 import FieldDraftV3
from nongshi.weather import OpenMeteoService, WeatherServiceError

START = date(2026, 10, 19)
LOCATION_RESPONSE = {
    "results": [
        {
            "id": 1,
            "name": "武昌区",
            "latitude": 30.55,
            "longitude": 114.32,
            "timezone": "Asia/Shanghai",
            "country": "中国",
            "country_code": "CN",
            "admin1": "湖北省",
            "admin2": "武汉市",
            "feature_code": "ADM3",
        },
        {
            "id": 2,
            "name": "武昌镇",
            "latitude": 30.42,
            "longitude": 114.38,
            "timezone": "Asia/Shanghai",
            "country": "中国",
            "country_code": "CN",
            "admin1": "湖北省",
            "admin2": "武汉市",
            "feature_code": "PPL",
        },
    ]
}


def forecast_payload(start: date = START, count: int = 7) -> dict[str, object]:
    dates = [(start + timedelta(days=offset)).isoformat() for offset in range(count)]
    return {
        "latitude": 30.5625,
        "longitude": 114.3125,
        "timezone": "Asia/Shanghai",
        "daily_units": {
            "weather_code": "wmo code",
            "temperature_2m_max": "°C",
            "temperature_2m_min": "°C",
            "precipitation_sum": "mm",
            "rain_sum": "mm",
            "showers_sum": "mm",
            "wind_speed_10m_max": "km/h",
        },
        "daily": {
            "time": dates,
            "weather_code": [3] * count,
            "temperature_2m_max": [22.0] * count,
            "temperature_2m_min": [13.0] * count,
            "precipitation_sum": [0.0] * count,
            "rain_sum": [0.0] * count,
            "showers_sum": [0.0] * count,
            "wind_speed_10m_max": [12.0] * count,
        },
    }


def field_store(path: Path) -> WorkbenchStoreV3:
    store = WorkbenchStoreV3(path)
    store.save_field_draft(
        FieldDraftV3(field_id="field-1"), expected_version=None, operation_id="create-field"
    )
    return store


def handler_for(forecast: dict[str, object], *, count: int = 2):
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=LOCATION_RESPONSE)
        return httpx.Response(200, json=forecast)

    return respond


def test_geocoding_returns_candidates_without_silently_selecting_first() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["countryCode"] == "CN"
        assert request.url.params["language"] == "zh"
        return httpx.Response(200, json=LOCATION_RESPONSE)

    service = OpenMeteoService(transport=httpx.MockTransport(respond))
    results = service.search_locations("武昌")

    assert len(results) == 2
    assert results[0].label == "湖北省 · 武汉市 · 武昌区"
    assert service.select_location(results, "1").latitude == 30.55


def test_forecast_uses_exact_seven_day_range_and_preserves_grid_and_units() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=LOCATION_RESPONSE)
        assert request.url.params["start_date"] == START.isoformat()
        assert request.url.params["end_date"] == (START + timedelta(days=6)).isoformat()
        assert request.url.params["timezone"] == "Asia/Shanghai"
        assert request.url.params["daily"]
        return httpx.Response(200, json=forecast_payload())

    service = OpenMeteoService(transport=httpx.MockTransport(respond))
    results = service.search_locations("武昌")
    location = service.select_location(results, "1")
    snapshot = service.fetch_forecast("field-1", location, START, START + timedelta(days=6))

    assert len(snapshot.days) == 7
    assert snapshot.requested_latitude == location.latitude
    assert snapshot.grid_latitude == 30.5625
    assert snapshot.units["precipitation_sum"] == "mm"
    assert snapshot.location.resolution == "unknown"


def test_forecast_rejects_a_missing_day_instead_of_filling_weather_with_a_guess() -> None:
    service = OpenMeteoService(
        transport=httpx.MockTransport(handler_for(forecast_payload(count=6)))
    )
    location = service.select_location(service.search_locations("武昌"), "1")

    with pytest.raises(WeatherServiceError, match="缺少日期"):
        service.fetch_forecast("field-1", location, START, START + timedelta(days=6))


def test_forecast_rejects_a_response_in_the_wrong_timezone() -> None:
    payload = forecast_payload()
    payload["timezone"] = "UTC"
    service = OpenMeteoService(transport=httpx.MockTransport(handler_for(payload)))
    location = service.select_location(service.search_locations("武昌"), "1")

    with pytest.raises(WeatherServiceError, match="时区"):
        service.fetch_forecast("field-1", location, START, START + timedelta(days=6))


def test_cached_forecast_expires_after_six_hours_and_never_sets_workability(
    tmp_path: Path,
) -> None:
    now = [datetime(2026, 10, 18, 8, tzinfo=UTC)]
    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=LOCATION_RESPONSE)
        requests += 1
        return httpx.Response(200, json=forecast_payload())

    store = field_store(tmp_path / "weather.sqlite3")
    service = OpenMeteoService(
        store=store,
        transport=httpx.MockTransport(respond),
        now=lambda: now[0],
    )
    location = service.select_location(service.search_locations("武昌"), "1")
    first = service.fetch_forecast("field-1", location, START, START + timedelta(days=6))
    cached = service.fetch_forecast("field-1", location, START, START + timedelta(days=6))
    assert cached.id == first.id
    assert requests == 1

    now[0] += timedelta(hours=7)
    refreshed = service.fetch_forecast("field-1", location, START, START + timedelta(days=6))

    assert refreshed.id != first.id
    assert requests == 2
    assert store.latest_weather("field-1").id == refreshed.id
