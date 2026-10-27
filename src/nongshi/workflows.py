"""Small workflow contract: intent, allowed capabilities, and completion checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Action = Literal[
    "field_update",
    "task_series",
    "location",
    "completed",
    "observation",
    "workability",
    "resource",
    "preview",
    "query",
]


class DeclareRequestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_id: str
    actions: list[Action] = Field(min_length=1, max_length=8)
    missing: list[str] = Field(default_factory=list, max_length=3)
    field_changes: dict[str, Any] = Field(default_factory=dict)
    field_change_quotes: dict[str, str] = Field(default_factory=dict)
    task_series: dict[str, Any] = Field(default_factory=dict)
    task_series_quote: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    workflow_actions: frozenset[Action]
    effect: Literal["read", "proposal", "preview", "declare"]
    cacheable: bool = False
    failure_class: str = "tool_failed"


CAPABILITIES: dict[str, Capability] = {
    "declare_request": Capability(
        "declare_request",
        "首轮同时声明事项、提取每条事实或档案改动，并列最多三个具体缺项；"
        "events 每项附用户原话 source_quote；field_changes 每个字段在 "
        "field_change_quotes 中附对应原话。不要补造完成时间或人员时段。",
        frozenset(),
        "declare",
    ),
    "read_field": Capability(
        "read_field",
        "读取当前地块已保存状态、任务和事实；只读查询计划时，与 evaluate_field 同轮调用。",
        frozenset(),
        "read",
        True,
    ),
    "search_rules": Capability(
        "search_rules",
        "检索农业原文及用途限制。",
        frozenset({"query", "preview", "field_update"}),
        "read",
        True,
    ),
    "evaluate_field": Capability(
        "evaluate_field",
        "计算当前候选事项、状态和缺项；只读查询计划时，与 read_field 同轮调用。",
        frozenset({"query", "preview", "field_update"}),
        "read",
        True,
    ),
    "query_weather": Capability(
        "query_weather", "兼容指定完整七天的天气查询。", frozenset({"query", "preview"}), "read"
    ),
    "query_current_weather": Capability(
        "query_current_weather",
        "查询已保存起始日起七天天气。",
        frozenset({"query", "preview"}),
        "read",
    ),
    "search_locations": Capability(
        "search_locations", "搜索地名候选，用户须确认。", frozenset({"location", "query"}), "read"
    ),
    "propose_location": Capability(
        "propose_location", "提出待确认查询位置。", frozenset({"location"}), "proposal"
    ),
    "preview_plan": Capability(
        "preview_plan", "只根据已保存事实创建预览。", frozenset({"preview"}), "preview"
    ),
    "propose_field_update": Capability(
        "propose_field_update", "提出待确认地块档案修改。", frozenset({"field_update"}), "proposal"
    ),
    "propose_feedback": Capability(
        "propose_feedback",
        "提出待确认完成、观察、作业条件或资源修改。",
        frozenset({"completed", "observation", "workability", "resource"}),
        "proposal",
    ),
    "propose_task_series": Capability(
        "propose_task_series",
        "根据用户明确描述提出单次或周期农事待办；不得补造目的、工时、步骤或农艺处置，"
        "不完整信息保留为空并等待用户确认。",
        frozenset({"task_series"}),
        "proposal",
    ),
}


def allowed_capabilities(actions: set[Action] | None) -> set[str]:
    if actions is None:
        return set(CAPABILITIES)
    return {"declare_request", "read_field"} | {
        name for name, item in CAPABILITIES.items() if item.workflow_actions & actions
    }
