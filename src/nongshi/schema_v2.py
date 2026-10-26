"""Version 2 inputs and snapshots for confirmed, minute-level planning."""

from __future__ import annotations

from datetime import date, time
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, Field, model_validator

Applicability = Literal["matched", "out_of_scope", "pending"]
Necessity = Literal["required", "no_need", "unknown"]
DailyWindowStatus = Literal["available", "unavailable", "pending"]
WeatherSource = Literal["synthetic", "user_estimate", "official"]
ScheduleStatus = Literal[
    "scheduled", "unscheduled", "pending", "not_generated", "completed", "cancelled"
]
TaskType = Literal["inspection", "maintenance"]
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


class TimeWindow(BaseModel):
    start: time
    end: time

    @model_validator(mode="after")
    def validate_window(self) -> TimeWindow:
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("每日可用时段请使用本地钟面时间，不含时区")
        if self.start.second or self.start.microsecond or self.end.second or self.end.microsecond:
            raise ValueError("可用时段必须精确到分钟")
        if self.end <= self.start:
            raise ValueError("可用时段结束时间必须晚于开始时间")
        return self

    @property
    def minutes(self) -> int:
        return self.end.hour * 60 + self.end.minute - self.start.hour * 60 - self.start.minute


class DayWindow(BaseModel):
    day: date
    site_workable: bool | None
    weather_workable: bool | None
    weather_source: WeatherSource = "synthetic"
    weather_snapshot_at: AwareDatetime | None = None
    resource_minutes: int | None = Field(default=None, ge=0)
    intervals: list[TimeWindow] = Field(default_factory=list)
    confirmation_basis: str = ""

    @model_validator(mode="after")
    def merge_and_validate_intervals(self) -> DayWindow:
        ordered = sorted(self.intervals, key=lambda interval: interval.start)
        merged: list[TimeWindow] = []
        for interval in ordered:
            if not merged:
                merged.append(interval)
                continue
            previous = merged[-1]
            if interval.start < previous.end:
                raise ValueError("每日可用时段不能重叠")
            if interval.start == previous.end:
                merged[-1] = TimeWindow(start=previous.start, end=interval.end)
            else:
                merged.append(interval)
        self.intervals = merged
        return self

    @property
    def available_minutes(self) -> int | None:
        if self.resource_minutes is None:
            return None
        return min(self.resource_minutes, sum(interval.minutes for interval in self.intervals))


class WorkabilityConfirmation(BaseModel):
    site: bool | None = None
    weather: bool | None = None
    weather_source: WeatherSource = "synthetic"
    weather_snapshot_at: AwareDatetime | None = None


