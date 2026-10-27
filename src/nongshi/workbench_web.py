"""HTML workbench presentation and validated action bridge for Streamlit."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from importlib.resources import files
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nongshi.agent import AgentServiceV3
from nongshi.presentation import (
    CHANGE_LABELS,
    proposal_change_rows,
    task_series_confirmation_rows,
    task_title,
)
from nongshi.schema_v2 import DayWindow, FeedbackV2, FieldProfile, TimeWindow
from nongshi.schema_v3 import (
    FactEventInputV3,
    FieldDraftV3,
    UIEditorDraftV1,
)
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.tasks_v4 import TaskSeriesV4, TaskTemplateV4
from nongshi.weather import WeatherServiceError
from nongshi.workbench_ui_protocol import UIAction, UIActionResult

ZONE = ZoneInfo("Asia/Shanghai")


class WorkbenchView(BaseModel):
    """Validated server projection rendered by the browser component."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: Literal["real", "demo"]
    today: date
    start_date: date
    date_range: str
    new_field_id: str
    field: dict[str, Any] | None = None
    fields: list[dict[str, Any]] = Field(default_factory=list)
    days: list[dict[str, Any]] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    task_series: list[dict[str, Any]] = Field(default_factory=list)
    execution_groups: list[dict[str, Any]] = Field(default_factory=list)
    group_candidates: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    plan: dict[str, Any] | None = None
    preview: dict[str, Any] | None = None
    weather: dict[str, Any] | None = None
    location: dict[str, Any] | None = None
    locations: list[dict[str, Any]] = Field(default_factory=list)
    forecast: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, str]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    editor_drafts: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    export_json: str = ""
    export_markdown: str = ""
    notice: str = ""
    action_result: dict[str, Any] | None = None
    operation: str = ""
    agent_busy: bool = False
    agent_status: str = ""


HTML = files("nongshi").joinpath("web_assets/index.html").read_text(encoding="utf-8")
CSS = files("nongshi").joinpath("web_assets/workbench.css").read_text(encoding="utf-8")
JS = files("nongshi").joinpath("web_assets/workbench.js").read_text(encoding="utf-8")
_WORKBENCH = st.components.v2.component(
    "nongshi_html_workbench", html=HTML, css=CSS, js=JS, isolate_styles=True
)


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(ZONE).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _tri(value: bool | None, yes: str, no: str) -> str:
    return yes if value is True else no if value is False else "待确认"


def _checked(value: Any) -> bool:
    return value is True or value == 1 or str(value).strip().lower() in {"true", "on", "1"}


def _template(item: Any) -> dict[str, Any]:
    return item.template_snapshot or {}


def _item_view(item: Any, *, now: datetime) -> dict[str, Any]:
    start = item.start_at.astimezone(ZONE) if item.start_at else None
    end = item.end_at.astimezone(ZONE) if item.end_at else None
    status = item.schedule_status
    return {
        "id": item.task_id,
        "field_id": item.plot_id,
        "title": item.title or task_title(item.task_type),
        "type": item.task_type,
        "type_label": task_title(item.task_type),
        "status": status,
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else "",
        "date": start.date().isoformat() if start else "",
        "date_label": start.strftime("%m月%d日 · 周") + "一二三四五六日"[start.weekday()]
        if start
        else "日期待确认",
        "time_label": f"{start:%H:%M}–{end:%H:%M}" if start and end else "",
        "duration": item.duration_minutes or 0,
        "execution_group_id": item.execution_group_id,
        "group_duration_minutes": item.group_duration_minutes,
        "purpose": str(_template(item).get("purpose") or ""),
        "reason": item.reason or "",
        "missing": item.missing_fields,
        "steps": _template(item).get("steps", []),
        "result_fields": _template(item).get("result_fields", []),
        "completion_standard": _template(item).get("completion_standard", ""),
        "related_issue_id": _template(item).get("related_issue_id") or "",
        "can_record": bool(
            status == "scheduled"
            and start is not None
            and start <= now.astimezone(ZONE)
            and not item.completed
        ),
        "completed": bool(item.completed),
        "evidence": [
            {
                "rule_id": e.rule_id,
                "source_id": e.source_id,
                "title": e.title,
                "publisher": e.publisher,
                "date": e.source_date or "日期未提供",
                "pages": ", ".join(str(p) for p in e.pdf_pages) or "页码未提供",
                "status": e.status_label,
                "applicability": e.applicability_text,
                "use": e.project_use_limit,
                "limits": e.limits,
                "quote": e.quote,
            }
            for e in item.evidence
        ],
    }


def _forecast_view(snapshot: Any) -> dict[str, Any]:
    return {
        "coverage": f"{snapshot.coverage_start:%m月%d日}—{snapshot.coverage_end:%m月%d日}",
        "days": [
            {
                "day": _iso(day.day),
                "temperature": (
                    f"{day.temperature_min:g}–{day.temperature_max:g} °C"
                    if day.temperature_min is not None and day.temperature_max is not None
                    else "气温缺失"
                ),
                "precipitation": (
                    f"降水 {day.precipitation_sum:g} mm"
                    if day.precipitation_sum is not None
                    else "降水缺失"
                ),
                "wind": (
                    f"最大风速 {day.wind_speed_max:g} km/h"
                    if day.wind_speed_max is not None
                    else "风速缺失"
                ),
            }
            for day in snapshot.days
        ],
    }


def _proposal_view(service: WorkbenchServiceV3, field_id: str) -> list[dict[str, Any]]:
    tasks = service.get_current_plan(field_id)
    task_names = {item.task_id: item.title for item in tasks.items} if tasks else {}
    rows = []
    for record in service.agent_proposal_records(field_id):
        if record.status != "pending":
            continue
        proposal = record.proposal
        if proposal.kind == "facts":
            changes = [
                __import__(
                    "nongshi.workbench", fromlist=["_proposal_event_description"]
                )._proposal_event_description(event, task_names=task_names)
                for event in proposal.events
            ]
        elif proposal.kind == "task_series" and proposal.task_series:
            changes = task_series_confirmation_rows(proposal.task_series)
        else:
            changes = proposal_change_rows(proposal.changes)
        rows.append(
            {
                "id": proposal.id,
                "rows": changes,
                "confirm_label": "保存待办并预览"
                if proposal.kind == "task_series"
                else "确认并保存",
            }
        )
    return rows


