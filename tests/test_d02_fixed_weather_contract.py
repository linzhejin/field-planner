"""Public synthetic fixtures exercise real Agent.run without external requests."""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_agent_service import final_response, tool_call_response
from test_d02_runner_scripted import START, _case

from nongshi.agent import AgentServiceV3
from nongshi.config import Settings
from nongshi.d02_runner import _FixedWeatherService, _ScriptSession
from nongshi.weather import WeatherServiceError


@pytest.fixture(autouse=True)
def forbid_sync_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fixed weather fixture must not make HTTP requests")

    monkeypatch.setattr(httpx.Client, "send", forbidden)


def make_session(tmp_path: Path, case=None):
    case = case or _case()
    session = _ScriptSession(case, database=tmp_path / "fixture.sqlite3")
    session.execute_step(case["interaction_script"][0])
    return case, session, _FixedWeatherService(case, session.store, now=session.clock.now)


@pytest.mark.parametrize("tool", ["search_locations", "query_current_weather"])
@pytest.mark.parametrize("weather_status", ["success", "missing_dates", "stale"])
def test_real_agent_uses_fixed_weather_contract(tmp_path: Path, monkeypatch, tool, weather_status):
    case, session, weather = make_session(tmp_path)
    if weather_status == "missing_dates":
        case["weather_fixture"]["daily"].pop()
    elif weather_status == "stale":
        session.clock.set(datetime.fromisoformat(case["received_at"]) + timedelta(hours=6))
    field_id = case["draft"]["field_id"]
    arguments = {"field_id": field_id}
    if tool == "search_locations":
        arguments["query"] = "模拟测试县"
    observed = []
    budgets = []
    method_name = "search_locations" if tool == "search_locations" else "fetch_forecast"
    fixture_method = getattr(weather, method_name)

    def record_fixture_call(*args, **kwargs):
        budgets.append(kwargs.get("timeout_seconds"))
        return fixture_method(*args, **kwargs)

    monkeypatch.setattr(weather, method_name, record_fixture_call)

    def respond(request):
        assert request.url.host == "model.invalid"
        payload = json.loads(request.content)
        results = [message for message in payload["messages"] if message["role"] == "tool"]
        if not results:
            return httpx.Response(200, json=tool_call_response(tool, json.dumps(arguments)))
        observed.append(json.loads(results[-1]["content"]))
        return httpx.Response(200, json=final_response())

    agent = AgentServiceV3(
        Settings(
            _env_file=None,
            llm_api_key="synthetic-key",
            llm_base_url="https://model.invalid",
            llm_timeout_seconds=20,
        ),
        session.service,
        weather=weather,
        transport=httpx.MockTransport(respond),
    )
    try:
        agent.run(field_id, "请查询", received_at=session.clock.now())
    finally:
        agent.close()
    assert len(observed) == 1
    assert len(budgets) == 1
    assert budgets[0] is not None and 0 < budgets[0] <= 20
    result = observed[0]
    if tool == "search_locations":
        assert len(result["candidates"]) == 1
    elif weather_status == "success":
        assert result["source"] == case["weather_fixture"]["source"]
        assert result["fetched_at"] == "2026-10-20T10:10:00Z"
    else:
        code = "missing_dates" if weather_status == "missing_dates" else "invalid"
        assert result["error"] == f"weather_{code}"


@pytest.mark.parametrize("budget", [0, -1])
@pytest.mark.parametrize("operation", ["search", "forecast"])
def test_fixed_fixture_rejects_expired_budget(tmp_path: Path, budget, operation):
    case, session, weather = make_session(tmp_path)
    with pytest.raises(WeatherServiceError) as failure:
        if operation == "search":
            weather.search_locations("模拟测试县", timeout_seconds=budget)
        else:
            location = session.service.get_field(case["draft"]["field_id"]).draft.location
            weather.fetch_forecast(
                case["draft"]["field_id"],
                location,
                START,
                START + timedelta(days=6),
                force_refresh=True,
                timeout_seconds=budget,
            )
    assert failure.value.code == "timeout"


def test_fixed_search_accepts_count_and_validates_like_live_service(tmp_path: Path):
    _, _, weather = make_session(tmp_path)
    assert len(weather.search_locations("模拟测试县", count=1, timeout_seconds=1)) == 1
    with pytest.raises(ValueError):
        weather.search_locations("模")
    with pytest.raises(ValueError):
        weather.search_locations("模拟测试县", count=11)


def test_fixed_search_limits_explicit_candidates(tmp_path: Path):
    case, _, weather = make_session(tmp_path)
    case["location_lookup_fixture"] = {
        "query": "模拟测试县",
        "candidates": [
            {"candidate_id": str(index), **case["weather_fixture"]["location"]}
            for index in range(3)
        ],
    }
    assert [row.id for row in weather.search_locations("模拟测试县", count=2)] == ["0", "1"]


@pytest.mark.parametrize("operation", ["search", "forecast"])
def test_fixture_checks_positive_budget_before_return_or_persistence(
    tmp_path: Path, monkeypatch, operation
):
    case, session, weather = make_session(tmp_path)
    with sqlite3.connect(tmp_path / "fixture.sqlite3") as connection:
        before = connection.execute("SELECT COUNT(*) FROM weather_snapshots_v3").fetchone()[0]
    ticks = iter([10.0, 10.0, 12.0])
    monkeypatch.setattr("nongshi.d02_runner.time.perf_counter", lambda: next(ticks))
    with pytest.raises(WeatherServiceError) as failure:
        if operation == "search":
            weather.search_locations("模拟测试县", timeout_seconds=1)
        else:
            location = session.service.get_field(case["draft"]["field_id"]).draft.location
            weather.fetch_forecast(
                case["draft"]["field_id"],
                location,
                START,
                START + timedelta(days=6),
                force_refresh=True,
                timeout_seconds=1,
            )
    assert failure.value.code == "timeout"
    with sqlite3.connect(tmp_path / "fixture.sqlite3") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM weather_snapshots_v3").fetchone()[0] == before
        )


def test_force_refresh_keeps_fixed_business_clock(tmp_path: Path):
    case, session, weather = make_session(tmp_path)
    location = session.service.get_field(case["draft"]["field_id"]).draft.location
    snapshot = weather.fetch_forecast(
        case["draft"]["field_id"],
        location,
        START,
        START + timedelta(days=6),
        force_refresh=True,
        timeout_seconds=1,
    )
    assert snapshot.fetched_at == session.clock.now()
    assert snapshot.cache_expires_at == session.clock.now() + timedelta(hours=6)
