"""Seal integrity and first-attempt protection using public development inputs."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from nongshi.config import Settings
from nongshi.d02_correction import prepare_correction
from nongshi.d02_runner import DEFAULT_CASES
from nongshi.d02_sealed import (
    FILES,
    _purpose_confirmed_in_case,
    _validate_formal_constraints,
    prepare_review,
    run_seal,
    score_seal,
    seal,
    verify_seal,
)


def _sources(root: Path) -> dict[str, Path]:
    case = json.loads(DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0])
    contract = {"case_id": case["case_id"]}
    values = {
        "main_cases": [case],
        "main_constraints": [contract],
        "boundary_cases": [],
        "boundary_constraints": [],
    }
    paths = {name: root / f"source_{filename}" for name, filename in FILES.items()}
    for name, path in paths.items():
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in values[name]),
            encoding="utf-8",
        )
    return paths


def test_development_seal_detects_changed_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nongshi.d02_sealed._git", lambda *args: "" if args[0] == "status" else "abc123"
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory = root / "sealed"
        manifest = seal(_sources(root), directory, development=True)
        assert manifest["classification"] == "public_development_only"
        assert manifest["version"] == "internal-auto-v6"
        assert "source_tree" in manifest["materials"]
        assert verify_seal(directory)["code_commit"] == "abc123"
        (directory / FILES["main_cases"]).write_text("tampered\n", encoding="utf-8")
        with pytest.raises(ValueError, match="封存文件已改变"):
            verify_seal(directory)


def test_correction_preparation_keeps_original_score_immutable(monkeypatch):
    import hashlib

    monkeypatch.setattr(
        "nongshi.d02_sealed._git", lambda *args: "" if args[0] == "status" else "new-code"
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        original = root / "original"
        seal(_sources(root), original, development=True)
        score = original / "first_scores.json"
        score.write_text('{"original":true}\n', encoding="utf-8")
        attempts = original / "first_attempts.json"
        case_id = json.loads((original / FILES["main_cases"]).read_text(encoding="utf-8"))[
            "case_id"
        ]
        attempts.write_text(
            json.dumps({"records": [{"result_id": "anon", "case_id": case_id}]}),
            encoding="utf-8",
        )
        mapping = original / "private_group_map.json"
        mapping.write_text(json.dumps({"anon": {"group": "A"}}), encoding="utf-8")
        (original / "run_integrity.json").write_text(
            json.dumps(
                {
                    "first_attempts_sha256": hashlib.sha256(attempts.read_bytes()).hexdigest(),
                    "private_group_map_sha256": hashlib.sha256(mapping.read_bytes()).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        before = score.read_bytes()
        corrected = root / "correction"
        prepare_correction(original, corrected)
        assert score.read_bytes() == before
        assert (corrected / "semantic_blind.jsonl").exists()
        assert (corrected / "correction_manifest.json").exists()


def test_formal_constraints_require_all_citations_and_real_tool_assertion() -> None:
    contract = {
        "case_id": "formal-1",
        "basis": ["产品设计范围", "C31原文"],
        "required_tasks": [{"task_type": "inspection", "allowed_statuses": ["scheduled"]}],
        "forbidden_task_types": [],
        "required_missing_fields": [],
        "citation_required_task_types": ["inspection", "maintenance"],
        "semantic_required": True,
        "c_only_assertions": [
            {"operation": "chat", "status": "ok", "required_tool": "search_rules"}
        ],
        "allowed_execution_statuses": [],
    }
    _validate_formal_constraints([contract])
    broken = {**contract, "citation_required_task_types": ["inspection"]}
    with pytest.raises(ValueError, match="两类已安排任务"):
        _validate_formal_constraints([broken])
    assert not _purpose_confirmed_in_case({"draft": {"profile": {"maintenance_purpose": None}}})


def test_declared_adapter_failure_may_require_a_failed_chat_instead_of_a_tool() -> None:
    contract = {
        "case_id": "PUBLIC-FAIL",
        "basis": ["fixed fault fixture"],
        "required_tasks": [],
        "forbidden_task_types": ["inspection", "maintenance"],
        "required_missing_fields": [],
        "citation_required_task_types": ["inspection", "maintenance"],
        "semantic_required": False,
        "c_only_assertions": [{"step": 2, "operation": "chat", "status": "failed"}],
        "allowed_execution_statuses": ["failed"],
        "expected_fault_injection": True,
    }
    _validate_formal_constraints([contract])


def test_formal_seal_rejects_missing_cases_before_creating_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nongshi.d02_sealed._git", lambda *args: "" if args[0] == "status" else "abc123"
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory = root / "sealed"
        with pytest.raises(ValueError, match="30个主案例和6个边界案例"):
            seal(_sources(root), directory)
        assert not directory.exists()


def test_partial_first_attempt_cannot_be_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nongshi.d02_sealed._git", lambda *args: "" if args[0] == "status" else "abc123"
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory = root / "sealed"
        seal(_sources(root), directory, development=True)
        (directory / "first_attempts.partial.jsonl").write_text("{}\n", encoding="utf-8")
        with pytest.raises(FileExistsError, match="首次记录已存在"):
            run_seal(directory, settings=Settings(llm_api_key="test-key"))


def test_boundary_repeats_have_fresh_databases_and_first_attempts_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "nongshi.d02_sealed._git", lambda *args: "" if args[0] == "status" else "abc123"
    )
    databases: list[Path] = []

    def fake_c(_case: dict, *, database: Path) -> dict:
        databases.append(database)
        return {"status": "executed", "result": {"current_plan_items": []}, "elapsed_ms": 1}

    def fake_model(_case: dict, _group: str, **_kwargs: object) -> dict:
        return {
            "status": "executed",
            "result": {"tasks": [], "missing_fields": []},
            "model_calls": 1,
            "elapsed_ms": 2,
            "token_usage": {"prompt": 1, "completion": 1},
        }

    monkeypatch.setattr("nongshi.d02_sealed.run_group_c", fake_c)
    monkeypatch.setattr("nongshi.d02_sealed.run_model_group", fake_model)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sources = _sources(root)
        boundary = json.loads(DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[1])
        sources["boundary_cases"].write_text(
            json.dumps(boundary, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        sources["boundary_constraints"].write_text(
            json.dumps({"case_id": boundary["case_id"]}) + "\n", encoding="utf-8"
        )
        directory = root / "sealed"
        seal(sources, directory, development=True)
        summary = run_seal(directory, settings=Settings(llm_api_key="test-key"), seed=7)
        assert {key: summary[key] for key in ("trials", "main", "boundary")} == {
            "trials": 6,
            "main": 3,
            "boundary": 3,
        }
        assert len(summary["first_attempts_sha256"]) == 64
        assert len(databases) == len(set(databases)) == 4
        first = json.loads((directory / "first_attempts.json").read_text(encoding="utf-8"))
        assert len(first["records"]) == 6
        mapping = json.loads((directory / "private_group_map.json").read_text(encoding="utf-8"))
        assert len(mapping) == 6
        scores = score_seal(directory)
        assert scores["classification"] == "public_development_only"
        assert scores["c_auto_acceptance"]["conclusion"] == "development_only"
        assert scores["boundary"]["trials"] == 3
        (directory / "first_attempts.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="首次结果或组别映射已改变"):
            prepare_review(directory)
        with pytest.raises(FileExistsError):
            run_seal(directory, settings=Settings(llm_api_key="test-key"))
