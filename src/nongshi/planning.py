"""Deterministic rule checks, seven-day scheduling, and feedback-based replanning."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from nongshi.schema import (
    Evidence,
    Feedback,
    PlanItem,
    PlanRequest,
    PlanVersion,
    Plot,
    RuleCard,
    Task,
    WindowStatus,
)

OBSERVATION_NAMES = {"ditch_condition": "沟渠情况", "field_access": "田间可进入性"}
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _evidence(rule: RuleCard) -> Evidence:
    return Evidence(
        rule_id=rule.id,
        rule_version=rule.version,
        source_title=rule.source_title,
        source_excerpt=rule.source_excerpt,
        source_url=rule.source_url,
        source_date=rule.source_date,
        source_location=rule.source_location,
        region=rule.region,
        stage=rule.stage,
        conditions=rule.conditions,
        inapplicable_when=rule.inapplicable_when,
        confirmed_observation_fields=rule.confirmed_observation_fields,
        required_prior_task_kinds=rule.required_prior_task_kinds,
        simulation=rule.is_demo,
    )


def _check_rule(
    rule: RuleCard, task: Task, plot: Plot, request: PlanRequest
) -> tuple[WindowStatus, str]:
    for field, excluded in rule.inapplicable_when.items():
        actual = plot.observations.get(field)
        if actual is None or actual == "unknown":
            return "pending", f"待确认{OBSERVATION_NAMES.get(field, field)}以排除不适用情况"
        if actual == excluded:
            return "blocked", f"{OBSERVATION_NAMES.get(field, field)}属于规则不适用情况"
    for field, expected in rule.conditions.items():
        actual = plot.observations.get(field)
        if actual is None or actual == "unknown":
            return "pending", f"待确认{OBSERVATION_NAMES.get(field, field)}"
        if actual != expected:
            return "blocked", f"{OBSERVATION_NAMES.get(field, field)}不满足该任务条件"
        if field in rule.confirmed_observation_fields and field not in plot.observation_times:
            return "pending", f"待记录{OBSERVATION_NAMES.get(field, field)}的现场观察时间"
    task_by_id = {candidate.id: candidate for candidate in request.tasks}
    prior_kinds = {
        task_by_id[parent_id].kind
        for parent_id in task.prerequisites
        if task_by_id[parent_id].plot_id == task.plot_id
    }
    if any(kind not in prior_kinds for kind in rule.required_prior_task_kinds):
        return "pending", "缺少规则要求的同地块前置任务"
    if rule.weather_required:
        if request.weather.status == "unavailable":
            return "pending", "天气不可用，暂不能核对该规则"
        return "pending", "天气条件尚未形成可核对的数据规则"
    return "ready", ""


def _window(
    task: Task, plot: Plot, request: PlanRequest
) -> tuple[WindowStatus, str, list[Evidence]]:
    if task.hours is None:
        return "pending", "待填写预计工时", []
    if not plot.stage_confirmed or not plot.stage:
        return "pending", "待确认地块生育期", []
    if plot.stage != "苗期":
        return "blocked", "本版仅适用于已确认处于苗期的地块", []
    applicable = [
        rule
        for rule in request.rules
        if rule.kind == task.kind
        and rule.region == plot.region
        and rule.stage == plot.stage
        and ((rule.is_demo and request.demo_mode) or (rule.reviewed and not rule.is_demo))
    ]
    if not applicable:
        return "pending", "缺少经核对且适用于本地块的规则依据", []
    outcomes = [(_check_rule(rule, task, plot, request), rule) for rule in applicable]
    for (status, reason), rule in outcomes:
        if status == "ready":
            return status, reason, [_evidence(rule)]
    for (status, reason), rule in outcomes:
        if status == "pending":
            return status, reason, [_evidence(rule)]
    (status, reason), rule = outcomes[0]
    return status, reason, [_evidence(rule)]


def _sort_key(task: Task) -> tuple[int, date, str]:
    return task.priority, task.due_day or date.max, task.id


def _change_log(previous: PlanVersion | None, items: list[PlanItem]) -> list[str]:
    if previous is None:
        return []
    before = {item.task_id: item for item in previous.items}
    changes: list[str] = []
    for item in items:
        prior = before.get(item.task_id)
        if prior is None:
            changes.append(f"{item.task_id}：新增任务")
        elif prior.completed != item.completed:
            changes.append(f"{item.task_id}：已记录完成，原安排保留")
        elif prior.scheduled_date != item.scheduled_date:
            old = prior.scheduled_date.isoformat() if prior.scheduled_date else "未排入"
            new = item.scheduled_date.isoformat() if item.scheduled_date else "未排入"
            changes.append(f"{item.task_id}：{old} → {new}；{item.reason}")
        elif prior.window_status != item.window_status:
            changes.append(f"{item.task_id}：窗口状态变化；{item.reason}")
    return changes


def build_plan(
    request: PlanRequest, previous: PlanVersion | None = None, feedback: Feedback | None = None
) -> PlanVersion:
    """Produce one immutable snapshot; never invent tasks or relax a hard constraint."""
    request = request.model_copy(deep=True)
    # Revalidate mutable models so an edited form cannot bypass field checks.
    request = PlanRequest.model_validate(request.model_dump())
    plots = {plot.id: plot for plot in request.plots}
    if previous:
        current_tasks = {task.id: task for task in request.tasks}
        for old_task in previous.request.tasks:
            if old_task.completed_at is None:
                continue
            current_task = current_tasks.get(old_task.id)
            if current_task is None:
                raise ValueError("已完成任务不可删除")
            if current_task != old_task:
                raise ValueError("已完成任务不可修改")
    previous_items = {item.task_id: item for item in previous.items} if previous else {}
    remaining = {day.day: day.hours for day in request.days}
    minimum_day = max(
        request.start_date,
        previous.not_before if previous and previous.not_before else request.start_date,
        previous.feedback.at.astimezone(LOCAL_ZONE).date()
        if previous and previous.feedback
        else request.start_date,
        feedback.at.astimezone(LOCAL_ZONE).date() if feedback else request.start_date,
    )
    items: dict[str, PlanItem] = {}
    for task in request.tasks:
        status, reason, evidence = _window(task, plots[task.plot_id], request)
        item = PlanItem(
            task_id=task.id,
            plot_id=task.plot_id,
            title=task.title,
            hours=task.hours,
            window_status=status,
            reason=reason,
            evidence=evidence,
        )
        prior = previous_items.get(task.id)
        if task.completed_at is not None:
            item.completed = True
            item.scheduled_date = prior.scheduled_date if prior else None
            item.sequence = prior.sequence if prior else None
            item.window_status = prior.window_status if prior else status
            item.evidence = prior.evidence if prior else evidence
            item.reason = "已完成；原安排锁定"
            if item.scheduled_date in remaining and item.hours is not None:
                remaining[item.scheduled_date] -= item.hours
        items[task.id] = item

    def prerequisite_date(task: Task, day: date) -> bool:
        return all(
            items[parent_id].scheduled_date is not None and items[parent_id].scheduled_date <= day
            for parent_id in task.prerequisites
        )

    def observation_day(task: Task) -> date:
        plot = plots[task.plot_id]
        return max(
            (
                plot.observation_times[field].astimezone(LOCAL_ZONE).date()
                for evidence in items[task.id].evidence
                for field in evidence.confirmed_observation_fields
            ),
            default=request.start_date,
        )

    def place(task: Task, day: date) -> bool:
        item = items[task.id]
        if (
            item.completed
            or item.window_status != "ready"
            or item.hours is None
            or day not in remaining
            or day < minimum_day
            or day < observation_day(task)
            or (task.earliest_day is not None and day < task.earliest_day)
            or (task.due_day is not None and day > task.due_day)
            or remaining[day] + 1e-9 < item.hours
            or not prerequisite_date(task, day)
        ):
            return False
        item.scheduled_date = day
        item.sequence = 1 + max(
            (other.sequence or 0 for other in items.values() if other.scheduled_date == day),
            default=0,
        )
        item.reason = "依据已满足，按优先级、期限和可用工时安排"
        remaining[day] -= item.hours
        return True

    ordered = sorted(request.tasks, key=_sort_key)
    # Keep earlier feasible dates first. Later passes can resolve same-day prerequisites.
    for _ in ordered:
        changed = False
        for task in ordered:
            prior = previous_items.get(task.id)
            if prior and prior.scheduled_date and items[task.id].scheduled_date is None:
                changed |= place(task, prior.scheduled_date)
        if not changed:
            break

    for _ in ordered:
        changed = False
        for task in ordered:
            item = items[task.id]
            if item.scheduled_date is not None or item.completed or item.window_status != "ready":
                continue
            for day in remaining:
                if place(task, day):
                    changed = True
                    break
        if not changed:
            break

    for task in request.tasks:
        item = items[task.id]
        if item.window_status == "ready" and item.scheduled_date is None and not item.completed:
            if minimum_day > request.days[-1].day:
                item.reason = "反馈日期已超出当前七天，请开启新一期计划"
            elif observation_day(task) > request.days[-1].day:
                item.reason = "现场观察时间晚于当前七天，请开启新一期计划"
            elif any(items[parent].scheduled_date is None for parent in task.prerequisites):
                item.reason = "前置任务尚未完成或安排"
            else:
                item.reason = "七天内可用工时不足，或任务日期期限冲突"

    result_items = [items[task.id] for task in request.tasks]
    conflicts = []
    for day in request.days:
        completed_hours = sum(
            item.hours or 0
            for item in result_items
            if item.completed and item.scheduled_date == day.day
        )
        if completed_hours > day.hours + 1e-9:
            conflicts.append(
                f"{day.day.isoformat()}：已完成任务记录 {completed_hours:g} 人时，"
                f"超过当前填写的 {day.hours:g} 人时；历史安排保留，请核对输入"
            )
    return PlanVersion(
        id=str(uuid4()),
        parent_id=previous.id if previous else None,
        created_at=datetime.now(UTC),
        not_before=minimum_day,
        request=request,
        items=result_items,
        changes=_change_log(previous, result_items),
        conflicts=conflicts,
        feedback=feedback,
    )


def apply_feedback(previous: PlanVersion, feedback: Feedback) -> PlanVersion:
    """Apply a user-confirmed event to a copy, preserving all prior versions."""
    request = previous.request.model_copy(deep=True)
    if feedback.kind == "completed":
        task = next((task for task in request.tasks if task.id == feedback.task_id), None)
        if task is None:
            raise ValueError("找不到要标记完成的任务")
        if task.completed_at is not None:
            raise ValueError("任务已记录完成")
        task.completed_at = feedback.at
    elif feedback.kind == "observation":
        plot = next((plot for plot in request.plots if plot.id == feedback.plot_id), None)
        if plot is None:
            raise ValueError("找不到要更新的地块")
        if feedback.field not in OBSERVATION_NAMES:
            raise ValueError("不支持的现场观察项")
        plot.observations[feedback.field] = feedback.value or "unknown"
        if feedback.value == "unknown":
            plot.observation_times.pop(feedback.field, None)
        else:
            plot.observation_times[feedback.field] = feedback.at
    else:
        day = next((day for day in request.days if day.day == feedback.day), None)
        if day is None:
            raise ValueError("调整日期不在当前七天内")
        if feedback.hours is None:
            raise ValueError("请填写可用工时")
        day.hours = feedback.hours
    return build_plan(request, previous=previous, feedback=feedback)
