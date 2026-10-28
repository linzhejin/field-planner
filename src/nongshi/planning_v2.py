"""Explicit business checks and single-worker, continuous-minute scheduling."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from nongshi.rules import RuleCatalog, load_rule_catalog
from nongshi.schema_v2 import (
    LOCAL_ZONE,
    DailyWindowStatus,
    DayWindow,
    FeedbackV2,
    FieldProfile,
    PlanItemV2,
    PlanRequestV2,
    PlanVersionV2,
    RuleEvidenceV2,
    WorkabilityConfirmation,
)
from nongshi.tasks_v4 import TaskExecutionGroupV4


def _scope(plot: FieldProfile) -> tuple[str, list[str], str]:
    missing: list[str] = []
    if plot.province is None:
        missing.append("province")
    elif not plot.province_confirmed:
        missing.append("province_confirmation")
    if plot.crop is None:
        missing.append("crop")
    elif not plot.crop_confirmed:
        missing.append("crop_confirmation")
    if plot.stage is None:
        missing.append("stage")
    elif not plot.stage_confirmed:
        missing.append("stage_confirmation")
    if missing:
        return "pending", missing, "待确认地区、作物和生育期"
    if plot.province not in {"湖北", "湖北省"} or plot.crop != "油菜" or plot.stage != "苗期":
        return "out_of_scope", [], "不在首版湖北油菜苗期范围内"
    return "matched", [], ""


def _problem_state(plot: FieldProfile) -> tuple[bool | None, list[str]]:
    # 现场观察（沟渠受阻、积水）只是事实，不能替用户推断"存在问题"；
    # 问题是否成立必须由用户显式确认（maintenance_problem_confirmed）。
    if plot.maintenance_problem_confirmed is not None:
        return plot.maintenance_problem_confirmed, []
    missing = ["maintenance_problem_confirmed"]
    if plot.ditch_connectivity is None:
        missing.append("ditch_connectivity")
    if plot.standing_water is None:
        missing.append("standing_water")
    return None, missing


def _task_state(plot: FieldProfile, task_type: str) -> tuple[str, str, list[str]]:
    scope, scope_missing, _ = _scope(plot)
    missing = list(scope_missing)
    if task_type == "inspection":
        if plot.inspection_requested is True:
            necessity = "required"
        elif plot.inspection_requested is False:
            necessity = "no_need"
        else:
            necessity = "unknown"
            missing.append("inspection_requested")
    elif plot.maintenance_need_confirmed is False:
        necessity = "no_need"
    elif plot.maintenance_need_confirmed is None:
        necessity = "unknown"
        _, problem_missing = _problem_state(plot)
        missing.extend([*problem_missing, "maintenance_need_confirmed"])
    else:
        problem, problem_missing = _problem_state(plot)
        if problem is True:
            if plot.maintenance_purpose == "clear_existing_ditch":
                necessity = "required"
            elif plot.maintenance_purpose is None:
                necessity = "unknown"
                missing.append("maintenance_purpose")
            else:
                necessity = "unknown"
        else:
            necessity = "unknown"
            missing.extend(problem_missing or ["maintenance_problem_confirmed"])

    applicability = scope
    if (
        task_type == "maintenance"
        and plot.maintenance_need_confirmed is not False
        and plot.maintenance_purpose is None
    ):
        missing.append("maintenance_purpose")
    if task_type == "maintenance" and necessity != "no_need" and applicability == "matched":
        if plot.field_type is None:
            applicability = "pending"
            missing.append("field_type")
        elif plot.field_type == "other":
            applicability = "out_of_scope"
        elif plot.maintenance_purpose == "other":
            applicability = "out_of_scope"
    return applicability, necessity, list(dict.fromkeys(missing))


def _rule_ids_for(plot: FieldProfile, task_type: str) -> list[str]:
    if task_type == "inspection":
        rules = ["C31", "C11"]
    else:
        rules = ["C23"]
    if plot.field_type == "lowland_rice_stubble":
        rules.append("C19")
    if plot.field_type == "dry_hilly":
        rules.append("C20")
    if plot.winter_before_context_confirmed is True:
        rules.append("C18")
    if plot.actual_rain_observed is True:
        rules.append("C24")
    return list(dict.fromkeys(rules))


@lru_cache(maxsize=1)
def _source_context_records() -> dict[str, dict[str, str]]:
    path = Path(__file__).with_name("data") / "rule_source_context.json"
    return json.loads(path.read_text(encoding="utf-8"))["records"]


def build_source_context_summary(raw: dict[str, Any]) -> str:
    """Only use the reviewed context for this exact rule and source pair."""
    key = f"{raw['rule_id']}/{raw['source_id']}"
    return _source_context_records().get(key, {}).get("source_context_summary", "来源背景待复核")


def build_rule_evidence(
    catalog: RuleCatalog,
    rule_ids: list[str],
    review_status: dict[str, str] | None = None,
) -> list[RuleEvidenceV2]:
    """Shared evidence contract for planning, search, tool results, and exports."""
    result: list[RuleEvidenceV2] = []
    for raw in catalog.evidence_for(rule_ids):
        rule_id = raw["rule_id"]
        user_status = (review_status or {}).get(rule_id)
        if user_status == "rejected":
            continue
        context = _source_context_records().get(f"{rule_id}/{raw['source_id']}", {})
        raw["source_context_summary"] = build_source_context_summary(raw)
        raw["project_use_limit"] = context.get("project_use_limit", "仅供查阅，项目用途待复核")
        evidence = RuleEvidenceV2(**raw)
        if rule_id in catalog.deferred_rule_ids:
            evidence.usage_role = "historical_reference"
            evidence.status_label = "暂缓自动适用，仅作历史参考"
        elif user_status == "adopted":
            evidence.usage_role = "limited_support"
            evidence.status_label = "已采用（限用途）"
        elif rule_id in catalog.limited_adopt_rule_ids:
            evidence.usage_role = "limited_support"
            evidence.status_label = "当前限用途依据（AI辅助复核已交付，正式采用待确认）"
        result.append(evidence)
    return result


def _make_evidence(
    catalog: RuleCatalog,
    plot: FieldProfile,
    task_type: str,
    review_status: dict[str, str] | None = None,
) -> list[RuleEvidenceV2]:
    """为每个证据条覆写 status_label，区分限用途当前依据与暂缓历史参考。

    优先级：用户 review_status > catalog 分类（正式确认）> 默认。
    不改 _rule_ids_for 返回值，暂缓卡仍出现在列表中但 label 不同。
    formal_adopted 仍为 0；AI 辅助复核交付不等于成员签认或正式采用。
    """
    return build_rule_evidence(catalog, _rule_ids_for(plot, task_type), review_status)


def _effective_workability(plot: FieldProfile, day: DayWindow) -> tuple[bool | None, bool | None]:
    specific = plot.workability_by_date.get(day.day)
    return (
        specific.site if specific is not None else day.site_workable,
        specific.weather if specific is not None else day.weather_workable,
    )


def _daily_window(plot: FieldProfile, day: DayWindow) -> DailyWindowStatus:
    site, weather = _effective_workability(plot, day)
    if site is False or weather is False:
        return "unavailable"
    if site is None or weather is None:
        return "pending"
    return "available"


def _base_item(
    plot: FieldProfile,
    task_type: str,
    catalog: RuleCatalog,
    days: list[DayWindow],
    review_status: dict[str, str] | None = None,
) -> PlanItemV2:
    applicability, necessity, missing = _task_state(plot, task_type)
    task_id = (
        f"{plot.id}-I" if task_type == "inspection" else plot.maintenance_task_id or f"{plot.id}-M"
    )
    duration = (
        plot.inspection_duration_minutes
        if task_type == "inspection"
        else plot.maintenance_duration_minutes
    )
    title = "巡田苗情墒情虫情与沟渠检查" if task_type == "inspection" else "既有沟渠维护候选"
    windows: dict[date, DailyWindowStatus] = {}
    if applicability == "matched" and necessity == "required":
        windows = {day.day: _daily_window(plot, day) for day in days}
        for day in days:
            site, weather = _effective_workability(plot, day)
            if site is None:
                missing.append(f"{day.day.isoformat()}:site_workability")
            if weather is None:
                missing.append(f"{day.day.isoformat()}:weather_workability")
            if day.resource_minutes is None:
                missing.append(f"{day.day.isoformat()}:resource_minutes")
    if necessity == "no_need":
        status, reason_code, reason = "not_generated", "no_need", "已确认不需要，未生成任务"
    elif applicability == "out_of_scope":
        status, reason_code, reason = "not_generated", "out_of_scope", "任务超出首版范围，不生成"
    elif applicability == "pending":
        status, reason_code, reason = "pending", "scope_pending", "范围信息待确认"
    elif necessity == "unknown":
        status, reason_code, reason = "pending", "necessity_pending", "需求或现场问题尚未确认"
    elif duration is None:
        status, reason_code, reason = (
            "pending",
            "missing_duration",
            "窗口可独立确认；待填写预计作业分钟数",
        )
        missing.append("duration_minutes")
    elif all(state == "pending" for state in windows.values()):
        status, reason_code, reason = (
            "pending",
            "workability_pending",
            "逐日现场和天气作业条件待确认",
        )
    else:
        status, reason_code, reason = (
            "unscheduled",
            "not_scheduled",
            "作业条件已确认，正在核对人力和时段",
        )
    return PlanItemV2(
        task_id=task_id,
        plot_id=plot.id,
        title=title,
        task_type=task_type,  # type: ignore[arg-type]
        applicability=applicability,  # type: ignore[arg-type]
        necessity=necessity,  # type: ignore[arg-type]
        window_by_date=windows,
        schedule_status=status,  # type: ignore[arg-type]
        duration_minutes=duration,
        reason=reason,
        reason_code=reason_code,
        missing_fields=list(dict.fromkeys(missing)),
        evidence=_make_evidence(catalog, plot, task_type, review_status),
    )


def _task_preferences(
    plot: FieldProfile,
    task_type: str,
    item: PlanItemV2 | None = None,
) -> tuple[int, date | None, date | None]:
    if item is not None and item.priority is not None:
        return item.priority, item.earliest_date, item.due_date
    if task_type == "inspection":
        return plot.inspection_priority, plot.inspection_earliest_date, plot.inspection_due_date
    return plot.maintenance_priority, plot.maintenance_earliest_date, plot.maintenance_due_date


def _task_prerequisites(
    plot: FieldProfile,
    task_type: str,
    item: PlanItemV2 | None = None,
    completed_at: dict[str, datetime] | None = None,
) -> list[str]:
    if item is not None and item.template_snapshot is not None:
        prerequisites = list(item.prerequisites)
        if task_type == "maintenance":
            prerequisites.extend(plot.maintenance_prerequisites)
        return [
            prerequisite
            for prerequisite in dict.fromkeys(prerequisites)
            if prerequisite not in (completed_at or {})
        ]
    return plot.maintenance_prerequisites if task_type == "maintenance" else []


def _item_window_status(item: PlanItemV2, plot: FieldProfile, day: DayWindow) -> str:
    if item.category == "offsite":
        return "available"
    return _daily_window(plot, day)


def _date_time(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=LOCAL_ZONE)


def _interval_fits(
    start: datetime,
    duration: int,
    day: DayWindow,
    reservations: dict[date, list[tuple[datetime, datetime, str]]],
    used_minutes: dict[date, int],
) -> bool:
    if (
        day.resource_minutes is None
        or used_minutes.get(day.day, 0) + duration > day.resource_minutes
    ):
        return False
    end = start + timedelta(minutes=duration)
    in_interval = any(
        _date_time(day.day, interval.start) <= start and end <= _date_time(day.day, interval.end)
        for interval in day.intervals
    )
    if not in_interval:
        return False
    return not any(
        start < booked_end and end > booked_start
        for booked_start, booked_end, _ in reservations.get(day.day, [])
    )


def _candidate_slots(
    day: DayWindow,
    duration: int,
    floor: datetime,
    reservations: dict[date, list[tuple[datetime, datetime, str]]],
    used_minutes: dict[date, int],
) -> list[datetime]:
    candidates: list[datetime] = []
    for interval in day.intervals:
        cursor = max(_date_time(day.day, interval.start), floor)
        bookings = sorted(reservations.get(day.day, []), key=lambda item: item[0])
        for booked_start, booked_end, _ in bookings:
            if booked_end <= cursor or booked_start >= _date_time(day.day, interval.end):
                continue
            if cursor + timedelta(minutes=duration) <= booked_start:
                break
            cursor = max(cursor, booked_end)
        if cursor + timedelta(minutes=duration) <= _date_time(day.day, interval.end):
            if (
                day.resource_minutes is not None
                and used_minutes.get(day.day, 0) + duration <= day.resource_minutes
            ):
                candidates.append(cursor)
    return candidates


def _reasons_if_unplaced(
    item: PlanItemV2,
    plot: FieldProfile,
    request: PlanRequestV2,
    by_id: dict[str, PlanItemV2],
    prerequisites: list[str],
    floor: datetime,
) -> tuple[str, str, str]:
    if item.duration_minutes is None:
        return "pending", "missing_duration", "窗口已判定；作业时长未知，未猜测"
    for parent_id in prerequisites:
        parent = by_id.get(parent_id)
        if parent is None or parent.end_at is None:
            return "pending", "prerequisite_pending", f"前置任务 {parent_id} 尚无完成或安排时间"
    if any(status == "pending" for status in item.window_by_date.values()):
        return "pending", "workability_pending", "仍有日期的现场或天气作业条件待确认"
    if any(
        item.window_by_date.get(day.day) == "available" and day.resource_minutes is None
        for day in request.days
    ):
        return "pending", "resource_pending", "作业窗口已确认；可用人力分钟数尚未确认"
    if floor.date() > request.days[-1].day:
        return "unscheduled", "horizon_passed", "反馈时间已超出七天范围，请新建下一期计划"
    available_days = [
        day for day in request.days if item.window_by_date.get(day.day) == "available"
    ]
    if not available_days:
        return "unscheduled", "no_available_window", "七天内没有现场与天气均确认可作业的日期"
    _, earliest, due = _task_preferences(plot, item.task_type, item)
    first_possible = max(floor.date(), earliest or request.start_date)
    if due is not None and due < first_possible:
        return "unscheduled", "deadline", "用户目标日期早于反馈后的最早可安排日期"
    candidate_days = [
        day
        for day in available_days
        if day.day >= first_possible and (due is None or day.day <= due)
    ]
    if not candidate_days:
        return "unscheduled", "deadline", "七天范围内没有满足用户日期要求的可作业日"
    if all(
        day.resource_minutes is not None and day.resource_minutes < item.duration_minutes
        for day in candidate_days
    ):
        return "unscheduled", "capacity", "可用人力分钟数不足以完成连续作业"
    if all(
        max((interval.minutes for interval in day.intervals), default=0) < item.duration_minutes
        for day in candidate_days
    ):
        return "unscheduled", "time_slot", "没有足够长的连续可用时段；任务不能拆分"
    return "unscheduled", "capacity", "可用人力或时段已被其他任务占用"


def _task_instance_item(
    instance: object, request: PlanRequestV2, catalog: RuleCatalog
) -> PlanItemV2:
    from nongshi.tasks_v4 import TaskInstanceV4

    if not isinstance(instance, TaskInstanceV4):
        raise TypeError("task_instances_v4 must contain TaskInstanceV4 values")
    plot = next(plot for plot in request.plots if plot.id == instance.field_id)
    instance_titles = {task.id: task.template.title for task in request.task_instances_v4}
    snapshot = instance.template.model_dump(mode="json")
    snapshot["prerequisites"] = [
        {"id": task_id, "title": instance_titles.get(task_id, task_id)}
        for task_id in instance.prerequisites
    ]
    execution_group = next(
        (
            group
            for group in request.execution_groups_v4
            if group.status != "cancelled" and instance.id in group.member_task_ids
        ),
        None,
    )
    if execution_group:
        snapshot["execution_group"] = {
            "id": execution_group.id,
            "member_task_ids": list(execution_group.member_task_ids),
            "duration_minutes": execution_group.duration_minutes,
            "version": execution_group.version,
        }
    governed = instance.template.task_type in {"inspection", "maintenance"}
    if governed:
        applicability, necessity, missing = _task_state(plot, instance.template.task_type)
        if instance.template.task_type == "inspection" and plot.inspection_requested is None:
            # Saving a concrete inspection instance is an explicit request for that task.
            # Keep an earlier explicit refusal distinct and preserve all scope checks.
            necessity = "required"
            missing = [field for field in missing if field != "inspection_requested"]
    else:
        applicability, necessity, missing = "matched", "required", []
    missing.extend(instance.missing_fields)
    windows: dict[date, str] = {}
    for day in request.days:
        if day.day < instance.earliest_date or day.day > instance.due_date:
            continue
        if necessity != "required" or applicability != "matched":
            continue
        if instance.template.category == "offsite":
            windows[day.day] = "available"
        else:
            windows[day.day] = _daily_window(plot, day)
            site, weather = _effective_workability(plot, day)
            if site is None:
                missing.append(f"{day.day.isoformat()}:site_workability")
            if weather is None:
                missing.append(f"{day.day.isoformat()}:weather_workability")
    if instance.status == "superseded":
        status, reason_code, reason = (
            "not_generated",
            "series_revised",
            "该次任务已由新的周期设置替代",
        )
    elif necessity == "no_need":
        status, reason_code, reason = "not_generated", "no_need", "已确认不需要，不生成本次任务"
    elif applicability == "out_of_scope":
        status, reason_code, reason = (
            "not_generated",
            "out_of_scope",
            "任务超出当前适用范围，不生成",
        )
    elif applicability == "pending" or necessity == "unknown":
        status, reason_code, reason = (
            "pending",
            "business_confirmation_pending",
            "业务条件或需求尚待确认",
        )
    elif "duration_minutes" in instance.missing_fields:
        status, reason_code, reason = "pending", "missing_duration", "待补充预计时长，暂不排入"
    elif instance.missing_fields:
        status, reason_code, reason = (
            "pending",
            "task_details_pending",
            "任务内容尚缺必填信息，暂不排入",
        )
    elif not instance.duration_minutes:
        status, reason_code, reason = "pending", "missing_duration", "待补充预计时长，暂不排入"
    elif instance.due_date < request.start_date and instance.id not in request.completed_at:
        status, reason_code, reason = "unscheduled", "overdue", "任务已逾期；请确认是否重新安排"
    elif instance.template.category == "onsite" and all(
        state == "pending" for state in windows.values()
    ):
        status, reason_code, reason = "pending", "workability_pending", "现场和天气作业条件待确认"
    else:
        status, reason_code, reason = (
            "unscheduled",
            "not_scheduled",
            "用户确认的任务，正在核对作业条件、时间和前置关系",
        )
    return PlanItemV2(
        task_id=instance.id,
        plot_id=instance.field_id,
        title=instance.template.title,
        task_type=instance.template.task_type,
        applicability=applicability,  # type: ignore[arg-type]
        necessity=necessity,  # type: ignore[arg-type]
        window_by_date=windows,
        schedule_status=status,  # type: ignore[arg-type]
        duration_minutes=instance.duration_minutes,
        reason=reason,
        reason_code=reason_code,
        missing_fields=list(dict.fromkeys(missing)),
        category=instance.template.category,
        priority=instance.priority,
        earliest_date=instance.earliest_date,
        due_date=instance.due_date,
        prerequisites=list(instance.prerequisites),
        template_snapshot=snapshot,
        execution_group_id=execution_group.id if execution_group else None,
        group_duration_minutes=execution_group.duration_minutes if execution_group else None,
        evidence=(
            _make_evidence(
                catalog,
                plot,
                instance.template.task_type,
                request.rule_review_status,
            )
            if governed
            else []
        ),
    )


def _change_log(previous: PlanVersionV2 | None, items: list[PlanItemV2]) -> list[str]:
    if previous is None:
        return []
    old_items = {item.task_id: item for item in previous.items}
    changes: list[str] = []
    for item in items:
        old = old_items.get(item.task_id)
        if old is None:
            continue
        if old.completed and item.completed:
            continue
        if not old.completed and item.completed:
            changes.append(f"{item.task_id}：已记录完成，原时段保留")
            continue
        if (
            old.start_at,
            old.end_at,
            old.schedule_status,
            old.execution_group_id,
            old.group_duration_minutes,
        ) != (
            item.start_at,
            item.end_at,
            item.schedule_status,
            item.execution_group_id,
            item.group_duration_minutes,
        ):
            old_text = (
                f"{old.start_at:%Y-%m-%d %H:%M}–{old.end_at:%H:%M}"
                if old.start_at and old.end_at
                else "未排入"
            )
            new_text = (
                f"{item.start_at:%Y-%m-%d %H:%M}–{item.end_at:%H:%M}"
                if item.start_at and item.end_at
                else "未排入"
            )
            changes.append(f"{item.task_id}：{old_text} → {new_text}；{item.reason}")
    return changes


def build_plan_v2(
    request: PlanRequestV2,
    previous: PlanVersionV2 | None = None,
    feedback: FeedbackV2 | None = None,
    *,
    catalog: RuleCatalog | None = None,
    created_at: datetime | None = None,
    reference_version: str | None = None,
    not_before: datetime | None = None,
) -> PlanVersionV2:
    """Recompute a v2 snapshot, preserving completed facts and feasible old times."""
    catalog = catalog or load_rule_catalog()
    request = PlanRequestV2.model_validate(request.model_dump(mode="python")).model_copy(
        update={"schema_version": 4}
    )
    if request.ruleset_version != catalog.delivery_version:
        raise ValueError("规则资料版本已变化；请选择并确认当前规则版本后再生成")
    if set(request.selected_rule_ids) != catalog.development_rule_ids:
        raise ValueError("开发验证规则名单与交付配置不一致")

    if previous:
        completed = dict(request.completed_at)
        for task_id, completed_at in previous.request.completed_at.items():
            if task_id in completed and completed[task_id] != completed_at:
                raise ValueError("已完成记录不可修改")
            completed[task_id] = completed_at
        request.completed_at = completed

    items: list[PlanItemV2] = []
    plot_by_id = {plot.id: plot for plot in request.plots}
    for plot in request.plots:
        v4_types = {
            task.template.task_type
            for task in request.task_instances_v4
            if task.field_id == plot.id
        }
        for task_type in ("inspection", "maintenance"):
            item = _base_item(plot, task_type, catalog, request.days, request.rule_review_status)
            if task_type in v4_types and item.task_id not in request.completed_at:
                item.schedule_status = "not_generated"
                item.reason_code = "replaced_by_task_series"
                item.reason = "已建立独立任务实例，不再重复安排旧单次候选"
                item.missing_fields = []
            items.append(item)
    items.extend(_task_instance_item(task, request, catalog) for task in request.task_instances_v4)
    previous_items = {item.task_id: item for item in previous.items} if previous else {}
    current_ids = {item.task_id for item in items}
    if previous:
        for old in previous.items:
            if old.task_id in request.completed_at and old.task_id not in current_ids:
                items.append(old.model_copy(deep=True))

    floor = datetime.combine(request.start_date, time.min, tzinfo=LOCAL_ZONE)
    if not_before is not None:
        floor = max(floor, not_before.astimezone(LOCAL_ZONE))
    if previous and previous.not_before:
        floor = max(floor, previous.not_before.astimezone(LOCAL_ZONE))
    if previous and previous.feedback:
        floor = max(floor, previous.feedback.at.astimezone(LOCAL_ZONE))
    if feedback:
        floor = max(floor, feedback.at.astimezone(LOCAL_ZONE))
    if floor.second or floor.microsecond:
        floor = (floor + timedelta(minutes=1)).replace(second=0, microsecond=0)

    reservations: dict[date, list[tuple[datetime, datetime, str]]] = {}
    used_minutes: dict[date, int] = {}
    groups_by_id = {
        group.id: group for group in request.execution_groups_v4 if group.status != "cancelled"
    }
    reserved_execution_slots: set[tuple[str, datetime, datetime]] = set()

    def reserve(item: PlanItemV2, start: datetime, end: datetime) -> None:
        group_id = item.execution_group_id
        if group_id:
            reservation_key = (group_id, start, end)
            if reservation_key not in reserved_execution_slots:
                group = groups_by_id.get(group_id)
                shared_minutes = (
                    item.group_duration_minutes
                    or (group.duration_minutes if group is not None else None)
                    or item.duration_minutes
                    or 0
                )
                reservations.setdefault(start.date(), []).append((start, end, group_id))
                used_minutes[start.date()] = used_minutes.get(start.date(), 0) + shared_minutes
                reserved_execution_slots.add(reservation_key)
        else:
            reservations.setdefault(start.date(), []).append((start, end, item.task_id))
            if item.duration_minutes:
                used_minutes[start.date()] = (
                    used_minutes.get(start.date(), 0) + item.duration_minutes
                )
        item.start_at = start
        item.end_at = end

    # Completed work is history: keep its original slot even if today's availability changed.
    for index, item in enumerate(items):
        completed_at = request.completed_at.get(item.task_id)
        if completed_at is None:
            continue
        old = previous_items.get(item.task_id)
        if old is not None:
            item = old.model_copy(deep=True)
            items[index] = item
        item.completed = True
        item.schedule_status = "completed"
        item.reason = "已完成；历史事实和原时段锁定"
        if old and old.start_at and old.end_at:
            reserve(item, old.start_at.astimezone(LOCAL_ZONE), old.end_at.astimezone(LOCAL_ZONE))
        else:
            item.start_at = None
            item.end_at = None
    by_id = {item.task_id: item for item in items}
    group_by_member = {
        task_id: group for group in groups_by_id.values() for task_id in group.member_task_ids
    }
    grouped_task_ids = set(group_by_member)
    for item in items:
        if item.task_id in request.cancelled_tasks and not item.completed:
            item.schedule_status = "cancelled"
            item.start_at = None
            item.end_at = None
            item.reason_code = "user_cancelled"
            item.reason = "已取消：" + request.cancelled_tasks[item.task_id]
            item.missing_fields = []

    # Keep feasible assignments, preferring shorter jobs when daily capacity shrinks.
    old_order = sorted(
        (
            item
            for item in items
            if item.task_id in previous_items
            and item.task_id not in grouped_task_ids
            and not item.completed
            and item.schedule_status != "cancelled"
        ),
        key=lambda item: (
            item.duration_minutes if item.duration_minutes is not None else 10**9,
            previous_items[item.task_id].start_at or datetime.max.replace(tzinfo=LOCAL_ZONE),
        ),
    )
    days_by_date = {day.day: day for day in request.days}
    for item in old_order:
        old = previous_items[item.task_id]
        if not old.start_at or not old.end_at or item.schedule_status == "not_generated":
            continue
        start = old.start_at.astimezone(LOCAL_ZONE)
        end = old.end_at.astimezone(LOCAL_ZONE)
        day = days_by_date.get(start.date())
        plot = plot_by_id[item.plot_id]
        _, earliest, due = _task_preferences(plot, item.task_type, item)
        prereqs = _task_prerequisites(plot, item.task_type, item, request.completed_at)
        prereqs_ready = all(
            parent in by_id
            and by_id[parent].start_at is not None
            and by_id[parent].end_at is not None
            and by_id[parent].end_at <= start
            for parent in prereqs
        )
        if (
            day
            and item.schedule_status not in {"pending", "not_generated"}
            and item.window_by_date.get(start.date()) == "available"
            and item.duration_minutes is not None
            and end - start == timedelta(minutes=item.duration_minutes)
            and start >= floor
            and (earliest is None or start.date() >= earliest)
            and (due is None or start.date() <= due)
            and _interval_fits(start, item.duration_minutes, day, reservations, used_minutes)
            and prereqs_ready
        ):
            reserve(item, start, end)
            item.schedule_status = "scheduled"
            item.reason = "沿用仍然可行的旧时段"

    def order_key(item: PlanItemV2) -> tuple[date, int, str]:
        plot = plot_by_id[item.plot_id]
        priority, _, due = _task_preferences(plot, item.task_type, item)
        return due or date.max, priority, item.task_id

    to_place = [
        item
        for item in sorted(items, key=order_key)
        if not item.completed
        and item.schedule_status not in {"not_generated", "scheduled", "cancelled"}
        and item.reason_code != "task_details_pending"
        and item.applicability == "matched"
        and item.necessity == "required"
        and item.duration_minutes is not None
        and item.reason_code != "overdue"
    ]
    while to_place:
        deferred: list[PlanItemV2] = []
        made_progress = False
        handled_groups: set[str] = set()
        for item in to_place:
            group = group_by_member.get(item.task_id)
            if group is not None:
                if group.id in handled_groups:
                    continue
                handled_groups.add(group.id)
                members = [by_id[task_id] for task_id in group.member_task_ids]
                member_ids = set(group.member_task_ids)
                if group.status != "confirmed":
                    for member in members:
                        if member.completed or member.schedule_status == "cancelled":
                            continue
                        member.schedule_status = "pending"
                        member.reason_code = "execution_group_needs_reconfirmation"
                        member.reason = "合并出行成员或内容已变化，请重新确认任务和总时长"
                    continue
                active_members = [
                    member
                    for member in members
                    if not member.completed and member.schedule_status != "cancelled"
                ]
                if not active_members:
                    continue
                prerequisites = list(
                    dict.fromkeys(
                        prerequisite
                        for member in active_members
                        for prerequisite in _task_prerequisites(
                            plot_by_id[member.plot_id],
                            member.task_type,
                            member,
                            request.completed_at,
                        )
                    )
                )
                if member_ids.intersection(prerequisites):
                    for member in members:
                        if member.completed or member.schedule_status == "cancelled":
                            continue
                        member.schedule_status = "pending"
                        member.reason_code = "execution_group_prerequisite_conflict"
                        member.reason = "同一合并出行内存在前置关系；请拆分安排"
                    continue
                if any(
                    by_id.get(parent_id) is None or by_id[parent_id].end_at is None
                    for parent_id in prerequisites
                ):
                    deferred.append(item)
                    continue
                prerequisite_ends = [
                    by_id[parent_id].end_at.astimezone(LOCAL_ZONE)
                    for parent_id in prerequisites
                    if by_id[parent_id].end_at is not None
                ]
                earliest_start = max([floor, *prerequisite_ends])
                day = days_by_date.get(group.execution_date)
                eligible_members = all(
                    member.schedule_status not in {"not_generated", "cancelled", "pending"}
                    and member.applicability == "matched"
                    and member.necessity == "required"
                    and member.duration_minutes is not None
                    for member in active_members
                )
                if not eligible_members:
                    blocker = next(
                        (
                            member
                            for member in active_members
                            if member.schedule_status in {"not_generated", "cancelled", "pending"}
                            or member.applicability != "matched"
                            or member.necessity != "required"
                            or member.duration_minutes is None
                        ),
                        members[0],
                    )
                    for member in active_members:
                        if (
                            member is blocker
                            or member.completed
                            or member.schedule_status == "cancelled"
                        ):
                            continue
                        member.schedule_status = "pending"
                        member.reason_code = "execution_group_member_pending"
                        member.reason = f"合并出行中的“{blocker.title}”尚未满足安排条件"
                    continue

                window_states = [
                    member.window_by_date.get(group.execution_date, "pending")
                    for member in active_members
                ]
                if day is None or any(state != "available" for state in window_states):
                    pending_window = "pending" in window_states
                    for member in active_members:
                        if member.completed or member.schedule_status == "cancelled":
                            continue
                        member.schedule_status = "pending" if pending_window else "unscheduled"
                        member.reason_code = (
                            "execution_group_workability_pending"
                            if pending_window
                            else "execution_group_unworkable"
                        )
                        member.reason = (
                            "合并出行日期的现场与天气条件待确认"
                            if pending_window
                            else "合并出行日期不可作业；请重新确认日期或拆分任务"
                        )
                    continue

                def reserve_group(
                    start: datetime,
                    execution_group: TaskExecutionGroupV4 = group,
                    group_members: list[PlanItemV2] = members,
                ) -> None:
                    end = start + timedelta(minutes=execution_group.duration_minutes)
                    reservation_key = (execution_group.id, start, end)
                    if reservation_key not in reserved_execution_slots:
                        reservations.setdefault(start.date(), []).append(
                            (start, end, execution_group.id)
                        )
                        used_minutes[start.date()] = (
                            used_minutes.get(start.date(), 0) + execution_group.duration_minutes
                        )
                        reserved_execution_slots.add(reservation_key)
                    for member in group_members:
                        if member.completed or member.schedule_status == "cancelled":
                            continue
                        member.start_at = start
                        member.end_at = end
                        member.schedule_status = "scheduled"
                        member.reason_code = "execution_group_scheduled"
                        member.reason = (
                            f"与同地任务合并出行；共享一次{execution_group.duration_minutes}分钟时段，"
                            "各项结果分别记录"
                        )
                        member.execution_group_id = execution_group.id
                        member.group_duration_minutes = execution_group.duration_minutes

                placed = False
                old_group = next(
                    (
                        old
                        for old in (previous.request.execution_groups_v4 if previous else [])
                        if old.id == group.id
                        and old.execution_date == group.execution_date
                        and old.status == "confirmed"
                    ),
                    None,
                )
                current_members_were_grouped = bool(old_group) and set(
                    group.member_task_ids
                ).issubset(old_group.member_task_ids)
                old_members = (
                    [previous_items.get(task_id) for task_id in old_group.member_task_ids]
                    if old_group
                    else []
                )
                old_starts = {entry.start_at for entry in old_members if entry and entry.start_at}
                old_ends = {entry.end_at for entry in old_members if entry and entry.end_at}
                reserved_slot_matches = any(
                    reservation_id == group.id
                    and start.date() == group.execution_date
                    and end - start == timedelta(minutes=group.duration_minutes)
                    for start, end, reservation_id in reservations.get(group.execution_date, [])
                )
                if (
                    old_group
                    and len(old_starts) == len(old_ends) == 1
                    and all(
                        entry is not None and entry.start_at and entry.end_at
                        for entry in old_members
                    )
                ):
                    old_start = next(iter(old_starts)).astimezone(LOCAL_ZONE)
                    if old_start.date() == group.execution_date and (
                        (reserved_slot_matches and current_members_were_grouped)
                        or (
                            old_start >= earliest_start
                            and _interval_fits(
                                old_start,
                                group.duration_minutes,
                                day,
                                reservations,
                                used_minutes,
                            )
                        )
                    ):
                        reserve_group(old_start)
                        for member in active_members:
                            member.reason = "沿用仍然可行的合并出行时段；各项结果分别记录"
                        placed = True
                        made_progress = True
                if not placed:
                    for candidate in _candidate_slots(
                        day,
                        group.duration_minutes,
                        earliest_start,
                        reservations,
                        used_minutes,
                    ):
                        if _interval_fits(
                            candidate,
                            group.duration_minutes,
                            day,
                            reservations,
                            used_minutes,
                        ):
                            reserve_group(candidate)
                            placed = True
                            made_progress = True
                            break
                if not placed:
                    reason = (
                        "合并出行的确认总时长超过当日可用人力"
                        if day.resource_minutes is not None
                        and day.resource_minutes < group.duration_minutes
                        else "所选日期没有足够长的连续时段；请调整总时长或拆分任务"
                    )
                    for member in active_members:
                        member.schedule_status = "unscheduled"
                        member.reason_code = "execution_group_time_unavailable"
                        member.reason = reason
                continue

            plot = plot_by_id[item.plot_id]
            _, earliest, due = _task_preferences(plot, item.task_type, item)
            prerequisites = _task_prerequisites(plot, item.task_type, item, request.completed_at)
            prerequisite_ends = []
            if any(
                by_id.get(parent_id) is None or by_id[parent_id].end_at is None
                for parent_id in prerequisites
            ):
                deferred.append(item)
                continue
            for parent_id in prerequisites:
                parent = by_id[parent_id]
                if parent.end_at:
                    prerequisite_ends.append(parent.end_at.astimezone(LOCAL_ZONE))
            earliest_start = max([floor, *prerequisite_ends])
            placed = False
            candidate_days = list(request.days)
            if request.scheduling_preference == "balanced":
                candidate_days.sort(
                    key=lambda day: (
                        used_minutes.get(day.day, 0)
                        / max(
                            day.resource_minutes
                            or sum(interval.minutes for interval in day.intervals),
                            1,
                        ),
                        used_minutes.get(day.day, 0),
                        day.day,
                    )
                )
            elif request.scheduling_preference == "concentrated":
                occupied_days = set(used_minutes)
                onsite_days = {
                    reserved_day
                    for reserved_day, day_reservations in reservations.items()
                    if any(
                        by_id.get(task_id) is not None and by_id[task_id].category == "onsite"
                        for _, _, task_id in day_reservations
                    )
                }
                candidate_days.sort(
                    key=lambda day: (
                        0 if day.day in onsite_days else 1 if day.day in occupied_days else 2,
                        day.day,
                    )
                )
            for day in candidate_days:
                if earliest and day.day < earliest:
                    continue
                if due and day.day > due:
                    continue
                if _item_window_status(item, plot, day) != "available":
                    continue
                for candidate in _candidate_slots(
                    day,
                    item.duration_minutes or 0,
                    earliest_start,
                    reservations,
                    used_minutes,
                ):
                    if _interval_fits(
                        candidate,
                        item.duration_minutes or 0,
                        day,
                        reservations,
                        used_minutes,
                    ):
                        reserve(
                            item,
                            candidate,
                            candidate + timedelta(minutes=item.duration_minutes or 0),
                        )
                        item.schedule_status = "scheduled"
                        item.reason_code = "scheduled"
                        item.reason = (
                            "非现场事项；按可用时间和任务约束安排"
                            if item.category == "offsite"
                            else "现场与天气均已确认；按用户期限、优先级和任务编号排入最早时段"
                        )
                        placed = True
                        made_progress = True
                        break
                if placed:
                    break
            if not placed:
                status, code, reason = _reasons_if_unplaced(
                    item, plot, request, by_id, prerequisites, floor
                )
                item.schedule_status = status  # type: ignore[assignment]
                item.reason_code = code
                item.reason = reason
        if not made_progress:
            to_place = deferred
            break
        to_place = deferred
    for item in to_place:
        item.schedule_status = "pending"
        item.reason_code = "prerequisite_pending"
        item.reason = "显式前置任务尚未完成或安排"

    return PlanVersionV2(
        schema_version=4,
        parent_id=previous.id if previous else None,
        created_at=created_at or datetime.now(UTC),
        reference_version=reference_version,
        not_before=floor,
        request=request,
        items=items,
        changes=_change_log(previous, items),
        feedback=feedback,
    )


def apply_feedback_v2(
    previous: PlanVersionV2,
    feedback: FeedbackV2,
    *,
    catalog: RuleCatalog | None = None,
) -> PlanVersionV2:
    """Apply field-specific feedback and recompute the next immutable snapshot."""
    request = previous.request.model_copy(deep=True)
    if feedback.kind == "completed":
        item = next((item for item in previous.items if item.task_id == feedback.task_id), None)
        if item is None:
            raise ValueError("找不到要标记完成的任务")
        if item.completed or feedback.task_id in request.completed_at:
            raise ValueError("任务已记录完成")
        if item.start_at is None:
            raise ValueError("未安排的任务不能标记为已完成")
        if feedback.at.astimezone(LOCAL_ZONE) < item.start_at.astimezone(LOCAL_ZONE):
            raise ValueError("完成反馈时间早于任务计划开始时间")
        request.completed_at[feedback.task_id or ""] = feedback.at
    elif feedback.kind == "cancelled":
        item = next((item for item in previous.items if item.task_id == feedback.task_id), None)
        if item is None:
            raise ValueError("找不到要取消的任务")
        if item.completed or item.task_id in request.completed_at:
            raise ValueError("已完成任务不能取消")
        request.cancelled_tasks[item.task_id] = feedback.reason
    elif feedback.kind == "observation":
        plot = next((plot for plot in request.plots if plot.id == feedback.plot_id), None)
        if plot is None:
            raise ValueError("找不到要更新的地块")
        if feedback.field is None:
            raise ValueError("现场观察项缺失")
        value: Any = feedback.value
        if feedback.field in {
            "standing_water",
            "actual_rain_observed",
            "maintenance_need_confirmed",
            "maintenance_problem_confirmed",
            "winter_before_context_confirmed",
            "pest_signs",
            "missing_seedlings",
            "crowding",
        } and value not in {True, False, None}:
            raise ValueError("该观察项需要是是/否/未知")
        if feedback.field == "leaf_count" and value is not None:
            value = float(value)
        changed = getattr(plot, feedback.field) != value
        setattr(plot, feedback.field, value)
        plot.observed_at = feedback.at
        plot.observation_source = "observed"
        if changed and feedback.field in {
            "ditch_connectivity",
            "standing_water",
            "moisture",
            "actual_rain_observed",
        }:
            for day in request.days:
                if day.day < feedback.at.astimezone(LOCAL_ZONE).date():
                    continue
                site, weather = _effective_workability(plot, day)
                current = plot.workability_by_date.get(day.day)
                weather_source = current.weather_source if current else day.weather_source
                weather_snapshot_at = (
                    current.weather_snapshot_at if current else day.weather_snapshot_at
                )
                if feedback.field == "actual_rain_observed":
                    if weather is True:
                        weather = None
                        weather_source = "synthetic"
                        weather_snapshot_at = None
                else:
                    if site is True:
                        site = None
                plot.workability_by_date[day.day] = WorkabilityConfirmation(
                    site=site,
                    weather=weather,
                    weather_source=weather_source,
                    weather_snapshot_at=weather_snapshot_at,
                )
    elif feedback.kind == "workability":
        plot = next((plot for plot in request.plots if plot.id == feedback.plot_id), None)
        if plot is None or feedback.day is None:
            raise ValueError("找不到对应的地块或日期")
        if feedback.day not in {day.day for day in request.days}:
            raise ValueError("作业条件日期不在当前七天内")
        for affected_day in request.days:
            if affected_day.day >= feedback.day:
                plot.workability_by_date[affected_day.day] = WorkabilityConfirmation()
        plot.workability_by_date[feedback.day] = WorkabilityConfirmation(
            site=feedback.site_workable,
            weather=feedback.weather_workable,
            weather_source="user_estimate"
            if feedback.weather_workable is not None
            else "synthetic",
            weather_snapshot_at=feedback.at if feedback.weather_workable is not None else None,
        )
    elif feedback.kind == "resource":
        if feedback.day is None or (feedback.intervals is None and not feedback.resource_unknown):
            raise ValueError("人员变化需要日期和可用时段")
        day = next((day for day in request.days if day.day == feedback.day), None)
        if day is None:
            raise ValueError("人员变化日期不在当前七天内")
        day.intervals = feedback.intervals or []
        day.resource_minutes = (
            None
            if feedback.resource_unknown
            else feedback.resource_minutes
            if feedback.resource_minutes is not None
            else sum(interval.minutes for interval in day.intervals)
        )
    elif feedback.kind == "task_result":
        # The detailed result is an immutable event; it does not rewrite the
        # field profile. Completion is a separate explicit event.
        pass

    request = PlanRequestV2.model_validate(request.model_dump(mode="python"))
    return build_plan_v2(
        request,
        previous,
        feedback,
        catalog=catalog,
        created_at=feedback.at,
    )
