"""Persistent, blank-first Streamlit workbench for the single-field Agent flow."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st
from pydantic import ValidationError

from nongshi.agent import AgentServiceV3
from nongshi.agent_jobs import AgentJob
from nongshi.config import Settings
from nongshi.presentation import (
    CHANGE_LABELS,
    field_value_label,
    missing_label,
    proposal_change_rows,
    task_title,
)
from nongshi.repository import PlanStore, PlanStoreV2
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import DayWindow, FeedbackV2, FieldProfile, TimeWindow
from nongshi.schema_v3 import (
    AgentProposalV3,
    FactEventInputV3,
    FieldDraftV3,
    FieldEvaluationV3,
    FieldStateV3,
    GeocodingResultV3,
    PlanPreviewV3,
    WeatherDayV3,
    WeatherLocationV3,
    WeatherSnapshotV3,
)
from nongshi.service_v3 import WorkbenchServiceV3, align_draft_dates
from nongshi.weather import WeatherServiceError

ZONE = ZoneInfo("Asia/Shanghai")
NEW_FIELD = "__new_field__"
FIELD_TYPES = {
    "未填写": None,
    "低洼/稻茬田": "lowland_rice_stubble",
    "丘陵旱地": "dry_hilly",
    "其他": "other",
}
TERRAIN_OPTIONS = {
    "未填写": None,
    "低洼": "lowland",
    "丘陵": "hilly",
    "平原": "flat",
    "其他": "other",
}
BOOL_OPTIONS = {"暂不确定": None, "是": True, "否": False}
MAINTENANCE_NEED_OPTIONS = {"暂不确定": None, "需要": True, "不需要": False}
MAINTENANCE_PROBLEM_OPTIONS = {"暂不确定": None, "有": True, "没有": False}
STAGE_OPTIONS = {"未填写": None, "苗期": "苗期", "其他生育期": "其他生育期"}
CROP_OPTIONS = {"未填写": None, "油菜": "油菜", "其他作物": "其他作物"}
WORKABILITY_OPTIONS = {"暂不确定": None, "可作业": True, "不可作业": False}
APPLICABILITY_LABELS = {"matched": "适用", "out_of_scope": "超出范围", "pending": "待确认"}
NECESSITY_LABELS = {"required": "需要", "no_need": "不需要", "unknown": "待确认"}
WINDOW_LABELS = {"available": "可安排", "unavailable": "暂不可安排", "pending": "待确认"}
SCHEDULE_LABELS = {
    "scheduled": "已安排",
    "unscheduled": "未排入",
    "pending": "待确认",
    "not_generated": "未生成",
    "completed": "已完成",
    "cancelled": "已取消",
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
        const nextCompact = width < 1000;
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
.stApp { background:#f4f5ef; color:#253e35; font-size: 16px;
  font-family:"Microsoft YaHei UI","PingFang SC","Noto Sans CJK SC",sans-serif; }
[data-testid="stHeader"], [data-testid="stDecoration"] { display:none !important; }
[data-testid="stMainBlockContainer"], .main .block-container {
  padding:12px 26px 12px !important; max-width:1440px !important; }
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"]
  > [data-testid="stElementContainer"]:has(style),
.st-key-workbench_viewport_layout {
  position:absolute !important; width:0 !important; height:0 !important;
  overflow:hidden !important; }
h1,h2,h3 { color:#253e35 !important; letter-spacing:-.02em; }
h2 { font-size:24px !important; margin:0 !important; }
h3 { font-size:18px !important; margin:0 !important; }
h4 { font-size:16px !important; margin:12px 0 5px !important; }
.st-key-workbench_header { background:#fff; border:1px solid #dce3d8;
  border-radius:10px; padding:9px 13px; margin-bottom:-3px; min-height:62px; }
.st-key-workbench_header [data-testid="stHorizontalBlock"] { align-items:center; }
.st-key-workbench_header h2 { white-space:nowrap; }
.st-key-workbench_header p { margin:0; white-space:nowrap; }
.header-date { font-size:14px; color:#3e544a; }
.header-mode { display:inline-block; background:#fff5dd; color:#80622a;
  border:1px solid #e7dfca; border-radius:9px; padding:5px 10px; font-size:14px; }
.header-mode-real { background:#edf3ed; color:#29513d; }
.st-key-workbench_header [data-testid="stPopover"] button {
  min-height:30px; width:170px; justify-content:flex-start;
  background:#fff5dd; color:#80622a; border-color:#e7dfca; }
.header-status { color:#42564b; font-size:14px; }
.st-key-workbench_columns {
  --workbench-panel-height:calc(100dvh - 101px);
  /* 22px covers the panel's 7px top, 13px bottom, and 2px border. */
  --workbench-panel-content-height:calc(var(--workbench-panel-height) - 22px); }
.st-key-workbench_columns > [data-testid="stLayoutWrapper"]
  > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
  height:var(--workbench-panel-height) !important;
  min-height:var(--workbench-panel-height) !important;
  max-height:var(--workbench-panel-height) !important;
  overflow:hidden; box-sizing:border-box; background:#fff;
  border:1px solid #dce3d8; border-radius:10px; padding:7px 12px 13px; }
.st-key-field_rail,.st-key-result_rail,.st-key-assistant_rail {
  height:var(--workbench-panel-content-height) !important;
  max-height:var(--workbench-panel-content-height);
  min-height:var(--workbench-panel-content-height) !important;
  overflow-y:auto !important; box-sizing:border-box; background:transparent;
  border:0; border-radius:0; padding:0; gap:6px !important; scrollbar-width:thin; }
.st-key-assistant_rail { display:flex; flex-direction:column; }
.st-key-assistant_rail > [data-testid="stLayoutWrapper"]:has(.st-key-assistant_scroll) {
  flex:1 1 auto; min-height:0; overflow:hidden; }
.st-key-assistant_rail > [data-testid="stLayoutWrapper"]:has(.st-key-assistant_compose) {
  flex:0 0 auto; }
.st-key-assistant_scroll { height:100% !important; min-height:0;
  overflow-y:auto !important; gap:0 !important; }
.st-key-assistant_compose { background:#fff; padding-top:7px;
  border-top:1px solid #e5e9e1; }
.st-key-assistant_compose textarea { background:#fff !important;
  border:1px solid #dce3d8 !important; }
.st-key-compact_workbench .st-key-field_rail,
.st-key-compact_workbench .st-key-result_rail,
.st-key-compact_workbench .st-key-assistant_rail {
  height:auto !important; max-height:none !important; min-height:0 !important;
  overflow:visible !important; padding:12px; }
.st-key-compact_workbench .st-key-assistant_rail { display:block; }
.st-key-compact_workbench .st-key-assistant_scroll {
  height:auto !important; max-height:none !important; overflow:visible !important; }
.st-key-compact_workbench .st-key-assistant_compose { position:static; }
.st-key-compact_workbench .st-key-assistant_rail .workbench-empty {
  min-height:0; line-height:1.55; }
.st-key-field_rail [data-testid="stButton"] button,
.st-key-assistant_rail [data-testid="stButton"] button,
.st-key-workbench_header [data-testid="stButton"] button {
  min-height:31px; padding:3px 10px; font-size:14px; border-radius:8px;
  background:#f1f4ee; border:1px solid #dce3d8; color:#274638; }
.st-key-result_rail [data-testid="stButton"] button {
  min-height:31px; padding:3px 10px; font-size:14px; border-radius:8px; }
[data-testid="stButton"] button[kind="primary"],
button[kind="primary"],button[data-testid="stBaseButton-primary"],
button[kind="primaryFormSubmit"] {
  background:#25634b !important; border-color:#25634b !important; color:#fff !important; }
[data-testid="stButton"] button:disabled { color:#758077; background:#f1f3ef; }
[data-testid="stVerticalBlockBorderWrapper"] {
  background:#fff; border-color:#dce3d8; border-radius:9px; }
[data-testid="stCaptionContainer"] { color:#4d6055; font-size:14px; }
.status-chip { display:inline-block; padding:2px 7px; border-radius:6px;
  font-size:13px; font-weight:600; vertical-align:middle; }
.status-green { color:#245b43; background:#eaf3e9; }
.status-amber { color:#775514; background:#fff2d5; }
.status-red { color:#8a3b32; background:#f0ddd9; }
.status-neutral { color:#506052; background:#eff1ed; }
.count-strip { display:grid; grid-template-columns:repeat(3,minmax(0,1fr));
  gap:10px; margin:8px 0 13px; }
.count-item { display:flex; align-items:center; gap:13px; border-radius:9px;
  border:1px solid #dce3d8; padding:9px 11px; min-width:0; }
.count-item.green { background:#edf5eb; }
.count-item.amber { background:#fff5dd; }
.count-item.neutral { background:#f4f1ee; }
.count-number { font-size:24px; font-weight:700; line-height:1.1; }
.count-label { font-size:14px; color:#354e40; white-space:nowrap; }
.workbench-note { border-left:4px solid #b9984b; padding:8px 11px;
  background:#edf1e7; border-radius:0 7px 7px 0; line-height:1.5; }
.workbench-summary { background:#f1f6ef; border:1px solid #dce3d8;
  border-radius:9px; padding:9px 11px; margin:8px 0; line-height:1.5; }
.workbench-summary strong { display:block; }
.workbench-summary span { color:#44584a; font-size:14px; }
.workbench-empty { background:#f8faf6; border:1px solid #dce3d8;
  border-radius:9px; padding:11px 13px; margin:7px 0; }
.st-key-assistant_rail .workbench-empty {
  min-height:158px; line-height:1.7; margin:0; }
.st-key-assistant_rail [data-testid="stMarkdownContainer"]:has(.workbench-empty) {
  margin-bottom:0 !important; }
.reference-question { background:#fff8e7; border:1px solid #e8dfc9;
  border-radius:9px; padding:10px 12px; margin:8px 0 14px; line-height:1.5; }
.reference-question strong { display:block; }
.reference-question span { color:#4f5e50; font-size:14px; }
[class*="st-key-task_scheduled_"],[class*="st-key-task_completed_"] {
  background:#f8faf6; border:1px solid #dce3d8; border-radius:9px;
  padding:8px 10px 11px; margin:7px 0; min-height:78px; }
[class*="st-key-task_pending_"] {
  background:#fff8e7; border:1px solid #e8dfc9; border-radius:9px;
  padding:8px 10px; margin:11px 0; }
[class*="st-key-task_unscheduled_"] {
  background:#f4f1ee; border:1px solid #e4ded8; border-radius:9px;
  padding:8px 10px; margin:11px 0; }
[class*="st-key-task_"] [data-testid="stVerticalBlockBorderWrapper"] {
  background:transparent; border:none; padding:0; }
[class*="st-key-task_"] p { margin:0; }
[class*="st-key-task_"] [data-testid="stCaptionContainer"] {
  margin-bottom:0 !important; }
.field-hint { font-size:13px; line-height:1.8; color:#42584a; margin-bottom:8px; }
.field-hint p { margin:0; }
.st-key-field_onboarding { max-width:820px; margin-inline:auto; background:#fff; }
[data-testid="stChatMessage"] { max-width:100%; }
.stApp :focus-visible { outline:2px solid #25634b; outline-offset:2px; }
@media (prefers-reduced-motion:reduce) {
  *,*::before,*::after { animation:none !important; transition:none !important; }
}
[role="tab"][aria-selected="true"] { color:#25634b !important; }
[role="tab"][aria-selected="true"] .react-aria-SelectionIndicator {
  background:#25634b !important; border-color:#25634b !important; }
@media (max-width:700px) {
  [data-testid="stMainBlockContainer"] { padding:9px 12px 24px !important; }
  .st-key-workbench_header { min-height:0; }
  .st-key-workbench_header p { white-space:normal; }
  .st-key-workbench_header [data-testid="stPopover"] button { width:100%; }
}
</style>
"""


