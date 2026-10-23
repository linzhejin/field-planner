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
TERRAIN_OPTIONS = {
    "未知": None,
    "低洼": "lowland",
    "丘陵": "hilly",
    "平原": "flat",
    "其他": "other",
}
BOOL_OPTIONS = {"未知": None, "是": True, "否": False}
STAGE_OPTIONS = {"未知": None, "苗期": "苗期", "其他生育期": "其他生育期"}
CROP_OPTIONS = {"未知": None, "油菜": "油菜", "其他作物": "其他作物"}
WORKABILITY_OPTIONS = {"未知": None, "可作业": True, "不可作业": False}
APPLICABILITY_LABELS = {"matched": "适用", "out_of_scope": "超出范围", "pending": "待确认"}
NECESSITY_LABELS = {"required": "需要", "no_need": "不需要", "unknown": "未知"}
WINDOW_LABELS = {"available": "可安排", "unavailable": "暂不可安排", "pending": "待确认"}
SCHEDULE_LABELS = {
    "scheduled": "已安排",
    "unscheduled": "未排入",
    "pending": "待确认",
    "not_generated": "未生成",
    "completed": "已完成",
}
TASK_TYPE_LABELS = {"inspection": "巡田检查", "maintenance": "沟渠维护"}

_VIEWPORT_LAYOUT = st.components.v2.component(
    "workbench_viewport_layout",
    js="""
    export default function({ data, setStateValue }) {
      let compact = data?.compact ?? true;
      let timer;
      const mainEl = () =>
        document.querySelector('[data-testid="stMainBlockContainer"]') ||
        document.querySelector('.main') ||
        document.body;
      // 断点按“主内容区实际宽度”判断：侧栏开合只改变内容区宽度，
      // 故用 ResizeObserver 监听它，三栏才能随侧栏状态重排。
      const measure = () => {
        const el = mainEl();
        const width = el ? el.clientWidth : window.innerWidth;
        const nextCompact = width < 1100;
        if (compact !== nextCompact) {
          compact = nextCompact;
          setStateValue("compact", compact);
        }
      };
      const schedule = () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(measure, 100);
      };
      const observer = new ResizeObserver(schedule);
      const start = () => {
        const el = mainEl();
        if (el) observer.observe(el);
        measure();
      };
      if (document.readyState === "complete") {
        start();
      } else {
        window.addEventListener("load", start, { once: true });
      }
      window.addEventListener("resize", schedule);
      return () => {
        observer.disconnect();
        window.removeEventListener("resize", schedule);
        window.removeEventListener("load", start);
        window.clearTimeout(timer);
      };
    }
    """,
)

