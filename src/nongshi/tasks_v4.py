"""Versioned task templates, recurring definitions, and stable task instances."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

TaskKindV4 = Literal["inspection", "maintenance", "custom", "follow_up", "management"]
TaskCategoryV4 = Literal["onsite", "offsite"]
RecurrenceV4 = Literal["once", "dates", "interval"]
TaskSourceV4 = Literal["user_defined", "reviewed_template", "development_example"]


class TaskTemplateV4(BaseModel):
    """Immutable execution instructions copied into each task instance."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = Field(default=1, ge=1)
    title: str = Field(min_length=1)
    task_type: TaskKindV4 = "custom"
    category: TaskCategoryV4 = "onsite"
    related_issue_id: str | None = None
    purpose: str = ""
    trigger: str = ""
    necessary_conditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    result_fields: list[str] = Field(default_factory=list)
    completion_standard: str = ""
    follow_up: str = ""
    source_kind: TaskSourceV4 = "user_defined"
    evidence: list[dict[str, str]] = Field(default_factory=list)
    agronomic_review_status: Literal["not_assessed", "development_only", "reviewed"] = (
        "not_assessed"
    )

    @model_validator(mode="after")
    def normalize_text(self) -> TaskTemplateV4:
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("名称不能为空")
        self.steps = [step.strip() for step in self.steps if step.strip()]
        self.necessary_conditions = [
            condition.strip() for condition in self.necessary_conditions if condition.strip()
        ]
        self.result_fields = [field.strip() for field in self.result_fields if field.strip()]
        if self.task_type in {"inspection", "maintenance"} and self.category != "onsite":
            raise ValueError("巡田和沟渠维护必须标记为现场任务")
        if self.task_type == "follow_up" and not self.related_issue_id:
            raise ValueError("问题复查必须关联一个已记录的问题")
        return self


class TaskSeriesV4(BaseModel):
    """A user-confirmed occurrence pattern with a frozen task template."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    field_id: str = Field(min_length=1)
    template: TaskTemplateV4
    recurrence: RecurrenceV4
    anchor_date: date
    end_date: date | None = None
    selected_dates: list[date] = Field(default_factory=list)
    interval_days: int | None = Field(default=None, gt=0)
    duration_minutes: int | None = Field(default=None, gt=0)
    priority: int = Field(default=2, ge=1, le=3)
    earliest_date: date | None = None
    due_date: date | None = None
    prerequisites: list[str] = Field(default_factory=list)
    conditions_confirmed: bool | None = None

    @field_validator("interval_days", mode="before")
    @classmethod
    def interval_must_be_positive(cls, value: object) -> object:
        if value is not None and (not isinstance(value, int) or value <= 0):
            raise ValueError("间隔天数必须为正整数")
        return value

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def duration_must_be_positive(cls, value: object) -> object:
        if value is not None and (not isinstance(value, int) or value <= 0):
            raise ValueError("预计时长必须为正整数分钟")
        return value

    @model_validator(mode="after")
    def validate_recurrence(self) -> TaskSeriesV4:
        if self.end_date is not None and self.end_date < self.anchor_date:
            raise ValueError("结束日期不能早于起始日期")
        if self.earliest_date and self.due_date and self.earliest_date > self.due_date:
            raise ValueError("最早日期不能晚于截止日期")
        if self.recurrence == "once":
            if self.selected_dates or self.interval_days is not None:
                raise ValueError("单次任务不能包含周期日期或间隔")
        elif self.recurrence == "dates":
            if not self.selected_dates:
                raise ValueError("至少选择一个日期")
            if len(self.selected_dates) > 3660:
                raise ValueError("单个周期最多登记3660次；请分段建立周期")
            if self.interval_days is not None:
                raise ValueError("指定日期任务不能同时设置间隔")
            if self.selected_dates != sorted(set(self.selected_dates)):
                raise ValueError("指定日期不能重复且必须按日期排列")
            if any(day < self.anchor_date for day in self.selected_dates):
                raise ValueError("指定日期不能早于起始日期")
            if self.end_date and any(day > self.end_date for day in self.selected_dates):
                raise ValueError("指定日期不能晚于结束日期")
        else:
            if self.interval_days is None:
                raise ValueError("间隔天数不能为空")
            if self.end_date is None:
                raise ValueError("周期任务需要明确结束日期")
            if (self.end_date - self.anchor_date).days // self.interval_days + 1 > 3660:
                raise ValueError("单个周期最多登记3660次；请缩短周期范围")
            if self.selected_dates:
                raise ValueError("间隔周期不能同时设置指定日期")
        return self


class TaskInstanceV4(BaseModel):
    """Stable per-occurrence snapshot; its completion is recorded as a fact event."""

    model_config = ConfigDict(extra="forbid")

    id: str
    series_id: str
    field_id: str
    occurrence_number: int = Field(ge=1)
    occurrence_date: date
    earliest_date: date
    due_date: date
    duration_minutes: int | None = Field(default=None, gt=0)
    priority: int = Field(ge=1, le=3)
    prerequisites: list[str] = Field(default_factory=list)
    template: TaskTemplateV4
    status: Literal["pending", "ready", "superseded"]
    missing_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_window(self) -> TaskInstanceV4:
        if self.earliest_date > self.due_date:
            raise ValueError("最早日期不能晚于截止日期")
        return self


class TaskExecutionGroupV4(BaseModel):
    """User-confirmed same-trip grouping; member results remain independent."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    field_id: str = Field(min_length=1)
    member_task_ids: list[str] = Field(min_length=2)
    execution_date: date
    duration_minutes: int = Field(gt=0)
    version: int = Field(default=1, ge=1)
    member_signature: str = ""
    status: Literal["confirmed", "needs_reconfirmation", "cancelled"] = "confirmed"
    confirmed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_members(self) -> TaskExecutionGroupV4:
        if len(self.member_task_ids) != len(set(self.member_task_ids)):
            raise ValueError("合并出行的任务不能重复")
        return self


