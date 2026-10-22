"""D02 unified runner: distinguish executed, partial, invalid and disabled arms."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx

from nongshi.config import Settings
from nongshi.d02_runner import DEFAULT_CASES, build_run, write_report


def test_offline_run_keeps_partial_feedback_and_marks_a_b_unrun() -> None:
    with tempfile.TemporaryDirectory() as directory:
        report = build_run(DEFAULT_CASES, run_directory=Path(directory))

    records = report["records"]
    assert report["summary"]["cases"] == 3
    assert [row["status"] for row in records if row["group_code"] == "g3"] == [
        "executed",
        "executed",
        "partial",
    ]
    assert [row["status"] for row in records if row["group_code"] in {"g1", "g2"}] == [
        "not_run_model_disabled"
    ] * 6
    j03 = next(row for row in records if row["case_id"] == "J03" and row["group_code"] == "g3")
    assert j03["result"]["committed_feedback"] == 3
    assert j03["result"]["events_after"] == 3
    assert j03["notes"]


def test_model_arms_execute_and_blind_mapping_is_separate() -> None:
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"tasks":[],"missing_fields":[]}'}}]},
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        report = build_run(
            DEFAULT_CASES,
            run_directory=root,
            enable_model=True,
            settings=Settings(llm_api_key="test-key"),
            model_transport=httpx.MockTransport(reply),
            blind=True,
        )
        output = root / "blind.json"
        write_report(report, output)
        public = json.loads(output.read_text(encoding="utf-8"))
    assert len(requests) == 6
    assert all(
        row["status"] == "executed" for row in report["records"] if row["status"] != "partial"
    )
    assert all("group_code" not in row for row in public["records"])
    assert "_private_group_map" not in public
    assert len(report["_private_group_map"]) == 9


def test_invalid_input_is_not_run_or_marked_executed() -> None:
    raw = json.loads(DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0])
    raw["case_id"] = "invalid-dates"
    raw["draft"]["days"] = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "case.jsonl"
        source.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
        report = build_run(source, run_directory=root / "db")
    assert report["summary"]["input_invalid"] == 3
    assert all(
        row["status"] == "input_invalid" and not row["scorable"] for row in report["records"]
    )
