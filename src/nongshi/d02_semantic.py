"""Blinded AI-assisted source-meaning checks; never override deterministic failures."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nongshi.config import Settings
from nongshi.d02_scoring import _effective_inputs, _tasks
from nongshi.model_http import ModelBudgetExceeded, post_model_json
from nongshi.rules import load_rule_catalog

JUDGE_PROMPT = (
    "你只核对 agricultural_claim 对农业来源的引用含义与限定用途是否准确。"
    "schedule_reason、起止时间、工时和可作业日由独立程序核对，不要求农业原文证明它们。"
    "pending 或 unscheduled 的任务只是条件性候选；缺少作业条件不能单独判农业引用不支持。"
    "若候选明确保留适用条件未知，核对来源是否支持该观察主题或处置用途；"
    "若擅自宣布适用、可作业或给出超出原文的处置，判 unsupported。"
    "sources 是资料库原文，claims.evidence 是被测系统的主张，不能把后者当作原始证据。"
    "没有匹配的原始出处时不得判 supported；判断引用含义所需事实确实缺失时判 uncertain。"
    "user_confirmed_facts 是用户脚本确认内容；saved_facts 是实际保存内容，不可混淆。"
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
    profile, days = _effective_inputs(case)
    fields = (
        "province",
        "province_confirmed",
        "crop",
        "crop_confirmed",
        "stage",
        "stage_confirmed",
        "field_type",
        "inspection_requested",
        "maintenance_need_confirmed",
        "maintenance_problem_confirmed",
        "maintenance_purpose",
        "standing_water",
        "ditch_connectivity",
    )
    tasks = _tasks(record)
    result = record.get("result") or {}
    saved_profile = dict((case.get("draft") or {}).get("profile") or {})
    saved_days = {str(day["day"]): dict(day) for day in (case.get("draft") or {}).get("days") or []}
    for event in result.get("events") or []:
        feedback = event.get("feedback") or {}
        kind = feedback.get("kind")
        if kind == "observation" and feedback.get("field"):
            saved_profile[feedback["field"]] = feedback.get("value")
        elif kind in {"resource", "workability"}:
            day = saved_days.get(str(feedback.get("day")))
            if day is not None:
                fields_to_update = (
                    ("resource_minutes", "intervals")
                    if kind == "resource"
                    else ("site_workable", "weather_workable")
                )
                day.update({name: feedback[name] for name in fields_to_update if name in feedback})
    catalog = load_rule_catalog()
    source_index = {
        (row["rule_id"], row["source_id"]): row
        for rule_id in catalog.cards
        for row in catalog.evidence_for([rule_id])
    }
    cited_keys = {
        (str(item.get("rule_id")), str(item.get("source_id")))
        for task in tasks
        for item in task.get("evidence_summaries") or []
    }
    sources = []
    for key in sorted(cited_keys):
        original = source_index.get(key)
        if original is None:
            continue
        adoption = catalog.rule_adoptions.get(key[0])
        sources.append(
            {
                "rule_id": key[0],
                "source_id": key[1],
                "quote": original["quote"],
                "pdf_pages": original["pdf_pages"],
                "applicability": original["applicability_text"],
                "condition": original["condition_text"],
                "limits": original["limits"],
                "adoption_decision": adoption.decision if adoption else None,
                "adoption_scope": adoption.scope if adoption else None,
                "usage_role": original.get("usage_role"),
                "project_use_limit": original.get("project_use_limit"),
            }
        )
    claims = [
        {
            "claim_id": f"task-{index}",
            "task_type": row.get("task_type"),
            "status": row.get("schedule_status"),
            "agricultural_claim": "巡田检查的观察主题与适用限制"
            if row.get("task_type") == "inspection"
            else "清理既有沟渠的处置用途与适用限制",
            "schedule_reason": row.get("reason"),
            "applicability": row.get("applicability"),
            "necessity": row.get("necessity"),
            "missing_fields": row.get("missing_fields") or [],
            "start_at": row.get("start_at"),
            "end_at": row.get("end_at"),
            "duration_minutes": row.get("duration_minutes"),
            "evidence": row.get("evidence_summaries") or [],
        }
        for index, row in enumerate(tasks)
        if row.get("schedule_status") in {"scheduled", "pending", "unscheduled"}
    ]
    return {
        "result_id": record["result_id"],
        "facts": {field: profile.get(field) for field in fields},
        "daily_facts": list(days.values()),
        "start_date": (case.get("draft") or {}).get("start_date"),
        "user_confirmed_facts": {field: profile.get(field) for field in fields},
        "saved_facts": {field: saved_profile.get(field) for field in fields},
        "saved_daily_facts": list(saved_days.values()),
        "sources": sources,
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
        response = post_model_json(
            settings.llm_base_url.rstrip("/") + "/chat/completions",
            api_key=key,
            payload={
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
                            {
                                "facts": packet["facts"],
                                "daily_facts": packet.get("daily_facts") or [],
                                "start_date": packet.get("start_date"),
                                "user_confirmed_facts": packet.get("user_confirmed_facts") or {},
                                "saved_facts": packet.get("saved_facts") or {},
                                "saved_daily_facts": packet.get("saved_daily_facts") or [],
                                "sources": packet.get("sources") or [],
                                "claims": claims,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            },
            deadline=started + 20.0,
            transport=transport,
        ).response
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
        available = {
            (str(source.get("rule_id")), str(source.get("source_id")))
            for source in packet.get("sources") or []
        }
        claims_by_id = {claim["claim_id"]: claim for claim in claims}
        judgments = []
        for item in parsed.judgments:
            judged = item.model_dump()
            claim = claims_by_id[item.claim_id]
            has_original = any(
                (str(citation.get("rule_id")), str(citation.get("source_id"))) in available
                for citation in claim.get("evidence") or []
            )
            if not has_original and judged["verdict"] == "supported":
                judged.update(
                    verdict="uncertain",
                    severity="none",
                    reason="未找到与本条建议引用匹配的资料库原文，不能判有依据",
                )
            judgments.append(judged)
        return {
            "status": "reviewed",
            "judgments": judgments,
            "first_result": raw_content,
            "model": body.get("model") or settings.llm_model,
            "token_usage": body.get("usage"),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "cost": None,
            "cost_status": "unknown",
            "reviewer_independence": "same_provider_ai_assistance_not_expert_review",
        }
    except (
        httpx.RequestError,
        ModelBudgetExceeded,
        ValueError,
        ValidationError,
        KeyError,
        IndexError,
    ) as error:
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
