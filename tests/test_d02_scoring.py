"""Public synthetic checks for the independent constraint scorer."""

from __future__ import annotations

from copy import deepcopy

from nongshi.d02_scoring import (
    _effective_inputs,
    _expected_feedback,
    _feedback_consistency,
    score_case,
    summarize_scores,
)


def test_confirmed_script_feedback_changes_scoring_facts() -> None:
    case = _case()
    event = {
        "kind": "resource",
        "day": "2026-10-20",
        "resource_minutes": 20,
        "intervals": [{"start": "09:00", "end": "09:20"}],
        "at": "2026-10-20T08:00:00+08:00",
    }
    case["interaction_script"] = [
        {
            "operation": "confirm_feedback",
            "payload": {"events": [{"feedback": event}]},
        }
    ]
    assert _expected_feedback(case) == [event]
    _profile, days = _effective_inputs(case)
    assert days["2026-10-20"]["resource_minutes"] == 20
    assert days["2026-10-20"]["intervals"][0]["end"] == "09:20"


def test_matching_completed_task_is_not_replaced_by_redundant_day() -> None:
    expected = {
        "kind": "completed",
        "task_id": "inspect-1",
        "at": "2026-10-20T09:30:00+08:00",
    }
    case = _case()
    case["interaction_script"] = [
        {"operation": "confirm_feedback", "payload": {"events": [{"feedback": expected}]}}
    ]
    actual = {**expected, "day": "2026-10-20"}
    record = _record()
    record["result"]["events"] = [{"feedback": actual}]
    assert _feedback_consistency(case, record)[0]["status"] == "pass"
    record["result"]["events"][0]["feedback"]["task_id"] = "another-task"
    assert _feedback_consistency(case, record)[0]["status"] == "fail"


def test_any_successful_agent_tool_is_a_valid_process_evidence() -> None:
    record = _record()
    record["group_code"] = "g3"
    record["steps"] = [
        {
            "step": 2,
            "operation": "chat",
            "status": "ok",
            "output": {"tools": ["read_field"], "tool_statuses": ["success"]},
        }
    ]
    contract = _contract()
    contract["c_only_assertions"] = [
        {
            "step": 2,
            "operation": "chat",
            "status": "ok",
            "required_tool": "*",
        }
    ]
    result = score_case(_case(), contract, record)
    assert any(
        check["check"] == "c_process" and check["status"] == "pass" for check in result["checks"]
    )


def test_invalid_input_never_scores_as_success_even_if_model_returned_json() -> None:
    record = _record()
    record["input_valid"] = False
    scored = score_case(_case(), _contract(), record)
    assert scored["verdict"] == "failed"
    assert any(
        check["check"] == "input_validity" and check["status"] == "fail"
        for check in scored["checks"]
    )


def _case() -> dict:
    return {
        "case_id": "PUBLIC-1",
        "draft": {
            "profile": {
                "province_confirmed": True,
                "crop_confirmed": True,
                "stage_confirmed": True,
                "inspection_duration_minutes": 30,
                "inspection_due_date": "2026-10-21",
            },
            "days": [
                {
                    "day": "2026-10-20",
                    "site_workable": True,
                    "weather_workable": True,
                    "resource_minutes": 60,
                    "intervals": [{"start": "09:00", "end": "10:00"}],
                }
            ],
        },
    }


def _contract() -> dict:
    return {
        "case_id": "PUBLIC-1",
        "required_tasks": [{"task_type": "inspection", "allowed_statuses": ["scheduled"]}],
        "forbidden_task_types": ["maintenance"],
        "citation_required_task_types": [],
    }


def _record(start: str = "09:00", end: str = "09:30") -> dict:
    return {
        "result_id": "demo-id",
        "group_code": "g1",
        "status": "executed",
        "elapsed_ms": 13,
        "result": {
            "tasks": [
                {
                    "task_id": "inspect-1",
                    "task_type": "inspection",
                    "schedule_status": "scheduled",
                    "start_at": f"2026-10-20T{start}:00+08:00",
                    "end_at": f"2026-10-20T{end}:00+08:00",
                    "duration_minutes": 30,
                }
            ],
            "missing_fields": [],
        },
    }


def test_alternative_legal_slot_passes_without_matching_reference_clock() -> None:
    first = score_case(_case(), _contract(), _record())
    second = score_case(_case(), _contract(), _record("09:30", "10:00"))
    assert first["verdict"] == second["verdict"] == "passed"


