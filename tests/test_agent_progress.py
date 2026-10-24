"""Real execution stages and cancellation must remain independent of Streamlit."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from threading import Event

import httpx
import pytest
from test_agent_service import START, final_response, make_agent, tool_call_response
from test_weather_service import forecast_payload

from nongshi.schema_v3 import GeocodingResultV3, WeatherLocationV3
from nongshi.weather import OpenMeteoService


def test_progress_reports_only_executed_model_and_tool_stages(tmp_path):
    events = []
    count = 0

    def respond(_):
        nonlocal count
        count += 1
        payload = (
            tool_call_response("read_field", json.dumps({"field_id": "field-1"}))
            if count == 1
            else final_response()
        )
        return httpx.Response(200, json=payload)

    agent, _, _ = make_agent(tmp_path, respond)
    try:
        result = agent.run("field-1", "看看资料", progress=events.append)
    finally:
        agent.close()
    assert result.status == "success"
    assert [(event.stage, event.status) for event in events] == [
        ("model", "started"),
        ("model", "success"),
        ("tool", "started"),
        ("tool", "success"),
        ("model", "started"),
        ("model", "success"),
        ("finished", "success"),
    ]
    assert events[2].tool_name == "read_field"
    assert events[-1].request_count == 2


def test_missing_credentials_reports_no_model_activity(tmp_path):
    events = []
    agent, _, _ = make_agent(tmp_path, lambda _: pytest.fail("unexpected HTTP"), configured=False)
    try:
        result = agent.run("field-1", "看看资料", progress=events.append)
    finally:
        agent.close()
    assert result.status == "missing_credentials"
    assert [(event.stage, event.status) for event in events] == [
        ("finished", "missing_credentials")
    ]


@pytest.mark.parametrize("tool_name", ["preview_plan", "propose_field_update"])
def test_cancel_during_model_response_prevents_tool_and_run_writes(tmp_path, tool_name):
    from nongshi.agent_jobs import AgentCancellation, AgentCancelled

    token = AgentCancellation()

    def respond(_):
        token.cancel()
        args = {"field_id": "field-1"}
        if tool_name == "propose_field_update":
            args["changes"] = {"name": "迟到修改"}
        return httpx.Response(200, json=tool_call_response(tool_name, json.dumps(args)))

    agent, service, _ = make_agent(tmp_path, respond)
    try:
        with pytest.raises(AgentCancelled):
            agent.run("field-1", "预览一下", cancellation=token)
    finally:
        agent.close()
    assert service.store.list_agent_runs("field-1") == []
    assert service.pending_agent_proposals("field-1") == []
    assert service.latest_actionable_preview("field-1") is None


def test_cancel_during_weather_return_prevents_snapshot_write(tmp_path):
    from nongshi.agent_jobs import AgentCancellation, AgentCancelled

    token = AgentCancellation()
    agent, service, _ = make_agent(
        tmp_path,
        lambda _: httpx.Response(
            200,
            json=tool_call_response("query_current_weather", '{"field_id":"field-1"}'),
        ),
    )
    state = service.get_field("field-1")
    service.save_field_draft(
        state.draft.model_copy(
            update={
                "location": WeatherLocationV3(
                    query="武汉", display_name="武汉", latitude=30.53, longitude=114.36
                )
            }
        ),
        expected_version=state.draft_version,
        operation_id="location",
    )

    def weather_return(_):
        token.cancel()
        return httpx.Response(200, json=forecast_payload(START))

    agent.weather.close()
    agent.weather = OpenMeteoService(
        store=service.store,
        transport=httpx.MockTransport(weather_return),
        now=lambda: datetime(2026, 10, 18, tzinfo=UTC),
    )
    try:
        with pytest.raises(AgentCancelled):
            agent.run("field-1", "查询天气", cancellation=token)
    finally:
        agent.close()
    assert service.store.latest_weather("field-1") is None
    assert service.store.list_agent_runs("field-1") == []


def test_background_job_exposes_progress_and_discards_late_network_result(tmp_path):
    from nongshi.agent_jobs import AgentJob

    entered = Event()
    release = Event()

    def respond(_):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=final_response())

    agent, service, _ = make_agent(tmp_path, respond)
    job = AgentJob(lambda: agent, "field-1", "看看资料")
    try:
        assert entered.wait(5)
        assert job.snapshot().events[-1].stage == "model"
        assert not job.snapshot().done
        job.cancel()
        assert job.snapshot().cancelled and job.snapshot().done
    finally:
        release.set()
        assert job.wait(5)
    assert job.snapshot().response is None
    assert service.store.list_agent_runs("field-1") == []


def test_background_job_returns_structured_fallback(tmp_path):
    from nongshi.agent_jobs import AgentJob

    agent, _, _ = make_agent(tmp_path, lambda _: pytest.fail("unexpected HTTP"), configured=False)
    job = AgentJob(lambda: agent, "field-1", "看看资料")
    assert job.wait(5)
    assert job.snapshot().done
    assert job.snapshot().response.status == "missing_credentials"


def test_cancel_during_preview_calculation_prevents_preview_persistence(tmp_path, monkeypatch):
    from nongshi.agent_jobs import AgentCancellation, AgentCancelled

    token = AgentCancellation()
    agent, service, _ = make_agent(
        tmp_path,
        lambda _: httpx.Response(
            200, json=tool_call_response("preview_plan", '{"field_id":"field-1"}')
        ),
    )
    calculate = service._evaluate_request

    def cancel_after_calculation(*args, **kwargs):
        result = calculate(*args, **kwargs)
        token.cancel()
        return result

    monkeypatch.setattr(service, "_evaluate_request", cancel_after_calculation)
    try:
        with pytest.raises(AgentCancelled):
            agent.run("field-1", "预览计划", cancellation=token)
    finally:
        agent.close()
    assert service.latest_actionable_preview("field-1") is None
    assert service.store.list_agent_runs("field-1") == []


def test_cancel_after_proposal_calculation_prevents_run_and_proposal_persistence(tmp_path):
    from nongshi.agent_jobs import AgentCancellation, AgentCancelled

    token = AgentCancellation()
    agent, service, _ = make_agent(
        tmp_path,
        lambda _: httpx.Response(
            200,
            json=tool_call_response(
                "declare_request",
                json.dumps(
                    {
                        "field_id": "field-1",
                        "actions": ["field_update"],
                        "field_changes": {"name": "新名字"},
                        "field_change_quotes": {"name": "把名字改为新名字"},
                    },
                    ensure_ascii=False,
                ),
            ),
        ),
    )

    def cancel_when_proposal_ready(event):
        if event.stage == "tool" and event.status == "success":
            token.cancel()

    try:
        with pytest.raises(AgentCancelled):
            agent.run(
                "field-1",
                "把名字改为新名字",
                progress=cancel_when_proposal_ready,
                cancellation=token,
            )
    finally:
        agent.close()
    assert service.pending_agent_proposals("field-1") == []
    assert service.store.list_agent_runs("field-1") == []


def test_background_job_factory_failure_is_retryable_and_redacted():
    from nongshi.agent_jobs import AgentJob

    def failing_factory():
        raise RuntimeError("private-provider-value")

    job = AgentJob(failing_factory, "field-1", "看看资料")
    assert job.wait(5)
    snapshot = job.snapshot()
    assert snapshot.done and snapshot.response is None
    assert "重试" in snapshot.error
    assert "private-provider-value" not in snapshot.error


def test_location_candidate_survives_successful_background_turn(tmp_path):
    from nongshi.agent_jobs import AgentJob

    candidate = GeocodingResultV3(
        id="local-5374",
        name="珞珈街道",
        latitude=30.536,
        longitude=114.365,
        country="中国",
        country_code="CN",
        admin1="湖北省",
        admin2="武汉市",
        admin3="洪山区",
        query="湖北省武汉市洪山区珞珈街道",
        match_source="local_gazetteer",
        resolution="township",
        match_reason="本地索引匹配",
    )
    cache = {}
    count = 0

    class FakeWeather:
        def search_locations(self, query, **_kwargs):  # type: ignore[no-untyped-def]
            assert query == "洪山区珞珈街道"
            return [candidate]

        select_location = staticmethod(OpenMeteoService.select_location)

        def close(self):
            return None

    def respond(_):
        nonlocal count
        count += 1
        if count == 1:
            payload = tool_call_response(
                "search_locations", '{"field_id":"field-1","query":"洪山区珞珈街道"}'
            )
        elif count == 3:
            payload = tool_call_response(
                "propose_location",
                json.dumps({"field_id": "field-1", "candidate_id": next(iter(cache))}),
            )
        else:
            payload = final_response()
        return httpx.Response(200, json=payload)

    def factory():
        agent, _, _ = make_agent(tmp_path, respond)
        agent.weather.close()
        agent.weather = FakeWeather()
        agent._geocoding_candidates = cache
        return agent

    first = AgentJob(factory, "field-1", "查找洪山区珞珈街道")
    assert first.wait(5)
    assert first.snapshot().response.status == "success"
    assert len(cache) == 1
    second = AgentJob(factory, "field-1", "选择刚才的候选地点")
    assert second.wait(5)
    assert second.snapshot().response.status == "success"
    assert second.snapshot().response.tools[0].status == "success"


def test_cancelled_location_search_does_not_publish_candidate(tmp_path):
    from nongshi.agent_jobs import AgentCancellation, AgentCancelled

    candidate = GeocodingResultV3(
        id="local-5374",
        name="珞珈街道",
        latitude=30.536,
        longitude=114.365,
        country="中国",
        country_code="CN",
        query="珞珈街道",
        match_source="local_gazetteer",
        resolution="township",
    )

    class FakeWeather:
        def search_locations(self, _query, **_kwargs):  # type: ignore[no-untyped-def]
            return [candidate]

        def close(self):
            return None

    token = AgentCancellation()
    agent, _, _ = make_agent(
        tmp_path,
        lambda _: httpx.Response(
            200,
            json=tool_call_response(
                "search_locations", '{"field_id":"field-1","query":"珞珈街道"}'
            ),
        ),
    )
    agent.weather.close()
    agent.weather = FakeWeather()
    cache = {}
    agent._geocoding_candidates = cache

    def cancel_after_search(event):
        if event.stage == "tool" and event.status == "success":
            token.cancel()

    try:
        with pytest.raises(AgentCancelled):
            agent.run("field-1", "查找珞珈街道", progress=cancel_after_search, cancellation=token)
    finally:
        agent.close()
    assert cache == {}
