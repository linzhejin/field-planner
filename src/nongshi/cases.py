"""Recompute public development cases and produce a bounded audit report."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from nongshi.planning_v2 import apply_feedback_v2, build_plan_v2
from nongshi.rules import DELIVERY_PACKAGE, RULES_ROOT, load_rule_catalog
from nongshi.schema_v2 import (
    DayWindow,
    FeedbackV2,
    FieldProfile,
    PlanRequestV2,
    PlanVersionV2,
    TimeWindow,
    WorkabilityConfirmation,
)

CASE_DIR = DELIVERY_PACKAGE / "结构化交付"
REFERENCE_FILE = CASE_DIR / "三块地与七天计划.json"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _ref_input() -> tuple[dict[str, Any], dict[str, Any]]:
    data = _read_json(REFERENCE_FILE)
    return data, {version["version"]: version for version in data["versions"]}


def make_reference_request() -> PlanRequestV2:
    data, versions = _ref_input()
    start = date.fromisoformat(data["dates"][0])
    fields = {field["id"]: field for field in data["fields"]}
    tasks = data["tasks"]
    plot_profiles: list[FieldProfile] = []
    for plot_id, raw in fields.items():
        field_type = "lowland_rice_stubble" if plot_id in {"A", "B"} else "dry_hilly"
        task_i = tasks.get(f"{plot_id}-I")
        task_m = tasks.get(f"{plot_id}-M")
        # C-I and A-I are requested in the example; B-I is an explicit check candidate.
        inspect = task_i is not None
        maintenance_need = raw.get("maintenance_need_confirmed")
        plot_profiles.append(
            FieldProfile(
                id=plot_id,
                name=f"{plot_id}地（模拟）",
                province=raw.get("province"),
                province_confirmed=raw.get("province") is not None,
                stage="苗期" if raw.get("stage") == "seedling" else raw.get("stage"),
                stage_confirmed=raw.get("stage") is not None,
                field_type=field_type,
                area_mu=raw.get("area_mu"),
                inspection_requested=inspect,
                maintenance_need_confirmed=maintenance_need,
                maintenance_problem_confirmed=(
                    True
                    if raw.get("ditch_connectivity") == "impaired"
                    or raw.get("standing_water") is True
                    else None
                ),
                moisture={"wet": "wet", "dry": "dry", "normal": "normal"}.get(
                    raw.get("moisture"), None
                ),
                ditch_connectivity={"impaired": "impaired", "clear": "clear"}.get(
                    raw.get("ditch_connectivity"), None
                ),
                standing_water=raw.get("standing_water"),
                leaf_count=raw.get("leaf_count"),
                inspection_duration_minutes=task_i.get("duration_minutes") if task_i else 30,
                maintenance_duration_minutes=task_m.get("duration_minutes") if task_m else 60,
                inspection_priority=1 if plot_id in {"A", "C"} else 2,
                maintenance_priority=1 if plot_id == "A" else 2,
                inspection_earliest_date=(
                    date.fromisoformat(task_i["earliest_date"]) if task_i else start
                ),
                inspection_due_date=(
                    date.fromisoformat(task_i["latest_date"])
                    if task_i
                    else start + timedelta(days=6)
                ),
                maintenance_earliest_date=(
                    date.fromisoformat(task_m["earliest_date"]) if task_m else start
                ),
                # Align both user dates so the adapter orders A-M before B-I by priority and ID.
                maintenance_due_date=(
                    date.fromisoformat(task_m["latest_date"])
                    if task_m
                    else start + timedelta(days=6)
                ),
                observed_at=(
                    datetime.fromisoformat(raw["observed_at"]) if raw.get("observed_at") else None
                ),
                observation_source="synthetic",
            )
        )
    # The published baseline puts A-M before B-I. Its synthetic preference order is explicit here.
    field_b = next(profile for profile in plot_profiles if profile.id == "B")
    field_b.inspection_due_date = date(2026, 10, 22)
    field_a = next(profile for profile in plot_profiles if profile.id == "A")
    field_a.maintenance_due_date = date(2026, 10, 22)
    field_b.maintenance_prerequisites = ["B-I"]

    first_version = versions["V1"]
    capacities = first_version["capacity_minutes"]
    availability = first_version["availability"]
    workability = first_version["workability"]
    days: list[DayWindow] = []
    for day_text in data["dates"]:
        day = date.fromisoformat(day_text)
        field_values = [workability[plot.id][day_text] for plot in plot_profiles]
        # Day-level defaults are permissive; explicit per-field confirmation remains attached below.
        intervals = [
            TimeWindow(start=time.fromisoformat(start_text), end=time.fromisoformat(end_text))
            for start_text, end_text in availability[day_text]
        ]
        days.append(
            DayWindow(
                day=day,
                site_workable=all(value["site"] for value in field_values),
                weather_workable=all(value["weather"] for value in field_values),
                resource_minutes=int(capacities[day_text]),
                intervals=intervals,
            )
        )
    for profile in plot_profiles:
        profile.workability_by_date = {
            date.fromisoformat(day_text): WorkabilityConfirmation(
                site=workability[profile.id][day_text]["site"],
                weather=workability[profile.id][day_text]["weather"],
            )
            for day_text in data["dates"]
        }

    catalog = load_rule_catalog()
    return PlanRequestV2(
        start_date=start,
        plots=plot_profiles,
        days=days,
        **catalog.plan_metadata(),
        selected_rule_ids=sorted(catalog.development_rule_ids),
        synthetic=True,
    )


def _fixed_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def replay_reference_history() -> list[PlanVersionV2]:
    """Use the dated fixture events as inputs; all status and slots come from the planner."""
    _, reference = _ref_input()
    request = make_reference_request()
    v1 = build_plan_v2(
        request,
        created_at=_fixed_datetime(reference["V1"]["as_of"]),
        reference_version="V1",
    )

    v2_request = v1.request.model_copy(deep=True)
    v2_request.completed_at.update(
        {
            "A-I": _fixed_datetime("2026-10-19T09:30:00+08:00"),
            "C-I": _fixed_datetime("2026-10-19T10:00:00+08:00"),
        }
    )
    day20 = next(day for day in v2_request.days if day.day == date(2026, 10, 20))
    day20.resource_minutes = int(reference["V2"]["capacity_minutes"]["2026-10-20"])
    event_v2 = FeedbackV2(
        kind="resource",
        at=_fixed_datetime(reference["V2"]["as_of"]),
        day=date(2026, 10, 20),
        resource_minutes=day20.resource_minutes,
        intervals=day20.intervals,
    )
    v2 = build_plan_v2(
        v2_request,
        previous=v1,
        feedback=event_v2,
        created_at=event_v2.at,
        reference_version="V2",
    )

    history = [v1, v2]
    v3 = v2
    # Replay each recorded event at its source timestamp so the date floor is deterministic.
    v3 = apply_feedback_v2(
        v3,
        FeedbackV2(
            kind="completed",
            task_id="B-I",
            at=_fixed_datetime("2026-10-20T11:00:00+08:00"),
        ),
    )
    v3.reference_version = "V3 · 完成与需求反馈"
    history.append(v3)
    v3 = apply_feedback_v2(
        v3,
        FeedbackV2(
            kind="observation",
            plot_id="B",
            field="maintenance_need_confirmed",
            value=True,
            at=_fixed_datetime(reference["V3"]["as_of"]),
        ),
    )
    v3.reference_version = "V3 · 问题观察"
    history.append(v3)
    v3 = apply_feedback_v2(
        v3,
        FeedbackV2(
            kind="observation",
            plot_id="B",
            field="ditch_connectivity",
            value="impaired",
            at=_fixed_datetime(reference["V3"]["as_of"]),
        ),
    )
    v3.reference_version = "V3 · 沟系复核"
    history.append(v3)
    v3 = apply_feedback_v2(
        v3,
        FeedbackV2(
            kind="workability",
            plot_id="B",
            day=date(2026, 10, 20),
            site_workable=False,
            weather_workable=None,
            at=_fixed_datetime(reference["V3"]["as_of"]),
        ),
    )
    v3.reference_version = "V3"
    history.append(v3)
    return history


def replay_reference_cases() -> dict[str, PlanVersionV2]:
    versions = replay_reference_history()
    return {"V1": versions[0], "V2": versions[1], "V3": versions[-1]}


def _guardrail_request(
    *,
    inspection_requested: bool | None = True,
    maintenance_need_confirmed: bool | None = False,
    field_type: str | None = "lowland_rice_stubble",
    site: bool | None = True,
    weather: bool | None = True,
    **plot_values: Any,
) -> PlanRequestV2:
    catalog = load_rule_catalog()
    start = date(2026, 10, 19)
    plot = FieldProfile(
        id="A",
        name="测试模拟地",
        province="湖北",
        province_confirmed=True,
        stage="苗期",
        stage_confirmed=True,
        field_type=field_type,
        inspection_requested=inspection_requested,
        maintenance_need_confirmed=maintenance_need_confirmed,
        inspection_duration_minutes=30,
        maintenance_duration_minutes=30,
        **plot_values,
    )
    days = [
        DayWindow(
            day=start + timedelta(days=offset),
            site_workable=site,
            weather_workable=weather,
            resource_minutes=120,
            intervals=[TimeWindow(start=time(9), end=time(11))],
        )
        for offset in range(7)
    ]
    return PlanRequestV2(
        start_date=start,
        plots=[plot],
        days=days,
        **catalog.plan_metadata(),
        selected_rule_ids=sorted(catalog.development_rule_ids),
        synthetic=True,
    )


def run_guardrail_cases() -> list[dict[str, Any]]:
    """Execute boundary scenarios inside current scope; label knowledge-only cases separately."""
    reference = _read_json(
        RULES_ROOT / "农时窗口_规则卡片_融合修订版_20261017" / "结构化资料" / "边界案例.json"
    )
    supplemental = _read_json(
        RULES_ROOT / "农时窗口_规则卡片_融合修订版_20261017" / "结构化资料" / "新增资料审查.json"
    )
    executed: dict[str, dict[str, Any]] = {}

    plan = build_plan_v2(
        _guardrail_request(inspection_requested=False, maintenance_need_confirmed=False)
    )
    executed["V01"] = {
        "ok": all(item.schedule_status == "not_generated" for item in plan.items),
        "actual": {item.task_id: item.schedule_status for item in plan.items},
    }

    plan = build_plan_v2(_guardrail_request(weather=None))
    item = next(item for item in plan.items if item.task_id == "A-I")
    executed["V02"] = {
        "ok": item.window_by_date[date(2026, 10, 19)] == "pending" and item.start_at is None,
        "actual": {
            "window": item.window_by_date[date(2026, 10, 19)],
            "schedule_status": item.schedule_status,
        },
    }

    plan = build_plan_v2(
        _guardrail_request(
            maintenance_need_confirmed=True,
            maintenance_problem_confirmed=True,
            ditch_connectivity="impaired",
        )
    )
    inspection = next(item for item in plan.items if item.task_id == "A-I")
    maintenance = next(item for item in plan.items if item.task_id == "A-M")
    executed["V03"] = {
        "ok": inspection.start_at is not None and maintenance.start_at is not None,
        "actual": {
            "inspection": inspection.schedule_status,
            "maintenance": maintenance.schedule_status,
            "maintenance_prerequisites": plan.request.plots[0].maintenance_prerequisites,
        },
    }

    excluded = {
        "V04": "缺少C29灌溉与排水互作处置模型，属于知识范围外案例",
        "V08": "需要比较C17与C18的参数适用场景，属于农艺知识解释",
        "V09": "C10叶龄分档未纳入候选规则，属于知识查询与插值边界",
        "V10": "温度预报与低温触发不在本轮字段和规则范围",
    }
    for identifier, reason in excluded.items():
        executed[identifier] = {"reference_only": reason}

    plan = build_plan_v2(_guardrail_request(actual_rain_observed=False, inspection_requested=True))
    item = next(item for item in plan.items if item.task_id == "A-I")
    executed["V05"] = {
        "ok": item.start_at is not None and "C24" not in {e.rule_id for e in item.evidence},
        "actual": {
            "inspection": item.schedule_status,
            "evidence_ids": sorted({e.rule_id for e in item.evidence}),
        },
    }

    plan = build_plan_v2(_guardrail_request(leaf_count=None))
    item = next(item for item in plan.items if item.task_id == "A-I")
    executed["V06"] = {
        "ok": item.start_at is not None,
        "actual": {"inspection": item.schedule_status, "leaf_count": None},
    }

    plan = build_plan_v2(
        _guardrail_request(
            inspection_requested=False,
            maintenance_need_confirmed=False,
            field_type="dry_hilly",
            moisture="dry",
            ditch_connectivity="clear",
            standing_water=False,
        )
    )
    maintenance = next(item for item in plan.items if item.task_id == "A-M")
    executed["V07"] = {
        "ok": maintenance.necessity == "no_need" and maintenance.schedule_status == "not_generated",
        "actual": {
            "necessity": maintenance.necessity,
            "schedule_status": maintenance.schedule_status,
        },
    }

    plan = build_plan_v2(
        _guardrail_request(inspection_requested=True, maintenance_need_confirmed=False)
    )
    inspection = next(item for item in plan.items if item.task_id == "A-I")
    executed["A01"] = {
        "ok": inspection.start_at is not None,
        "actual": {"inspection": inspection.schedule_status},
    }
    plan = build_plan_v2(_guardrail_request(weather=None))
    inspection = next(item for item in plan.items if item.task_id == "A-I")
    executed["A03"] = {
        "ok": all(value == "pending" for value in inspection.window_by_date.values()),
        "actual": {
            "window_by_date": {
                key.isoformat(): value for key, value in inspection.window_by_date.items()
            }
        },
    }
    plan = build_plan_v2(
        _guardrail_request(inspection_requested=True, maintenance_need_confirmed=False)
    )
    executed["A06"] = {
        "ok": all(item.task_type in {"inspection", "maintenance"} for item in plan.items)
        and not any(
            item.task_type == "maintenance" and item.schedule_status != "not_generated"
            for item in plan.items
        ),
        "actual": {"task_types": [item.task_type for item in plan.items]},
    }
    plan = build_plan_v2(
        _guardrail_request(
            inspection_requested=True,
            maintenance_need_confirmed=True,
            maintenance_problem_confirmed=True,
            ditch_connectivity="impaired",
            standing_water=True,
            moisture="wet",
        )
    )
    executed["A07"] = {
        "ok": sum(item.task_id == "A-M" for item in plan.items) == 1,
        "actual": {"maintenance_candidates": sum(item.task_id == "A-M" for item in plan.items)},
    }

    plan = build_plan_v2(
        _guardrail_request(
            maintenance_need_confirmed=True,
            maintenance_problem_confirmed=True,
            ditch_connectivity="impaired",
        )
    )
    maintenance = next(item for item in plan.items if item.task_id == "A-M")
    if maintenance.start_at:
        completed = apply_feedback_v2(
            plan,
            FeedbackV2(kind="completed", task_id="A-M", at=maintenance.end_at),
        )
        observed = apply_feedback_v2(
            completed,
            FeedbackV2(
                kind="observation",
                plot_id="A",
                field="standing_water",
                value=True,
                at=maintenance.end_at + timedelta(minutes=1),
            ),
        )
        after = next(item for item in observed.items if item.task_id == "A-M")
        executed["V11"] = {
            "ok": after.completed and observed.request.plots[0].standing_water is True,
            "actual": {
                "completed": after.completed,
                "standing_water": observed.request.plots[0].standing_water,
            },
        }
    else:
        executed["V11"] = {"ok": False, "actual": {"reason": maintenance.reason}}

    plan = build_plan_v2(
        _guardrail_request(winter_before_context_confirmed=True, field_type="lowland_rice_stubble")
    )
    inspections = [item for item in plan.items if item.task_type == "inspection"]
    evidence_ids = {evidence.rule_id for item in inspections for evidence in item.evidence}
    executed["V12"] = {
        "ok": len(inspections) == 1 and {"C11", "C18", "C19", "C31"} <= evidence_ids,
        "actual": {"inspection_count": len(inspections), "evidence_ids": sorted(evidence_ids)},
    }

    supplemental_excluded = {
        "A02": "地块市级行政区未纳入结构化输入，跨地区来源复核需人工完成",
        "A04": "需比较不同来源沟深参数，超出已选候选卡执行范围",
        "A05": "苗情分档解释和专业评估不属于本轮任务生成",
        "A08": "寒潮预报及冻害后处置不在本轮字段与规则范围",
    }
    rows: list[dict[str, Any]] = []
    for case in reference["cases"]:
        result = executed[case["id"]]
        rows.append(
            {
                "id": case["id"],
                "input": case["input"],
                "expected": case["expected"],
                "status": "executed"
                if result.get("ok")
                else "reference_only_not_executed"
                if "reference_only" in result
                else "failed",
                "actual": result.get("actual", result.get("reference_only")),
            }
        )
    for case in supplemental["boundary_cases"]:
        result = executed.get(case["id"])
        if result is None:
            result = {"reference_only": supplemental_excluded[case["id"]]}
        rows.append(
            {
                "id": case["id"],
                "input": case["input"],
                "expected": case["expected"],
                "status": "executed"
                if result.get("ok")
                else "reference_only_not_executed"
                if "reference_only" in result
                else "failed",
                "actual": result.get("actual", result.get("reference_only")),
            }
        )
    return rows


def _case_request(case: dict[str, Any]) -> PlanRequestV2:
    values = case["input"]
    start = date(2026, 10, 17)
    matched = values.get("scope") != "out_of_scope"
    province = values.get("province", "湖北" if matched else "湖南")
    stage = values.get("stage", "苗期" if matched else "开花期")
    need = values.get("need")
    inspection = values.get("inspection_requested", case["id"] == "T01")
    issue = values.get("issue")
    site = values.get("site", True)
    weather = values.get("weather", True)
    if case["id"] in {"T06", "T07", "T08", "T09"}:
        issue = issue or "impaired"
    profile = FieldProfile(
        id="A",
        name="案例地块",
        province=province,
        province_confirmed=province is not None,
        stage=stage,
        stage_confirmed=stage is not None,
        field_type="lowland_rice_stubble",
        inspection_requested=inspection,
        maintenance_need_confirmed=need,
        maintenance_problem_confirmed=(True if issue == "impaired" else None),
        ditch_connectivity={"impaired": "impaired", "clear": "clear"}.get(
            values.get("ditch_connectivity", issue), None
        ),
        inspection_duration_minutes=values.get("duration", 30),
        maintenance_duration_minutes=values.get("duration", 30),
    )
    if case["id"] == "T04":
        profile.inspection_requested = True
    days = [
        DayWindow(
            day=start + timedelta(days=index),
            site_workable=site,
            weather_workable=weather,
            resource_minutes=int(values.get("capacity", 120)),
            intervals=[TimeWindow(start=time(9), end=time(11))],
        )
        for index in range(7)
    ]
    catalog = load_rule_catalog()
    return PlanRequestV2(
        start_date=start,
        plots=[profile],
        days=days,
        **catalog.plan_metadata(),
        selected_rule_ids=sorted(catalog.development_rule_ids),
        synthetic=True,
    )


def _one_item(plan: PlanVersionV2, task_id: str | None = None):
    candidates = plan.items if task_id is None else [i for i in plan.items if i.task_id == task_id]
    return candidates[0]


def _execute_simple_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = case["id"]
    request = _case_request(case)
    if case_id == "T05":
        request.plots[0].maintenance_problem_confirmed = None
    if case_id == "T06":
        request.plots[0].maintenance_problem_confirmed = True
    if case_id == "T07":
        request.plots[0].maintenance_problem_confirmed = True
    if case_id == "T09":
        request.plots[0].maintenance_problem_confirmed = True
    plan = build_plan_v2(request)
    target_id = "A-I" if case_id in {"T01", "T04"} else "A-M"
    item = _one_item(plan, target_id)
    overall_window = next(iter(item.window_by_date.values()), None)
    if overall_window is None and item.schedule_status == "pending":
        overall_window = "pending"
    actual = {
        "applicability": item.applicability,
        "necessity": item.necessity,
        "window": overall_window,
        "schedule_status": item.schedule_status,
        "missing_fields": item.missing_fields,
        "reason_code": item.reason_code,
    }
    expected = case.get("expected", {})
    checks: list[bool] = []
    for field in ("necessity", "schedule_status"):
        if expected.get(field) is not None:
            checks.append(actual[field] == expected[field])
    if "window" in expected:
        checks.append(actual["window"] == expected["window"])
    if expected.get("ask"):
        checks.append(
            all(
                any(
                    field == name or field.endswith(f":{name}")
                    for field in actual["missing_fields"]
                )
                for name in expected["ask"]
            )
        )
    if expected.get("reason") == "capacity":
        checks.append(actual["reason_code"] == "capacity")
    return {
        "id": case_id,
        "name": case["name"],
        "status": "executed" if all(checks) else "failed",
        "expected": expected,
        "actual": actual,
    }


def build_case_audit() -> dict[str, Any]:
    """Run in-scope cases; register out-of-scope knowledge cases without false pass claims."""
    raw = _read_json(CASE_DIR / "12组开发案例.json")
    development: list[dict[str, Any]] = []
    simple = {f"T{number:02}": case for number, case in enumerate(raw["cases"], 1)}
    for number in range(1, 10):
        development.append(_execute_simple_case(simple[f"T{number:02}"]))

    versions = replay_reference_cases()
    v1 = {item.task_id: item for item in versions["V1"].items}
    v2 = {item.task_id: item for item in versions["V2"].items}
    v3 = {item.task_id: item for item in versions["V3"].items}
    checks_t10 = (
        v2["A-I"].completed
        and v2["C-I"].completed
        and v2["B-I"].start_at == v1["B-I"].start_at
        and v2["A-M"].start_at is not None
        and v2["A-M"].start_at.date() == date(2026, 10, 21)
    )
    development.append(
        {
            "id": "T10",
            "name": "完成保留和重排",
            "status": "executed" if checks_t10 else "failed",
            "actual": {
                "completed": [task for task in ("A-I", "C-I") if v2[task].completed],
                "B-I": v2["B-I"].start_at.isoformat() if v2["B-I"].start_at else None,
                "A-M": v2["A-M"].start_at.isoformat() if v2["A-M"].start_at else None,
            },
        }
    )
    b_m_day = date(2026, 10, 20)
    checks_t11 = (
        v3["B-M"].necessity == "required"
        and v3["B-M"].window_by_date.get(b_m_day) == "unavailable"
        and all(
            v3["B-M"].window_by_date.get(b_m_day + timedelta(days=offset)) == "pending"
            for offset in range(1, 6)
        )
        and v3["A-M"].start_at == v2["A-M"].start_at
    )
    development.append(
        {
            "id": "T11",
            "name": "现场反馈变化",
            "status": "executed" if checks_t11 else "failed",
            "actual": {
                "B-M": {
                    "necessity": v3["B-M"].necessity,
                    "2026-10-20": v3["B-M"].window_by_date.get(b_m_day),
                    "later": sorted(
                        {state for day, state in v3["B-M"].window_by_date.items() if day > b_m_day}
                    ),
                },
                "A-M": v3["A-M"].start_at.isoformat() if v3["A-M"].start_at else None,
            },
        }
    )

    catalog = load_rule_catalog()
    v3_replay = {item.task_id: item for item in versions["V3"].items}
    t12_checks = [
        (
            "T12-draft-as-final",
            all(
                not catalog.cards[rule_id].runtime_enabled
                for rule_id in catalog.development_rule_ids
            ),
            "7张候选卡保持runtime_enabled=false",
        ),
        (
            "T12-flowering-source",
            all(
                item.applicability == "out_of_scope"
                for item in build_plan_v2(
                    _case_request({"id": "T04", "input": {"province": "湖北", "stage": "开花期"}})
                ).items
            ),
            "开花期输入保持超出苗期范围",
        ),
        (
            "T12-weather-failure",
            v3_replay["B-M"].schedule_status == "pending"
            and v3_replay["B-M"].window_by_date.get(date(2026, 10, 20)) == "unavailable"
            and all(
                v3_replay["B-M"].window_by_date.get(date(2026, 10, 20) + timedelta(days=offset))
                == "pending"
                for offset in range(1, 6)
            )
            and all(
                versions["V3"].request.completed_at.get(task) for task in ("A-I", "C-I", "B-I")
            ),
            "天气未确认时保持待确认并保留完成记录",
        ),
    ]
    development.extend(
        {
            "id": identifier,
            "name": identifier,
            "status": "executed" if passed else "failed",
            "actual": detail,
        }
        for identifier, passed, detail in t12_checks
    )

    boundary_data = _read_json(
        RULES_ROOT / "农时窗口_规则卡片_融合修订版_20261017" / "结构化资料" / "边界案例.json"
    )
    supplemental_data = _read_json(
        RULES_ROOT / "农时窗口_规则卡片_融合修订版_20261017" / "结构化资料" / "新增资料审查.json"
    )
    guardrail_rows = run_guardrail_cases()
    guardrail_by_id = {row["id"]: row for row in guardrail_rows}
    return {
        "report_version": "1.0",
        "classification": "本地开发验证；不是独立留出评测或农业适用性结论",
        "ruleset_version": catalog.delivery_version,
        "legacy_rule_version": catalog.legacy_rule_version,
        "legacy_mapping_type": catalog.legacy_mapping_type,
        "engineering_rules_version": catalog.engineering_rules_version,
        "engineering_rule_ids": sorted(str(row["id"]) for row in catalog.engineering_rules),
        "selected_rule_ids": sorted(catalog.development_rule_ids),
        "development_cases": development,
        "boundary_cases": [guardrail_by_id[row["id"]] for row in boundary_data["cases"]],
        "supplemental_cases": [
            guardrail_by_id[row["id"]] for row in supplemental_data["boundary_cases"]
        ],
        "reference_replay": {
            version: {
                "id": plan.id,
                "created_at": plan.created_at.isoformat(),
                "synthetic": plan.request.synthetic,
                "assignments": [
                    {
                        "task_id": item.task_id,
                        "start_at": item.start_at.isoformat() if item.start_at else None,
                        "end_at": item.end_at.isoformat() if item.end_at else None,
                        "status": item.schedule_status,
                    }
                    for item in plan.items
                    if item.start_at or item.schedule_status == "completed"
                ],
            }
            for version, plan in versions.items()
        },
    }


def main() -> None:
    output = RULES_ROOT.parent / "reports" / "development_case_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = build_case_audit()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failures = [row["id"] for row in report["development_cases"] if row["status"] != "executed"]
    print(f"开发案例：{len(report['development_cases'])} 组；失败：{', '.join(failures) or '无'}")
    print(f"报告：{output}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
