"""D02/D03 统一三组运行器：A 直接生成 / B 加检索 / C 完整系统。

支持两种案例格式：

- 开发样例（第一步联调样例）：draft + feedback_events，C 组直接执行；
- 正式候选（第二步执行输入包）：draft + interaction_script + weather_fixture，按脚本逐步执行。

安全约定：

- 正式候选默认只做静态校验（validate），不运行、不产生计划输出；
- 执行模式需要显式 --mode execute，正式候选报告只能写到仓库外；
- A/B 组必须真实调用语言模型；离线运行不生成对照结果，记为“待模型授权”，避免伪造评测数据。

记录输入快照、任务与状态、错误、耗时与成本；评分阶段按 g1/g2/g3 隐藏组名。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import platform
import subprocess
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v2 import LOCAL_ZONE, FeedbackV2, TimeWindow
from nongshi.schema_v3 import (
    FactEventInputV3,
    FieldDraftV3,
    WeatherDayV3,
    WeatherLocationV3,
    WeatherSnapshotV3,
)
from nongshi.service_v3 import WorkbenchServiceV3

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = (
    PROJECT_ROOT
    / "任务落实_20261020"
    / "李思默"
    / "第一步_需求与执行准备"
    / "联调与开发回归"
    / "不计分联调样例_3例.jsonl"
)

RUNNER_VERSION = "1.1"
GROUP_KEY = {
    "g1": "A 直接生成（需真实语言模型）",
    "g2": "B 加检索（需真实语言模型）",
    "g3": "C 完整系统（本地确定性，离线可运行）",
}
OUTPUT_DIR = PROJECT_ROOT / "reports"

SCRIPT_OPERATIONS = {
    "save_draft",
    "evaluate",
    "preview",
    "adopt",
    "cancel_preview",
    "commit_feedback",
    "read_history",
    "request",
    "chat",
}
# 系统当前没有“关联已完成任务的新建维护请求”入口，按能力缺口记录，不伪造任务。
UNSUPPORTED_OPERATIONS = {
    "request": "系统尚无“关联已完成任务的新建维护请求”入口；记录能力缺口，不伪造新任务",
}
MODEL_OPERATIONS = {"chat"}
EXPECTED_WEATHER_STATUSES = {
    "success",
    "location_unselected",
    "http_error",
    "timeout",
    "missing_dates",
}
# 夹具是固定预报快照；为避免第二天再取时被判过期，按长期有效注入（仅用于对照）。
WEATHER_FIXTURE_TTL = timedelta(days=30)


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _code_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _parse_moment(value: object) -> datetime:
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_ZONE)
    return moment.astimezone(UTC)


def _plan_rows(items: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "task_id": item.task_id,
            "task_type": item.task_type,
            "applicability": item.applicability,
            "necessity": item.necessity,
            "schedule_status": item.schedule_status,
            "start_at": item.start_at.isoformat() if item.start_at else None,
            "end_at": item.end_at.isoformat() if item.end_at else None,
            "missing_fields": list(item.missing_fields),
            "current_evidence_rule_ids": sorted(
                {e.rule_id for e in item.evidence if "暂缓" not in e.status_label}
            ),
            "deferred_reference_rule_ids": sorted(
                {e.rule_id for e in item.evidence if "暂缓" in e.status_label}
            ),
            "reason_code": item.reason_code,
            "reason": item.reason,
        }
        for item in items
    ]


class _Clock:
    """按案例脚本推进的固定时钟；服务层通过 today/now 注入。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment.astimezone(UTC)

    def set(self, moment: datetime) -> None:
        self._moment = moment.astimezone(UTC)

    def now(self) -> datetime:
        return self._moment

    def today(self) -> date:
        return self._moment.astimezone(LOCAL_ZONE).date()


def _weather_location(raw: object) -> WeatherLocationV3 | None:
    if not isinstance(raw, dict) or raw.get("latitude") is None or raw.get("longitude") is None:
        return None
    resolution = raw.get("resolution")
    if resolution not in {"township", "county", "city", "unknown"}:
        resolution = "unknown"
    return WeatherLocationV3(
        query=str(raw.get("query") or ""),
        display_name=str(raw.get("display_name") or raw.get("query") or ""),
        latitude=float(raw["latitude"]),
        longitude=float(raw["longitude"]),
        timezone=str(raw.get("timezone") or "Asia/Shanghai"),
        resolution=resolution,
        administrative_area=[str(item) for item in raw.get("administrative_area") or []],
        geocoding_source=str(raw.get("geocoding_source") or "synthetic_geocoding_fixture"),
    )


