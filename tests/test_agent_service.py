"""The chat model may call validated tools but cannot write accepted user state."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nongshi.agent import AgentServiceV3, ProposeFeedbackArgs
from nongshi.config import Settings
from nongshi.d02_model_groups import _retrieval, shared_rule_query
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import DayWindow, FieldProfile, TimeWindow
from nongshi.schema_v3 import (
    FactEventInputV3,
    FieldDraftV3,
    GeocodingResultV3,
    WeatherLocationV3,
)
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.weather import OpenMeteoService

TEST_KEY = "sk-test-agent-key-do-not-log"
START = date(2026, 10, 19)


def make_agent(tmp_path: Path, handler, *, configured: bool = True):
    database = tmp_path / "workbench.sqlite3"
    store = WorkbenchStoreV3(database)
    service = WorkbenchServiceV3(
        store,
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 18),
    )
    profile = FieldProfile(
        id="field-1",
        name="一号田",
        province="湖北",
        province_confirmed=True,
        crop="油菜",
        crop_confirmed=True,
        stage="苗期",
        stage_confirmed=True,
        field_type="lowland_rice_stubble",
        inspection_requested=True,
        inspection_duration_minutes=30,
    )
    days = [
        DayWindow(
            day=START + timedelta(days=offset),
            site_workable=True,
            weather_workable=True,
            resource_minutes=120,
            intervals=[TimeWindow(start="09:00", end="11:00")],
        )
        for offset in range(7)
    ]
    service.save_field_draft(
        FieldDraftV3(field_id="field-1", profile=profile, start_date=START, days=days),
        expected_version=None,
        operation_id="create-field",
    )
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_model="deepseek-flash",
        llm_base_url="https://api.deepseek.com",
        llm_api_key=TEST_KEY if configured else "",
        llm_timeout_seconds=20,
    )
    return (
        AgentServiceV3(
            settings,
            service,
            transport=httpx.MockTransport(handler),
        ),
        service,
        database,
    )


def tool_call_response(name: str, arguments: str, *, call_id: str = "tool-1") -> dict[str, object]:
    return {
        "id": "chat-1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }


def final_response(text: str = "已读取地块信息。") -> dict[str, object]:
    return {
        "id": "chat-2",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {"prompt_tokens": 7, "completion_tokens": 4},
    }


def test_sealed_b_and_c_receive_identical_rule_snapshot(tmp_path: Path) -> None:
    agent, service, _ = make_agent(
        tmp_path, lambda _request: httpx.Response(200, json=final_response())
    )
    case = {
        "received_at": "2026-10-18T16:00:00+08:00",
        "draft": service.get_field("field-1").draft.model_dump(mode="json"),
    }
    expected, _ = _retrieval(case)
    agent._evaluation_rule_query = lambda: shared_rule_query(case)
    actual, _, _, status = agent._execute_tool(
        "search_rules",
        json.dumps({"query": {"task_type": "inspection"}}),
        field_id="field-1",
        run_id="eval-1",
    )
    agent.close()
    assert status == "success"
    assert actual == expected


def test_chat_calls_local_read_tool_with_deepseek_flash_and_records_redacted_metrics(
    tmp_path: Path,
) -> None:
    request_payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
        payload = json.loads(request.content)
        request_payloads.append(payload)
        if len(request_payloads) == 1:
            assert payload["model"] == "deepseek-flash"
            assert payload["thinking"] == {"type": "disabled"}
            return httpx.Response(
                200,
                json=tool_call_response("read_field", '{"field_id":"field-1"}'),
            )
        return httpx.Response(200, json=final_response())

    agent, service, database = make_agent(tmp_path, respond)
    result = agent.run("field-1", "看看一号田资料")

    assert result.status == "success"
    assert result.request_count == 2
    assert result.tools[0].name == "read_field"
    run = service.store.list_agent_runs("field-1")[0]
    assert run.request_count == 2 and run.tool_count == 1
    assert run.prompt_tokens == 12 and run.completion_tokens == 6
    assert run.tool_metrics[0].name == "read_field"
    assert run.tool_metrics[0].status == "success"
    assert TEST_KEY.encode() not in database.read_bytes()


def test_injected_received_at_fixes_the_agent_clock_for_deterministic_runs(
    tmp_path: Path,
) -> None:
    payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=final_response())

    agent, _, _ = make_agent(tmp_path, respond)
    agent.run("field-1", "看看资料", received_at=datetime(2026, 10, 20, 8, 30, tzinfo=UTC))

    system = payloads[0]["messages"][0]["content"]
    assert "2026-10-20 16:30" in system


def test_model_update_is_only_a_proposal_until_the_user_confirms_it(tmp_path: Path) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json=tool_call_response(
                    "propose_field_update",
                    '{"field_id":"field-1","changes":{"maintenance_need_confirmed":true}}',
                ),
            )
        return httpx.Response(200, json=final_response("我准备把巡田需求设为需要，请确认。"))

    agent, service, _ = make_agent(tmp_path, respond)
    result = agent.run("field-1", "我想巡田")
    state = service.get_field("field-1")

    assert result.status == "success"
    assert len(result.proposals) == 1
    assert result.proposals[0].kind == "field_update"
    assert state.draft_version == 1
    assert state.draft.profile.maintenance_need_confirmed is None


def test_bad_tool_arguments_are_rejected_and_unknown_task_ids_cannot_be_proposed(
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            tool_call_response("read_field", "not-json"),
            final_response("输入无法识别，已保留原信息。"),
        ]
    )

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    agent, service, _ = make_agent(tmp_path, respond)
    result = agent.run("field-1", "把未知内容当任务完成")

    assert result.status == "success"
    assert result.tools[0].status == "invalid_arguments"
    assert service.get_field("field-1").fact_version == 0
    assert service.get_current_plan("field-1") is None


def test_location_tools_only_propose_a_user_selectable_search_candidate(tmp_path: Path) -> None:
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

    class FakeWeather:
        def search_locations(self, query: str):
            assert query == "洪山区珞珈街道"
            return [candidate]

        select_location = staticmethod(OpenMeteoService.select_location)

        def close(self) -> None:
            return None

    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(500))
    agent.weather.close()
    agent.weather = FakeWeather()
    result, _, _, status = agent._execute_tool(
        "search_locations",
        '{"field_id":"field-1","query":"洪山区珞珈街道"}',
        field_id="field-1",
        run_id="location-run",
    )

    assert status == "success"
    candidate_id = result["candidates"][0]["candidate_id"]
    result, proposal, _, status = agent._execute_tool(
        "propose_location",
        json.dumps({"field_id": "field-1", "candidate_id": candidate_id}),
        field_id="field-1",
        run_id="location-run",
    )
    assert status == "success"
    assert result["requires_user_confirmation"] is True
    assert proposal is not None and proposal.draft is not None
    assert proposal.draft.location.display_name == "湖北省 · 武汉市 · 洪山区 · 珞珈街道"
    assert service.get_field("field-1").draft.location is None


def test_missing_credential_keeps_structured_workbench_available_without_network(
    tmp_path: Path,
) -> None:
    def should_not_call(_: httpx.Request) -> httpx.Response:
        raise AssertionError("模型密钥为空时不得发出请求")

    agent, service, _ = make_agent(tmp_path, should_not_call, configured=False)
    result = agent.run("field-1", "解释当前情况")

    assert result.status == "missing_credentials"
    assert result.request_count == 0
    assert service.get_field("field-1").draft.profile.name == "一号田"
    assert service.store.list_agent_runs("field-1")[0].status == "missing_credentials"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(401, "auth_error"), (402, "insufficient_balance"), (429, "rate_limited")],
)
def test_provider_auth_balance_and_rate_limit_errors_are_distinguished(
    tmp_path: Path,
    status_code: int,
    expected: str,
) -> None:
    agent, service, _ = make_agent(
        tmp_path,
        lambda _: httpx.Response(
            status_code, json={"error": {"message": "redacted provider error"}}
        ),
    )
    result = agent.run("field-1", "查询天气")

    assert result.status == expected
    assert service.store.list_agent_runs("field-1")[0].status == expected


def test_call_and_tool_budgets_are_enforced(tmp_path: Path) -> None:
    requests = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests <= 3:
            return httpx.Response(
                200,
                json=tool_call_response(
                    "read_field", '{"field_id":"field-1"}', call_id=f"tool-{requests}"
                ),
            )
        return httpx.Response(200, json=final_response())

    agent, _, _ = make_agent(tmp_path, respond)
    result = agent.run("field-1", "反复读取")

    assert result.request_count == 3
    assert requests == 3
    assert len([tool for tool in result.tools if tool.status == "success"]) == 3


def test_tool_budget_caps_execution_at_six_even_when_model_returns_more_calls(
    tmp_path: Path,
) -> None:
    calls = [
        {
            "id": f"tool-{index}",
            "type": "function",
            "function": {
                "name": "read_field",
                "arguments": '{"field_id":"field-1"}',
            },
        }
        for index in range(7)
    ]

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"role": "assistant", "content": None, "tool_calls": calls},
                    }
                ]
            },
        )

    agent, _, _ = make_agent(tmp_path, respond)
    result = agent.run("field-1", "读取资料")

    assert result.request_count == 1
    assert len([trace for trace in result.tools if trace.status == "success"]) == 6
    assert result.tools[-1].status == "limit_reached"
    assert result.status == "error"
    assert "上限" in result.error_message


def test_nonexistent_task_id_is_rejected_before_a_feedback_proposal_is_shown(
    tmp_path: Path,
) -> None:
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=final_response()))
    tool_result, proposal, preview, status = agent._execute_tool(
        "propose_feedback",
        (
            '{"field_id":"field-1","events":[{"feedback":{"kind":"completed",'
            '"task_id":"field-1-M-FAKE","at":"2026-10-20T10:00:00+08:00"}}]}'
        ),
        field_id="field-1",
        run_id="test-run",
    )

    assert proposal is None and preview is None
    assert status == "invalid_arguments"
    assert tool_result["error"] == "tool_validation_failed"
    agent.close()


def test_resource_interval_cannot_be_disguised_as_workability() -> None:
    with pytest.raises(ValueError, match="作业条件反馈不能包含可用时段"):
        ProposeFeedbackArgs.model_validate(
            {
                "field_id": "field-1",
                "events": [
                    {
                        "feedback": {
                            "kind": "workability",
                            "at": "2026-10-20T10:00:00+08:00",
                            "plot_id": "field-1",
                            "day": "2026-10-21",
                            "intervals": [{"start": "14:00", "end": "15:00"}],
                        }
                    }
                ],
            }
        )


def test_explicit_unknown_workability_remains_allowed() -> None:
    event = FactEventInputV3.model_validate(
        {
            "feedback": {
                "kind": "workability",
                "at": "2026-10-20T10:00:00+08:00",
                "plot_id": "field-1",
                "day": "2026-10-21",
                "site_workable": None,
                "weather_workable": None,
            }
        }
    )
    assert event.feedback.site_workable is None


def test_feedback_tool_accepts_flat_event_without_changing_its_kind() -> None:
    args = ProposeFeedbackArgs.model_validate(
        {
            "field_id": "field-1",
            "events": [
                {
                    "kind": "resource",
                    "at": "2026-10-20T10:00:00+08:00",
                    "day": "2026-10-21",
                    "plot_id": "field-1",
                    "intervals": [{"start": "14:00", "end": "15:00"}],
                    "resource_minutes": 60,
                }
            ],
        }
    )
    assert args.validated_events()[0].feedback.kind == "resource"


def test_feedback_tool_keeps_four_distinct_confirmable_facts() -> None:
    at = "2026-10-20T10:00:00+08:00"
    args = ProposeFeedbackArgs.model_validate(
        {
            "field_id": "field-1",
            "events": [
                {"kind": "completed", "at": at, "task_id": "known-I", "plot_id": "field-1"},
                {
                    "kind": "observation",
                    "at": at,
                    "plot_id": "field-1",
                    "field": "standing_water",
                    "value": True,
                },
                {
                    "kind": "observation",
                    "at": at,
                    "plot_id": "field-1",
                    "field": "pest_signs",
                    "value": None,
                },
                {
                    "kind": "resource",
                    "at": at,
                    "day": "2026-10-21",
                    "intervals": [{"start": "14:30", "end": "15:30"}],
                    "resource_minutes": 60,
                },
            ],
        }
    )
    events = args.validated_events()
    assert [event.feedback.kind for event in events] == [
        "completed",
        "observation",
        "observation",
        "resource",
    ]
    assert events[2].feedback.value is None


def test_agent_cannot_claim_feedback_saved_without_proposal(tmp_path: Path) -> None:
    agent, _, _ = make_agent(
        tmp_path, lambda _: httpx.Response(200, json=final_response("已记录今天的巡田完成"))
    )
    result = agent.run("field-1", "今天巡田完成了，请记录完成事实")
    assert result.status == "invalid_response"
    assert not result.proposals
    agent.close()


def test_current_weather_uses_saved_start_and_complete_seven_days(tmp_path: Path) -> None:
    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(500))
    state = service.get_field("field-1")
    draft = state.draft.model_copy(
        update={
            "location": WeatherLocationV3(
                query="洪山区",
                display_name="湖北省 · 武汉市 · 洪山区",
                latitude=30.53,
                longitude=114.36,
            )
        }
    )
    service.save_field_draft(draft, expected_version=state.draft_version, operation_id="loc")
    requested = []

    class FakeWeather:
        def fetch_forecast(self, field_id, location, start, end):
            requested.append((field_id, start, end))
            return SimpleNamespace(
                model_dump=lambda **_: {"coverage_start": str(start), "coverage_end": str(end)}
            )

        def close(self):
            pass

    agent.weather.close()
    agent.weather = FakeWeather()
    result, _, _, status = agent._execute_tool(
        "query_current_weather",
        '{"field_id":"field-1"}',
        field_id="field-1",
        run_id="weather-test",
    )
    assert status == "success"
    assert requested == [("field-1", START, START + timedelta(days=6))]
    assert result["coverage_end"] == str(START + timedelta(days=6))
    bad, _, _, status = agent._execute_tool(
        "query_weather",
        '{"field_id":"field-1","start_date":"2026-10-19","end_date":"2026-10-20"}',
        field_id="field-1",
        run_id="weather-test",
    )
    assert status == "invalid_arguments" and bad["error"]
    agent.close()


def test_agent_explanation_filters_unverified_dates_and_rule_citations() -> None:
    from nongshi.agent import AgentServiceV3

    safe = AgentServiceV3._safe_explanation("我已核对现有地块资料。")
    unsupported = AgentServiceV3._safe_explanation("安排在 2026-10-20 09:00，依据 C11。")

    assert safe == "我已核对现有地块资料。"
    assert "结构化内容" in unsupported
