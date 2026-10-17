"""Validated inputs and versioned outputs for the local planning workflow."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

TaskKind = Literal["inspect_seedling", "inspect_ditch", "maintain_ditch", "other"]
WindowStatus = Literal["ready", "blocked", "pending"]
OBSERVATION_VALUES = {
    "ditch_condition": ({"unknown", "clear", "blocked"}, "沟渠情况"),
    "field_access": ({"unknown", "yes", "no"}, "田间可进入性"),
}


class Plot(BaseModel):
    id: str
    name: str
    region: str
    location_label: str | None = Field(default=None, max_length=120)
    stage: str | None = None
    stage_confirmed: bool = False
    area_mu: float | None = Field(default=None, gt=0)
    observations: dict[str, str] = Field(default_factory=dict)
    observation_times: dict[str, AwareDatetime] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_observations(self) -> Plot:
        for field, (allowed, label) in OBSERVATION_VALUES.items():
            if field in self.observations and self.observations[field] not in allowed:
                raise ValueError(f"{label}不是允许的结构化选项")
        for field in self.observation_times:
            if self.observations.get(field) in (None, "unknown"):
                raise ValueError("未知现场情况不能保留观察时间")
        return self


class Task(BaseModel):
    id: str
    plot_id: str
    kind: TaskKind
    title: str
    hours: float | None = Field(default=None, gt=0)
    priority: int = Field(default=2, ge=1, le=3)
    earliest_day: date | None = None
    due_day: date | None = None
    prerequisites: list[str] = Field(default_factory=list)
    completed_at: datetime | None = None


class DayCapacity(BaseModel):
    day: date
    hours: float = Field(ge=0)


class RuleCard(BaseModel):
    id: str
    version: str = Field(default="1", min_length=1)
    kind: TaskKind
    region: str
    stage: str
    conditions: dict[str, str] = Field(default_factory=dict)
    inapplicable_when: dict[str, str] = Field(default_factory=dict)
    confirmed_observation_fields: list[str] = Field(default_factory=list)
    required_prior_task_kinds: list[TaskKind] = Field(default_factory=list)
    source_title: str
    source_excerpt: str
    source_url: str | None = None
    source_date: date | None = None
    source_location: str | None = None
    weather_required: bool = False
    reviewed: bool = False
    is_demo: bool = False

    @model_validator(mode="after")
    def check_reviewed_source(self) -> RuleCard:
        if any(field not in self.conditions for field in self.confirmed_observation_fields):
            raise ValueError("需现场确认的字段必须列在适用条件中")
        if self.reviewed and self.is_demo:
            raise ValueError("演示规则不能标记为已核对")
        if self.reviewed and self.source_date is None:
            raise ValueError("已核对规则需要发布日期")
        if self.reviewed and not self.source_location:
            raise ValueError("已核对规则需要原文位置")
        if self.reviewed and not self.source_excerpt.strip():
            raise ValueError("已核对规则需要原文片段")
        return self


class WeatherSnapshot(BaseModel):
    status: Literal["fixed_demo", "cached", "unavailable"]
    source_label: str
    issued_at: datetime | None = None
    summary: str = ""


class PlanRequest(BaseModel):
    start_date: date
    demo_mode: bool = False
    plots: list[Plot]
    tasks: list[Task]
    days: list[DayCapacity]
    rules: list[RuleCard]
    weather: WeatherSnapshot

    @model_validator(mode="after")
    def check_references_and_days(self) -> PlanRequest:
        if len(self.days) != 7 or [entry.day for entry in self.days] != [
            date.fromordinal(self.start_date.toordinal() + offset) for offset in range(7)
        ]:
            raise ValueError("每天可用工时必须覆盖从开始日期起连续七天")
        plot_ids = [plot.id for plot in self.plots]
        task_ids = [task.id for task in self.tasks]
        rule_ids = [rule.id for rule in self.rules]
        if len(set(plot_ids)) != len(plot_ids) or len(set(task_ids)) != len(task_ids):
            raise ValueError("地块和任务编号不能重复")
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("规则编号不能重复")
        dependencies = {task.id: task.prerequisites for task in self.tasks}
        for task in self.tasks:
            if task.plot_id not in plot_ids:
                raise ValueError(f"任务 {task.id} 引用了不存在的地块")
            if any(parent not in dependencies for parent in task.prerequisites):
                raise ValueError(f"任务 {task.id} 引用了不存在的前置任务")
            if task.earliest_day and task.due_day and task.earliest_day > task.due_day:
                raise ValueError(f"任务 {task.id} 的最早日期晚于期限")
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("任务前置关系形成循环")
            if task_id in visited:
                return
            visiting.add(task_id)
            for parent_id in dependencies[task_id]:
                visit(parent_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self


class Evidence(BaseModel):
    rule_id: str
    rule_version: str = "1"
    source_title: str
    source_excerpt: str
    source_url: str | None = None
    source_date: date | None = None
    source_location: str | None = None
    region: str
    stage: str
    conditions: dict[str, str] = Field(default_factory=dict)
    inapplicable_when: dict[str, str] = Field(default_factory=dict)
    confirmed_observation_fields: list[str] = Field(default_factory=list)
    required_prior_task_kinds: list[TaskKind] = Field(default_factory=list)
    simulation: bool = False


class PlanItem(BaseModel):
    task_id: str
    plot_id: str
    title: str
    hours: float | None
    window_status: WindowStatus
    scheduled_date: date | None = None
    sequence: int | None = None
    completed: bool = False
    reason: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class Feedback(BaseModel):
    kind: Literal["completed", "observation", "availability"]
    at: AwareDatetime
    task_id: str | None = None
    plot_id: str | None = None
    field: str | None = None
    value: str | None = None
    day: date | None = None
    hours: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_details(self) -> Feedback:
        if self.kind == "completed" and not self.task_id:
            raise ValueError("完成反馈需要任务编号")
        if self.kind == "observation" and not (self.plot_id and self.field and self.value):
            raise ValueError("现场观察需要地块、观察项和观察结果")
        if self.kind == "observation":
            if self.field not in OBSERVATION_VALUES:
                raise ValueError("不支持的现场观察项")
            allowed, label = OBSERVATION_VALUES[self.field]
            if self.value not in allowed:
                raise ValueError(f"{label}不是允许的结构化选项")
        if self.kind == "availability" and (self.day is None or self.hours is None):
            raise ValueError("人力变化需要日期和可用工时")
        return self


class PlanVersion(BaseModel):
    schema_version: Literal[1] = 1
    id: str
    parent_id: str | None = None
    created_at: datetime
    not_before: date | None = None
    request: PlanRequest
    items: list[PlanItem]
    changes: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    feedback: Feedback | None = None
