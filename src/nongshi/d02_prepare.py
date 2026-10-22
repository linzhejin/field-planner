"""Convert candidate scripts into fixed user interactions for the sealed trial.

The conversion preserves source drafts and fixtures. It changes who performs an
action: natural-language requests go through the Agent, while a user confirmation
can only commit a matching proposal returned by that Agent.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def normalize_candidate(case: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a new case and a traceable operation map without executing it."""
    prepared = deepcopy(case)
    steps: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    old_to_new: dict[int, int] = {}
    preview_bindings: dict[str, str] = {}

    def append(source: dict[str, Any], operation: str, payload: dict[str, Any]) -> int:
        number = len(steps) + 1
        row = {
            "step": number,
            "at": source["at"],
            "operation": operation,
            "user_message": source.get("user_message") or "",
            "payload": payload,
        }
        steps.append(row)
        return number

    for source in case.get("interaction_script") or []:
        old_number = int(source["step"])
        operation = str(source["operation"])
        payload = deepcopy(source.get("payload") or {})
        generated: list[int] = []
        if operation == "save_draft":
            generated.append(append(source, operation, payload))
        elif operation in {"evaluate", "preview", "request", "chat"}:
            if payload.get("adapter_action") == "save_additional_field":
                generated.append(
                    append(
                        source,
                        "save_additional_field",
                        {"draft_ref": payload.get("draft_ref") or "companion_field_draft"},
                    )
                )
            chat_payload: dict[str, Any] = {}
            if operation in {"evaluate", "preview"}:
                binding = f"preview_{old_number}"
                preview_bindings[binding] = binding
                chat_payload["preview_binding"] = binding
            generated.append(append(source, "chat", chat_payload))
        elif operation == "commit_feedback":
            chat = append(source, "chat", {})
            generated.append(chat)
            generated.append(
                append(
                    source,
                    "confirm_feedback",
                    {
                        "proposal_step": chat,
                        "events": payload.get("events") or [],
                    },
                )
            )
            steps[-1]["user_message"] = ""  # confirmation is a UI action
        elif operation == "cancel_preview":
            old_binding = str(payload.get("preview_binding") or "")
            payload["preview_binding"] = preview_bindings.get(old_binding, old_binding)
            generated.append(append(source, operation, payload))
        else:
            generated.append(append(source, operation, payload))
        old_to_new[old_number] = generated[0]
        changes.append(
            {
                "original_step": old_number,
                "original_operation": operation,
                "prepared_steps": generated,
                "prepared_operations": [steps[number - 1]["operation"] for number in generated],
            }
        )

    # A preview reference is an artifact name, not the numeric prepared step.
    for source, row in zip(case.get("interaction_script") or [], changes, strict=True):
        if source["operation"] == "preview":
            new_number = row["prepared_steps"][0]
            binding = f"preview_{source['step']}"
            steps[new_number - 1]["payload"]["preview_binding"] = binding

    fault = prepared.get("adapter_failure_fixture")
    if isinstance(fault, dict):
        if fault.get("operation") in {"evaluate", "preview", "request"}:
            fault["operation"] = "chat"
        if fault.get("inject_at_step") is not None:
            fault["inject_at_step"] = old_to_new[int(fault["inject_at_step"])]
        for attempt in fault.get("attempts") or []:
            if attempt.get("inject_at_step") is not None:
                attempt["inject_at_step"] = old_to_new[int(attempt["inject_at_step"])]
    prepared["interaction_script"] = steps
    prepared["continue_after_failure"] = True
    return prepared, changes
