"""Small workflow contract: intent, allowed capabilities, and completion checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Action = Literal[
    "field_update",
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
        "首轮声明用户本次要办的事项和最多三个具体缺项；多事项分别列出，不能补造时间。",
        frozenset(),
        "declare",
    ),
    "read_field": Capability(
        "read_field", "读取当前地块已保存状态、任务和事实。", frozenset(), "read", True
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
        "计算当前候选事项、状态和缺项。",
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
}


def allowed_capabilities(actions: set[Action] | None) -> set[str]:
    if actions is None:
        return set(CAPABILITIES)
    return {"declare_request", "read_field"} | {
        name for name, item in CAPABILITIES.items() if item.workflow_actions & actions
    }