def build_workbench_view(
    service: WorkbenchServiceV3,
    mode: Literal["real", "demo"],
    field_id: str,
    state: Any | None,
    *,
    today: date,
    now: datetime,
    session: Any,
) -> WorkbenchView:
    """Build the one server-owned view used by the HTML page and exports."""
    saved = service.list_fields(mode=mode)
    fields = [
        {
            "id": item.id,
            "name": item.draft.profile.name if item.draft.profile else "未命名地块",
        }
        for item in saved
    ]
    start = (state.draft.start_date if state else None) or (today + timedelta(days=1))
    end = start + timedelta(days=6)
    evaluation = service.evaluate_field(field_id) if state else None
    adopted = service.get_current_plan(field_id)
    preview = service.latest_actionable_preview(field_id) if state else None
    # The adopted snapshot remains the execution source while a preview is pending.
    selected = adopted
    source_items = selected.items if selected else evaluation.items if evaluation else []
    items = [_item_view(item, now=now) for item in source_items]
    if state and state.draft.profile is None:
        items = []
    days = []
    draft_days = {d.day: d for d in (state.draft.days if state else [])}
    for index in range(7):
        day = start + timedelta(days=index)
        row = draft_days.get(day)
        day_items = [item for item in items if item["date"] == day.isoformat()]
        confirmation = (
            state.draft.profile.workability_by_date.get(day)
            if state and state.draft.profile
            else None
        )
        site = confirmation.site if confirmation else row.site_workable if row else None
        weather = confirmation.weather if confirmation else row.weather_workable if row else None
        if site is False or weather is False:
            workability, label = "unavailable", "已确认不可作业"
        elif site is True and weather is True:
            workability, label = "available", "现场与天气均确认可作业"
        else:
            workability, label = "pending", "现场或天气条件待确认"
        if row:
            resource_label = (
                f"{sum(i.minutes for i in row.intervals)} 分钟可用"
                if row.intervals and row.resource_minutes is not None
                else f"{row.resource_minutes} 分钟可用"
                if row.resource_minutes is not None
                else "时段待确认"
            )
        else:
            resource_label = "时段待确认"
        empty = not day_items
        days.append(
            {
                "date": day.isoformat(),
                "items": day_items,
                "workability": workability,
                "workability_label": label,
                "resource_label": resource_label,
                "empty_reason": (
                    "今天没有已安排事项。没有明确需求的任务不会自动补进计划。"
                    if day == today
                    else "没有符合条件且已安排的任务；空白日期不会自动补任务。"
                )
                if empty
                else "",
            }
        )
    profile = state.draft.profile if state else None
    field = None
    if state:
        field = {
            "id": field_id,
            "draft_version": state.draft_version,
            "fact_version": state.fact_version,
            "task_version": state.task_version,
            "plan_stale": state.plan_stale,
            "notes": state.draft.notes,
            "profile": (
                {
                    "name": profile.name,
                    "province": profile.province,
                    "province_confirmed": profile.province_confirmed,
                    "crop": profile.crop,
                    "crop_confirmed": profile.crop_confirmed,
                    "stage": profile.stage,
                    "stage_confirmed": profile.stage_confirmed,
                    "field_type": profile.field_type,
                    "field_type_label": {
                        "lowland_rice_stubble": "低洼/稻茬田",
                        "dry_hilly": "丘陵旱地",
                        "other": "其他田型",
                    }.get(profile.field_type, "田型未填写"),
                    "area_mu": profile.area_mu,
                    "inspection_requested": profile.inspection_requested,
                    "inspection_duration_minutes": profile.inspection_duration_minutes,
                    "maintenance_duration_minutes": profile.maintenance_duration_minutes,
                    "maintenance_need_confirmed": profile.maintenance_need_confirmed,
                }
                if profile
                else None
            ),
        }
    plan_view = (
        {
            "id": adopted.id,
            "version": len(service.plan_history(field_id)),
        }
        if adopted
        else None
    )
    preview_view = None
    if preview:
        change_titles = {item["id"]: item["title"] for item in items}
        change_titles.update({item.task_id: item.title for item in preview.version.items})
        preview_view = {
            "id": preview.id,
            "changes": [
                {
                    "title": change_titles.get(change.task_id, "农事任务"),
                    "kind_label": CHANGE_LABELS.get(change.kind, "调整"),
                    "old_time": (
                        f"{change.old_start_at.astimezone(ZONE):%m月%d日 %H:%M}"
                        if change.old_start_at
                        else "无旧时段"
                    ),
                    "new_time": (
                        f"{change.new_start_at.astimezone(ZONE):%m月%d日 %H:%M}"
                        if change.new_start_at
                        else "未排入"
                    ),
                    "reason": change.reason,
                }
                for change in preview.version.changes
                if change.kind != "unchanged"
            ],
        }
    weather_view = None
    location_view = None
    forecast_view = None
    if state and state.draft.location:
        location = state.draft.location
        location_view = {
            "display_name": location.display_name,
            "resolution_label": {
                "township": "乡镇级",
                "county": "县/区级",
                "city": "市级",
                "unknown": "级别未核实",
            }.get(location.resolution, "级别未核实"),
            "geocoding_source": location.geocoding_source,
            "latitude": location.latitude,
            "longitude": location.longitude,
            "coverage": f"{start:%Y-%m-%d}—{end:%Y-%m-%d}",
        }
        matched = service.latest_weather(
            field_id, location=location, start_date=start, end_date=end
        )
        if matched:
            forecast_view = _forecast_view(matched)
            weather_view = {
                "summary": f"{len(matched.days)} 天预报已保存",
                "coverage": f"{matched.coverage_start:%m月%d日}—{matched.coverage_end:%m月%d日}",
                "freshness": "过期，仅供历史参考" if matched.is_stale else "数据有效",
            }
    loc_key = f"agent_geo_candidates:{service.store.path.resolve()}:{field_id}"
    candidate_cache = session.get(loc_key, {})
    candidate_rows = list(candidate_cache.values()) if isinstance(candidate_cache, dict) else []
    locations = []
    if candidate_rows:
        results = candidate_rows[-1][1]
        locations = [
            {
                "id": item.id,
                "label": item.label,
                "resolution_label": {
                    "township": "乡镇级",
                    "county": "县/区级",
                    "city": "市级",
                    "unknown": "级别未核实",
                }.get(item.resolution, "级别未核实"),
                "source_label": "湖北离线地名库"
                if item.match_source == "local_gazetteer"
                else "在线地名服务",
                "match_reason": item.match_reason,
            }
            for item in results
        ]
    evidence_map: dict[tuple[str, str], Any] = {}
    for item in source_items:
        for evidence in item.evidence:
            evidence_map[(evidence.rule_id, evidence.source_id)] = evidence
    evidence = [
        {
            "rule_id": e.rule_id,
            "source_id": e.source_id,
            "title": e.title,
            "publisher": e.publisher,
            "date": e.source_date or "日期未提供",
            "pages": ", ".join(str(p) for p in e.pdf_pages) or "页码未提供",
            "status": e.status_label,
            "applicability": e.applicability_text,
            "use": e.project_use_limit,
            "limits": e.limits,
            "quote": e.quote,
        }
        for e in evidence_map.values()
    ]
    history_rows = []
    history = service.plan_history(field_id) if state else []
    for index, version in enumerate(history, start=1):
        changes = [c for c in version.changes if c.kind != "unchanged"]
        history_rows.append(
            {
                "version": index,
                "created_at": version.created_at.astimezone(ZONE).strftime("%Y-%m-%d %H:%M"),
                "summary": (
                    f"{len(version.items)} 项 · "
                    f"{sum(i.schedule_status == 'scheduled' for i in version.items)} 项已安排"
                ),
                "changes": "；".join(
                    f"{CHANGE_LABELS.get(c.kind, c.kind)}：{c.reason}" for c in changes[:3]
                ),
            }
        )
    recurrence_labels = {"once": "单次", "dates": "指定日期", "interval": "固定间隔"}
    task_series = []
    execution_groups = []
    group_candidates = []
    issues = []
    if state and profile:
        series_rows = service.list_task_series(field_id)
        task_series = [
            {
                "id": row.id,
                "title": row.template.title,
                "purpose": row.template.purpose,
                "completion_standard": row.template.completion_standard,
                "recurrence": row.recurrence,
                "recurrence_label": recurrence_labels[row.recurrence],
                "anchor_date": row.anchor_date.isoformat(),
                "end_date": row.end_date.isoformat() if row.end_date else "",
                "due_date": row.due_date.isoformat() if row.due_date else "",
                "selected_dates": ", ".join(day.isoformat() for day in row.selected_dates),
                "interval_days": row.interval_days or "",
                "duration_minutes": row.duration_minutes or "",
                "priority": row.priority,
                "template_version": row.template.version,
            }
            for row in series_rows
        ]
        instances = service.store.list_task_instances(field_id)
        instance_by_id = {instance.id: instance for instance in instances}
        item_by_id = {item["id"]: item for item in items}
        completed_ids = {
            task_id
            for task_id, item in item_by_id.items()
            if item["completed"] or item["status"] in {"completed", "cancelled"}
        }
        for instance in instances:
            task = instance.template
            if (
                instance.status == "superseded"
                or task.category != "onsite"
                or task.task_type not in {"inspection", "follow_up"}
                or instance.duration_minutes is None
                or instance.id in completed_ids
                or not start <= instance.occurrence_date <= end
            ):
                continue
            item = item_by_id.get(instance.id)
            if item is None or item["status"] not in {"scheduled", "pending", "unscheduled"}:
                continue
            group_candidates.append(
                {
                    "id": instance.id,
                    "title": task.title,
                    "date": instance.occurrence_date.isoformat(),
                    "date_label": instance.occurrence_date.strftime("%m月%d日"),
                    "earliest_date": instance.earliest_date.isoformat(),
                    "due_date": instance.due_date.isoformat(),
                    "duration_minutes": instance.duration_minutes,
                    "status": item["status"],
                }
            )
        group_members = {
            group_id: [instance_by_id[task_id].template.title for task_id in group.member_task_ids]
            if all(task_id in instance_by_id for task_id in group.member_task_ids)
            else []
            for group_id, group in (
                (group.id, group) for group in service.list_task_execution_groups(field_id)
            )
        }
        execution_groups = [
            {
                "id": group.id,
                "date": group.execution_date.isoformat(),
                "date_label": group.execution_date.strftime("%m月%d日"),
                "duration_minutes": group.duration_minutes,
                "member_task_ids": group.member_task_ids,
                "member_names": group_members[group.id],
                "status": group.status,
                "status_label": {
                    "confirmed": "已确认",
                    "needs_reconfirmation": "成员变化，待重新确认",
                    "cancelled": "已取消",
                }[group.status],
            }
            for group in service.list_task_execution_groups(field_id)
        ]
        issues = [
            {
                "id": issue.id,
                "title": issue.title,
                "description": issue.description,
                "status": issue.status,
                "status_label": "待解决" if issue.status == "open" else "已确认解决",
                "related_task_count": len(issue.related_task_ids),
                "repeat_of": issue.repeat_of,
            }
            for issue in service.list_field_issues(field_id)
        ]
    messages = list(session.get(f"chat:{field_id}", []))[-18:]
    status_key = f"agent_job_status:{field_id}"
    error_key = f"agent_job_error:{field_id}"
    agent_status = str(session.get(error_key) or session.get(status_key) or "")
    job = session.get("active_agent_job")
    busy = bool(job and job.field_id == field_id and not job.snapshot().done)
    if job and job.field_id == field_id and job.snapshot().done:
        snapshot = job.snapshot()
        if snapshot.response:
            messages.extend(
                [
                    {"role": "user", "content": job.user_message},
                    {
                        "role": "assistant",
                        "content": snapshot.response.text
                        or snapshot.response.error_message
                        or "本轮没有生成可展示的结果。",
                    },
                ]
            )
        elif snapshot.cancelled:
            agent_status = "本轮助手操作已取消；已保存内容仍保留，可重新发送或使用下方表单。"
            messages.extend(
                [
                    {"role": "user", "content": job.user_message},
                    {"role": "assistant", "content": agent_status},
                ]
            )
            session[error_key] = agent_status
        elif snapshot.error:
            messages.append({"role": "assistant", "content": snapshot.error})
            agent_status = f"{snapshot.error} 可重新描述后重试，或使用下方表单。"
            session[error_key] = agent_status
        elif not snapshot.response:
            agent_status = "本轮没有返回结果；可以重新发送，或使用下方表单。"
            messages.append({"role": "assistant", "content": agent_status})
            session[error_key] = agent_status
        session[f"chat:{field_id}"] = messages[-20:]
        session[f"agent_result:{field_id}"] = snapshot.response
        session.pop("active_agent_job", None)
        session.pop("active_agent_database", None)
        busy = False
    response = session.get(f"agent_result:{field_id}")
    if response:
        response_summary = (
            f"{response.request_count} 次模型请求 · "
            f"{len(response.tools)} 个工具结果 · {response.status}"
        )
        agent_status = (
            f"{response.error_message} · {response_summary}"
            if response.status != "success" and response.error_message
            else response_summary
        )
        session[status_key] = agent_status
        if response.status == "success":
            session.pop(error_key, None)
        else:
            session[error_key] = agent_status
    elif not job and agent_status.startswith("正在处理"):
        agent_status = "上一轮助手没有返回结果；可以重新发送，或使用下方表单。"
        session[error_key] = agent_status
        session[status_key] = agent_status
    draft_kinds = {}
    current_versions = (
        (state.draft_version, state.fact_version, state.task_version) if state else (0, 0, 0)
    )
    for draft in service.store.ui_editor_drafts(mode, field_id):
        draft_versions = (
            draft.base_draft_version,
            draft.base_fact_version,
            draft.base_task_version,
        )
        if draft_versions == current_versions:
            key = f"{draft.form_kind}:{draft.subject_id}" if draft.subject_id else draft.form_kind
            draft_kinds[key] = {
                "payload": draft.payload,
                "saved_at": draft.updated_at.isoformat(),
            }
    schedule = {}
    if state:
        for row in state.draft.days:
            schedule[row.day.isoformat()] = {
                "site_workable": row.site_workable,
                "weather_workable": row.weather_workable,
                "resource_minutes": row.resource_minutes,
                "intervals": ";".join(f"{w.start:%H:%M}-{w.end:%H:%M}" for w in row.intervals),
            }
        if profile:
            for day, confirmation in profile.workability_by_date.items():
                schedule.setdefault(day.isoformat(), {}).update(
                    {"site_workable": confirmation.site, "weather_workable": confirmation.weather}
                )
    date_range = f"{start:%m月%d日}—{end:%m月%d日}"
    action_result = session.pop("web_action_result", None)
    operation = "close_panel" if session.pop("web_close_panel", False) else ""
    notice = session.pop(f"web_notice:{field_id}", "")
    export_json = service.export_json(field_id) if adopted else ""
    export_markdown = service.export_markdown(field_id) if adopted else ""
    return WorkbenchView(
        mode=mode,
        today=today,
        start_date=start,
        date_range=date_range,
        new_field_id=field_id,
        field=field,
        fields=fields,
        days=days,
        items=items,
        task_series=task_series,
        execution_groups=execution_groups,
        group_candidates=group_candidates,
        issues=issues,
        plan=plan_view,
        preview=preview_view,
        weather=weather_view,
        location=location_view,
        locations=locations,
        forecast=forecast_view,
        evidence=evidence,
        proposals=_proposal_view(service, field_id) if state else [],
        messages=[
            {"role": m.get("role", "assistant"), "content": m.get("content", "")} for m in messages
        ],
        history=history_rows,
        editor_drafts=draft_kinds,
        schedule=schedule,
        export_json=export_json,
        export_markdown=export_markdown,
        notice=notice,
        action_result=action_result,
        operation=operation,
        agent_busy=busy,
        agent_status=agent_status,
    )


