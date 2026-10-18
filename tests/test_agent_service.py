"""The chat model may call validated tools but cannot write accepted user state."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from nongshi.agent import AgentServiceV3
from nongshi.config import Settings
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import DayWindow, FieldProfile, TimeWindow
from nongshi.schema_v3 import FieldDraftV3
from nongshi.service_v3 import WorkbenchServiceV3

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


def test_agent_explanation_filters_unverified_dates_and_rule_citations() -> None:
    from nongshi.agent import AgentServiceV3

    safe = AgentServiceV3._safe_explanation("我已核对现有地块资料。")
    unsupported = AgentServiceV3._safe_explanation("安排在 2026-10-20 09:00，依据 C11。")

    assert safe == "我已核对现有地块资料。"
    assert "结构化内容" in unsupported
