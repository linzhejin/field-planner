"""Read-only reconciliation of published progress, frozen evidence and Git worktrees.

This checks recorded evidence; it does not rerun model calls, alter scores or certify
the current checkout. Private case text and model responses are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MANIFEST = Path("reports/current_status.json")
START = "<!-- CURRENT_STATUS:START -->"
END = "<!-- CURRENT_STATUS:END -->"


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_summary(manifest: dict) -> str:
    evaluation = manifest["evaluation"]
    expected = evaluation["expected"]
    main = expected["main"]
    boundary = expected["boundary"]
    verdict = {"passed": "通过", "failed": "未通过"}.get(expected["conclusion"], "证据不足")
    lines = [
        START,
        f"**最新冻结结论：内部已知题自动阶段验收{verdict}。**",
        "",
        f"- 批次：`{evaluation['run_id']}`。",
        f"- 冻结代码：`{evaluation['code_commit']}`。",
        f"- 证据范围：`{evaluation['classification']}`；不属于独立盲测或田间验证。",
        "",
        "| 组别 | 主案例通过 | 边界运行 |",
        "| --- | ---: | --- |",
    ]
    for key, label in (("g1", "A 直接生成"), ("g2", "B 模型加检索"), ("g3", "C 完整 Agent")):
        score = main[key]
        extra = "未运行（协议仅对 C 组要求）"
        if key == "g3":
            extra = f"{boundary['passed_trials']}/{boundary['trials']} 通过"
        lines.append(
            f"| {label} | {score['verdicts'].get('passed', 0)}/{score['trials']} | {extra} |"
        )
    lines += [
        "",
        f"C 组硬约束违规 {expected['hard_violations']}；"
        f"边界 {boundary['all_three_passed_cases']}/{boundary['cases']} 题均连续通过三次。",
        f"冻结版本的自动测试记录为 {manifest['source_tests']['passed']} 项通过；"
        "这是对应版本的历史运行记录，不表示当前工作区已重新运行。",
        f"公开证据：[本批次报告]({evaluation['report']})。",
        END,
    ]
    return "\n".join(lines)


def git_output(root: Path, *args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def list_worktrees(root: Path) -> list[Path]:
    code, output = git_output(root, "worktree", "list", "--porcelain")
    if code:
        return [root]
    return [
        Path(line.removeprefix("worktree "))
        for line in output.splitlines()
        if line.startswith("worktree ")
    ]


def source_context(root: Path, frozen_commit: str) -> dict:
    code, head = git_output(root, "rev-parse", "HEAD")
    if code:
        return {"path": str(root), "git": "unavailable"}
    ancestor, _ = git_output(root, "merge-base", "--is-ancestor", frozen_commit, "HEAD")
    diff_code, changed = git_output(
        root,
        "diff",
        "--name-only",
        frozen_commit,
        "HEAD",
        "--",
        "src/nongshi",
        "pyproject.toml",
        "uv.lock",
    )
    dirty_code, dirty = git_output(
        root, "status", "--porcelain", "--", "src/nongshi", "pyproject.toml", "uv.lock"
    )
    return {
        "path": str(root),
        "head": head,
        "contains_frozen_commit": ancestor == 0,
        "runtime_matches_frozen": diff_code == 0 and dirty_code == 0 and not changed and not dirty,
        "committed_runtime_differences": changed.splitlines(),
        "uncommitted_runtime_changes": dirty.splitlines(),
    }


def score_summary(scores: dict) -> dict:
    return {
        "conclusion": scores["c_auto_acceptance"]["conclusion"],
        "main": {
            key: {"trials": value["trials"], "verdicts": value["verdicts"]}
            for key, value in scores["main"].items()
        },
        "boundary": scores["boundary"],
        "hard_violations": scores["main"]["g3"]["hard_violations"],
    }


def check_progress(
    root: Path,
    evaluation_root: Path,
    *,
    worktrees: list[Path] | None = None,
    require_private: bool = False,
) -> dict:
    result = {"errors": [], "warnings": [], "private_evidence": "unavailable", "worktrees": []}
    errors = result["errors"]
    warnings = result["warnings"]
    try:
        manifest = read_json(root / MANIFEST)
        evaluation = manifest["evaluation"]
        result["run_id"] = evaluation["run_id"]
        result["frozen_commit"] = evaluation["code_commit"]
        if manifest["schema_version"] != 1:
            raise ValueError("unsupported schema")
        status_text = (root / "STATUS.md").read_text(encoding="utf-8-sig")
        if render_summary(manifest) not in status_text:
            errors.append("SUMMARY_DRIFT: STATUS.md 核心摘要与 current_status.json 不一致")
        for relative, expected_hash in manifest["public_artifacts"].items():
            path = root / relative
            if not path.is_file() or digest(path) != expected_hash:
                errors.append(f"PUBLIC_HASH_MISMATCH: {relative}")
        if evaluation["report"] not in manifest["public_artifacts"]:
            errors.append("REPORT_UNPINNED: 当前报告未登记文件指纹")
        current_time = datetime.fromisoformat(evaluation["sealed_at"])
        if current_time.utcoffset() is None:
            raise ValueError("seal time must include timezone")
    except (OSError, ValueError, KeyError, TypeError):
        errors.append("MANIFEST_INVALID: 当前进度入口、字段或时间缺失/无效")
        return result

    # Compare registered worktrees, not just the invoking checkout.
    roots = list_worktrees(root) if worktrees is None else worktrees
    for other in roots:
        context = source_context(other, evaluation["code_commit"])
        result["worktrees"].append(context)
        if context.get("git") != "unavailable" and not context["runtime_matches_frozen"]:
            warnings.append(f"CHECKOUT_DIFFERS: {other} 的当前源码不等同于冻结验收版本")
        if not (other / MANIFEST).is_file():
            warnings.append(f"NO_STATUS_ENTRY: {other} 未登记当前进度（可能为历史工作树）")
            continue
        try:
            if read_json(other / MANIFEST) != manifest:
                errors.append(f"WORKTREE_CONFLICT: {other} 的当前进度记录不同")
            other_status = (other / "STATUS.md").read_text(encoding="utf-8-sig")
            if render_summary(manifest) not in other_status:
                errors.append(f"WORKTREE_SUMMARY_DRIFT: {other} 的进度文字与登记摘要不一致")
            for relative, expected_hash in manifest["public_artifacts"].items():
                path = other / relative
                if not path.is_file() or digest(path) != expected_hash:
                    errors.append(f"WORKTREE_PUBLIC_HASH_MISMATCH: {path}")
        except (OSError, ValueError):
            errors.append(f"WORKTREE_INVALID: {other} 的当前进度记录无法读取")
        match = re.search(r"-v(\d+)-", evaluation["run_id"])
        if match:
            current_number = int(match[1])
            for report in (other / "reports").glob("internal_regression_v*.md"):
                numbers = re.findall(r"(?:^internal_regression_|_)v(\d+)", report.name)
                if numbers and max(map(int, numbers)) > current_number:
                    errors.append(f"NEWER_REPORT: {report} 尚未并入当前进度记录")

    # Scan all seals including incomplete newer runs; file mtime/protocol version
    # is not an evaluation ordering key (v12 still uses internal-auto-v7).
    for seal_path in evaluation_root.glob("sealed-*/seal.json"):
        try:
            seal = read_json(seal_path)
            candidate_time = datetime.fromisoformat(seal["sealed_at"])
            if candidate_time > current_time:
                errors.append(f"NEWER_RUN: {seal_path.parent.name} 比登记批次新，须核实后更新入口")
        except (OSError, ValueError, KeyError, TypeError):
            errors.append(f"UNREADABLE_SEAL: {seal_path.parent.name}")

    run = evaluation_root / evaluation["run_id"]
    if not run.is_dir():
        message = "PRIVATE_UNAVAILABLE: 未取得本机原始评测证据，只检查了公开进度文件"
        (errors if require_private else warnings).append(message)
        return result
    private_errors = len(errors)
    for name, expected_hash in evaluation["artifacts"].items():
        path = run / name
        if not path.is_file() or digest(path) != expected_hash:
            errors.append(f"HASH_MISMATCH: {evaluation['run_id']}/{name}")
    try:
        seal = read_json(run / "seal.json")
        for key in ("code_commit", "sealed_at", "classification"):
            if seal[key] != evaluation[key]:
                errors.append(f"SEAL_MISMATCH: {key}")
        scores = read_json(run / "first_scores.json")
        if score_summary(scores) != evaluation["expected"]:
            errors.append("SCORE_MISMATCH: 冻结原始评分与登记摘要不一致")
        if scores["classification"] != evaluation["classification"]:
            errors.append("SCOPE_MISMATCH: 原始评分证据范围不一致")
        integrity = read_json(run / "run_integrity.json")
        if integrity["code_commit"] != evaluation["code_commit"]:
            errors.append("INTEGRITY_COMMIT_MISMATCH: 原始运行提交不一致")
        for name, key in (
            ("first_attempts.json", "first_attempts_sha256"),
            ("private_group_map.json", "private_group_map_sha256"),
        ):
            if digest(run / name) != integrity[key]:
                errors.append(f"INTEGRITY_HASH_MISMATCH: {name}")
    except (OSError, ValueError, KeyError, TypeError):
        errors.append("RAW_EVIDENCE_INVALID: 原始记录缺失或结构无法核验")
    result["private_evidence"] = "verified" if len(errors) == private_errors else "invalid"
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--eval-root", type=Path, default=Path.home() / "Documents/nongshi-eval")
    parser.add_argument("--require-private", action="store_true", help="缺少本机原始证据时返回失败")
    parser.add_argument("--json", action="store_true", help="输出脱敏核查结果")
    parser.add_argument("--render-summary", action="store_true", help="只将核心摘要输出至标准输出")
    args = parser.parse_args()
    if args.render_summary:
        print(render_summary(read_json(args.repo / MANIFEST)))
        return 0
    result = check_progress(args.repo, args.eval_root, require_private=args.require_private)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"登记批次：{result.get('run_id', '缺失')}")
        print(f"原始证据：{result['private_evidence']}")
        for error in result["errors"]:
            print(f"错误：{error}")
        for warning in result["warnings"]:
            print(f"提示：{warning}")
        print("进度一致性：" + ("未通过" if result["errors"] else "通过"))
        print("本命令未重跑测试或在线模型，不将冻结成绩授予当前未验证源码。")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