def _weather_snapshot(case: dict[str, Any]) -> WeatherSnapshotV3 | None:
    """把案例天气夹具转换为快照；状态非 success 或缺日期时返回 None（按预期失败处理）。"""
    fixture = case.get("weather_fixture") or {}
    if fixture.get("status") != "success":
        return None
    location = _weather_location(fixture.get("location"))
    if location is None:
        return None
    rows = fixture.get("daily") or []
    days: list[WeatherDayV3] = []
    for row in rows:
        try:
            days.append(
                WeatherDayV3(
                    day=date.fromisoformat(str(row["date"])),
                    precipitation_sum=row.get("precipitation"),
                    temperature_max=row.get("temperature_max"),
                    temperature_min=row.get("temperature_min"),
                    wind_speed_max=row.get("wind_speed"),
                )
            )
        except (KeyError, TypeError, ValueError):
            return None
    if len(days) != 7:
        return None
    expected_days = [days[0].day + timedelta(days=index) for index in range(7)]
    if [row.day for row in days] != expected_days:
        return None
    fetched_at = _parse_moment(fixture.get("fetched_at") or case.get("received_at"))
    return WeatherSnapshotV3(
        field_id=case["draft"]["field_id"],
        source=str(fixture.get("source") or "synthetic_weather_fixture"),
        location=location,
        requested_latitude=location.latitude,
        requested_longitude=location.longitude,
        grid_latitude=location.latitude,
        grid_longitude=location.longitude,
        timezone="Asia/Shanghai",
        fetched_at=fetched_at,
        coverage_start=days[0].day,
        coverage_end=days[-1].day,
        units={str(key): str(value) for key, value in (fixture.get("units") or {}).items()},
        days=days,
        cache_expires_at=fetched_at + WEATHER_FIXTURE_TTL,
    )


def validate_case(case: dict[str, Any]) -> dict[str, Any]:
    """静态校验一例输入：字段可解析、七天连续、天气覆盖、脚本操作可识别。"""
    case_id = str(case.get("case_id") or "<unknown>")
    scripted = "interaction_script" in case
    problems: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    draft_raw = case.get("draft") or {}
    try:
        FieldDraftV3.model_validate(copy.deepcopy(draft_raw))
    except Exception as error:  # noqa: BLE001 - 校验问题需逐条记录
        detail = " ".join(str(error).split())
        problems.append({"code": "draft_invalid", "detail": detail[:300]})

    day_values: list[date] = []
    for row in draft_raw.get("days") or []:
        try:
            day_values.append(date.fromisoformat(str(row["day"])))
        except (KeyError, TypeError, ValueError):
            problems.append({"code": "day_unreadable", "detail": str(row)[:120]})
    if len(day_values) != 7:
        problems.append({"code": "day_count", "detail": f"七天窗口实际 {len(day_values)} 天"})
    elif any(
        right - left != timedelta(days=1)
        for left, right in zip(day_values, day_values[1:], strict=False)
    ):
        problems.append({"code": "day_not_consecutive", "detail": "七天窗口不连续"})
    if day_values and draft_raw.get("start_date") != day_values[0].isoformat():
        problems.append({"code": "start_date_mismatch", "detail": "start_date 与首日不一致"})

    fixture = case.get("weather_fixture") or {}
    weather_status = str(fixture.get("status") or "missing")
    if weather_status not in EXPECTED_WEATHER_STATUSES:
        problems.append({"code": "weather_status", "detail": f"未知天气夹具状态 {weather_status}"})
    if weather_status == "success":
        coverage = [str(item) for item in fixture.get("coverage_dates") or []]
        if coverage != [day.isoformat() for day in day_values]:
            problems.append({"code": "weather_coverage", "detail": "天气覆盖日期与七天窗口不一致"})
        if _weather_snapshot(case) is None:
            problems.append({"code": "weather_unusable", "detail": "天气夹具无法转换为快照"})

    operations: list[str] = []
    if scripted:
        steps = case.get("interaction_script") or []
        operations = [str(step.get("operation")) for step in steps]
        unknown = sorted({code for code in operations if code not in SCRIPT_OPERATIONS})
        if unknown:
            problems.append({"code": "unknown_operation", "detail": "、".join(unknown)})
        numbers = [step.get("step") for step in steps]
        if numbers != sorted(numbers):
            problems.append({"code": "step_order", "detail": "脚本步骤未按 step 升序"})
        for code, reason in UNSUPPORTED_OPERATIONS.items():
            if code in operations:
                warnings.append({"code": f"unsupported:{code}", "detail": reason})
        if any(code in MODEL_OPERATIONS for code in operations):
            warnings.append(
                {"code": "requires_model", "detail": "脚本含 chat 步骤，C 组离线只能记为待模型授权"}
            )

    status = "invalid" if problems else ("warning" if warnings else "ok")
    return {
        "case_id": case_id,
        "format": "scripted" if scripted else "legacy",
        "status": status,
        "operations": operations,
        "problems": problems,
        "warnings": warnings,
    }


