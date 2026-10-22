"""Freeze, run, and score a private D02/D03 evaluation without overwriting first attempts.

Private inputs, constraints, mapping, raw runs, and scores belong outside the public repo.
The legacy ``nongshi.d02_runner`` development entrypoint remains available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import secrets
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from nongshi.config import Settings
from nongshi.d02_model_groups import run_model_group
from nongshi.d02_runner import (
    PROJECT_ROOT,
    _load_cases,
    _parse_moment,
    injected_failure_for_step,
    run_group_c,
    run_scripted_case,
    validate_case,
)
from nongshi.d02_scoring import score_case, summarize_scores
from nongshi.d02_semantic import judge_packet, write_blind_review_packets

SEAL_VERSION = "internal-auto-v1"
FILES = {
    "main_cases": "main_cases.jsonl",
    "main_constraints": "main_constraints.jsonl",
    "boundary_cases": "boundary_cases.jsonl",
    "boundary_constraints": "boundary_constraints.jsonl",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(_sha256(path).encode("ascii"))
    return digest.hexdigest()


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _inside_git_checkout(path: Path) -> bool:
    parent = path.resolve()
    while not parent.exists():
        parent = parent.parent
    result = subprocess.run(
        ["git", "-C", str(parent), "rev-parse", "--show-toplevel"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return _load_cases(path)


def _validate_pair(cases: list[dict[str, Any]], constraints: list[dict[str, Any]]) -> None:
    case_ids = [str(row.get("case_id")) for row in cases]
    contract_ids = [str(row.get("case_id")) for row in constraints]
    if len(set(case_ids)) != len(case_ids) or set(case_ids) != set(contract_ids):
        raise ValueError("案例与约束编号不一一对应或存在重复")
    if len(set(contract_ids)) != len(contract_ids):
        raise ValueError("参考约束编号重复")
    for case in cases:
        checked = validate_case(case)
        if checked["status"] == "invalid":
            raise ValueError(f"{case['case_id']} 输入无效：{checked['problems']}")


def _validate_formal_constraints(constraints: list[dict[str, Any]]) -> None:
    required = {
        "case_id",
        "basis",
        "required_tasks",
        "forbidden_task_types",
        "required_missing_fields",
        "citation_required_task_types",
        "semantic_required",
        "c_only_assertions",
        "allowed_execution_statuses",
    }
    for contract in constraints:
        missing = required - contract.keys()
        if missing:
            raise ValueError(f"{contract.get('case_id')} 缺少预先定义的评分字段：{sorted(missing)}")
        if not contract["basis"]:
            raise ValueError(f"{contract['case_id']} 未登记参考约束依据")
        if set(contract["citation_required_task_types"]) != {"inspection", "maintenance"}:
            raise ValueError(f"{contract['case_id']} 两类已安排任务均须预先纳入引用检查")
        if not contract["semantic_required"] and (
            set(contract["forbidden_task_types"]) != {"inspection", "maintenance"}
            or contract["required_tasks"]
        ):
            raise ValueError(f"{contract['case_id']} 语义复核不适用仅限预期无农业建议的案例")
        if not any(
            contract[field]
            for field in (
                "required_tasks",
                "forbidden_task_types",
                "required_missing_fields",
                "c_only_assertions",
                "allowed_execution_statuses",
            )
        ):
            raise ValueError(f"{contract['case_id']} 没有可执行的参考断言")
        has_tool_assertion = any(
            row.get("operation") == "chat" and row.get("required_tool")
            for row in contract["c_only_assertions"]
        )
        expected_fault = contract.get("expected_fault_injection") is True and any(
            row.get("operation") == "chat" and row.get("status") == "failed"
            for row in contract["c_only_assertions"]
        )
        if not has_tool_assertion and not expected_fault:
            raise ValueError(f"{contract['case_id']} 缺少C组真实对话工具断言")


def _purpose_confirmed_in_case(case: dict[str, Any]) -> bool:
    profile = (case.get("draft") or {}).get("profile") or {}
    if profile.get("maintenance_purpose") == "clear_existing_ditch":
        return True
    for step in case.get("interaction_script") or []:
        payload = step.get("payload") or {}
        request = payload.get("new_task_request") or {}
        if request.get("maintenance_purpose") == "clear_existing_ditch":
            return True
        if any(
            (event.get("feedback") or {}).get("field") == "maintenance_purpose"
            and (event.get("feedback") or {}).get("value") == "clear_existing_ditch"
            for event in payload.get("events") or []
        ):
            return True
    return False


def seal(
    sources: dict[str, Path],
    output_directory: Path,
    *,
    development: bool = False,
    known_exposure: list[str] | None = None,
    model: str = "deepseek-flash",
    base_url: str = "https://api.deepseek.com",
) -> dict[str, Any]:
    """Lock inputs, judgments and code before results exist; refuse a dirty code tree."""
    if _inside_git_checkout(output_directory):
        raise ValueError("封存目录必须位于公开仓库及其他 Git 工作区外")
    if output_directory.exists() and any(output_directory.iterdir()):
        raise FileExistsError("封存目录非空；首次记录不得覆盖")
    if _git("status", "--porcelain"):
        raise ValueError("待测代码尚有未提交改动，不能封存")
    if set(sources) != set(FILES):
        raise ValueError("需要主案例、主约束、边界案例、边界约束四个独立文件")
    loaded = {key: _read_jsonl(path) for key, path in sources.items()}
    _validate_pair(loaded["main_cases"], loaded["main_constraints"])
    _validate_pair(loaded["boundary_cases"], loaded["boundary_constraints"])
    if not development and (len(loaded["main_cases"]) != 30 or len(loaded["boundary_cases"]) != 6):
        raise ValueError("正式封存需要30个主案例和6个边界案例")
    if not development:
        if not known_exposure:
            raise ValueError("正式封存必须登记已知题目接触与暴露记录")
        _validate_formal_constraints(loaded["main_constraints"] + loaded["boundary_constraints"])
        all_cases = loaded["main_cases"] + loaded["boundary_cases"]
        by_id = {
            row["case_id"]: row
            for row in loaded["main_constraints"] + loaded["boundary_constraints"]
        }
        for case in all_cases:
            if by_id[case["case_id"]].get("expected_fault_injection") and not case.get(
                "adapter_failure_fixture"
            ):
                raise ValueError(f"{case['case_id']} 评分预期接口故障，但输入未封存故障夹具")
            expects_maintenance = any(
                row.get("task_type") == "maintenance"
                and "scheduled" in (row.get("allowed_statuses") or [])
                for row in by_id[case["case_id"]]["required_tasks"]
            )
            if expects_maintenance and not _purpose_confirmed_in_case(case):
                raise ValueError(f"{case['case_id']} 维护目的未知，不能封存为必排维护")
        expected = {
            "normal": 8,
            "missing": 6,
            "conflict": 6,
            "feedback": 6,
            "inapplicable_or_failure": 4,
        }
        actual = {
            name: sum(case.get("classification") == name for case in loaded["main_cases"])
            for name in expected
        }
        if actual != expected:
            raise ValueError(f"主案例分类数量不符：{actual}")
        if any(
            "interaction_script" not in case
            for case in loaded["main_cases"] + loaded["boundary_cases"]
        ):
            raise ValueError("正式案例必须包含固定逐轮交互脚本")
        if any(
            not any(step.get("operation") == "chat" for step in case["interaction_script"])
            for case in loaded["main_cases"] + loaded["boundary_cases"]
        ):
            raise ValueError("正式C组案例必须含实际 Agent 对话步骤")
        for case in loaded["main_cases"] + loaded["boundary_cases"]:
            allowed_operations = {
                "save_draft",
                "save_additional_field",
                "chat",
                "confirm_feedback",
                "adopt",
                "cancel_preview",
                "read_history",
            }
            unexpected = {
                step.get("operation") for step in case["interaction_script"]
            } - allowed_operations
            if unexpected:
                raise ValueError(f"{case['case_id']} 含绕过 Agent 的脚本操作：{sorted(unexpected)}")
            moments = [_parse_moment(case["received_at"])] + [
                _parse_moment(step["at"]) for step in case["interaction_script"]
            ]
            if moments != sorted(moments):
                raise ValueError(f"{case['case_id']} 消息接收时间倒流，须在首次运行前修订")
    output_directory.mkdir(parents=True, exist_ok=True)
    for key, path in sources.items():
        (output_directory / FILES[key]).write_bytes(path.read_bytes())
    tracked = {
        key: {
            "name": filename,
            "sha256": _sha256(output_directory / filename),
            "count": len(loaded[key]),
        }
        for key, filename in FILES.items()
    }
    dependencies = {
        name: _sha256(PROJECT_ROOT / name)
        for name in [
            "uv.lock",
            "src/nongshi/agent.py",
            "src/nongshi/d02_model_groups.py",
            "src/nongshi/d02_scoring.py",
            "src/nongshi/d02_sealed.py",
            "src/nongshi/d02_semantic.py",
            "src/nongshi/data/rule_adoption_development.json",
        ]
    }
    materials = {
        "rules_tree": _tree_sha256(PROJECT_ROOT / "rules"),
        "package_data_tree": _tree_sha256(PROJECT_ROOT / "src" / "nongshi" / "data"),
    }
    manifest = {
        "version": SEAL_VERSION,
        "sealed_at": datetime.now(UTC).isoformat(),
        "classification": "public_development_only"
        if development
        else "internal_sealed_auto_acceptance",
        "code_commit": _git("rev-parse", "HEAD"),
        "inputs": tracked,
        "dependencies": dependencies,
        "materials": materials,
        "model_configuration": {
            "model": model,
            "base_url": base_url.rstrip("/"),
            "temperature": 0,
            "thinking": "disabled",
            "max_tokens": 4096,
            "request_budget_seconds": 20,
            "a_b_requests_per_turn": 1,
            "c_requests_per_turn": 3,
            "c_tools_per_turn": 6,
        },
        "scoring_protocol": "hard gates and per-dimension pass rates; no 100-point total",
        "known_exposure": known_exposure
        if known_exposure is not None
        else ["候选题作者接触作者参考；其他接触范围尚未核实"],
        "independence_claim": "none; content hashes do not establish independent authorship",
    }
    target = output_directory / "seal.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify_seal(directory: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "seal.json").read_text(encoding="utf-8"))
    if manifest.get("version") != SEAL_VERSION:
        raise ValueError("封存协议版本不匹配")
    for item in manifest["inputs"].values():
        if _sha256(directory / item["name"]) != item["sha256"]:
            raise ValueError(f"封存文件已改变：{item['name']}")
    for name, digest in manifest["dependencies"].items():
        if _sha256(PROJECT_ROOT / name) != digest:
            raise ValueError(f"程序或资料已改变：{name}")
    material_roots = {
        "rules_tree": PROJECT_ROOT / "rules",
        "package_data_tree": PROJECT_ROOT / "src" / "nongshi" / "data",
    }
    for name, root in material_roots.items():
        if _tree_sha256(root) != manifest["materials"][name]:
            raise ValueError(f"封存资料已改变：{name}")
    if _git("rev-parse", "HEAD") != manifest["code_commit"] or _git("status", "--porcelain"):
        raise ValueError("运行代码不是封存时的干净提交")
    return manifest


def _model_turns(case: dict[str, Any], group: str, settings: Settings) -> dict[str, Any]:
    steps = case.get("interaction_script") or []
    turns = [step for step in steps if step.get("operation") == "chat"]
    if not turns:
        turns = [next((step for step in steps if step.get("user_message")), {"step": 0})]
    outputs = []
    previous_turns: list[dict[str, Any]] = []
    for step in turns:
        injected = injected_failure_for_step(case, step)
        if injected is not None:
            output = {
                "status": "failed",
                "scorable": True,
                "failure_status": injected.get("status"),
                "errors": [str(injected.get("message") or injected.get("status"))],
                "result": None,
                "first_result": None,
                "model_calls": 0,
                "elapsed_ms": 0,
                "token_usage": {"prompt": None, "completion": None},
                "tool_records": [],
                "cost": None,
                "cost_status": "unknown",
            }
        else:
            output = run_model_group(
                case,
                group,
                settings=settings,
                through_step=int(step["step"]),
                previous_turns=list(previous_turns),
            )
        outputs.append({"step": step["step"], "output": output})
        previous_turns.append(
            {
                "step": step["step"],
                "model_result": output.get("result"),
                "status": output["status"],
                "errors": output.get("errors") or [],
            }
        )
    last = outputs[-1]["output"]
    return {
        **last,
        "status": "executed"
        if all(row["output"]["status"] == "executed" for row in outputs)
        else "failed",
        "scorable": all(row["output"].get("scorable") is True for row in outputs),
        "steps": outputs,
        "model_calls": sum(row["output"]["model_calls"] for row in outputs),
        "elapsed_ms": sum(row["output"]["elapsed_ms"] for row in outputs),
        "tool_records": [
            record for row in outputs for record in row["output"].get("tool_records", [])
        ],
        "errors": [error for row in outputs for error in row["output"].get("errors", [])],
        "token_usage": {
            key: sum(row["output"]["token_usage"][key] or 0 for row in outputs)
            if all(row["output"]["token_usage"][key] is not None for row in outputs)
            else None
            for key in ("prompt", "completion")
        },
    }


def _common_result(outcome: dict[str, Any]) -> dict[str, Any]:
    raw = outcome.get("result") or {}
    if raw.get("displayed_result") is not None:
        raw = raw["displayed_result"]
    tasks = (
        raw.get("tasks")
        if isinstance(raw.get("tasks"), list)
        else raw.get("current_plan_items") or []
    )
    missing = list(raw.get("missing_fields") or [])
    missing.extend(field for row in tasks for field in row.get("missing_fields") or [])
    return {
        "tasks": tasks,
        "missing_fields": sorted(set(missing)),
        "execution_status": outcome.get("status"),
    }


def run_seal(directory: Path, *, settings: Settings, seed: int | None = None) -> dict[str, Any]:
    manifest = verify_seal(directory)
    result_path = directory / "first_attempts.json"
    mapping_path = directory / "private_group_map.json"
    checkpoint = directory / "first_attempts.partial.jsonl"
    mapping_checkpoint = directory / "private_group_map.partial.jsonl"
    if any(path.exists() for path in (result_path, mapping_path, checkpoint, mapping_checkpoint)):
        raise FileExistsError("首次记录已存在；回归必须建立新封存版本")
    config = manifest["model_configuration"]
    if (
        settings.llm_model != config["model"]
        or settings.llm_base_url.rstrip("/") != config["base_url"]
        or settings.llm_timeout_seconds != config["request_budget_seconds"]
    ):
        raise ValueError("运行模型、地址或20秒预算与封存配置不一致")
    if not settings.llm_api_key.get_secret_value().strip():
        raise ValueError("没有本地模型密钥；未启动首次正式运行")
    main = _read_jsonl(directory / FILES["main_cases"])
    boundary = _read_jsonl(directory / FILES["boundary_cases"])
    contracts = {
        row["case_id"]: row
        for name in ("main_constraints", "boundary_constraints")
        for row in _read_jsonl(directory / FILES[name])
    }
    jobs = [(case, group, 1, "main") for case in main for group in ("A", "B", "C")]
    jobs.extend((case, "C", repeat, "boundary") for case in boundary for repeat in range(1, 4))
    order_seed = seed if seed is not None else secrets.randbits(64)
    random.Random(order_seed).shuffle(jobs)
    records: list[dict[str, Any]] = []
    mapping: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="nongshi_sealed_") as temporary:
        for case, group, repeat, set_name in jobs:
            result_id = uuid4().hex
            db = Path(temporary) / f"{result_id}.sqlite3"
            try:
                if group == "C" and "interaction_script" in case:
                    outcome = run_scripted_case(
                        case, database=db, enable_model=True, settings=settings
                    )
                    if (
                        manifest["classification"] != "public_development_only"
                        and not contracts[case["case_id"]].get("expected_fault_injection")
                        and not any(
                            run.get("tool_count", 0) > 0
                            for run in (outcome.get("result") or {}).get("agent_runs") or []
                        )
                    ):
                        outcome["status"] = "failed"
                        outcome.setdefault("errors", []).append("C组没有可核查的 Agent 工具调用")
                elif group == "C":
                    outcome = run_group_c(case, database=db)
                else:
                    outcome = _model_turns(case, group, settings)
            except Exception as error:  # noqa: BLE001 - 首次失败必须保留并继续下一个试验
                outcome = {
                    "status": "failed",
                    "errors": [f"{type(error).__name__}: {error}"],
                    "cost": None,
                    "cost_status": "unknown",
                }
            record = {
                "result_id": result_id,
                "case_id": case["case_id"],
                "set": set_name,
                "repeat": repeat,
                "input_valid": validate_case(case)["status"] != "invalid",
                "execution_status": outcome.get("status"),
                "scorable": bool(outcome.get("steps")) or outcome.get("status") == "executed",
                "common_result": _common_result(outcome),
                **outcome,
            }
            records.append(record)
            mapping[result_id] = {
                "group": group,
                "case_id": case["case_id"],
                "set": set_name,
                "repeat": repeat,
            }
            # Append-only checkpoint: a crash preserves every completed first attempt.
            with checkpoint.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            with mapping_checkpoint.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"result_id": result_id, **mapping[result_id]}, ensure_ascii=False)
                    + "\n"
                )
    result_path.write_text(
        json.dumps(
            {"seal": manifest["code_commit"], "order_seed": order_seed, "records": records},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    mapping_path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    integrity = {
        "first_attempts_sha256": _sha256(result_path),
        "private_group_map_sha256": _sha256(mapping_path),
        "code_commit": manifest["code_commit"],
    }
    (directory / "run_integrity.json").write_text(
        json.dumps(integrity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "trials": len(records),
        "main": len(main) * 3,
        "boundary": len(boundary) * 3,
        "first_attempts_sha256": integrity["first_attempts_sha256"],
    }


def _verify_run_integrity(directory: Path) -> None:
    integrity = json.loads((directory / "run_integrity.json").read_text(encoding="utf-8"))
    if (
        _sha256(directory / "first_attempts.json") != integrity["first_attempts_sha256"]
        or _sha256(directory / "private_group_map.json") != integrity["private_group_map_sha256"]
    ):
        raise ValueError("首次结果或组别映射已改变；拒绝评分")


def score_seal(directory: Path, *, semantic_reviews: Path | None = None) -> dict[str, Any]:
    manifest = verify_seal(directory)
    target = directory / "first_scores.json"
    if target.exists():
        raise FileExistsError("首次评分已存在；勘误须另建版本并保留原分")
    _verify_run_integrity(directory)
    raw = json.loads((directory / "first_attempts.json").read_text(encoding="utf-8"))
    mapping = json.loads((directory / "private_group_map.json").read_text(encoding="utf-8"))
    cases = {
        case["case_id"]: case
        for name in ("main_cases", "boundary_cases")
        for case in _read_jsonl(directory / FILES[name])
    }
    contracts = {
        row["case_id"]: row
        for name in ("main_constraints", "boundary_constraints")
        for row in _read_jsonl(directory / FILES[name])
    }
    review_path = semantic_reviews or directory / "semantic_reviews.json"
    reviews = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    rows: list[dict[str, Any]] = []
    for record in raw["records"]:
        result_id = record["result_id"]
        group = {"A": "g1", "B": "g2", "C": "g3"}[mapping[result_id]["group"]]
        scored = score_case(
            cases[record["case_id"]],
            contracts[record["case_id"]],
            {**record, "group_code": group},
            semantic=reviews.get(result_id),
        )
        rows.append(
            {**scored, "group_code": group, "set": record["set"], "repeat": record["repeat"]}
        )
    main = [row for row in rows if row["set"] == "main"]
    boundary = [row for row in rows if row["set"] == "boundary"]
    boundary_all = sum(
        all(row["verdict"] == "passed" for row in boundary if row["case_id"] == case_id)
        for case_id in {row["case_id"] for row in boundary}
    )
    c_rows = [row for row in rows if row["group_code"] == "g3"]
    c_failures = any(row["verdict"] == "failed" for row in c_rows)
    c_pending = any(row["verdict"] == "insufficient_evidence" for row in c_rows)
    cited = sum(row["citation_valid"] for row in c_rows)
    required = sum(row["citation_required"] for row in c_rows)
    supported = sum(row["semantic_supported"] for row in c_rows)
    reviewed = sum(row["semantic_total"] for row in c_rows)
    serious = sum(row["serious_unsupported"] for row in c_rows)
    if manifest["classification"] == "public_development_only":
        conclusion = "development_only"
    elif (
        c_failures
        or serious
        or (required and cited < required)
        or (reviewed and supported / reviewed < 0.95)
    ):
        conclusion = "failed"
    elif len(main) != 90 or len(boundary) != 18 or c_pending or reviewed == 0:
        conclusion = "insufficient_evidence"
    else:
        conclusion = "passed"
    report = {
        "seal_version": manifest["version"],
        "classification": manifest["classification"],
        "semantic_review": {
            "status": "provided" if reviews else "not_run",
            "sha256": _sha256(review_path) if review_path.exists() else None,
            "reviewer_independence": "same_provider_ai_assistance_not_expert_review"
            if reviews
            and any(row.get("status") == "reviewed" for row in reviews.values())
            and all(
                row.get("reviewer_independence") == "same_provider_ai_assistance_not_expert_review"
                for row in reviews.values()
                if row.get("status") == "reviewed"
            )
            else "unverified_or_not_run",
        },
        "c_auto_acceptance": {
            "conclusion": conclusion,
            "citation_valid": cited,
            "citation_required": required,
            "semantic_supported": supported,
            "semantic_reviewed": reviewed,
            "semantic_support_rate": supported / reviewed if reviewed else None,
            "serious_unsupported": serious,
        },
        "main": summarize_scores(main),
        "boundary": {
            "passed_trials": sum(row["verdict"] == "passed" for row in boundary),
            "trials": len(boundary),
            "all_three_passed_cases": boundary_all,
            "cases": len({row["case_id"] for row in boundary}),
        },
        "scores": rows,
        "limitation": "自动阶段验收；未包含双人人工独立评分或田间有效性证明。",
    }
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def prepare_review(directory: Path) -> Path:
    verify_seal(directory)
    _verify_run_integrity(directory)
    target = directory / "semantic_blind.jsonl"
    raw = json.loads((directory / "first_attempts.json").read_text(encoding="utf-8"))
    cases = {
        case["case_id"]: case
        for name in ("main_cases", "boundary_cases")
        for case in _read_jsonl(directory / FILES[name])
    }
    write_blind_review_packets(raw["records"], cases, target)
    return target


def judge_seal(directory: Path, *, settings: Settings) -> dict[str, Any]:
    manifest = verify_seal(directory)
    _verify_run_integrity(directory)
    config = manifest["model_configuration"]
    if (
        settings.llm_model != config["model"]
        or settings.llm_base_url.rstrip("/") != config["base_url"]
    ):
        raise ValueError("语义复核模型或地址与封存配置不一致")
    if not settings.llm_api_key.get_secret_value().strip():
        raise ValueError("缺少本地模型密钥；未执行语义辅助复核")
    blind = directory / "semantic_blind.jsonl"
    if not blind.exists():
        prepare_review(directory)
    target = directory / "semantic_reviews.json"
    checkpoint = directory / "semantic_reviews.partial.jsonl"
    if target.exists() or checkpoint.exists():
        raise FileExistsError("首次语义复核已存在；不能覆盖")
    reviews: dict[str, dict[str, Any]] = {}
    for packet in _read_jsonl(blind):
        outcome = judge_packet(packet, settings=settings)
        reviews[packet["result_id"]] = outcome
        with checkpoint.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"result_id": packet["result_id"], **outcome}, ensure_ascii=False) + "\n"
            )
    target.write_text(json.dumps(reviews, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "reviewed": sum(row["status"] == "reviewed" for row in reviews.values()),
        "total": len(reviews),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sealing = commands.add_parser("seal")
    sealing.add_argument("--main-cases", required=True, type=Path)
    sealing.add_argument("--main-constraints", required=True, type=Path)
    sealing.add_argument("--boundary-cases", required=True, type=Path)
    sealing.add_argument("--boundary-constraints", required=True, type=Path)
    sealing.add_argument("--output-dir", required=True, type=Path)
    sealing.add_argument("--development", action="store_true")
    sealing.add_argument("--env-file", type=Path)
    sealing.add_argument("--exposure-log", type=Path, help="仓库外 JSON 字符串数组；正式封存必填")
    running = commands.add_parser("run")
    running.add_argument("--seal", required=True, type=Path)
    running.add_argument("--env-file", type=Path)
    scoring = commands.add_parser("score")
    scoring.add_argument("--seal", required=True, type=Path)
    scoring.add_argument("--semantic-reviews", type=Path)
    preparing = commands.add_parser("prepare-review")
    preparing.add_argument("--seal", required=True, type=Path)
    judging = commands.add_parser("judge")
    judging.add_argument("--seal", required=True, type=Path)
    judging.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.command == "seal":
        sources = {key: getattr(args, key) for key in FILES}
        settings = Settings(_env_file=args.env_file) if args.env_file else Settings()
        known_exposure = None
        if args.exposure_log:
            known_exposure = json.loads(args.exposure_log.read_text(encoding="utf-8"))
            if not isinstance(known_exposure, list) or not all(
                isinstance(item, str) for item in known_exposure
            ):
                raise SystemExit("暴露记录须为 JSON 字符串数组")
        result = seal(
            sources,
            args.output_dir,
            development=args.development,
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            known_exposure=known_exposure,
        )
        print(
            json.dumps(
                {
                    "version": result["version"],
                    "classification": result["classification"],
                    "counts": {key: value["count"] for key, value in result["inputs"].items()},
                },
                ensure_ascii=False,
            )
        )
    elif args.command == "run":
        print(
            json.dumps(
                run_seal(
                    args.seal,
                    settings=Settings(_env_file=args.env_file) if args.env_file else Settings(),
                ),
                ensure_ascii=False,
            )
        )
    elif args.command == "prepare-review":
        print(prepare_review(args.seal))
    elif args.command == "judge":
        settings = Settings(_env_file=args.env_file) if args.env_file else Settings()
        print(json.dumps(judge_seal(args.seal, settings=settings), ensure_ascii=False))
    else:
        report = score_seal(args.seal, semantic_reviews=args.semantic_reviews)
        print(
            json.dumps({"main": report["main"], "boundary": report["boundary"]}, ensure_ascii=False)
        )


if __name__ == "__main__":
    main()
