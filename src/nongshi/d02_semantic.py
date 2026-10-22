"""Blinded AI-assisted source-meaning checks; never override deterministic failures."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nongshi.config import Settings
from nongshi.d02_scoring import _tasks

JUDGE_PROMPT = (
    "你只核对农业建议是否被所引原文、地区、作物、生育期、田型和用途限制支持。"
    "不能根据模型输出补造现场事实；历史参考不能当直接处置依据。"
    "逐条返回 supported、unsupported 或 uncertain；严重无依据处置标 serious。"
    '只输出 JSON：{"judgments":[{"claim_id":"task-0",'
    '"verdict":"supported","severity":"none","reason":"..."}]}。'
)


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    verdict: Literal["supported", "unsupported", "uncertain"]
    severity: Literal["none", "minor", "serious"]
    reason: str = Field(min_length=1, max_length=500)


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    judgments: list[Judgment]


def blind_packet(case: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Exclude group name, runtime, token/cost and tool logs from the judge's view."""
    profile = (case.get("draft") or {}).get("profile") or {}
    fields = (
        "province",
        "crop",
        "stage",
        "field_type",
        "maintenance_need_confirmed",
        "maintenance_problem_confirmed",
        "maintenance_purpose",
        "standing_water",
        "ditch_connectivity",
    )
    tasks = _tasks(record)
    claims = [
        {
            "claim_id": f"task-{index}",
            "task_type": row.get("task_type"),
            "status": row.get("schedule_status"),
            "reason": row.get("reason"),
            "evidence": row.get("evidence_summaries") or [],
        }
        for index, row in enumerate(tasks)
        if row.get("schedule_status") in {"scheduled", "pending", "unscheduled"}
    ]
    return {
        "result_id": record["result_id"],
        "facts": {field: profile.get(field) for field in fields},
        "claims": claims,
    }


def judge_packet(
    packet: dict[str, Any],
    *,
    settings: Settings,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    claims = packet["claims"]
    if not claims:
        return {"status": "not_applicable", "judgments": [], "cost": None, "cost_status": "unknown"}
    key = settings.llm_api_key.get_secret_value().strip()
    if not key:
        return {
            "status": "missing_credential",
            "judgments": [],
            "cost": None,
            "cost_status": "unknown",
        }
    started = time.perf_counter()
    raw_content: str | None = None
    try:
        with httpx.Client(transport=transport, timeout=20.0) as client:
            response = client.post(
                settings.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": settings.llm_model,
                    "temperature": 0,
                    "thinking": {"type": "disabled"},
                    "max_tokens": 4096,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": JUDGE_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"facts": packet["facts"], "claims": claims}, ensure_ascii=False
                            ),
                        },
                    ],
                },
            )
        if response.status_code != 200:
            return {
                "status": "provider_error",
                "http_status": response.status_code,
                "judgments": [],
                "cost": None,
                "cost_status": "unknown",
            }
        body = response.json()
        raw_content = body["choices"][0]["message"]["content"]
        parsed = JudgeResult.model_validate_json(raw_content)
        expected = {claim["claim_id"] for claim in claims}
        actual = [item.claim_id for item in parsed.judgments]
        if set(actual) != expected or len(actual) != len(expected):
            raise ValueError("语义复核未逐条返回全部建议")
        return {
            "status": "reviewed",
            "judgments": [item.model_dump() for item in parsed.judgments],
            "first_result": raw_content,
            "model": body.get("model") or settings.llm_model,
            "token_usage": body.get("usage"),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "cost": None,
            "cost_status": "unknown",
            "reviewer_independence": "same_provider_ai_assistance_not_expert_review",
        }
    except (httpx.RequestError, ValueError, ValidationError, KeyError, IndexError) as error:
        return {
            "status": "review_error",
            "error": f"{type(error).__name__}: {error}",
            "first_result": raw_content,
            "judgments": [],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "cost": None,
            "cost_status": "unknown",
        }


def write_blind_review_packets(
    records: list[dict[str, Any]], cases: dict[str, dict[str, Any]], path: Path
) -> None:
    if path.exists():
        raise FileExistsError("盲评视图已存在")
    with path.open("x", encoding="utf-8") as handle:
        for record in records:
            packet = blind_packet(cases[record["case_id"]], record)
            handle.write(json.dumps(packet, ensure_ascii=False) + "\n")
