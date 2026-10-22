"""D02/D03 统一三组运行器：A 直接生成 / B 加检索 / C 完整系统。

支持两种案例格式：

- 开发样例（第一步联调样例）：draft + feedback_events，C 组直接执行；
- 正式候选（第二步执行输入包）：draft + interaction_script + weather_fixture，按脚本逐步执行。

安全约定：

- 正式候选默认只做静态校验（validate），不运行、不产生计划输出；
- 执行模式需要显式 --mode execute，正式候选报告只能写到仓库外；
- A/B 组仅在 --enable-model 下调用真实模型；离线模式明确记录未执行。

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
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from nongshi.config import Settings
from nongshi.d02_model_groups import run_model_group, shared_rule_query
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v2 import LOCAL_ZONE, FeedbackV2, TimeWindow
from nongshi.schema_v3 import (
    FactEventInputV3,
    FieldDraftV3,
    GeocodingResultV3,
    WeatherDayV3,
    WeatherLocationV3,
    WeatherSnapshotV3,
)
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.weather import OpenMeteoService, WeatherServiceError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = (
    PROJECT_ROOT
    / "任务落实_20261020"
    / "李思默"
    / "第一步_需求与执行准备"
    / "联调与开发回归"
    / "不计分联调样例_3例.jsonl"
)

RUNNER_VERSION = "1.2"
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
MODEL_OPERATIONS = {"chat"}
EXPECTED_WEATHER_STATUSES = {
    "success",
    "location_unselected",
    "http_error",
    "timeout",
    "missing_dates",
}
# 固定预报夹具仍按模拟时钟判断有效期，不因实际运行日期变化而延长缓存。
WEATHER_FIXTURE_TTL = timedelta(hours=6)


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


def _code_worktree_dirty() -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(completed.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return None


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
            "duration_minutes": item.duration_minutes,
            "completed": item.completed,
            "start_at": item.start_at.isoformat() if item.start_at else None,
            "end_at": item.end_at.isoformat() if item.end_at else None,
            "missing_fields": list(item.missing_fields),
            "current_evidence_rule_ids": sorted(
                {e.rule_id for e in item.evidence if e.usage_role == "limited_support"}
            ),
            "deferred_reference_rule_ids": sorted(
                {e.rule_id for e in item.evidence if e.usage_role == "historical_reference"}
            ),
            "evidence_summaries": [e.model_dump(mode="json") for e in item.evidence],
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


class _FixedWeatherService:
    """Evaluation-only weather tool: fixed fixture, no HTTP or wall-clock fallback."""

    def __init__(
        self, case: dict[str, Any], store: WorkbenchStoreV3, *, now: Callable[[], datetime]
    ) -> None:
        self.case = case
        self.store = store
        self.now = now

    def search_locations(self, query: str) -> list[GeocodingResultV3]:
        raw = (self.case.get("weather_fixture") or {}).get("location") or {}
        location = _weather_location(raw)
        if location is None or not query.strip():
            return []
        target = f"{location.query} {location.display_name}".casefold()
        if query.strip().casefold() not in target:
            return []
        return [
            GeocodingResultV3(
                id="fixed-weather-location",
                name=location.display_name,
                latitude=location.latitude,
                longitude=location.longitude,
                timezone="Asia/Shanghai",
                query=location.query,
                match_source="local_gazetteer",
                resolution=location.resolution,
            )
        ]

    @staticmethod
    def select_location(results: list[GeocodingResultV3], result_id: str) -> WeatherLocationV3:
        return OpenMeteoService.select_location(results, result_id)

    def fetch_forecast(
        self,
        field_id: str,
        location: WeatherLocationV3,
        start_date: date,
        end_date: date,
    ) -> WeatherSnapshotV3:
        fixture = self.case.get("weather_fixture") or {}
        status = fixture.get("status")
        if status != "success":
            code = {
                "timeout": "timeout",
                "http_error": "http",
                "missing_dates": "missing_dates",
            }.get(status, "invalid")
            raise WeatherServiceError(code, f"固定天气夹具状态：{status}")
        snapshot = _weather_snapshot(self.case)
        if (
            snapshot is None
            or snapshot.coverage_start != start_date
            or snapshot.coverage_end != end_date
        ):
            raise WeatherServiceError("missing_dates", "固定天气夹具没有完整覆盖七天")
        if self.now().astimezone(UTC) >= snapshot.cache_expires_at:
            raise WeatherServiceError("invalid", "固定天气快照按案例时钟已过期")
        if (
            snapshot.field_id != field_id
            or snapshot.location.latitude != location.latitude
            or snapshot.location.longitude != location.longitude
        ):
            raise WeatherServiceError("invalid", "固定天气夹具位置与当前地块不符")
        self.store.save_weather_snapshot(snapshot)
        return snapshot

    def close(self) -> None:
        return None


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
        cache_expires_at=_parse_moment(fixture["cache_expires_at"])
        if fixture.get("cache_expires_at")
        else fetched_at + WEATHER_FIXTURE_TTL,
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
    if not scripted and weather_status == "missing":
        pass  # 旧联调格式只有逐日确认，不要求独立天气夹具。
    elif weather_status not in EXPECTED_WEATHER_STATUSES:
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
        if any(code in MODEL_OPERATIONS for code in operations):
            warnings.append(
                {"code": "requires_model", "detail": "脚本含 chat 步骤，C 组离线只能记为待模型授权"}
            )
        for step in steps:
            if step.get("operation") == "request":
                demand = (step.get("payload") or {}).get("new_task_request") or {}
                if not (
                    demand.get("related_completed_task_id")
                    and demand.get("maintenance_need_confirmed") is True
                    and demand.get("maintenance_problem_confirmed") is True
                    and demand.get("maintenance_purpose") == "clear_existing_ditch"
                ):
                    warnings.append(
                        {
                            "code": "request_missing_confirmation",
                            "detail": "新需求缺少已完成任务关联或三项明确确认；执行时会拒绝",
                        }
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
        settings: Settings | None = None,
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
        self._settings = settings or Settings()
        self._agent = None
        self._active_step = 0

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
        if preview_id is None:
            return "failed", {}, f"脚本引用 {binding} 没有对应预览，未暗中补建"
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
            None,
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
        payload = (step.get("payload") or {}).get("new_task_request") or {}
        completed_id = self._map_task_id(payload.get("related_completed_task_id"))
        current = self.service.get_current_plan(self.field_id)
        completed_events = self.service.list_events(self.field_id)
        if (
            not current
            or not any(item.task_id == completed_id for item in current.items)
            or not any(
                event.feedback
                and event.feedback.kind == "completed"
                and event.feedback.task_id == completed_id
                for event in completed_events
            )
        ):
            return "failed", {}, "新需求必须关联已完成且存在的任务 ID"
        if not (
            payload.get("maintenance_need_confirmed") is True
            and payload.get("maintenance_problem_confirmed") is True
            and payload.get("maintenance_purpose") == "clear_existing_ditch"
        ):
            return "failed", {}, "缺少新需求、现场问题或清理既有沟渠目的的明确确认"
        events = [
            FactEventInputV3(
                feedback=FeedbackV2(
                    kind="observation",
                    plot_id=self.field_id,
                    field=field,
                    value=value,
                    at=self.clock.now(),
                ),
                source="user",
            )
            for field, value in (
                ("maintenance_problem_confirmed", True),
                ("maintenance_purpose", "clear_existing_ditch"),
                ("maintenance_need_confirmed", True),
            )
        ]
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
                "related_completed_task_id": completed_id,
                "event_ids": [event.event_id for event in result.events],
                "fact_version": result.fact_version,
            },
            None,
        )

    def _op_chat(self, step: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
        if not self._enable_model:
            return (
                "pending_model_authorization",
                {"user_message": step.get("user_message")},
                "chat 步骤需真实模型；离线运行不生成结果",
            )
        if self._agent is None:
            from nongshi.agent import AgentServiceV3

            self._agent = AgentServiceV3(
                self._settings,
                self.service,
                weather=_FixedWeatherService(self.case, self.store, now=self.clock.now),
                evaluation_rule_query=lambda: shared_rule_query(
                    self.case, through_step=self._active_step
                ),
            )
        self._active_step = int(step["step"])
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
                "tool_statuses": [tool.status for tool in response.tools],
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
            "events": [
                event.model_dump(mode="json") for event in self.service.list_events(self.field_id)
            ],
            "plan_history": [
                {
                    "id": version.id,
                    "created_at": version.created_at.isoformat(),
                    "items": _plan_rows(list(version.items)),
                }
                for version in self.service.plan_history(self.field_id)
            ],
            "task_id_map": dict(self.actual_task_ids),
            "bindings": dict(self.bindings),
            "weather_status": self.weather_status,
            "weather_injected": self.weather_injected,
            "agent_runs": [
                run.model_dump(mode="json") for run in self.store.list_agent_runs(self.field_id)
            ],
        }


def run_scripted_case(
    case: dict[str, Any],
    *,
    database: Path,
    enable_model: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """按 interaction_script 执行一例；失败即停并保留已完成步骤，不重试、不覆盖。"""
    errors: list[str] = []
    step_rows: list[dict[str, Any]] = []
    started = time.perf_counter()
    session = _ScriptSession(case, database=database, enable_model=enable_model, settings=settings)
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
            if status == "failed":
                errors.append(f"步骤 {step.get('step')} {step.get('operation')}: {note or '失败'}")
                failed_at = index
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
    finally:
        if session._agent is not None:
            session._agent.close()

    statuses = {row["status"] for row in step_rows}
    if errors or "failed" in statuses:
        status = "failed"
    elif statuses & {"unsupported_capability", "pending_model_authorization"}:
        status = "partial"
    else:
        status = "executed"
    agent_runs = result.get("agent_runs", [])
    return {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "model_calls": sum(run["request_count"] for run in agent_runs),
        "token_usage": {
            "prompt": sum(run["prompt_tokens"] or 0 for run in agent_runs)
            if agent_runs and all(run["prompt_tokens"] is not None for run in agent_runs)
            else None,
            "completion": sum(run["completion_tokens"] or 0 for run in agent_runs)
            if agent_runs and all(run["completion_tokens"] is not None for run in agent_runs)
            else None,
        },
        "tool_records": [metric for run in agent_runs for metric in run.get("tool_metrics", [])],
        "weather_http_calls": 0,
        "cost": None,
        "cost_status": "unknown",
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
    status = "failed" if errors else "partial" if notes else "executed"
    return {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "model_calls": 0,
        "weather_http_calls": 0,
        "token_usage": {"prompt": None, "completion": None},
        "cost": None,
        "cost_status": "unknown",
        "errors": errors,
        "notes": notes,
        "result": result,
    }


def _not_run_group() -> dict[str, Any]:
    return {
        "status": "not_run_model_disabled",
        "model_calls": 0,
        "weather_http_calls": 0,
        "cost": None,
        "cost_status": "unknown",
        "errors": [],
        "detail": "本次未启用模型，A/B 组没有执行。",
    }


def build_run(
    cases_path: Path,
    *,
    run_directory: Path,
    extra_paths: tuple[Path, ...] = (),
    enable_model: bool = False,
    settings: Settings | None = None,
    model_transport: httpx.BaseTransport | None = None,
    blind: bool = False,
) -> dict[str, Any]:
    paths = [cases_path, *extra_paths]
    loaded = [(path, case) for path in paths for case in _load_cases(path)]
    records: list[dict[str, Any]] = []
    private_group_map: dict[str, str] = {}
    for path, case in loaded:
        validation = validate_case(case)
        valid = validation["status"] != "invalid"
        if not valid:
            outcome = {
                "status": "input_invalid",
                "scorable": False,
                "model_calls": 0,
                "weather_http_calls": 0,
                "cost": None,
                "cost_status": "unknown",
                "errors": [item["detail"] for item in validation["problems"]],
            }
        elif "interaction_script" in case:
            outcome = run_scripted_case(
                case,
                database=run_directory / f"{case['case_id']}.sqlite3",
                enable_model=enable_model,
                settings=settings,
            )
        else:
            outcome = run_group_c(case, database=run_directory / f"{case['case_id']}.sqlite3")

        def append_record(
            group: str,
            result: dict[str, Any],
            *,
            case_id: str = case["case_id"],
            source: Path = path,
            checked: dict[str, Any] = validation,
        ) -> None:
            result_id = uuid4().hex
            private_group_map[result_id] = group
            record = {
                "case_id": case_id,
                "result_id": result_id,
                "source": str(source),
                "validation": checked,
                "scorable": result.get("scorable", result["status"] == "executed"),
                **result,
            }
            if not blind:
                record["group_code"] = group
            records.append(record)

        append_record("g3", outcome)
        for group, model_group in (("g1", "A"), ("g2", "B")):
            if not valid:
                model_outcome = outcome
            elif enable_model:
                model_outcome = run_model_group(
                    case,
                    model_group,
                    settings=settings,
                    transport=model_transport,
                )
            else:
                model_outcome = _not_run_group()
            append_record(group, model_outcome)
    executed = sum(1 for row in records if row["status"] == "executed")
    partial = sum(1 for row in records if row["status"] == "partial")
    failed = sum(1 for row in records if row["status"] == "failed")
    not_run = sum(1 for row in records if row["status"] == "not_run_model_disabled")
    invalid = sum(1 for row in records if row["status"] == "input_invalid")
    missing_credentials = sum(1 for row in records if row["status"] == "missing_credential")
    return {
        "runner_version": RUNNER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "classification": (
            "D02/D03 统一运行；A/B 仅在启用模型时产生真实输出；不计入独立留出成绩，不代表农业适用性"
        ),
        "code_commit": _code_commit(),
        "code_worktree_dirty": _code_worktree_dirty(),
        "cases_source": [
            str(path.relative_to(PROJECT_ROOT)) if path.is_relative_to(PROJECT_ROOT) else str(path)
            for path in paths
        ],
        "cases_sha256": {str(path.name): _file_sha256(path) for path in paths},
        **({"group_key": GROUP_KEY} if not blind else {}),
        "masking_note": "结果编号随机生成；组别映射另存，不应交给评分方。"
        if blind
        else "开发运行未盲化。",
        "_private_group_map": private_group_map,
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
            "not_run_model_disabled": not_run,
            "input_invalid": invalid,
            "missing_credentials": missing_credentials,
        },
    }


def write_report(report: dict[str, Any], output: Path | None = None) -> Path:
    target = output or (OUTPUT_DIR / "d02_development_run.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    public = {key: value for key, value in report.items() if key != "_private_group_map"}
    target.write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
    parser.add_argument("--env-file", type=Path, default=None, help="本机 .env 路径，不写入报告")
    parser.add_argument("--blind", action="store_true", help="随机结果编号，组别映射另存")
    parser.add_argument("--mapping-output", type=Path, help="盲化组别映射路径，须与结果分开保管")
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
    if scripted and args.output is None:
        raise SystemExit("脚本候选执行必须用 --output 指定仓库外的结果文件。")
    blind = args.blind or scripted
    if blind and (args.mapping_output is None or args.output is None):
        raise SystemExit("盲化运行须同时提供 --output 和 --mapping-output。")
    if args.mapping_output and args.mapping_output.resolve().is_relative_to(PROJECT_ROOT):
        raise SystemExit("组别映射必须保存在仓库外。")
    with tempfile.TemporaryDirectory(prefix="nongshi_d02_") as directory:
        report = build_run(
            paths[0],
            run_directory=Path(directory),
            extra_paths=tuple(paths[1:]),
            enable_model=args.enable_model,
            settings=Settings(_env_file=args.env_file) if args.env_file else None,
            blind=blind,
        )
    output = args.output
    if output is None and not scripted:
        output = OUTPUT_DIR / "d02_development_run.json"
    if output is not None:
        write_report(report, output)
        print(f"报告：{output}")
    if blind and args.mapping_output is not None:
        args.mapping_output.parent.mkdir(parents=True, exist_ok=True)
        args.mapping_output.write_text(
            json.dumps(report["_private_group_map"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"组别映射：{args.mapping_output}")
    summary = report["summary"]
    print(
        f"案例 {summary['cases']} 例；执行 {summary['executed']}，部分 {summary['partial']}，"
        f"失败 {summary['failed']}，输入无效 {summary['input_invalid']}，"
        f"未运行模型 {summary['not_run_model_disabled']}，"
        f"缺少密钥 {summary['missing_credentials']}"
    )
    if summary["failed"] or summary["input_invalid"] or summary["missing_credentials"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
