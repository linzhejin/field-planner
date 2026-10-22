"""The A/B arms must call a model and keep invalid output unscorable."""

from __future__ import annotations

import json

import httpx

from nongshi.config import Settings
from nongshi.d02_model_groups import _case_input, _retrieval, run_model_group


def _case() -> dict:
    return {
        "case_id": "D01",
        "received_at": "2026-10-21T18:00:00+08:00",
        "draft": {
            "field_id": "demo-1",
            "profile": {
                "id": "demo-1",
                "name": "模拟田",
                "province": "湖北",
                "province_confirmed": True,
                "crop": "油菜",
                "crop_confirmed": True,
                "stage": "苗期",
                "stage_confirmed": True,
                "field_type": "lowland_rice_stubble",
                "inspection_requested": True,
            },
        },
        "feedback_events": [],
        "expected_answer": {"secret": "must not enter prompt"},
    }


def _settings() -> Settings:
    return Settings(llm_api_key="test-key", llm_model="deepseek-flash")


def test_a_and_b_make_real_requests_with_same_case_and_b_retrieval() -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"tasks": [], "missing_fields": []})}}
                ],
                "usage": {"prompt_tokens": 23, "completion_tokens": 7},
            },
        )

    transport = httpx.MockTransport(respond)
    a = run_model_group(_case(), "A", settings=_settings(), transport=transport)
    b = run_model_group(_case(), "B", settings=_settings(), transport=transport)
    assert a["status"] == b["status"] == "executed", b["errors"]
    assert a["model_calls"] == b["model_calls"] == 1
    assert a["token_usage"] == b["token_usage"] == {"prompt": 23, "completion": 7}
    assert a["cost"] is None and b["cost"] is None
    assert a["tool_records"] == []
    assert b["tool_records"][0]["name"] == "search_rules"
    assert "must not enter prompt" not in json.dumps(requests, ensure_ascii=False)
    assert requests[0]["temperature"] == requests[1]["temperature"] == 0
    assert requests[0]["model"] == requests[1]["model"]
    assert "limited_support" in json.dumps(requests[1], ensure_ascii=False)


def test_bad_model_output_is_unscorable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not json"}}]},
        )
    )
    row = run_model_group(_case(), "A", settings=_settings(), transport=transport)
    assert row["status"] == "failed"
    assert row["scorable"] is False
    assert row["model_calls"] == 1
    assert row["first_result"] == "not json"


def test_model_input_only_contains_reached_steps_and_no_future_feedback() -> None:
    case = _case()
    case["feedback_events"] = [{"user_message": "future secret"}]
    case["messages"] = [
        {"sequence": 1, "user_message": "now"},
        {"sequence": 1, "at": "2026-10-22T18:00:00+08:00", "user_message": "future secret"},
    ]
    case["interaction_script"] = [
        {"step": 1, "at": "2026-10-21T18:00:00+08:00", "operation": "chat", "user_message": "now"},
        {
            "step": 2,
            "at": "2026-10-22T18:00:00+08:00",
            "operation": "commit_feedback",
            "user_message": "future secret",
        },
    ]
    first = _case_input(case, through_step=1)
    assert "future secret" not in json.dumps(first, ensure_ascii=False)
    assert len(first["interaction_script"]) == 1
    assert "feedback_events" not in first


def test_model_input_hides_future_weather_retry() -> None:
    case = _case()
    case["weather_fixture"] = {
        "status": "http_error",
        "attempts": [
            {"at": "2026-10-21T18:00:00+08:00", "status": "http_error"},
            {
                "at": "2026-10-21T18:05:00+08:00",
                "status": "success",
                "daily": [{"date": "2026-10-22", "secret": "future-weather"}],
            },
        ],
    }
    case["interaction_script"] = [
        {"step": 1, "at": "2026-10-21T18:00:00+08:00", "operation": "save_draft"},
        {"step": 2, "at": "2026-10-21T18:01:00+08:00", "operation": "chat"},
        {"step": 3, "at": "2026-10-21T18:06:00+08:00", "operation": "chat"},
    ]
    first = _case_input(case, through_step=2)
    later = _case_input(case, through_step=3)
    assert first["weather_fixture"]["status"] == "http_error"
    assert "future-weather" not in json.dumps(first)
    assert later["weather_fixture"]["status"] == "success"
    assert later["weather_fixture"]["daily"][0]["secret"] == "future-weather"


def test_model_input_applies_only_confirmed_resource_change() -> None:
    case = _case()
    case["draft"]["days"] = [
        {
            "day": "2026-10-22",
            "resource_minutes": 60,
            "intervals": [{"start": "09:00", "end": "10:00"}],
        }
    ]
    case["interaction_script"] = [
        {"step": 1, "at": "2026-10-21T18:00:00+08:00", "operation": "save_draft"},
        {
            "step": 2,
            "at": "2026-10-21T18:01:00+08:00",
            "operation": "chat",
            "user_message": "下午不在",
        },
        {
            "step": 3,
            "at": "2026-10-21T18:02:00+08:00",
            "operation": "confirm_feedback",
            "payload": {
                "events": [
                    {
                        "feedback": {
                            "kind": "resource",
                            "day": "2026-10-22",
                            "resource_minutes": 30,
                            "intervals": [{"start": "09:00", "end": "09:30"}],
                        }
                    }
                ]
            },
        },
    ]
    assert _case_input(case, through_step=2)["draft"]["days"][0]["resource_minutes"] == 60
    assert _case_input(case, through_step=3)["draft"]["days"][0]["resource_minutes"] == 30


def test_b_retrieval_contains_same_full_evidence_fields_as_agent_tool() -> None:
    payload, _ = _retrieval(_case())
    evidence = [row for hit in payload["hits"] for row in hit["evidence"]]
    assert evidence
    assert all(
        {"source_id", "pdf_pages", "quote", "usage_role", "project_use_limit"} <= row.keys()
        for row in evidence
    )
