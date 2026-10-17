from __future__ import annotations

from nongshi.cases import build_case_audit, replay_reference_cases


def test_reference_versions_are_recomputed_and_match_required_differences() -> None:
    versions = replay_reference_cases()
    v1, v2, v3 = versions["V1"], versions["V2"], versions["V3"]
    first = {item.task_id: item for item in v1.items}
    second = {item.task_id: item for item in v2.items}
    third = {item.task_id: item for item in v3.items}

    assert v1.request.synthetic and v2.request.synthetic and v3.request.synthetic
    assert second["A-I"].completed is True
    assert second["C-I"].completed is True
    assert second["B-I"].start_at == first["B-I"].start_at
    assert second["B-I"].end_at == first["B-I"].end_at
    assert second["A-M"].start_at.date().isoformat() == "2026-10-21"
    assert third["A-M"].start_at == second["A-M"].start_at
    assert third["B-M"].necessity == "required"
    assert (
        third["B-M"].window_by_date[
            next(day for day in third["B-M"].window_by_date if day.isoformat() == "2026-10-20")
        ]
        == "unavailable"
    )
    assert third["B-M"].schedule_status in {"pending", "unscheduled"}


def test_case_audit_registers_fourteen_development_scenarios_and_limits_claims() -> None:
    report = build_case_audit()
    development = report["development_cases"]
    assert len(development) == 14
    assert all(row["status"] == "executed" for row in development)
    assert any(row["id"] == "T12-weather-failure" for row in development)
    assert len(report["boundary_cases"]) == 12
    assert len(report["supplemental_cases"]) == 8
    assert sum(row["status"] == "executed" for row in report["boundary_cases"]) == 8
    assert sum(row["status"] == "executed" for row in report["supplemental_cases"]) == 4
    assert not any(row["status"] == "failed" for row in report["boundary_cases"])
    assert not any(row["status"] == "failed" for row in report["supplemental_cases"])
