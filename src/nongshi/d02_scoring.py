"""Independent, constraint-based scoring for sealed agricultural-agent trials.

This module reads recorded outputs and frozen case constraints. It does not call
the planner, field evaluator, or plan service to decide whether an answer is right.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from copy import deepcopy
from datetime import datetime, time
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

from nongshi.rules import RuleCatalog, load_rule_catalog

PASS = "pass"
FAIL = "fail"
NA = "not_applicable"
PENDING = "pending_review"
ACTIVE = {"scheduled", "pending", "unscheduled", "completed"}


def _check(name: str, status: str, reason: str, evidence: Any = None) -> dict[str, Any]:
    return {"check": name, "status": status, "reason": reason, "evidence": evidence}


def _moment(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise ValueError("时间必须带时区")
    return parsed


def _tasks(record: dict[str, Any]) -> list[dict[str, Any]]:
    common = record.get("common_result") or {}
    if isinstance(common.get("tasks"), list):
        return common["tasks"]
    result = record.get("result") or {}
    if isinstance(result.get("tasks"), list):
        return result["tasks"]
    if isinstance(result.get("current_plan_items"), list):
        return result["current_plan_items"]
    return []


def _expected_feedback(case: dict[str, Any]) -> list[dict[str, Any]]:
    feedback: list[dict[str, Any]] = []
    for step in case.get("interaction_script") or []:
        if step.get("operation") in {"commit_feedback", "confirm_feedback"}:
            feedback.extend(
                item.get("feedback") or {}
                for item in (step.get("payload") or {}).get("events") or []
            )
        elif step.get("operation") == "request":
            demand = (step.get("payload") or {}).get("new_task_request") or {}
            if demand:
                feedback.extend(
                    {"kind": "observation", "field": field, "value": demand[field]}
                    for field in (
                        "maintenance_problem_confirmed",
                        "maintenance_purpose",
                        "maintenance_need_confirmed",
                    )
                    if field in demand
                )
    if not case.get("interaction_script"):
        feedback.extend(case.get("feedback_events") or [])
    return feedback


def _effective_inputs(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    draft = case.get("draft") or {}
    profile = deepcopy(draft.get("profile") or {})
    days = {str(day["day"]): deepcopy(day) for day in draft.get("days") or []}
    for event in _expected_feedback(case):
        kind = event.get("kind")
        if kind == "observation" and event.get("field"):
            profile[event["field"]] = event.get("value")
        elif kind in {"workability", "resource"}:
            day = days.get(str(event.get("day")))
            if day is None:
                continue
            fields = (
                ("site_workable", "weather_workable")
                if kind == "workability"
                else ("resource_minutes", "intervals")
            )
            for field in fields:
                if field in event:
                    day[field] = event[field]
    return profile, days


def _feedback_consistency(case: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    expected = _expected_feedback(case)
    result = record.get("result") or {}
    actual = [event.get("feedback") or {} for event in result.get("events") or []]
    if not expected:
        return [
            _check(
                "feedback_consistency",
                FAIL if actual else NA,
                "出现未经脚本确认的事实" if actual else "案例无反馈",
                actual if actual else None,
            )
        ]
    if not actual:
        return [_check("feedback_consistency", FAIL, "已确认反馈未保存")]
    aliases = result.get("task_id_map") or {}

    def signature(event: dict[str, Any]) -> tuple[str, str, str]:
        task_id = event.get("task_id")
        if task_id is None and event.get("task_ref") == "$scheduled_maintenance":
            task_id = aliases.get("maintenance")
        for task_type, declared in (
            (case.get("stage_execution_policy") or {}).get("task_ids", {}).items()
        ):
            if task_id == declared:
                task_id = aliases.get(task_type, declared)
        target = event.get("field") or event.get("day") or task_id or ""
        kind = str(event.get("kind"))
        if kind == "resource":
            intervals = [
                (
                    time.fromisoformat(str(span["start"])).isoformat(timespec="minutes"),
                    time.fromisoformat(str(span["end"])).isoformat(timespec="minutes"),
                )
                for span in event.get("intervals") or []
            ]
            detail = json.dumps(
                {"minutes": event.get("resource_minutes"), "intervals": intervals},
                sort_keys=True,
            )
        elif kind == "workability":
            detail = json.dumps(
                {
                    "site": event.get("site_workable"),
                    "weather": event.get("weather_workable"),
                },
                sort_keys=True,
            )
        elif kind == "completed":
            detail = str(event.get("at"))
        else:
            detail = str(event.get("value"))
        return kind, str(target), detail

    expected_counts = Counter(map(signature, expected))
    actual_counts = Counter(map(signature, actual))
    missing = expected_counts - actual_counts
    unexpected = actual_counts - expected_counts
    evidence = {
        "missing": [{"signature": list(key), "count": count} for key, count in missing.items()],
        "unexpected": [
            {"signature": list(key), "count": count} for key, count in unexpected.items()
        ],
    }
    return [
        _check(
            "feedback_consistency",
            FAIL if missing or unexpected else PASS,
            "反馈事实核对",
            evidence,
        )
    ]


def _hard_constraints(
    tasks: list[dict[str, Any]], case: dict[str, Any], record: dict[str, Any]
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    profile, days = _effective_inputs(case)
    scheduled_by_day: dict[str, list[tuple[datetime, datetime, str]]] = {}
    scheduled_types: Counter[str] = Counter()
    seen_ids: set[str] = set()
    by_id = {str(row.get("task_id")): row for row in tasks if row.get("task_id")}
    for row in tasks:
        task_id = str(row.get("task_id") or row.get("task_type") or "unknown")
        if row.get("task_id") in seen_ids:
            violations.append({"type": "duplicate_task_id", "task_id": task_id})
        if row.get("task_id"):
            seen_ids.add(row["task_id"])
        if row.get("schedule_status") != "scheduled":
            continue
        scheduled_types[str(row.get("task_type"))] += 1
        if any(
            profile.get(f"{field}_confirmed") is not True for field in ("province", "crop", "stage")
        ):
            violations.append({"type": "unconfirmed_profile", "task_id": task_id})
        if row.get("task_type") == "maintenance" and not (
            profile.get("maintenance_need_confirmed") is True
            and profile.get("maintenance_problem_confirmed") is True
            and profile.get("maintenance_purpose") == "clear_existing_ditch"
        ):
            violations.append({"type": "unconfirmed_maintenance", "task_id": task_id})
        try:
            start, end = _moment(row.get("start_at")), _moment(row.get("end_at"))
        except (TypeError, ValueError):
            violations.append({"type": "invalid_time", "task_id": task_id})
            continue
        local_start = start.astimezone(ZoneInfo("Asia/Shanghai"))
        local_end = end.astimezone(ZoneInfo("Asia/Shanghai"))
        if end <= start or local_start.date() != local_end.date():
            violations.append({"type": "invalid_duration", "task_id": task_id})
            continue
        task_type = row.get("task_type")
        duration = profile.get(f"{task_type}_duration_minutes")
        actual_minutes = (end - start).total_seconds() / 60
        if (
            duration is None
            or actual_minutes != duration
            or actual_minutes != int(actual_minutes)
            or (row.get("duration_minutes") is not None and row["duration_minutes"] != duration)
        ):
            violations.append({"type": "duration_mismatch", "task_id": task_id})
        local_day = local_start.date().isoformat()
        day = days.get(local_day)
        if day is None:
            violations.append({"type": "outside_seven_days", "task_id": task_id})
            continue
        if day.get("site_workable") is not True or day.get("weather_workable") is not True:
            violations.append({"type": "unconfirmed_workability", "task_id": task_id})
        intervals = day.get("intervals") or []
        if not any(
            time.fromisoformat(str(span["start"])) <= local_start.timetz().replace(tzinfo=None)
            and local_end.timetz().replace(tzinfo=None) <= time.fromisoformat(str(span["end"]))
            for span in intervals
        ):
            violations.append({"type": "outside_continuous_interval", "task_id": task_id})
        earliest = profile.get(f"{task_type}_earliest_date")
        due = profile.get(f"{task_type}_due_date")
        if earliest and local_day < str(earliest):
            violations.append({"type": "before_earliest_date", "task_id": task_id})
        if due and local_day > str(due):
            violations.append({"type": "after_deadline", "task_id": task_id})
        scheduled_by_day.setdefault(local_day, []).append((start, end, task_id))
    for local_day, slots in scheduled_by_day.items():
        slots.sort()
        for left, right in zip(slots, slots[1:], strict=False):
            if left[1] > right[0]:
                violations.append({"type": "worker_overlap", "task_ids": [left[2], right[2]]})
        resource = days[local_day].get("resource_minutes")
        if (
            resource is None
            or sum((end - start).total_seconds() / 60 for start, end, _ in slots) > resource
        ):
            violations.append({"type": "resource_minutes_exceeded", "day": local_day})
    for task_type, count in scheduled_types.items():
        if count > 1:
            violations.append({"type": "duplicate_active_issue", "task_type": task_type})
    for row in tasks:
        if row.get("task_type") != "maintenance" or row.get("schedule_status") != "scheduled":
            continue
        for prerequisite in profile.get("maintenance_prerequisites") or []:
            prior = by_id.get(prerequisite)
            if prior is None or prior.get("schedule_status") not in {"scheduled", "completed"}:
                violations.append({"type": "missing_prerequisite", "task_id": row.get("task_id")})
                continue
            if prior.get("schedule_status") == "scheduled" and prior.get("end_at"):
                try:
                    if _moment(prior["end_at"]) > _moment(row["start_at"]):
                        violations.append(
                            {"type": "prerequisite_after_task", "task_id": row.get("task_id")}
                        )
                except (TypeError, ValueError):
                    violations.append(
                        {"type": "invalid_prerequisite_time", "task_id": row.get("task_id")}
                    )
    result = record.get("result") or {}
    if record.get("group_code") == "g3" or result.get("plan_history"):
        events = result.get("events") or []
        for event in events:
            feedback = event.get("feedback") or {}
            if feedback.get("kind") != "completed":
                continue
            completed_id = feedback.get("task_id")
            current = by_id.get(completed_id)
            if (
                current
                and current.get("schedule_status") != "completed"
                and result.get("plan_stale") is not True
            ):
                violations.append({"type": "completion_rewritten", "task_id": completed_id})
        history = result.get("plan_history") or []
        if len(history) >= 2:
            previous = history[-2]
            prior_rows = {item.get("task_id"): item for item in previous.get("items") or []}
            previous_created = _moment(previous["created_at"])
            new_facts = [
                _moment(event["feedback"]["at"])
                for event in events
                if event.get("feedback") and _moment(event["feedback"]["at"]) >= previous_created
            ]
            feedback_at = max(new_facts) if new_facts else None
            for task_id, row in by_id.items():
                old = prior_rows.get(task_id)
                if old and old.get("schedule_status") == "completed":
                    if row.get("schedule_status") != "completed" or row.get("end_at") != old.get(
                        "end_at"
                    ):
                        violations.append({"type": "completed_history_changed", "task_id": task_id})
                if feedback_at and row.get("schedule_status") == "scheduled":
                    moved = old is None or old.get("start_at") != row.get("start_at")
                    if moved and row.get("start_at") and _moment(row["start_at"]) < feedback_at:
                        violations.append({"type": "scheduled_before_feedback", "task_id": task_id})
    return violations


def _source_index(catalog: RuleCatalog) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (row["rule_id"], row["source_id"]): row
        for rule_id in catalog.cards
        for row in catalog.evidence_for([rule_id])
    }


def _evidence_checks(
    tasks: list[dict[str, Any]],
    contract: dict[str, Any],
    catalog: RuleCatalog,
) -> tuple[list[dict[str, Any]], int, int]:
    checks: list[dict[str, Any]] = []
    index = _source_index(catalog)
    required_types = set(contract.get("citation_required_task_types") or [])
    cited = total = 0
    for row in tasks:
        if row.get("schedule_status") == "scheduled":
            claimed_direct = set(row.get("current_evidence_rule_ids") or []) | set(
                row.get("evidence_rule_ids") or []
            )
            deferred = claimed_direct & catalog.deferred_rule_ids
            if deferred:
                checks.append(
                    _check(
                        "deferred_as_support", FAIL, "暂缓资料被作为直接处置依据", sorted(deferred)
                    )
                )
        if row.get("schedule_status") != "scheduled" or row.get("task_type") not in required_types:
            continue
        total += 1
        direct: list[dict[str, Any]] = []
        for item in row.get("evidence_summaries") or []:
            key = (str(item.get("rule_id")), str(item.get("source_id")))
            original = index.get(key)
            role = item.get("usage_role")
            if role == "historical_reference":
                if key[0] in (row.get("current_evidence_rule_ids") or []):
                    checks.append(
                        _check("deferred_as_support", FAIL, "暂缓资料被标为直接支撑", key)
                    )
                continue
            if role != "limited_support":
                continue
            valid = (
                original is not None
                and key[0] not in catalog.deferred_rule_ids
                and bool(set(item.get("pdf_pages") or []) & set(original.get("pdf_pages") or []))
                and bool(item.get("quote"))
                and str(item.get("quote")) == str(original.get("quote"))
                and bool(item.get("project_use_limit"))
            )
            if valid:
                direct.append(item)
            else:
                checks.append(_check("citation_locator", FAIL, "来源、页码、原文或用途无效", key))
        if direct:
            cited += 1
        else:
            checks.append(
                _check("required_citation", FAIL, "已安排事项缺少有效直接依据", row.get("task_id"))
            )
    if not checks:
        checks.append(_check("citation_integrity", PASS if total else NA, "引用已核对"))
    return checks, cited, total


def score_case(
    case: dict[str, Any],
    contract: dict[str, Any],
    record: dict[str, Any],
    *,
    catalog: RuleCatalog | None = None,
    semantic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one frozen first attempt; unknowns remain visible rather than disappearing."""
    if case.get("case_id") != contract.get("case_id"):
        raise ValueError("案例与约束编号不一致")
    catalog = catalog or load_rule_catalog()
    checks: list[dict[str, Any]] = []
    status = record.get("status")
    if status == "input_invalid" or record.get("input_valid") is False:
        checks.append(_check("input_validity", FAIL, "输入无效，不能算执行成功"))
    elif status != "executed":
        allowed = status in set(contract.get("allowed_execution_statuses") or [])
        checks.append(_check("execution", PASS if allowed else FAIL, f"执行状态：{status}"))
    else:
        checks.append(_check("execution", PASS, "运行完成"))
    tasks = _tasks(record)
    violations = _hard_constraints(tasks, case, record)
    checks.append(
        _check(
            "hard_constraints",
            FAIL if violations else PASS,
            f"违规 {len(violations)} 项",
            violations,
        )
    )
    for requirement in contract.get("required_tasks") or []:
        matching = [
            row
            for row in tasks
            if row.get("task_type") == requirement["task_type"]
            and row.get("schedule_status") in requirement.get("allowed_statuses", [])
        ]
        checks.append(
            _check(
                "required_task",
                PASS if len(matching) >= requirement.get("min_count", 1) else FAIL,
                f"{requirement['task_type']} 满足 {len(matching)} 项",
            )
        )
    for task_type in contract.get("forbidden_task_types") or []:
        matching = [
            row
            for row in tasks
            if row.get("task_type") == task_type and row.get("schedule_status") in ACTIVE
        ]
        checks.append(_check("forbidden_task", FAIL if matching else PASS, task_type))
    for task_type in contract.get("forbidden_scheduled_task_types") or []:
        scheduled = [
            row
            for row in tasks
            if row.get("task_type") == task_type and row.get("schedule_status") == "scheduled"
        ]
        checks.append(
            _check(
                "forbidden_scheduled_task",
                FAIL if scheduled else PASS,
                task_type,
                [row.get("task_id") for row in scheduled],
            )
        )
    if "min_scheduled_tasks" in contract or "max_scheduled_tasks" in contract:
        count = sum(row.get("schedule_status") == "scheduled" for row in tasks)
        minimum = contract.get("min_scheduled_tasks", 0)
        maximum = contract.get("max_scheduled_tasks", float("inf"))
        checks.append(
            _check(
                "scheduled_task_count",
                PASS if minimum <= count <= maximum else FAIL,
                f"已安排 {count} 项，允许 {minimum} 至 {maximum} 项",
                count,
            )
        )
    missing = set((record.get("common_result") or {}).get("missing_fields") or [])
    missing.update((record.get("result") or {}).get("missing_fields") or [])
    missing.update(field for row in tasks for field in row.get("missing_fields") or [])
    for field in contract.get("required_missing_fields") or []:
        checks.append(_check("missing_field", PASS if field in missing else FAIL, field))
    citation_checks, cited, citation_total = _evidence_checks(tasks, contract, catalog)
    checks.extend(citation_checks)
    if record.get("group_code") == "g3":
        checks.extend(_feedback_consistency(case, record))
    semantic_supported = semantic_total = serious_unsupported = 0
    if contract.get("semantic_required"):
        if semantic is None:
            checks.append(_check("semantic_support", PENDING, "关键语义尚未辅助复核"))
        else:
            judgments = semantic.get("judgments") or []
            verdicts = [item.get("verdict") for item in judgments]
            semantic_total = len(verdicts)
            semantic_supported = verdicts.count("supported")
            serious_unsupported = sum(
                item.get("verdict") == "unsupported" and item.get("severity") == "serious"
                for item in judgments
            )
            if serious_unsupported:
                checks.append(_check("semantic_support", FAIL, "严重无依据建议", judgments))
            elif not verdicts or "uncertain" in verdicts:
                checks.append(_check("semantic_support", PENDING, "语义证据不足", judgments))
            else:
                checks.append(_check("semantic_support", PASS, "辅助复核已判定", judgments))
    if contract.get("c_only_assertions") and record.get("group_code") == "g3":
        steps = record.get("steps") or []
        for requirement in contract["c_only_assertions"]:
            matched = False
            for step in steps:
                if (
                    (
                        requirement.get("step") is not None
                        and step.get("step") != requirement["step"]
                    )
                    or step.get("operation") != requirement["operation"]
                    or step.get("status") != requirement["status"]
                ):
                    continue
                if requirement.get("failure_status") is not None and (
                    (step.get("output") or {}).get("failure_status")
                    != requirement["failure_status"]
                ):
                    continue
                tool = requirement.get("required_tool")
                if not tool:
                    matched = True
                    break
                output = step.get("output") or {}
                if any(
                    (tool == "*" or name == tool) and status == "success"
                    for name, status in zip(
                        output.get("tools") or [],
                        output.get("tool_statuses") or [],
                        strict=False,
                    )
                ):
                    matched = True
                    break
            checks.append(_check("c_process", PASS if matched else FAIL, str(requirement)))
    failed = any(item["status"] == FAIL for item in checks)
    pending = any(item["status"] == PENDING for item in checks)
    verdict = "failed" if failed else "insufficient_evidence" if pending else "passed"
    return {
        "result_id": record.get("result_id"),
        "case_id": case["case_id"],
        "verdict": verdict,
        "checks": checks,
        "hard_violation_count": len(violations),
        "citation_valid": cited,
        "citation_required": citation_total,
        "semantic_supported": semantic_supported,
        "semantic_total": semantic_total,
        "serious_unsupported": serious_unsupported,
        "elapsed_ms": record.get("elapsed_ms"),
        "model_calls": record.get("model_calls"),
        "tool_executions": len(record.get("tool_records") or []),
        "token_usage": record.get("token_usage"),
        "cost": record.get("cost"),
        "cost_status": record.get("cost_status", "unknown"),
    }