def _now() -> datetime:
    return datetime.now(ZONE)


def _today() -> date:
    return _now().date()


def _dismiss_dialog() -> None:
    st.session_state.pop("workbench_dialog", None)


def _editor_buffer(kind: str, field_id: str, state: FieldStateV3 | None) -> dict:
    key = f"edit_buffer:{kind}:{field_id}"
    buffer = st.session_state.setdefault(
        key, {"state": state, "values": {}, "baseline": None, "dirty": False}
    )
    if not buffer["dirty"] and buffer["state"] != state:
        buffer = {"state": state, "values": {}, "baseline": None, "dirty": False}
        st.session_state[key] = buffer
    for widget_key, value in buffer["values"].items():
        if widget_key not in st.session_state:
            st.session_state[widget_key] = value
    return buffer


def _capture_editor(buffer: dict, kind: str, field_id: str, version: int) -> None:
    marker = f":{field_id}:{version}"
    if kind == "field":
        marker += _editor_reset_suffix(kind, field_id)
    prefixes = (
        "site:",
        "weather:",
        "minutes:",
        "intervals:",
        "resources_enabled:",
        "resource_known:",
        "resource_touched:",
        "basis:",
    )
    values = {
        key: st.session_state[key]
        for key in st.session_state
        if (
            (kind == "field" and key.endswith(marker) and not key.startswith(("save_", *prefixes)))
            or (kind == "schedule" and marker in key and key.startswith(prefixes))
        )
    }
    if buffer["baseline"] is None:
        buffer["baseline"] = values.copy()
    buffer["values"] = values
    buffer["dirty"] = values != buffer["baseline"]
    if buffer["dirty"]:
        st.caption("有未保存修改 · 关闭窗口或切换地块会保留本次编辑。")


def _has_unsaved_edits(field_id: str) -> bool:
    return any(
        st.session_state.get(f"edit_buffer:{kind}:{field_id}", {}).get("dirty", False)
        for kind in ("field", "schedule")
    )


def _clear_editor(kind: str, field_id: str) -> None:
    st.session_state.pop(f"edit_buffer:{kind}:{field_id}", None)


def _discard_editor(kind: str, field_id: str) -> None:
    buffer = st.session_state.get(f"edit_buffer:{kind}:{field_id}", {})
    for key in buffer.get("values", {}):
        st.session_state.pop(key, None)
    _clear_editor(kind, field_id)
    generation_key = f"editor_generation:{kind}:{field_id}"
    st.session_state[generation_key] = st.session_state.get(generation_key, 0) + 1
    # The editor is a dialog fragment, but the saved/unsaved label lives outside it.
    st.rerun(scope="app")


def _render_discard(kind: str, field_id: str, buffer: dict) -> None:
    if buffer["dirty"]:
        st.button(
            "放弃未保存修改，重新载入",
            key=f"discard:{kind}:{field_id}",
            on_click=_discard_editor,
            args=(kind, field_id),
        )


def _open_dialog(kind: str, field_id: str, **values: object) -> None:
    st.session_state["workbench_dialog"] = {"kind": kind, "field_id": field_id, **values}


def _render_open_dialog(service: WorkbenchServiceV3, mode: str) -> None:
    request = st.session_state.get("workbench_dialog")
    if not request:
        return
    field_id = request["field_id"]
    state = _field_state(service, field_id)
    kind = request["kind"]
    if kind == "field":
        _field_form_dialog(service, field_id, state, mode)
    elif kind == "schedule":
        _schedule_dialog(service, field_id, state)
    elif kind == "history":
        _history_dialog(service, field_id)
    elif kind == "export":
        _export_dialog(service, field_id)
    elif kind == "task":
        _task_detail_dialog(service, field_id, request["item"], request.get("scope", ""))
    elif kind == "observation":
        focus_labels = {
            "ditch_connectivity": "沟渠连通",
            "standing_water": "积水情况",
            "maintenance_need_confirmed": "维护需求",
            "maintenance_problem_confirmed": "维护问题",
            "maintenance_purpose": "维护目的",
        }
        label = focus_labels.get(request.get("focus"))
        if label:
            st.session_state[f"quick_observation_field:{field_id}"] = label
        _observation_dialog(service, field_id)


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
    if len(fields) > 1 or (fields and st.session_state.get(key) == NEW_FIELD):
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
        st.session_state[f"previous_field:{mode}"] = st.session_state.get(key)
        if not _has_unsaved_edits(_active_field_id(mode)):
            st.session_state[f"new_field_id:{mode}"] = f"field-{uuid4().hex[:10]}"
        st.session_state[f"pending_field_selection:{mode}"] = NEW_FIELD
        st.rerun()
    if fields and st.session_state.get(key) == NEW_FIELD:
        if st.button("返回已保存地块", key=f"cancel_new_field:{mode}", width="stretch"):
            previous = st.session_state.get(f"previous_field:{mode}")
            valid_ids = {field.id for field in fields}
            st.session_state[f"pending_field_selection:{mode}"] = (
                previous if previous in valid_ids else fields[-1].id
            )
            st.rerun()


