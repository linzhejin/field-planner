"""The public preparation transform must never grant scripted answers to the agent."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from test_d02_runner_scripted import _case

from nongshi.d02_prepare import normalize_candidate
from nongshi.d02_runner import _ScriptSession


def test_normalize_feedback_and_preview_into_agent_turns():
    case = _case()
    case["interaction_script"] = [
        {
            "step": 1,
            "at": case["received_at"],
            "operation": "save_draft",
            "user_message": "保存档案",
            "payload": {"draft_ref": "draft"},
        },
        {
            "step": 2,
            "at": case["received_at"],
            "operation": "evaluate",
            "user_message": "请查资料并预览",
            "payload": {"field_id": case["draft"]["field_id"]},
        },
        {
            "step": 3,
            "at": case["received_at"],
            "operation": "adopt",
            "user_message": "采用",
            "payload": {"plan_binding": "initial_preview"},
        },
        {
            "step": 4,
            "at": case["received_at"],
            "operation": "commit_feedback",
            "user_message": "我看见积水，请记录",
            "payload": {
                "events": [
                    {"feedback": {"kind": "observation", "field": "standing_water", "value": True}}
                ]
            },
        },
        {
            "step": 5,
            "at": case["received_at"],
            "operation": "preview",
            "user_message": "请预览新版",
            "payload": {},
        },
        {
            "step": 6,
            "at": case["received_at"],
            "operation": "cancel_preview",
            "user_message": "取消",
            "payload": {"preview_binding": "preview_5"},
        },
    ]
    original = deepcopy(case)
    prepared, changes = normalize_candidate(case)
    assert case == original
    assert [step["operation"] for step in prepared["interaction_script"]] == [
        "save_draft",
        "chat",
        "adopt",
        "chat",
        "confirm_feedback",
        "chat",
        "cancel_preview",
    ]
    feedback_chat = prepared["interaction_script"][3]
    confirmation = prepared["interaction_script"][4]
    assert "events" not in feedback_chat.get("payload", {})
    assert confirmation["payload"]["proposal_step"] == feedback_chat["step"]
    assert prepared["interaction_script"][-1]["payload"]["preview_binding"] == "preview_5"
    assert any(change["original_operation"] == "commit_feedback" for change in changes)


def test_normalize_does_not_invent_maintenance_purpose_or_weather_response():
    case = _case()
    case["draft"]["profile"]["maintenance_purpose"] = None
    case["weather_fixture"] = {
        "status": "http_error",
        "attempts": [
            {"attempt": 1, "at": case["received_at"], "status": "http_error"},
            {"attempt": 2, "at": "2026-10-20T18:13:00+08:00", "status": "success"},
        ],
    }
    case["interaction_script"].append(
        {
            "step": 99,
            "at": "2026-10-20T18:12:00+08:00",
            "operation": "request",
            "user_message": "重试天气",
            "payload": {"adapter_action": "retry_weather", "attempt": 2},
        }
    )
    prepared, _ = normalize_candidate(case)
    assert prepared["draft"]["profile"]["maintenance_purpose"] is None
    assert prepared["weather_fixture"] == case["weather_fixture"]
    assert prepared["interaction_script"][-1]["operation"] == "chat"


def test_companion_field_is_saved_separately_without_joint_schedule(tmp_path: Path):
    case = _case()
    companion = deepcopy(case["draft"])
    companion["field_id"] = "other-field"
    companion["profile"]["id"] = "other-field"
    case["companion_field_draft"] = companion
    session = _ScriptSession(case, database=tmp_path / "two.sqlite3")
    session.execute_step(case["interaction_script"][0])
    status, result, _ = session.execute_step(
        {
            "step": 3,
            "operation": "save_additional_field",
            "payload": {"draft_ref": "companion_field_draft"},
        }
    )
    assert status == "ok" and result["independent_schedule_only"] is True
    assert set(session.summary()["saved_field_ids"]) == {case["draft"]["field_id"], "other-field"}


def test_prepared_explicit_history_runs_after_injected_failure(tmp_path: Path):
    from nongshi.d02_runner import run_scripted_case

    case = _case()
    case["interaction_script"] = [
        {"step": 1, "at": case["received_at"], "operation": "save_draft"},
        {"step": 2, "at": case["received_at"], "operation": "chat", "user_message": "请预览"},
        {"step": 3, "at": case["received_at"], "operation": "read_history"},
    ]
    case["continue_after_failure"] = True
    case["adapter_failure_fixture"] = {
        "inject_at_step": 2,
        "operation": "chat",
        "response": {"status": "timeout", "output": None, "message": "公开模拟超时"},
    }
    outcome = run_scripted_case(case, database=tmp_path / "failed.sqlite3")
    assert [row["status"] for row in outcome["steps"]] == ["ok", "failed", "ok"]
    assert outcome["status"] == "failed"
    assert outcome["result"]["draft_version"] == 1
    assert outcome["result"]["plan_history_ids"] == []


def test_weather_attempt_is_not_visible_before_its_fixture_time(tmp_path: Path):
    from nongshi.d02_runner import _FixedWeatherService
    from nongshi.weather import WeatherServiceError

    case = _case()
    fixture = case["weather_fixture"]
    fixture["status"] = "http_error"
    fixture["attempts"] = [
        {
            "attempt": 1,
            "at": "2026-10-20T18:10:00+08:00",
            "status": "http_error",
            "http_status": 401,
        },
        {
            "attempt": 2,
            "at": "2026-10-20T18:13:00+08:00",
            "status": "success",
            "coverage_dates": fixture["coverage_dates"],
            "daily": fixture["daily"],
        },
    ]
    clock = [datetime.fromisoformat("2026-10-20T18:12:00+08:00")]
    session = _ScriptSession(case, database=tmp_path / "weather.sqlite3")
    session.execute_step(case["interaction_script"][0])
    service = _FixedWeatherService(case, session.store, now=lambda: clock[0])
    location = service.search_locations(fixture["location"]["query"])[0]
    selected = service.select_location([location], location.id)
    start = case["draft"]["days"][0]["day"]
    end = case["draft"]["days"][-1]["day"]
    with pytest.raises(WeatherServiceError):
        service.fetch_forecast(
            case["draft"]["field_id"],
            selected,
            datetime.fromisoformat(start).date(),
            datetime.fromisoformat(end).date(),
        )
    clock[0] = datetime.fromisoformat("2026-10-20T18:13:00+08:00")
    snapshot = service.fetch_forecast(
        case["draft"]["field_id"],
        selected,
        datetime.fromisoformat(start).date(),
        datetime.fromisoformat(end).date(),
    )
    assert snapshot.coverage_start.isoformat() == start


def test_ambiguous_location_fixture_returns_both_candidates(tmp_path: Path):
    from nongshi.d02_runner import _FixedWeatherService
    from nongshi.repository_v3 import WorkbenchStoreV3

    case = _case()
    case["location_lookup_fixture"] = {
        "query": "清河镇",
        "candidates": [
            {
                "candidate_id": "hb",
                "query": "清河镇",
                "display_name": "湖北清河镇",
                "latitude": 31.0,
                "longitude": 112.0,
                "resolution": "township",
            },
            {
                "candidate_id": "hn",
                "query": "清河镇",
                "display_name": "河南清河镇",
                "latitude": 33.0,
                "longitude": 113.0,
                "resolution": "township",
            },
        ],
    }
    service = _FixedWeatherService(
        case,
        WorkbenchStoreV3(tmp_path / "locations.sqlite3"),
        now=lambda: datetime.fromisoformat(case["received_at"]),
    )
    assert [row.id for row in service.search_locations("清河镇")] == ["hb", "hn"]
