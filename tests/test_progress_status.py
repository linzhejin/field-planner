"""Progress checks must reject stale summaries without touching evaluation records."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_progress.py"


def test_checker_is_available():
    assert SCRIPT.is_file(), "missing read-only progress checker"


def load_checker():
    assert SCRIPT.is_file(), "missing read-only progress checker"
    spec = importlib.util.spec_from_file_location("progress_checker", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def evidence(tmp_path: Path):
    checker = load_checker()
    repo = tmp_path / "repo"
    evaluation_root = tmp_path / "evaluations"
    run = evaluation_root / "sealed-internal-regression-v12-20261024"
    scores = {
        "classification": "internal_regression_after_v1",
        "c_auto_acceptance": {"conclusion": "passed"},
        "main": {
            "g1": {"trials": 30, "verdicts": {"passed": 1, "failed": 29}},
            "g2": {"trials": 30, "verdicts": {"passed": 7, "failed": 23}},
            "g3": {"trials": 30, "verdicts": {"passed": 30}, "hard_violations": 0},
        },
        "boundary": {"passed_trials": 18, "trials": 18, "all_three_passed_cases": 6, "cases": 6},
    }
    seal = {
        "version": "internal-auto-v7",
        "sealed_at": "2026-10-23T21:47:18+00:00",
        "code_commit": "a" * 40,
        "classification": scores["classification"],
    }
    write_json(run / "seal.json", seal)
    write_json(run / "first_scores.json", scores)
    write_json(run / "first_attempts.json", {"private": "do not print this content"})
    write_json(run / "private_group_map.json", {"private": "anonymous mapping"})
    write_json(
        run / "run_integrity.json",
        {
            "code_commit": seal["code_commit"],
            "first_attempts_sha256": digest(run / "first_attempts.json"),
            "private_group_map_sha256": digest(run / "private_group_map.json"),
        },
    )
    report = repo / "reports" / "v12.md"
    report.parent.mkdir(parents=True)
    report.write_text("Frozen v12 report", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "updated_at": "2026-10-24T10:00:00+08:00",
        "evaluation": {
            "run_id": run.name,
            "sealed_at": seal["sealed_at"],
            "code_commit": seal["code_commit"],
            "classification": seal["classification"],
            "report": "reports/v12.md",
            "expected": {
                "conclusion": "passed",
                "main": {
                    k: {"trials": v["trials"], "verdicts": v["verdicts"]}
                    for k, v in scores["main"].items()
                },
                "boundary": scores["boundary"],
                "hard_violations": 0,
            },
            "artifacts": {p.name: digest(p) for p in run.iterdir()},
        },
        "public_artifacts": {"reports/v12.md": digest(report)},
        "source_tests": {"passed": 444, "code_commit": seal["code_commit"]},
    }
    write_json(repo / "reports/current_status.json", manifest)
    (repo / "STATUS.md").write_text(
        "# Current progress\n\n" + checker.render_summary(manifest) + "\n", encoding="utf-8"
    )
    return checker, repo, evaluation_root, run, manifest


def test_verified_snapshot_is_read_only_and_does_not_expose_private_inputs(evidence):
    checker, repo, root, run, _ = evidence
    before = {p.name: digest(p) for p in run.iterdir()}
    result = checker.check_progress(repo, root, worktrees=[], require_private=True)
    assert result["errors"] == []
    assert result["private_evidence"] == "verified"
    assert before == {p.name: digest(p) for p in run.iterdir()}
    assert "do not print this content" not in json.dumps(result)


def test_newer_run_is_found_by_seal_time_not_protocol_version(evidence):
    checker, repo, root, _, manifest = evidence
    write_json(
        root / "sealed-internal-regression-v13-20261024" / "seal.json",
        {"version": "internal-auto-v7", "sealed_at": "2026-10-24T05:00:00+00:00"},
    )
    result = checker.check_progress(repo, root, worktrees=[])
    assert any("NEWER_RUN" in error for error in result["errors"])
    assert manifest["evaluation"]["run_id"] in result["run_id"]


def test_changed_raw_output_is_rejected(evidence):
    checker, repo, root, run, _ = evidence
    (run / "first_attempts.json").write_text("{}", encoding="utf-8")
    result = checker.check_progress(repo, root, worktrees=[])
    assert any("HASH_MISMATCH" in error for error in result["errors"])


def test_frozen_commit_disagreement_is_rejected_even_with_new_hash(evidence):
    checker, repo, root, run, manifest = evidence
    seal = json.loads((run / "seal.json").read_text(encoding="utf-8"))
    seal["code_commit"] = "b" * 40
    write_json(run / "seal.json", seal)
    manifest["evaluation"]["artifacts"]["seal.json"] = digest(run / "seal.json")
    write_json(repo / "reports/current_status.json", manifest)
    result = checker.check_progress(repo, root, worktrees=[])
    assert any("SEAL_MISMATCH" in error for error in result["errors"])


def test_human_summary_drift_is_rejected(evidence):
    checker, repo, root, _, _ = evidence
    path = repo / "STATUS.md"
    path.write_text(path.read_text(encoding="utf-8").replace("30/30", "11/30"), encoding="utf-8")
    result = checker.check_progress(repo, root, worktrees=[])
    assert any("SUMMARY_DRIFT" in error for error in result["errors"])


def test_other_worktree_manifest_conflict_is_rejected(evidence, tmp_path):
    checker, repo, root, _, manifest = evidence
    other = tmp_path / "another_worktree"
    manifest["evaluation"]["run_id"] = "sealed-internal-regression-v13-20261024"
    write_json(other / "reports/current_status.json", manifest)
    result = checker.check_progress(repo, root, worktrees=[other])
    assert any("WORKTREE_CONFLICT" in error for error in result["errors"])


def test_other_worktree_human_summary_drift_is_rejected(evidence, tmp_path):
    checker, repo, root, _, manifest = evidence
    other = tmp_path / "another_worktree"
    write_json(other / "reports/current_status.json", manifest)
    (other / "STATUS.md").write_text("Old v3 failed result", encoding="utf-8")
    result = checker.check_progress(repo, root, worktrees=[other])
    assert any("WORKTREE_SUMMARY_DRIFT" in error for error in result["errors"])


def test_other_worktree_public_report_change_is_rejected(evidence, tmp_path):
    checker, repo, root, _, manifest = evidence
    other = tmp_path / "another_worktree"
    write_json(other / "reports/current_status.json", manifest)
    (other / "STATUS.md").write_text(checker.render_summary(manifest), encoding="utf-8")
    (other / "reports/v12.md").write_text("Changed after manifest was written", encoding="utf-8")
    result = checker.check_progress(repo, root, worktrees=[other])
    assert any("WORKTREE_PUBLIC_HASH_MISMATCH" in error for error in result["errors"])


def test_current_claim_must_match_scores_even_if_hash_is_updated(evidence):
    checker, repo, root, run, manifest = evidence
    path = run / "first_scores.json"
    scores = json.loads(path.read_text(encoding="utf-8"))
    scores["main"]["g3"]["verdicts"] = {"passed": 29, "failed": 1}
    write_json(path, scores)
    manifest["evaluation"]["artifacts"][path.name] = digest(path)
    write_json(repo / "reports/current_status.json", manifest)
    result = checker.check_progress(repo, root, worktrees=[])
    assert any("SCORE_MISMATCH" in error for error in result["errors"])


def test_missing_private_evidence_is_explicit_and_can_be_required(evidence, tmp_path):
    checker, repo, _, _, _ = evidence
    result = checker.check_progress(repo, tmp_path / "absent", worktrees=[])
    assert result["private_evidence"] == "unavailable"
    assert result["errors"] == []
    required = checker.check_progress(repo, tmp_path / "absent", worktrees=[], require_private=True)
    assert any("PRIVATE_UNAVAILABLE" in error for error in required["errors"])
