"""Validated boundary between the HTML workbench and Python services."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

UIActionKind = Literal[
    "select_field",
    "new_field",
    "switch_mode",
    "seed_demo",
    "save_profile",
    "save_profile_draft",
    "save_schedule",
    "save_task_series",
    "save_follow_up",
    "update_task_series",
    "cancel_task",
    "save_execution_group",
    "cancel_execution_group",
    "preview_plan",
    "adopt_plan",
    "cancel_preview",
    "confirm_proposal",
    "ignore_proposal",
    "retry_preview",
    "record_result",
    "record_observation",
    "record_resource",
    "send_message",
    "poll_agent",
    "search_location",
    "select_location",
    "fetch_weather",
    "export_plan",
]


class UIAction(BaseModel):
    """A single client request; the referenced entity and revisions are checked server-side."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(default_factory=lambda: str(uuid4()), min_length=8, max_length=80)
    kind: UIActionKind
    field_id: str = Field(min_length=1, max_length=120)
    expected_draft_version: int | None = Field(default=None, ge=0)
    expected_fact_version: int | None = Field(default=None, ge=0)
    expected_task_version: int | None = Field(default=None, ge=0)
    expected_plan_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class UIActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    success: bool
    message: str
    refresh: bool = True
    outcome: Literal[
        "saved",
        "draft_saved",
        "preview_ready",
        "adopted",
        "no_change",
        "agent_running",
        "agent_ready",
        "error",
    ] = "saved"
