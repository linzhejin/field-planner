"""Pure, conservative grounding of actual completion time in user supplied text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nongshi.schema_v2 import LOCAL_ZONE


@dataclass(frozen=True)
class CompletionTimeGrounding:
    accepted: bool
    actual_at: datetime | None
    reason: str


def ground_completed_time(
    proposed_at: datetime, received_at: datetime, source_text: str
) -> CompletionTimeGrounding:
    """Validate a proposed instant against an actual user fragment.

    Caller must verify provenance and task identity. This parser never trusts model
    narration, schedule ends or save timestamps. Unsupported wording asks for time.
    Bare numeric clocks refer to receipt's local date; explicit dates override it.
    """

    def reject(reason: str) -> CompletionTimeGrounding:
        return CompletionTimeGrounding(False, None, reason)

    if any(
        value.tzinfo is None or value.utcoffset() is None for value in (proposed_at, received_at)
    ):
        return reject("timezone_required")
    if proposed_at > received_at:
        return reject("future_actual_completion_time")
    received = received_at.astimezone(LOCAL_ZONE)
    candidates: list[datetime] = []
    # Clause boundaries prevent an independent resource clock grounding completion.
    for clause in re.findall(r"[^，,。；;！？!?\n]+[！？?]?", source_text):
        if not re.search(r"完成|做完|结束", clause):
            continue
        if re.search(
            r"计划|预计|将要|打算|准备|没|未|不|吗|么|是否|可能|如果|假如|也许|大概|大约|估计|差不多|左右|或许|应该|似乎|好像|吧|[？?]",
            clause,
        ):
            continue
        if re.search(r"资源|可用|空闲|有空", clause):
            continue
        if re.search(r"(?:刚刚|现在)\s*(?:已|已经)?\s*(?:完成|做完|结束)", clause):
            candidates.append(received_at)
            continue
        iso = re.search(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})", clause
        )
        if iso:
            try:
                candidates.append(datetime.fromisoformat(iso.group().replace("Z", "+00:00")))
            except ValueError:
                pass
            continue
        if "秒" in clause:
            continue
        day = received.date()
        explicit = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?", clause)
        try:
            if explicit:
                day = day.replace(
                    year=int(explicit[1]), month=int(explicit[2]), day=int(explicit[3])
                )
            elif "昨天" in clause:
                day -= timedelta(days=1)
            elif re.search(r"前天|明天|后天", clause):
                continue
            if len(re.findall(r"\d{1,2}(?::\d{2}|点)", clause)) != 1:
                continue
            clock = re.search(
                r"(?<![\d:])(?:(上午|下午|晚上|中午|凌晨)\s*)?(\d{1,2})"
                r"(?::(\d{2})|点(?:(\d{1,2})分?)?)(?![\d:.])",
                clause,
            )
            if re.search(
                r"点[半一二三四五六七八九十零两]|点\d+(?:分)?[半一二三四五六七八九十零两]", clause
            ):
                continue
            if not clock:
                continue
            hour = int(clock[2])
            minute = int(clock[3] or clock[4] or 0)
            if clock[1]:
                if not 1 <= hour <= 12:
                    continue
                if clock[1] in {"下午", "晚上", "中午"} and hour < 12:
                    hour += 12
                elif clock[1] in {"上午", "凌晨"} and hour == 12:
                    hour = 0
            candidates.append(
                datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_ZONE)
            )
        except ValueError:
            continue
    if not candidates:
        return reject("missing_actual_completion_time")
    if len(set(candidates)) != 1:
        return reject("ambiguous_actual_completion_time")
    if candidates[0] != proposed_at:
        return reject("actual_completion_time_mismatch")
    return CompletionTimeGrounding(True, proposed_at, "explicit_user_completion_time")


def audit_completed_time_visibility(case: dict[str, Any]) -> list[str]:
    """Audit formal case inputs only; never inject hidden confirmations into runtime."""
    problems: list[str] = []
    steps = case.get("interaction_script") or []
    receipt = datetime.fromisoformat(str(case["received_at"]))
    visible: dict[int, list[tuple[str, datetime]]] = {}
    history: list[tuple[str, datetime]] = []
    for step in steps:
        if step.get("at"):
            receipt = datetime.fromisoformat(str(step["at"]))
        if step.get("operation") == "chat":
            history.append((str(step.get("user_message") or ""), receipt))
            visible[int(step["step"])] = list(history)
        if step.get("operation") != "confirm_feedback":
            continue
        payload = step.get("payload") or {}
        sources = visible.get(int(payload.get("proposal_step", -1)), [])
        for index, event in enumerate(payload.get("events") or []):
            feedback = event.get("feedback") or {}
            if feedback.get("kind") != "completed":
                continue
            try:
                proposed = datetime.fromisoformat(str(feedback["at"]))
                task_id = str(feedback.get("task_id") or "")
                declared = (case.get("stage_execution_policy") or {}).get("task_ids") or {}
                kinds = {kind for kind, identifier in declared.items() if identifier == task_id}
                if task_id in {"inspection", "maintenance"}:
                    kinds.add(task_id)
                aliases = {
                    "inspection": r"巡查|巡田|巡视|检查",
                    "maintenance": r"维护|清沟|清理沟|疏通沟|沟渠清理",
                }
                kind = next(iter(kinds)) if len(kinds) == 1 else None
                grounded = False
                for text, when in sources:
                    for clause in re.findall(r"[^，,。；;！？!?\n]+[！？?]?", text):
                        exact_id = bool(task_id) and bool(
                            re.search(rf"(?<![\w-]){re.escape(task_id)}(?![\w-])", clause)
                        )
                        present_kinds = {
                            name for name, pattern in aliases.items() if re.search(pattern, clause)
                        }
                        same_kind = kind is not None and present_kinds == {kind}
                        if (exact_id or same_kind) and ground_completed_time(
                            proposed, when, clause
                        ).accepted:
                            grounded = True
            except (ValueError, KeyError, TypeError):
                grounded = False
            if not grounded:
                problems.append(
                    f"{case.get('case_id')} step {step['step']} event {index}: "
                    "missing visible actual completion time"
                )
    return problems
