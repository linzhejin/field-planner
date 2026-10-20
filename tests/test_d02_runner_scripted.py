"""正式候选脚本运行器：合成案例覆盖脚本执行、天气注入、失败与能力缺口。"""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from nongshi.d02_runner import run_scripted_case, validate_case

START = date(2026, 10, 20)


def _days() -> list[dict[str, Any]]:
    return [
        {
            "day": (START + timedelta(days=index)).isoformat(),
            "site_workable": True,
            "weather_workable": True,
            "weather_source": "synthetic",
            "weather_snapshot_at": "2026-10-20T18:10:00+08:00",
            "resource_minutes": 120,
            "intervals": [{"start": "09:00:00", "end": "11:00:00"}],
        }
        for index in range(7)
    ]


def _weather(status: str = "success") -> dict[str, Any]:
    coverage = [(START + timedelta(days=index)).isoformat() for index in range(7)]
    location = {
        "query": "模拟测试县",
        "display_name": "模拟位置·测试县",
        "latitude": 30.71,
        "longitude": 112.59,
        "timezone": "Asia/Shanghai",
        "resolution": "county",
    }
    return {
        "data_mode": "synthetic",
        "status": status,
        "source": "synthetic_weather_fixture_v0.1",
        "requested_at": "2026-10-20T18:10:00+08:00",
        "fetched_at": "2026-10-20T18:10:00+08:00",
        "location": location,
        "coverage_dates": coverage,
        "units": {"precipitation": "mm/day"},
        "daily": [
            {
                "date": day,
                "precipitation": 0.0,
                "temperature_min": 11,
                "temperature_max": 19,
                "wind_speed": 1.2,
            }
            for day in coverage
        ],
    }


def _script() -> list[dict[str, Any]]:
    return [
        {"step": 1, "at": "2026-10-20T18:10:00+08:00", "operation": "save_draft", "payload": {}},
        {"step": 2, "at": "2026-10-20T18:11:00+08:00", "operation": "evaluate", "payload": {}},
        {
            "step": 3,
            "at": "2026-10-20T18:12:00+08:00",
            "operation": "adopt",
            "payload": {"plan_binding": "initial_preview"},
        },
        {
            "step": 4,
            "at": "2026-10-20T10:00:00+08:00",
            "operation": "commit_feedback",
            "payload": {
                "events": [
                    {
                        "event_id": "T01-evt-4",
                        "feedback": {
                            "kind": "completed",
                            "task_id": "sim-t01-I",
                            "plot_id": "sim-t01",
                            "at": "2026-10-20T10:00:00+08:00",
                        },
                        "source": "user",
                        "correction_of": None,
                        "retracts": False,
                    }
                ]
            },
        },
        {"step": 5, "at": "2026-10-21T10:00:00+08:00", "operation": "preview", "payload": {}},
        {
            "step": 6,
            "at": "2026-10-21T10:01:00+08:00",
            "operation": "cancel_preview",
            "payload": {"preview_binding": "preview_5"},
        },
        {"step": 7, "at": "2026-10-21T10:02:00+08:00", "operation": "read_history", "payload": {}},
    ]


def _case(
    *,
    weather_status: str = "success",
    script: list[dict[str, Any]] | None = None,
    reversed_dates: bool = False,
) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "id": "sim-t01",
        "name": "脚本运行测试地块",
        "province": "湖北",
        "province_confirmed": True,
        "crop": "油菜",
        "crop_confirmed": True,
        "stage": "苗期",
        "stage_confirmed": True,
        "field_type": "lowland_rice_stubble",
        "inspection_requested": True,
        "inspection_duration_minutes": 30,
    }
    if reversed_dates:
        profile["maintenance_earliest_date"] = (START + timedelta(days=3)).isoformat()
        profile["maintenance_due_date"] = (START + timedelta(days=2)).isoformat()
    return {
        "case_id": "T01",
        "title": "合成脚本案例",
        "received_at": "2026-10-20T18:10:00+08:00",
        "timezone": "Asia/Shanghai",
        "draft": {
            "schema_version": 3,
            "field_id": "sim-t01",
            "mode": "demo",
            "profile": profile,
            "location": _weather()["location"],
            "start_date": START.isoformat(),
            "days": _days(),
            "input_source": "demo",
        },
        "weather_fixture": _weather(weather_status),
        "interaction_script": script if script is not None else _script(),
        "stage_execution_policy": {
            "task_ids": {"inspection": "sim-t01-I", "maintenance": "sim-t01-M"}
        },
    }


