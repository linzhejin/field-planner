from nongshi.cases import run_guardrail_cases


def test_supported_boundary_cases_execute_and_knowledge_cases_stay_unclaimed() -> None:
    rows = run_guardrail_cases()
    by_id = {row["id"]: row for row in rows}
    executed_ids = {
        "V01",
        "V02",
        "V03",
        "V05",
        "V06",
        "V07",
        "V11",
        "V12",
        "A01",
        "A03",
        "A06",
        "A07",
    }
    reference_only_ids = {"V04", "V08", "V09", "V10", "A02", "A04", "A05", "A08"}

    assert len(rows) == 20
    assert {row_id for row_id, row in by_id.items() if row["status"] == "executed"} == executed_ids
    assert {
        row_id for row_id, row in by_id.items() if row["status"] == "reference_only_not_executed"
    } == reference_only_ids
    assert not any(row["status"] == "failed" for row in rows)
