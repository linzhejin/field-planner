"""D02 unified runner: offline C execution and pending A/B records."""

from __future__ import annotations

import tempfile
from pathlib import Path

from nongshi.d02_runner import DEFAULT_CASES, build_run


def test_offline_run_executes_group_c_and_marks_a_b_pending() -> None:
    with tempfile.TemporaryDirectory() as directory:
        report = build_run(DEFAULT_CASES, run_directory=Path(directory))

    records = report["records"]
    assert report["summary"]["cases"] == 3
    assert all(row["status"] == "executed" for row in records if row["group_code"] == "g3")
    assert [row["status"] for row in records if row["group_code"] in {"g1", "g2"}] == [
        "pending_model_authorization"
    ] * 6
    j03 = next(row for row in records if row["case_id"] == "J03" and row["group_code"] == "g3")
    assert j03["result"]["committed_feedback"] == 4
    assert j03["result"]["events_after"] == 4