def summarize_scores(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_group: dict[str, dict[str, Any]] = {}
    for group in sorted({str(row.get("group_code")) for row in rows}):
        selected = [row for row in rows if str(row.get("group_code")) == group]
        counts = Counter(row["verdict"] for row in selected)
        dimensions: dict[str, Counter[str]] = {}
        for row in selected:
            for check in row["checks"]:
                dimensions.setdefault(check["check"], Counter())[check["status"]] += 1
        elapsed = sorted(
            float(row["elapsed_ms"]) for row in selected if row.get("elapsed_ms") is not None
        )
        by_group[group] = {
            "trials": len(selected),
            "verdicts": dict(counts),
            "dimensions": {
                name: {
                    "counts": dict(statuses),
                    "pass_rate": statuses[PASS] / (statuses[PASS] + statuses[FAIL])
                    if statuses[PASS] + statuses[FAIL]
                    else None,
                }
                for name, statuses in dimensions.items()
            },
            "hard_violations": sum(row["hard_violation_count"] for row in selected),
            "citation_valid": sum(row["citation_valid"] for row in selected),
            "citation_required": sum(row["citation_required"] for row in selected),
            "semantic_supported": sum(row["semantic_supported"] for row in selected),
            "semantic_total": sum(row["semantic_total"] for row in selected),
            "serious_unsupported": sum(row["serious_unsupported"] for row in selected),
            "elapsed_median_ms": statistics.median(elapsed) if elapsed else None,
            "elapsed_p95_ms": elapsed[ceil(0.95 * len(elapsed)) - 1] if elapsed else None,
            "model_calls": sum(row.get("model_calls") or 0 for row in selected),
            "tool_executions": sum(row.get("tool_executions") or 0 for row in selected),
            "prompt_tokens": sum(
                (row.get("token_usage") or {}).get("prompt") or 0 for row in selected
            )
            if all((row.get("token_usage") or {}).get("prompt") is not None for row in selected)
            else None,
            "completion_tokens": sum(
                (row.get("token_usage") or {}).get("completion") or 0 for row in selected
            )
            if all((row.get("token_usage") or {}).get("completion") is not None for row in selected)
            else None,
            "cost_status": "unknown"
            if any(row.get("cost") is None for row in selected)
            else "known",
        }
    return by_group