def _operation_feedback(
    session: Any,
    action: UIAction,
    success: bool,
    message: str,
    outcome: str = "saved",
    *,
    close_panel: bool = True,
) -> UIActionResult:
    result = UIActionResult(
        operation_id=action.operation_id,
        success=success,
        message=message,
        outcome=outcome if success else "error",
    )
    cache = session.setdefault("web_action_replays", {})
    cache[action.operation_id] = result.model_dump(mode="json")
    if len(cache) > 200:
        session["web_action_replays"] = dict(list(cache.items())[-150:])
    session["web_action_result"] = result.model_dump(mode="json")
    if success and close_panel and outcome not in {"draft_saved", "agent_running"}:
        session["web_close_panel"] = True
    return result


def _validate_action_versions(action: UIAction, state: Any | None) -> None:
    if state is None:
        if action.kind not in {
            "save_profile",
            "save_profile_draft",
            "select_field",
            "new_field",
            "switch_mode",
            "seed_demo",
            "search_location",
        }:
            raise ValueError("先选择或保存当前地块，再继续这个操作")
        return
    expected = (
        action.expected_draft_version,
        action.expected_fact_version,
        action.expected_task_version,
    )
    actual = (state.draft_version, state.fact_version, state.task_version)
    if any(
        value is not None and value != found for value, found in zip(expected, actual, strict=True)
    ):
        raise ValueError("地块信息已更新，请刷新后核对当前内容再提交")


