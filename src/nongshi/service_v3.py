"""Shared business service used by the form, chat tools, exports, and history."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import uuid4

from nongshi.planning_v2 import apply_feedback_v2, build_plan_v2
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import RuleCatalog, load_rule_catalog
from nongshi.schema_v2 import (
    LOCAL_ZONE,
    DayWindow,
    FeedbackV2,
    FieldProfile,
    PlanItemV2,
    PlanRequestV2,
    PlanVersionV2,
    RuleEvidenceV2,
)
from nongshi.schema_v3 import (
    AgentProposalOutcomeV3,
    AgentProposalRecordV3,
    AgentProposalV3,
    EventCommitResultV3,
    FactEventInputV3,
    FieldDraftV3,
    FieldEvaluationV3,
    FieldStateV3,
    PlanAdoptionResultV3,
    PlanChangeV3,
    PlanPreviewV3,
    PlanVersionV3,
    RuleAdoptionRecordV3,
    TaskActionV3,
    WeatherSnapshotV3,
)
from nongshi.tasks_v4 import (
    TaskExecutionGroupSaveResultV4,
    TaskExecutionGroupV4,
    TaskSeriesSaveResultV4,
    TaskSeriesUpdateResultV4,
    TaskSeriesV4,
    expand_task_series_all,
)


def align_draft_dates(draft: FieldDraftV3, *, start_date: date | None = None) -> FieldDraftV3:
    """Retain confirmations by absolute date, leaving newly added dates unknown."""
    start = start_date or draft.start_date
    if start is None:
        return FieldDraftV3.model_validate(draft.model_dump(mode="python"))
    supplied = {day.day: day for day in draft.days}
    dates = [start + timedelta(days=offset) for offset in range(7)]
    days = (
        [
            supplied[day].model_copy(deep=True)
            if day in supplied
            else DayWindow(day=day, site_workable=None, weather_workable=None)
            for day in dates
        ]
        if draft.days
        else []
    )
    profile = draft.profile.model_copy(deep=True) if draft.profile else None
    if profile:
        profile.workability_by_date = {
            day: confirmation
            for day, confirmation in profile.workability_by_date.items()
            if day in dates
        }
    return FieldDraftV3.model_validate(
        draft.model_copy(update={"start_date": start, "days": days, "profile": profile}).model_dump(
            mode="python"
        )
    )


class WorkbenchServiceV3:
    """Keep business state shared between structured UI and model tools."""

    def __init__(
        self,
        store: WorkbenchStoreV3,
        *,
        catalog: RuleCatalog | None = None,
        today: Callable[[], date] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog or load_rule_catalog()
        self._now = now or (lambda: datetime.now(UTC))
        self._today = today or (lambda: self._now().astimezone(LOCAL_ZONE).date())

    def save_field_draft(
        self,
        draft: FieldDraftV3,
        *,
        expected_version: int | None,
        operation_id: str,
        proposal_id: str | None = None,
    ) -> FieldStateV3:
        return self.store.save_field_draft(
            align_draft_dates(draft),
            expected_version=expected_version,
            operation_id=operation_id,
            proposal_id=proposal_id,
        )

    def get_field(self, field_id: str) -> FieldStateV3:
        return self.store.get_field(field_id)

    def list_fields(self, *, mode: Literal["real", "demo"] | None = None) -> list[FieldStateV3]:
        return self.store.list_fields(mode=mode)

    def list_events(self, field_id: str):  # type: ignore[no-untyped-def]
        return self.store.list_events(field_id)

    def save_task_series(
        self,
        series: TaskSeriesV4,
        *,
        expected_draft_version: int,
        expected_fact_version: int,
        expected_task_version: int | None = None,
        proposal_id: str | None = None,
        operation_id: str,
    ) -> TaskSeriesSaveResultV4:
        """Persist a user-confirmed task pattern; planning remains a separate preview step."""
        state = self.get_field(series.field_id)
        if state.draft.profile is None:
            raise ValueError("先保存地块档案，再建立农事任务")
        instances = expand_task_series_all(series)
        self._validate_task_dependencies(series.field_id, instances)
        return self.store.save_task_series(
            series,
            expected_draft_version=expected_draft_version,
            expected_fact_version=expected_fact_version,
            expected_task_version=expected_task_version,
            proposal_id=proposal_id,
            operation_id=operation_id,
        )

    def list_task_series(self, field_id: str) -> list[TaskSeriesV4]:
        return self.store.list_task_series(field_id)

    @staticmethod
    def _execution_group_signature(instances) -> str:  # type: ignore[no-untyped-def]
        fingerprint = [
            {
                "id": task.id,
                "field_id": task.field_id,
                "occurrence_date": task.occurrence_date.isoformat(),
                "earliest_date": task.earliest_date.isoformat(),
                "due_date": task.due_date.isoformat(),
                "duration_minutes": task.duration_minutes,
                "priority": task.priority,
                "prerequisites": task.prerequisites,
                "template_id": task.template.id,
                "template_version": task.template.version,
                "task_type": task.template.task_type,
                "category": task.template.category,
                "status": task.status,
            }
            for task in sorted(instances, key=lambda item: item.id)
        ]
        raw = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def list_task_execution_groups(self, field_id: str) -> list[TaskExecutionGroupV4]:
        """Mark a confirmed group stale if its membership or task snapshot has changed."""
        instances = {item.id: item for item in self.store.list_task_instances(field_id)}
        cancelled = {
            event.feedback.task_id
            for event in self._active_events(field_id)
            if event.feedback and event.feedback.kind == "cancelled" and event.feedback.task_id
        }
        result = []
        for group in self.store.list_task_execution_groups(field_id):
            if group.status == "cancelled":
                result.append(group)
                continue
            members = [instances.get(task_id) for task_id in group.member_task_ids]
            current_signature = self._execution_group_signature(members) if all(members) else ""
            stale = (
                not all(members)
                or current_signature != group.member_signature
                or bool(cancelled.intersection(group.member_task_ids))
            )
            result.append(
                group.model_copy(
                    update={"status": "needs_reconfirmation" if stale else "confirmed"}
                )
            )
        return result

    def save_task_execution_group(
        self,
        *,
        field_id: str,
        group_id: str,
        member_task_ids: list[str],
        execution_date: date,
        duration_minutes: int,
        expected_task_version: int,
        operation_id: str,
    ) -> TaskExecutionGroupSaveResultV4:
        """Confirm or reconfirm a same-day outing and its shared time estimate."""
        if not operation_id.strip():
            raise ValueError("保存合并出行需要操作编号")
        replay = self.store.replay_task_execution_group_save(
            field_id=field_id,
            group_id=group_id,
            member_task_ids=member_task_ids,
            execution_date=execution_date,
            duration_minutes=duration_minutes,
            expected_task_version=expected_task_version,
            operation_id=operation_id,
        )
        if replay is not None:
            return replay
        if len(member_task_ids) < 2 or len(member_task_ids) != len(set(member_task_ids)):
            raise ValueError("请选择至少两项不同任务")
        if duration_minutes <= 0:
            raise ValueError("合并出行总时长必须是正整数分钟")
        state = self.get_field(field_id)
        if state.task_version != expected_task_version:
            raise ValueError("版本冲突：任务已变化，请刷新后重新确认合并出行")
        all_instances = {item.id: item for item in self.store.list_task_instances(field_id)}
        members = [all_instances.get(task_id) for task_id in member_task_ids]
        if not all(members):
            raise ValueError("所选任务已不存在，请重新选择")
        active_members = [item for item in members if item is not None]
        if any(
            item.field_id != field_id
            or item.status == "superseded"
            or item.template.category != "onsite"
            or item.template.task_type not in {"inspection", "follow_up"}
            or item.duration_minutes is None
            for item in active_members
        ):
            raise ValueError("仅可合并时长已确认的现场巡田与问题复查任务")
        if any(
            not item.earliest_date <= execution_date <= item.due_date for item in active_members
        ):
            raise ValueError("合并日期必须落在每项任务的日期窗口内")
        member_ids = set(member_task_ids)
        if any(member_ids.intersection(item.prerequisites) for item in active_members):
            raise ValueError("有前置关系的任务不能安排在同一执行组，请分开安排")
        completed = set(self._completion_facts(field_id, None))
        if completed.intersection(member_ids):
            raise ValueError("已完成任务不能加入新的出行组")
        cancelled = {
            event.feedback.task_id
            for event in self._active_events(field_id)
            if event.feedback and event.feedback.kind == "cancelled" and event.feedback.task_id
        }
        if cancelled.intersection(member_ids):
            raise ValueError("已取消任务不能加入出行组")
        old = next(
            (
                group
                for group in self.store.list_task_execution_groups(field_id)
                if group.id == group_id
            ),
            None,
        )
        for group in self.list_task_execution_groups(field_id):
            if group.id != group_id and group.status != "cancelled":
                if member_ids.intersection(group.member_task_ids):
                    raise ValueError("有任务已属于另一合并出行组")
        group = TaskExecutionGroupV4(
            id=group_id,
            field_id=field_id,
            member_task_ids=member_task_ids,
            execution_date=execution_date,
            duration_minutes=duration_minutes,
            version=old.version + 1 if old else 1,
            member_signature=self._execution_group_signature(active_members),
            status="confirmed",
            confirmed_at=self._now(),
        )
        return self.store.save_task_execution_group(
            group,
            expected_task_version=expected_task_version,
            operation_id=operation_id,
        )

    def cancel_task_execution_group(
        self,
        field_id: str,
        group_id: str,
        *,
        expected_task_version: int,
        operation_id: str,
    ) -> TaskExecutionGroupSaveResultV4:
        """Release members back to individual scheduling while retaining group history."""
        group = next(
            (
                item
                for item in self.store.list_task_execution_groups(field_id)
                if item.id == group_id
            ),
            None,
        )
        if group is None:
            raise KeyError("找不到合并出行组")
        replay = self.store.replay_task_execution_group_save(
            field_id=field_id,
            group_id=group_id,
            member_task_ids=group.member_task_ids,
            execution_date=group.execution_date,
            duration_minutes=group.duration_minutes,
            expected_task_version=expected_task_version,
            operation_id=operation_id,
            action="cancel_task_execution_group",
        )
        if replay is not None:
            return replay
        if group.status == "cancelled":
            raise ValueError("合并出行已取消")
        cancelled = group.model_copy(
            update={
                "version": group.version + 1,
                "status": "cancelled",
                "confirmed_at": self._now(),
            }
        )
        return self.store.save_task_execution_group(
            cancelled,
            expected_task_version=expected_task_version,
            operation_id=operation_id,
            action="cancel_task_execution_group",
        )

    def list_field_issues(self, field_id: str):  # type: ignore[no-untyped-def]
        return self.store.list_field_issues(field_id)

    def update_task_series(
        self,
        series: TaskSeriesV4,
        *,
        expected_draft_version: int,
        expected_fact_version: int,
        operation_id: str,
    ) -> TaskSeriesUpdateResultV4:
        """Revise future open occurrences while preserving executed history snapshots."""
        series = TaskSeriesV4.model_validate(series.model_dump(mode="python"))
        state = self.get_field(series.field_id)
        current = self.store.get_task_series(series.field_id, series.id)
        if (state.draft_version, state.fact_version) != (
            expected_draft_version,
            expected_fact_version,
        ):
            raise ValueError("版本冲突：地块信息已变化，请刷新后修改周期")
        if series.template.version < current.template.version or (
            series.template.version == current.template.version and series != current
        ):
            raise ValueError("修改周期内容时请递增模板版本")
        protected = set(self._completion_facts(series.field_id, None))
        for event in self._active_events(series.field_id):
            feedback = event.feedback
            if feedback and feedback.kind in {"cancelled", "task_result"} and feedback.task_id:
                protected.add(feedback.task_id)
        instances = expand_task_series_all(series)
        self._validate_task_dependencies(series.field_id, instances, replacing_series=series.id)
        return self.store.update_task_series(
            series,
            instances,
            blocked_instance_ids=protected,
            expected_draft_version=expected_draft_version,
            expected_fact_version=expected_fact_version,
            expected_task_version=state.task_version,
            operation_id=operation_id,
        )

    def _validate_task_dependencies(
        self,
        field_id: str,
        proposed_instances,
        *,
        replacing_series: str | None = None,
    ) -> None:  # type: ignore[no-untyped-def]
        state = self.get_field(field_id)
        existing = [
            instance
            for instance in self.store.list_task_instances(field_id)
            if instance.series_id != replacing_series
        ]
        instances = [*existing, *proposed_instances]
        task_ids = {instance.id for instance in instances}
        task_ids.update({f"{field_id}-I", f"{field_id}-M"})
        if state.draft.profile:
            task_ids.update(state.draft.profile.completed_maintenance_task_ids)
        task_ids.update(self._completion_facts(field_id, None))
        graph: dict[str, list[str]] = {}
        for instance in instances:
            unknown = set(instance.prerequisites).difference(task_ids)
            if unknown:
                raise ValueError(f"前置任务不属于当前地块或已不存在：{', '.join(sorted(unknown))}")
            graph[instance.id] = list(instance.prerequisites)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("任务前置关系形成循环")
            if task_id in visited:
                return
            visiting.add(task_id)
            for prerequisite in graph.get(task_id, []):
                if prerequisite in graph:
                    visit(prerequisite)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in graph:
            visit(task_id)

    def commit_events(
        self,
        field_id: str,
        events: list[FactEventInputV3],
        *,
        operation_id: str,
        expected_fact_version: int,
        proposal_id: str | None = None,
        expected_draft_version: int | None = None,
    ) -> EventCommitResultV3:
        for event in events:
            feedback = event.feedback
            if (
                feedback
                and feedback.kind == "workability"
                and (feedback.intervals is not None or feedback.resource_minutes is not None)
            ):
                raise ValueError("作业条件反馈不能包含可用时段或人员工时")
            if (
                feedback
                and feedback.kind == "resource"
                and (feedback.site_workable is not None or feedback.weather_workable is not None)
            ):
                raise ValueError("人员时间反馈不能包含田间作业条件")
        recorded_events = self.store.list_events(field_id)
        existing_ids = {event.event_id for event in recorded_events}
        replayed_event_ids = all(event.event_id in existing_ids for event in events)
        state = self.get_field(field_id)
        if (
            not replayed_event_ids
            and expected_draft_version is not None
            and state.draft_version != expected_draft_version
        ):
            raise ValueError("版本冲突：地块档案已变化，请刷新任务详情后重试")
        if not replayed_event_ids and state.fact_version != expected_fact_version:
            raise ValueError(
                f"版本冲突：现场事实为 v{state.fact_version}，提交依据为 v{expected_fact_version}"
            )
        if not replayed_event_ids:
            current = self.store.get_current_plan(field_id)
            current_items = list(current.items) if current else []
            recorded_completions = set(
                self._completion_facts(field_id, current.plan if current else None)
            )
            cancelled_ids = {
                event.feedback.task_id
                for event in self._active_events(field_id)
                if event.feedback and event.feedback.kind == "cancelled"
            }
            by_id = {event.event_id: event for event in recorded_events}
            superseded = {event.correction_of for event in recorded_events if event.correction_of}
            for event in events:
                feedback = event.feedback
                target_feedback = None
                if event.correction_of:
                    target = by_id.get(event.correction_of)
                    if target is None:
                        raise ValueError("更正引用的原事件不存在")
                    if target.event_id in superseded:
                        raise ValueError("只能更正当前有效的最新事件，请刷新后重试")
                    ancestor = target
                    while ancestor.feedback is None and ancestor.correction_of:
                        ancestor = by_id[ancestor.correction_of]
                    target_feedback = ancestor.feedback
                    if (
                        feedback
                        and target_feedback
                        and (
                            feedback.kind,
                            feedback.task_id,
                            feedback.plot_id or field_id,
                            feedback.field,
                            feedback.day,
                        )
                        != (
                            target_feedback.kind,
                            target_feedback.task_id,
                            target_feedback.plot_id or field_id,
                            target_feedback.field,
                            target_feedback.day,
                        )
                    ):
                        raise ValueError("更正必须对应原事件的同一任务或现场事实")
                    # A tombstone has no active completion to release. Reviving it
                    # must still respect a completion recorded on another chain.
                    if target.feedback and target.feedback.kind == "completed":
                        recorded_completions.discard(target.feedback.task_id)
                    superseded.add(event.correction_of)
                by_id[event.event_id] = event
                if feedback and feedback.kind == "cancelled":
                    if feedback.task_id in recorded_completions:
                        raise ValueError("已完成任务不能取消")
                    if not any(item.task_id == feedback.task_id for item in current_items):
                        raise ValueError("找不到当前地块要取消的任务")
                    cancelled_ids.add(feedback.task_id)
                    continue
                if not feedback or feedback.kind != "completed":
                    if feedback and feedback.kind == "task_result":
                        item = next(
                            (
                                task
                                for task in current_items
                                if task.task_id == feedback.task_id and task.plot_id == field_id
                            ),
                            None,
                        )
                        if item is None or item.start_at is None:
                            raise ValueError("只能为当前已安排任务记录执行结果")
                        if feedback.at.astimezone(LOCAL_ZONE) < item.start_at.astimezone(
                            LOCAL_ZONE
                        ):
                            raise ValueError("执行结果时间早于任务计划开始时间")
                        declared = set((item.template_snapshot or {}).get("result_fields", []))
                        if declared and set(feedback.result_values).difference(declared):
                            raise ValueError("执行结果包含任务模板未声明的记录项")
                    continue
                if feedback.task_id in cancelled_ids:
                    raise ValueError("任务已取消，不能直接记录完成")
                if feedback.task_id in recorded_completions:
                    raise ValueError("任务已记录完成；同一任务不能重复提交完成事件")
                item = next(
                    (
                        task
                        for task in current_items
                        if task.task_id == feedback.task_id and task.plot_id == field_id
                    ),
                    None,
                )
                if item is None or item.start_at is None:
                    raise ValueError("只能记录当前已安排且尚未完成的任务")
                if feedback.at.astimezone(LOCAL_ZONE) < item.start_at.astimezone(LOCAL_ZONE):
                    raise ValueError("完成时间早于任务计划开始时间")
                recorded_completions.add(feedback.task_id)
        return self.store.commit_events(
            field_id,
            events,
            expected_fact_version=expected_fact_version,
            operation_id=operation_id,
            proposal_id=proposal_id,
            expected_draft_version=expected_draft_version,
        )

    def record_task_result(
        self,
        field_id: str,
        task_id: str,
        result_values: dict[str, str],
        *,
        notes: str = "",
        completed: bool = False,
        resolve_issue_id: str | None = None,
        expected_draft_version: int,
        expected_fact_version: int,
        operation_id: str,
    ) -> EventCommitResultV3:
        """Save actual task outcomes and, separately, an optional completion fact."""
        at = self._now()
        events = [
            FactEventInputV3(
                feedback=FeedbackV2(
                    kind="task_result",
                    task_id=task_id,
                    plot_id=field_id,
                    at=at,
                    result_values=result_values,
                    result_notes=notes,
                    resolved_issue_id=resolve_issue_id,
                )
            )
        ]
        if completed:
            events.append(
                FactEventInputV3(
                    feedback=FeedbackV2(kind="completed", task_id=task_id, plot_id=field_id, at=at)
                )
            )
        return self.commit_events(
            field_id,
            events,
            operation_id=operation_id,
            expected_fact_version=expected_fact_version,
            expected_draft_version=expected_draft_version,
        )

    def _editable_task(
        self,
        field_id: str,
        task_id: str,
        *,
        expected_draft_version: int,
        expected_fact_version: int,
    ) -> tuple[FieldStateV3, PlanItemV2]:
        state = self.get_field(field_id)
        if (state.draft_version, state.fact_version) != (
            expected_draft_version,
            expected_fact_version,
        ):
            raise ValueError("版本冲突：地块档案或现场事实已变化，请刷新任务详情后重试")
        current = self.get_current_plan(field_id)
        if current is None or state.draft.profile is None:
            raise ValueError("请先采用当前地块的计划，再修改任务")
        item = next((item for item in current.items if item.task_id == task_id), None)
        if item is None:
            raise ValueError("找不到当前地块的任务")
        if task_id in self._completion_facts(field_id, current.plan):
            raise ValueError("已完成任务不可修改或取消；请使用显式更正完成记录")
        if any(
            event.feedback
            and event.feedback.kind == "cancelled"
            and event.feedback.task_id == task_id
            for event in self._active_events(field_id)
        ):
            raise ValueError("任务已取消，不能重复修改或取消")
        if state.plan_stale or current.task_version != state.task_version:
            raise ValueError("任务或地块情况已变化，请先生成并采用当前预览")
        return state, item

    def edit_task(
        self,
        field_id: str,
        task_id: str,
        *,
        preferred_date: date,
        duration_minutes: int,
        priority: int,
        expected_draft_version: int,
        expected_fact_version: int,
        expected_task_version: int | None = None,
        operation_id: str,
    ) -> PlanPreviewV3:
        """Save user-confirmed task preferences, then preview current-fact scheduling."""
        request = {
            "field_id": field_id,
            "task_id": task_id,
            "preferred_date": preferred_date,
            "duration_minutes": duration_minutes,
            "priority": priority,
            "expected_draft_version": expected_draft_version,
            "expected_fact_version": expected_fact_version,
            "expected_task_version": expected_task_version,
        }

        def prepare() -> TaskActionV3:
            state, item = self._editable_task(
                field_id,
                task_id,
                expected_draft_version=expected_draft_version,
                expected_fact_version=expected_fact_version,
            )
            if expected_task_version is not None and expected_task_version != state.task_version:
                raise ValueError("任务版本冲突，请刷新任务详情后重试")
            start = state.draft.start_date or (self._today() + timedelta(days=1))
            if not start <= preferred_date <= start + timedelta(days=6):
                raise ValueError("指定日期必须位于当前七天计划内")
            if preferred_date < self._today():
                raise ValueError("不能把未完成任务改到过去日期")
            if item.template_snapshot is not None:
                instance = self.store.get_task_instance(field_id, task_id)
                updated_instance = instance.model_copy(
                    update={
                        "occurrence_date": preferred_date,
                        "earliest_date": preferred_date,
                        "due_date": preferred_date,
                        "duration_minutes": duration_minutes,
                        "priority": priority,
                        "status": "ready" if not instance.missing_fields else "pending",
                    }
                )
                return TaskActionV3(
                    field_id=field_id,
                    instance=updated_instance,
                    expected_draft_version=expected_draft_version,
                    expected_fact_version=expected_fact_version,
                    expected_task_version=state.task_version,
                )
            prefix = item.task_type
            profile = state.draft.profile.model_copy(
                update={
                    f"{prefix}_earliest_date": preferred_date,
                    f"{prefix}_due_date": preferred_date,
                    f"{prefix}_duration_minutes": duration_minutes,
                    f"{prefix}_priority": priority,
                }
            )
            draft = FieldDraftV3.model_validate(
                state.draft.model_copy(update={"profile": profile}).model_dump(mode="python")
            )
            return TaskActionV3(
                field_id=field_id,
                draft=draft,
                expected_draft_version=expected_draft_version,
                expected_fact_version=expected_fact_version,
            )

        intent = self._task_action_intent(operation_id, "edit_task", request, prepare)
        if intent.instance is not None:
            self.store.update_task_instance(
                intent.instance,
                expected_task_version=intent.expected_task_version or 0,
                operation_id=f"task-action:{intent.id}:instance",
            )
            return self.store.task_action_preview(intent) or self.preview_plan(
                field_id, operation_id=f"task-action:{intent.id}:preview"
            )
        if intent.draft is None:
            raise ValueError("任务修改提案缺少有效内容")
        self.store.save_field_draft(
            intent.draft,
            expected_version=expected_draft_version,
            expected_fact_version=expected_fact_version,
            operation_id=f"task-action:{intent.id}:draft",
        )
        return self.store.task_action_preview(intent) or self.preview_plan(
            field_id, operation_id=f"task-action:{intent.id}:preview"
        )

    def cancel_task(
        self,
        field_id: str,
        task_id: str,
        *,
        reason: str,
        expected_draft_version: int,
        expected_fact_version: int,
        operation_id: str,
    ) -> PlanPreviewV3:
        """Record cancellation as an immutable fact, retaining the task and its reason."""
        request = {
            "field_id": field_id,
            "task_id": task_id,
            "reason": reason,
            "expected_draft_version": expected_draft_version,
            "expected_fact_version": expected_fact_version,
        }

        def prepare() -> TaskActionV3:
            self._editable_task(
                field_id,
                task_id,
                expected_draft_version=expected_draft_version,
                expected_fact_version=expected_fact_version,
            )
            event = FactEventInputV3(
                feedback=FeedbackV2(
                    kind="cancelled",
                    task_id=task_id,
                    plot_id=field_id,
                    at=self._now(),
                    reason=reason,
                )
            )
            return TaskActionV3(
                field_id=field_id,
                event=event,
                expected_draft_version=expected_draft_version,
                expected_fact_version=expected_fact_version,
            )

        intent = self._task_action_intent(operation_id, "cancel_task", request, prepare)
        # The frozen intent was validated before registration. The store atomically
        # checks both versions or replays this exact event, including its original ID/time.
        self.store.commit_events(
            field_id,
            [intent.event],
            expected_fact_version=expected_fact_version,
            expected_draft_version=expected_draft_version,
            operation_id=f"task-action:{intent.id}:fact",
        )
        return self.store.task_action_preview(intent) or self.preview_plan(
            field_id, operation_id=f"task-action:{intent.id}:preview"
        )

    def _task_action_intent(
        self,
        operation_id: str,
        action: str,
        request: dict[str, object],
        prepare: Callable[[], TaskActionV3],
    ) -> TaskActionV3:
        cached = self.store.task_action(operation_id, action, request)
        if cached is not None:
            return cached
        try:
            intent = prepare()
        except ValueError:
            # An identical concurrent request may have registered and committed
            # after our first lookup. Recover its intent before treating guards as stale.
            cached = self.store.task_action(operation_id, action, request)
            if cached is not None:
                return cached
            raise
        return self.store.prepare_task_action(operation_id, action, request, intent)

    def pending_agent_proposals(self, field_id: str) -> list[AgentProposalV3]:
        return [
            record.proposal
            for record in self.store.agent_proposals(field_id)
            if record.status == "pending"
        ]

    def agent_proposal_records(self, field_id: str) -> list[AgentProposalRecordV3]:
        return self.store.agent_proposals(field_id)

    def ignore_agent_proposal(self, proposal_id: str) -> AgentProposalRecordV3:
        record = self.store.agent_proposal(proposal_id)
        if record.status != "pending":
            raise ValueError("只有待确认修改可以忽略")
        ignored = record.model_copy(update={"status": "ignored"})
        self.store.update_agent_proposal(ignored)
        return ignored

    def confirm_agent_proposal(
        self, proposal_id: str, *, operation_id: str
    ) -> AgentProposalOutcomeV3:
        record = self.store.agent_proposal(proposal_id)
        if record.status == "ignored":
            raise ValueError("修改提案已忽略，请重新提出")
        if record.status == "confirmed":
            if record.confirmed_operation_id != operation_id:
                raise ValueError("修改已确认，不能用新操作编号重复提交")
            return self.retry_agent_preview(proposal_id)
        proposal = record.proposal
        if proposal.kind == "field_update" and proposal.draft is not None:
            saved = self.save_field_draft(
                proposal.draft,
                expected_version=proposal.expected_draft_version,
                operation_id=operation_id,
                proposal_id=proposal_id,
            )

            return self._preview_confirmed_agent_proposal(
                proposal_id, draft_version=saved.draft_version
            )
        if proposal.kind == "facts" and proposal.events:
            saved = self.commit_events(
                proposal.field_id,
                proposal.events,
                operation_id=operation_id,
                expected_fact_version=proposal.expected_fact_version or 0,
                proposal_id=proposal_id,
            )
            return self._preview_confirmed_agent_proposal(
                proposal_id, fact_version=saved.fact_version
            )
        if proposal.kind == "task_series" and proposal.task_series is not None:
            self.save_task_series(
                proposal.task_series,
                expected_draft_version=proposal.expected_draft_version or 0,
                expected_fact_version=proposal.expected_fact_version or 0,
                expected_task_version=proposal.expected_task_version,
                proposal_id=proposal_id,
                operation_id=operation_id,
            )
            return self._preview_confirmed_agent_proposal(proposal_id)
        raise ValueError("待确认修改内容不完整")

    def retry_agent_preview(self, proposal_id: str) -> AgentProposalOutcomeV3:
        record = self.store.agent_proposal(proposal_id)
        if record.status != "confirmed":
            raise ValueError("请先确认并保存修改，再重新计算")
        if record.preview_id:
            preview = self.store.load_preview(record.preview_id)
            if preview.status == "preview":
                return AgentProposalOutcomeV3(
                    proposal_id=proposal_id,
                    plan_state="preview_ready",
                    preview=preview,
                    fact_version=preview.expected_fact_version,
                    draft_version=preview.expected_draft_version,
                )
            if preview.status == "adopted":
                return AgentProposalOutcomeV3(
                    proposal_id=proposal_id,
                    plan_state="no_change",
                    fact_version=preview.expected_fact_version,
                    draft_version=preview.expected_draft_version,
                )
        state = self.get_field(record.proposal.field_id)
        return self._preview_confirmed_agent_proposal(
            proposal_id,
            fact_version=state.fact_version,
            draft_version=state.draft_version,
        )

    def _preview_confirmed_agent_proposal(
        self,
        proposal_id: str,
        *,
        fact_version: int | None = None,
        draft_version: int | None = None,
    ) -> AgentProposalOutcomeV3:
        record = self.store.agent_proposal(proposal_id)
        try:
            preview = self.preview_plan(
                record.proposal.field_id,
                operation_id=f"agent-proposal-{proposal_id}-preview-{uuid4()}",
                source="assistant",
            )
        except (ValueError, KeyError, RuntimeError) as error:
            self.store.update_agent_proposal(record.model_copy(update={"last_error": str(error)}))
            return AgentProposalOutcomeV3(
                proposal_id=proposal_id,
                plan_state="calculation_failed",
                fact_version=fact_version,
                draft_version=draft_version,
                error_message=f"事实已保存，安排待更新：{error}",
            )
        if preview.expected_plan_id and all(
            change.kind == "unchanged" for change in preview.version.changes
        ):
            self.cancel_preview(preview.id)
            self.store.update_agent_proposal(record.model_copy(update={"last_error": ""}))
            return AgentProposalOutcomeV3(
                proposal_id=proposal_id,
                plan_state="no_change",
                fact_version=preview.expected_fact_version,
                draft_version=preview.expected_draft_version,
            )
        self.store.update_agent_proposal(
            record.model_copy(update={"preview_id": preview.id, "last_error": ""})
        )
        return AgentProposalOutcomeV3(
            proposal_id=proposal_id,
            plan_state="preview_ready",
            fact_version=preview.expected_fact_version,
            draft_version=preview.expected_draft_version,
            preview=preview,
        )

    def evaluate_field(self, field_id: str) -> FieldEvaluationV3:
        state = self.get_field(field_id)
        now = self._now()
        if state.draft.profile is None:
            return FieldEvaluationV3(
                field_id=field_id,
                draft_version=state.draft_version,
                fact_version=state.fact_version,
                evaluated_at=now,
                profile_ready=False,
                missing_fields=["field_profile"],
                clarification_questions=["先填写地块名称和基本情况"],
                ruleset_version=self.catalog.delivery_version,
            )
        request, previous = self._build_request(state)
        plan = self._evaluate_request(state, request, previous)
        missing = list(dict.fromkeys(field for item in plan.items for field in item.missing_fields))
        return FieldEvaluationV3(
            field_id=field_id,
            draft_version=state.draft_version,
            fact_version=state.fact_version,
            task_version=state.task_version,
            evaluated_at=now,
            profile_ready=True,
            items=plan.items,
            missing_fields=missing,
            clarification_questions=self._questions(missing),
            ruleset_version=self.catalog.delivery_version,
        )

    def preview_plan(
        self,
        field_id: str,
        *,
        operation_id: str | None = None,
        source: Literal["manual", "assistant", "demo", "copied_legacy"] | None = None,
    ) -> PlanPreviewV3:
        state = self.get_field(field_id)
        if state.draft.profile is None:
            raise ValueError("先保存地块名称和基本情况，再生成计划")
        request, previous = self._build_request(state)
        calculated = self._evaluate_request(state, request, previous)
        return self._save_plan_preview(state, calculated, operation_id=operation_id, source=source)

    def preview_restore_plan(
        self,
        field_id: str,
        version_id: str,
        *,
        operation_id: str | None = None,
    ) -> PlanPreviewV3:
        """Recheck old unfinished slots against today's draft, facts, and completion locks."""
        state = self.get_field(field_id)
        historic = next(
            (version for version in self.plan_history(field_id) if version.id == version_id), None
        )
        if historic is None:
            raise ValueError("所选历史版本不存在或不属于当前地块")
        request, current = self._build_request(state)
        # Only unfinished historical placements are preferences. Completion facts
        # and their original slots always come from the current event projection.
        current_items = {item.task_id: item for item in current.items} if current else {}
        historic_items = {item.task_id: item for item in historic.items if not item.completed}
        preferred = (current or historic.plan).model_copy(deep=True)
        preferred.request = request.model_copy(deep=True)
        preferred.items = [
            (
                current_items[task_id]
                if task_id in request.completed_at
                else historic_items[task_id]
            ).model_copy(deep=True)
            for task_id in dict.fromkeys([*historic_items, *current_items])
            if (task_id in request.completed_at and task_id in current_items)
            or (task_id not in request.completed_at and task_id in historic_items)
        ]
        calculated = self._evaluate_request(state, request, preferred)
        calculated.reference_version = historic.id
        return self._save_plan_preview(
            state,
            calculated,
            operation_id=operation_id,
            extra_basis=["恢复历史安排仅作为时段偏好；已按当前档案、现场事实和可用时间重新校验"],
        )

    def _save_plan_preview(
        self,
        state: FieldStateV3,
        calculated: PlanVersionV2,
        *,
        operation_id: str | None = None,
        source: Literal["manual", "assistant", "demo", "copied_legacy"] | None = None,
        extra_basis: list[str] | None = None,
    ) -> PlanPreviewV3:
        field_id = state.id
        now = self._now()
        current = self.store.get_current_plan(field_id)
        changes = self._diff(current, calculated)
        weather = self.store.latest_weather(field_id)
        if weather and (
            state.draft.location is None
            or weather.requested_latitude != state.draft.location.latitude
            or weather.requested_longitude != state.draft.location.longitude
            or weather.cache_expires_at <= now
            or not self._weather_covers(weather, calculated.request.start_date)
        ):
            weather = None
        reviews = self.store.list_rule_adoptions(field_id)
        adoptions = [record for record in reviews if record.status == "adopted"]
        calculation_basis = [
            "单地块独立计划；尚未统筹其他地块时间，不代表合并后可执行",
            "天气预报仅用于风险展示；现场与天气作业条件仍需分别确认",
            "候选规则处于开发验证阶段，适用性待复核",
        ]
        calculation_basis.extend(extra_basis or [])
        if reviews:
            calculation_basis.append("已采用规则仅限登记用途；暂缓规则不作为计划依据")
        if any(plot.standing_water is True for plot in calculated.request.plots):
            calculation_basis.append(
                "田里仍有积水记录：积水持续或加重时请先现场核实或联系当地农技人员；"
                "系统不替代现场处置"
            )
        version = PlanVersionV3(
            field_id=field_id,
            parent_id=current.id if current else None,
            created_at=now,
            draft_version=state.draft_version,
            fact_version=state.fact_version,
            task_version=state.task_version,
            plan=calculated,
            weather_snapshot_id=weather.id if weather else None,
            ruleset_version=self.catalog.delivery_version,
            rule_adoption_ids=[record.id for record in adoptions],
            source=source or self._source_for(state),
            changes=changes,
            calculation_basis=calculation_basis,
        )
        preview = PlanPreviewV3(
            field_id=field_id,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            expected_plan_id=current.id if current else None,
            created_at=now,
            version=version,
        )
        return self.store.save_preview(
            preview,
            operation_id=operation_id or f"preview-{uuid4()}",
        )

    def adopt_plan(
        self,
        preview_id: str,
        *,
        expected_fact_version: int,
        expected_plan_id: str | None,
        operation_id: str,
    ) -> PlanAdoptionResultV3:
        replay = self.store.replay_adoption(
            preview_id,
            expected_fact_version=expected_fact_version,
            expected_plan_id=expected_plan_id,
            operation_id=operation_id,
        )
        if replay is not None:
            return replay
        preview = self.store.load_preview(preview_id)
        state = self.get_field(preview.field_id)
        if (
            state.fact_version != preview.expected_fact_version
            or state.draft_version != preview.expected_draft_version
            or state.latest_plan_id != preview.expected_plan_id
            or expected_fact_version != preview.expected_fact_version
            or expected_plan_id != preview.expected_plan_id
        ):
            self.store.mark_preview_stale(preview_id)
            raise ValueError("预览已过期，请重新计算")
        snapshot_id = preview.version.weather_snapshot_id
        if snapshot_id:
            snapshot = self.store.get_weather_snapshot(snapshot_id)
            if snapshot.cache_expires_at <= self._now():
                self.store.mark_preview_stale(preview_id)
                raise ValueError("预览引用的天气快照已过期，请刷新天气并重新预览")
        if self._preview_has_elapsed_work(preview):
            self.store.mark_preview_stale(preview_id)
            raise ValueError("预览的未完成任务时段已过期，请重新计算")
        return self.store.adopt_plan(
            preview_id,
            expected_fact_version=expected_fact_version,
            expected_plan_id=expected_plan_id,
            operation_id=operation_id,
        )

    def cancel_preview(self, preview_id: str) -> PlanPreviewV3:
        return self.store.cancel_preview(preview_id)

    def latest_actionable_preview(self, field_id: str) -> PlanPreviewV3 | None:
        """Read the latest recoverable preview without creating or changing state."""
        preview = self.store.latest_actionable_preview(field_id)
        if preview is None:
            return None
        snapshot_id = preview.version.weather_snapshot_id
        if (
            snapshot_id
            and self.store.get_weather_snapshot(snapshot_id).cache_expires_at <= self._now()
        ):
            return None
        if self._preview_has_elapsed_work(preview):
            return None
        return preview

    def _preview_has_elapsed_work(self, preview: PlanPreviewV3) -> bool:
        now = self._now()
        return any(
            not item.completed and item.start_at is not None and item.start_at < now
            for item in preview.version.items
        )

    def get_current_plan(self, field_id: str) -> PlanVersionV3 | None:
        return self.store.get_current_plan(field_id)

    def plan_history(self, field_id: str) -> list[PlanVersionV3]:
        return self.store.plan_history(field_id)

    def record_rule_adoption(
        self,
        field_id: str,
        rule_id: str,
        *,
        status: Literal["pending_review", "adopted", "rejected"],
        reviewer: str = "",
        notes: str = "",
    ) -> RuleAdoptionRecordV3:
        if rule_id not in self.catalog.cards:
            raise ValueError(f"规则资料中没有 {rule_id}")
        evidence = self.catalog.evidence_for([rule_id])
        record = RuleAdoptionRecordV3(
            field_id=field_id,
            rule_id=rule_id,
            status=status,
            reviewer=reviewer,
            reviewed_at=self._now(),
            notes=notes,
            ruleset_version=self.catalog.delivery_version,
            source_fingerprints=sorted({str(item["source_sha256"]) for item in evidence}),
        )
        self.store.save_rule_adoption(record)
        return record

    def rule_adoptions(self, field_id: str) -> list[RuleAdoptionRecordV3]:
        return self.store.list_rule_adoptions(field_id)

    def list_rule_adoptions(self, field_id: str) -> list[RuleAdoptionRecordV3]:
        return self.rule_adoptions(field_id)

    def copy_legacy_plan(self, version_id: str, *, operation_id: str) -> FieldStateV3:
        from nongshi.repository import PlanStore, PlanStoreV2
        from nongshi.schema import Plot

        legacy_store = PlanStore(self.store.path)
        try:
            old = legacy_store.load(version_id)
            old_request = old.request
        except KeyError:
            modern = PlanStoreV2(self.store.path).load(version_id)
            old_request = modern.request
        if not old_request.plots:
            raise ValueError("旧计划没有可复制的地块输入")
        new_id = f"field-{uuid4().hex[:10]}"
        original_profile = old_request.plots[0]
        if isinstance(original_profile, Plot):
            legacy_tasks = [
                task for task in old_request.tasks if task.plot_id == original_profile.id
            ]
            inspections = [task for task in legacy_tasks if task.kind.startswith("inspect")]
            maintenance = [task for task in legacy_tasks if task.kind == "maintain_ditch"]
            observations = original_profile.observations
            old_profile = FieldProfile(
                id=new_id,
                name=original_profile.name,
                province=original_profile.region or None,
                province_confirmed=bool(original_profile.region),
                crop=None,
                crop_confirmed=False,
                stage=original_profile.stage,
                stage_confirmed=original_profile.stage_confirmed,
                area_mu=original_profile.area_mu,
                inspection_requested=bool(inspections) if inspections else None,
                maintenance_need_confirmed=(bool(maintenance) if maintenance else None),
                ditch_connectivity={
                    "clear": "clear",
                    "blocked": "impaired",
                }.get(observations.get("ditch_condition")),
                inspection_duration_minutes=(
                    round(inspections[0].hours * 60)
                    if inspections and inspections[0].hours
                    else None
                ),
                maintenance_duration_minutes=(
                    round(maintenance[0].hours * 60)
                    if maintenance and maintenance[0].hours
                    else None
                ),
                observation_source="synthetic",
            )
            copied_days: list[DayWindow] = []
        else:
            old_profile = original_profile.model_copy(deep=True)
            old_profile.id = new_id
            copied_days = old_request.days
        draft = FieldDraftV3(
            field_id=new_id,
            mode="real",
            profile=old_profile,
            start_date=old_request.start_date,
            days=copied_days,
            input_source="copied_legacy",
            source_plan_id=version_id,
            notes="从旧计划复制；未填写的信息继续保持未知。",
        )
        return self.save_field_draft(
            draft,
            expected_version=None,
            operation_id=operation_id,
        )

    def export_json(self, field_id: str) -> str:
        state = self.get_field(field_id)
        version = self.store.get_current_plan(field_id)
        events = self.store.list_events(field_id)
        payload = {
            "schema_version": 4,
            "field": state.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
            "task_series_v4": [
                series.model_dump(mode="json") for series in self.store.list_task_series(field_id)
            ],
            "task_instances_v4": [
                task.model_dump(mode="json") for task in self.store.list_task_instances(field_id)
            ],
            "task_execution_groups_v4": [
                group.model_dump(mode="json") for group in self.list_task_execution_groups(field_id)
            ],
            "issues_v4": [
                issue.model_dump(mode="json") for issue in self.store.list_field_issues(field_id)
            ],
            "issue_events_v4": self.store.list_field_issue_events(field_id),
            "adopted_plan": version.model_dump(mode="json") if version else None,
            "history": [item.model_dump(mode="json") for item in self.store.plan_history(field_id)],
            "weather": (
                self.store.latest_weather(field_id).model_dump(mode="json")
                if self.store.latest_weather(field_id)
                else None
            ),
            "rule_adoptions": [
                record.model_dump(mode="json")
                for record in self.store.list_rule_adoptions(field_id)
            ],
            "workflow_runs": [
                run.model_dump(mode="json") for run in self.store.list_agent_runs(field_id)
            ],
            "agent_proposals": [
                record.model_dump(mode="json") for record in self.agent_proposal_records(field_id)
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def export_markdown(self, field_id: str) -> str:
        state = self.get_field(field_id)
        version = self.store.get_current_plan(field_id)
        events = self.store.list_events(field_id)
        issues = self.store.list_field_issues(field_id)
        lines = [
            f"# {state.draft.profile.name if state.draft.profile else '未命名地块'}计划",
            "",
            "- 导出结构：v4（旧计划仍按原格式只读）",
            f"- 计划版本：{version.id if version else '尚未采用'}",
            f"- 现场事实版本：v{state.fact_version}",
            f"- 规则资料：{version.ruleset_version if version else self.catalog.delivery_version}",
            "- 模式：开发验证，适用性待复核",
            "- 排程范围：单地块；尚未统筹其他地块时间，不代表合并后可执行",
            "",
        ]
        if version is None:
            evaluation = self.evaluate_field(field_id)
            lines.extend(["## 候选事项与缺项", ""])
            for item in evaluation.items:
                lines.append(f"- {item.title}：{item.reason}（{item.task_id}）")
            if evaluation.missing_fields:
                lines.extend(["", "缺少信息：" + "、".join(evaluation.missing_fields)])
            if issues:
                lines.extend(["", "## 问题与复查", ""])
                lines.extend(
                    f"- {issue.title}｜{'待处理' if issue.status == 'open' else '已解决'}｜"
                    f"首次记录 {issue.opened_at.astimezone(LOCAL_ZONE):%Y-%m-%d}｜"
                    f"关联任务：{'、'.join(issue.related_task_ids) or '无'}"
                    for issue in issues
                )
            return "\n".join(lines) + "\n"
        lines.extend(["## 七天安排", ""])
        for item in version.items:
            if item.schedule_status == "cancelled":
                when = "已取消"
            elif item.start_at and item.end_at:
                when = f"{item.start_at:%Y-%m-%d %H:%M}–{item.end_at:%H:%M}"
            else:
                when = "未排入"
            if item.execution_group_id:
                when += (
                    f" · 合并出行 {item.execution_group_id[:8]}，"
                    f"共{item.group_duration_minutes}分钟；本项预计{item.duration_minutes}分钟"
                )

            def describe_evidence(evidence: RuleEvidenceV2) -> str:
                pages = "、".join(f"PDF第{page}页" for page in evidence.pdf_pages)
                details = "；".join(
                    part
                    for part in (
                        pages,
                        evidence.source_context_summary,
                        f"用途限制：{evidence.project_use_limit}"
                        if evidence.project_use_limit
                        else "",
                    )
                    if part
                )
                return f"{evidence.rule_id}/{evidence.source_id}（{details}）"

            current = [
                describe_evidence(e) for e in item.evidence if e.usage_role == "limited_support"
            ]
            deferred = [
                describe_evidence(e)
                for e in item.evidence
                if e.usage_role == "historical_reference"
            ]
            evidence_parts = []
            if current:
                evidence_parts.append("依据：" + "、".join(current))
            if deferred:
                evidence_parts.append("暂缓参考：" + "、".join(deferred))
            evidence = "；".join(evidence_parts) or "依据：无直接依据"
            lines.append(f"- {when}｜{item.title}｜{item.reason}｜{evidence}｜任务：{item.task_id}")
        lines.extend(["", "## 缺项和未排入原因", ""])
        for item in version.items:
            if item.missing_fields or item.schedule_status in {"pending", "unscheduled"}:
                details = "、".join(item.missing_fields) or item.reason
                lines.append(f"- {item.task_id}：{details}")
        if not any(
            item.missing_fields or item.schedule_status in {"pending", "unscheduled"}
            for item in version.items
        ):
            lines.append("- 无")
        result_events = [
            event for event in events if event.feedback and event.feedback.kind == "task_result"
        ]
        lines.extend(["", "## 执行结果记录", ""])
        if result_events:
            task_names = {item.task_id: item.title for item in version.items}
            result_labels = {
                "normal": "正常",
                "issue": "发现问题",
                "unable": "无法判断",
                "unknown": "未知",
                "not_checked": "未检查",
            }
            lines.extend(
                f"- {event.feedback.at.astimezone(LOCAL_ZONE):%Y-%m-%d %H:%M}｜"
                f"{task_names.get(event.feedback.task_id or '', event.feedback.task_id or '任务')}"
                "｜"
                + "、".join(
                    f"{name}：{result_labels.get(value, value)}"
                    for name, value in event.feedback.result_values.items()
                )
                + (f"｜{event.feedback.result_notes}" if event.feedback.result_notes else "")
                for event in result_events
            )
        else:
            lines.append("- 暂无执行结果")
        lines.extend(["", "## 问题与复查", ""])
        if issues:
            lines.extend(
                f"- {issue.title}｜{'待处理' if issue.status == 'open' else '已解决'}｜"
                f"首次记录 {issue.opened_at.astimezone(LOCAL_ZONE):%Y-%m-%d}｜"
                f"来源记录 {len(issue.source_event_ids)} 次｜"
                f"关联任务：{'、'.join(issue.related_task_ids) or '无'}"
                + (f"｜再次发生关联 {issue.repeat_of}" if issue.repeat_of else "")
                for issue in issues
            )
        else:
            lines.append("- 暂无问题记录")
        lines.extend(["", "## 版本变化", ""])
        change_labels = {
            "added": "新增",
            "moved": "调整时段",
            "removed": "未排入",
            "cancelled": "已取消",
            "unchanged": "保持不变",
            "completed": "已完成",
            "content_changed": "内容变化",
        }
        lines.extend(
            f"- {change.task_id}：{change_labels[change.kind]}；{change.reason}"
            for change in version.changes
        )
        if not version.changes:
            lines.append("- 首版计划")
        return "\n".join(lines) + "\n"

    def store_weather(self, snapshot: WeatherSnapshotV3) -> None:
        self.store.save_weather_snapshot(snapshot)

    def latest_weather(
        self,
        field_id: str,
        *,
        location=None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> WeatherSnapshotV3 | None:
        snapshot = self.store.latest_weather(field_id)
        if snapshot is None:
            return None
        mismatch = bool(
            location
            and (
                snapshot.requested_latitude != location.latitude
                or snapshot.requested_longitude != location.longitude
            )
        ) or bool(
            start_date
            and end_date
            and (snapshot.coverage_start != start_date or snapshot.coverage_end != end_date)
        )
        return snapshot.model_copy(
            update={"is_stale": snapshot.cache_expires_at <= self._now() or mismatch}
        )

    def _build_request(self, state: FieldStateV3) -> tuple[PlanRequestV2, PlanVersionV2 | None]:
        draft = state.draft
        if draft.profile is None:
            raise ValueError("地块档案尚未填写")
        start_date = draft.start_date or (self._today() + timedelta(days=1))
        days = draft.days or [
            DayWindow(
                day=start_date + timedelta(days=offset),
                site_workable=None,
                weather_workable=None,
                resource_minutes=None,
                intervals=[],
            )
            for offset in range(7)
        ]
        profile = draft.profile.model_copy(deep=True)
        previous_version = self.store.get_current_plan(state.id)
        previous = previous_version.plan.model_copy(deep=True) if previous_version else None
        completed_at = self._completion_facts(state.id, previous)
        if previous:
            # Only the event projection may release an adopted completion lock. V2's
            # normal merge protection still applies to all other callers and edits.
            previous.request.completed_at = dict(completed_at)
        active_events = self._active_events(state.id)
        all_task_instances = self.store.list_task_instances(state.id)
        instances_by_id = {instance.id: instance for instance in all_task_instances}
        window_end = start_date + timedelta(days=6)
        required_instance_ids = {
            instance.id
            for instance in all_task_instances
            if instance.due_date >= start_date and instance.earliest_date <= window_end
        }
        execution_groups = [
            group
            for group in self.list_task_execution_groups(state.id)
            if group.status != "cancelled"
            and any(task_id in required_instance_ids for task_id in group.member_task_ids)
        ]
        for group in execution_groups:
            required_instance_ids.update(group.member_task_ids)
        required_instance_ids.update(
            task_id for task_id in completed_at if task_id in instances_by_id
        )
        required_instance_ids.update(
            event.feedback.task_id
            for event in active_events
            if event.feedback
            and event.feedback.kind in {"cancelled", "task_result"}
            and event.feedback.task_id in instances_by_id
        )
        if previous:
            required_instance_ids.update(
                item.task_id
                for item in previous.items
                if item.task_id in instances_by_id and item.completed
            )
        # Keep unresolved historical prerequisites visible as overdue items so a
        # dependent occurrence cannot become detached when the seven-day window moves.
        pending = list(required_instance_ids)
        while pending:
            task_id = pending.pop()
            instance = instances_by_id.get(task_id)
            if instance is None:
                continue
            for prerequisite in instance.prerequisites:
                if prerequisite in completed_at or prerequisite not in instances_by_id:
                    continue
                if prerequisite not in required_instance_ids:
                    required_instance_ids.add(prerequisite)
                    pending.append(prerequisite)
        task_instances = [
            instances_by_id[task_id]
            for task_id in sorted(
                required_instance_ids,
                key=lambda value: (instances_by_id[value].occurrence_date, value),
            )
        ]
        if previous_version and previous:
            prior_profile = next(
                (plot for plot in previous.request.plots if plot.id == state.id), None
            )
            if prior_profile:
                profile.maintenance_task_id = prior_profile.maintenance_task_id
                profile.completed_maintenance_task_ids = list(
                    task_id
                    for task_id in prior_profile.completed_maintenance_task_ids
                    if task_id in completed_at
                )
                profile.completed_maintenance_task_ids.extend(
                    item.task_id
                    for item in previous.items
                    if item.task_id in completed_at
                    and item.task_type == "maintenance"
                    and item.task_id != (profile.maintenance_task_id or f"{state.id}-M")
                    and item.task_id not in profile.completed_maintenance_task_ids
                )
            completed_maintenance = [
                item
                for item in previous.items
                if item.task_type == "maintenance" and item.task_id in completed_at
            ]
            new_demand = next(
                (
                    event
                    for event in reversed(active_events)
                    if event.fact_version > previous_version.fact_version
                    and event.feedback
                    and event.feedback.kind == "observation"
                    and event.feedback.field == "maintenance_need_confirmed"
                    and event.feedback.value is True
                ),
                None,
            )
            if completed_maintenance and new_demand:
                old_id = completed_maintenance[-1].task_id
                profile.completed_maintenance_task_ids = list(
                    dict.fromkeys([*profile.completed_maintenance_task_ids, old_id])
                )
                profile.maintenance_task_id = f"{state.id}-M-{new_demand.event_id[:8]}"
        # 采用/暂缓记录在评估前注入：rejected 卡不再作为任务依据，
        # adopted 卡仅改标注。同卡多条记录时以最新一条为准。
        rule_review_status = {
            record.rule_id: record.status for record in self.store.list_rule_adoptions(state.id)
        }
        request = PlanRequestV2(
            start_date=start_date,
            plots=[profile],
            days=[day.model_copy(deep=True) for day in days],
            **self.catalog.plan_metadata(),
            selected_rule_ids=sorted(self.catalog.development_rule_ids),
            adoption_profile="development_validation",
            synthetic=draft.mode == "demo" or draft.input_source == "demo",
            completed_at=completed_at,
            rule_review_status=rule_review_status,
            scheduling_preference=draft.scheduling_preference,
            task_instances_v4=task_instances,
            execution_groups_v4=execution_groups,
        )
        initial = build_plan_v2(
            request,
            previous=previous,
            catalog=self.catalog,
            created_at=self._now(),
            not_before=self._now(),
        )
        for event in active_events:
            feedback = event.feedback
            if feedback is None or feedback.kind == "completed":
                continue
            if feedback.kind in {"resource", "workability"} and feedback.day not in {
                day.day for day in request.days
            }:
                continue
            initial = apply_feedback_v2(initial, feedback, catalog=self.catalog)
        return initial.request, previous

    def _evaluate_request(
        self,
        state: FieldStateV3,
        request: PlanRequestV2,
        previous: PlanVersionV2 | None,
    ) -> PlanVersionV2:
        # Rebuild from the facts-applied request and preserve the feedback time floor.
        feedback = next(
            (
                event.feedback
                for event in reversed(self._active_events(state.id))
                if event.feedback is not None
            ),
            None,
        )
        return build_plan_v2(
            request,
            previous=previous,
            feedback=feedback,
            catalog=self.catalog,
            created_at=self._now(),
            not_before=self._now(),
        )

    def _completion_facts(
        self, field_id: str, previous: PlanVersionV2 | None
    ) -> dict[str, datetime]:
        events = self.store.list_events(field_id)
        managed_ids = {
            event.feedback.task_id
            for event in events
            if event.feedback and event.feedback.kind == "completed"
        }
        completions = {
            task_id: at
            for task_id, at in (previous.request.completed_at.items() if previous else [])
            if task_id not in managed_ids
        }
        for event in self._active_events(field_id):
            if event.feedback and event.feedback.kind == "completed" and event.feedback.task_id:
                completions[event.feedback.task_id] = event.feedback.at
        return completions

    def _active_events(self, field_id: str):  # type: ignore[no-untyped-def]
        events = self.store.list_events(field_id)
        superseded = {event.correction_of for event in events if event.correction_of}
        return [
            event
            for event in events
            if event.event_id not in superseded and not (event.retracts and event.feedback is None)
        ]

    @staticmethod
    def _source_for(state: FieldStateV3) -> Literal["manual", "assistant", "demo", "copied_legacy"]:
        if state.mode == "demo" or state.draft.input_source == "demo":
            return "demo"
        if state.draft.input_source == "copied_legacy":
            return "copied_legacy"
        return "manual"

    @staticmethod
    def _diff(previous: PlanVersionV3 | None, current: PlanVersionV2) -> list[PlanChangeV3]:
        old_items = {item.task_id: item for item in previous.items} if previous else {}
        new_items = {item.task_id: item for item in current.items}
        changes: list[PlanChangeV3] = []
        for task_id, item in new_items.items():
            old = old_items.get(task_id)
            if item.schedule_status == "cancelled" and (
                old is None or old.schedule_status != "cancelled"
            ):
                kind = "cancelled"
                reason = item.reason
            elif old is None:
                if item.necessity == "no_need":
                    continue
                kind = "added"
                reason = item.reason
            elif item.completed and not old.completed:
                kind = "completed"
                reason = "用户确认完成，历史事实保持锁定"
            elif (
                item.completed
                and previous
                and (
                    previous.plan.request.completed_at.get(task_id)
                    != current.request.completed_at.get(task_id)
                )
            ):
                kind = "completed"
                reason = "用户显式更正完成记录；保留原事件和原时段"
            elif old is not None and (
                old.title,
                old.duration_minutes,
                old.category,
                old.priority,
                old.earliest_date,
                old.due_date,
                old.prerequisites,
                old.template_snapshot,
            ) != (
                item.title,
                item.duration_minutes,
                item.category,
                item.priority,
                item.earliest_date,
                item.due_date,
                item.prerequisites,
                item.template_snapshot,
            ):
                kind = "content_changed"
                reason = "任务内容或排程条件已更新"
            elif (old.start_at, old.end_at, old.schedule_status) == (
                item.start_at,
                item.end_at,
                item.schedule_status,
            ):
                kind = "unchanged"
                reason = "安排与上一版本相同" if old else item.reason
            elif old.start_at and item.start_at:
                kind = "moved"
                reason = item.reason
            elif old.start_at and not item.start_at:
                kind = "cancelled" if item.schedule_status == "not_generated" else "removed"
                reason = item.reason
            else:
                kind = "added"
                reason = item.reason
            changes.append(
                PlanChangeV3(
                    task_id=task_id,
                    kind=kind,
                    old_start_at=old.start_at if old else None,
                    old_end_at=old.end_at if old else None,
                    new_start_at=item.start_at,
                    new_end_at=item.end_at,
                    reason=reason,
                )
            )
        for task_id, old in old_items.items():
            if task_id not in new_items:
                changes.append(
                    PlanChangeV3(
                        task_id=task_id,
                        kind="removed",
                        old_start_at=old.start_at,
                        old_end_at=old.end_at,
                        reason="当前事实下未再生成该事项",
                    )
                )
        return changes

    @staticmethod
    def _weather_covers(snapshot: WeatherSnapshotV3, start_date: date) -> bool:
        return (
            snapshot.coverage_start <= start_date
            and snapshot.coverage_end >= start_date + timedelta(days=6)
        )

    @staticmethod
    def _questions(missing: list[str]) -> list[str]:
        labels = {
            "province": "地块所在省份",
            "province_confirmation": "地块所在省份是否已确认",
            "crop": "当前种植的作物",
            "crop_confirmation": "作物信息是否已确认",
            "stage": "油菜当前生育期",
            "stage_confirmation": "油菜生育期是否已由现场确认",
            "field_type": "田块类型",
            "inspection_requested": "是否需要巡田",
            "maintenance_need_confirmed": "要安排沟渠维护吗？",
            "maintenance_problem_confirmed": "沟渠有需要处理的堵塞或排水问题吗？",
            "maintenance_purpose": "维护沟渠是为了什么？",
            "duration_minutes": "任务预计需要多少分钟",
            "resource_minutes": "每天可用的作业分钟数",
            "site_workability": "当天现场能否进入作业",
            "weather_workability": "当天天气是否适合作业",
            "ditch_connectivity": "沟渠是否通畅",
            "standing_water": "田里是否有积水",
            "pest_signs": "田里是否发现虫害迹象",
            "missing_seedlings": "田里是否缺苗",
            "crowding": "苗是否拥挤",
        }
        questions: list[str] = []
        for item in missing:
            field = item.rsplit(":", maxsplit=1)[-1]
            label = labels.get(field, labels.get(item))
            if label and label not in questions:
                questions.append(label)
            if len(questions) == 3:
                break
        return questions