def _enum_label(mapping: dict[str, object], value: object) -> str:
    return next(
        (label for label, stored in mapping.items() if stored == value), next(iter(mapping))
    )


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
    *,
    initial_setup: bool = False,
) -> None:
    buffer = _editor_buffer("field", field_id, state)
    current_version = state.draft_version if state else 0
    state = buffer["state"]
    previous = state.draft if state else FieldDraftV3(field_id=field_id, mode=mode)  # type: ignore[arg-type]
    profile = previous.profile
    version_suffix = f"{field_id}:{state.draft_version if state else 0}"
    key_suffix = version_suffix + _editor_reset_suffix("field", field_id)
    # 单一「下一步」：结果区已展示主按钮时，保存档案降为次级样式。
    pending_preview = bool(st.session_state.get(f"current_preview:{field_id}"))
    save_primary = _primary_action(pending_preview, profile is not None) == "save"
    if not initial_setup:
        st.subheader("地块档案")
    st.caption(
        "先写地块名称即可保存，其他情况可以稍后补充。"
        if initial_setup
        else "先填确定的情况，拿不准的可以暂时留空。"
    )
    if current_version != (state.draft_version if state else 0):
        st.warning("档案已被其他操作更新。本次编辑仍保留，保存时会检查冲突。")
    initial_submitted = False
    if initial_setup:
        name = st.text_input(
            "地块名称",
            value=profile.name if profile else "",
            max_chars=80,
            key=f"field_name:{key_suffix}",
        )
        name_error_slot = st.empty()
        initial_submitted = st.button(
            "保存地块档案",
            type="primary",
            width="stretch",
            key=f"save_field:{version_suffix}",
        )
        st.caption("保存后可继续补充现场情况与可用时间，再查看七天安排。")
    with st.container(border=not initial_setup):
        focus = st.session_state.get("workbench_dialog", {}).get("focus", "")
        with st.expander("基本信息", expanded=not initial_setup):
            if not initial_setup:
                name_column, province_column = st.columns(2)
                name = name_column.text_input(
                    "地块名称",
                    value=profile.name if profile else "",
                    max_chars=80,
                    key=f"field_name:{key_suffix}",
                )
                name_error_slot = name_column.empty()
            else:
                province_column = st
            province = province_column.text_input(
                "省份",
                value=profile.province or "" if profile else "",
                key=f"field_province:{key_suffix}",
            )
            province_confirmed = province_column.checkbox(
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
        with st.expander(
            "田块情况",
            expanded=focus
            in {"field_type", "ditch_connectivity", "standing_water", "moisture", "leaf_count"},
        ):
            st.caption("下面几项仅作地块记录。")
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
            moisture_options = {"暂不确定": None, "偏干": "dry", "适中": "normal", "偏湿": "wet"}
            moisture = st.selectbox(
                "土壤墒情",
                list(moisture_options),
                index=list(moisture_options).index(
                    _enum_label(moisture_options, profile.moisture if profile else None)
                ),
                key=f"field_moisture:{key_suffix}",
            )
            ditch_options = {"暂不确定": None, "通畅": "clear", "受阻": "impaired"}
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
        with st.expander("任务与时间", expanded=focus.startswith(("inspection", "maintenance"))):
            inspect_label = st.selectbox(
                "是否需要巡田",
                list(BOOL_OPTIONS),
                index=list(BOOL_OPTIONS).index(
                    _enum_label(BOOL_OPTIONS, profile.inspection_requested if profile else None)
                ),
                key=f"inspection_requested:{key_suffix}",
            )
            maintenance_label = st.selectbox(
                "要安排沟渠维护吗？",
                list(MAINTENANCE_NEED_OPTIONS),
                index=list(MAINTENANCE_NEED_OPTIONS).index(
                    _enum_label(
                        MAINTENANCE_NEED_OPTIONS,
                        profile.maintenance_need_confirmed if profile else None,
                    )
                ),
                key=f"maintenance_need:{key_suffix}",
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
                "补充说明",
                value=previous.notes,
                key=f"field_notes:{key_suffix}",
            )
        with st.expander(
            "沟渠情况",
            expanded=MAINTENANCE_NEED_OPTIONS[maintenance_label] is True
            or focus in {"maintenance_problem_confirmed", "maintenance_purpose"},
        ):
            st.caption("有积水或沟渠受阻时，请根据现场情况判断是否需要处理。")
            problem_label = st.selectbox(
                "沟渠有需要处理的堵塞或排水问题吗？",
                list(MAINTENANCE_PROBLEM_OPTIONS),
                index=list(MAINTENANCE_PROBLEM_OPTIONS).index(
                    _enum_label(
                        MAINTENANCE_PROBLEM_OPTIONS,
                        profile.maintenance_problem_confirmed if profile else None,
                    )
                ),
                key=f"maintenance_problem:{key_suffix}",
            )
            purpose_options = {
                "暂不确定": None,
                "清理沟渠，改善排水": "clear_existing_ditch",
                "其他目的（暂不自动安排）": "other",
            }
            purpose_label = st.selectbox(
                "维护沟渠是为了什么？",
                list(purpose_options),
                index=list(purpose_options).index(
                    _enum_label(purpose_options, profile.maintenance_purpose if profile else None)
                ),
                key=f"maintenance_purpose:{key_suffix}",
            )
        submitted_below = st.button(
            "保存已填写信息" if initial_setup else "保存地块档案",
            type="secondary" if initial_setup else "primary" if save_primary else "secondary",
            width="stretch",
            key=(
                f"save_field_below:{version_suffix}"
                if initial_setup
                else f"save_field:{version_suffix}"
            ),
        )

    submitted = initial_submitted or submitted_below
    _capture_editor(buffer, "field", field_id, state.draft_version if state else 0)
    _render_discard("field", field_id, buffer)

    if submitted:
        if not name.strip():
            name_error_slot.error("请填写地块名称，再保存。")
            return
        try:
            observed_values = (
                moisture_options[moisture],
                ditch_options[ditch],
                BOOL_OPTIONS[water],
                leaf_count if leaf_known else None,
                MAINTENANCE_PROBLEM_OPTIONS[problem_label],
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
                "maintenance_need_confirmed": MAINTENANCE_NEED_OPTIONS[maintenance_label],
                "maintenance_problem_confirmed": MAINTENANCE_PROBLEM_OPTIONS[problem_label],
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
            draft = align_draft_dates(previous, start_date=start_date).model_copy(
                update={
                    "profile": updated_profile,
                    "start_date": start_date,
                    "notes": notes,
                    "mode": mode,
                }
            )
            with service.store.connection_budget(1.0):
                saved = service.save_field_draft(
                    draft,
                    expected_version=state.draft_version if state else None,
                    operation_id=f"draft-{uuid4()}",
                )
            st.session_state[f"pending_field_selection:{mode}"] = saved.id
            st.session_state[f"notice:{saved.id}"] = "地块档案已保存。"
            _clear_editor("field", field_id)
            _dismiss_dialog()
            st.rerun()
        except ValidationError:
            st.error("没有保存：请检查填写内容后重试；已输入的信息仍保留。")
        except ValueError as error:
            message = str(error)
            if any("\u4e00" <= character <= "\u9fff" for character in message):
                st.error(f"没有保存：{message}")
            else:
                LOGGER.exception("Unexpected non-localized value error while saving the field")
                st.error(
                    "没有保存：系统遇到暂时性问题，已输入的信息仍保留。请重试；若问题持续，请刷新页面。"
                )
        except sqlite3.Error:
            LOGGER.exception("Failed to save field draft to the local database")
            st.error("没有保存：本地数据暂时无法写入，请稍后重试；已输入的信息仍保留。")
        except Exception:
            LOGGER.exception("Unexpected error while saving the field draft")
            st.error(
                "没有保存：系统遇到暂时性问题，已输入的信息仍保留。请重试；若问题持续，请刷新页面。"
            )


@st.dialog("编辑地块档案", width="large", on_dismiss=_dismiss_dialog)
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


def _mark_resource_touched(suffix: str) -> None:
    st.session_state[f"resource_touched:{suffix}"] = True


def _render_batch_schedule(
    field_id: str, version: int, start: date, reset_suffix: str = ""
) -> None:
    with st.expander("按日期范围批量填写", expanded=False):
        st.caption("只修改你选中的日期；先应用到编辑草稿，核对后再保存。")
        left, right = st.columns(2)
        first = left.date_input(
            "从哪天开始",
            value=start,
            min_value=start,
            max_value=start + timedelta(days=6),
            key=f"batch_start:{field_id}:{version}{reset_suffix}",
        )
        last = right.date_input(
            "到哪天结束",
            value=start,
            min_value=start,
            max_value=start + timedelta(days=6),
            key=f"batch_end:{field_id}:{version}{reset_suffix}",
        )
        options = ["保持原值", *WORKABILITY_OPTIONS]
        site = st.selectbox("批量现场条件", options, key=f"batch_site:{field_id}{reset_suffix}")
        weather = st.selectbox(
            "批量天气条件", options, key=f"batch_weather:{field_id}{reset_suffix}"
        )
        basis = st.text_input(
            "确认依据",
            placeholder="例如：现场观察；已核实的作业天气条件",
            key=f"batch_basis:{field_id}{reset_suffix}",
        )
        change_time = st.checkbox(
            "同时设置这些日期的可用时段", key=f"batch_time:{field_id}{reset_suffix}"
        )
        intervals = st.text_input(
            "这些日期的具体时段",
            placeholder="09:00-11:00;13:00-15:00",
            disabled=not change_time,
            key=f"batch_intervals:{field_id}{reset_suffix}",
        )
        limit = st.number_input(
            "每日工时上限（分钟；0 表示按时段合计）",
            min_value=0,
            max_value=1440,
            value=0,
            step=15,
            disabled=not change_time,
            key=f"batch_limit:{field_id}{reset_suffix}",
        )
        st.caption(f"待应用范围：{first:%m月%d日} — {last:%m月%d日}；范围外日期保持原值。")
        if st.button("应用到编辑草稿", key=f"apply_batch:{field_id}{reset_suffix}"):
            try:
                if first > last:
                    raise ValueError("结束日期需要不早于开始日期")
                if (site != "保持原值" or weather != "保持原值") and not basis.strip():
                    raise ValueError("请填写这段日期的确认依据；拿不准时请选择“暂不确定”")
                windows = _parse_intervals(intervals) if change_time else []
                if change_time and not windows:
                    raise ValueError("请填写具体可用时段")
                # Validate overlap before changing any widget value.
                if change_time:
                    DayWindow(
                        day=first, site_workable=None, weather_workable=None, intervals=windows
                    )
                for index in range(7):
                    day = start + timedelta(days=index)
                    if not first <= day <= last:
                        continue
                    suffix = f"{field_id}:{version}:{index}"
                    if site != "保持原值":
                        st.session_state[f"site:{suffix}"] = site
                    if weather != "保持原值":
                        st.session_state[f"weather:{suffix}"] = weather
                    if site != "保持原值" or weather != "保持原值":
                        st.session_state[f"basis:{suffix}"] = (
                            f"{basis.strip()}（{_now():%m-%d %H:%M} 确认）"
                        )
                    if change_time:
                        minutes = sum(window.minutes for window in windows)
                        st.session_state[f"minutes:{suffix}"] = (
                            min(minutes, int(limit)) if limit else minutes
                        )
                        st.session_state[f"intervals:{suffix}"] = intervals.strip()
                        st.session_state[f"resource_known:{suffix}"] = True
                        _mark_resource_touched(suffix)
                        st.session_state[
                            f"resources_enabled:{field_id}:{version}{reset_suffix}"
                        ] = True
                st.success("已应用到下方编辑草稿，请核对后保存。")
            except ValueError as error:
                st.error(str(error))


def _render_schedule_form(
    service: WorkbenchServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
) -> None:
    st.subheader("逐日作业条件与可用时段")
    if state is None:
        st.info("先保存地块档案，再录入逐日资源。")
        return
    buffer = _editor_buffer("schedule", field_id, state)
    state = buffer["state"]
    draft = state.draft
    reset_suffix = _editor_reset_suffix("schedule", field_id)
    start_date = draft.start_date or (_today() + timedelta(days=1))
    old_days = {entry.day: entry for entry in draft.days}
    default_days = [old_days.get(start_date + timedelta(days=i)) for i in range(7)]
    _render_discard("schedule", field_id, buffer)
    _render_batch_schedule(field_id, state.draft_version, start_date, reset_suffix)
    resources_default = any(day and day.resource_minutes is not None for day in default_days)
    resources_enabled = st.checkbox(
        "编辑可用分钟数和具体时段",
        value=resources_default,
        key=f"resources_enabled:{field_id}:{state.draft_version}{reset_suffix}",
    )
    st.caption("逐日确认后才会保存人员时间；关闭编辑不会清除原有记录。")
    # 时段格式错误就近显示：上次保存失败后暂存的按天错误，在本次渲染中提示。
    schedule_error_key = f"schedule_error:{field_id}:{state.draft_version}"
    schedule_error = st.session_state.pop(schedule_error_key, None)
    if schedule_error:
        st.error(schedule_error[0])
    with st.container(border=True):
        st.caption("现场能否进田、天气是否适合作业分别确认；预报本身不会自动成为作业确认。")
        day_values: list[tuple[date, str, str, int, str, bool]] = []
        for index in range(7):
            day_date = start_date + timedelta(days=index)
            previous = default_days[index]
            suffix = f"{field_id}:{state.draft_version}:{index}{reset_suffix}"
            with st.container(border=True):
                st.markdown(f"**{day_date:%Y年%m月%d日}**")
                site_value = previous.site_workable if previous else None
                weather_value = previous.weather_workable if previous else None
                site_options = list(WORKABILITY_OPTIONS)
                site = st.selectbox(
                    "现场条件",
                    site_options,
                    index=site_options.index(_enum_label(WORKABILITY_OPTIONS, site_value)),
                    key=f"site:{suffix}",
                )
                weather = st.selectbox(
                    "天气条件",
                    site_options,
                    index=site_options.index(_enum_label(WORKABILITY_OPTIONS, weather_value)),
                    key=f"weather:{suffix}",
                )
                known_key = f"resource_known:{suffix}"
                if known_key not in st.session_state:
                    st.session_state[known_key] = bool(
                        previous and (previous.resource_minutes is not None or previous.intervals)
                    )
                known = st.checkbox(
                    "已确认当天人员时间",
                    key=known_key,
                    disabled=not resources_enabled,
                    on_change=_mark_resource_touched,
                    args=(suffix,),
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
                    key=f"minutes:{suffix}",
                    disabled=not resources_enabled or not known,
                    on_change=_mark_resource_touched,
                    args=(suffix,),
                )
                interval_default = ";".join(
                    f"{window.start:%H:%M}-{window.end:%H:%M}"
                    for window in (previous.intervals if previous else [])
                )
                interval_text = st.text_input(
                    "可用时段（多个用分号隔开）",
                    value=interval_default,
                    placeholder="例如 09:00-11:00;13:00-15:00",
                    key=f"intervals:{suffix}",
                    disabled=not resources_enabled or not known,
                    on_change=_mark_resource_touched,
                    args=(suffix,),
                )
                if schedule_error and index in schedule_error[1]:
                    st.error("请修正时段格式：" + schedule_error[1][index])
                basis_key = f"basis:{suffix}"
                if basis_key not in st.session_state:
                    st.session_state[basis_key] = previous.confirmation_basis if previous else ""
                if st.session_state[basis_key]:
                    st.caption("确认依据：" + st.session_state[basis_key])
                day_values.append((day_date, site, weather, int(minutes), interval_text, known))
        submitted = st.button(
            "保存逐日条件和资源",
            type="secondary",
            width="stretch",
            key=f"save_schedule:{field_id}:{state.draft_version}",
        )
    _capture_editor(buffer, "schedule", field_id, state.draft_version)
    if submitted:
        # 先按天校验时段格式：非法时把错误就近挂到对应日期，成功后正常保存。
        day_errors: dict[int, str] = {}
        if resources_enabled:
            for index, row in enumerate(day_values):
                suffix = f"{field_id}:{state.draft_version}:{index}{reset_suffix}"
                if not row[5] or not st.session_state.get(f"resource_touched:{suffix}"):
                    continue
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
            days = []
            for index, (day_date, site, weather, minutes, interval_text, known) in enumerate(
                day_values
            ):
                previous = default_days[index]
                touched = resources_enabled and st.session_state.get(
                    f"resource_touched:{field_id}:{state.draft_version}:{index}{reset_suffix}"
                )
                if touched:
                    resource_minutes = minutes if known else None
                    resource_intervals = _parse_intervals(interval_text) if known else []
                else:
                    resource_minutes = previous.resource_minutes if previous else None
                    resource_intervals = previous.intervals if previous else []
                days.append(
                    DayWindow(
                        day=day_date,
                        site_workable=WORKABILITY_OPTIONS[site],
                        weather_workable=WORKABILITY_OPTIONS[weather],
                        resource_minutes=resource_minutes,
                        intervals=resource_intervals,
                        confirmation_basis=st.session_state.get(
                            f"basis:{field_id}:{state.draft_version}:{index}{reset_suffix}", ""
                        ),
                    )
                )
            draft = state.draft.model_copy(update={"start_date": start_date, "days": days})
            service.save_field_draft(
                draft,
                expected_version=state.draft_version,
                operation_id=f"schedule-{uuid4()}",
            )
            st.session_state[schedule_error_key] = None
            st.session_state[f"notice:{field_id}"] = "逐日条件已保存；现场与天气仍按天分别判断。"
            _clear_editor("schedule", field_id)
            _dismiss_dialog()
            st.rerun()
        except (ValueError, ValidationError) as error:
            st.error(f"没有保存：{error}")


@st.dialog("逐日作业条件与可用时段", width="large", on_dismiss=_dismiss_dialog)
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
    """Short weather provenance for the result heading; details remain expandable."""
    snapshot = service.latest_weather(field_id)
    if snapshot is None:
        return "天气：尚未获取天气"
    return f"天气：{snapshot.source} {snapshot.fetched_at.astimezone(ZONE):%m月%d日 %H:%M}"


def _render_count_strip(items: list) -> None:  # type: ignore[type-arg]
    """Compact, text-labelled counts for the currently displayed result source."""
    scheduled = sum(1 for item in items if item.schedule_status == "scheduled")
    completed = sum(1 for item in items if item.schedule_status == "completed")
    pending = sum(1 for item in items if item.schedule_status == "pending")
    unscheduled = sum(1 for item in items if item.schedule_status == "unscheduled")
    cards = [
        ("已安排", scheduled, "green"),
        ("待确认", pending, "amber"),
        ("未排入", unscheduled, "neutral"),
    ]
    st.markdown(
        '<div class="count-strip">'
        + "".join(
            f'<div class="count-item {tone}">'
            f'<span class="count-number count-{tone}">{count}</span>'
            f'<span class="count-label">{label}</span></div>'
            for label, count, tone in cards
        )
        + "</div>",
        unsafe_allow_html=True,
    )
    if completed:
        st.caption(f"已完成 {completed} 项，原安排保留在计划记录中。")


@st.dialog("历史版本", width="large", on_dismiss=_dismiss_dialog)
def _history_dialog(service: WorkbenchServiceV3, field_id: str) -> None:
    """Read one immutable version and restore it only through a new validated preview."""
    history = service.plan_history(field_id)
    if not history:
        st.caption("尚无已采用的计划版本。")
        return
    by_id = {version.id: version for version in history}
    selected_id = st.selectbox(
        "选择历史版本",
        list(by_id),
        format_func=lambda value: (
            f"{history.index(by_id[value]) + 1} 版 · "
            f"{by_id[value].created_at.astimezone(ZONE):%m月%d日 %H:%M}"
        ),
        key=f"history_version:{field_id}",
    )
    version = by_id[selected_id]
    current = service.get_current_plan(field_id)
    st.caption("历史计划仅供查看。恢复时会用当前地块情况重新核对。")
    st.markdown("#### 当时的完整安排")
    _render_week(
        version.items,
        version.plan.request.start_date,
        scope=f"history-{version.id}",
        compact_empty=False,
    )
    st.markdown("#### 与上一版的变化")
    if version.changes:
        by_task = {item.task_id: item for item in version.items}
        for change in version.changes:
            name = (
                task_title(by_task[change.task_id].task_type)
                if change.task_id in by_task
                else "任务"
            )
            old = (
                change.old_start_at.astimezone(ZONE).strftime("%m月%d日 %H:%M")
                if change.old_start_at
                else "未安排"
            )
            new = (
                change.new_start_at.astimezone(ZONE).strftime("%m月%d日 %H:%M")
                if change.new_start_at
                else "未排入"
            )
            st.write(
                f"{name} · {CHANGE_LABELS.get(change.kind, '调整')}：{old} → {new}；{change.reason}"
            )
    else:
        st.caption("首版计划，尚无上一版可比较。")
    with st.expander("当时的输入与依据（技术记录）"):
        st.write("地块档案版本", version.draft_version, "现场记录版本", version.fact_version)
        st.write("规则资料版本", version.ruleset_version)
        st.write("天气快照编号", version.weather_snapshot_id or "未使用天气快照")
        st.json(version.plan.request.model_dump(mode="json"))
        for basis in version.calculation_basis:
            st.write(basis)
    if current and current.id != version.id:
        if st.button("按当前情况重新预览", key=f"restore_history:{version.id}"):
            try:
                preview = service.preview_restore_plan(
                    field_id, version.id, operation_id=f"restore-{uuid4()}"
                )
                st.session_state[f"current_preview:{field_id}"] = preview.id
                st.session_state[f"notice:{field_id}"] = (
                    "已按当前情况重新预览。请核对后决定是否采用。"
                )
                _dismiss_dialog()
                st.rerun()
            except (ValueError, KeyError) as error:
                st.error(f"未能恢复：{error}。请核对当前地块情况后重试。")


@st.dialog("导出计划", on_dismiss=_dismiss_dialog)
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
    *,
    compact: bool,
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
    mode_text = "真实地块" if mode == "real" else "模拟地块与天气"
    status = "有未保存修改" if _has_unsaved_edits(field_id) else "已保存" if saved else "尚未保存"

    def mode_picker(column) -> None:  # type: ignore[no-untyped-def]
        with column.popover(mode_text, key="top_mode_switch", width="stretch"):
            st.radio(
                "数据空间",
                ["真实地块", "独立模拟示例"],
                horizontal=False,
                key="workspace_mode",
                on_change=_remember_workspace_mode,
            )
            st.caption("模拟示例与真实地块分别保存。")
            if mode == "demo" and st.button("新建模拟示例", key="seed_demo"):
                new_id = _seed_demo(service)
                st.session_state["pending_field_selection:demo"] = new_id
                st.rerun()

    def version_actions(history_col, export_col) -> None:  # type: ignore[no-untyped-def]
        if not saved:
            return
        if history_col.button("历史版本", key="top_history", width="stretch"):
            _open_dialog("history", field_id)
        if has_plan:
            if export_col.button("导出计划", key="top_export", width="stretch"):
                _open_dialog("export", field_id)
        else:
            export_col.caption("采用后可导出")

    with st.container(key="workbench_header"):
        if compact:
            title_col, mode_col = st.columns(
                [1.1, 1.4], gap="small", vertical_alignment="center", wrap=False
            )
            title_col.markdown("## 农时窗口")
            mode_picker(mode_col)
            st.markdown(
                f'<span class="header-date">{date_range} · 计划 {version} · {status}</span>',
                unsafe_allow_html=True,
            )
            history_col, export_col = st.columns(
                2, gap="small", vertical_alignment="center", wrap=False
            )
            version_actions(history_col, export_col)
        else:
            title_col, date_col, mode_col, status_col, history_col, export_col = st.columns(
                [1.25, 2.35, 3.1, 0.65, 0.85, 1.15],
                gap="small",
                vertical_alignment="center",
                wrap=False,
            )
            title_col.markdown("## 农时窗口")
            date_col.markdown(
                f'<span class="header-date">{date_range} · 计划 {version}</span>',
                unsafe_allow_html=True,
            )
            mode_picker(mode_col)
            status_col.markdown(
                f'<span class="header-status">{status}</span>', unsafe_allow_html=True
            )
            version_actions(history_col, export_col)


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
                st.success(f"找到 {len(results)} 个地点。请选择与你的地块最接近的位置。")
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


def _calculate_preview(
    service: WorkbenchServiceV3,
    state: FieldStateV3,
    field_id: str,
    preview_id: str | None,
) -> None:
    signature = (state.draft_version, state.fact_version)
    if preview_id and st.session_state.get(f"preview_signature:{field_id}") == signature:
        st.caption("已按当前地块情况生成预览。")
        return
    try:
        preview = service.preview_plan(field_id, operation_id=f"manual-preview-{uuid4()}")
        st.session_state[f"current_preview:{field_id}"] = preview.id
        st.session_state[f"preview_signature:{field_id}"] = signature
        st.rerun()
    except (ValueError, KeyError) as error:
        st.error(f"无法生成预览：{error}")


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
        (
            "待确认项",
            "、".join(map(missing_label, item.missing_fields)) if item.missing_fields else "无",
        ),
    ]
    adjustment = getattr(item, "adjustment_reason", None)
    if adjustment:
        rows.append(("调整原因", str(adjustment)))
    return rows


def _render_item(item, *, scope: str) -> None:  # type: ignore[no-untyped-def]
    """任务卡主区只显示摘要；完整字段移入「查看详情」对话框。"""
    label = SCHEDULE_LABELS.get(item.schedule_status, item.schedule_status)
    maintenance_question = (
        item.schedule_status == "pending"
        and item.task_type == "maintenance"
        and any(
            value.rsplit(":", 1)[-1]
            in {"maintenance_need_confirmed", "maintenance_problem_confirmed"}
            for value in item.missing_fields
        )
    )
    with st.container(key=f"task_{item.schedule_status}_{scope}_{item.task_id}", border=True):
        title_col, detail_col = st.columns([3, 1.2], vertical_alignment="center")
        with title_col:
            if item.start_at:
                local_start = item.start_at.astimezone(ZONE)
                weekday = "一二三四五六日"[local_start.weekday()]
                st.caption(f"{local_start:%m月%d日} · 周{weekday}")
            if maintenance_question:
                st.markdown("**待确认：要安排沟渠维护吗？**")
            else:
                st.markdown(f"**{task_title(item.task_type)}**")
        if item.start_at and item.end_at:
            st.caption(
                f"{item.start_at.astimezone(ZONE):%H:%M}—"
                f"{item.end_at.astimezone(ZONE):%H:%M} · "
                f"{item.duration_minutes}分钟 · {label}"
            )
        else:
            description = (
                "先确认沟渠实际情况，再判断维护需求。"
                if maintenance_question
                else f"{label} · {item.reason}"
            )
            st.caption(description)
        # scope 用于区分候选、预览与采用版中重名的任务编号，避免按钮 key 冲突。
        if detail_col.button("查看详情", key=f"detail:{scope}:{item.task_id}"):
            _open_dialog("task", item.plot_id, item=item, scope=scope)
            st.rerun(scope="app")


def _report_task_action_failure(
    service: WorkbenchServiceV3,
    field_id: str,
    previous: FieldStateV3,
    error: Exception,
    subject: str,
) -> None:
    """Tell the user whether the write succeeded before a preview failed."""
    current = service.get_field(field_id)
    if (
        current.draft_version != previous.draft_version
        or current.fact_version != previous.fact_version
    ):
        st.session_state[f"notice:{field_id}"] = (
            f"{subject}已保存，但预览未生成：{error}。请点「重新计算计划」后核对。"
        )
        _dismiss_dialog()
        st.rerun()
    st.error(f"{subject}未保存：{error}")


@st.dialog("任务详情", width="medium", on_dismiss=_dismiss_dialog)
def _task_detail_dialog(service: WorkbenchServiceV3, field_id: str, item, scope: str) -> None:  # type: ignore[no-untyped-def]
    """Show source and validated task actions for the current adopted version."""
    for name, value in _item_detail_rows(item):
        if name != "编号":
            st.markdown(f"**{name}**：{value}")
    with st.expander("技术编号"):
        st.markdown(f"**编号**：`{item.task_id}`")
    st.divider()
    if item.evidence:
        _render_evidence(item)
    else:
        st.caption("当前资料不足以支持这项建议")
    current = service.get_current_plan(field_id)
    state = service.get_field(field_id)
    if not current or not scope.startswith("adopted-") or scope != f"adopted-{current.id}":
        st.caption("这里仅供查看。要修改任务，请回到当前已采用的计划。")
        return
    active = [
        event
        for event in service._active_events(field_id)
        if event.feedback
        and event.feedback.kind == "completed"
        and event.feedback.task_id == item.task_id
    ]
    if active:
        event = active[-1]
        st.markdown("#### 更正已完成记录")
        st.caption("原记录和本次更正都会留在历史中。更正后需核对新的安排预览。")
        with st.form(f"correct_completed:{item.task_id}:{state.fact_version}"):
            corrected_day = st.date_input(
                "实际完成日期", value=event.feedback.at.astimezone(ZONE).date()
            )
            corrected_time = st.time_input(
                "实际完成时间", value=event.feedback.at.astimezone(ZONE).time()
            )
            confirm = st.checkbox("我确认要更正这条完成记录")
            save_correction = st.form_submit_button("保存更正")
            retract = st.form_submit_button("撤回误报完成")
        if save_correction or retract:
            if not confirm:
                st.error("请先勾选确认，再保存更正或撤回。")
                return
            try:
                feedback = (
                    None
                    if retract
                    else FeedbackV2(
                        kind="completed",
                        task_id=item.task_id,
                        plot_id=field_id,
                        at=datetime.combine(corrected_day, corrected_time, tzinfo=ZONE),
                    )
                )
                service.commit_events(
                    field_id,
                    [
                        FactEventInputV3(
                            feedback=feedback, correction_of=event.event_id, retracts=retract
                        )
                    ],
                    expected_fact_version=state.fact_version,
                    expected_draft_version=state.draft_version,
                    operation_id=f"correct-completion-{uuid4()}",
                )
                preview = service.preview_plan(field_id, operation_id=f"correct-preview-{uuid4()}")
                st.session_state[f"current_preview:{field_id}"] = preview.id
                st.session_state[f"notice:{field_id}"] = (
                    "完成记录已更正，原记录保留。请核对新的安排预览。"
                )
                _dismiss_dialog()
                st.rerun()
            except (ValueError, KeyError) as error:
                _report_task_action_failure(service, field_id, state, error, "完成更正事实")
        return
    if item.schedule_status == "cancelled":
        st.caption("这项任务已取消，原因保留在历史记录中。")
        return
    st.markdown("#### 这项任务的操作")
    eligible = item.start_at is not None and item.start_at.astimezone(ZONE) <= _now()
    if st.button("记录这项任务已完成", key=f"task_complete:{item.task_id}", disabled=not eligible):
        try:
            _commit_feedback(
                service,
                state,
                FeedbackV2(kind="completed", task_id=item.task_id, plot_id=field_id, at=_now()),
            )
            st.session_state[f"notice:{field_id}"] = "任务完成已记录。请重新计算并核对安排。"
            _dismiss_dialog()
            st.rerun()
        except ValueError as error:
            st.error(f"未记录完成：{error}")
    if not eligible:
        st.caption("到计划开始时间后才可记录完成；实际完成时间不会被预填为过去。")
    with st.expander("修改日期、时长或优先级"):
        start = state.draft.start_date or _today()
        latest = start + timedelta(days=6)
        earliest = max(start, _today())
        if earliest > latest:
            st.warning("这期七天计划已过期；请先在地块档案中调整起始日。")
        else:
            profile = state.draft.profile
            old_date = item.start_at.astimezone(ZONE).date() if item.start_at else earliest
            saved_earliest = getattr(profile, f"{item.task_type}_earliest_date")
            saved_due = getattr(profile, f"{item.task_type}_due_date")
            saved_date = saved_due if saved_due == saved_earliest else None
            default_date = max(earliest, min(saved_date or old_date, latest))
            preferred_date = st.date_input(
                "希望安排的日期",
                value=default_date,
                min_value=earliest,
                max_value=latest,
                key=f"task_date:{item.task_id}:{state.draft_version}",
            )
            duration = st.number_input(
                "预计时长（分钟）",
                min_value=1,
                max_value=1440,
                value=getattr(profile, f"{item.task_type}_duration_minutes")
                or item.duration_minutes
                or 30,
                key=f"task_duration:{item.task_id}:{state.draft_version}",
            )
            current_priority = getattr(profile, f"{item.task_type}_priority")
            priority = st.selectbox(
                "优先级",
                [1, 2, 3],
                index=[1, 2, 3].index(current_priority),
                format_func=lambda value: {1: "高", 2: "中", 3: "低"}[value],
                key=f"task_priority:{item.task_id}:{state.draft_version}",
            )
            st.caption("希望日期是你的偏好；当天现场、天气和时段仍要通过校验。")
            if st.button("保存修改并查看预览", key=f"task_edit:{item.task_id}"):
                try:
                    preview = service.edit_task(
                        field_id,
                        item.task_id,
                        preferred_date=preferred_date,
                        duration_minutes=int(duration),
                        priority=priority,
                        expected_draft_version=state.draft_version,
                        expected_fact_version=state.fact_version,
                        operation_id=f"task-edit-{uuid4()}",
                    )
                    st.session_state[f"current_preview:{field_id}"] = preview.id
                    st.session_state[f"notice:{field_id}"] = (
                        "任务偏好已保存，请核对新预览后采用安排。"
                    )
                    _dismiss_dialog()
                    st.rerun()
                except (ValueError, KeyError) as error:
                    _report_task_action_failure(service, field_id, state, error, "任务偏好")
    with st.expander("取消这项未完成任务"):
        reason = st.text_input("取消原因", key=f"task_cancel_reason:{item.task_id}")
        confirm_cancel = st.checkbox(
            "我确认取消这项未完成任务", key=f"task_cancel_check:{item.task_id}"
        )
        if st.button(
            "保存取消并查看预览",
            key=f"task_cancel:{item.task_id}",
            disabled=not reason.strip() or not confirm_cancel,
        ):
            try:
                preview = service.cancel_task(
                    field_id,
                    item.task_id,
                    reason=reason.strip(),
                    expected_draft_version=state.draft_version,
                    expected_fact_version=state.fact_version,
                    operation_id=f"task-cancel-{uuid4()}",
                )
                st.session_state[f"current_preview:{field_id}"] = preview.id
                st.session_state[f"notice:{field_id}"] = "取消原因已保存。请核对新的安排预览。"
                _dismiss_dialog()
                st.rerun()
            except (ValueError, KeyError) as error:
                _report_task_action_failure(service, field_id, state, error, "取消事实")


def _visible_questions(
    evaluation: FieldEvaluationV3, field_id: str, state: FieldStateV3
) -> tuple[list[str], set[str], str]:
    """Keep decisions with the greatest task impact first, then stable input order."""
    skip_key = f"skipped_questions:{field_id}:{state.draft_version}:{state.fact_version}"
    skipped: set[str] = st.session_state.setdefault(skip_key, set())
    subjects = list(dict.fromkeys(value.rsplit(":", 1)[-1] for value in evaluation.missing_fields))
    impacts = {
        subject: sum(
            any(value.rsplit(":", 1)[-1] == subject for value in item.missing_fields)
            for item in evaluation.items
        )
        for subject in subjects
    }
    subjects.sort(key=lambda subject: -impacts[subject])
    return [subject for subject in subjects if subject not in skipped][:3], skipped, skip_key


def _open_question(field_id: str, subject: str) -> None:
    kind = (
        "schedule"
        if subject
        in {
            "site_workability",
            "weather_workability",
            "resource_minutes",
            "time_intervals",
            "intervals",
        }
        else "field"
    )
    if subject in {
        "ditch_connectivity",
        "standing_water",
        "maintenance_need_confirmed",
        "maintenance_problem_confirmed",
        "maintenance_purpose",
    }:
        kind = "observation"
    _open_dialog(kind, field_id, focus=subject)


def _render_questions(
    evaluation: FieldEvaluationV3,
    field_id: str,
    visible: list[str],
    skipped: set[str],
    skip_key: str,
    *,
    featured: bool,
) -> None:
    remaining = visible[1:] if featured else visible
    if remaining:
        label = "其他待确认信息" if featured else "待确认信息"
        with st.expander(f"{label} · {len(remaining)} 项", expanded=False):
            for subject in remaining:
                _render_question_row(evaluation, field_id, subject, skipped)
    if skipped:
        st.caption(f"已暂时跳过 {len(skipped)} 项；相关任务仍待确认。")
        if st.button("重新查看跳过的问题", key=f"reset_questions:{field_id}"):
            st.session_state[skip_key] = set()
            st.rerun()


def _render_question_row(
    evaluation: FieldEvaluationV3, field_id: str, subject: str, skipped: set[str]
) -> None:
    """A short task row replaces a tall question card."""
    with st.container():
        affected = list(
            dict.fromkeys(
                task_title(item.task_type)
                for item in evaluation.items
                if any(value.rsplit(":", 1)[-1] == subject for value in item.missing_fields)
            )
        )
        st.markdown(f"**{missing_label(subject)}** · 关系到{'、'.join(affected or ['相关任务'])}")
        fill, skip = st.columns([1, 1], wrap=False)
        if fill.button("补充信息", key=f"fill_question:{field_id}:{subject}"):
            _open_question(field_id, subject)
        if skip.button("暂时跳过", key=f"skip_question:{field_id}:{subject}"):
            skipped.add(subject)
            st.rerun()


def _render_evaluation(
    service: WorkbenchServiceV3, field_id: str, state: FieldStateV3 | None
) -> None:
    st.subheader("待核对的安排")
    if state is None or state.draft.profile is None:
        st.info("先保存地块信息，再查看这块地的安排。")
        return
    evaluation = service.evaluate_field(field_id)
    st.caption(
        f"规则资料 {evaluation.ruleset_version} · {evaluation.status_label} · "
        f"现场记录 v{evaluation.fact_version} / 档案 v{evaluation.draft_version}"
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
    if evaluation.items:
        for item in evaluation.items:
            _render_item(item, scope="candidate")
    else:
        st.info("目前没有可显示的安排。需要补充的信息可在上方填写。")


def _render_week(items: list, start: date, *, scope: str, compact_empty: bool = True) -> None:
    """Show every day while keeping unresolved and historical work visible."""
    end = start + timedelta(days=6)
    empty_days: list[date] = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        entries = [
            item for item in items if item.start_at and item.start_at.astimezone(ZONE).date() == day
        ]
        if entries:
            for item in sorted(entries, key=lambda item: item.start_at):
                _render_item(item, scope=scope)
        elif not compact_empty:
            weekday = "一二三四五六日"[day.weekday()]
            st.caption(f"{day:%m月%d日} · 周{weekday}　暂无安排")
        else:
            empty_days.append(day)
    unresolved = [
        item for item in items if not item.start_at and item.schedule_status != "not_generated"
    ]
    if unresolved:
        for item in unresolved:
            _render_item(item, scope=scope)
    if empty_days:
        spans: list[tuple[date, date]] = []
        for day in empty_days:
            if spans and day == spans[-1][1] + timedelta(days=1):
                spans[-1] = (spans[-1][0], day)
            else:
                spans.append((day, day))
        for first, last in spans:
            dates = f"{first:%m月%d日}" if first == last else f"{first:%m月%d日}—{last:%m月%d日}"
            st.caption(f"{dates}　暂无其他安排")
    historical = [
        item
        for item in items
        if item.start_at and not start <= item.start_at.astimezone(ZONE).date() <= end
    ]
    if historical:
        with st.expander("已完成历史与本周之外的记录"):
            for item in historical:
                _render_item(item, scope=scope)
    omitted = [item for item in items if item.schedule_status == "not_generated"]
    if omitted:
        with st.expander("无需安排的事项"):
            for item in omitted:
                _render_item(item, scope=scope)


def _show_preview(
    service: WorkbenchServiceV3,
    preview: PlanPreviewV3,
    *,
    scope: str = "results",
    emphasize_adopt: bool = True,
) -> None:
    """scope 用于区分结果区与助手区展示同一预览时的按钮 key。"""
    st.markdown("#### 七天计划预览")
    st.caption(
        f"依据：档案 v{preview.expected_draft_version} · "
        f"现场记录 v{preview.expected_fact_version} · 尚未采用",
    )
    for basis in preview.version.calculation_basis:
        if not basis.startswith("单地块独立计划"):
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
            task = next(
                (item for item in preview.version.items if item.task_id == change.task_id), None
            )
            name = task_title(task.task_type) if task else "原有任务"
            st.write(
                f"- {name} · {CHANGE_LABELS.get(change.kind, '调整')}：{old_time}{old_end} → "
                f"{new_time}{new_end}；{change.reason}"
            )
    else:
        st.caption("首次预览。")
    _render_week(
        preview.version.items,
        preview.version.plan.request.start_date,
        scope=f"{scope}-preview-{preview.id}",
    )
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
                st.session_state[f"notice:{preview.field_id}"] = (
                    "计划预览已取消，已采用的计划保持不变。"
                )
                st.rerun()
        left, right = st.columns(2)
        if left.button(
            "采用这版安排",
            key=f"adopt:{preview.id}",
            type="primary" if emphasize_adopt else "secondary",
        ):
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
                    else "现场情况已更新，安排没有变化。"
                )
                st.rerun()
            except ValueError as error:
                st.error(f"预览已失效：{error}。请重新计算。")
        if right.button("取消预览", key=f"cancel:{preview.id}"):
            st.session_state[confirm_key] = True
            st.rerun()
    else:
        st.info(f"预览状态：{preview.status}")


def _preview_matches_state(preview: PlanPreviewV3, state: FieldStateV3) -> bool:
    """Only show a preview that still targets the current field revision."""
    return (
        preview.status == "preview"
        and preview.expected_draft_version == state.draft_version
        and preview.expected_fact_version == state.fact_version
        and preview.expected_plan_id == state.latest_plan_id
    )


def _render_plans(
    service: WorkbenchServiceV3,
    field_id: str,
    state: FieldStateV3 | None,
    *,
    emphasize_adopt: bool = True,
) -> None:
    """中栏「今日与后续安排」：待决预览与已采用版的排程卡片。"""
    if state is None or state.draft.profile is None:
        return
    if state.plan_stale:
        st.warning("地块情况已变化。请重新预览安排，已采用的旧版仍可查看。")
    restored = service.latest_actionable_preview(field_id)
    preview_id = restored.id if restored else None
    st.session_state[f"current_preview:{field_id}"] = preview_id
    if preview_id:
        try:
            _show_preview(
                service,
                service.store.load_preview(preview_id),
                emphasize_adopt=emphasize_adopt,
            )
        except KeyError:
            st.session_state[f"current_preview:{field_id}"] = None
    adopted = service.get_current_plan(field_id)
    if not preview_id and not adopted:
        st.markdown(
            '<div class="workbench-empty"><strong>还没有已采用的日程</strong><br>'
            "先补充确定的情况，再计算七天安排。暂不确定的事项会留待核对。"
            "</div>",
            unsafe_allow_html=True,
        )
    if adopted:
        if preview_id:
            with st.expander("查看当前采用的安排"):
                _render_week(
                    adopted.items, adopted.plan.request.start_date, scope=f"adopted-{adopted.id}"
                )
        else:
            _render_week(
                adopted.items, adopted.plan.request.start_date, scope=f"adopted-{adopted.id}"
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
    st.session_state[f"notice:{state.id}"] = "现场情况已保存。原计划保留，安排待更新。"


def _reset_observation_value(field_id: str, fact_version: int) -> None:
    st.session_state[f"observation_value:{field_id}:{fact_version}"] = "暂不确定"


def _render_feedback(
    service: WorkbenchServiceV3, field_id: str, state: FieldStateV3 | None
) -> None:
    st.subheader("记录现场情况与可用时间")
    # 右栏快捷动作预选反馈类型：弹出对应表单组。
    kind = st.session_state.pop(f"feedback_kind:{field_id}", None)
    current = service.get_current_plan(field_id) if state else None
    if state is None:
        st.caption("保存地块信息后，可以记录现场情况。")
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
                            st.error(f"未能记录任务完成：{error}")
            else:
                st.caption("当前没有已到计划开始时间且尚未完成的任务。")
    observation_fields = {
        "沟渠连通": ("ditch_connectivity", {"暂不确定": None, "通畅": "clear", "受阻": "impaired"}),
        "积水情况": ("standing_water", BOOL_OPTIONS),
        "土壤墒情": (
            "moisture",
            {"暂不确定": None, "偏干": "dry", "适中": "normal", "偏湿": "wet"},
        ),
        "维护需求": ("maintenance_need_confirmed", MAINTENANCE_NEED_OPTIONS),
        "维护问题": ("maintenance_problem_confirmed", MAINTENANCE_PROBLEM_OPTIONS),
        "维护目的": (
            "maintenance_purpose",
            {
                "暂不确定": None,
                "清理沟渠，改善排水": "clear_existing_ditch",
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
                "保存现场记录", key=f"save_observation:{field_id}:{state.fact_version}"
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
    outcome = service.confirm_agent_proposal(proposal.id, operation_id=f"confirm-{proposal.id}")
    if outcome.preview is not None:
        st.session_state[f"current_preview:{proposal.field_id}"] = outcome.preview.id
        return "修改已保存，调整预览可查看；采用计划仍需点击确认。"
    if outcome.plan_state == "no_change":
        return "修改已保存，重新计算后安排没有变化。"
    return outcome.error_message or "修改已保存，安排待更新；可点击重新计算。"


def _proposal_event_description(
    event: FactEventInputV3, *, task_names: dict[str, str] | None = None
) -> str:
    feedback = event.feedback
    if feedback is None:
        return f"撤回或更正原事件 {event.correction_of or '未指定'}"
    at = feedback.at.astimezone(ZONE).strftime("%Y-%m-%d %H:%M")
    if feedback.kind == "completed":
        name = (task_names or {}).get(feedback.task_id or "", "这项任务")
        detail = f"记录{name}已完成"
    elif feedback.kind == "observation":
        field = feedback.field or ""
        label = missing_label(field)
        value = field_value_label(field, feedback.value)
        detail = f"记录{label}：{value}"
    elif feedback.kind == "workability":

        def state(value: bool | None) -> str:
            return "暂不确定" if value is None else "可作业" if value else "不可作业"

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
    return f"{detail}（{at}）"


def _quick_feedback_dialog(service: WorkbenchServiceV3, field_id: str, kind: str) -> None:
    """在窄屏助手页签直接呈现可操作的反馈表单。"""
    state = _field_state(service, field_id)
    if state is None:
        st.info("请先保存地块档案。")
        return
    generation = st.session_state.get(f"quick_feedback_generation:{field_id}", 0)
    reset_suffix = f":reset{generation}" if generation else ""
    if kind == "observation":
        fields = {
            "沟渠连通": (
                "ditch_connectivity",
                {"暂不确定": None, "通畅": "clear", "受阻": "impaired"},
            ),
            "积水情况": ("standing_water", BOOL_OPTIONS),
            "维护需求": ("maintenance_need_confirmed", MAINTENANCE_NEED_OPTIONS),
            "维护问题": ("maintenance_problem_confirmed", MAINTENANCE_PROBLEM_OPTIONS),
            "维护目的": (
                "maintenance_purpose",
                {
                    "暂不确定": None,
                    "清理沟渠，改善排水": "clear_existing_ditch",
                    "其他目的": "other",
                },
            ),
        }
        label = st.selectbox(
            "观察项目", list(fields), key=f"quick_observation_field:{field_id}{reset_suffix}"
        )
        field, choices = fields[label]
        with st.form(f"quick_observation_form:{field_id}:{state.fact_version}{reset_suffix}"):
            value_label = st.selectbox(
                "记录值",
                list(choices),
                key=f"quick_observation_value:{field_id}:{label}{reset_suffix}",
            )
            submitted = st.form_submit_button(
                "保存现场记录",
                key=f"quick_save_observation:{field_id}:{state.fact_version}{reset_suffix}",
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
                _reset_quick_feedback(field_id)
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
        with st.form(f"quick_completion_form:{field_id}:{state.fact_version}{reset_suffix}"):
            task = st.selectbox(
                "已完成的已安排任务",
                eligible,
                format_func=lambda item: item.title,
                key=f"quick_completion_task:{field_id}:{state.fact_version}{reset_suffix}",
            )
            submitted = st.form_submit_button(
                "记录完成",
                key=f"quick_save_completion:{field_id}:{state.fact_version}{reset_suffix}",
            )
        if submitted:
            try:
                _commit_feedback(
                    service, state, FeedbackV2(kind="completed", task_id=task.task_id, at=_now())
                )
                _reset_quick_feedback(field_id)
                st.rerun()
            except ValueError as error:
                st.error(f"未能记录任务完成：{error}")
    elif kind == "resource":
        start = state.draft.start_date or (_today() + timedelta(days=1))
        with st.form(f"quick_resource_form:{field_id}:{state.fact_version}{reset_suffix}"):
            day = st.date_input(
                "资源变化日期",
                value=start,
                min_value=start,
                max_value=start + timedelta(days=6),
                key=f"quick_resource_day:{field_id}:{state.fact_version}{reset_suffix}",
            )
            minutes = st.number_input(
                "人员可用分钟数",
                min_value=0,
                max_value=1440,
                step=15,
                key=f"quick_resource_minutes:{field_id}:{state.fact_version}{reset_suffix}",
            )
            intervals = st.text_input(
                "可用时段",
                placeholder="09:00-11:00;13:00-15:00",
                key=f"quick_resource_intervals:{field_id}:{state.fact_version}{reset_suffix}",
            )
            submitted = st.form_submit_button(
                "保存资源变化",
                key=f"quick_save_resource:{field_id}:{state.fact_version}{reset_suffix}",
            )
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
                _reset_quick_feedback(field_id)
                st.rerun()
            except (ValueError, ValidationError) as error:
                st.error(f"资源变化未保存：{error}")


@st.dialog("补充现场观察", width="large", on_dismiss=_dismiss_dialog)
def _observation_dialog(service: WorkbenchServiceV3, field_id: str) -> None:
    _quick_feedback_dialog(service, field_id, "observation")


def _render_quick_actions(service: WorkbenchServiceV3, field_id: str) -> None:
    """右栏快捷动作：在当前页签展开对应反馈表单。"""
    current = service.get_current_plan(field_id)
    primary_observation = bool(
        current
        and not service.latest_actionable_preview(field_id)
        and not service.pending_agent_proposals(field_id)
    )
    if st.button(
        "补充现场观察",
        key=f"quick_observe:{field_id}",
        width="stretch",
        type="primary" if primary_observation else "secondary",
    ):
        st.session_state[f"quick_feedback_kind:{field_id}"] = "observation"
    eligible = bool(
        current
        and any(
            item.schedule_status == "scheduled"
            and item.start_at
            and not item.completed
            and item.start_at.astimezone(ZONE) <= _now()
            for item in current.items
        )
    )
    if eligible and st.button(
        "记录任务完成",
        key=f"quick_complete:{field_id}",
        width="stretch",
    ):
        st.session_state[f"quick_feedback_kind:{field_id}"] = "completed"
    if st.button("修改可用时间", key=f"quick_resource:{field_id}", width="stretch"):
        st.session_state[f"quick_feedback_kind:{field_id}"] = "resource"
    kind = st.session_state.get(f"quick_feedback_kind:{field_id}")
    if kind == "completed" and not eligible:
        kind = None
        st.session_state.pop(f"quick_feedback_kind:{field_id}", None)
    if kind:
        with st.container(border=True):
            if st.button("取消填写", key=f"quick_cancel_feedback:{field_id}"):
                _reset_quick_feedback(field_id)
                st.rerun()
            _quick_feedback_dialog(service, field_id, kind)


def _start_agent_job(agent: AgentServiceV3, field_id: str, message: str, history: list) -> None:
    settings = agent.settings
    database = agent.workbench.store.path
    candidate_key = f"agent_geo_candidates:{database.resolve()}:{field_id}"
    candidate_cache = st.session_state.setdefault(candidate_key, {})

    def factory() -> AgentServiceV3:
        workbench = WorkbenchServiceV3(WorkbenchStoreV3(database), catalog=load_rule_catalog())
        background_agent = AgentServiceV3(settings, workbench)
        background_agent._geocoding_candidates = candidate_cache
        return background_agent

    st.session_state[f"agent_last_message:{field_id}"] = message
    st.session_state["active_agent_job"] = AgentJob(
        factory, field_id, message, conversation=history, received_at=_now()
    )
    st.session_state["active_agent_database"] = str(database)
    st.session_state.pop(f"agent_job_error:{field_id}", None)


def _render_chat_input(agent: AgentServiceV3, field_id: str, history: list[dict[str, str]]) -> None:
    job = st.session_state.get("active_agent_job")
    if job and job.field_id == field_id:
        _render_agent_job_progress(field_id)
        return

    error = st.session_state.get(f"agent_job_error:{field_id}")
    response = st.session_state.get(f"agent_result:{field_id}")
    if error:
        st.info(error)
    if error or (response and response.status != "success"):
        retry, structured = st.columns(2, wrap=False)
        last_message = st.session_state.get(f"agent_last_message:{field_id}")
        if retry.button("重试", key=f"retry_agent:{field_id}", disabled=not last_message):
            _start_agent_job(agent, field_id, last_message, history)
            st.rerun()
        if structured.button("手动填写", key=f"agent_structured:{field_id}"):
            _open_dialog("field", field_id)
            st.rerun()
    with st.form(f"assistant_form:{field_id}", clear_on_submit=True, border=False):
        user_message = st.text_area(
            "描述现场变化或时间安排",
            key=f"assistant_draft:{field_id}",
            placeholder="例如：明天只能干一小时",
            label_visibility="collapsed",
            height=64,
        )
        _, send_col = st.columns([2, 1], vertical_alignment="center", wrap=False)
        submitted = send_col.form_submit_button(
            "发送", key=f"assistant_send:{field_id}", type="primary"
        )
    if submitted:
        if user_message.strip():
            _start_agent_job(agent, field_id, user_message.strip(), history)
            st.rerun()
        st.info("请先输入要告诉助手的情况。")


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
        st.info("先保存地块信息，再告诉助手这块地的情况。也可以直接手动填写。")
        return
    history_key = f"chat:{field_id}"
    history: list[dict[str, str]] = st.session_state.setdefault(history_key, [])
    if not history and not st.session_state.get(f"agent_result:{field_id}"):
        plan = service.get_current_plan(field_id)
        pending = (
            next((item for item in plan.items if item.schedule_status == "pending"), None)
            if plan
            else None
        )
        if pending:
            opening = (
                f"{task_title(pending.task_type)}还有情况待确认。"
                "告诉我你看到的情况，保存前会请你核对。"
            )
        else:
            opening = "告诉我现场有什么变化，或你哪天有空。例如：明天只能干一小时。"
        st.markdown(
            f'<div class="workbench-empty">{opening}</div>',
            unsafe_allow_html=True,
        )
    for message in history[-20:]:
        with st.chat_message(message["role"]):
            st.write(message["content"])
    response = st.session_state.get(f"agent_result:{field_id}")
    if response:
        if response.status == "success" and response.business_outcome == "needs_information":
            st.info("还需要补充一些情况。")
        if response.status == "success":
            if response.text and not any(
                message["content"] == response.text for message in history[-2:]
            ):
                st.markdown(response.text)
        else:
            if not any(
                message["content"]
                == (response.text or response.error_message or "这次没有完成，请重试。")
                for message in history[-2:]
            ):
                st.warning(response.error_message or response.status)
        if response.tools or response.prompt_tokens is not None:
            with st.expander("技术详情", expanded=False):
                st.caption(
                    f"执行编号 {response.run_id} · 请求编号 {', '.join(response.request_ids)}"
                )
                for trace in response.tools:
                    st.write(
                        f"{trace.name} · {trace.status} · {trace.elapsed_ms} ms · "
                        f"{trace.result_summary}"
                    )
                if response.prompt_tokens is not None:
                    st.caption(
                        f"{response.request_count} 次模型请求 · "
                        f"{response.prompt_tokens + (response.completion_tokens or 0)} tokens"
                    )
        if response.previews:
            st.info("安排预览已生成，请在计划结果区核对并采用。")
    records = service.agent_proposal_records(field_id)
    primary_proposal_id = next(
        (record.proposal.id for record in records if record.status == "pending"), None
    )
    for record in records:
        proposal = record.proposal
        if record.status != "pending":
            continue
        st.markdown("#### 请核对这次修改")
        if proposal.kind == "facts":
            current_plan = service.get_current_plan(field_id)
            task_names = (
                {item.task_id: item.title for item in current_plan.items} if current_plan else {}
            )
            for index, event in enumerate(proposal.events, start=1):
                st.markdown(f"{index}. {_proposal_event_description(event, task_names=task_names)}")
            with st.expander("完整记录（技术详情）", expanded=False):
                st.json(
                    [event.model_dump(mode="json", exclude_none=True) for event in proposal.events]
                )
        else:
            for row in proposal_change_rows(proposal.changes):
                st.markdown(f"- {row}")
            with st.expander("完整修改（技术详情）", expanded=False):
                st.json(proposal.changes)
        yes, no = st.columns(2)
        if yes.button(
            "确认并保存",
            key=f"confirm_proposal:{proposal.id}",
            type="primary" if proposal.id == primary_proposal_id else "secondary",
        ):
            try:
                with st.status("正在保存并更新安排…", expanded=False) as progress:
                    st.session_state[f"notice:{field_id}"] = _apply_proposal(service, proposal)
                    progress.update(label="保存已完成", state="complete")
                st.session_state[f"agent_result:{field_id}"] = None
                st.rerun()
            except (ValueError, KeyError) as error:
                st.error(f"确认失败：{error}。请刷新状态后重新提出。")
        if no.button("放弃这条修改", key=f"ignore_proposal:{proposal.id}"):
            service.ignore_agent_proposal(proposal.id)
            st.session_state[f"agent_result:{field_id}"] = None
            st.rerun()
    for record in records:
        if record.status == "confirmed" and record.last_error:
            st.warning(f"情况已保存，但安排还没更新：{record.last_error}。请重新计算。")
            if st.button("重新计算安排", key=f"retry_agent_preview:{record.proposal.id}"):
                result = service.retry_agent_preview(record.proposal.id)
                if result.preview is not None:
                    st.session_state[f"current_preview:{field_id}"] = result.preview.id
                st.session_state[f"notice:{field_id}"] = (
                    "调整预览已恢复，请核对后采用。"
                    if result.preview is not None
                    else result.error_message or "重新计算后安排没有变化。"
                )
                st.rerun()


def _seed_demo(service: WorkbenchServiceV3) -> str:
    field_id = f"demo-{uuid4().hex[:8]}"
    start = _today() + timedelta(days=1)
    now = _now()
    profile = FieldProfile(
        id=field_id,
        name="我的第一块地",
        province="湖北",
        province_confirmed=True,
        crop="油菜",
        crop_confirmed=True,
        stage="苗期",
        stage_confirmed=True,
        field_type="lowland_rice_stubble",
        inspection_requested=True,
        inspection_duration_minutes=30,
        observation_source="synthetic",
    )
    location = WeatherLocationV3(
        query="武汉模拟地点",
        display_name="模拟地块附近",
        latitude=30.59,
        longitude=114.30,
        geocoding_source="模拟数据",
    )
    service.save_field_draft(
        FieldDraftV3(
            field_id=field_id,
            mode="demo",
            profile=profile,
            location=location,
            start_date=start,
            days=[
                DayWindow(
                    day=start + timedelta(days=offset),
                    site_workable=True,
                    weather_workable=True,
                    resource_minutes=120,
                    intervals=[TimeWindow(start=time(9), end=time(11))],
                )
                for offset in range(7)
            ],
            input_source="demo",
            notes="模拟现场条件与可用时间，仅用于体验界面流程。",
        ),
        expected_version=None,
        operation_id=f"demo-{uuid4()}",
    )
    service.store_weather(
        WeatherSnapshotV3(
            field_id=field_id,
            source="模拟天气",
            location=location,
            requested_latitude=location.latitude,
            requested_longitude=location.longitude,
            grid_latitude=location.latitude,
            grid_longitude=location.longitude,
            fetched_at=now,
            coverage_start=start,
            coverage_end=start + timedelta(days=6),
            units={},
            days=[WeatherDayV3(day=start + timedelta(days=offset)) for offset in range(7)],
            cache_expires_at=now + timedelta(days=8),
        )
    )
    preview = service.preview_plan(field_id, source="demo")
    service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id=f"adopt-demo-{field_id}",
    )
    return field_id


def _initial_workspace_mode() -> str:
    requested = st.query_params.get("mode") or os.getenv("NONGSHI_DEFAULT_MODE")
    if requested in {"real", "demo"}:
        return "真实地块" if requested == "real" else "独立模拟示例"
    real_service, _, _ = _context("real")
    return "真实地块" if _saved_fields(real_service, "real") else "独立模拟示例"


def _remember_workspace_mode() -> None:
    st.query_params["mode"] = "real" if st.session_state["workspace_mode"] == "真实地块" else "demo"


def _start_real_use() -> None:
    st.session_state["workspace_mode"] = "真实地块"
    _remember_workspace_mode()


def _start_demo_use() -> None:
    st.session_state["workspace_mode"] = "独立模拟示例"
    _remember_workspace_mode()


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
                        "已复制原来的填写内容。未填写的仍留空，原记录保留。"
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
    if "workspace_mode" not in st.session_state:
        st.session_state["workspace_mode"] = _initial_workspace_mode()
    mode = "real" if st.session_state["workspace_mode"] == "真实地块" else "demo"
    service, agent, database = _context(mode)
    if mode == "demo":
        demo_fields = _saved_fields(service, "demo")
        if not any(service.get_current_plan(field.id) for field in demo_fields):
            st.session_state["pending_field_selection:demo"] = _seed_demo(service)

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
            st.toast(notice, icon="✅")
        has_profile = field_state is not None and field_state.draft.profile is not None
        if has_profile:
            _render_field_card(field_state)
        # 当前地块下拉紧贴卡片下方；「+ 新增地块」紧随其后。
        _render_field_selector(service, mode)
        if not has_profile:
            # 空白优先：首次进入或新增地块时直接显示空白表单，无需对话框。
            _render_field_form(service, field_id, field_state, mode)
            return field_id, field_state
        st.markdown(
            '<div class="field-hint">一块地也能独立使用<br>'
            "新增后填写新地块信息<br>各地块分别保存结果</div>",
            unsafe_allow_html=True,
        )
        if st.button("编辑当前地块", key=f"edit_field:{field_id}", width="stretch"):
            _open_dialog("field", field_id)
        st.markdown("#### 可用时间")
        _render_availability_panel(field_state)
        if st.button("调整可用时段", key=f"edit_schedule:{field_id}", width="stretch"):
            _open_dialog("schedule", field_id)
        st.caption("开发验证，适用性待复核")
        if mode == "demo":
            st.button(
                "开始真实使用",
                key="start_real_use",
                width="stretch",
                on_click=_start_real_use,
            )
        else:
            st.button(
                "查看模拟示例",
                key="start_demo_use",
                width="stretch",
                on_click=_start_demo_use,
            )
        return field_id, field_state

    def render_results(field_id: str) -> None:
        current = _field_state(service, field_id)
        profile = current.draft.profile if current else None
        name = profile.name if profile and profile.name else "未命名地块"
        heading, action = st.columns([3.5, 1.3], vertical_alignment="center")
        heading.markdown(f"### {name} · 结果")
        if current is None or profile is None:
            return
        evaluation = service.evaluate_field(field_id)
        pending_proposals = service.pending_agent_proposals(field_id)
        visible, skipped, skip_key = _visible_questions(evaluation, field_id, current)
        active_preview = service.latest_actionable_preview(field_id)
        preview_id = active_preview.id if active_preview else None
        old_preview_id = st.session_state.get(f"current_preview:{field_id}")
        st.session_state[f"current_preview:{field_id}"] = preview_id
        if old_preview_id and not preview_id:
            st.info("先前的预览已过期。请按现在的时间和地块情况重新计算。")
        adopted = service.get_current_plan(field_id)
        if not active_preview and action.button(
            "重新计算计划" if adopted else "生成计划预览",
            key=f"preview_plan:{field_id}",
            width="stretch",
        ):
            _calculate_preview(service, current, field_id, preview_id)
        items = (
            active_preview.version.items
            if active_preview
            else adopted.items
            if adopted
            else evaluation.items
        )
        pending_count = sum(item.schedule_status == "pending" for item in items)
        if active_preview:
            status = "有一版计划预览等待核对"
            guidance = "核对下方安排和待确认事项后，才能采用为当前计划。"
        elif adopted and current.plan_stale:
            status = "已保存的计划需要更新"
            guidance = "地块情况有变化。原计划还在，请先查看调整预览。"
        elif adopted:
            status = "当前计划已采用"
            guidance = "先看今天的安排。现场有变化时，记录情况并重新核对七天计划。"
        else:
            status = f"当前没有已采用计划，{pending_count} 项任务待确认"
            has_resource_entry = any(
                day.resource_minutes is not None or day.intervals for day in current.draft.days
            )
            guidance = (
                "尚未录入可用时间；可以先查看部分判断，具体钟点仍待确认。"
                if not has_resource_entry
                else "先补充会影响判断的信息，也可以查看部分计划。"
            )
        if not adopted or active_preview or current.plan_stale:
            st.markdown(
                '<div class="workbench-summary">'
                f"<strong>{status}</strong><span>{guidance}</span></div>",
                unsafe_allow_html=True,
            )
        if pending_proposals:
            st.info(
                f"助手有 {len(pending_proposals)} 条修改待确认。"
                "请在农事助手中核对并保存，当前计划尚未改变。"
            )

        featured = bool(
            visible
            and not adopted
            and not active_preview
            and not current.plan_stale
            and not pending_proposals
        )
        if featured:
            subject = visible[0]
            if st.button(
                f"补充：{missing_label(subject)}",
                key=f"next_question:{field_id}",
                type="primary",
                width="stretch",
            ):
                _open_question(field_id, subject)
            if st.button("暂时跳过这项", key=f"skip_primary_question:{field_id}"):
                skipped.add(subject)
                st.rerun()

        source = "待采用预览" if active_preview else "已采用计划" if adopted else "待确认安排"
        st.caption(f"{_weather_summary(service, field_id)} · {source}")
        _render_count_strip(items)
        st.markdown("#### 今日与后续安排")
        today_message = "今天还没有已采用的安排。下方可查看预览或待确认事项。"
        if adopted:
            today = _today()
            plan_start = adopted.plan.request.start_date
            if today < plan_start:
                today_message = f"今天暂无安排；七天计划从 {plan_start:%m月%d日} 开始。"
            elif today > plan_start + timedelta(days=6):
                today_message = "已采用计划未覆盖今天；请重新核对七天安排。"
            else:
                today_count = sum(
                    item.start_at is not None
                    and item.start_at.astimezone(ZONE).date() == today
                    and item.schedule_status in {"scheduled", "completed"}
                    for item in adopted.items
                )
                today_message = f"今天的已采用安排：{today_count} 项。"
        _render_plans(
            service,
            field_id,
            current,
            emphasize_adopt=not pending_proposals,
        )
        st.caption(today_message)
        if adopted:
            st.caption(f"已采用 · {adopted.created_at.astimezone(ZONE):%m月%d日 %H:%M} 保存")
        _render_questions(evaluation, field_id, visible, skipped, skip_key, featured=featured)
        st.caption("以上为当前地块结果；依据可在任务详情查看。")
        st.caption("单地块独立计划；尚未统筹其他地块时间，不代表合并后可执行。")
        with st.expander("天气与查询位置", expanded=False):
            _render_weather(service, agent, field_id, current)
        with st.expander("安排依据", expanded=False):
            _render_evaluation(service, field_id, current)
        with st.expander("反馈与历史", expanded=False):
            if current:
                _render_feedback(service, field_id, current)

    def render_assistant(field_id: str) -> None:
        current = _field_state(service, field_id)
        st.markdown("### 农事助手")
        with st.container(key="assistant_scroll"):
            _render_agent(service, agent, field_id, current)
            _render_quick_actions(service, field_id)
            st.markdown("#### 最近操作")
            recent = []
            if current:
                for event in service.list_events(field_id)[-3:]:
                    feedback = event.feedback
                    if feedback:
                        recent.append(_proposal_event_description(event))
                adopted = service.get_current_plan(field_id)
                if not recent and adopted:
                    scheduled = sum(
                        item.schedule_status in {"scheduled", "completed"} for item in adopted.items
                    )
                    version = len(service.plan_history(field_id))
                    recent.append(f"本地块 V{version} · {scheduled}项已安排")
            for description in reversed(recent):
                st.caption(description)
            if not recent:
                st.caption("本地块还没有操作记录。")
            _render_legacy_history(service, database, mode)
        with st.container(key="assistant_compose"):
            st.caption("输入后先核对修改")
            history = st.session_state.setdefault(f"chat:{field_id}", [])
            _render_chat_input(agent, field_id, history)

    initial_id, initial_state = _select_field(service, mode)
    job = st.session_state.get("active_agent_job")
    if job and (
        job.field_id != initial_id or st.session_state.get("active_agent_database") != str(database)
    ):
        job.cancel()
        st.session_state[f"agent_job_error:{job.field_id}"] = (
            "已切换地块，刚才的等待已停止。已保存的内容还在。"
        )
        st.session_state.pop("active_agent_job", None)
    if initial_state is None or initial_state.draft.profile is None:
        with st.container(key="field_onboarding", border=True):
            st.markdown("### 从一块地开始")
            saved_fields = _saved_fields(service, mode)
            if saved_fields:
                st.caption("这是新地块；未保存的输入不会进入档案。")
                if st.button(
                    "放弃未保存内容并返回",
                    key=f"cancel_new_field:{mode}",
                    width="stretch",
                ):
                    previous = st.session_state.get(f"previous_field:{mode}")
                    valid_ids = {field.id for field in saved_fields}
                    st.session_state[f"pending_field_selection:{mode}"] = (
                        previous if previous in valid_ids else saved_fields[-1].id
                    )
                    _clear_editor("field", initial_id)
                    st.rerun()
            _render_field_form(service, initial_id, initial_state, mode, initial_setup=True)
        with top_slot.container():
            _render_top_bar(service, initial_id, initial_state, mode, compact=compact)
    elif compact:
        with st.container(key="compact_workbench"):
            fields_tab, results_tab, assistant_tab = st.tabs(
                ["地块与资源", "计划结果", "助手与历史"],
                default="计划结果",
                key=f"main_tabs:{mode}",
            )
            with fields_tab:
                with st.container(key="field_rail"):
                    field_id, field_state = render_fields()
            with results_tab:
                with st.container(key="result_rail"):
                    render_results(field_id)
            with assistant_tab:
                with st.container(key="assistant_rail"):
                    render_assistant(field_id)
        with top_slot.container():
            _render_top_bar(service, field_id, field_state, mode, compact=compact)
    else:
        with st.container(key="workbench_columns"):
            left, middle, right = st.columns([1.9, 5.3, 2.8], gap="small")
            with left:
                with st.container(key="field_rail"):
                    field_id, current = render_fields()
            with middle:
                with st.container(key="result_rail"):
                    render_results(field_id)
            with right:
                with st.container(key="assistant_rail"):
                    render_assistant(field_id)
        with top_slot.container():
            _render_top_bar(service, field_id, current, mode, compact=compact)
    selected = _field_state(service, initial_id)
    if selected and selected.draft.profile:
        with st.expander("开发者选项", expanded=False):
            st.caption(f"数据文件：`{database.name}`")
            _render_rule_reviews(service, initial_id, selected)
    _render_open_dialog(service, mode)


LOGGER = logging.getLogger(__name__)


def _editor_reset_suffix(kind: str, field_id: str) -> str:
    generation = st.session_state.get(f"editor_generation:{kind}:{field_id}", 0)
    return f":reset{generation}" if generation else ""


def _reset_quick_feedback(field_id: str) -> None:
    st.session_state.pop(f"quick_feedback_kind:{field_id}", None)
    generation_key = f"quick_feedback_generation:{field_id}"
    st.session_state[generation_key] = st.session_state.get(generation_key, 0) + 1


@st.fragment(run_every=0.5)
def _render_agent_job_progress(field_id: str) -> None:
    job = st.session_state.get("active_agent_job")
    if not job or job.field_id != field_id:
        return

    snapshot = job.snapshot()
    if snapshot.done:
        st.session_state.pop("active_agent_job", None)
        if snapshot.cancelled:
            st.session_state[f"agent_job_error:{field_id}"] = "已停止等待。已保存的内容还在。"
        elif snapshot.response:
            response = snapshot.response
            history = st.session_state.setdefault(f"chat:{field_id}", [])
            history.extend(
                [
                    {"role": "user", "content": job.user_message},
                    {
                        "role": "assistant",
                        "content": response.text
                        or response.error_message
                        or "这次没有完成，请重试。",
                    },
                ]
            )
            st.session_state[f"agent_result:{field_id}"] = response
        else:
            st.session_state[f"agent_job_error:{field_id}"] = snapshot.error
        st.rerun()

    tool_labels = {
        "read_field": "读取地块档案",
        "evaluate_field": "核对条件与缺项",
        "search_rules": "查找规则依据",
        "query_weather": "获取天气",
        "query_current_weather": "获取天气",
        "preview_plan": "计算安排预览",
        "search_locations": "查找地点",
        "propose_location": "整理地点候选",
        "propose_feedback": "整理待确认反馈",
        "propose_field_update": "整理档案修改",
        "declare_request": "确认本轮请求",
    }
    label = "正在处理，请稍候"
    if snapshot.events:
        latest = snapshot.events[-1]
        label = (
            "正在处理，请稍候"
            if latest.stage == "model"
            else tool_labels.get(latest.tool_name, "正在整理结果")
        )
    with st.status(label, expanded=False):
        for event in snapshot.events[-4:]:
            stage = (
                "模型请求"
                if event.stage == "model"
                else tool_labels.get(event.tool_name, "整理结果")
            )
            status = (
                "开始"
                if event.status == "started"
                else "完成"
                if event.status == "success"
                else "结束"
            )
            st.caption(f"{stage} · {status}")
    if st.button("退出等待", key=f"cancel_agent:{field_id}"):
        job.cancel()
        st.rerun()


if __name__ == "__main__":
    main()