def _parse_intervals(text: str) -> list[TimeWindow]:
    from nongshi.workbench import _parse_intervals as parse

    return parse(text)


def _save_editor_draft(
    service: WorkbenchServiceV3,
    action: UIAction,
    state: Any | None,
    mode: Literal["real", "demo"],
) -> str:
    payload = dict(action.payload)
    form_kind = payload.pop("form_kind", "profile")
    subject_key = {
        "result": "task_id",
        "series": "series_id",
        "outing": "group_id",
        "follow_up": "issue_id",
    }.get(form_kind, "")
    subject_id = str(payload.get(subject_key, "")) if subject_key else ""
    draft = UIEditorDraftV1(
        mode=mode,
        field_id=action.field_id,
        form_kind=form_kind,
        subject_id=subject_id,
        base_draft_version=state.draft_version if state else 0,
        base_fact_version=state.fact_version if state else 0,
        base_task_version=state.task_version if state else 0,
        payload=payload,
        updated_at=datetime.now().astimezone(ZONE),
    )
    service.save_ui_editor_draft(draft, operation_id=action.operation_id)
    return "草稿已保存在本地数据库，可刷新后继续填写。"


def dispatch_ui_action(
    action_value: dict[str, Any] | UIAction,
    *,
    service: WorkbenchServiceV3,
    agent: AgentServiceV3,
    mode: Literal["real", "demo"],
    database: Any,
    current_field_id: str,
    current_state: Any | None,
    session: Any,
) -> UIActionResult:
    """Validate an HTML event and apply it through the existing business service."""
    action = UIAction.model_validate(action_value)
    cached = session.get("web_action_replays", {}).get(action.operation_id)
    if cached:
        replay = UIActionResult.model_validate(cached)
        session["web_action_result"] = replay.model_dump(mode="json")
        return replay
    if action.kind not in {"select_field", "new_field", "switch_mode", "seed_demo"}:
        if action.field_id != current_field_id:
            raise ValueError("该操作引用了其他地块，请在当前地块重新操作")
        _validate_action_versions(action, current_state)
    from nongshi import workbench as legacy

    try:
        payload = action.payload
        if action.kind == "select_field":
            field_id = str(payload.get("selected_field_id", ""))
            valid_ids = {item.id for item in service.list_fields(mode=mode)}
            if field_id not in valid_ids:
                raise ValueError("找不到所选地块")
            session[f"pending_field_selection:{mode}"] = field_id
            return _operation_feedback(session, action, True, "已切换地块。", "saved")
        if action.kind == "new_field":
            session[f"previous_field:{mode}"] = session.get(f"field_selection:{mode}")
            field_id = f"field-{uuid4().hex[:10]}"
            session[f"new_field_id:{mode}"] = field_id
            session[f"pending_field_selection:{mode}"] = legacy.NEW_FIELD
            return _operation_feedback(
                session, action, True, "已新建空白地块，请填写地块名称。", "saved"
            )
        if action.kind == "switch_mode":
            target = payload.get("mode")
            if target not in {"real", "demo"}:
                raise ValueError("无效的数据空间")
            session["workspace_mode"] = "真实地块" if target == "real" else "独立模拟示例"
            st.query_params["mode"] = target
            return _operation_feedback(session, action, True, "已切换数据空间。", "saved")
        if action.kind == "seed_demo":
            scenario = payload.get("scenario", "sparse")
            if scenario not in {"rich", "sparse"}:
                raise ValueError("无效的模拟示例类型")
            field_id = legacy._seed_demo(service, scenario=scenario)
            session[f"pending_field_selection:{mode}"] = field_id
            return _operation_feedback(
                session, action, True, "模拟示例已在独立数据空间创建。", "saved"
            )
        if action.kind == "save_profile_draft":
            return _operation_feedback(
                session,
                action,
                True,
                _save_editor_draft(service, action, current_state, mode),
                "draft_saved",
            )
        if action.kind == "save_profile":
            name = str(payload.get("name", "")).strip()
            if not name:
                raise ValueError("请填写地块名称")
            previous = current_state.draft.profile if current_state else None
            crop = payload.get("crop") or None
            stage = payload.get("stage") or None
            province = str(payload.get("province", "")).strip() or None
            province_confirmed = _checked(payload.get("province_confirmed"))
            crop_confirmed = _checked(payload.get("crop_confirmed"))
            stage_confirmed = _checked(payload.get("stage_confirmed"))
            if province_confirmed and not province:
                raise ValueError("确认省份前请先填写省份")
            if crop_confirmed and not crop:
                raise ValueError("确认作物前请先选择作物")
            if stage_confirmed and not stage:
                raise ValueError("确认生育期前请先选择生育期")
            field_type = payload.get("field_type") or None
            area = float(payload["area_mu"]) if payload.get("area_mu") else None
            inspection = payload.get("inspection_requested")
            inspection_duration = (
                int(payload["inspection_duration_minutes"])
                if payload.get("inspection_duration_minutes")
                else None
            )
            maintenance_duration = (
                int(payload["maintenance_duration_minutes"])
                if payload.get("maintenance_duration_minutes")
                else None
            )
            if any(
                value is not None and not 1 <= value <= 1440
                for value in (inspection_duration, maintenance_duration)
            ):
                raise ValueError("任务时长需填写 1—1440 分钟，未知时留空")
            profile_values = {
                "id": action.field_id,
                "name": name,
                "province": province,
                "province_confirmed": province_confirmed,
                "crop": crop,
                "crop_confirmed": crop_confirmed,
                "stage": stage,
                "stage_confirmed": stage_confirmed,
                "field_type": field_type,
                "area_mu": area,
                "inspection_requested": (
                    True if inspection == "true" else False if inspection == "false" else None
                ),
                "inspection_duration_minutes": inspection_duration,
                "maintenance_duration_minutes": maintenance_duration,
            }
            if previous:
                profile = previous.model_copy(update=profile_values)
            else:
                profile = FieldProfile(**profile_values)
            draft = (
                current_state.draft.model_copy(
                    update={
                        "profile": profile,
                        "start_date": date.fromisoformat(payload["start_date"])
                        if payload.get("start_date")
                        else current_state.draft.start_date,
                        "notes": str(
                            payload.get("notes", current_state.draft.notes if current_state else "")
                        ),
                    }
                )
                if current_state
                else FieldDraftV3(
                    field_id=action.field_id,
                    mode=mode,
                    profile=profile,
                    start_date=date.fromisoformat(payload["start_date"])
                    if payload.get("start_date")
                    else None,
                    notes=str(payload.get("notes", "")),
                )
            )
            saved = service.save_field_draft(
                draft,
                expected_version=current_state.draft_version if current_state else None,
                operation_id=action.operation_id,
            )
            session[f"field_selection:{mode}"] = saved.id
            return _operation_feedback(session, action, True, "地块资料已保存。", "saved")
        if action.kind == "save_schedule":
            if current_state is None or current_state.draft.profile is None:
                raise ValueError("先保存地块档案，再录入作业时间")
            start = current_state.draft.start_date or (legacy._today() + timedelta(days=1))
            rows = {str(r["date"]): r for r in payload.get("rows", [])}
            days = []
            for offset in range(7):
                day = start + timedelta(days=offset)
                row = rows.get(day.isoformat(), {})
                minutes = (
                    int(row["resource_minutes"])
                    if str(row.get("resource_minutes", "")).strip()
                    else None
                )
                intervals = _parse_intervals(str(row.get("intervals", "")))
                if intervals and minutes is None:
                    raise ValueError(f"{day:%m月%d日} 已填写具体时段，请同时填写可用分钟数")
                days.append(
                    DayWindow(
                        day=day,
                        site_workable=True
                        if row.get("site_workable") == "true"
                        else False
                        if row.get("site_workable") == "false"
                        else None,
                        weather_workable=True
                        if row.get("weather_workable") == "true"
                        else False
                        if row.get("weather_workable") == "false"
                        else None,
                        resource_minutes=minutes,
                        intervals=intervals,
                    )
                )
            draft = current_state.draft.model_copy(update={"start_date": start, "days": days})
            service.save_field_draft(
                draft,
                expected_version=current_state.draft_version,
                operation_id=action.operation_id,
            )
            try:
                service.preview_plan(action.field_id, operation_id=f"{action.operation_id}-preview")
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"七天条件和人员时间已保存，但安排待更新：{error}。可重新计算。",
                    "saved",
                )
            return _operation_feedback(
                session,
                action,
                True,
                "七天作业条件和可用时间已保存，调整预览已生成。",
                "preview_ready",
            )
        if action.kind == "preview_plan":
            preview = service.preview_plan(action.field_id, operation_id=action.operation_id)
            session[f"current_preview:{action.field_id}"] = preview.id
            return _operation_feedback(
                session, action, True, "七天安排预览已生成，请核对后采用。", "preview_ready"
            )
        if action.kind == "adopt_plan":
            preview_id = str(payload.get("preview_id", ""))
            active = service.latest_actionable_preview(action.field_id)
            if active is None or active.id != preview_id:
                raise ValueError("这版预览已过期，请重新计算")
            adopted = service.adopt_plan(
                preview_id,
                expected_fact_version=action.expected_fact_version or 0,
                expected_plan_id=action.expected_plan_id,
                operation_id=action.operation_id,
            )
            return _operation_feedback(
                session,
                action,
                True,
                "计划已采用并保存新版本。"
                if adopted.created_new_version
                else "当前情况已保存，安排没有变化。",
                "adopted" if adopted.created_new_version else "no_change",
            )
        if action.kind == "cancel_preview":
            active = service.latest_actionable_preview(action.field_id)
            if active is None or active.id != payload.get("preview_id"):
                raise ValueError("这版预览已经过期")
            service.cancel_preview(active.id)
            return _operation_feedback(
                session, action, True, "调整预览已取消，原计划保持不变。", "saved"
            )
        if action.kind in {"confirm_proposal", "ignore_proposal", "retry_preview"}:
            proposal_id = str(payload.get("proposal_id", ""))
            record = service.store.agent_proposal(proposal_id)
            if record.proposal.field_id != action.field_id:
                raise ValueError("修改提案不属于当前地块")
            if action.kind == "confirm_proposal":
                result = service.confirm_agent_proposal(
                    proposal_id, operation_id=action.operation_id
                )
                text = (
                    "修改已保存，安排预览已生成，请核对。"
                    if result.preview
                    else result.error_message or "修改已保存，安排没有变化。"
                )
                outcome = "preview_ready" if result.preview else "no_change"
            elif action.kind == "ignore_proposal":
                service.ignore_agent_proposal(proposal_id)
                text, outcome = "这条修改已放弃，事实和当前计划保持不变。", "saved"
            else:
                result = service.retry_agent_preview(proposal_id)
                text = (
                    "调整预览已重新生成。"
                    if result.preview
                    else result.error_message or "重新计算后安排没有变化。"
                )
                outcome = "preview_ready" if result.preview else "no_change"
            return _operation_feedback(session, action, True, text, outcome)
        if action.kind == "save_task_series":
            if current_state is None or current_state.draft.profile is None:
                raise ValueError("请先保存地块档案")
            anchor = date.fromisoformat(str(payload["anchor_date"]))
            due = date.fromisoformat(str(payload["due_date"]))
            title = str(payload.get("title", "")).strip()
            purpose = str(payload.get("purpose", "")).strip()
            if not title or not purpose:
                raise ValueError("待办名称和目的都需要填写")
            protected_terms = ("巡田", "巡查", "沟渠", "清沟", "排水沟", "清理沟渠", "维护")
            if any(term in title for term in protected_terms):
                raise ValueError("巡田和沟渠维护需要按现场规则确认，不能作为普通自定义待办绕过条件")
            recurrence = payload.get("recurrence", "once")
            if recurrence not in {"once", "dates", "interval"}:
                raise ValueError("请选择有效的重复方式")
            selected_dates: list[date] = []
            interval_days = None
            end_date = None
            due_date = None
            if recurrence == "once":
                if due < anchor:
                    raise ValueError("最晚完成日期不能早于开始日期")
                due_date = due
            elif recurrence == "dates":
                raw_dates = str(payload.get("selected_dates", ""))
                selected_dates = sorted(
                    {
                        date.fromisoformat(part.strip())
                        for part in raw_dates.replace("，", ",").split(",")
                        if part.strip()
                    }
                )
                if not selected_dates:
                    raise ValueError("请填写至少一个指定日期")
                if any(day < anchor for day in selected_dates):
                    raise ValueError("指定日期不能早于起始日期")
                end_date = max(selected_dates)
            else:
                if due <= anchor:
                    raise ValueError("周期结束日必须晚于起始日期")
                interval_days = int(payload.get("interval_days", 0))
                if not 1 <= interval_days <= 365:
                    raise ValueError("周期间隔需要在 1 到 365 天之间")
                end_date = due
            template = TaskTemplateV4(
                id=f"custom-{uuid4().hex[:12]}",
                title=title,
                task_type="custom",
                category="offsite" if payload.get("category") == "offsite" else "onsite",
                purpose=purpose,
                steps=[],
                result_fields=["完成情况"],
                completion_standard=str(payload.get("completion_standard", "")).strip(),
                source_kind="user_defined",
                agronomic_review_status="not_assessed",
            )
            series = TaskSeriesV4(
                id=f"series-{uuid4().hex[:12]}",
                field_id=action.field_id,
                template=template,
                recurrence=recurrence,
                anchor_date=anchor,
                end_date=end_date,
                selected_dates=selected_dates,
                interval_days=interval_days,
                duration_minutes=int(payload["duration_minutes"]),
                priority=int(payload.get("priority", 2)),
                earliest_date=anchor,
                due_date=due_date,
            )
            result = service.save_task_series(
                series,
                expected_draft_version=current_state.draft_version,
                expected_fact_version=current_state.fact_version,
                expected_task_version=current_state.task_version,
                operation_id=action.operation_id,
            )
            try:
                service.preview_plan(action.field_id, operation_id=f"{action.operation_id}-preview")
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"待办已保存，七天安排预览已生成（{len(result.created_instance_ids)} 项）。",
                    "preview_ready",
                )
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"待办已保存，但安排还没更新：{error}。可重新计算。",
                    "saved",
                )
        if action.kind == "save_follow_up":
            if current_state is None or current_state.draft.profile is None:
                raise ValueError("请先保存地块档案")
            issue_id = str(payload.get("issue_id", "")).strip()
            issue = next(
                (
                    row
                    for row in service.list_field_issues(action.field_id)
                    if row.id == issue_id and row.status == "open"
                ),
                None,
            )
            if issue is None:
                raise ValueError("这个问题已更新或已解决，请刷新后再安排复查")
            anchor = date.fromisoformat(str(payload["anchor_date"]))
            horizon_start = current_state.draft.start_date or (legacy._today() + timedelta(days=1))
            if not horizon_start <= anchor <= horizon_start + timedelta(days=6):
                raise ValueError("复查日期需在当前七天计划范围内")
            duration = int(payload.get("duration_minutes", 0))
            if not 1 <= duration <= 1440:
                raise ValueError("请填写 1—1440 分钟的复查时长")
            purpose = str(payload.get("purpose", "")).strip()
            if not purpose:
                raise ValueError("请说明本次复查要确认什么")
            series = TaskSeriesV4(
                id=f"follow-up-{uuid4().hex[:12]}",
                field_id=action.field_id,
                template=TaskTemplateV4(
                    id=f"template-follow-up-{uuid4().hex[:12]}",
                    title=str(payload.get("title") or f"复查：{issue.title}").strip(),
                    task_type="follow_up",
                    category="onsite",
                    related_issue_id=issue.id,
                    purpose=purpose,
                    trigger=issue.description,
                    steps=["按关联问题复查现场实际情况", "记录正常、仍有问题或无法判断"],
                    result_fields=["复查结果"],
                    completion_standard="完成本次复查并记录实际结果",
                    source_kind="user_defined",
                    agronomic_review_status="not_assessed",
                ),
                recurrence="once",
                anchor_date=anchor,
                duration_minutes=duration,
                earliest_date=anchor,
                due_date=anchor,
            )
            service.save_task_series(
                series,
                expected_draft_version=current_state.draft_version,
                expected_fact_version=current_state.fact_version,
                expected_task_version=current_state.task_version,
                operation_id=action.operation_id,
            )
            try:
                service.preview_plan(action.field_id, operation_id=f"{action.operation_id}-preview")
                return _operation_feedback(
                    session,
                    action,
                    True,
                    "复查任务已保存并关联现场问题，新的安排预览已生成。",
                    "preview_ready",
                )
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"复查任务已保存并关联现场问题，但安排待更新：{error}。可重新计算。",
                    "saved",
                )
        if action.kind == "update_task_series":
            series_id = str(payload.get("series_id", ""))
            current_series = next(
                (row for row in service.list_task_series(action.field_id) if row.id == series_id),
                None,
            )
            if current_series is None:
                raise ValueError("找不到要修改的周期，请刷新后重试")
            recurrence = payload.get("recurrence", current_series.recurrence)
            if recurrence not in {"once", "dates", "interval"}:
                raise ValueError("请选择有效的重复方式")
            anchor = date.fromisoformat(str(payload.get("anchor_date", current_series.anchor_date)))
            due_date = None
            end_date = None
            selected_dates: list[date] = []
            interval_days = None
            if recurrence == "once":
                due_date = date.fromisoformat(
                    str(payload.get("due_date") or payload.get("end_date") or anchor)
                )
                if due_date < anchor:
                    raise ValueError("最晚完成日期不能早于开始日期")
            elif recurrence == "dates":
                raw_dates = str(payload.get("selected_dates", ""))
                selected_dates = sorted(
                    {
                        date.fromisoformat(part.strip())
                        for part in raw_dates.replace("，", ",").replace(";", ",").split(",")
                        if part.strip()
                    }
                )
                if not selected_dates or any(day < anchor for day in selected_dates):
                    raise ValueError("请填写不早于起始日期的指定日期")
                end_date = max(selected_dates)
            else:
                raw_end = str(payload.get("end_date", current_series.end_date or ""))
                if not raw_end:
                    raise ValueError("周期任务需要明确结束日期")
                end_date = date.fromisoformat(raw_end)
                interval_days = int(payload.get("interval_days", current_series.interval_days or 0))
                if not 1 <= interval_days <= 365:
                    raise ValueError("周期间隔需要在 1 到 365 天之间")
                if end_date <= anchor:
                    raise ValueError("周期结束日必须晚于起始日期")
            old_template = current_series.template
            template = old_template.model_copy(
                update={
                    "version": old_template.version + 1,
                    "title": str(payload.get("title", old_template.title)).strip(),
                    "purpose": str(payload.get("purpose", old_template.purpose)).strip(),
                    "completion_standard": str(
                        payload.get("completion_standard", old_template.completion_standard)
                    ).strip(),
                }
            )
            updated_series = current_series.model_copy(
                update={
                    "template": template,
                    "recurrence": recurrence,
                    "anchor_date": anchor,
                    "end_date": end_date,
                    "selected_dates": selected_dates,
                    "interval_days": interval_days,
                    "duration_minutes": int(
                        payload.get("duration_minutes", current_series.duration_minutes or 0)
                    ),
                    "priority": int(payload.get("priority", current_series.priority)),
                    "earliest_date": anchor if recurrence == "once" else None,
                    "due_date": due_date,
                }
            )
            result = service.update_task_series(
                updated_series,
                expected_draft_version=current_state.draft_version,
                expected_fact_version=current_state.fact_version,
                operation_id=action.operation_id,
            )
            try:
                service.preview_plan(action.field_id, operation_id=f"{action.operation_id}-preview")
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"后续周期已更新（{len(result.updated_instance_ids)} 项调整），"
                    "安排预览已生成。",
                    "preview_ready",
                )
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"后续周期已更新，但安排待重算：{error}。可以重新计算。",
                    "saved",
                )
        if action.kind == "save_execution_group":
            member_ids = payload.get("member_task_ids")
            if not isinstance(member_ids, list) or not all(
                isinstance(task_id, str) for task_id in member_ids
            ):
                raise ValueError("请选择要合并的任务")
            group_id = str(payload.get("group_id") or f"outing-{uuid4().hex[:12]}")
            service.save_task_execution_group(
                field_id=action.field_id,
                group_id=group_id,
                member_task_ids=member_ids,
                execution_date=date.fromisoformat(str(payload["execution_date"])),
                duration_minutes=int(payload["duration_minutes"]),
                expected_task_version=current_state.task_version,
                operation_id=action.operation_id,
            )
            try:
                service.preview_plan(action.field_id, operation_id=f"{action.operation_id}-preview")
                return _operation_feedback(
                    session, action, True, "合并出行已保存，新的安排预览已生成。", "preview_ready"
                )
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"合并出行已保存，但安排待重算：{error}。可以重新计算。",
                    "saved",
                )
        if action.kind == "cancel_execution_group":
            service.cancel_task_execution_group(
                action.field_id,
                str(payload.get("group_id", "")),
                expected_task_version=current_state.task_version,
                operation_id=action.operation_id,
            )
            try:
                service.preview_plan(action.field_id, operation_id=f"{action.operation_id}-preview")
                return _operation_feedback(
                    session,
                    action,
                    True,
                    "这次合并出行已取消，独立排程预览已生成。",
                    "preview_ready",
                )
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"合并出行已取消，但安排待重算：{error}。可以重新计算。",
                    "saved",
                )
        if action.kind == "cancel_task":
            task_id = str(payload.get("task_id", ""))
            reason = str(payload.get("reason", "")).strip()
            if not reason:
                raise ValueError("请说明取消本次任务的原因")
            adopted = service.get_current_plan(action.field_id)
            target = (
                next((item for item in adopted.items if item.task_id == task_id), None)
                if adopted
                else None
            )
            if target is None:
                raise ValueError("只能取消当前已采用计划中的具体任务")
            if target.schedule_status == "completed":
                raise ValueError("已完成任务不能取消")
            if any(
                event.feedback
                and event.feedback.kind == "cancelled"
                and event.feedback.task_id == task_id
                for event in service.list_events(action.field_id)
            ):
                raise ValueError("本次任务已经取消")
            event = FactEventInputV3(
                feedback=FeedbackV2(
                    kind="cancelled",
                    task_id=task_id,
                    plot_id=action.field_id,
                    at=datetime.now().astimezone(ZONE),
                    reason=reason,
                )
            )
            service.commit_events(
                action.field_id,
                [event],
                operation_id=action.operation_id,
                expected_fact_version=current_state.fact_version,
                expected_draft_version=current_state.draft_version,
                expected_task_version=current_state.task_version,
            )
            try:
                service.preview_plan(action.field_id, operation_id=f"{action.operation_id}-preview")
                return _operation_feedback(
                    session,
                    action,
                    True,
                    "本次任务取消原因已保存，新的安排预览已生成。",
                    "preview_ready",
                )
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"取消记录已保存，但安排待更新：{error}。可以重新计算。",
                    "saved",
                )
        if action.kind == "record_result":
            values = payload.get("result_values")
            if not isinstance(values, dict) or not values:
                raise ValueError("至少记录一个现场结果")
            if payload.get("completed") and not any(
                value in {"normal", "issue", "unable"} for value in values.values()
            ):
                raise ValueError("所有项目都标记为未知或未检查，不能记录任务完成")
            adopted = service.get_current_plan(action.field_id)
            item = (
                next(
                    (item for item in adopted.items if item.task_id == payload.get("task_id")),
                    None,
                )
                if adopted
                else None
            )
            if item is None or item.schedule_status not in {"scheduled", "completed"}:
                raise ValueError("只能记录当前已采用计划中的任务")
            resolve_issue_id = str(payload.get("resolve_issue_id") or "").strip() or None
            if resolve_issue_id:
                if item.task_type != "follow_up":
                    raise ValueError("只有关联问题的复查任务可以确认问题已解决")
                related_issue_id = str((item.template_snapshot or {}).get("related_issue_id") or "")
                if resolve_issue_id != related_issue_id:
                    raise ValueError("所选问题与当前复查任务不匹配")
                if not payload.get("completed"):
                    raise ValueError("确认问题已解决前，须同时确认复查任务已完成")
                if any(value != "normal" for value in values.values()):
                    raise ValueError("复查仍有异常、未知或未检查项目时，不能确认问题已解决")
            service.record_task_result(
                action.field_id,
                str(payload.get("task_id", "")),
                values,
                notes=str(payload.get("notes", "")),
                completed=bool(payload.get("completed")),
                resolve_issue_id=resolve_issue_id,
                expected_draft_version=current_state.draft_version,
                expected_fact_version=current_state.fact_version,
                operation_id=action.operation_id,
            )
            try:
                preview = service.preview_plan(
                    action.field_id, operation_id=f"{action.operation_id}-preview"
                )
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session,
                    action,
                    True,
                    f"现场结果已保存，但安排待更新：{error}。可重新计算。",
                    "saved",
                )
            return _operation_feedback(
                session, action, True, "现场结果已保存，新的安排预览已生成。", "preview_ready"
            )
        if action.kind == "record_observation":
            field_name = str(payload.get("field", ""))
            allowed_fields = {
                "maintenance_need_confirmed",
                "maintenance_problem_confirmed",
                "maintenance_purpose",
                "moisture",
                "ditch_connectivity",
                "standing_water",
                "leaf_count",
                "actual_rain_observed",
                "winter_before_context_confirmed",
                "pest_signs",
                "missing_seedlings",
                "crowding",
            }
            if field_name not in allowed_fields:
                raise ValueError("不支持此观察项目，请刷新后重试")
            raw = payload.get("value")
            boolean_fields = {
                "standing_water",
                "maintenance_need_confirmed",
                "maintenance_problem_confirmed",
                "actual_rain_observed",
                "pest_signs",
                "missing_seedlings",
                "crowding",
            }
            value: Any = (
                (True if raw == "true" else False if raw == "false" else None)
                if field_name in boolean_fields
                else (raw or None)
            )
            event = FactEventInputV3(
                feedback=FeedbackV2(
                    kind="observation",
                    plot_id=action.field_id,
                    field=field_name,
                    value=value,
                    at=datetime.now().astimezone(ZONE),
                )
            )
            service.commit_events(
                action.field_id,
                [event],
                operation_id=action.operation_id,
                expected_fact_version=current_state.fact_version,
                expected_draft_version=current_state.draft_version,
                expected_task_version=current_state.task_version,
            )
            try:
                service.preview_plan(action.field_id, operation_id=f"{action.operation_id}-preview")
                return _operation_feedback(
                    session, action, True, "现场观察已保存，新的安排预览已生成。", "preview_ready"
                )
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session, action, True, f"现场观察已保存，安排待更新：{error}", "saved"
                )
        if action.kind == "record_resource":
            day = date.fromisoformat(str(payload["day"]))
            start = current_state.draft.start_date or (legacy._today() + timedelta(days=1))
            if not start <= day <= start + timedelta(days=6):
                raise ValueError("日期不在当前七天安排内，请刷新后重试")
            intervals = _parse_intervals(str(payload.get("intervals", "")))
            minutes = int(payload.get("resource_minutes", 0))
            event = FactEventInputV3(
                feedback=FeedbackV2(
                    kind="resource",
                    day=day,
                    resource_minutes=minutes,
                    intervals=intervals,
                    at=datetime.now().astimezone(ZONE),
                )
            )
            service.commit_events(
                action.field_id,
                [event],
                operation_id=action.operation_id,
                expected_fact_version=current_state.fact_version,
                expected_draft_version=current_state.draft_version,
                expected_task_version=current_state.task_version,
            )
            try:
                service.preview_plan(action.field_id, operation_id=f"{action.operation_id}-preview")
                return _operation_feedback(
                    session, action, True, "人员时间已保存，新的安排预览已生成。", "preview_ready"
                )
            except (ValueError, KeyError, RuntimeError) as error:
                return _operation_feedback(
                    session, action, True, f"人员时间已保存，安排待更新：{error}", "saved"
                )
        if action.kind == "search_location":
            query = str(payload.get("query", "")).strip()
            results = agent.weather.search_locations(query, count=10)
            cache_key = f"agent_geo_candidates:{database.resolve()}:{action.field_id}"
            candidate_cache = session.setdefault(cache_key, {})
            candidate_cache[query] = (query, results)
            return _operation_feedback(
                session,
                action,
                True,
                f"找到 {len(results)} 个查询点，请手动选择。",
                "saved",
                close_panel=False,
            )
        if action.kind == "select_location":
            cache_key = f"agent_geo_candidates:{database.resolve()}:{action.field_id}"
            candidate_cache = session.get(cache_key, {})
            result_id = str(payload.get("location_id", ""))
            match = next(
                (
                    results
                    for _query, results in candidate_cache.values()
                    if any(row.id == result_id for row in results)
                ),
                None,
            )
            if match is None:
                raise ValueError("地点候选已过期，请重新搜索")
            location = agent.weather.select_location(match, result_id)
            draft = current_state.draft.model_copy(update={"location": location})
            service.save_field_draft(
                draft,
                expected_version=current_state.draft_version,
                operation_id=action.operation_id,
            )
            session.pop(cache_key, None)
            return _operation_feedback(
                session,
                action,
                True,
                "天气查询点已保存；它代表行政区附近位置，不是田块实测坐标。",
                "saved",
                close_panel=False,
            )
        if action.kind == "fetch_weather":
            location = current_state.draft.location
            if location is None:
                raise ValueError("请先搜索并确认天气查询点")
            start = current_state.draft.start_date or (legacy._today() + timedelta(days=1))
            snapshot = agent.weather.fetch_forecast(
                action.field_id, location, start, start + timedelta(days=6), force_refresh=True
            )
            return _operation_feedback(
                session,
                action,
                True,
                f"已获取 {len(snapshot.days)} 天天气数据。天气仅用于展示风险。",
                "saved",
                close_panel=False,
            )
        if action.kind == "send_message":
            message = str(payload.get("message", "")).strip()
            if not message:
                raise ValueError("请先输入要告诉助手的情况")
            job = session.get("active_agent_job")
            if job and not job.snapshot().done:
                raise ValueError("上一条消息还在处理中，请稍候")
            history = session.setdefault(f"chat:{action.field_id}", [])
            legacy._start_agent_job(agent, action.field_id, message, history)
            return _operation_feedback(
                session, action, True, "消息已发送，正在整理结果。", "agent_running"
            )
        if action.kind == "poll_agent":
            _operation_feedback(session, action, True, "正在处理。", "agent_running")
            return session["web_action_result"]
        if action.kind == "export_plan":
            return _operation_feedback(session, action, True, "导出内容已准备。", "saved")
        raise ValueError("当前操作不受支持")
    except (ValueError, KeyError, ValidationError, WeatherServiceError, RuntimeError) as error:
        result = _operation_feedback(session, action, False, str(error))
        session[f"web_notice:{current_field_id}"] = str(error)
        return result


def render_html_workbench(
    service: WorkbenchServiceV3,
    agent: AgentServiceV3,
    database: Any,
    mode: Literal["real", "demo"],
    field_id: str,
    state: Any | None,
    *,
    today: date,
    now: datetime,
    session: Any,
) -> None:
    view = build_workbench_view(
        service, mode, field_id, state, today=today, now=now, session=session
    )
    result = _WORKBENCH(
        data=view.model_dump(mode="json"),
        key="nongshi_workbench_html",
        width="stretch",
        height="stretch",
        on_action_change=lambda: None,
    )
    action_value = getattr(result, "action", None)
    if action_value:
        try:
            dispatch_ui_action(
                action_value,
                service=service,
                agent=agent,
                mode=mode,
                database=database,
                current_field_id=field_id,
                current_state=state,
                session=session,
            )
            st.rerun()
        except (ValueError, KeyError, ValidationError) as error:
            session[f"web_notice:{field_id}"] = str(error)
            if isinstance(action_value, dict) and action_value.get("operation_id"):
                session["web_action_result"] = UIActionResult(
                    operation_id=str(action_value["operation_id"]),
                    success=False,
                    message=str(error),
                    outcome="error",
                ).model_dump(mode="json")
            st.rerun()