def test_scripted_case_runs_full_flow_and_keeps_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        outcome = run_scripted_case(_case(), database=Path(directory) / "t01.sqlite3")

    assert outcome["status"] == "executed"
    assert [row["status"] for row in outcome["steps"]] == ["ok"] * 7
    steps = {row["step"]: row for row in outcome["steps"]}
    assert steps[2]["note"] is None
    assert steps[3]["output"]["adopted_version_id"]
    assert steps[4]["output"]["fact_version"] == 1
    assert steps[6]["output"]["status"] == "cancelled"
    result = outcome["result"]
    assert result["weather_injected"] is True
    assert result["event_count"] == 1
    assert result["task_id_map"]["inspection"] == "sim-t01-I"
    assert len(result["plan_history_ids"]) == 1


def test_weather_failure_fixture_is_not_injected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        outcome = run_scripted_case(
            _case(weather_status="http_error"), database=Path(directory) / "t01.sqlite3"
        )

    assert outcome["status"] == "executed"
    assert outcome["result"]["weather_injected"] is False
    assert any("http_error" in note for note in outcome["notes"])


def test_invalid_draft_stops_script_and_keeps_remaining_steps() -> None:
    with tempfile.TemporaryDirectory() as directory:
        outcome = run_scripted_case(
            _case(reversed_dates=True), database=Path(directory) / "t01.sqlite3"
        )

    assert outcome["status"] == "failed"
    assert outcome["steps"][0]["status"] == "failed"
    assert {row["status"] for row in outcome["steps"][1:]} == {"skipped_after_failure"}
    assert outcome["result"] == {}


def test_validate_reports_invalid_dates_and_capability_gaps() -> None:
    invalid = validate_case(_case(reversed_dates=True))
    assert invalid["status"] == "invalid"
    assert any(problem["code"] == "draft_invalid" for problem in invalid["problems"])

    gap_script = [
        {"step": 1, "at": "2026-10-20T18:10:00+08:00", "operation": "save_draft", "payload": {}},
        {
            "step": 2,
            "at": "2026-10-21T10:00:00+08:00",
            "operation": "request",
            "payload": {"new_task_request": {"task_id": "sim-t01-M2"}},
        },
    ]
    gap = validate_case(_case(script=gap_script))
    assert gap["status"] == "warning"
    assert any(item["code"] == "unsupported:request" for item in gap["warnings"])


def test_unsupported_and_model_steps_are_recorded_as_partial() -> None:
    script = [
        {"step": 1, "at": "2026-10-20T18:10:00+08:00", "operation": "save_draft", "payload": {}},
        {
            "step": 2,
            "at": "2026-10-21T10:00:00+08:00",
            "operation": "request",
            "payload": {"new_task_request": {"task_id": "sim-t01-M2"}},
        },
        {
            "step": 3,
            "at": "2026-10-21T10:01:00+08:00",
            "operation": "chat",
            "payload": {"feedback_parse_request": True},
        },
    ]
    with tempfile.TemporaryDirectory() as directory:
        outcome = run_scripted_case(_case(script=script), database=Path(directory) / "t01.sqlite3")

    assert outcome["status"] == "partial"
    assert [row["status"] for row in outcome["steps"]] == [
        "ok",
        "unsupported_capability",
        "pending_model_authorization",
    ]
    assert outcome["result"]["draft_version"] == 1