class TaskSeriesSaveResultV4(BaseModel):
    field_id: str
    series_id: str
    task_version: int = Field(ge=1)
    created_instance_ids: list[str] = Field(default_factory=list)
    created_at: AwareDatetime


class TaskInstanceUpdateResultV4(BaseModel):
    field_id: str
    task_id: str
    task_version: int = Field(ge=1)
    instance: TaskInstanceV4
    updated_at: AwareDatetime


class TaskSeriesUpdateResultV4(BaseModel):
    field_id: str
    series_id: str
    task_version: int = Field(ge=1)
    updated_instance_ids: list[str] = Field(default_factory=list)
    created_instance_ids: list[str] = Field(default_factory=list)
    superseded_instance_ids: list[str] = Field(default_factory=list)
    updated_at: AwareDatetime


class TaskExecutionGroupSaveResultV4(BaseModel):
    field_id: str
    group_id: str
    group_version: int = Field(ge=1)
    task_version: int = Field(ge=1)
    updated_at: AwareDatetime


class FieldIssueV4(BaseModel):
    """Current projection of a user-confirmed field problem and its follow-up."""

    id: str
    field_id: str
    issue_key: str
    title: str
    description: str = ""
    status: Literal["open", "resolved"] = "open"
    source_task_id: str
    source_event_ids: list[str] = Field(default_factory=list)
    related_task_ids: list[str] = Field(default_factory=list)
    repeat_of: str | None = None
    opened_at: AwareDatetime
    resolved_at: AwareDatetime | None = None


def _occurrence_dates(series: TaskSeriesV4) -> list[tuple[int, date]]:
    if series.recurrence == "once":
        return [(1, series.anchor_date)]
    if series.recurrence == "dates":
        return [(index, day) for index, day in enumerate(series.selected_dates, start=1)]

    assert series.interval_days is not None and series.end_date is not None
    dates: list[tuple[int, date]] = []
    day = series.anchor_date
    index = 1
    while day <= series.end_date:
        dates.append((index, day))
        day += timedelta(days=series.interval_days)
        index += 1
    return dates


def expand_task_series(
    series: TaskSeriesV4,
    window_start: date,
    *,
    days: int = 7,
) -> list[TaskInstanceV4]:
    """Expand only occurrences whose confirmed date window intersects the plan."""
    if days < 1:
        raise ValueError("计划天数必须大于零")
    window_end = window_start + timedelta(days=days - 1)
    result: list[TaskInstanceV4] = []
    for occurrence_number, occurrence_date in _occurrence_dates(series):
        earliest = max(occurrence_date, series.earliest_date or occurrence_date)
        due = max(occurrence_date, series.due_date or occurrence_date)
        if due < window_start or earliest > window_end:
            continue
        missing = []
        if series.duration_minutes is None:
            missing.append("duration_minutes")
        if not series.template.purpose.strip():
            missing.append("purpose")
        if not series.template.completion_standard.strip():
            missing.append("completion_standard")
        if series.template.necessary_conditions and series.conditions_confirmed is not True:
            missing.append("conditions_confirmation")
        result.append(
            TaskInstanceV4(
                id=f"{series.id}:{occurrence_number:06d}",
                series_id=series.id,
                field_id=series.field_id,
                occurrence_number=occurrence_number,
                occurrence_date=occurrence_date,
                earliest_date=earliest,
                due_date=due,
                duration_minutes=series.duration_minutes,
                priority=series.priority,
                prerequisites=list(series.prerequisites),
                template=series.template.model_copy(deep=True),
                status="pending" if missing else "ready",
                missing_fields=missing,
            )
        )
    return result


def expand_task_series_all(series: TaskSeriesV4) -> list[TaskInstanceV4]:
    """Expand a bounded recurrence for persistence and explicit series edits."""
    horizon_end = series.end_date or max(series.selected_dates or [series.anchor_date])
    days = (horizon_end - series.anchor_date).days + 1
    if days < 1 or days > 3660:
        raise ValueError("周期展开范围须在起始日至结束日十年以内")
    return expand_task_series(series, series.anchor_date, days=days)