PALETTE = """
<style>
.stApp { background: #f5f6f1; color: #1d332b; font-size: 16px; }
[data-testid="stHeader"] { background: rgba(245,246,241,.94); height: 2.6rem; }
h1, h2, h3 { color: #173f32; letter-spacing: -.025em; }
/* 压缩顶部留白：收紧主区块内边距，并隐藏仅用于测量视口的自定义组件占位。 */
[data-testid="stMainBlockContainer"], .main .block-container {
  padding-top: 1.1rem !important; padding-bottom: 2.5rem !important; max-width: 100% !important; }
iframe[width="1"], iframe[height="1"] { display: none !important; }
[data-testid="stElementContainer"]:has(> iframe[width="1"]) { display: none !important; }
h1 { font-size: 1.7rem; margin: .1rem 0 .15rem; }
h2 { margin: .3rem 0 .2rem; }
h3 { margin: .5rem 0 .2rem; }
[data-testid="stButton"] button[kind="primary"] { background: #276653; border-color: #276653; }
[data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button { font-size: 16px; }
[data-testid="stVerticalBlockBorderWrapper"] { background: #ffffff; }
/* 侧栏不再硬撑宽度：交回 Streamlit 默认，内容区随开合自适应。 */
[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
  padding-left: 1rem; padding-right: 1rem; }
.workbench-note { border-left: 4px solid #b9984b; padding: .65rem .9rem;
  background: #edf1e7; border-radius: 0 .4rem .4rem 0; line-height: 1.55; }
.muted { color: #627369; font-size: .9rem; }
.status-chip { display: inline-block; padding: .1rem .55rem; border-radius: 999px;
  font-size: .8rem; font-weight: 600; vertical-align: middle; white-space: nowrap; }
.status-green { color: #1f5e46; background: #e2efe8; }
.status-amber { color: #7a5a13; background: #f3ead2; }
.status-red { color: #8a3b32; background: #f0ddd9; }
.status-neutral { color: #5b6b62; background: #eceee9; }
.count-number { font-size: 1.6rem; font-weight: 700; line-height: 1.2; }
.count-green { color: #1f5e46; }
.count-amber { color: #7a5a13; }
.count-neutral { color: #5b6b62; }
.stApp :focus-visible { outline: 2px solid #276653; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
div[data-testid="stTabs"] [data-baseweb="tab-list"] { gap: .5rem; }
/* 对话气泡限宽，避免窄栏里逐字换行造成的“细条”观感。 */
[data-testid="stChatMessage"] { max-width: 92%; }
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
    """解析当前选中地块（含待处理跳转）；不渲染控件，控件在左栏按设计顺序单独绘制。"""
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
    selected = st.session_state.get(key)
    field_id = _active_field_id(mode) if selected == NEW_FIELD else selected
    return field_id, _field_state(service, field_id)


def _render_field_selector(service: WorkbenchServiceV3, mode: str) -> None:
    """左栏「当前地块」下拉与「+ 新增地块」按钮；沿用原有控件 key。

    示意图只有一个地块时没有下拉：仅当存在两块及以上地块时才显示切换下拉。
    """
    fields = _saved_fields(service, mode)
    key = f"field_selection:{mode}"
    if len(fields) > 1:
        st.selectbox(
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
    if st.button("+ 新增地块", key=f"add_field:{mode}", width="stretch"):
        st.session_state[f"new_field_id:{mode}"] = f"field-{uuid4().hex[:10]}"
        st.session_state[f"pending_field_selection:{mode}"] = NEW_FIELD
        st.rerun()


def _enum_label(mapping: dict[str, object], value: object) -> str:
    return next((label for label, stored in mapping.items() if stored == value), "未知")


def _profile_from_form(previous: FieldProfile | None, values: dict[str, object]) -> FieldProfile:
    """主表单只渲染部分字段；已有档案时保留未渲染字段（维护前置、逐日作业条件、苗情观察等）。

    重新保存主表单不应清空这些字段（SYS-01）。
    """

    if previous is None:
        return FieldProfile(**values)
    return previous.model_copy(update=values)


def _render_field_form(
    service: WorkbenchServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
    mode: str,
) -> None:
    previous = state.draft if state else FieldDraftV3(field_id=field_id, mode=mode)  # type: ignore[arg-type]
    profile = previous.profile
    key_suffix = f"{field_id}:{state.draft_version if state else 0}"
    # 单一「下一步」：结果区已展示主按钮时，保存档案降为次级样式。
    pending_preview = bool(st.session_state.get(f"current_preview:{field_id}"))
    save_primary = _primary_action(pending_preview, profile is not None) == "save"
    st.subheader("地块档案")
    st.caption("首次使用从空白开始；未知信息保留为空，不会替你补值。")
    with st.form(f"field_form:{key_suffix}"):
        with st.expander("基本信息", expanded=True):
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
                index=crop_options.index(
                    _enum_label(CROP_OPTIONS, profile.crop if profile else None)
                ),
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
        with st.expander("田块情况", expanded=False):
            st.caption("以下田型事实只作档案记录，不影响候选规则判断。")
            rice_stubble_label = st.selectbox(
                "是否稻茬田",
                list(BOOL_OPTIONS),
                index=list(BOOL_OPTIONS).index(
                    _enum_label(BOOL_OPTIONS, profile.rice_stubble if profile else None)
                ),
                key=f"field_rice_stubble:{key_suffix}",
            )
            cold_waterlogged_label = st.selectbox(
                "是否冷浸田",
                list(BOOL_OPTIONS),
                index=list(BOOL_OPTIONS).index(
                    _enum_label(BOOL_OPTIONS, profile.cold_waterlogged if profile else None)
                ),
                key=f"field_cold_waterlogged:{key_suffix}",
            )
            terrain_label = st.selectbox(
                "地形",
                list(TERRAIN_OPTIONS),
                index=list(TERRAIN_OPTIONS).index(
                    _enum_label(TERRAIN_OPTIONS, profile.terrain if profile else None)
                ),
                key=f"field_terrain:{key_suffix}",
            )
            area_mu = st.number_input(
                "面积（亩，可留空）",
                min_value=0.0,
                value=float(profile.area_mu or 0) if profile else 0.0,
                step=0.5,
                key=f"field_area:{key_suffix}",
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
        with st.expander("任务与时间", expanded=False):
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
                    _enum_label(
                        BOOL_OPTIONS, profile.maintenance_need_confirmed if profile else None
                    )
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
            purpose_options = {
                "未知": None,
                "清理既有沟渠，改善已确认的连通问题": "clear_existing_ditch",
                "其他目的（本版暂不自动安排）": "other",
            }
            purpose_label = st.selectbox(
                "本次沟渠作业目的",
                list(purpose_options),
                index=list(purpose_options).index(
                    _enum_label(purpose_options, profile.maintenance_purpose if profile else None)
                ),
                key=f"maintenance_purpose:{key_suffix}",
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
            type="primary" if save_primary else "secondary",
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
            form_profile_values: dict[str, object] = {
                "id": field_id,
                "name": name.strip(),
                "province": province.strip() or None,
                "province_confirmed": province_confirmed,
                "crop": CROP_OPTIONS[crop_label],
                "crop_confirmed": crop_confirmed,
                "stage": STAGE_OPTIONS[stage_label],
                "stage_confirmed": stage_confirmed,
                "field_type": FIELD_TYPES[field_type_label],
                "rice_stubble": BOOL_OPTIONS[rice_stubble_label],
                "cold_waterlogged": BOOL_OPTIONS[cold_waterlogged_label],
                "terrain": TERRAIN_OPTIONS[terrain_label],
                "area_mu": area_mu or None,
                "inspection_requested": BOOL_OPTIONS[inspect_label],
                "maintenance_need_confirmed": BOOL_OPTIONS[maintenance_label],
                "maintenance_problem_confirmed": BOOL_OPTIONS[problem_label],
                "maintenance_purpose": purpose_options[purpose_label],
                "moisture": moisture_options[moisture],
                "ditch_connectivity": ditch_options[ditch],
                "standing_water": BOOL_OPTIONS[water],
                "leaf_count": leaf_count if leaf_known else None,
                "inspection_duration_minutes": inspection_minutes or None,
                "maintenance_duration_minutes": maintenance_minutes or None,
                "inspection_priority": inspection_priority,
                "maintenance_priority": maintenance_priority,
                "inspection_due_date": inspection_due if inspection_due_enabled else None,
                "maintenance_due_date": maintenance_due if maintenance_due_enabled else None,
                "observed_at": datetime.now(UTC) if has_observation else None,
                "observation_source": "observed" if has_observation else "synthetic",
            }
            updated_profile = _profile_from_form(profile, form_profile_values)
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


@st.dialog("编辑地块档案")
def _field_form_dialog(
    service: WorkbenchServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
    mode: str,
) -> None:
    """按需弹出档案表单：内容与空白优先的内联表单完全一致。"""
    _render_field_form(service, field_id, state, mode)


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
    # 时段格式错误就近显示：上次保存失败后暂存的按天错误，在本次渲染中提示。
    schedule_error_key = f"schedule_error:{field_id}:{state.draft_version}"
    schedule_error = st.session_state.pop(schedule_error_key, None)
    if schedule_error:
        st.error(schedule_error[0])
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
                if schedule_error and index in schedule_error[1]:
                    st.error("请修正时段格式：" + schedule_error[1][index])
                day_values.append((day_date, site, weather, int(minutes), interval_text))
        submitted = st.form_submit_button(
            "保存逐日条件和资源",
            type="secondary",
            width="stretch",
            key=f"save_schedule:{field_id}:{state.draft_version}",
        )
    if submitted:
        # 先按天校验时段格式：非法时把错误就近挂到对应日期，成功后正常保存。
        day_errors: dict[int, str] = {}
        if resources_enabled:
            for index, row in enumerate(day_values):
                try:
                    _parse_intervals(row[4])
                except ValueError as error:
                    day_errors[index] = str(error)
        if day_errors:
            st.session_state[schedule_error_key] = (
                "没有保存：请修正下方标出的可用时段格式。",
                day_errors,
            )
            st.rerun()
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
            st.session_state[schedule_error_key] = None
            st.session_state[f"notice:{field_id}"] = "逐日条件已保存；现场与天气仍按天分别判断。"
            st.rerun()
        except (ValueError, ValidationError) as error:
            st.error(f"没有保存：{error}")


@st.dialog("逐日作业条件与可用时段")
def _schedule_dialog(
    service: WorkbenchServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
) -> None:
    """按需弹出逐日条件与可用时段表单；内容与逐日折叠区一致。"""
    _render_schedule_form(service, field_id, state)


def _render_field_card(state: FieldStateV3) -> None:
    """左栏「我的地块」卡片：名称、作物·生育期·田型与保存状态。"""
    profile = state.draft.profile
    name = profile.name if profile and profile.name else f"未命名地块 · {state.id[-5:]}"
    crop = _enum_label(CROP_OPTIONS, profile.crop if profile else None)
    stage = _enum_label(STAGE_OPTIONS, profile.stage if profile else None)
    field_type = _enum_label(FIELD_TYPES, profile.field_type if profile else None)
    saved = profile is not None
    status = "当前地块 · 已保存" if saved else "当前地块 · 尚未保存"
    with st.container(border=True):
        st.markdown(f"**{name}**")
        st.caption(f"{crop} · {stage} · {field_type}")
        st.markdown(
            f'<span class="status-chip status-neutral">{status}</span>',
            unsafe_allow_html=True,
        )


def _render_availability_panel(state: FieldStateV3 | None) -> None:
    """左栏可用时间面板：一名执行者，汇总逐日可用时段。"""
    st.caption("一名执行者")
    days = state.draft.days if state else []
    if not days:
        st.caption("尚未录入可用时段")
        return
    signatures = {
        (tuple((window.start, window.end) for window in day.intervals), day.resource_minutes)
        for day in days
    }
    if len(signatures) != 1:
        st.caption("按日不同，见逐日条件")
        return
    shared = days[0]
    intervals_text = "；".join(
        f"{window.start:%H:%M}—{window.end:%H:%M}" for window in shared.intervals
    )
    minutes = shared.resource_minutes
    if intervals_text and minutes is not None:
        st.caption(f"每天 {intervals_text} · {minutes} 分钟")
    elif minutes is not None:
        st.caption(f"每天可用 {minutes} 分钟")
    elif intervals_text:
        st.caption(f"每天 {intervals_text}")
    else:
        st.caption("每天尚未录入时段")


def _weather_summary(service: WorkbenchServiceV3, field_id: str) -> str:
    """天气摘要行：查询位置 · 来源 · 获取时间 · 覆盖日期。"""
    snapshot = service.latest_weather(field_id)
    if snapshot is None:
        return "天气：尚未获取天气"
    return (
        f"天气：{snapshot.location.display_name} · {snapshot.source} · "
        f"获取 {snapshot.fetched_at.astimezone(ZONE):%Y-%m-%d %H:%M} · "
        f"覆盖 {snapshot.coverage_start:%m月%d日}—{snapshot.coverage_end:%m月%d日}"
    )


def _render_count_strip(items: list) -> None:  # type: ignore[type-arg]
    """中栏计数条：已安排/待确认/未排入三块白底卡片（数字在左、标签在右）。"""
    scheduled = sum(1 for item in items if item.schedule_status in {"scheduled", "completed"})
    pending = sum(1 for item in items if item.schedule_status == "pending")
    unscheduled = len(items) - scheduled - pending
    cards = [
        ("已安排", scheduled, "green"),
        ("待确认", pending, "amber"),
        ("未排入", unscheduled, "neutral"),
    ]
    columns = st.columns(3)
    for column, (label, count, tone) in zip(columns, cards, strict=True):
        with column.container(border=True):
            number_col, label_col = st.columns([1, 2], vertical_alignment="center")
            number_col.markdown(
                f'<div class="count-number count-{tone}">{count}</div>', unsafe_allow_html=True
            )
            label_col.markdown(
                f'<span class="status-chip status-{tone}">{label}</span>',
                unsafe_allow_html=True,
            )


@st.dialog("历史版本")
def _history_dialog(service: WorkbenchServiceV3, field_id: str) -> None:
    """顶栏历史版本：列出计划版本与变化，内容移自原中栏折叠区。"""
    history = service.plan_history(field_id)
    if not history:
        st.caption("尚无已采用的计划版本。")
        return
    for version in history:
        st.markdown(f"**{version.created_at.astimezone(ZONE):%Y-%m-%d %H:%M} · `{version.id}`**")
        if version.parent_id:
            st.caption(f"基于 `{version.parent_id}`")
        if version.changes:
            for change in version.changes:
                st.write(f"- {change.task_id}：{change.kind}；{change.reason}")
        else:
            st.caption("首版计划")


@st.dialog("导出计划")
def _export_dialog(service: WorkbenchServiceV3, field_id: str) -> None:
    """顶栏导出计划：Markdown 清单与 JSON 记录下载。"""
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


def _render_top_bar(
    service: WorkbenchServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
    mode: str,
) -> None:
    """顶栏信息条：农时窗口 · 日期范围·计划版本 / 模式标签 / 保存状态·历史·导出。"""
    draft = state.draft if state else None
    start = draft.start_date if draft else None
    if start:
        end = start + timedelta(days=6)
        if start.month == end.month:
            date_range = f"{start.month}月{start.day}日—{end.day}日"
        else:
            date_range = f"{start.month}月{start.day}日—{end.month}月{end.day}日"
    else:
        date_range = "未设置起始日"
    history = service.plan_history(field_id)
    version = f"V{len(history)}" if history else "未采用"
    saved = state is not None and state.draft.profile is not None
    has_plan = service.get_current_plan(field_id) is not None
    with st.container(border=True):
        left, middle, right = st.columns([3.4, 1.2, 2.4], vertical_alignment="center")
        with left:
            title_col, meta_col = st.columns([1.3, 2.1], vertical_alignment="center")
            title_col.markdown("### 农时窗口")
            meta_col.markdown(f"{date_range} · 计划 {version}")
        with middle:
            if mode == "real":
                st.markdown(
                    '<span class="status-chip status-neutral">真实地块</span>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<span class="status-chip status-amber">模拟地块与天气</span>',
                    unsafe_allow_html=True,
                )
        with right:
            status_col, history_col, export_col = st.columns(
                [0.9, 1, 1], vertical_alignment="center"
            )
            status_col.caption("已保存" if saved else "尚未保存")
            if history_col.button("历史版本", key="top_history", width="stretch"):
                _history_dialog(service, field_id)
            if export_col.button(
                "导出计划",
                key="top_export",
                disabled=not has_plan,
                width="stretch",
                help=None if has_plan else "采用计划后可导出",
            ):
                _export_dialog(service, field_id)


def _render_weather(
    service: WorkbenchServiceV3,
    agent: AgentServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
) -> None:
    st.subheader("天气与查询地点")
    if state is None:
        st.caption("保存地块后可以搜索查询点。")
        return
    query_key = f"weather_query:{field_id}"
    choice_key = f"geo_choice:{field_id}"
    with st.form(f"geo_search:{field_id}"):
        query = st.text_input(
            "输入地名或完整地址",
            key=query_key,
            placeholder="例如：湖北省武汉市洪山区，也可只输入洪山区",
        )
        search = st.form_submit_button("搜索地点")
    if search:
        st.session_state[f"geo_results:{field_id}"] = []
        st.session_state[choice_key] = None
        try:
            results = agent.weather.search_locations(query)
            st.session_state[f"geo_results:{field_id}"] = results
            if not results:
                st.warning(
                    "没有匹配到地点。可以补充县区或上级城市，例如“湖北省武汉市洪山区”；"
                    "湖北地名支持离线查询，其他地区会尝试在线搜索。"
                )
            else:
                st.success(f"找到 {len(results)} 个地点候选，请核对行政区和查询点级别。")
        except (ValueError, WeatherServiceError) as error:
            st.error(str(error))
    results: list[GeocodingResultV3] = st.session_state.get(f"geo_results:{field_id}", [])
    if results:
        choices = {result.id: result for result in results}
        resolution_labels = {
            "township": "乡镇级",
            "county": "县/区级",
            "city": "市级",
            "unknown": "普通地点（级别未核实）",
        }

        def format_location(result_id: str) -> str:
            result = choices[result_id]
            source = (
                "湖北离线地名库" if result.match_source == "local_gazetteer" else "在线地名服务"
            )
            level = resolution_labels[result.resolution]
            detail = f" · {result.match_reason}" if result.match_reason else ""
            return f"{result.label} · {level} · {source}{detail}"

        selected_id = st.selectbox(
            "选择天气查询点（必须手动选择）",
            options=list(choices),
            index=None,
            format_func=format_location,
            key=choice_key,
        )
        st.caption("地图查询点代表行政区附近位置，不能当作地块实测坐标。")
        if st.button(
            "确认所选地点", key=f"confirm_location:{field_id}", disabled=selected_id is None
        ):
            location = agent.weather.select_location(results, str(selected_id))
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
        resolution_labels = {
            "township": "乡镇级",
            "county": "县/区级",
            "city": "市级",
            "unknown": "级别未核实",
        }
        st.write(f"已选：{location.display_name} · {resolution_labels[location.resolution]}查询点")
        st.caption(
            f"查询位置 {location.query} · {location.geocoding_source} · "
            f"坐标 {location.latitude:.4f}, {location.longitude:.4f} · Asia/Shanghai"
        )
        start = state.draft.start_date or (_today() + timedelta(days=1))
        end = start + timedelta(days=6)
        query_col, refresh_col = st.columns(2)
        should_query = query_col.button("查询七天天气", key=f"fetch_weather:{field_id}")
        should_refresh = refresh_col.button("刷新天气", key=f"refresh_weather:{field_id}")
        if should_query or should_refresh:
            try:
                snapshot = agent.weather.fetch_forecast(
                    field_id,
                    location,
                    start,
                    end,
                    force_refresh=should_refresh,
                )
                if should_refresh:
                    st.session_state.pop(f"weather_refresh_error:{field_id}", None)
                st.session_state[f"notice:{field_id}"] = f"已获取 {len(snapshot.days)} 天天气数据。"
                st.rerun()
            except (ValueError, WeatherServiceError) as error:
                if should_refresh:
                    st.session_state[f"weather_refresh_error:{field_id}"] = str(error)
                st.error(f"天气查询失败：{error}")
        snapshot = service.latest_weather(
            field_id, location=location, start_date=start, end_date=end
        )
        if snapshot:
            refresh_error = st.session_state.get(f"weather_refresh_error:{field_id}")
            same_location = (
                snapshot.requested_latitude == location.latitude
                and snapshot.requested_longitude == location.longitude
            )
            same_range = snapshot.coverage_start == start and snapshot.coverage_end == end
            if not same_location or not same_range:
                with st.expander("查看上次天气（位置或日期与当前输入不同）"):
                    _render_weather_snapshot(snapshot, stale=True)
            else:
                _render_weather_snapshot(snapshot, stale=snapshot.is_stale or bool(refresh_error))
            if refresh_error:
                st.warning(f"天气刷新失败，下面仅保留上次查询结果：{refresh_error}")
        else:
            st.caption("尚无当前地点和日期的天气数据。")


def _render_weather_snapshot(snapshot, *, stale: bool) -> None:  # type: ignore[no-untyped-def]
    state_label = "过期天气，仅作历史参考" if stale else "七天预报"
    st.caption(
        f"{state_label} · {snapshot.source} · 查询点 {snapshot.location.display_name} · "
        f"覆盖 {snapshot.coverage_start:%Y-%m-%d} 至 {snapshot.coverage_end:%Y-%m-%d} · "
        f"获取于 {snapshot.fetched_at.astimezone(ZONE):%Y-%m-%d %H:%M} · "
        f"预报网格 {snapshot.grid_latitude:.3f}, {snapshot.grid_longitude:.3f}"
    )
    st.info("天气预报只展示风险信息，不自动表示田间积水、道路通畅或可以进田。")
    for day in snapshot.days:
        temperature = (
            f"{day.temperature_min:g}–{day.temperature_max:g} °C"
            if day.temperature_min is not None and day.temperature_max is not None
            else "气温缺失"
        )
        rain = (
            f"降水 {day.precipitation_sum:g} mm"
            if day.precipitation_sum is not None
            else "降水缺失"
        )
        wind = (
            f"最大风速 {day.wind_speed_max:g} km/h"
            if day.wind_speed_max is not None
            else "风速缺失"
        )
        st.write(f"{day.day:%m月%d日} · {temperature} · {rain} · {wind}")


def _render_evidence(item) -> None:  # type: ignore[no-untyped-def]
    if not item.evidence:
        return
    catalog = load_rule_catalog()
    with st.expander("规则依据与来源"):
        rendered_rules: set[str] = set()
        for evidence in item.evidence:
            st.markdown(f"**{evidence.rule_id}/{evidence.source_id} · {evidence.title}**")
            st.caption(
                f"{evidence.publisher} · {evidence.source_date or '日期未提供'} · "
                f"页码 {', '.join(map(str, evidence.pdf_pages)) or '未提供'} · "
                f"{evidence.status_label} · runtime_enabled={str(evidence.runtime_enabled).lower()}"
            )
            st.write(f"适用条件：{evidence.applicability_text or '未提供'}")
            st.write(f"来源背景：{evidence.source_context_summary}")
            st.write(f"本项目用途：{evidence.project_use_limit}")
            st.write(f"限制：{evidence.limits or '未提供'}")
            if evidence.quote:
                st.write(f"原文：{evidence.quote}")
            st.caption(f"来源指纹：{evidence.source_sha256}")
            if evidence.source_url:
                st.link_button("打开来源", evidence.source_url)
            if evidence.rule_id in rendered_rules:
                continue
            rendered_rules.add(evidence.rule_id)
            _render_supplementary_sources(catalog.supplementary_for([evidence.rule_id]))


def _render_supplementary_sources(rows: list) -> None:  # type: ignore[type-arg]
    """显示 A04 补充来源；单独标识，不改写原卡适用性或启用状态。"""
    for source in rows:
        st.markdown(f"↳ {source['source_id']} · {source['source_title']}")
        st.caption(
            f"{source['publisher']} · {source['source_date'] or '日期未提供'} · "
            f"{source['locator']} · 访问日 {source['access_date'] or '未提供'}"
        )
        st.caption(f"用途限制：{source['use'] or '未提供'}")
        st.caption(f"{source['status_label']} · 指纹 {source['source_sha256']}")
        if source["source_url"]:
            st.link_button("打开补充来源", source["source_url"])


def _status_tone(item) -> str:  # type: ignore[no-untyped-def]
    """按排程结果给卡片配色：绿=正常、琥珀=待确认、灰红=未排入。"""
    if item.schedule_status in {"scheduled", "completed"}:
        return "green"
    if item.schedule_status == "unscheduled":
        return "red"
    if item.schedule_status == "pending":
        return "amber"
    return "neutral"


def _overall_window(item) -> str | None:  # type: ignore[no-untyped-def]
    states = set(item.window_by_date.values())
    for candidate in ("available", "pending", "unavailable"):
        if candidate in states:
            return candidate
    return None


def _primary_action(has_pending_preview: bool, has_profile: bool) -> str:
    """单一「下一步」归属：采用预览 > 生成/重算计划 > 保存档案。"""
    if has_pending_preview:
        return "adopt"
    if has_profile:
        return "plan"
    return "save"


def _item_detail_rows(item) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    """整理任务详情行；纯函数，不做任何 Streamlit 调用，便于单元测试。"""
    type_label = TASK_TYPE_LABELS.get(item.task_type, item.task_type)
    if item.start_at and item.end_at:
        when = (
            f"{item.start_at.astimezone(ZONE):%Y-%m-%d %H:%M}–{item.end_at.astimezone(ZONE):%H:%M}"
        )
    else:
        when = "未排入"
    window = _overall_window(item)
    rows = [
        ("任务", f"{item.title} · {type_label}"),
        ("编号", item.task_id),
        ("日期与时间", when),
        ("预计时长", f"{item.duration_minutes} 分钟" if item.duration_minutes else "未提供"),
        ("窗口", WINDOW_LABELS.get(window, "未提供") if window else "未提供"),
        ("适用性", APPLICABILITY_LABELS.get(item.applicability, item.applicability)),
        ("需求", NECESSITY_LABELS.get(item.necessity, item.necessity)),
        ("执行状态", SCHEDULE_LABELS.get(item.schedule_status, item.schedule_status)),
        ("安排原因", item.reason),
        ("待确认项", "、".join(item.missing_fields) if item.missing_fields else "无"),
    ]
    adjustment = getattr(item, "adjustment_reason", None)
    if adjustment:
        rows.append(("调整原因", str(adjustment)))
    return rows


def _render_item(item, *, scope: str) -> None:  # type: ignore[no-untyped-def]
    """任务卡主区只显示摘要；完整字段移入「查看详情」对话框。"""
    label = SCHEDULE_LABELS.get(item.schedule_status, item.schedule_status)
    tone = _status_tone(item)
    with st.container(border=True):
        st.markdown(
            f'**{item.title}**　<span class="status-chip status-{tone}">{label}</span>',
            unsafe_allow_html=True,
        )
        if item.start_at and item.end_at:
            st.write(
                f"{item.start_at.astimezone(ZONE):%Y-%m-%d %H:%M}–"
                f"{item.end_at.astimezone(ZONE):%H:%M} · {item.duration_minutes} 分钟"
            )
        st.write(item.reason)
        if item.missing_fields:
            st.caption("待补信息：" + "、".join(item.missing_fields[:6]))
        # scope 用于区分候选、预览与采用版中重名的任务编号，避免按钮 key 冲突。
        if st.button("查看详情", key=f"detail:{scope}:{item.task_id}"):
            _task_detail_dialog(item)


@st.dialog("任务详情")
def _task_detail_dialog(item) -> None:  # type: ignore[no-untyped-def]
    """模态展示任务完整字段与依据；关闭后草稿、事实与计划版本均不变。"""
    for name, value in _item_detail_rows(item):
        st.markdown(f"**{name}**：{value}")
    st.divider()
    if item.evidence:
        _render_evidence(item)
    else:
        st.caption("当前资料不足以支持这项建议")


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
    profile = state.draft.profile
    standing_water = profile.standing_water if profile else None
    for event in service._active_events(field_id):
        feedback = event.feedback
        if feedback and feedback.kind == "observation" and feedback.field == "standing_water":
            standing_water = feedback.value if isinstance(feedback.value, bool) else None
    if standing_water is True:
        st.warning(
            "田里仍有积水记录：若积水持续或加重，请先现场核实或联系当地农技人员；"
            "系统不替代现场处置。"
        )
    if evaluation.clarification_questions:
        st.warning("优先核对：" + "；".join(evaluation.clarification_questions[:3]))
    if evaluation.items:
        for item in evaluation.items:
            _render_item(item, scope="candidate")
    else:
        st.info("当前没有候选事项。")
    if evaluation.missing_fields:
        st.caption("其他缺项：" + "、".join(evaluation.missing_fields[:12]))


def _show_preview(
    service: WorkbenchServiceV3, preview: PlanPreviewV3, *, scope: str = "results"
) -> None:
    """scope 用于区分结果区与助手区展示同一预览时的按钮 key。"""
    st.markdown("#### 七天计划预览")
    st.caption(
        f"依据：档案 v{preview.expected_draft_version} · "
        f"现场事实 v{preview.expected_fact_version} · 单地块计算，"
        "尚未统筹其他地块时间，不代表合并后可执行",
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
        _render_item(item, scope=f"{scope}-preview-{preview.id}")
    if preview.status == "preview":
        confirm_key = f"confirm_cancel:{preview.id}"
        if st.session_state.get(confirm_key):
            st.warning("取消这版预览后需要重新计算，确定取消吗？")
            keep, discard = st.columns(2)
            if keep.button("保留预览", key=f"keep_preview:{preview.id}"):
                st.session_state[confirm_key] = False
                st.rerun()
            if discard.button("确定取消预览", key=f"cancel_yes:{preview.id}"):
                service.cancel_preview(preview.id)
                st.session_state[f"current_preview:{preview.field_id}"] = None
                st.session_state[confirm_key] = False
                st.session_state.pop(f"preview_signature:{preview.field_id}", None)
                st.rerun()
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
                st.session_state.pop(f"preview_signature:{preview.field_id}", None)
                st.session_state[f"notice:{preview.field_id}"] = (
                    "计划已采用并保存新版本。"
                    if adopted.created_new_version
                    else "安排无变化；事实已更新，没有重复创建计划版本。"
                )
                st.rerun()
            except ValueError as error:
                st.error(f"预览已失效：{error}。请重新计算。")
        if right.button("取消预览", key=f"cancel:{preview.id}"):
            st.session_state[confirm_key] = True
            st.rerun()
    else:
        st.info(f"预览状态：{preview.status}")


def _render_plans(service: WorkbenchServiceV3, field_id: str, state: FieldStateV3 | None) -> None:
    """中栏「今日与后续安排」：待决预览与已采用版的排程卡片。"""
    if state is None or state.draft.profile is None:
        return
    if state.plan_stale:
        st.warning("现场事实或档案已变化，当前采用版需要重新预览。旧版仍保留。")
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
            _render_item(item, scope=f"adopted-{adopted.id}")


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


def _reset_observation_value(field_id: str, fact_version: int) -> None:
    st.session_state[f"observation_value:{field_id}:{fact_version}"] = "未知"


def _render_feedback(
    service: WorkbenchServiceV3, field_id: str, state: FieldStateV3 | None
) -> None:
    st.subheader("记录现场事实与资源变化")
    # 右栏快捷动作预选反馈类型：弹出对应表单组。
    kind = st.session_state.pop(f"feedback_kind:{field_id}", None)
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
        with st.expander("记录任务完成", expanded=(kind == "completed")):
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
        "维护目的": (
            "maintenance_purpose",
            {
                "未知": None,
                "清理既有沟渠": "clear_existing_ditch",
                "其他目的": "other",
            },
        ),
        "是否有实际降雨": ("actual_rain_observed", BOOL_OPTIONS),
        "虫害迹象": ("pest_signs", BOOL_OPTIONS),
        "缺苗情况": ("missing_seedlings", BOOL_OPTIONS),
        "拥挤情况": ("crowding", BOOL_OPTIONS),
    }
    with st.expander("补充现场观察", expanded=(kind == "observation")):
        label = st.selectbox(
            "观察项目",
            list(observation_fields),
            key=f"observation_field:{field_id}:{state.fact_version}",
            on_change=_reset_observation_value,
            args=(field_id, state.fact_version),
        )
        field, choices = observation_fields[label]
        with st.form(f"observation:{field_id}:{state.fact_version}"):
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
    with st.expander("修改当天作业条件", expanded=(kind == "workability")):
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
    with st.expander("修改可用时间", expanded=(kind == "resource")):
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


def _proposal_event_description(event: FactEventInputV3) -> str:
    feedback = event.feedback
    if feedback is None:
        return f"撤回或更正原事件 {event.correction_of or '未指定'}"
    at = feedback.at.astimezone(ZONE).strftime("%Y-%m-%d %H:%M")
    if feedback.kind == "completed":
        detail = f"记录任务 {feedback.task_id} 完成"
    elif feedback.kind == "observation":
        field = {
            "standing_water": "积水情况",
            "ditch_connectivity": "沟渠连通",
            "pest_signs": "虫情迹象",
            "maintenance_need_confirmed": "维护需求",
            "maintenance_problem_confirmed": "维护问题",
            "maintenance_purpose": "维护目的",
        }.get(feedback.field or "", feedback.field or "观察项")
        value = (
            "未知"
            if feedback.value is None
            else "是"
            if feedback.value is True
            else "否"
            if feedback.value is False
            else str(feedback.value)
        )
        detail = f"记录地块 {feedback.plot_id} 的{field}：{value}"
    elif feedback.kind == "workability":

        def state(value: bool | None) -> str:
            return "未知" if value is None else "可作业" if value else "不可作业"

        detail = (
            f"将 {feedback.day} 的现场条件设为{state(feedback.site_workable)}，"
            f"天气条件设为{state(feedback.weather_workable)}"
        )
    else:
        intervals = (
            "；".join(
                f"{window.start:%H:%M}–{window.end:%H:%M}" for window in (feedback.intervals or [])
            )
            or "无可用时段"
        )
        minutes = (
            "总分钟数未填写"
            if feedback.resource_minutes is None
            else f"{feedback.resource_minutes} 分钟"
        )
        detail = f"将 {feedback.day} 的人员可用时间改为 {intervals}，{minutes}"
    return f"{detail}（反馈时间 {at}）"


def _quick_feedback_dialog(service: WorkbenchServiceV3, field_id: str, kind: str) -> None:
    """在窄屏助手页签直接呈现可操作的反馈表单。"""
    state = _field_state(service, field_id)
    if state is None:
        st.info("请先保存地块档案。")
        return
    if kind == "observation":
        fields = {
            "沟渠连通": ("ditch_connectivity", {"未知": None, "通畅": "clear", "受阻": "impaired"}),
            "积水情况": ("standing_water", BOOL_OPTIONS),
            "维护需求": ("maintenance_need_confirmed", BOOL_OPTIONS),
            "维护问题": ("maintenance_problem_confirmed", BOOL_OPTIONS),
            "维护目的": (
                "maintenance_purpose",
                {"未知": None, "清理既有沟渠": "clear_existing_ditch", "其他目的": "other"},
            ),
        }
        label = st.selectbox("观察项目", list(fields), key=f"quick_observation_field:{field_id}")
        field, choices = fields[label]
        with st.form(f"quick_observation_form:{field_id}:{state.fact_version}"):
            value_label = st.selectbox(
                "记录值", list(choices), key=f"quick_observation_value:{field_id}:{label}"
            )
            submitted = st.form_submit_button(
                "保存观察事实", key=f"quick_save_observation:{field_id}:{state.fact_version}"
            )
        if submitted:
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
    elif kind == "completed":
        current = service.get_current_plan(field_id)
        eligible = (
            [
                item
                for item in current.items
                if item.start_at and not item.completed and item.start_at.astimezone(ZONE) <= _now()
            ]
            if current
            else []
        )
        if not eligible:
            st.info("当前没有已到计划开始时间且尚未完成的任务。")
            return
        with st.form(f"quick_completion_form:{field_id}:{state.fact_version}"):
            task = st.selectbox("已完成的已安排任务", eligible, format_func=lambda item: item.title)
            submitted = st.form_submit_button("记录完成")
        if submitted:
            try:
                _commit_feedback(
                    service, state, FeedbackV2(kind="completed", task_id=task.task_id, at=_now())
                )
                st.rerun()
            except ValueError as error:
                st.error(f"完成事实未保存：{error}")
    elif kind == "resource":
        start = state.draft.start_date or (_today() + timedelta(days=1))
        with st.form(f"quick_resource_form:{field_id}:{state.fact_version}"):
            day = st.date_input(
                "资源变化日期", value=start, min_value=start, max_value=start + timedelta(days=6)
            )
            minutes = st.number_input("人员可用分钟数", min_value=0, max_value=1440, step=15)
            intervals = st.text_input("可用时段", placeholder="09:00-11:00;13:00-15:00")
            submitted = st.form_submit_button("保存资源变化")
        if submitted:
            try:
                _commit_feedback(
                    service,
                    state,
                    FeedbackV2(
                        kind="resource",
                        day=day,
                        resource_minutes=int(minutes),
                        intervals=_parse_intervals(intervals),
                        at=_now(),
                    ),
                )
                st.rerun()
            except (ValueError, ValidationError) as error:
                st.error(f"资源变化未保存：{error}")


def _render_quick_actions(service: WorkbenchServiceV3, field_id: str) -> None:
    """右栏快捷动作：在当前页签展开对应反馈表单。"""
    if st.button("补充现场观察", key=f"quick_observe:{field_id}", type="primary", width="stretch"):
        st.session_state[f"quick_feedback_kind:{field_id}"] = "observation"
    if st.button("记录任务完成", key=f"quick_complete:{field_id}", width="stretch"):
        st.session_state[f"quick_feedback_kind:{field_id}"] = "completed"
    if st.button("修改可用时间", key=f"quick_resource:{field_id}", width="stretch"):
        st.session_state[f"quick_feedback_kind:{field_id}"] = "resource"
    kind = st.session_state.get(f"quick_feedback_kind:{field_id}")
    if kind:
        with st.container(border=True):
            _quick_feedback_dialog(service, field_id, kind)


def _render_chat_input(agent: AgentServiceV3, field_id: str, history: list[dict[str, str]]) -> None:
    """对话输入区：用 chat_input 取代窄栏里被压成细长条的 text_area。"""
    user_message = st.chat_input(
        "例如：明天只能干一小时",
        key=f"assistant_input:{field_id}",
    )
    if user_message:
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


def _render_agent(
    service: WorkbenchServiceV3,
    agent: AgentServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
) -> None:
    profile = state.draft.profile if state else None
    name = profile.name if profile and profile.name else "未命名地块"
    # 页签名已含「农事助手」，此处不再重复大标题。
    st.caption(f"当前对话 · {name}")
    if state is None or profile is None:
        st.info("先保存地块档案，再与助手对话。结构化表单不依赖模型密钥。")
        return
    history_key = f"chat:{field_id}"
    history: list[dict[str, str]] = st.session_state.setdefault(history_key, [])
    for message in history[-20:]:
        with st.chat_message(message["role"]):
            st.write(message["content"])
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
            if proposal.kind == "facts":
                for index, event in enumerate(proposal.events, start=1):
                    st.markdown(f"{index}. {_proposal_event_description(event)}")
                with st.expander("核对完整反馈字段"):
                    st.json(
                        [
                            event.model_dump(mode="json", exclude_none=True)
                            for event in proposal.events
                        ]
                    )
            else:
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
            _show_preview(service, preview, scope="agent")
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
    with st.expander("A04 补充来源登记（只作追溯，不代表原卡启用）"):
        st.caption("补充来源单独标识为 A04-Sxx；不覆盖原卡、原来源索引或原件指纹。")
        for rule_id, source_ids in service.catalog.supplementary_links.items():
            if not source_ids:
                continue
            st.markdown(f"**{rule_id}**")
            for source in service.catalog.supplementary_for([rule_id]):
                st.caption(
                    f"{source['source_id']} · {source['source_title']} · {source['locator']}"
                )
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
    st.set_page_config(
        page_title="农时窗口 · 农事工作台",
        page_icon="🌱",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(PALETTE, unsafe_allow_html=True)
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
        st.caption(f"数据文件：`{database.name}`")
        if mode == "demo" and st.button("新建固定模拟地块", key="seed_demo"):
            new_id = _seed_demo(service)
            st.session_state[f"pending_field_selection:{mode}"] = new_id
            st.rerun()

    compact = _VIEWPORT_LAYOUT(
        key="workbench_viewport_layout",
        data={"compact": True},
        default={"compact": True},
        on_compact_change=lambda: None,
        width=1,
        height=1,
    ).compact
    top_slot = st.empty()

    def render_fields() -> tuple[str, FieldStateV3 | None]:
        st.markdown("### 我的地块")
        field_id, field_state = _select_field(service, mode)
        notice = st.session_state.pop(f"notice:{field_id}", None)
        if notice:
            st.success(notice)
            # 保存成功后关闭仍在打开的编辑对话框。
            st.session_state.pop(f"field_form_open:{field_id}", None)
            st.session_state.pop(f"schedule_open:{field_id}", None)
        has_profile = field_state is not None and field_state.draft.profile is not None
        if has_profile:
            _render_field_card(field_state)
        # 当前地块下拉紧贴卡片下方；「+ 新增地块」紧随其后。
        _render_field_selector(service, mode)
        st.caption("一块地也能独立使用")
        st.caption("新增后填写新地块信息")
        st.caption("各地块分别保存结果")
        if not has_profile:
            # 空白优先：首次进入或新增地块时直接显示空白表单，无需对话框。
            _render_field_form(service, field_id, field_state, mode)
            return field_id, field_state
        if st.button("编辑当前地块", key=f"edit_field:{field_id}", width="stretch"):
            st.session_state[f"field_form_open:{field_id}"] = True
            st.rerun()
        st.markdown("#### 可用时间")
        _render_availability_panel(field_state)
        if st.button("调整可用时段", key=f"edit_schedule:{field_id}", width="stretch"):
            st.session_state[f"schedule_open:{field_id}"] = True
            st.rerun()
        st.caption("地块与事实随计划保存")
        st.caption("开发验证，适用性待复核")
        if st.session_state.get(f"field_form_open:{field_id}"):
            _field_form_dialog(service, field_id, field_state, mode)
        if st.session_state.get(f"schedule_open:{field_id}"):
            _schedule_dialog(service, field_id, field_state)
        return field_id, field_state

    def render_results(field_id: str) -> None:
        current = _field_state(service, field_id)
        if current and current.plan_stale:
            st.warning("已保存事实或档案有更新；旧计划保留，等待重新预览和采用。")
        profile = current.draft.profile if current else None
        name = profile.name if profile and profile.name else "未命名地块"
        header_left, header_right = st.columns([2.9, 1.5], vertical_alignment="center")
        header_left.markdown(f"#### {name} · 结果")
        if current and profile is not None:
            preview_id = st.session_state.get(f"current_preview:{field_id}")
            plan_primary = _primary_action(bool(preview_id), True) == "plan"
            if header_right.button(
                "重新计算计划",
                key=f"preview_plan:{field_id}",
                type="primary" if plan_primary else "secondary",
                width="stretch",
            ):
                signature = (current.draft_version, current.fact_version)
                last_signature = st.session_state.get(f"preview_signature:{field_id}")
                if preview_id and last_signature == signature:
                    # 防重复提交：同一档案与事实版本已生成预览，不再重复创建。
                    st.caption("已按当前档案与事实生成预览，未重复计算。")
                else:
                    try:
                        preview = service.preview_plan(
                            field_id, operation_id=f"manual-preview-{uuid4()}"
                        )
                        st.session_state[f"current_preview:{field_id}"] = preview.id
                        st.session_state[f"preview_signature:{field_id}"] = signature
                    except (ValueError, KeyError) as error:
                        st.error(f"无法生成预览：{error}")
        st.caption(_weather_summary(service, field_id))
        items: list = []
        adopted = service.get_current_plan(field_id)
        if adopted:
            items = adopted.items
        elif current and profile is not None:
            items = service.evaluate_field(field_id).items
        _render_count_strip(items)
        st.markdown("#### 今日与后续安排")
        _render_plans(service, field_id, current)
        st.caption("以上为当前地块结果；依据可在任务详情查看。")
        st.caption("有其他地块？点击左侧「新增地块」继续。")
        with st.expander("天气与查询位置", expanded=False):
            _render_weather(service, agent, field_id, current)
        with st.expander("候选与依据", expanded=False):
            _render_evaluation(service, field_id, current)
        with st.expander("反馈与历史", expanded=False):
            if current:
                _render_feedback(service, field_id, current)

    def render_assistant(field_id: str) -> None:
        current = _field_state(service, field_id)
        # 右栏内容用子页签分隔，避免全部竖排造成“细长条”。
        agent_tab, recent_tab = st.tabs(["农事助手", "最近操作"])
        with agent_tab:
            _render_agent(service, agent, field_id, current)
            _render_quick_actions(service, field_id)
        with recent_tab:
            if current:
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
        # 输入区放在右栏底部（chat_input，固定高度）。
        history = st.session_state.setdefault(f"chat:{field_id}", [])
        _render_chat_input(agent, field_id, history)

    if compact:
        fields_tab, results_tab, assistant_tab = st.tabs(["地块与资源", "计划结果", "助手与历史"])
        with fields_tab:
            field_id, field_state = render_fields()
        with results_tab:
            render_results(field_id)
        with assistant_tab:
            render_assistant(field_id)
        with top_slot.container():
            _render_top_bar(service, field_id, field_state, mode)
    else:
        left, middle, right = st.columns([2.5, 5.0, 3.5], gap="medium")
        with left:
            with st.container(border=True):
                field_id, current = render_fields()
        with middle:
            with st.container(border=True):
                render_results(field_id)
        with right:
            with st.container(border=True):
                render_assistant(field_id)
        with top_slot.container():
            _render_top_bar(service, field_id, current, mode)
        if current:
            with st.expander("规则复核记录（开发用）", expanded=False):
                _render_rule_reviews(service, field_id, current)


if __name__ == "__main__":
    main()