def validate_cases(paths: list[Path]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for path in paths:
        cases = _load_cases(path)
        results.extend(validate_case(case) for case in cases)
        files.append(
            {
                "path": str(path),
                "cases": len(cases),
                "sha256": _file_sha256(path),
            }
        )
    summary = {
        "cases": len(results),
        "ok": sum(1 for row in results if row["status"] == "ok"),
        "warning": sum(1 for row in results if row["status"] == "warning"),
        "invalid": sum(1 for row in results if row["status"] == "invalid"),
    }
    return {
        "runner_version": RUNNER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "validate_only_no_case_executed",
        "files": files,
        "summary": summary,
        "cases": results,
    }


class _ScriptSession:
    """按 interaction_script 驱动本地确定性全流程；每步使用脚本固定时间。"""

    def __init__(
        self,
        case: dict[str, Any],
        *,
        database: Path,
        enable_model: bool = False,
    ) -> None:
        self.case = case
        self.case_id = str(case.get("case_id") or "case")
        self.field_id = str(case["draft"]["field_id"])
        self.clock = _Clock(_parse_moment(case["received_at"]))
        self.store = WorkbenchStoreV3(database)
        self.service = WorkbenchServiceV3(
            self.store,
            today=self.clock.today,
            now=self.clock.now,
        )
        policy = case.get("stage_execution_policy") or {}
        self.declared_task_ids = {
            str(key): str(value) for key, value in (policy.get("task_ids") or {}).items()
        }
        self.actual_task_ids: dict[str, str] = {}
        self.bindings: dict[str, str] = {}
        self.notes: list[str] = []
        self.weather_status = str((case.get("weather_fixture") or {}).get("status") or "missing")
        self.weather_injected = False
        self._enable_model = enable_model
        self._agent = None

    def _operation_id(self, step: dict[str, Any], suffix: str = "") -> str:
        return f"{self.case_id}-s{step.get('step')}{suffix}"

    def _map_task_id(self, value: object) -> object:
        for kind, declared in self.declared_task_ids.items():
            if value == declared:
                return self.actual_task_ids.get(kind, value)
        return value

    def _remember_tasks(self, items: list[Any]) -> None:
        for item in items:
            self.actual_task_ids.setdefault(item.task_type, item.task_id)

    def _inject_weather(self) -> None:
        if self.weather_status != "success":
            self.notes.append(f"天气夹具状态 {self.weather_status}：不注入快照，按预期失败分支记录")
            return
        snapshot = _weather_snapshot(self.case)
        if snapshot is None:
            self.notes.append("天气夹具无法转换为快照：未注入")
            return
        self.store.save_weather_snapshot(snapshot)
        self.weather_injected = True

    def _op_save_draft(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        draft = FieldDraftV3.model_validate(copy.deepcopy(self.case["draft"]))
        expected: int | None = None
        try:
            expected = self.store.get_field(self.field_id).draft_version
        except KeyError:
            expected = None
        state = self.service.save_field_draft(
            draft,
            expected_version=expected,
            operation_id=self._operation_id(step),
        )
        self._inject_weather()
        return (
            "ok",
            {
                "draft_version": state.draft_version,
                "fact_version": state.fact_version,
                "field_id": state.id,
                "weather_injected": self.weather_injected,
            },
            None,
        )

    def _op_evaluate(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        # 案例 runtime_bindings 约定 initial_preview 取自 evaluate 步（检查资料并给出预览），
        # 因此本步在 evaluate 之后同时生成一版预览并登记绑定。
        evaluation = self.service.evaluate_field(self.field_id)
        preview = self.service.preview_plan(
            self.field_id,
            operation_id=self._operation_id(step, "-preview"),
            source="demo",
        )
        self._remember_tasks(list(evaluation.items))
        self.bindings.setdefault("initial_preview", preview.id)
        self.bindings[f"preview_{step.get('step')}"] = preview.id
        return (
            "ok",
            {
                "profile_ready": evaluation.profile_ready,
                "missing_fields": list(evaluation.missing_fields),
                "items": _plan_rows(list(evaluation.items)),
                "clarification_questions": list(evaluation.clarification_questions),
                "preview_id": preview.id,
                "version_id": preview.version.id,
                "weather_snapshot_id": preview.version.weather_snapshot_id,
                "preview_items": _plan_rows(list(preview.version.plan.items)),
            },
            None,
        )

    def _op_preview(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        preview = self.service.preview_plan(
            self.field_id,
            operation_id=self._operation_id(step),
            source="demo",
        )
        self.bindings.setdefault("initial_preview", preview.id)
        self.bindings[f"preview_{step.get('step')}"] = preview.id
        self._remember_tasks(list(preview.version.plan.items))
        return (
            "ok",
            {
                "preview_id": preview.id,
                "version_id": preview.version.id,
                "item_count": len(preview.version.plan.items),
                "change_count": len(preview.version.changes),
                "weather_snapshot_id": preview.version.weather_snapshot_id,
                "items": _plan_rows(list(preview.version.plan.items)),
            },
            None,
        )

    def _op_adopt(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        payload = step.get("payload") or {}
        binding = str(payload.get("plan_binding") or "initial_preview")
        preview_id = self.bindings.get(binding)
        note = None
        if preview_id is None:
            preview = self.service.preview_plan(
                self.field_id,
                operation_id=self._operation_id(step, "-implicit"),
                source="demo",
            )
            preview_id = preview.id
            self.bindings.setdefault("initial_preview", preview_id)
            self.bindings[f"preview_{step.get('step')}"] = preview_id
            note = f"脚本引用 {binding} 没有对应预览；按产品流程先生成预览再采用"
        preview = self.store.load_preview(preview_id)
        result = self.service.adopt_plan(
            preview_id,
            expected_fact_version=preview.expected_fact_version,
            expected_plan_id=preview.expected_plan_id,
            operation_id=self._operation_id(step),
        )
        self.bindings["initial_plan"] = result.version.id
        self._remember_tasks(list(result.version.plan.items))
        return (
            "ok",
            {
                "adopted_version_id": result.version.id,
                "created_new_version": result.created_new_version,
                "replayed": result.replayed,
                "item_count": len(result.version.plan.items),
            },
            note,
        )

    def _op_cancel_preview(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        payload = step.get("payload") or {}
        binding = str(payload.get("preview_binding") or "")
        preview_id = self.bindings.get(binding)
        if preview_id is None:
            return "failed", {}, f"脚本引用 {binding} 没有对应预览，无法取消"
        preview = self.service.cancel_preview(preview_id)
        return "ok", {"preview_id": preview.id, "status": preview.status}, None

    def _op_commit_feedback(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        payload = step.get("payload") or {}
        events: list[FactEventInputV3] = []
        for raw in payload.get("events") or []:
            item = copy.deepcopy(raw)
            feedback = item.get("feedback") or {}
            if feedback.get("task_id") is not None:
                feedback["task_id"] = self._map_task_id(feedback["task_id"])
            if feedback.get("plot_id") is not None:
                feedback["plot_id"] = self.field_id
            events.append(FactEventInputV3.model_validate(item))
        state = self.service.get_field(self.field_id)
        result = self.service.commit_events(
            self.field_id,
            events,
            operation_id=self._operation_id(step),
            expected_fact_version=state.fact_version,
        )
        return (
            "ok",
            {
                "fact_version": result.fact_version,
                "plan_stale": result.plan_stale,
                "replayed": result.replayed,
                "event_ids": [event.event_id for event in result.events],
            },
            None,
        )

    def _op_read_history(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        plans = self.service.plan_history(self.field_id)
        events = self.service.list_events(self.field_id)
        current = self.service.get_current_plan(self.field_id)
        return (
            "ok",
            {
                "plan_version_ids": [version.id for version in plans],
                "event_count": len(events),
                "current_plan_id": current.id if current else None,
            },
            None,
        )

    def _op_request(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        reason = UNSUPPORTED_OPERATIONS["request"]
        return "unsupported_capability", {"requested": step.get("payload") or {}}, reason

    def _op_chat(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        if not self._enable_model:
            return (
                "pending_model_authorization",
                {"user_message": step.get("user_message")},
                "chat 步骤需真实模型；离线运行不生成结果",
            )
        if self._agent is None:
            from nongshi.agent import AgentServiceV3
            from nongshi.config import Settings

            self._agent = AgentServiceV3(Settings(), self.service)
        response = self._agent.run(
            self.field_id,
            str(step.get("user_message") or ""),
            received_at=self.clock.now(),
        )
        status = "ok" if response.status == "success" else "failed"
        return (
            status,
            {
                "status": response.status,
                "text": response.text,
                "model": response.model,
                "tools": [tool.name for tool in response.tools],
            },
            None if status == "ok" else response.error_message,
        )

    def execute_step(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        operation = str(step.get("operation"))
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:
            raise ValueError(f"未实现的操作 {operation}")
        return handler(step)

    def summary(self) -> dict[str, Any]:
        state = self.service.get_field(self.field_id)
        current = self.service.get_current_plan(self.field_id)
        return {
            "draft_version": state.draft_version,
            "fact_version": state.fact_version,
            "latest_plan_id": state.latest_plan_id,
            "plan_stale": state.plan_stale,
            "current_plan_items": _plan_rows(list(current.items)) if current else [],
            "plan_history_ids": [
                version.id for version in self.service.plan_history(self.field_id)
            ],
            "event_count": len(self.service.list_events(self.field_id)),
            "task_id_map": dict(self.actual_task_ids),
            "bindings": dict(self.bindings),
            "weather_status": self.weather_status,
            "weather_injected": self.weather_injected,
        }


def run_scripted_case(
    case: dict[str, Any],
    *,
    database: Path,
    enable_model: bool = False,
) -> dict[str, Any]:
    """按 interaction_script 执行一例；失败即停并保留已完成步骤，不重试、不覆盖。"""
    errors: list[str] = []
    step_rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    session = _ScriptSession(case, database=database, enable_model=enable_model)
    steps = list(case.get("interaction_script") or [])
    failed_at: int | None = None
    for index, step in enumerate(steps):
        if failed_at is not None:
            step_rows.append(
                {
                    "step": step.get("step"),
                    "operation": step.get("operation"),
                    "at": step.get("at"),
                    "status": "skipped_after_failure",
                }
            )
            continue
        session.clock.set(_parse_moment(step["at"]))
        step_started = time.perf_counter()
        note = None
        try:
            status, output, note = session.execute_step(step)
        except Exception as error:  # noqa: BLE001 - 记录失败而非中断整批
            status = "failed"
            output = {}
            errors.append(
                f"步骤 {step.get('step')} {step.get('operation')}: {type(error).__name__}: {error}"
            )
            failed_at = index
        step_rows.append(
            {
                "step": step.get("step"),
                "operation": step.get("operation"),
                "at": step.get("at"),
                "status": status,
                "elapsed_ms": round((time.perf_counter() - step_started) * 1000, 3),
                "output": output,
                "note": note,
            }
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

    result: dict[str, Any] = {}
    try:
        result = session.summary()
    except Exception as error:  # noqa: BLE001
        errors.append(f"汇总失败: {type(error).__name__}: {error}")

    statuses = {row["status"] for row in step_rows}
    if errors or "failed" in statuses:
        status = "failed"
    elif statuses & {"unsupported_capability", "pending_model_authorization"}:
        status = "partial"
    else:
        status = "executed"
    return {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "model_calls": 0,
        "weather_http_calls": 0,
        "cost": 0,
        "errors": errors,
        "notes": list(session.notes),
        "steps": step_rows,
        "result": result,
    }


def _resolve_feedback(
    case: dict[str, Any],
    event: dict[str, Any],
    plan_items: list[Any],
) -> FeedbackV2 | None:
    """把开发样例里的反馈记录解析为 FeedbackV2；无法解析的占位引用返回 None。"""
    field_id = case["draft"]["field_id"]
    kind = event.get("kind")
    if kind == "completed":
        reference = event.get("task_ref")
        if reference == "$scheduled_maintenance":
            item = next(
                (row for row in plan_items if row.task_type == "maintenance" and row.start_at),
                None,
            )
        else:
            item = next((row for row in plan_items if row.task_id == reference), None)
        if item is None:
            return None
        when = event.get("at")
        if when == "$task_end":
            if item.end_at is None:
                return None
            sent_at = item.end_at
        else:
            sent_at = datetime.fromisoformat(str(when))
        return FeedbackV2(kind="completed", task_id=item.task_id, at=sent_at)
    if kind == "observation":
        return FeedbackV2(
            kind="observation",
            plot_id=event.get("plot_id") or field_id,
            field=event.get("field"),
            value=event.get("value"),
            at=datetime.fromisoformat(str(event["at"])),
        )
    if kind == "workability":
        return FeedbackV2(
            kind="workability",
            plot_id=event.get("plot_id") or field_id,
            day=date.fromisoformat(str(event["day"])),
            site_workable=event.get("site_workable"),
            weather_workable=event.get("weather_workable"),
            at=datetime.fromisoformat(str(event["at"])),
        )
    if kind == "resource":
        intervals = [
            TimeWindow(
                start=clock_time.fromisoformat(str(row["start"])),
                end=clock_time.fromisoformat(str(row["end"])),
            )
            for row in event.get("intervals", [])
        ]
        return FeedbackV2(
            kind="resource",
            day=date.fromisoformat(str(event["day"])),
            resource_minutes=event.get("resource_minutes"),
            intervals=intervals,
            at=datetime.fromisoformat(str(event["at"])),
        )
    return None


def run_group_c(case: dict[str, Any], *, database: Path) -> dict[str, Any]:
    """开发样例 C 组：draft → evaluate → preview → adopt → feedback_events。"""
    field_id = case["draft"]["field_id"]
    received = datetime.fromisoformat(str(case["received_at"]))
    service = WorkbenchServiceV3(
        WorkbenchStoreV3(database),
        today=lambda: received.astimezone(LOCAL_ZONE).date(),
        now=lambda: received.astimezone(UTC),
    )
    errors: list[str] = []
    notes: list[str] = []
    started = time.perf_counter()
    result: dict[str, Any] = {}
    try:
        draft = FieldDraftV3.model_validate(case["draft"])
        service.save_field_draft(
            draft,
            expected_version=None,
            operation_id=f"{case['case_id']}-save",
        )
        evaluation = service.evaluate_field(field_id)
        preview = service.preview_plan(
            field_id,
            operation_id=f"{case['case_id']}-preview",
            source="demo",
        )
        service.adopt_plan(
            preview.id,
            expected_fact_version=preview.expected_fact_version,
            expected_plan_id=preview.expected_plan_id,
            operation_id=f"{case['case_id']}-adopt",
        )
        adopted = service.get_current_plan(field_id)
        committed = 0
        for index, event in enumerate(case.get("feedback_events", [])):
            feedback = _resolve_feedback(
                case, event, list(adopted.items) if adopted is not None else []
            )
            if feedback is None:
                notes.append(f"反馈 {index}（{event.get('kind')}）无法解析，已跳过")
                continue
            state = service.get_field(field_id)
            service.commit_events(
                field_id,
                [FactEventInputV3(feedback=feedback, source="user")],
                operation_id=f"{case['case_id']}-fb-{index}",
                expected_fact_version=state.fact_version,
            )
            committed += 1
        current = service.get_current_plan(field_id)
        result = {
            "profile_ready": evaluation.profile_ready,
            "missing_fields": list(evaluation.missing_fields),
            "evaluation_items": _plan_rows(list(evaluation.items)),
            "adopted_version_id": preview.version.id,
            "current_plan_items": _plan_rows(list(current.items) if current is not None else []),
            "committed_feedback": committed,
            "feedback_notes": notes,
            "events_after": len(service.list_events(field_id)),
        }
    except Exception as error:  # noqa: BLE001 - 记录失败而非中断整批
        errors.append(f"{type(error).__name__}: {error}")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    status = "executed" if not errors else "failed"
    return {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "model_calls": 0,
        "weather_http_calls": 0,
        "cost": 0,
        "errors": errors,
        "notes": notes,
        "result": result,
    }


def _pending_group(group_code: str) -> dict[str, Any]:
    return {
        "status": "pending_model_authorization",
        "model_calls": 0,
        "weather_http_calls": 0,
        "cost": 0,
        "errors": [],
        "detail": "A/B 组必须真实调用语言模型；离线运行不生成对照结果，避免伪造评测数据。",
    }


def build_run(
    cases_path: Path,
    *,
    run_directory: Path,
    extra_paths: tuple[Path, ...] = (),
    enable_model: bool = False,
) -> dict[str, Any]:
    paths = [cases_path, *extra_paths]
    loaded = [(path, case) for path in paths for case in _load_cases(path)]
    records: list[dict[str, Any]] = []
    for path, case in loaded:
        validation = validate_case(case)
        if "interaction_script" in case:
            outcome = run_scripted_case(
                case,
                database=run_directory / f"{case['case_id']}.sqlite3",
                enable_model=enable_model,
            )
        else:
            outcome = run_group_c(case, database=run_directory / f"{case['case_id']}.sqlite3")
        records.append(
            {
                "case_id": case["case_id"],
                "group_code": "g3",
                "source": str(path),
                "validation": validation,
                **outcome,
            }
        )
        for group_code in ("g1", "g2"):
            records.append(
                {
                    "case_id": case["case_id"],
                    "group_code": group_code,
                    "source": str(path),
                    "validation": validation,
                    **_pending_group(group_code),
                }
            )
    executed = sum(1 for row in records if row["status"] == "executed")
    partial = sum(1 for row in records if row["status"] == "partial")
    failed = sum(1 for row in records if row["status"] == "failed")
    pending = sum(1 for row in records if row["status"] == "pending_model_authorization")
    return {
        "runner_version": RUNNER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "classification": (
            "D02/D03 统一运行；C 为本地确定性结果，A/B 待模型授权；"
            "不计入独立留出成绩，不代表农业适用性"
        ),
        "code_commit": _code_commit(),
        "cases_source": [
            str(path.relative_to(PROJECT_ROOT)) if path.is_relative_to(PROJECT_ROOT) else str(path)
            for path in paths
        ],
        "cases_sha256": {str(path.name): _file_sha256(path) for path in paths},
        "group_key": GROUP_KEY,
        "masking_note": "评分阶段隐藏 group_key；对外记录只用 g1/g2/g3。",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "records": records,
        "summary": {
            "cases": len(loaded),
            "executed": executed,
            "partial": partial,
            "failed": failed,
            "pending_model_authorization": pending,
        },
    }


def write_report(report: dict[str, Any], output: Path | None = None) -> Path:
    target = output or (OUTPUT_DIR / "d02_development_run.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="案例输入 JSONL 路径")
    parser.add_argument("--boundary", type=Path, default=None, help="另列边界案例 JSONL 路径")
    parser.add_argument(
        "--mode",
        choices=["auto", "validate", "execute"],
        default="auto",
        help="auto：开发样例执行，正式候选只校验",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="报告输出路径（正式候选须在仓库外）"
    )
    parser.add_argument("--enable-model", action="store_true", help="允许 chat 步骤真实调用模型")
    args = parser.parse_args()

    paths = [args.cases, *([args.boundary] if args.boundary else [])]
    scripted = any("interaction_script" in case for path in paths for case in _load_cases(path))
    mode = args.mode
    if mode == "auto":
        mode = "validate" if scripted else "execute"

    if mode == "validate":
        report = validate_cases(paths)
        print(json.dumps(report["summary"], ensure_ascii=False))
        for row in report["cases"]:
            if row["status"] != "ok":
                print(json.dumps(row, ensure_ascii=False))
        if report["summary"]["invalid"]:
            raise SystemExit(1)
        return

    if args.output is not None and scripted and args.output.resolve().is_relative_to(PROJECT_ROOT):
        raise SystemExit("正式候选运行报告不要写入公开仓库；请用 --output 指定仓库外位置。")
    with tempfile.TemporaryDirectory(prefix="nongshi_d02_") as directory:
        report = build_run(
            paths[0],
            run_directory=Path(directory),
            extra_paths=tuple(paths[1:]),
            enable_model=args.enable_model,
        )
    output = args.output
    if output is None and not scripted:
        output = OUTPUT_DIR / "d02_development_run.json"
    if output is not None:
        write_report(report, output)
        print(f"报告：{output}")
    summary = report["summary"]
    print(
        f"案例 {summary['cases']} 例；C 执行 {summary['executed']}，部分 {summary['partial']}，"
        f"失败 {summary['failed']}，A/B 待授权 {summary['pending_model_authorization']}"
    )
    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
