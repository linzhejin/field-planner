"""Real direct-generation (A) and retrieval-augmented (B) evaluation arms."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nongshi.config import Settings
from nongshi.model_http import ModelBudgetExceeded, post_model_json
from nongshi.rule_retrieval import RuleRetrieverV3
from nongshi.schema_v2 import FieldProfile
from nongshi.schema_v3 import RuleSearchQueryV3


class ModelTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: Literal["inspection", "maintenance"]
    task_id: str | None = None
    schedule_status: Literal["scheduled", "pending", "unscheduled", "not_generated", "completed"]
    start_at: str | None = None
    end_at: str | None = None
    duration_minutes: int | None = None
    completed: bool = False
    reason: str = ""
    evidence_rule_ids: list[str] = Field(default_factory=list)
    evidence_summaries: list[dict[str, Any]] = Field(default_factory=list)


class ModelPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[ModelTask]
    missing_fields: list[str] = Field(default_factory=list)
    explanation: str = ""


def _visible_profile(case: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    profile = deepcopy((case.get("draft") or {}).get("profile") or {})
    for step in steps:
        if step.get("operation") in {"commit_feedback", "confirm_feedback"}:
            for event in (step.get("payload") or {}).get("events") or []:
                feedback = event.get("feedback") or {}
                if feedback.get("kind") == "observation" and feedback.get("field"):
                    profile[feedback["field"]] = feedback.get("value")
        elif step.get("operation") == "request":
            demand = (step.get("payload") or {}).get("new_task_request") or {}
            for field in (
                "maintenance_need_confirmed",
                "maintenance_problem_confirmed",
                "maintenance_purpose",
            ):
                if field in demand:
                    profile[field] = demand[field]
    return profile


def _visible_days(case: dict[str, Any], steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days = deepcopy((case.get("draft") or {}).get("days") or [])
    index = {str(day.get("day")): day for day in days}
    for step in steps:
        if step.get("operation") not in {"commit_feedback", "confirm_feedback"}:
            continue
        for event in (step.get("payload") or {}).get("events") or []:
            feedback = event.get("feedback") or {}
            day = index.get(str(feedback.get("day")))
            if day is None:
                continue
            fields = {
                "resource": ("resource_minutes", "intervals"),
                "workability": ("site_workable", "weather_workable"),
            }.get(feedback.get("kind"), ())
            for field in fields:
                if field in feedback:
                    day[field] = feedback[field]
    return days


def _visible_weather(case: dict[str, Any], visible_at: str) -> dict[str, Any] | None:
    fixture = deepcopy(case.get("weather_fixture"))
    if not isinstance(fixture, dict):
        return None
    attempts = fixture.pop("attempts", None)
    if not attempts:
        return fixture
    now = datetime.fromisoformat(visible_at)
    available = [
        attempt
        for attempt in attempts
        if attempt.get("at") and datetime.fromisoformat(attempt["at"]) <= now
    ]
    if not available:
        return {"status": "not_yet_queried"}
    latest = available[-1]
    return {**fixture, **{key: value for key, value in latest.items() if key != "attempt"}}


def _case_input(case: dict[str, Any], *, through_step: int | None = None) -> dict[str, Any]:
    """Allowlist user-facing inputs; never include reference answers or scoring metadata."""
    raw_steps = case.get("interaction_script") or []
    if raw_steps:
        cutoff = through_step if through_step is not None else int(raw_steps[0]["step"])
        visible_steps = [step for step in raw_steps if int(step["step"]) <= cutoff]
        visible_at = str(visible_steps[-1].get("at") or case.get("received_at"))
    else:
        visible_steps = []
        visible_at = str(case.get("received_at") or "")
    draft = deepcopy(case.get("draft"))
    if isinstance(draft, dict) and isinstance(draft.get("profile"), dict):
        draft["profile"] = _visible_profile(case, visible_steps)
        draft["days"] = _visible_days(case, visible_steps)
    return {
        "received_at": case.get("received_at"),
        "draft": draft,
        "weather_fixture": _visible_weather(case, visible_at)
        if not raw_steps or any(step.get("operation") == "save_draft" for step in visible_steps)
        else None,
        "messages": [
            message
            for message in case.get("messages", [])
            if (
                (
                    not raw_steps
                    and str(message.get("at") or case.get("received_at") or "") <= visible_at
                )
                or (raw_steps and message.get("at") and str(message["at"]) <= visible_at)
                or (
                    raw_steps
                    and not message.get("at")
                    and message.get("sequence") is not None
                    and int(message["sequence"]) <= cutoff
                )
            )
        ],
        "interaction_script": [
            {
                "at": step.get("at"),
                "operation": step.get("operation"),
                "user_message": step.get("user_message"),
                "payload": step.get("payload") if step.get("operation") != "chat" else None,
            }
            for step in visible_steps
        ],
    }


def shared_rule_query(
    case: dict[str, Any], *, through_step: int | None = None
) -> RuleSearchQueryV3:
    visible = _case_input(case, through_step=through_step)
    profile = FieldProfile.model_validate((visible.get("draft") or {}).get("profile") or {})
    return RuleSearchQueryV3(
        province=profile.province,
        province_confirmed=profile.province_confirmed,
        crop=profile.crop,
        crop_confirmed=profile.crop_confirmed,
        stage=profile.stage,
        stage_confirmed=profile.stage_confirmed,
        field_type=profile.field_type,
        task_type="all",
        problem_confirmed=profile.maintenance_problem_confirmed,
        maintenance_requested=profile.maintenance_need_confirmed,
        maintenance_purpose=profile.maintenance_purpose,
        winter_before_context_confirmed=profile.winter_before_context_confirmed,
        actual_rain_observed=profile.actual_rain_observed,
    )


def _retrieval(
    case: dict[str, Any], *, through_step: int | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    query = shared_rule_query(case, through_step=through_step)
    result = RuleRetrieverV3().search(query)
    payload = {
        "query": query.model_dump(mode="json"),
        **result.model_dump(mode="json"),
    }
    return payload, {"name": "search_rules", "status": result.status, "query": payload["query"]}


def run_model_group(
    case: dict[str, Any],
    group: Literal["A", "B"],
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    through_step: int | None = None,
    previous_turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute one first attempt; preserve raw output, metrics and failures for later scoring."""
    if group not in {"A", "B"}:
        raise ValueError("评测组只能是 A 或 B")
    config = settings or Settings()
    started = time.perf_counter()
    row: dict[str, Any] = {
        "status": "failed",
        "scorable": False,
        "model": config.llm_model,
        "model_calls": 0,
        "weather_http_calls": 0,
        "token_usage": {"prompt": None, "completion": None},
        "cost": None,
        "cost_status": "unknown",
        "tool_records": [],
        "first_result": None,
        "result": None,
        "errors": [],
    }
    try:
        api_key = config.llm_api_key.get_secret_value().strip()
        if not api_key:
            row["status"] = "missing_credential"
            row["errors"].append("未配置模型密钥；A/B 组未执行")
            return row
        input_data = _case_input(case, through_step=through_step)
        input_data["previous_model_turns"] = previous_turns or []
        if group == "B":
            evidence, record = _retrieval(case, through_step=through_step)
            input_data["rule_retrieval"] = evidence
            row["tool_records"].append(record)
        prompt = (
            "请只根据输入生成农事候选及七天计划，输出 JSON 对象。"
            "不得补造现场确认、天气或农业规则；未知保持待确认。"
            "任务只能是 inspection/maintenance，开始结束时间用带时区 ISO 格式。"
            '输出结构：{"tasks":[{"task_type":"inspection",'
            '"task_id":null,"schedule_status":"pending",'
            '"start_at":null,"end_at":null,"reason":"",'
            '"duration_minutes":null,"completed":false,"evidence_rule_ids":[],"evidence_summaries":[]}]'
            ',"missing_fields":[],"explanation":""}。引用须含规则、来源、页码、原文和用途。'
        )
        request = {
            "model": config.llm_model,
            "temperature": 0,
            "max_tokens": 4096,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(input_data, ensure_ascii=False, default=str),
                },
            ],
        }
        row["model_calls"] = 1
        response = post_model_json(
            config.llm_base_url.rstrip("/") + "/chat/completions",
            api_key=api_key,
            payload=request,
            deadline=started + config.llm_timeout_seconds,
            transport=transport,
        ).response
        if response.status_code != 200:
            row["errors"].append(f"模型 HTTP {response.status_code}")
            return row
        body = response.json()
        usage = body.get("usage") or {}
        row["token_usage"] = {
            "prompt": usage.get("prompt_tokens"),
            "completion": usage.get("completion_tokens"),
        }
        content = body["choices"][0]["message"]["content"]
        row["first_result"] = content
        parsed = ModelPlan.model_validate_json(content)
        row["result"] = parsed.model_dump(mode="json")
        row["scorable"] = True
        row["status"] = "executed"
    except (
        httpx.RequestError,
        ModelBudgetExceeded,
        json.JSONDecodeError,
        ValidationError,
        KeyError,
        IndexError,
    ) as error:
        row["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return row
