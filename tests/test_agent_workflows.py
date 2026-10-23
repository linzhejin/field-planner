"""Public, model-driven workflow regressions for bounded Agent turns."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from test_agent_service import final_response, make_agent, tool_call_response
from test_d02_fixed_weather_contract import make_session

from nongshi.agent import AgentServiceV3
from nongshi.config import Settings

ZONE = timezone(timedelta(hours=8))


def model_calls(*calls: tuple[str, dict[str, object]]) -> dict[str, object]:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                        for index, (name, args) in enumerate(calls)
                    ],
                },
            }
        ]
    }


def resource_event(day: str, source_quote: str) -> dict[str, object]:
    return {
        "source_quote": source_quote,
        "feedback": {
            "kind": "resource",
            "at": "2026-10-18T10:00:00+08:00",
            "day": day,
            "plot_id": "field-1",
            "intervals": [{"start": "14:00", "end": "16:00"}],
            "resource_minutes": 120,
        },
    }


def test_declaration_proposes_explicit_resource_in_first_model_request(tmp_path) -> None:
    request_count = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json=model_calls(
                (
                    "declare_request",
                    {
                        "field_id": "field-1",
                        "actions": ["resource"],
                        "events": [resource_event("2026-10-19", "明天14:00到16:00有空")],
                    },
                )
            ),
        )

    agent, service, _ = make_agent(tmp_path, respond)
    result = agent.run(
        "field-1",
        "明天14:00到16:00有空，请修改人员时间",
        received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
    )

    assert result.status == "success"
    assert result.business_outcome == "awaiting_confirmation"
    assert request_count == result.request_count == 1
    assert [tool.name for tool in result.tools] == ["declare_request", "propose_feedback"]
    assert result.proposals[0].events[0].feedback.kind == "resource"
    assert service.list_events("field-1") == []
    agent.close()


def test_two_observations_need_two_facts_not_one_kind(tmp_path) -> None:
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["observation"],
                "events": [
                    {
                        "source_quote": "发现积水",
                        "feedback": {
                            "kind": "observation",
                            "at": "2026-10-18T10:00:00+08:00",
                            "plot_id": "field-1",
                            "field": "standing_water",
                            "value": True,
                        },
                    }
                ],
            },
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "发现积水，虫情未知，请记录",
        received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
    )
    assert result.business_outcome == "partial"
    assert len(result.proposals) == 1
    assert "虫情" in result.text
    agent.close()


def test_missing_completion_clock_does_not_discard_independent_resource(tmp_path) -> None:
    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(500))
    initial = service.preview_plan("field-1")
    service.adopt_plan(
        initial.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    )
    task_id = next(item.task_id for item in initial.version.plan.items if item.start_at)
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["completed", "resource"],
                "events": [
                    {
                        "source_quote": "今天巡田完成",
                        "feedback": {
                            "kind": "completed",
                            "at": "2026-10-20T10:00:00+08:00",
                            "task_id": task_id,
                            "plot_id": "field-1",
                        },
                    },
                    resource_event("2026-10-21", "明天14:00到16:00有空"),
                ],
            },
        )
    )
    agent._transport = httpx.MockTransport(lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "今天巡田完成，明天14:00到16:00有空，请记录并修改人员时间",
        received_at=datetime(2026, 10, 20, 10, 2, tzinfo=ZONE),
    )
    assert result.business_outcome == "partial"
    assert len(result.proposals) == 1
    assert [event.feedback.kind for event in result.proposals[0].events] == ["resource"]
    assert "实际完成的准确日期和时间" in result.text
    agent.close()


def test_preview_declaration_calculates_from_saved_state(tmp_path) -> None:
    answer = model_calls(("declare_request", {"field_id": "field-1", "actions": ["preview"]}))
    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    result = agent.run("field-1", "根据已保存的信息预览计划")
    assert result.status == "success"
    assert result.business_outcome == "preview_ready"
    assert len(result.previews) == 1
    assert service.get_current_plan("field-1") is None
    assert [tool.name for tool in result.tools] == ["declare_request", "preview_plan"]
    agent.close()


def test_valid_proposal_is_not_hidden_by_later_irrelevant_tool(tmp_path) -> None:
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["resource"],
                "events": [resource_event("2026-10-19", "明天14:00到16:00有空")],
            },
        ),
        ("query_current_weather", {"field_id": "field-1"}),
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "明天14:00到16:00有空，请修改人员时间",
        received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
    )
    assert result.business_outcome == "awaiting_confirmation"
    assert len(result.proposals) == 1
    assert all(tool.name != "query_current_weather" for tool in result.tools)
    agent.close()


def test_four_distinct_facts_from_one_model_declaration(tmp_path) -> None:
    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(500))
    first = service.preview_plan("field-1")
    service.adopt_plan(
        first.id, expected_fact_version=0, expected_plan_id=None, operation_id="adopt-v1"
    )
    task_id = next(item.task_id for item in first.version.plan.items if item.start_at)
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["completed", "observation", "resource"],
                "events": [
                    {
                        "source_quote": "今天10:00巡田完成",
                        "feedback": {
                            "kind": "completed",
                            "at": "2026-10-20T10:00:00+08:00",
                            "task_id": task_id,
                            "plot_id": "field-1",
                        },
                    },
                    {
                        "source_quote": "发现积水",
                        "feedback": {
                            "kind": "observation",
                            "at": "2026-10-20T10:02:00+08:00",
                            "plot_id": "field-1",
                            "field": "standing_water",
                            "value": True,
                        },
                    },
                    {
                        "source_quote": "虫情未知",
                        "feedback": {
                            "kind": "observation",
                            "at": "2026-10-20T10:02:00+08:00",
                            "plot_id": "field-1",
                            "field": "pest_signs",
                            "value": None,
                        },
                    },
                    resource_event("2026-10-21", "明天14:00到16:00有空"),
                ],
            },
        )
    )
    agent._transport = httpx.MockTransport(lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "今天10:00巡田完成，发现积水，虫情未知，明天14:00到16:00有空",
        received_at=datetime(2026, 10, 20, 10, 2, tzinfo=ZONE),
    )
    assert result.business_outcome == "awaiting_confirmation"
    assert len(result.proposals) == 1
    assert [event.feedback.kind for event in result.proposals[0].events] == [
        "completed",
        "observation",
        "observation",
        "resource",
    ]
    assert service.list_events("field-1") == []
    agent.close()


def test_live_chinese_date_and_negative_pest_feedback_keeps_all_facts(tmp_path) -> None:
    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(500))
    first = service.preview_plan("field-1")
    service.adopt_plan(
        first.id, expected_fact_version=0, expected_plan_id=None, operation_id="adopt-v1"
    )
    task_id = next(item.task_id for item in first.version.plan.items if item.start_at)
    message = (
        "巡田在2026年10月24日09:30实际完成；巡田时未发现虫情；"
        "把2026年10月25日的人员可用时段改为14:00到15:30，合计90分钟；"
        "其他现场和天气作业条件不变。请分别提出待确认修改。"
    )
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["completed", "observation", "resource"],
                "events": [
                    {
                        "source_quote": "巡田在2026年10月24日09:30实际完成",
                        "feedback": {
                            "kind": "completed",
                            "at": "2026-10-24T09:30:00+08:00",
                            "task_id": task_id,
                            "plot_id": "field-1",
                            "day": "2026-10-24",
                        },
                    },
                    {
                        "source_quote": "巡田时未发现虫情",
                        "feedback": {
                            "kind": "observation",
                            "at": "2026-10-24T09:30:00+08:00",
                            "plot_id": "field-1",
                            "field": "pest_signs",
                            "value": False,
                            "day": "2026-10-24",
                        },
                    },
                    {
                        "source_quote": (
                            "把2026年10月25日的人员可用时段改为14:00到15:30，合计90分钟"
                        ),
                        "feedback": {
                            "kind": "resource",
                            "at": "2026-10-24T10:00:00+08:00",
                            "day": "2026-10-25",
                            "plot_id": "field-1",
                            "intervals": [{"start": "14:00", "end": "15:30"}],
                            "resource_minutes": 90,
                        },
                    },
                ],
            },
        )
    )
    agent._transport = httpx.MockTransport(lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1", message, received_at=datetime(2026, 10, 24, 10, 0, tzinfo=ZONE)
        )
    finally:
        agent.close()
    assert result.business_outcome == "awaiting_confirmation"
    assert len(result.proposals) == 1
    assert [item.feedback.kind for item in result.proposals[0].events] == [
        "completed",
        "observation",
        "resource",
    ]
    assert result.proposals[0].events[1].feedback.at == datetime(2026, 10, 24, 10, 0, tzinfo=ZONE)
    assert service.list_events("field-1") == []


def test_model_questions_do_not_mark_fully_grounded_changes_partial(tmp_path) -> None:
    event = resource_event("2026-10-19", "明天14:00到16:00有空")
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["resource"],
                "events": [event],
                "missing": ["请再次确认明天是否确实有空"],
            },
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            "明天14:00到16:00有空，请修改人员时间",
            received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert result.business_outcome == "awaiting_confirmation"
    assert len(result.proposals) == 1


@pytest.mark.parametrize("day,accepted", [("2026-10-25", True), ("2026-10-26", False)])
def test_chinese_calendar_date_must_match_resource_day(tmp_path, day: str, accepted: bool) -> None:
    event = resource_event(day, "2026年10月25日14:00到16:00有空")
    answer = model_calls(
        (
            "declare_request",
            {"field_id": "field-1", "actions": ["resource"], "events": [event]},
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            "2026年10月25日14:00到16:00有空，请修改人员时间",
            received_at=datetime(2026, 10, 24, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert bool(result.proposals) is accepted


@pytest.mark.parametrize(
    "time_clause,accepted",
    [
        ("巡田的实际完成时间是 2026-10-19T09:30:00+08:00", True),
        ("沟渠维护的实际完成时间是 2026-10-19T09:30:00+08:00", False),
        (
            "巡田的实际完成时间是 2026-10-19T09:30:00+08:00；"
            "巡田的实际完成时间是 2026-10-19T10:00:00+08:00",
            False,
        ),
    ],
)
def test_completed_quote_uses_only_matching_explicit_task_time(
    tmp_path, time_clause: str, accepted: bool
) -> None:
    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(500))
    preview = service.preview_plan("field-1")
    service.adopt_plan(
        preview.id, expected_fact_version=0, expected_plan_id=None, operation_id="adopt-v1"
    )
    inspection_id = next(
        item.task_id for item in preview.version.plan.items if item.task_type == "inspection"
    )
    event = {
        "source_quote": "巡田已经完成",
        "feedback": {
            "kind": "completed",
            "at": "2026-10-19T09:30:00+08:00",
            "task_id": inspection_id,
            "plot_id": "field-1",
        },
    }
    answer = model_calls(
        ("declare_request", {"field_id": "field-1", "actions": ["completed"], "events": [event]})
    )
    agent._transport = httpx.MockTransport(lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            f"巡田已经完成；补充确认：{time_clause}",
            received_at=datetime(2026, 10, 20, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert bool(result.proposals) is accepted


def test_first_three_and_third_to_seventh_days_are_grounded_from_plan_start() -> None:
    received = datetime(2026, 10, 20, 10, 0, tzinfo=ZONE)
    start = datetime(2026, 10, 19, 0, 0, tzinfo=ZONE).date()
    assert AgentServiceV3._mentioned_days("前三天无法进田", received, start) == {
        start + timedelta(days=offset) for offset in range(3)
    }
    expected = {start + timedelta(days=offset) for offset in range(2, 7)}
    assert AgentServiceV3._mentioned_days("第三天到第七天改为0分钟", received, start) == expected
    assert (
        AgentServiceV3._resource_days_in_message(
            "第三天到第七天都改为0分钟", received_at=received, plan_start=start
        )
        == expected
    )


def test_zero_resource_minutes_for_five_days_propose_five_distinct_facts(tmp_path) -> None:
    quote = "第三天到第七天都改为0分钟"
    events = [
        {
            "source_quote": quote,
            "feedback": {
                "kind": "resource",
                "at": "2026-10-20T10:00:00+08:00",
                "day": (datetime(2026, 10, 19, tzinfo=ZONE) + timedelta(days=offset))
                .date()
                .isoformat(),
                "plot_id": "field-1",
                "intervals": [],
                "resource_minutes": 0,
            },
        }
        for offset in range(2, 7)
    ]
    answer = model_calls(
        ("declare_request", {"field_id": "field-1", "actions": ["resource"], "events": events})
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            f"{quote}，第二天原有空档保留。",
            received_at=datetime(2026, 10, 20, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert result.business_outcome == "awaiting_confirmation", result.text
    assert len(result.proposals) == 1
    assert len(result.proposals[0].events) == 5


def test_three_day_no_field_access_keeps_weather_independently_workable(tmp_path) -> None:
    quote = "我确认前三天无法进田，天气条件仍可；请把三天现场可作业改为否"
    events = [
        {
            "source_quote": quote,
            "feedback": {
                "kind": "workability",
                "at": "2026-10-18T18:00:00+08:00",
                "plot_id": "field-1",
                "day": (datetime(2026, 10, 19, tzinfo=ZONE) + timedelta(days=offset))
                .date()
                .isoformat(),
                "site_workable": False,
                "weather_workable": True,
            },
        }
        for offset in range(3)
    ]
    answer = model_calls(
        (
            "declare_request",
            {"field_id": "field-1", "actions": ["workability"], "events": events},
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run("field-1", quote, received_at=datetime(2026, 10, 18, 18, 0, tzinfo=ZONE))
    finally:
        agent.close()
    assert result.business_outcome == "awaiting_confirmation"
    assert len(result.proposals) == 1
    assert len(result.proposals[0].events) == 3


def test_unobserved_pests_and_unchecked_ditch_stay_unknown(tmp_path) -> None:
    quotes = ["积水有", "虫情未观察", "既有沟渠清理完成，效果还没复查"]
    fields = ["standing_water", "pest_signs", "ditch_connectivity"]
    values = [True, None, None]
    events = [
        {
            "source_quote": quote,
            "feedback": {
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": field,
                "value": value,
            },
        }
        for quote, field, value in zip(quotes, fields, values, strict=True)
    ]
    answer = model_calls(
        (
            "declare_request",
            {"field_id": "field-1", "actions": ["observation"], "events": events},
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            "；".join(quotes),
            received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert result.business_outcome == "awaiting_confirmation", result.text
    assert len(result.proposals) == 1
    assert [event.feedback.value for event in result.proposals[0].events] == values


def test_preview_is_not_reported_ready_when_declared_field_change_is_unverified(tmp_path) -> None:
    event = {
        "source_quote": "明天田里无法进田",
        "feedback": {
            "kind": "workability",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "day": "2026-10-21",
            "site_workable": False,
        },
    }
    answer = model_calls(
        (
            "declare_request",
            {"field_id": "field-1", "actions": ["workability", "preview"], "events": [event]},
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            "明天田里无法进田，请修改后预览",
            received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert result.business_outcome == "needs_information"
    assert not result.previews


def test_resource_clock_in_unrelated_completion_clause_cannot_ground_interval(tmp_path) -> None:
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["resource"],
                "events": [resource_event("2026-10-19", "明天只有下午有空")],
            },
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "今天10:00完成巡田，明天只有下午有空",
        received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
    )
    assert result.business_outcome == "needs_information"
    assert not result.proposals
    assert "具体开始和结束" in result.text
    agent.close()


def test_two_resource_days_are_preserved_as_two_events(tmp_path) -> None:
    first_day = resource_event("2026-10-19", "明天14:00到16:00有空")
    second_day = resource_event("2026-10-20", "后天14:00到16:00有空")
    answer = model_calls(
        (
            "declare_request",
            {"field_id": "field-1", "actions": ["resource"], "events": [first_day, second_day]},
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "明天14:00到16:00有空，后天14:00到16:00有空",
        received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
    )
    assert result.business_outcome == "awaiting_confirmation"
    assert len(result.proposals[0].events) == 2
    assert {e.feedback.day.isoformat() for e in result.proposals[0].events} == {
        "2026-10-19",
        "2026-10-20",
    }
    agent.close()


def test_one_of_two_resource_days_remains_explicitly_missing(tmp_path) -> None:
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["resource"],
                "events": [resource_event("2026-10-19", "明天14:00到16:00有空")],
            },
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "明天14:00到16:00有空，后天14:00到16:00有空",
        received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
    )
    assert result.business_outcome == "partial"
    assert "2026-10-20" in result.text
    assert len(result.proposals[0].events) == 1
    agent.close()


def test_completion_clock_mismatch_keeps_other_valid_event(tmp_path) -> None:
    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(500))
    first = service.preview_plan("field-1")
    service.adopt_plan(
        first.id, expected_fact_version=0, expected_plan_id=None, operation_id="adopt-v1"
    )
    task_id = next(item.task_id for item in first.version.plan.items if item.start_at)
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["completed", "resource"],
                "events": [
                    {
                        "source_quote": "今天10:00巡田完成",
                        "feedback": {
                            "kind": "completed",
                            "at": "2026-10-20T10:01:00+08:00",
                            "task_id": task_id,
                            "plot_id": "field-1",
                        },
                    },
                    resource_event("2026-10-21", "明天14:00到16:00有空"),
                ],
            },
        )
    )
    agent._transport = httpx.MockTransport(lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "今天10:00巡田完成，明天14:00到16:00有空",
        received_at=datetime(2026, 10, 20, 10, 2, tzinfo=ZONE),
    )
    assert result.business_outcome == "partial"
    assert [event.feedback.kind for event in result.proposals[0].events] == ["resource"]
    agent.close()


def test_disallowed_tool_attempts_count_toward_six_call_limit(tmp_path) -> None:
    calls = [("declare_request", {"field_id": "field-1", "actions": ["resource"]})]
    calls.extend(("query_current_weather", {"field_id": "field-1"}) for _ in range(7))
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=model_calls(*calls)))
    result = agent.run("field-1", "请修改人员时间")
    assert result.status == "error"
    assert result.tools[-1].status == "limit_reached"
    assert len([tool for tool in result.tools if tool.status != "limit_reached"]) == 6
    agent.close()


def test_same_failed_weather_query_is_not_retried_through_alias(tmp_path) -> None:
    answer = model_calls(
        ("declare_request", {"field_id": "field-1", "actions": ["query"]}),
        ("query_current_weather", {"field_id": "field-1"}),
        (
            "query_weather",
            {"field_id": "field-1", "start_date": "2026-10-19", "end_date": "2026-10-25"},
        ),
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    result = agent.run("field-1", "查一下当前七天天气")
    weather_tools = [tool for tool in result.tools if tool.name.startswith("query_")]
    assert len(weather_tools) == 2
    assert weather_tools[0].status == "invalid_arguments"
    assert weather_tools[1].result["error"] == "duplicate_weather_attempt"
    agent.close()


def test_explicit_user_interval_gets_one_recovery_call_when_declaration_omits_it(tmp_path) -> None:
    answers = iter(
        [
            model_calls(("declare_request", {"field_id": "field-1", "actions": ["resource"]})),
            model_calls(
                (
                    "propose_feedback",
                    {
                        "field_id": "field-1",
                        "events": [resource_event("2026-10-19", "明天14:00到16:00有空")],
                    },
                )
            ),
        ]
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=next(answers)))
    result = agent.run(
        "field-1",
        "明天14:00到16:00有空，请修改人员时间",
        received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
    )
    assert result.request_count == 2
    assert result.business_outcome == "awaiting_confirmation"
    assert len(result.proposals) == 1
    agent.close()


def test_direct_feedback_tool_cannot_invent_actual_completion_clock(tmp_path) -> None:
    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(500))
    first = service.preview_plan("field-1")
    service.adopt_plan(
        first.id, expected_fact_version=0, expected_plan_id=None, operation_id="adopt-v1"
    )
    task_id = next(item.task_id for item in first.version.plan.items if item.start_at)
    answer = model_calls(
        (
            "propose_feedback",
            {
                "field_id": "field-1",
                "events": [
                    {
                        "feedback": {
                            "kind": "completed",
                            "at": "2026-10-20T10:00:00+08:00",
                            "task_id": task_id,
                            "plot_id": "field-1",
                        }
                    }
                ],
            },
        )
    )
    agent._transport = httpx.MockTransport(lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "今天巡田已完成，请记录",
        received_at=datetime(2026, 10, 20, 10, 2, tzinfo=ZONE),
    )
    assert result.proposals == []
    assert result.business_outcome == "needs_information"
    assert "实际完成" in result.text
    agent.close()


def test_same_pending_resource_proposal_is_reused_on_repeat_turn(tmp_path) -> None:
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["resource"],
                "events": [resource_event("2026-10-19", "明天14:00到16:00有空")],
            },
        )
    )
    agent, service, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    received_at = datetime(2026, 10, 18, 10, 0, tzinfo=ZONE)
    first = agent.run("field-1", "明天14:00到16:00有空", received_at=received_at)
    second = agent.run("field-1", "明天14:00到16:00有空", received_at=received_at)
    assert first.proposals[0].id == second.proposals[0].id
    assert len(service.pending_agent_proposals("field-1")) == 1
    agent.close()


def test_first_request_receives_saved_task_id_and_forces_declaration(tmp_path) -> None:
    seen: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(
            200,
            json=model_calls(("declare_request", {"field_id": "field-1", "actions": ["query"]})),
        )

    agent, service, _ = make_agent(tmp_path, respond)
    first = service.preview_plan("field-1")
    service.adopt_plan(
        first.id, expected_fact_version=0, expected_plan_id=None, operation_id="adopt-v1"
    )
    task_id = next(item.task_id for item in first.version.plan.items if item.start_at)
    agent.run("field-1", "查看当前任务")
    assert seen[0]["tool_choice"] == {"type": "function", "function": {"name": "declare_request"}}
    assert task_id in seen[0]["messages"][0]["content"]
    agent.close()


def test_preview_request_without_preview_cannot_be_answered_as_done(tmp_path) -> None:
    from test_agent_service import final_response

    agent, _, _ = make_agent(
        tmp_path, lambda _: httpx.Response(200, json=final_response("计划已经生成。"))
    )
    result = agent.run("field-1", "请预览七天计划")
    assert result.status != "success" or result.business_outcome != "answered"
    assert result.previews == []
    agent.close()


def test_rain_forecast_cannot_become_confirmed_field_workability(tmp_path) -> None:
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["workability"],
                "events": [
                    {
                        "source_quote": "明天可能下雨",
                        "feedback": {
                            "kind": "workability",
                            "at": "2026-10-18T10:00:00+08:00",
                            "plot_id": "field-1",
                            "day": "2026-10-19",
                            "site_workable": False,
                        },
                    }
                ],
            },
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    result = agent.run(
        "field-1",
        "明天可能下雨，看看风险",
        received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
    )
    assert result.proposals == []
    assert result.business_outcome == "needs_information"
    agent.close()


def test_first_tool_contract_describes_typed_fact_events(tmp_path) -> None:
    seen: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=model_calls(("declare_request", {"field_id": "field-1", "actions": ["query"]})),
        )

    agent, _, _ = make_agent(tmp_path, respond)
    agent.run("field-1", "查看当前状态")
    definitions = seen[0]["tools"]
    assert [item["function"]["name"] for item in definitions] == ["declare_request"]
    schema = definitions[0]["function"]["parameters"]
    event_ref = schema["properties"]["events"]["items"]["$ref"]
    event_schema = schema["$defs"][event_ref.split("/")[-1]]
    assert "feedback" in event_schema["properties"]
    assert "source_quote" in event_schema["required"]
    agent.close()


@pytest.mark.parametrize(
    ("field", "value", "quote", "expect_proposal"),
    [
        ("inspection_requested", True, "我想巡田", True),
        ("maintenance_need_confirmed", True, "我想巡田", False),
        ("maintenance_purpose", "clear_existing_ditch", "仍需清理既有沟渠", True),
        ("maintenance_purpose", "clear_existing_ditch", "不要清理既有沟渠", False),
    ],
)
def test_declared_field_change_must_match_quoted_user_intent(
    tmp_path, field: str, value: object, quote: str, expect_proposal: bool
) -> None:
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["field_update"],
                "field_changes": {field: value},
                "field_change_quotes": {field: quote},
            },
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run("field-1", quote)
    finally:
        agent.close()
    if expect_proposal:
        assert result.business_outcome == "awaiting_confirmation"
        assert result.proposals[0].changes[field]["new"] == value
    else:
        assert result.proposals == []
        assert result.business_outcome == "needs_information"


@pytest.mark.parametrize(
    ("fixture_status", "expected_error"),
    [("missing_dates", "weather_missing_dates"), ("stale", "weather_invalid")],
)
def test_failed_fixed_weather_is_explained_without_retrying_alias(
    tmp_path, fixture_status: str, expected_error: str
) -> None:
    case, session, weather = make_session(tmp_path)
    if fixture_status == "missing_dates":
        case["weather_fixture"]["daily"].pop()
    else:
        session.clock.set(datetime.fromisoformat(case["received_at"]) + timedelta(hours=6))
    field_id = case["draft"]["field_id"]
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json=model_calls(("query_current_weather", {"field_id": field_id})),
            )
        return httpx.Response(200, json=final_response("天气正常，可以作业。"))

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
        result = agent.run(field_id, "请查询天气", received_at=session.clock.now())
    finally:
        agent.close()
    assert calls == 2
    assert result.status == "error"
    assert result.error_category == expected_error
    assert "重试" in result.error_message
    assert [item.name for item in result.tools] == ["query_current_weather"]
    assert result.tools[0].result["error"] == expected_error


@pytest.mark.parametrize("route", ["declare", "direct"])
@pytest.mark.parametrize(
    ("message", "field", "value", "accepted"),
    [
        ("发现积水，请记录", "standing_water", False, False),
        ("发现积水，请记录", "standing_water", True, True),
        ("没有积水，请记录", "standing_water", True, False),
        ("没有积水，请记录", "standing_water", False, True),
        ("虫情未知，请记录", "pest_signs", True, False),
        ("虫情未知，请记录", "pest_signs", None, True),
        ("发现积水，虫情未知，请记录", "standing_water", None, False),
        ("发现沟渠堵塞，请记录", "ditch_connectivity", "clear", False),
        ("发现沟渠堵塞，请记录", "ditch_connectivity", "impaired", True),
        ("沟渠通畅，请记录", "ditch_connectivity", "clear", True),
        ("沟渠通畅，请记录", "ditch_connectivity", "impaired", False),
    ],
)
def test_observation_value_must_match_user_words(
    tmp_path, route: str, message: str, field: str, value: object, accepted: bool
) -> None:
    event = {
        "source_quote": message,
        "feedback": {
            "kind": "observation",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "field": field,
            "value": value,
        },
    }
    if route == "declare":
        answer = model_calls(
            (
                "declare_request",
                {"field_id": "field-1", "actions": ["observation"], "events": [event]},
            )
        )
    else:
        answer = tool_call_response(
            "propose_feedback", json.dumps({"field_id": "field-1", "events": [event]})
        )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1", message, received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE)
        )
    finally:
        agent.close()
    assert bool(result.proposals) is accepted


@pytest.mark.parametrize("route", ["declare", "direct"])
@pytest.mark.parametrize(
    ("message", "site_workable", "day", "accepted"),
    [
        ("明天田里不能作业，请记录", True, "2026-10-19", False),
        ("明天田里不能作业，请记录", False, "2026-10-19", True),
        ("明天田里可以作业，请记录", False, "2026-10-19", False),
        ("明天田里可以作业，请记录", True, "2026-10-19", True),
        ("明天田里不能作业，请记录", False, "2026-10-20", False),
        ("明天田里不能作业，请记录", None, "2026-10-19", False),
        ("明天田里情况未知，请记录", None, "2026-10-19", True),
    ],
)
def test_workability_value_and_day_must_match_user_words(
    tmp_path, route: str, message: str, site_workable: bool | None, day: str, accepted: bool
) -> None:
    event = {
        "source_quote": message,
        "feedback": {
            "kind": "workability",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "day": day,
            "site_workable": site_workable,
        },
    }
    if route == "declare":
        answer = model_calls(
            (
                "declare_request",
                {"field_id": "field-1", "actions": ["workability"], "events": [event]},
            )
        )
    else:
        answer = tool_call_response(
            "propose_feedback", json.dumps({"field_id": "field-1", "events": [event]})
        )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1", message, received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE)
        )
    finally:
        agent.close()
    assert bool(result.proposals) is accepted


@pytest.mark.parametrize("route", ["declare", "direct"])
@pytest.mark.parametrize("minutes,accepted", [(0, False), (120, True), (None, True)])
def test_resource_capacity_must_match_grounded_intervals(
    tmp_path, route: str, minutes: int | None, accepted: bool
) -> None:
    event = resource_event("2026-10-19", "明天14:00到16:00有空")
    event["feedback"]["resource_minutes"] = minutes
    if route == "declare":
        answer = model_calls(
            ("declare_request", {"field_id": "field-1", "actions": ["resource"], "events": [event]})
        )
    else:
        answer = tool_call_response(
            "propose_feedback", json.dumps({"field_id": "field-1", "events": [event]})
        )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            "明天14:00到16:00有空",
            received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert bool(result.proposals) is accepted


@pytest.mark.parametrize("route", ["declare", "direct"])
def test_unstated_observation_time_uses_receipt_not_model_future_date(tmp_path, route: str) -> None:
    event = {
        "source_quote": "发现积水",
        "feedback": {
            "kind": "observation",
            "at": "2026-10-25T10:00:00+08:00",
            "plot_id": "field-1",
            "field": "standing_water",
            "value": True,
        },
    }
    if route == "declare":
        answer = model_calls(
            (
                "declare_request",
                {"field_id": "field-1", "actions": ["observation"], "events": [event]},
            )
        )
    else:
        answer = tool_call_response(
            "propose_feedback", json.dumps({"field_id": "field-1", "events": [event]})
        )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    received_at = datetime(2026, 10, 18, 10, 0, tzinfo=ZONE)
    try:
        result = agent.run("field-1", "发现积水，请记录", received_at=received_at)
    finally:
        agent.close()
    assert len(result.proposals) == 1
    assert result.proposals[0].events[0].feedback.at == received_at


@pytest.mark.parametrize("route", ["declare", "direct"])
@pytest.mark.parametrize(
    ("message", "field", "value", "accepted"),
    [
        ("不想巡田", "inspection_requested", True, False),
        ("不想巡田", "inspection_requested", False, True),
        ("不需要巡田", "inspection_requested", False, True),
        ("不需要巡田", "inspection_requested", True, False),
        ("想巡田", "inspection_requested", True, True),
        ("不要清理既有沟渠", "maintenance_need_confirmed", True, False),
        ("不要清理既有沟渠", "maintenance_need_confirmed", False, True),
        ("不需要清理既有沟渠", "maintenance_need_confirmed", False, True),
        ("不需要清理既有沟渠", "maintenance_need_confirmed", True, False),
        ("仍需清理既有沟渠", "maintenance_need_confirmed", True, True),
    ],
)
def test_field_change_negation_is_not_inverted(
    tmp_path, route: str, message: str, field: str, value: bool, accepted: bool
) -> None:
    if route == "declare":
        answer = model_calls(
            (
                "declare_request",
                {
                    "field_id": "field-1",
                    "actions": ["field_update"],
                    "field_changes": {field: value},
                    "field_change_quotes": {field: message},
                },
            )
        )
    else:
        answer = tool_call_response(
            "propose_field_update",
            json.dumps({"field_id": "field-1", "changes": {field: value}}),
        )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run("field-1", message)
    finally:
        agent.close()
    assert bool(result.proposals) is accepted


@pytest.mark.parametrize("route", ["declare", "direct"])
def test_rejected_observation_keeps_independent_resource_proposal(tmp_path, route: str) -> None:
    events = [
        {
            "source_quote": "发现积水",
            "feedback": {
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": "standing_water",
                "value": False,
            },
        },
        resource_event("2026-10-19", "明天14:00到16:00有空"),
    ]
    if route == "declare":
        answer = model_calls(
            (
                "declare_request",
                {"field_id": "field-1", "actions": ["observation", "resource"], "events": events},
            )
        )
    else:
        answer = tool_call_response(
            "propose_feedback", json.dumps({"field_id": "field-1", "events": events})
        )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            "发现积水，明天14:00到16:00有空，请记录",
            received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert result.business_outcome == "partial"
    assert len(result.proposals) == 1
    assert [event.feedback.kind for event in result.proposals[0].events] == ["resource"]


@pytest.mark.parametrize("with_independent_missing", [False, True])
def test_corrected_observation_retires_only_its_rejection(tmp_path, with_independent_missing):
    received_at = datetime(2026, 10, 18, 10, 0, tzinfo=ZONE)

    def observation(value: bool) -> dict[str, object]:
        return {
            "source_quote": "发现积水",
            "feedback": {
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": "standing_water",
                "value": value,
            },
        }

    request_count = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            answer = model_calls(
                (
                    "declare_request",
                    {
                        "field_id": "field-1",
                        "actions": ["observation"],
                        "events": [observation(False)],
                    },
                )
            )
        else:
            answer = tool_call_response(
                "propose_feedback",
                json.dumps(
                    {"field_id": "field-1", "events": [observation(True)]},
                    ensure_ascii=False,
                ),
            )
        return httpx.Response(200, json=answer)

    agent, _, _ = make_agent(tmp_path, respond)
    message = "发现积水，请记录"
    if with_independent_missing:
        message = "发现积水，虫情未知，请记录"
    try:
        result = agent.run("field-1", message, received_at=received_at)
    finally:
        agent.close()

    assert request_count == result.request_count == 2
    assert len(result.proposals) == 1
    assert result.proposals[0].events[0].feedback.value is True
    if with_independent_missing:
        assert result.business_outcome == "partial"
        assert "虫情" in result.text
    else:
        assert result.business_outcome == "awaiting_confirmation"
    assert "请核对现场观察结果与用户原话" not in result.text


@pytest.mark.parametrize(
    ("proposed_at", "accepted"),
    [
        ("2026-10-18T09:40:00+08:00", True),
        ("2026-10-25T09:40:00+08:00", False),
    ],
)
def test_explicit_observation_clock_must_match_source(
    tmp_path, proposed_at: str, accepted: bool
) -> None:
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["observation"],
                "events": [
                    {
                        "source_quote": "今天09:40发现积水",
                        "feedback": {
                            "kind": "observation",
                            "at": proposed_at,
                            "plot_id": "field-1",
                            "field": "standing_water",
                            "value": True,
                        },
                    }
                ],
            },
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            "今天09:40发现积水，请记录",
            received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert bool(result.proposals) is accepted


def test_unsupported_explicit_observation_clock_is_not_replaced_by_receipt(tmp_path) -> None:
    answer = model_calls(
        (
            "declare_request",
            {
                "field_id": "field-1",
                "actions": ["observation"],
                "events": [
                    {
                        "source_quote": "昨天9点发现积水",
                        "feedback": {
                            "kind": "observation",
                            "at": "2026-10-18T10:00:00+08:00",
                            "plot_id": "field-1",
                            "field": "standing_water",
                            "value": True,
                        },
                    }
                ],
            },
        )
    )
    agent, _, _ = make_agent(tmp_path, lambda _: httpx.Response(200, json=answer))
    try:
        result = agent.run(
            "field-1",
            "昨天9点发现积水，请记录",
            received_at=datetime(2026, 10, 18, 10, 0, tzinfo=ZONE),
        )
    finally:
        agent.close()
    assert result.proposals == []
