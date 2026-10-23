"""Retrospective scoring correction for immutable first-attempt records.

The archived seal and its first score are read-only. New blind packets, AI reviews,
and corrected scores live in a separate private directory with their own lineage.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nongshi import d02_sealed as sealed
from nongshi.config import Settings
from nongshi.d02_scoring import score_case, summarize_scores
from nongshi.d02_semantic import JUDGE_PROMPT, judge_packet, write_blind_review_packets


def _source_manifest(source: Path) -> dict[str, Any]:
    manifest = json.loads((source / "seal.json").read_text(encoding="utf-8"))
    for item in manifest["inputs"].values():
        if sealed._sha256(source / item["name"]) != item["sha256"]:
            raise ValueError("原封存输入指纹不符")
    sealed._verify_run_integrity(source)
    if not (source / "first_scores.json").exists():
        raise FileNotFoundError("原首次评分缺失")
    return manifest


def _read_correction(output: Path) -> tuple[Path, dict[str, Any]]:
    manifest = json.loads((output / "correction_manifest.json").read_text(encoding="utf-8"))
    source = Path(manifest["source_seal"])
    _source_manifest(source)
    for name, digest in manifest["source_hashes"].items():
        if sealed._sha256(source / name) != digest:
            raise ValueError(f"原始材料在勘误后改变：{name}")
    if sealed._git("rev-parse", "HEAD") != manifest["correction_code_commit"]:
        raise ValueError("当前程序不是勘误时的提交")
    if sealed._git("status", "--porcelain"):
        raise ValueError("勘误运行需使用干净提交")
    return source, manifest


def prepare_correction(
    source: Path, output: Path, *, reuse_source_reviews: bool = False
) -> dict[str, Any]:
    """Build a new blinded view without altering the archived trial."""
    source = source.resolve()
    output = output.resolve()
    if sealed._inside_git_checkout(output) or output == source or source in output.parents:
        raise ValueError("勘误目录必须与原封存分离，并位于公开仓库外")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("勘误目录非空")
    if sealed._git("status", "--porcelain"):
        raise ValueError("请先提交勘误程序，再制作盲评材料")
    _source_manifest(source)
    cases = {
        row["case_id"]: row
        for name in ("main_cases", "boundary_cases")
        for row in sealed._read_jsonl(source / sealed.FILES[name])
    }
    records = json.loads((source / "first_attempts.json").read_text(encoding="utf-8"))["records"]
    source_reviews = source / "semantic_reviews.json"
    if reuse_source_reviews and not source_reviews.exists():
        raise FileNotFoundError("原封存缺少语义复核，不能复用")
    output.mkdir(parents=True, exist_ok=True)
    write_blind_review_packets(records, cases, output / "semantic_blind.jsonl")
    if reuse_source_reviews:
        (output / "semantic_reviews.json").write_bytes(source_reviews.read_bytes())
    tracked = [
        "seal.json",
        "first_attempts.json",
        "private_group_map.json",
        "run_integrity.json",
        "first_scores.json",
        *sealed.FILES.values(),
    ]
    if reuse_source_reviews:
        tracked.append("semantic_reviews.json")
    manifest = {
        "classification": "retrospective_scoring_correction_not_new_trial",
        "source_seal": str(source),
        "source_hashes": {name: sealed._sha256(source / name) for name in tracked},
        "correction_code_commit": sealed._git("rev-parse", "HEAD"),
        "judge_prompt": JUDGE_PROMPT,
        "prepared_at": datetime.now(UTC).isoformat(),
        "blind_sha256": sealed._sha256(output / "semantic_blind.jsonl"),
        "review_origin": "source_reused_unchanged"
        if reuse_source_reviews
        else "new_ai_assisted_review",
    }
    (output / "correction_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"records": len(records), "blind_sha256": manifest["blind_sha256"]}


def judge_correction(output: Path, *, settings: Settings) -> dict[str, int]:
    source, manifest = _read_correction(output)
    frozen_config = _source_manifest(source)["model_configuration"]
    if (
        settings.llm_model != frozen_config["model"]
        or settings.llm_base_url.rstrip("/") != frozen_config["base_url"]
    ):
        raise ValueError("语义复核模型或地址与原封存配置不一致")
    if not settings.llm_api_key.get_secret_value().strip():
        raise ValueError("本地密钥不可用")
    blind = output / "semantic_blind.jsonl"
    if sealed._sha256(blind) != manifest["blind_sha256"]:
        raise ValueError("盲化材料指纹不符")
    target = output / "semantic_reviews.json"
    checkpoint = output / "semantic_reviews.partial.jsonl"
    if target.exists():
        raise FileExistsError("勘误复核已完成")
    packets = sealed._read_jsonl(blind)
    reviews = (
        {
            row["result_id"]: {key: value for key, value in row.items() if key != "result_id"}
            for row in sealed._read_jsonl(checkpoint)
        }
        if checkpoint.exists()
        else {}
    )
    packet_ids = {row["result_id"] for row in packets}
    if set(reviews) - packet_ids:
        raise ValueError("复核检查点含未知编号")
    for packet in packets:
        if packet["result_id"] in reviews:
            continue
        outcome = judge_packet(packet, settings=settings)
        reviews[packet["result_id"]] = outcome
        with checkpoint.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"result_id": packet["result_id"], **outcome}, ensure_ascii=False) + "\n"
            )
    target.write_text(json.dumps(reviews, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "total": len(reviews),
        "reviewed": sum(r["status"] == "reviewed" for r in reviews.values()),
    }


def score_correction(output: Path) -> dict[str, Any]:
    source, manifest = _read_correction(output)
    target = output / "corrected_scores.json"
    if target.exists():
        raise FileExistsError("勘误评分已存在")
    reviews_path = output / "semantic_reviews.json"
    if manifest.get("review_origin") == "source_reused_unchanged" and (
        sealed._sha256(reviews_path) != manifest["source_hashes"]["semantic_reviews.json"]
    ):
        raise ValueError("复用的语义复核与原封存指纹不符")
    reviews = json.loads(reviews_path.read_text(encoding="utf-8"))
    raw = json.loads((source / "first_attempts.json").read_text(encoding="utf-8"))
    mapping = json.loads((source / "private_group_map.json").read_text(encoding="utf-8"))
    cases = {
        row["case_id"]: row
        for name in ("main_cases", "boundary_cases")
        for row in sealed._read_jsonl(source / sealed.FILES[name])
    }
    constraints = {
        row["case_id"]: row
        for name in ("main_constraints", "boundary_constraints")
        for row in sealed._read_jsonl(source / sealed.FILES[name])
    }
    result_ids = {row["result_id"] for row in raw["records"]}
    if result_ids != set(mapping) or result_ids != set(reviews):
        raise ValueError("首次记录、组别映射和新复核编号不一致")
    rows = []
    for record in raw["records"]:
        result_id = record["result_id"]
        group = {"A": "g1", "B": "g2", "C": "g3"}[mapping[result_id]["group"]]
        score = score_case(
            cases[record["case_id"]],
            constraints[record["case_id"]],
            {**record, "group_code": group},
            semantic=reviews[result_id],
        )
        rows.append(
            {**score, "group_code": group, "set": record["set"], "repeat": record["repeat"]}
        )
    c_rows = [row for row in rows if row["group_code"] == "g3"]
    supported = sum(row["semantic_supported"] for row in c_rows)
    total = sum(row["semantic_total"] for row in c_rows)
    report = {
        "classification": manifest["classification"],
        "original_first_scores_sha256": manifest["source_hashes"]["first_scores.json"],
        "original_first_attempts_sha256": manifest["source_hashes"]["first_attempts.json"],
        "new_review_sha256": sealed._sha256(reviews_path),
        "review_origin": manifest.get("review_origin", "new_ai_assisted_review"),
        "records": len(rows),
        "main": summarize_scores([row for row in rows if row["set"] == "main"]),
        "boundary": dict(Counter(row["verdict"] for row in rows if row["set"] == "boundary")),
        "c_semantic_support": {
            "supported": supported,
            "total": total,
            "rate": supported / total if total else None,
        },
        "scores": rows,
    }
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {key: report[key] for key in ("records", "main", "boundary", "c_semantic_support")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparing = commands.add_parser("prepare")
    preparing.add_argument("--source-seal", required=True, type=Path)
    preparing.add_argument("--output-dir", required=True, type=Path)
    preparing.add_argument("--reuse-source-reviews", action="store_true")
    judging = commands.add_parser("judge")
    judging.add_argument("--correction-dir", required=True, type=Path)
    judging.add_argument("--env-file", type=Path)
    scoring = commands.add_parser("score")
    scoring.add_argument("--correction-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_correction(
            args.source_seal,
            args.output_dir,
            reuse_source_reviews=args.reuse_source_reviews,
        )
    elif args.command == "judge":
        result = judge_correction(
            args.correction_dir,
            settings=Settings(_env_file=args.env_file) if args.env_file else Settings(),
        )
    else:
        result = score_correction(args.correction_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
