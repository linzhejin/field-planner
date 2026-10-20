"""Shared business service used by the form, chat tools, exports, and history."""

from __future__ import annotations

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
    FieldProfile,
    PlanRequestV2,
    PlanVersionV2,
)
from nongshi.schema_v3 import (
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
    WeatherSnapshotV3,
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
        self._today = today or (lambda: datetime.now(LOCAL_ZONE).date())
        self._now = now or (lambda: datetime.now(UTC))

    def save_field_draft(
        self,
        draft: FieldDraftV3,
        *,
        expected_version: int | None,
        operation_id: str,
    ) -> FieldStateV3:
        return self.store.save_field_draft(
            draft,
            expected_version=expected_version,
            operation_id=operation_id,
        )

    def get_field(self, field_id: str) -> FieldStateV3:
        return self.store.get_field(field_id)

    def list_fields(self, *, mode: Literal["real", "demo"] | None = None) -> list[FieldStateV3]:
        return self.store.list_fields(mode=mode)

    def list_events(self, field_id: str):  # type: ignore[no-untyped-def]
        return self.store.list_events(field_id)

    def commit_events(
        self,
        field_id: str,
        events: list[FactEventInputV3],
        *,
        operation_id: str,
        expected_fact_version: int,
    ) -> EventCommitResultV3:
        existing_ids = {event.event_id for event in self.store.list_events(field_id)}
        replayed_event_ids = all(event.event_id in existing_ids for event in events)
        state = self.get_field(field_id)
        if not replayed_event_ids and state.fact_version != expected_fact_version:
            raise ValueError(
                f"版本冲突：现场事实为 v{state.fact_version}，提交依据为 v{expected_fact_version}"
            )
        if not replayed_event_ids:
            current = self.store.get_current_plan(field_id)
            current_items = list(current.items) if current else []
            recorded_completions = {
                event.feedback.task_id
                for event in self.store.list_events(field_id)
                if event.feedback and event.feedback.kind == "completed"
            }
            for event in events:
                feedback = event.feedback
                if not feedback or feedback.kind != "completed":
                    continue
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
                if item is None or item.completed or item.start_at is None:
                    raise ValueError("只能记录当前已安排且尚未完成的任务")
                if feedback.at.astimezone(LOCAL_ZONE) < item.start_at.astimezone(LOCAL_ZONE):
                    raise ValueError("完成时间早于任务计划开始时间")
        return self.store.commit_events(
            field_id,
            events,
            expected_fact_version=expected_fact_version,
            operation_id=operation_id,
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
            "单地块独立计划；尚未统筹其他地块时间",
            "天气预报仅用于风险展示；现场与天气作业条件仍需分别确认",
            "候选规则处于开发验证阶段，适用性待复核",
        ]
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
        return self.store.adopt_plan(
            preview_id,
            expected_fact_version=expected_fact_version,
            expected_plan_id=expected_plan_id,
            operation_id=operation_id,
        )

    def cancel_preview(self, preview_id: str) -> PlanPreviewV3:
        return self.store.cancel_preview(preview_id)

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
            "schema_version": 3,
            "field": state.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
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
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def export_markdown(self, field_id: str) -> str:
        state = self.get_field(field_id)
        version = self.store.get_current_plan(field_id)
        lines = [
            f"# {state.draft.profile.name if state.draft.profile else '未命名地块'}计划",
            "",
            "- 数据结构：v3",
            f"- 计划版本：{version.id if version else '尚未采用'}",
            f"- 现场事实版本：v{state.fact_version}",
            f"- 规则资料：{version.ruleset_version if version else self.catalog.delivery_version}",
            "- 模式：开发验证，适用性待复核",
            "- 排程范围：单地块；尚未统筹其他地块时间",
            "",
        ]
        if version is None:
            evaluation = self.evaluate_field(field_id)
            lines.extend(["## 候选事项与缺项", ""])
            for item in evaluation.items:
                lines.append(f"- {item.title}：{item.reason}（{item.task_id}）")
            if evaluation.missing_fields:
                lines.extend(["", "缺少信息：" + "、".join(evaluation.missing_fields)])
            return "\n".join(lines) + "\n"
        lines.extend(["## 七天安排", ""])
        for item in version.items:
            if item.start_at and item.end_at:
                when = f"{item.start_at:%Y-%m-%d %H:%M}–{item.end_at:%H:%M}"
            else:
                when = "未排入"
            evidence = "、".join(dict.fromkeys(e.rule_id for e in item.evidence)) or "无直接依据"
            lines.append(
                f"- {when}｜{item.title}｜{item.reason}｜依据：{evidence}｜任务：{item.task_id}"
            )
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
        lines.extend(["", "## 版本变化", ""])
        lines.extend(
            f"- {change.task_id}：{change.kind}；{change.reason}" for change in version.changes
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
        previous = previous_version.plan if previous_version else None
        active_events = self._active_events(state.id)
        if previous_version and previous:
            prior_profile = next(
                (plot for plot in previous.request.plots if plot.id == state.id), None
            )
            if prior_profile:
                profile.maintenance_task_id = prior_profile.maintenance_task_id
                profile.completed_maintenance_task_ids = list(
                    prior_profile.completed_maintenance_task_ids
                )
                profile.completed_maintenance_task_ids.extend(
                    item.task_id
                    for item in previous.items
                    if item.completed
                    and item.task_type == "maintenance"
                    and item.task_id not in profile.completed_maintenance_task_ids
                )
            completed_maintenance = [
                item
                for item in previous.items
                if item.completed and item.task_type == "maintenance"
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
        completed_at = dict(previous.request.completed_at) if previous else {}
        for event in active_events:
            feedback = event.feedback
            if feedback and feedback.kind == "completed" and feedback.task_id:
                if (
                    feedback.task_id in completed_at
                    and completed_at[feedback.task_id] != feedback.at
                ):
                    raise ValueError("已完成记录不可更改；请提交明确的更正事件")
                completed_at[feedback.task_id] = feedback.at
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
        )
        initial = build_plan_v2(
            request,
            previous=previous,
            catalog=self.catalog,
            created_at=self._now(),
        )
        for event in active_events:
            feedback = event.feedback
            if feedback is None or feedback.kind == "completed":
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
        )

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
            if old is None:
                if item.necessity == "no_need":
                    continue
                kind = "added"
                reason = item.reason
            elif item.completed and not old.completed:
                kind = "completed"
                reason = "用户确认完成，历史事实保持锁定"
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
            "maintenance_need_confirmed": "是否确认需要沟渠维护",
            "maintenance_problem_confirmed": "是否确认存在需要处理的问题",
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
