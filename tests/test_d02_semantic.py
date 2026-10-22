"""Blind semantic review must not receive group or trace information."""

from __future__ import annotations

import json

import httpx

from nongshi.config import Settings
from nongshi.d02_semantic import blind_packet, judge_packet
from nongshi.rules import load_rule_catalog


def test_blind_packet_excludes_group_cost_and_trace() -> None:
    case = {"draft": {"profile": {"province": "湖北", "stage": "苗期"}}}
    record = {
        "result_id": "anon-1",
        "group_code": "g3",
        "elapsed_ms": 99,
        "model_calls": 2,
        "tool_records": ["secret"],
        "result": {
            "tasks": [
                {
                    "task_id": "one",
                    "task_type": "inspection",
                    "schedule_status": "scheduled",
                    "reason": "巡田",
                    "evidence_summaries": [],
                }
            ]
        },
    }
    packet = blind_packet(case, record)
    encoded = json.dumps(packet, ensure_ascii=False)
    assert "g3" not in encoded and "secret" not in encoded
    assert "model_calls" not in encoded and "elapsed_ms" not in encoded
    assert packet["claims"][0]["claim_id"] == "task-0"


def test_blind_packet_separates_agricultural_claim_from_schedule_reason() -> None:
    case = {"draft": {"profile": {"province": "湖北", "inspection_requested": True}}}
    record = {
        "result_id": "anon-pending",
        "result": {
            "tasks": [
                {
                    "task_type": "inspection",
                    "schedule_status": "unscheduled",
                    "reason": "没有足够连续可用时间",
                    "duration_minutes": 60,
                    "start_at": None,
                    "end_at": None,
                    "evidence_summaries": [],
                }
            ]
        },
    }
    packet = blind_packet(case, record)
    assert packet["facts"]["inspection_requested"] is True
    assert packet["claims"][0]["schedule_reason"] == "没有足够连续可用时间"
    assert packet["claims"][0]["agricultural_claim"] != "没有足够连续可用时间"
    assert packet["claims"][0]["duration_minutes"] == 60


def test_blind_packet_uses_authoritative_source_and_confirmed_facts() -> None:
    case = {
        "draft": {"profile": {"province": "湖北", "maintenance_purpose": None}, "days": []},
        "interaction_script": [
            {
                "operation": "commit_feedback",
                "payload": {
                    "events": [
                        {
                            "feedback": {
                                "kind": "observation",
                                "field": "maintenance_purpose",
                                "value": "clear_existing_ditch",
                            }
                        }
                    ]
                },
            }
        ],
    }
    record = {
        "result_id": "anon-source",
        "result": {
            "tasks": [
                {
                    "task_type": "maintenance",
                    "schedule_status": "scheduled",
                    "evidence_summaries": [
                        {"rule_id": "C23", "source_id": "S999", "quote": "fabricated"}
                    ],
                }
            ]
        },
    }
    packet = blind_packet(case, record)
    assert packet["facts"]["maintenance_purpose"] == "clear_existing_ditch"
    assert packet["sources"] == []
    assert packet["claims"][0]["evidence"][0]["quote"] == "fabricated"


def test_blind_packet_includes_catalog_quote_for_cited_source() -> None:
    original = load_rule_catalog().evidence_for(["C23"])[0]
    record = {
        "result_id": "anon-real-source",
        "result": {
            "tasks": [
                {
                    "task_type": "maintenance",
                    "schedule_status": "scheduled",
                    "evidence_summaries": [
                        {
                            "rule_id": original["rule_id"],
                            "source_id": original["source_id"],
                            "quote": "incorrect summary",
                        }
                    ],
                }
            ]
        },
    }
    packet = blind_packet({"draft": {"profile": {}}}, record)
    assert packet["sources"][0]["quote"] == original["quote"]
    assert packet["sources"][0]["quote"] != packet["claims"][0]["evidence"][0]["quote"]


def test_semantic_judge_requires_all_claim_ids() -> None:
    packet = {
        "result_id": "anon-1",
        "facts": {"province": "湖北"},
        "claims": [{"claim_id": "task-0", "reason": "巡田"}],
    }
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"judgments":[]}'}}]})

    outcome = judge_packet(
        packet,
        settings=Settings(llm_api_key="test-key"),
        transport=httpx.MockTransport(respond),
    )
    assert outcome["status"] == "review_error"
    assert requests[0]["temperature"] == 0
    assert requests[0]["max_tokens"] == 4096


def test_semantic_judge_records_supported_result_without_claiming_expert_review() -> None:
    packet = {
        "result_id": "anon-2",
        "facts": {"province": "湖北"},
        "sources": [{"rule_id": "C11", "source_id": "Z15", "quote": "original"}],
        "claims": [
            {
                "claim_id": "task-0",
                "reason": "巡田",
                "evidence": [{"rule_id": "C11", "source_id": "Z15"}],
            }
        ],
    }
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "judgments": [
                                        {
                                            "claim_id": "task-0",
                                            "verdict": "supported",
                                            "severity": "none",
                                            "reason": "来源与用途相符",
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    outcome = judge_packet(packet, settings=Settings(llm_api_key="test-key"), transport=transport)
    assert outcome["status"] == "reviewed"
    assert outcome["judgments"][0]["verdict"] == "supported"
    assert outcome["reviewer_independence"] == "same_provider_ai_assistance_not_expert_review"


def test_semantic_judge_cannot_mark_unsourced_claim_supported() -> None:
    packet = {
        "result_id": "anon-3",
        "facts": {},
        "sources": [],
        "claims": [{"claim_id": "task-0", "evidence": [{"rule_id": "C11", "source_id": "fake"}]}],
    }
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "judgments": [
                                        {
                                            "claim_id": "task-0",
                                            "verdict": "supported",
                                            "severity": "none",
                                            "reason": "I think so",
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ],
            },
        )
    )
    outcome = judge_packet(packet, settings=Settings(llm_api_key="test-key"), transport=transport)
    assert outcome["status"] == "reviewed"
    assert outcome["judgments"][0]["verdict"] == "uncertain"