def test_overlap_and_unconfirmed_time_fail_hard_constraints() -> None:
    record = _record()
    other = deepcopy(record["result"]["tasks"][0])
    other.update(
        task_id="inspect-2",
        start_at="2026-10-20T09:15:00+08:00",
        end_at="2026-10-20T09:45:00+08:00",
    )
    record["result"]["tasks"].append(other)
    scored = score_case(_case(), _contract(), record)
    assert scored["verdict"] == "failed"
    assert any(item["type"] == "worker_overlap" for item in scored["checks"][1]["evidence"])
    case = _case()
    case["draft"]["days"][0]["site_workable"] = None
    assert score_case(case, _contract(), _record())["hard_violation_count"] == 1


def test_model_cannot_shorten_user_duration_to_fit_slot() -> None:
    record = _record("09:00", "09:20")
    record["result"]["tasks"][0]["duration_minutes"] = 20
    scored = score_case(_case(), _contract(), record)
    assert any(
        item["type"] == "duration_mismatch"
        for item in next(x for x in scored["checks"] if x["check"] == "hard_constraints")[
            "evidence"
        ]
    )


def test_unrequested_feedback_write_fails_even_when_no_feedback_was_expected() -> None:
    record = _record()
    record["group_code"] = "g3"
    record["result"]["events"] = [
        {
            "feedback": {
                "kind": "observation",
                "field": "standing_water",
                "value": True,
            }
        }
    ]
    scored = score_case(_case(), _contract(), record)
    assert any(
        row["check"] == "feedback_consistency" and row["status"] == "fail"
        for row in scored["checks"]
    )


def test_c_process_assertion_requires_the_declared_turn() -> None:
    record = _record()
    record["group_code"] = "g3"
    record["steps"] = [
        {
            "step": 2,
            "operation": "chat",
            "status": "ok",
            "output": {
                "tools": ["search_rules"],
                "tool_statuses": ["success"],
            },
        }
    ]
    contract = _contract()
    contract["c_only_assertions"] = [
        {
            "step": 4,
            "operation": "chat",
            "status": "ok",
            "required_tool": "search_rules",
        }
    ]
    scored = score_case(_case(), contract, record)
    assert any(row["check"] == "c_process" and row["status"] == "fail" for row in scored["checks"])


def test_expected_interface_failure_can_be_scored_without_semantic_claims() -> None:
    record = _record()
    record.update(status="failed", result={"tasks": [], "missing_fields": []})
    contract = {
        **_contract(),
        "required_tasks": [],
        "forbidden_task_types": ["inspection", "maintenance"],
        "allowed_execution_statuses": ["failed"],
        "semantic_required": False,
    }
    scored = score_case(_case(), contract, record)
    assert scored["verdict"] == "passed"


def test_no_active_agricultural_claim_has_no_semantic_review_denominator() -> None:
    record = _record()
    record["result"]["tasks"] = [
        {"task_id": "inspect-1", "task_type": "inspection", "schedule_status": "completed"},
        {"task_id": "maint-1", "task_type": "maintenance", "schedule_status": "not_generated"},
    ]
    contract = {
        **_contract(),
        "required_tasks": [{"task_type": "inspection", "allowed_statuses": ["completed"]}],
        "forbidden_task_types": [],
        "semantic_required": True,
    }
    scored = score_case(
        _case(), contract, record, semantic={"status": "not_applicable", "judgments": []}
    )
    assert scored["verdict"] == "passed"
    assert any(
        check["check"] == "semantic_support" and check["status"] == "not_applicable"
        for check in scored["checks"]
    )


def test_active_agricultural_claim_without_judgment_remains_pending() -> None:
    contract = {**_contract(), "semantic_required": True}
    scored = score_case(
        _case(), contract, _record(), semantic={"status": "not_applicable", "judgments": []}
    )
    assert scored["verdict"] == "insufficient_evidence"


def test_forbidden_scheduled_type_and_capacity_are_independent_checks() -> None:
    record = _record()
    contract = {
        **_contract(),
        "required_tasks": [],
        "forbidden_task_types": [],
        "forbidden_scheduled_task_types": ["inspection"],
        "max_scheduled_tasks": 0,
    }
    scored = score_case(_case(), contract, record)
    assert {row["check"] for row in scored["checks"] if row["status"] == "fail"} >= {
        "forbidden_scheduled_task",
        "scheduled_task_count",
    }


def test_completed_fact_does_not_rewrite_old_adopted_plan_after_cancel() -> None:
    record = _record()
    record["group_code"] = "g3"
    record["result"]["plan_stale"] = True
    record["result"]["events"] = [
        {
            "feedback": {
                "kind": "completed",
                "task_id": "inspect-1",
                "at": "2026-10-20T10:00:00+08:00",
            }
        }
    ]
    contract = _contract()
    scored = score_case(_case(), contract, record)
    assert not any(
        issue["type"] == "completion_rewritten"
        for issue in next(row for row in scored["checks"] if row["check"] == "hard_constraints")[
            "evidence"
        ]
    )


