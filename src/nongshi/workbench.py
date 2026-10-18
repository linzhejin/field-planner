"""Persistent, blank-first Streamlit workbench for the single-field Agent flow."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st
from pydantic import ValidationError

from nongshi.agent import AgentServiceV3
from nongshi.config import Settings
from nongshi.repository import PlanStore, PlanStoreV2
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import DayWindow, FeedbackV2, FieldProfile, TimeWindow
from nongshi.schema_v3 import (
    AgentProposalV3,
    FactEventInputV3,
    FieldDraftV3,
    FieldStateV3,
    GeocodingResultV3,
    PlanPreviewV3,
)
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.weather import WeatherServiceError

ZONE = ZoneInfo("Asia/Shanghai")
NEW_FIELD = "__new_field__"
FIELD_TYPES = {
    "未知": None,
    "低洼/稻茬田": "lowland_rice_stubble",
    "丘陵旱地": "dry_hilly",
    "其他": "other",
}
BOOL_OPTIONS = {"未知": None, "是": True, "否": False}
STAGE_OPTIONS = {"未知": None, "苗期": "苗期", "其他生育期": "其他生育期"}
CROP_OPTIONS = {"未知": None, "油菜": "油菜", "其他作物": "其他作物"}
WORKABILITY_OPTIONS = {"未知": None, "可作业": True, "不可作业": False}

PALETTE = """
<style>
.stApp { background: #f5f6f1; color: #1d332b; }
[data-testid="stHeader"] { background: rgba(245,246,241,.94); }
h1, h2, h3 { color: #173f32; letter-spacing: -.025em; }
[data-testid="stButton"] button[kind="primary"] { background: #276653; border-color: #276653; }
.workbench-note { border-left: 4px solid #b9984b; padding: .65rem .9rem;
  background: #edf1e7; border-radius: 0 .4rem .4rem 0; line-height: 1.55; }
.muted { color: #627369; font-size: .9rem; }
</style>
"""


def _now() -> datetime:
    return datetime.now(ZONE)


def _today() -> date:
    return _now().date()


def _context(mode: str) -> tuple[WorkbenchServiceV3, AgentServiceV3, Path]:
    settings = Settings()
    database = settings.database_path
    if mode == "demo":
        database = database.with_name(f"{database.stem}.demo{database.suffix}")
    cache_key = f"workbench_context:{mode}:{database.resolve()}"
    if cache_key not in st.session_state:
        store = WorkbenchStoreV3(database)
        service = WorkbenchServiceV3(store, catalog=load_rule_catalog())
        agent = AgentServiceV3(settings, service)
        st.session_state[cache_key] = (service, agent)
    return *st.session_state[cache_key], database


def _saved_fields(service: WorkbenchServiceV3, mode: str) -> list[FieldStateV3]:
    return service.list_fields(mode=mode)


def _active_field_id(mode: str) -> str:
    return st.session_state.setdefault(f"new_field_id:{mode}", f"field-{uuid4().hex[:10]}")


def _field_state(service: WorkbenchServiceV3, field_id: str) -> FieldStateV3 | None:
    try:
        return service.get_field(field_id)
    except KeyError:
        return None


def _select_field(service: WorkbenchServiceV3, mode: str) -> tuple[str, FieldStateV3 | None]:
    fields = _saved_fields(service, mode)
    key = f"field_selection:{mode}"
    pending_selection_key = f"pending_field_selection:{mode}"
    pending_selection = st.session_state.pop(pending_selection_key, None)
    if pending_selection is not None:
        st.session_state[key] = pending_selection
    valid = {field.id for field in fields}
    if not fields:
        st.session_state[key] = NEW_FIELD
    elif st.session_state.get(key) not in valid | {NEW_FIELD}:
        st.session_state[key] = fields[-1].id
    selected = st.selectbox(
        "当前地块",
        options=[NEW_FIELD, *[field.id for field in fields]],
        format_func=lambda value: (
            "＋ 新建空白地块"
            if value == NEW_FIELD
            else next(
                (
                    field.draft.profile.name
                    if field.draft.profile
                    else f"未命名地块 · {field.id[-5:]}"
                    for field in fields
                    if field.id == value
                ),
                value,
            )
        ),
        key=key,
    )
    if st.button("新增地块", key=f"add_field:{mode}", width="stretch"):
        st.session_state[f"new_field_id:{mode}"] = f"field-{uuid4().hex[:10]}"
        st.session_state[pending_selection_key] = NEW_FIELD
        st.rerun()
    field_id = _active_field_id(mode) if selected == NEW_FIELD else selected
    return field_id, _field_state(service, field_id)


def _enum_label(mapping: dict[str, object], value: object) -> str:
    return next((label for label, stored in mapping.items() if stored == value), "未知")


def _save_draft_form(
    service: WorkbenchServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
    mode: str,
) -> None:
    previous = state.draft if state else FieldDraftV3(field_id=field_id, mode=mode)  # type: ignore[arg-type]
    profile = previous.profile
    key_suffix = f"{field_id}:{state.draft_version if state else 0}"
    st.subheader("地块档案")
    st.caption("首次使用从空白开始；未知信息保留为空，不会替你补值。")
    with st.form(f"field_form:{key_suffix}"):
        left, right = st.columns(2)
        name = left.text_input(
            "地块名称",
            value=profile.name if profile else "",
            max_chars=80,
            key=f"field_name:{key_suffix}",
        )
        province = right.text_input(
            "省份",
            value=profile.province or "" if profile else "",
            key=f"field_province:{key_suffix}",
        )
        province_confirmed = right.checkbox(
            "已确认省份",
            value=profile.province_confirmed if profile else False,
            key=f"province_confirmed:{key_suffix}",
        )
        crop_options = list(CROP_OPTIONS)
        crop_label = st.selectbox(
            "作物",
            crop_options,
            index=crop_options.index(_enum_label(CROP_OPTIONS, profile.crop if profile else None)),
            key=f"field_crop:{key_suffix}",
        )
        crop_confirmed = st.checkbox(
            "已确认作物",
            value=profile.crop_confirmed if profile else False,
            key=f"crop_confirmed:{key_suffix}",
        )
        stage_options = list(STAGE_OPTIONS)
        stage_label = st.selectbox(
            "生育期",
            stage_options,
            index=stage_options.index(
                _enum_label(STAGE_OPTIONS, profile.stage if profile else None)
            ),
            key=f"field_stage:{key_suffix}",
        )
        stage_confirmed = st.checkbox(
            "已由现场确认生育期",
            value=profile.stage_confirmed if profile else False,
            key=f"stage_confirmed:{key_suffix}",
        )
        type_options = list(FIELD_TYPES)
        field_type_label = st.selectbox(
            "田型",
            type_options,
            index=type_options.index(
                _enum_label(FIELD_TYPES, profile.field_type if profile else None)
            ),
            key=f"field_type:{key_suffix}",
        )
        area_mu = st.number_input(
            "面积（亩，可留空）",
            min_value=0.0,
            value=float(profile.area_mu or 0) if profile else 0.0,
            step=0.5,
            key=f"field_area:{key_suffix}",
        )
        inspect_label = st.selectbox(
            "是否需要巡田",
            list(BOOL_OPTIONS),
            index=list(BOOL_OPTIONS).index(
                _enum_label(BOOL_OPTIONS, profile.inspection_requested if profile else None)
            ),
            key=f"inspection_requested:{key_suffix}",
        )
        maintenance_label = st.selectbox(
            "是否需要沟渠维护",
            list(BOOL_OPTIONS),
            index=list(BOOL_OPTIONS).index(
                _enum_label(BOOL_OPTIONS, profile.maintenance_need_confirmed if profile else None)
            ),
            key=f"maintenance_need:{key_suffix}",
        )
        problem_label = st.selectbox(
            "是否确认存在需要处理的问题",
            list(BOOL_OPTIONS),
            index=list(BOOL_OPTIONS).index(
                _enum_label(
                    BOOL_OPTIONS, profile.maintenance_problem_confirmed if profile else None
                )
            ),
            key=f"maintenance_problem:{key_suffix}",
        )
        moisture_options = {"未知": None, "偏干": "dry", "适中": "normal", "偏湿": "wet"}
        moisture = st.selectbox(
            "土壤墒情",
            list(moisture_options),
            index=list(moisture_options).index(
                _enum_label(moisture_options, profile.moisture if profile else None)
            ),
            key=f"field_moisture:{key_suffix}",
        )
        ditch_options = {"未知": None, "通畅": "clear", "受阻": "impaired"}
        ditch = st.selectbox(
            "沟渠连通情况",
            list(ditch_options),
            index=list(ditch_options).index(
                _enum_label(ditch_options, profile.ditch_connectivity if profile else None)
            ),
            key=f"field_ditch:{key_suffix}",
        )
        water = st.selectbox(
            "是否有积水",
            list(BOOL_OPTIONS),
            index=list(BOOL_OPTIONS).index(
                _enum_label(BOOL_OPTIONS, profile.standing_water if profile else None)
            ),
            key=f"field_water:{key_suffix}",
        )
        leaf_known = st.checkbox(
            "记录叶片数",
            value=profile.leaf_count is not None if profile else False,
            key=f"leaf_known:{key_suffix}",
        )
        leaf_count = st.number_input(
            "叶片数",
            min_value=0.0,
            value=float(profile.leaf_count or 0) if profile else 0.0,
            disabled=not leaf_known,
            key=f"leaf_count:{key_suffix}",
        )
        duration_left, duration_right = st.columns(2)
        inspection_minutes = duration_left.number_input(
            "巡田预计时长（分钟）",
            min_value=0,
            step=15,
            value=profile.inspection_duration_minutes or 0 if profile else 0,
            key=f"inspection_minutes:{key_suffix}",
        )
        maintenance_minutes = duration_right.number_input(
            "维护预计时长（分钟）",
            min_value=0,
            step=15,
            value=profile.maintenance_duration_minutes or 0 if profile else 0,
            key=f"maintenance_minutes:{key_suffix}",
        )
        priority_left, priority_right = st.columns(2)
        inspection_priority = priority_left.selectbox(
            "巡田优先级（1 最高）",
            [1, 2, 3],
            index=(profile.inspection_priority - 1) if profile else 1,
            key=f"inspection_priority:{key_suffix}",
        )
        maintenance_priority = priority_right.selectbox(
            "维护优先级（1 最高）",
            [1, 2, 3],
            index=(profile.maintenance_priority - 1) if profile else 1,
            key=f"maintenance_priority:{key_suffix}",
        )
        default_start = max(previous.start_date or (_today() + timedelta(days=1)), _today())
        start_date = st.date_input(
            "七天计划起始日",
            value=default_start,
            min_value=_today(),
            help="默认从明天开始；若选择今天，只填写当前时刻之后的时段。",
            key=f"start_date:{key_suffix}",
        )
        inspection_due_enabled = st.checkbox(
            "设置巡田目标日期",
            value=profile.inspection_due_date is not None if profile else False,
            key=f"inspection_due_enabled:{key_suffix}",
        )
        inspection_due = st.date_input(
            "巡田目标日期",
            value=max(profile.inspection_due_date or start_date, start_date)
            if profile
            else start_date,
            min_value=start_date,
            disabled=not inspection_due_enabled,
            key=f"inspection_due:{key_suffix}",
        )
        maintenance_due_enabled = st.checkbox(
            "设置维护目标日期",
            value=profile.maintenance_due_date is not None if profile else False,
            key=f"maintenance_due_enabled:{key_suffix}",
        )
        maintenance_due = st.date_input(
            "维护目标日期",
            value=max(profile.maintenance_due_date or start_date, start_date)
            if profile
            else start_date,
            min_value=start_date,
            disabled=not maintenance_due_enabled,
            key=f"maintenance_due:{key_suffix}",
        )
        notes = st.text_area(
            "补充说明（不会自动转成规则）",
            value=previous.notes,
            key=f"field_notes:{key_suffix}",
        )
        submitted = st.form_submit_button(
            "保存地块档案",
            type="primary",
            width="stretch",
            key=f"save_field:{field_id}:{state.draft_version if state else 0}",
        )

    if submitted:
        try:
            observed_values = (
                moisture_options[moisture],
                ditch_options[ditch],
                BOOL_OPTIONS[water],
                leaf_count if leaf_known else None,
                BOOL_OPTIONS[problem_label],
            )
            has_observation = any(value is not None for value in observed_values)
            updated_profile = FieldProfile(
                id=field_id,
                name=name.strip(),
                province=province.strip() or None,
                province_confirmed=province_confirmed,
                crop=CROP_OPTIONS[crop_label],
                crop_confirmed=crop_confirmed,
                stage=STAGE_OPTIONS[stage_label],
                stage_confirmed=stage_confirmed,
                field_type=FIELD_TYPES[field_type_label],
                area_mu=area_mu or None,
                inspection_requested=BOOL_OPTIONS[inspect_label],
                maintenance_need_confirmed=BOOL_OPTIONS[maintenance_label],
                maintenance_problem_confirmed=BOOL_OPTIONS[problem_label],
                moisture=moisture_options[moisture],
                ditch_connectivity=ditch_options[ditch],
                standing_water=BOOL_OPTIONS[water],
                leaf_count=leaf_count if leaf_known else None,
                inspection_duration_minutes=inspection_minutes or None,
                maintenance_duration_minutes=maintenance_minutes or None,
                inspection_priority=inspection_priority,
                maintenance_priority=maintenance_priority,
                inspection_earliest_date=profile.inspection_earliest_date if profile else None,
                inspection_due_date=inspection_due if inspection_due_enabled else None,
                maintenance_earliest_date=profile.maintenance_earliest_date if profile else None,
                maintenance_due_date=maintenance_due if maintenance_due_enabled else None,
                maintenance_task_id=profile.maintenance_task_id if profile else None,
                completed_maintenance_task_ids=(
                    profile.completed_maintenance_task_ids if profile else []
                ),
                observed_at=datetime.now(UTC) if has_observation else None,
                observation_source="observed" if has_observation else "synthetic",
            )
            days = previous.days
            if previous.start_date and previous.start_date != start_date and days:
                days = [
                    day.model_copy(update={"day": start_date + timedelta(days=i)})
                    for i, day in enumerate(days)
                ]
            draft = previous.model_copy(
                update={
                    "profile": updated_profile,
                    "start_date": start_date,
                    "days": days,
                    "notes": notes,
                    "mode": mode,
                }
            )
            saved = service.save_field_draft(
                draft,
                expected_version=state.draft_version if state else None,
                operation_id=f"draft-{uuid4()}",
            )
            st.session_state[f"pending_field_selection:{mode}"] = saved.id
            st.session_state[f"notice:{saved.id}"] = "地块档案已保存。"
            st.rerun()
        except (ValueError, ValidationError) as error:
            st.error(f"没有保存：{error}")


def _parse_intervals(value: str) -> list[TimeWindow]:
    intervals: list[TimeWindow] = []
    for part in filter(None, (piece.strip() for piece in value.split(";"))):
        match = re.fullmatch(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", part)
        if not match:
            raise ValueError(f"时段格式应为 HH:MM-HH:MM，收到：{part}")
        intervals.append(
            TimeWindow(
                start=time.fromisoformat(match.group(1)), end=time.fromisoformat(match.group(2))
            )
        )
    return intervals


def _render_schedule_form(
    service: WorkbenchServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
) -> None:
    st.subheader("逐日作业条件与可用时段")
    if state is None:
        st.info("先保存地块档案，再录入逐日资源。")
        return
    draft = state.draft
    start_date = draft.start_date or (_today() + timedelta(days=1))
    old_days = {entry.day: entry for entry in draft.days}
    default_days = [
        old_days.get(start_date + timedelta(days=i))
        or (draft.days[i] if i < len(draft.days) else None)
        for i in range(7)
    ]
    resources_default = any(day and day.resource_minutes is not None for day in default_days)
    resources_enabled = st.checkbox(
        "录入可用分钟数和具体时段",
        value=resources_default,
        key=f"resources_enabled:{field_id}:{state.draft_version}",
    )
    with st.form(f"schedule_form:{field_id}:{state.draft_version}"):
        st.caption("现场能否进田、天气是否适合作业分别确认；预报本身不会自动成为作业确认。")
        day_values: list[tuple[date, str, str, int, str]] = []
        for index in range(7):
            day_date = start_date + timedelta(days=index)
            previous = default_days[index]
            with st.container(border=True):
                st.markdown(f"**{day_date:%Y年%m月%d日}**")
                site_value = previous.site_workable if previous else None
                weather_value = previous.weather_workable if previous else None
                site_options = list(WORKABILITY_OPTIONS)
                site = st.selectbox(
                    "现场条件",
                    site_options,
                    index=site_options.index(_enum_label(WORKABILITY_OPTIONS, site_value)),
                    key=f"site:{field_id}:{state.draft_version}:{index}",
                )
                weather = st.selectbox(
                    "天气条件",
                    site_options,
                    index=site_options.index(_enum_label(WORKABILITY_OPTIONS, weather_value)),
                    key=f"weather:{field_id}:{state.draft_version}:{index}",
                )
                capacity = (
                    previous.resource_minutes
                    if previous and previous.resource_minutes is not None
                    else 0
                )
                minutes = st.number_input(
                    "可用分钟数（0 表示当天没有可用人力）",
                    min_value=0,
                    max_value=1440,
                    step=15,
                    value=capacity,
                    key=f"minutes:{field_id}:{state.draft_version}:{index}",
                    disabled=not resources_enabled,
                )
                interval_default = ";".join(
                    f"{window.start:%H:%M}-{window.end:%H:%M}"
                    for window in (previous.intervals if previous else [])
                )
                interval_text = st.text_input(
                    "可用时段（多个用分号隔开）",
                    value=interval_default,
                    placeholder="例如 09:00-11:00;13:00-15:00",
                    key=f"intervals:{field_id}:{state.draft_version}:{index}",
                    disabled=not resources_enabled,
                )
                day_values.append((day_date, site, weather, int(minutes), interval_text))
        submitted = st.form_submit_button(
            "保存逐日条件和资源",
            type="primary",
            width="stretch",
            key=f"save_schedule:{field_id}:{state.draft_version}",
        )
    if submitted:
        try:
            days = [
                DayWindow(
                    day=day_date,
                    site_workable=WORKABILITY_OPTIONS[site],
                    weather_workable=WORKABILITY_OPTIONS[weather],
                    resource_minutes=minutes if resources_enabled else None,
                    intervals=_parse_intervals(interval_text) if resources_enabled else [],
                )
                for day_date, site, weather, minutes, interval_text in day_values
            ]
            draft = state.draft.model_copy(update={"start_date": start_date, "days": days})
            service.save_field_draft(
                draft,
                expected_version=state.draft_version,
                operation_id=f"schedule-{uuid4()}",
            )
            st.session_state[f"notice:{field_id}"] = "逐日条件已保存；现场与天气仍按天分别判断。"
            st.rerun()
        except (ValueError, ValidationError) as error:
            st.error(f"没有保存：{error}")


def _render_weather(
    service: WorkbenchServiceV3,
    agent: AgentServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
) -> None:
    st.subheader("地点与天气参考")
    if state is None:
        st.caption("保存地块后可以搜索查询点。")
        return
    query_key = f"weather_query:{field_id}"
    with st.form(f"geo_search:{field_id}"):
        query = st.text_input(
            "搜索乡镇或县区", key=query_key, placeholder="例如：湖北省武汉市洪山区"
        )
        search = st.form_submit_button("搜索地点")
    if search:
        try:
            results = agent.weather.search_locations(query)
            st.session_state[f"geo_results:{field_id}"] = results
            if not results:
                st.warning("没有找到地点候选；可尝试县区名称。")
        except (ValueError, WeatherServiceError) as error:
            st.error(str(error))
    results: list[GeocodingResultV3] = st.session_state.get(f"geo_results:{field_id}", [])
    if results:
        choices = {result.id: result for result in results}
        selected_id = st.selectbox(
            "选择天气查询点（必须手动选择）",
            options=list(choices),
            index=None,
            format_func=lambda value: choices[value].label,
            key=f"geo_choice:{field_id}",
        )
        resolution_labels = {
            "未判定": "unknown",
            "乡镇级": "township",
            "县/区级": "county",
            "市级": "city",
        }
        resolution = st.selectbox(
            "查询点级别", list(resolution_labels), key=f"geo_resolution:{field_id}"
        )
        if st.button(
            "确认所选地点", key=f"confirm_location:{field_id}", disabled=selected_id is None
        ):
            location = agent.weather.select_location(
                results,
                str(selected_id),
                resolution=resolution_labels[resolution],
            )
            updated = state.draft.model_copy(update={"location": location})
            try:
                service.save_field_draft(
                    updated,
                    expected_version=state.draft_version,
                    operation_id=f"location-{uuid4()}",
                )
                st.session_state[f"notice:{field_id}"] = "天气查询点已保存。"
                st.rerun()
            except ValueError as error:
                st.error(f"地点未保存：{error}")
    state = _field_state(service, field_id)
    if state and state.draft.location:
        location = state.draft.location
        st.write(f"已选：{location.display_name} · {location.resolution}查询点")
        st.caption(f"坐标 {location.latitude:.4f}, {location.longitude:.4f}；时区 Asia/Shanghai")
        start = state.draft.start_date or (_today() + timedelta(days=1))
        if st.button("查询完整七天天气", key=f"fetch_weather:{field_id}"):
            try:
                snapshot = agent.weather.fetch_forecast(
                    field_id, location, start, start + timedelta(days=6)
                )
                st.session_state[f"notice:{field_id}"] = f"已获取 {len(snapshot.days)} 天天气数据。"
                st.rerun()
            except (ValueError, WeatherServiceError) as error:
                st.error(f"天气查询失败：{error}")
        snapshot = service.latest_weather(field_id)
        if snapshot:
            state_label = "缓存已过期，仅作历史参考" if snapshot.is_stale else "当前缓存"
            st.caption(
                f"{state_label} · {snapshot.source} · 获取于 "
                f"{snapshot.fetched_at.astimezone(ZONE):%Y-%m-%d %H:%M} · "
                f"返回网格 {snapshot.grid_latitude:.3f}, {snapshot.grid_longitude:.3f}"
            )
            st.info("天气预报只展示风险信息，不自动表示田间积水、道路通畅或可以进田。")
            for day in snapshot.days:
                st.write(
                    f"{day.day:%m月%d日} · {day.temperature_min}–{day.temperature_max} °C · "
                    f"降水 {day.precipitation_sum} mm · 最大风速 {day.wind_speed_max} km/h"
                )


def _render_evidence(item) -> None:  # type: ignore[no-untyped-def]
    if not item.evidence:
        return
    with st.expander("规则依据与来源"):
        for evidence in item.evidence:
            st.markdown(f"**{evidence.rule_id} · {evidence.title}**")
            st.caption(
                f"{evidence.publisher} · {evidence.source_date or '日期未提供'} · "
                f"页码 {', '.join(map(str, evidence.pdf_pages)) or '未提供'} · "
                f"{evidence.status_label} · runtime_enabled={str(evidence.runtime_enabled).lower()}"
            )
            st.write(f"适用条件：{evidence.applicability_text or '未提供'}")
            st.write(f"限制：{evidence.limits or '未提供'}")
            if evidence.quote:
                st.write(f"原文：{evidence.quote}")
            st.caption(f"来源指纹：{evidence.source_sha256}")
            if evidence.source_url:
                st.link_button("打开来源", evidence.source_url)


def _render_item(item) -> None:  # type: ignore[no-untyped-def]
    status = {
        "scheduled": "已安排",
        "unscheduled": "未排入",
        "pending": "待确认",
        "not_generated": "未生成",
        "completed": "已完成",
    }.get(item.schedule_status, item.schedule_status)
    with st.container(border=True):
        st.markdown(f"**{item.title}**　`{item.task_id}`")
        st.caption(f"适用性：{item.applicability} · 需求：{item.necessity} · 排程：{status}")
        if item.start_at and item.end_at:
            st.write(
                f"{item.start_at.astimezone(ZONE):%Y-%m-%d %H:%M}–"
                f"{item.end_at.astimezone(ZONE):%H:%M} · {item.duration_minutes} 分钟"
            )
        st.write(item.reason)
        if item.missing_fields:
            st.caption("待补信息：" + "、".join(item.missing_fields[:6]))
        _render_evidence(item)


def _render_evaluation(
    service: WorkbenchServiceV3, field_id: str, state: FieldStateV3 | None
) -> None:
    st.subheader("候选事项与待确认信息")
    if state is None or state.draft.profile is None:
        st.info("先保存地块基本信息。这里不会用示例数据代替你的地块。")
        return
    evaluation = service.evaluate_field(field_id)
    st.caption(
        f"规则资料 {evaluation.ruleset_version} · {evaluation.status_label} · "
        f"事实 v{evaluation.fact_version} / 档案 v{evaluation.draft_version}"
    )
    if evaluation.clarification_questions:
        st.warning("优先核对：" + "；".join(evaluation.clarification_questions[:3]))
    if evaluation.items:
        for item in evaluation.items:
            _render_item(item)
    else:
        st.info("当前没有候选事项。")
    if evaluation.missing_fields:
        st.caption("其他缺项：" + "、".join(evaluation.missing_fields[:12]))


def _show_preview(service: WorkbenchServiceV3, preview: PlanPreviewV3) -> None:
    st.markdown("#### 七天计划预览")
    st.caption(
        f"依据：档案 v{preview.expected_draft_version} · "
        f"现场事实 v{preview.expected_fact_version} · 单地块计算，尚未统筹其他地块时间"
    )
    for basis in preview.version.calculation_basis:
        st.caption(basis)
    if preview.version.changes:
        st.markdown("**变化与原因**")
        for change in preview.version.changes:
            old_time = (
                f"{change.old_start_at.astimezone(ZONE):%m-%d %H:%M}"
                if change.old_start_at
                else "无旧时段"
            )
            new_time = (
                f"{change.new_start_at.astimezone(ZONE):%m-%d %H:%M}"
                if change.new_start_at
                else "未排入"
            )
            old_end = (
                f"–{change.old_end_at.astimezone(ZONE):%H:%M}"
                if change.old_start_at and change.old_end_at
                else ""
            )
            new_end = (
                f"–{change.new_end_at.astimezone(ZONE):%H:%M}"
                if change.new_start_at and change.new_end_at
                else ""
            )
            st.write(
                f"- {change.task_id} {change.kind}：{old_time}{old_end} → "
                f"{new_time}{new_end}；{change.reason}"
            )
    else:
        st.caption("首次预览。")
    for item in preview.version.plan.items:
        _render_item(item)
    if preview.status == "preview":
        left, right = st.columns(2)
        if left.button("采用这版安排", key=f"adopt:{preview.id}", type="primary"):
            try:
                adopted = service.adopt_plan(
                    preview.id,
                    expected_fact_version=preview.expected_fact_version,
                    expected_plan_id=preview.expected_plan_id,
                    operation_id=f"adopt-{uuid4()}",
                )
                st.session_state[f"current_preview:{preview.field_id}"] = None
                st.session_state[f"notice:{preview.field_id}"] = (
                    "计划已采用并保存新版本。"
                    if adopted.created_new_version
                    else "安排无变化；事实已更新，没有重复创建计划版本。"
                )
                st.rerun()
            except ValueError as error:
                st.error(f"预览已失效：{error}。请重新计算。")
        if right.button("取消预览", key=f"cancel:{preview.id}"):
            service.cancel_preview(preview.id)
            st.session_state[f"current_preview:{preview.field_id}"] = None
            st.rerun()
    else:
        st.info(f"预览状态：{preview.status}")


def _render_plans(service: WorkbenchServiceV3, field_id: str, state: FieldStateV3 | None) -> None:
    st.subheader("计划与版本")
    if state is None or state.draft.profile is None:
        st.info("保存地块后可先看候选事项；排程信息可以稍后补。")
        return
    if state.plan_stale:
        st.warning("现场事实或档案已变化，当前采用版需要重新预览。旧版仍保留。")
    if st.button("计算七天安排预览", key=f"preview_plan:{field_id}", type="primary"):
        try:
            preview = service.preview_plan(field_id, operation_id=f"manual-preview-{uuid4()}")
            st.session_state[f"current_preview:{field_id}"] = preview.id
        except (ValueError, KeyError) as error:
            st.error(f"无法生成预览：{error}")
    preview_id = st.session_state.get(f"current_preview:{field_id}")
    if preview_id:
        try:
            _show_preview(service, service.store.load_preview(preview_id))
        except KeyError:
            st.session_state[f"current_preview:{field_id}"] = None
    adopted = service.get_current_plan(field_id)
    if adopted:
        st.markdown("#### 当前采用版")
        st.caption(f"版本 `{adopted.id}` · {adopted.created_at.astimezone(ZONE):%Y-%m-%d %H:%M}")
        for item in adopted.items:
            _render_item(item)
    history = service.plan_history(field_id)
    if history:
        with st.expander(f"历史版本（{len(history)}）"):
            for version in history:
                st.markdown(
                    f"**{version.created_at.astimezone(ZONE):%Y-%m-%d %H:%M} · `{version.id}`**"
                )
                if version.parent_id:
                    st.caption(f"基于 `{version.parent_id}`")
                if version.changes:
                    for change in version.changes:
                        st.write(f"- {change.task_id}：{change.kind}；{change.reason}")
                else:
                    st.caption("首版计划")
    if adopted:
        st.download_button(
            "下载 Markdown 清单",
            service.export_markdown(field_id),
            file_name=f"{field_id}-plan.md",
            mime="text/markdown",
            key=f"export_md:{field_id}",
        )
        st.download_button(
            "下载 JSON 记录",
            service.export_json(field_id),
            file_name=f"{field_id}-plan.json",
            mime="application/json",
            key=f"export_json:{field_id}",
        )


def _commit_feedback(
    service: WorkbenchServiceV3,
    state: FieldStateV3,
    event: FeedbackV2,
) -> None:
    service.commit_events(
        state.id,
        [FactEventInputV3(feedback=event)],
        operation_id=f"user-event-{uuid4()}",
        expected_fact_version=state.fact_version,
    )
    st.session_state[f"current_preview:{state.id}"] = None
    st.session_state[f"notice:{state.id}"] = "现场事实已保存；旧计划保留并标记待更新。"


def _render_feedback(
    service: WorkbenchServiceV3, field_id: str, state: FieldStateV3 | None
) -> None:
    st.subheader("记录现场事实与资源变化")
    current = service.get_current_plan(field_id) if state else None
    if state is None:
        st.caption("保存地块档案后可以记录事实。")
        return
    if current:
        eligible = [
            item
            for item in current.items
            if item.start_at and not item.completed and item.start_at.astimezone(ZONE) <= _now()
        ]
        if eligible:
            with st.form(f"completion:{field_id}:{state.fact_version}"):
                task = st.selectbox(
                    "已完成的已安排任务",
                    eligible,
                    format_func=lambda item: (
                        f"{item.title} · {item.start_at.astimezone(ZONE):%m-%d %H:%M}"
                    ),
                )
                if st.form_submit_button("记录完成"):
                    try:
                        _commit_feedback(
                            service,
                            state,
                            FeedbackV2(kind="completed", task_id=task.task_id, at=_now()),
                        )
                        st.rerun()
                    except ValueError as error:
                        st.error(f"完成事实未保存：{error}")
        else:
            st.caption("当前没有已到计划开始时间且尚未完成的任务。")
    observation_fields = {
        "沟渠连通": ("ditch_connectivity", {"未知": None, "通畅": "clear", "受阻": "impaired"}),
        "积水情况": ("standing_water", BOOL_OPTIONS),
        "土壤墒情": ("moisture", {"未知": None, "偏干": "dry", "适中": "normal", "偏湿": "wet"}),
        "维护需求": ("maintenance_need_confirmed", BOOL_OPTIONS),
        "维护问题": ("maintenance_problem_confirmed", BOOL_OPTIONS),
        "是否有实际降雨": ("actual_rain_observed", BOOL_OPTIONS),
    }
    with st.form(f"observation:{field_id}:{state.fact_version}"):
        label = st.selectbox(
            "观察项目",
            list(observation_fields),
            key=f"observation_field:{field_id}:{state.fact_version}",
        )
        field, choices = observation_fields[label]
        value_label = st.selectbox(
            "记录值", list(choices), key=f"observation_value:{field_id}:{state.fact_version}"
        )
        if st.form_submit_button(
            "保存观察事实", key=f"save_observation:{field_id}:{state.fact_version}"
        ):
            try:
                _commit_feedback(
                    service,
                    state,
                    FeedbackV2(
                        kind="observation",
                        plot_id=field_id,
                        field=field,
                        value=choices[value_label],
                        at=_now(),
                    ),
                )
                st.rerun()
            except ValueError as error:
                st.error(f"观察未保存：{error}")
    start = state.draft.start_date or (_today() + timedelta(days=1))
    with st.form(f"workability:{field_id}:{state.fact_version}"):
        day = st.date_input(
            "作业条件日期", value=start, min_value=start, max_value=start + timedelta(days=6)
        )
        site_label = st.selectbox("现场能否作业", list(WORKABILITY_OPTIONS))
        weather_label = st.selectbox("天气是否适合", list(WORKABILITY_OPTIONS))
        if st.form_submit_button("保存当天条件"):
            try:
                _commit_feedback(
                    service,
                    state,
                    FeedbackV2(
                        kind="workability",
                        plot_id=field_id,
                        day=day,
                        site_workable=WORKABILITY_OPTIONS[site_label],
                        weather_workable=WORKABILITY_OPTIONS[weather_label],
                        at=_now(),
                    ),
                )
                st.rerun()
            except ValueError as error:
                st.error(f"当天条件未保存：{error}")
    with st.form(f"resource:{field_id}:{state.fact_version}"):
        resource_day = st.date_input(
            "资源变化日期", value=start, min_value=start, max_value=start + timedelta(days=6)
        )
        resource_minutes = st.number_input(
            "人员可用分钟数", min_value=0, max_value=1440, step=15, value=0
        )
        intervals_text = st.text_input("可用时段", placeholder="09:00-11:00;13:00-15:00")
        if st.form_submit_button("保存资源变化"):
            try:
                _commit_feedback(
                    service,
                    state,
                    FeedbackV2(
                        kind="resource",
                        day=resource_day,
                        resource_minutes=int(resource_minutes),
                        intervals=_parse_intervals(intervals_text),
                        at=_now(),
                    ),
                )
                st.rerun()
            except (ValueError, ValidationError) as error:
                st.error(f"资源变化未保存：{error}")


def _apply_proposal(service: WorkbenchServiceV3, proposal: AgentProposalV3) -> str:
    if proposal.kind == "field_update" and proposal.draft is not None:
        saved = service.save_field_draft(
            proposal.draft,
            expected_version=proposal.expected_draft_version,
            operation_id=f"confirm-{proposal.id}",
        )
        return f"档案修改已保存为 v{saved.draft_version}。"
    if proposal.kind == "facts":
        committed = service.commit_events(
            proposal.field_id,
            proposal.events,
            operation_id=f"confirm-{proposal.id}",
            expected_fact_version=proposal.expected_fact_version or 0,
        )
        return f"现场事实已保存为 v{committed.fact_version}；请重新预览计划。"
    raise ValueError("此修改建议格式无效")


def _render_agent(
    service: WorkbenchServiceV3,
    agent: AgentServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
) -> None:
    st.subheader("农时助手")
    st.caption(f"模型：{agent.settings.llm_model} · 修改必须逐项确认")
    if state is None or state.draft.profile is None:
        st.info("先保存地块档案，再与助手对话。结构化表单不依赖模型密钥。")
        return
    history_key = f"chat:{field_id}"
    history: list[dict[str, str]] = st.session_state.setdefault(history_key, [])
    for message in history[-20:]:
        with st.chat_message(message["role"]):
            st.write(message["content"])
    with st.form(f"assistant_form:{field_id}"):
        user_message = st.text_area(
            "你想补充或询问什么？", placeholder="例如：我确认这里需要巡田，帮我看看还缺什么"
        )
        submitted = st.form_submit_button("发送给助手", type="primary")
    if submitted:
        try:
            response = agent.run(field_id, user_message, conversation=history)
            history.extend(
                [
                    {"role": "user", "content": user_message},
                    {
                        "role": "assistant",
                        "content": response.text or response.error_message or response.status,
                    },
                ]
            )
            st.session_state[f"agent_result:{field_id}"] = response
            st.rerun()
        except (ValueError, KeyError) as error:
            st.error(f"本轮未完成：{error}")
    response = st.session_state.get(f"agent_result:{field_id}")
    if response:
        if response.status == "success":
            if response.text:
                st.markdown(response.text)
        else:
            st.warning(response.error_message or response.status)
            st.caption("已保存的档案与现场事实不受模型失败影响；可继续用结构化表单操作。")
        if response.tools:
            with st.expander("本轮工具调用记录"):
                for trace in response.tools:
                    st.write(
                        f"{trace.name} · {trace.status} · {trace.elapsed_ms} ms · "
                        f"{trace.result_summary}"
                    )
        for proposal in response.proposals:
            st.markdown("#### 等待你确认的修改")
            st.write(proposal.summary)
            st.json(proposal.changes)
            yes, no = st.columns(2)
            if yes.button("确认并保存", key=f"confirm_proposal:{proposal.id}", type="primary"):
                try:
                    st.session_state[f"notice:{field_id}"] = _apply_proposal(service, proposal)
                    st.session_state[f"agent_result:{field_id}"] = None
                    st.rerun()
                except (ValueError, KeyError) as error:
                    st.error(f"确认失败：{error}。请刷新状态后重新提出。")
            if no.button("忽略建议", key=f"ignore_proposal:{proposal.id}"):
                st.session_state[f"agent_result:{field_id}"] = None
                st.rerun()
        for preview in response.previews:
            st.markdown("#### 助手生成的预览（尚未采用）")
            st.session_state[f"current_preview:{field_id}"] = preview.id
            _show_preview(service, preview)
        if response.prompt_tokens is not None:
            st.caption(
                f"本轮 {response.request_count} 次模型请求 · "
                f"{response.prompt_tokens + (response.completion_tokens or 0)} tokens"
            )


def _seed_demo(service: WorkbenchServiceV3) -> str:
    field_id = f"demo-{uuid4().hex[:8]}"
    profile = FieldProfile(
        id=field_id,
        name="演示地块",
        province="湖北",
        province_confirmed=True,
        crop="油菜",
        crop_confirmed=True,
        stage="苗期",
        stage_confirmed=True,
        field_type="lowland_rice_stubble",
        inspection_requested=True,
        inspection_duration_minutes=30,
    )
    service.save_field_draft(
        FieldDraftV3(
            field_id=field_id,
            mode="demo",
            profile=profile,
            input_source="demo",
            notes="固定模拟数据，仅用于演示界面流程。",
        ),
        expected_version=None,
        operation_id=f"demo-{uuid4()}",
    )
    return field_id


def _render_legacy_history(service: WorkbenchServiceV3, database: Path, mode: str) -> None:
    if mode != "real":
        return
    with st.expander("旧版计划（只读；可选择复制为新地块）"):
        legacy_v1 = PlanStore(database).heads()
        legacy_v2 = PlanStoreV2(database).heads()
        if not legacy_v1 and not legacy_v2:
            st.caption("本地数据库中没有旧版计划。")
        for version in legacy_v1:
            st.markdown(f"**旧版 v1 · {version.created_at.astimezone(ZONE):%Y-%m-%d %H:%M}**")
            st.caption(
                f"只读计划 {version.id} · {len(version.request.plots)} 块地 · "
                f"{len(version.items)} 项任务"
            )
            st.write("、".join(plot.name for plot in version.request.plots))
            if st.button("复制此 v1 输入", key=f"copy_legacy_v1:{version.id}"):
                try:
                    copied = service.copy_legacy_plan(version.id, operation_id=f"copy-v1-{uuid4()}")
                    st.session_state["pending_field_selection:real"] = copied.id
                    st.session_state[f"notice:{copied.id}"] = (
                        "已复制旧版输入；不确定的作物、田型、天气和作业条件仍为空。"
                    )
                    st.rerun()
                except (ValueError, KeyError) as error:
                    st.error(f"复制失败：{error}")
        for version in legacy_v2:
            st.markdown(f"**旧版 v2 · {version.created_at.astimezone(ZONE):%Y-%m-%d %H:%M}**")
            st.caption(
                f"只读计划 {version.id} · {len(version.request.plots)} 块地 · "
                f"{len(version.items)} 项任务"
            )
            st.write("、".join(plot.name for plot in version.request.plots))
            if st.button("复制此 v2 输入", key=f"copy_legacy_v2:{version.id}"):
                try:
                    copied = service.copy_legacy_plan(version.id, operation_id=f"copy-v2-{uuid4()}")
                    st.session_state["pending_field_selection:real"] = copied.id
                    st.session_state[f"notice:{copied.id}"] = (
                        "已复制旧版输入；未知信息保持为空。旧版记录未被修改。"
                    )
                    st.rerun()
                except (ValueError, KeyError) as error:
                    st.error(f"复制失败：{error}")


def _render_rule_reviews(service: WorkbenchServiceV3, field_id: str, state: FieldStateV3) -> None:
    st.markdown("#### 规则复核记录")
    st.caption("复核记录独立保存；不改写规则卡原文，也不会把 runtime_enabled 改为正式启用。")
    for record in service.rule_adoptions(field_id):
        st.write(
            f"{record.rule_id} · {record.status} · {record.reviewer or '未填写复核人'} · "
            f"{record.reviewed_at.astimezone(ZONE):%Y-%m-%d %H:%M}"
        )
        if record.notes:
            st.caption(record.notes)
    labels = {
        "待复核": "pending_review",
        "复核通过（仅开发验证）": "adopted",
        "不采纳": "rejected",
    }
    with st.form(f"rule_review:{field_id}:{state.draft_version}"):
        rule_id = st.selectbox("规则卡", sorted(service.catalog.development_rule_ids))
        status_label = st.selectbox("复核结论", list(labels))
        reviewer = st.text_input("复核人")
        notes = st.text_area("适用范围、限制或待补内容")
        submitted = st.form_submit_button(
            "保存复核记录", key=f"save_rule_review:{field_id}:{state.draft_version}"
        )
    if submitted:
        try:
            service.record_rule_adoption(
                field_id,
                rule_id,
                status=labels[status_label],
                reviewer=reviewer.strip(),
                notes=notes.strip(),
            )
            st.session_state[f"notice:{field_id}"] = "规则复核记录已保存；原卡和运行配置未改变。"
            st.rerun()
        except ValueError as error:
            st.error(f"复核记录未保存：{error}")


def main() -> None:
    st.set_page_config(page_title="农时窗口 · 单地块工作台", page_icon="🌱", layout="wide")
    st.markdown(PALETTE, unsafe_allow_html=True)
    st.title("农时窗口")
    st.caption("油菜苗期任务助手 · 单地块工作台")
    with st.sidebar:
        st.markdown("### 工作空间")
        mode_label = st.radio(
            "数据空间", ["真实地块", "独立模拟示例"], horizontal=False, key="workspace_mode"
        )
        mode = "real" if mode_label == "真实地块" else "demo"
        st.caption(
            "真实地块数据库与模拟示例数据库彼此隔离。"
            if mode == "real"
            else "此处的输入只写入独立模拟数据库，不会进入真实地块。"
        )
        service, agent, database = _context(mode)
        field_id, state = _select_field(service, mode)
        st.caption(f"数据文件：`{database.name}`")
        if mode == "demo" and st.button("新建固定模拟地块", key="seed_demo"):
            new_id = _seed_demo(service)
            st.session_state[f"pending_field_selection:{mode}"] = new_id
            st.rerun()
    notice = st.session_state.pop(f"notice:{field_id}", None)
    if notice:
        st.success(notice)
    if state and state.plan_stale:
        st.warning("已保存事实或档案有更新；旧计划保留，等待重新预览和采用。")
    st.markdown(
        '<div class="workbench-note">规则候选处于<strong>开发验证，适用性待复核</strong>。'
        "天气用于查看风险，不替代现场确认；每块地单独排程，尚未统筹其他地块时间。</div>",
        unsafe_allow_html=True,
    )
    profile_tab, results_tab, assistant_tab = st.tabs(["地块与输入", "计划结果", "助手与历史"])
    with profile_tab:
        _save_draft_form(service, field_id, state, mode)
        current = _field_state(service, field_id)
        st.divider()
        _render_schedule_form(service, field_id, current)
        st.divider()
        _render_weather(service, agent, field_id, current)
    with results_tab:
        current = _field_state(service, field_id)
        _render_evaluation(service, field_id, current)
        st.divider()
        _render_plans(service, field_id, current)
        if current:
            st.divider()
            _render_feedback(service, field_id, current)
    with assistant_tab:
        current = _field_state(service, field_id)
        _render_agent(service, agent, field_id, current)
        st.divider()
        if current:
            st.markdown("#### 现场事件记录")
            for event in service.list_events(field_id):
                feedback = event.feedback
                if feedback:
                    st.write(
                        f"v{event.fact_version} · {feedback.kind} · "
                        f"{feedback.at.astimezone(ZONE):%Y-%m-%d %H:%M} · "
                        f"{feedback.field or feedback.task_id or feedback.day or ''} · "
                        f"{feedback.value if feedback.value is not None else ''}"
                    )
        _render_legacy_history(service, database, mode)
        if current:
            st.divider()
            _render_rule_reviews(service, field_id, current)


if __name__ == "__main__":
    main()
