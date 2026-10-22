"""Blind semantic review must not receive group or trace information."""

from __future__ import annotations

import json

import httpx

from nongshi.config import Settings
from nongshi.d02_semantic import blind_packet, judge_packet


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
        "claims": [{"claim_id": "task-0", "reason": "巡田"}],
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