def test_missing_task_and_invalid_input_do_not_disappear() -> None:
    record = _record()
    record["status"] = "input_invalid"
    record["result"]["tasks"] = []
    scored = score_case(_case(), _contract(), record)
    assert scored["verdict"] == "failed"
    assert any(
        row["check"] == "input_validity" and row["status"] == "fail" for row in scored["checks"]
    )


def test_semantic_uncertainty_is_evidence_insufficient_and_cost_unknown() -> None:
    contract = _contract()
    contract["semantic_required"] = True
    scored = score_case(_case(), contract, _record())
    assert scored["verdict"] == "insufficient_evidence"
    stats = summarize_scores([{**scored, "group_code": "g1"}])
    assert stats["g1"]["cost_status"] == "unknown"


def test_feedback_resource_change_is_used_instead_of_old_draft() -> None:
    case = _case()
    case["interaction_script"] = [
        {
            "step": 1,
            "operation": "commit_feedback",
            "payload": {
                "events": [
                    {
                        "feedback": {
                            "kind": "resource",
                            "day": "2026-10-20",
                            "resource_minutes": 15,
                            "intervals": [{"start": "09:00", "end": "09:15"}],
                        }
                    }
                ]
            },
        }
    ]
    scored = score_case(case, _contract(), _record())
    assert scored["verdict"] == "failed"
    kinds = {item["type"] for item in scored["checks"][1]["evidence"]}
    assert {"outside_continuous_interval", "resource_minutes_exceeded"} <= kinds


def test_missing_or_fabricated_source_fails_citation_gate() -> None:
    contract = _contract()
    contract["citation_required_task_types"] = ["inspection"]
    missing = score_case(_case(), contract, _record())
    assert missing["citation_required"] == 1
    assert missing["citation_valid"] == 0
    assert missing["verdict"] == "failed"
    record = _record()
    record["result"]["tasks"][0]["evidence_summaries"] = [
        {
            "rule_id": "C31",
            "source_id": "S-FAKE",
            "pdf_pages": [1],
            "quote": "fabricated",
            "usage_role": "limited_support",
            "project_use_limit": "任何条件",
        }
    ]
    assert score_case(_case(), contract, record)["verdict"] == "failed"


def test_c_completion_event_cannot_be_rewritten() -> None:
    record = _record()
    record["group_code"] = "g3"
    record["result"] = {
        "current_plan_items": record["result"]["tasks"],
        "events": [{"feedback": {"kind": "completed", "task_id": "inspect-1"}}],
    }
    scored = score_case(_case(), _contract(), record)
    assert any(item["type"] == "completion_rewritten" for item in scored["checks"][1]["evidence"])


def test_unconfirmed_maintenance_purpose_cannot_be_invented() -> None:
    case = _case()
    case["draft"]["profile"].update(
        maintenance_need_confirmed=True,
        maintenance_problem_confirmed=True,
        maintenance_purpose=None,
        maintenance_duration_minutes=45,
    )
    record = _record("09:00", "09:45")
    record["result"]["tasks"][0].update(task_type="maintenance", duration_minutes=45)
    contract = _contract()
    contract["required_tasks"] = [
        {"task_type": "maintenance", "allowed_statuses": ["pending", "unscheduled"]}
    ]
    contract["forbidden_task_types"] = []
    scored = score_case(case, contract, record)
    assert any(
        item["type"] == "unconfirmed_maintenance" for item in scored["checks"][1]["evidence"]
    )


def test_confirmed_feedback_missing_from_c_events_fails() -> None:
    case = _case()
    case["interaction_script"] = [
        {
            "step": 1,
            "operation": "commit_feedback",
            "payload": {
                "events": [
                    {"feedback": {"kind": "observation", "field": "standing_water", "value": True}}
                ]
            },
        }
    ]
    record = _record()
    record["group_code"] = "g3"
    record["result"] = {"current_plan_items": record["result"]["tasks"], "events": []}
    scored = score_case(case, _contract(), record)
    assert any(
        item["check"] == "feedback_consistency" and item["status"] == "fail"
        for item in scored["checks"]
    )


def test_c_tool_name_without_successful_execution_fails_process_assertion() -> None:
    record = _record()
    record["group_code"] = "g3"
    record["steps"] = [
        {
            "operation": "chat",
            "status": "ok",
            "output": {"tools": ["search_rules"], "tool_statuses": ["invalid_arguments"]},
        }
    ]
    contract = _contract()
    contract["c_only_assertions"] = [
        {"operation": "chat", "status": "ok", "required_tool": "search_rules"}
    ]
    scored = score_case(_case(), contract, record)
    assert scored["verdict"] == "failed"
    assert any(
        item["check"] == "c_process" and item["status"] == "fail" for item in scored["checks"]
    )
