"""Real direct-generation (A) and retrieval-augmented (B) evaluation arms."""

from __future__ import annotations

import json
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nongshi.config import Settings
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
    reason: str = ""
    evidence_rule_ids: list[str] = Field(default_factory=list)


class ModelPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[ModelTask]
    missing_fields: list[str] = Field(default_factory=list)
    explanation: str = ""


def _case_input(case: dict[str, Any]) -> dict[str, Any]:
    """Allowlist user-facing inputs; never include reference answers or scoring metadata."""
    raw_steps = case.get("interaction_script") or []
    return {
        "received_at": case.get("received_at"),
        "draft": case.get("draft"),
        "weather_fixture": case.get("weather_fixture"),
        "feedback_events": case.get("feedback_events", []),
        "messages": case.get("messages", []),
        "interaction_script": [
            {
                "at": step.get("at"),
                "operation": step.get("operation"),
                "user_message": step.get("user_message"),
                "payload": step.get("payload"),
            }
            for step in raw_steps
        ],
    }


def _retrieval(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = FieldProfile.model_validate((case.get("draft") or {}).get("profile") or {})
    query = RuleSearchQueryV3(
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
    result = RuleRetrieverV3().search(query)
    payload = {
        "query": query.model_dump(mode="json"),
        "status": result.status,
        "reason": result.reason,
        "hits": [
            {
                "rule_id": hit.rule_id,
                "applicability": hit.applicability,
                "reason": hit.reason,
                "evidence": [
                    {
                        "rule_id": row.rule_id,
                        "source_id": row.source_id,
                        "usage_role": row.usage_role,
                        "source_context_summary": row.source_context_summary,
                        "project_use_limit": row.project_use_limit,
                    }
                    for row in hit.evidence
                ],
            }
            for hit in result.hits
        ],
    }
    return payload, {"name": "search_rules", "status": result.status, "query": payload["query"]}


def run_model_group(
    case: dict[str, Any],
    group: Literal["A", "B"],
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
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
        input_data = _case_input(case)
        if group == "B":
            evidence, record = _retrieval(case)
            input_data["rule_retrieval"] = evidence
            row["tool_records"].append(record)
        prompt = (
            "请只根据输入生成农事候选及七天计划，输出 JSON 对象。"
            "不得补造现场确认、天气或农业规则；未知保持待确认。"
            "任务只能是 inspection/maintenance，开始结束时间用带时区 ISO 格式。"
            '输出结构：{"tasks":[{"task_type":"inspection",'
            '"task_id":null,"schedule_status":"pending",'
            '"start_at":null,"end_at":null,"reason":"",'
            '"evidence_rule_ids":[]}],"missing_fields":[],"explanation":""}。'
        )
        request = {
            "model": config.llm_model,
            "temperature": 0,
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
        with httpx.Client(transport=transport, timeout=config.llm_timeout_seconds) as client:
            row["model_calls"] = 1
            response = client.post(
                config.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=request,
            )
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
        json.JSONDecodeError,
        ValidationError,
        KeyError,
        IndexError,
    ) as error:
        row["errors"].append(f"{type(error).__name__}: {error}")
    finally:
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return row