class FieldProfile(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    province: str | None = None
    province_confirmed: bool = False
    crop: str | None = None
    crop_confirmed: bool = False
    stage: str | None = None
    stage_confirmed: bool = False
    field_type: Literal["lowland_rice_stubble", "dry_hilly", "other"] | None = None
    # 田型相关事实拆分记录；仅作档案，不替代 field_type 的规则门控
    rice_stubble: bool | None = None
    cold_waterlogged: bool | None = None
    terrain: Literal["lowland", "hilly", "flat", "other"] | None = None
    area_mu: float | None = Field(default=None, gt=0)
    inspection_requested: bool | None = None
    maintenance_need_confirmed: bool | None = None
    maintenance_problem_confirmed: bool | None = None
    maintenance_purpose: Literal["clear_existing_ditch", "other"] | None = None
    moisture: Literal["dry", "normal", "wet"] | None = None
    ditch_connectivity: Literal["clear", "impaired"] | None = None
    standing_water: bool | None = None
    leaf_count: float | None = Field(default=None, ge=0)
    # 苗情观察事实；只作记录，不发明任何农业反应逻辑
    pest_signs: bool | None = None
    missing_seedlings: bool | None = None
    crowding: bool | None = None
    actual_rain_observed: bool | None = None
    winter_before_context_confirmed: bool | None = None
    workability_by_date: dict[date, WorkabilityConfirmation] = Field(default_factory=dict)
    inspection_duration_minutes: int | None = Field(default=None, gt=0)
    maintenance_duration_minutes: int | None = Field(default=None, gt=0)
    inspection_priority: int = Field(default=2, ge=1, le=3)
    maintenance_priority: int = Field(default=2, ge=1, le=3)
    inspection_earliest_date: date | None = None
    inspection_due_date: date | None = None
    maintenance_earliest_date: date | None = None
    maintenance_due_date: date | None = None
    maintenance_prerequisites: list[str] = Field(default_factory=list)
    maintenance_task_id: str | None = None
    completed_maintenance_task_ids: list[str] = Field(default_factory=list)
    observed_at: AwareDatetime | None = None
    observation_source: Literal["observed", "user_estimate", "synthetic", "official"] = "synthetic"

    @model_validator(mode="after")
    def validate_date_and_observation(self) -> FieldProfile:
        if (
            self.inspection_earliest_date
            and self.inspection_due_date
            and self.inspection_earliest_date > self.inspection_due_date
        ):
            raise ValueError("巡田任务最早日期晚于目标日期")
        if (
            self.maintenance_earliest_date
            and self.maintenance_due_date
            and self.maintenance_earliest_date > self.maintenance_due_date
        ):
            raise ValueError("维护任务最早日期晚于目标日期")
        return self


class PlanRequestV2(BaseModel):
    schema_version: Literal[2] = 2
    id: str = Field(default_factory=lambda: str(uuid4()))
    start_date: date
    plots: list[FieldProfile]
    days: list[DayWindow]
    ruleset_version: str
    legacy_rule_version: str = ""
    legacy_mapping_type: str = ""
    engineering_rules_version: str = ""
    engineering_rule_ids: list[str] = Field(
        default_factory=lambda: [f"D{i:02}" for i in range(1, 13)]
    )
    adoption_profile: Literal["development_validation"] = "development_validation"
    selected_rule_ids: list[str] = Field(
        default_factory=lambda: ["C11", "C18", "C19", "C20", "C23", "C24", "C31"]
    )
    synthetic: bool = True
    completed_at: dict[str, AwareDatetime] = Field(default_factory=dict)
    cancelled_tasks: dict[str, str] = Field(default_factory=dict)
    # 地块级规则复核状态（rule_id -> pending_review/adopted/rejected）；
    # 缺省为空表示全部待复核，保持现状。由服务层在评估前注入。
    rule_review_status: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_plan_input(self) -> PlanRequestV2:
        expected_days = [date.fromordinal(self.start_date.toordinal() + i) for i in range(7)]
        if [entry.day for entry in self.days] != expected_days:
            raise ValueError("每日现场与天气确认、资源时段必须覆盖连续七天")
        if not self.plots or len({plot.id for plot in self.plots}) != len(self.plots):
            raise ValueError("地块不能为空且编号不能重复")
        if len(set(self.selected_rule_ids)) != len(self.selected_rule_ids):
            raise ValueError("规则编号不能重复")
        if set(self.engineering_rule_ids) != {f"D{i:02}" for i in range(1, 13)}:
            raise ValueError("工程约定必须独立保留D01—D12编号")
        task_ids: set[str] = set()
        for plot in self.plots:
            task_ids.add(f"{plot.id}-I")
            task_ids.add(plot.maintenance_task_id or f"{plot.id}-M")
            task_ids.update(plot.completed_maintenance_task_ids)
        if len(task_ids) != sum(
            2 + len(plot.completed_maintenance_task_ids) for plot in self.plots
        ):
            raise ValueError("当前和已完成任务编号不能重复")
        if any(
            task_id not in task_ids
            for plot in self.plots
            for task_id in plot.maintenance_prerequisites
        ):
            raise ValueError("维护任务引用了不存在的前置任务")
        if any(task_id not in task_ids for task_id in self.completed_at):
            raise ValueError("完成记录引用了不存在的任务")
        dependencies = {
            task_id: prerequisites
            for plot in self.plots
            for task_id, prerequisites in (
                (f"{plot.id}-I", []),
                (f"{plot.id}-M", plot.maintenance_prerequisites),
            )
        }
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("任务前置关系形成循环")
            if task_id in visited:
                return
            visiting.add(task_id)
            for parent_id in dependencies.get(task_id, []):
                visit(parent_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in dependencies:
            visit(task_id)
        return self


class RuleEvidenceV2(BaseModel):
    rule_id: str
    title: str
    pack_version: str
    evidence_version: str
    source_id: str
    source_title: str
    publisher: str
    source_date: str
    source_url: str
    pdf_pages: list[int]
    quote: str
    applicability_text: str
    condition_text: str
    limits: str
    source_sha256: str
    runtime_enabled: bool = False
    status_label: str = "开发验证，适用性待复核"
    usage_role: Literal["limited_support", "historical_reference", "development_candidate"] = (
        "development_candidate"
    )
    source_context_summary: str = ""
    project_use_limit: str = ""

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_usage_role(cls, value: Any) -> Any:
        """Read older stored snapshots without rewriting their evidence rows."""
        if isinstance(value, dict) and "usage_role" not in value:
            result = dict(value)
            label = str(result.get("status_label") or "")
            if "暂缓" in label:
                result["usage_role"] = "historical_reference"
            elif "限用途" in label:
                result["usage_role"] = "limited_support"
            return result
        return value


class PlanItemV2(BaseModel):
    task_id: str
    plot_id: str
    title: str
    task_type: TaskType
    applicability: Applicability
    necessity: Necessity
    window_by_date: dict[date, DailyWindowStatus] = Field(default_factory=dict)
    schedule_status: ScheduleStatus
    duration_minutes: int | None
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    completed: bool = False
    reason: str
    reason_code: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    evidence: list[RuleEvidenceV2] = Field(default_factory=list)


class FeedbackV2(BaseModel):
    kind: Literal["completed", "observation", "workability", "resource", "cancelled"]
    at: AwareDatetime
    task_id: str | None = None
    plot_id: str | None = None
    field: (
        Literal[
            "maintenance_need_confirmed",
            "maintenance_problem_confirmed",
            "maintenance_purpose",
            "moisture",
            "ditch_connectivity",
            "standing_water",
            "leaf_count",
            "actual_rain_observed",
            "winter_before_context_confirmed",
            "pest_signs",
            "missing_seedlings",
            "crowding",
        ]
        | None
    ) = None
    value: str | bool | float | None = None
    day: date | None = None
    site_workable: bool | None = None
    weather_workable: bool | None = None
    intervals: list[TimeWindow] | None = None
    resource_minutes: int | None = Field(default=None, ge=0)
    reason: str = ""

    @model_validator(mode="after")
    def validate_feedback_details(self) -> FeedbackV2:
        if self.kind == "completed" and not self.task_id:
            raise ValueError("完成反馈需要任务编号")
        if self.kind == "cancelled":
            if not self.task_id:
                raise ValueError("取消任务需要任务编号")
            self.reason = self.reason.strip()
            if not self.reason:
                raise ValueError("取消任务必须填写原因")
        if self.kind == "observation" and not (self.plot_id and self.field):
            raise ValueError("现场反馈需要地块和观察项")
        if self.kind == "workability" and not (self.plot_id and self.day):
            raise ValueError("作业条件反馈需要地块和日期")
        if self.kind == "resource" and (self.day is None or self.intervals is None):
            raise ValueError("人员反馈需要日期和可用时段")
        return self


class PlanVersionV2(BaseModel):
    schema_version: Literal[2] = 2
    id: str = Field(default_factory=lambda: str(uuid4()))
    parent_id: str | None = None
    created_at: AwareDatetime
    reference_version: str | None = None
    not_before: AwareDatetime | None = None
    request: PlanRequestV2
    items: list[PlanItemV2]
    changes: list[str] = Field(default_factory=list)
    feedback: FeedbackV2 | None = None
