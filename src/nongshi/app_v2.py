"""Streamlit workbench for the source-traceable, offline v2 prototype."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import streamlit as st

from nongshi.cases import replay_reference_history
from nongshi.config import Settings
from nongshi.demo_v2 import make_demo_request_v2
from nongshi.planning_v2 import apply_feedback_v2, build_plan_v2
from nongshi.repository import PlanStore, PlanStoreV2
from nongshi.rules import load_rule_catalog
from nongshi.schema import PlanVersion
from nongshi.schema_v2 import (
    LOCAL_ZONE,
    FeedbackV2,
    PlanItemV2,
    PlanRequestV2,
    PlanVersionV2,
    TimeWindow,
    WorkabilityConfirmation,
)

STATUS = {
    "scheduled": "已安排",
    "unscheduled": "窗口可行，未排入",
    "pending": "待确认",
    "not_generated": "未生成",
    "completed": "已完成",
}
NECESSITY = {"required": "需要", "no_need": "不需要", "unknown": "未知"}
APPLICABILITY = {"matched": "适用", "out_of_scope": "超出范围", "pending": "待确认"}
TRI_OPTIONS: tuple[bool | None, ...] = (None, True, False)
TRI_LABELS = {None: "待确认", True: "是", False: "否"}
PLOT_TYPES = {
    None: "未知",
    "lowland_rice_stubble": "低洼/稻茬田",
    "dry_hilly": "丘陵旱地",
    "other": "其他田型",
}
CONNECTIVITY = {None: "未知", "clear": "通畅", "impaired": "不畅/有堵塞"}
MOISTURE = {None: "未知", "dry": "偏干", "normal": "一般", "wet": "偏湿"}
FIELD_LABEL = {
    "maintenance_need_confirmed": "维护需求",
    "maintenance_problem_confirmed": "存在维护问题",
    "moisture": "土壤干湿",
    "ditch_connectivity": "沟系连通",
    "standing_water": "田面积水",
    "leaf_count": "叶片数",
    "actual_rain_observed": "已确认降雨实况",
    "winter_before_context_confirmed": "冬前情境",
}

CSS = """
<style>
.stApp { background: #f4f6ef; color: #1c332b; }
[data-testid="stHeader"] { background: rgba(244,246,239,.92); }
h1 { font-family: 'Georgia', 'Noto Serif CJK SC', serif; color: #18392f; letter-spacing: -.04em; }
h2, h3 { color: #275246; }
.workbench-kicker { color:#8c6d2f; font:700 .75rem 'Consolas',monospace; letter-spacing:.14em; }
.workbench-note { border-left:3px solid #ba9b52; padding:.65rem .9rem;
  background:#e9eee2; line-height:1.55; }
[data-testid="stVerticalBlockBorderWrapper"] { border-color:#d4ded2; }
[data-testid="stButton"] button[kind="primary"] { background:#2b6655; border-color:#2b6655; }
</style>
"""


def _today() -> date:
    return datetime.now(LOCAL_ZONE).date() + timedelta(days=1)


def _parse_intervals(value: str) -> list[TimeWindow]:
    result: list[TimeWindow] = []
    if not value.strip():
        return result
    for part in value.split(";"):
        fields = part.strip().split("-")
        if len(fields) != 2:
            raise ValueError("时段请按 09:00-11:00;13:00-15:00 填写")
        result.append(
            TimeWindow(
                start=time.fromisoformat(fields[0].strip()),
                end=time.fromisoformat(fields[1].strip()),
            )
        )
    return result


def _format_intervals(intervals: list[TimeWindow]) -> str:
    return ";".join(f"{item.start:%H:%M}-{item.end:%H:%M}" for item in intervals)


def _load_state(v2_store: PlanStoreV2) -> None:
    if st.session_state.get("v2_loaded"):
        return
    latest = v2_store.latest()
    st.session_state.v2_plan = latest
    st.session_state.v2_request = (
        latest.request.model_copy(deep=True) if latest else make_demo_request_v2(_today())
    )
    st.session_state.v2_input_epoch = 0
    st.session_state.v2_loaded = True


def _save_version(store: PlanStoreV2, version: PlanVersionV2) -> None:
    store.save(version)
    st.session_state.v2_plan = version
    st.session_state.v2_request = version.request.model_copy(deep=True)
    st.session_state.v2_input_epoch += 1
    st.session_state.pop("v2_plan_head", None)
    st.rerun()


def _v2_status(item: PlanItemV2) -> str:
    return " · ".join(
        (
            STATUS[item.schedule_status],
            NECESSITY[item.necessity],
            APPLICABILITY[item.applicability],
        )
    )


def _render_rule_evidence(item: PlanItemV2) -> None:
    if not item.evidence:
        return
    with st.expander("依据与适用限制"):
        for evidence in item.evidence:
            st.markdown(f"**{evidence.rule_id} · {evidence.title}**")
            st.caption(
                f"{evidence.status_label}｜规则包 {evidence.pack_version}｜"
                f"来源 {evidence.source_id}"
            )
            if evidence.source_context_summary:
                st.caption(f"来源背景：{evidence.source_context_summary}")
            if evidence.project_use_limit:
                st.caption(f"本项目用途：{evidence.project_use_limit}")
            st.write(f"发布机构：{evidence.publisher} · 日期：{evidence.source_date}")
            st.write(f"适用条件：{evidence.applicability_text} {evidence.condition_text}")
            st.write(f"原文位置：PDF 第 {', '.join(map(str, evidence.pdf_pages))} 页")
            st.write(evidence.quote)
            st.caption(f"适用限制：{evidence.limits}")
            st.caption(f"来源 SHA-256：{evidence.source_sha256[:16]}…")
            if evidence.source_url:
                st.link_button("打开来源网页", evidence.source_url)


def _tri_select(label: str, value: bool | None, key: str) -> bool | None:
    return st.selectbox(
        label,
        options=list(TRI_OPTIONS),
        index=list(TRI_OPTIONS).index(value),
        format_func=TRI_LABELS.get,
        key=key,
    )


def _render_inputs() -> None:
    request: PlanRequestV2 = st.session_state.v2_request
    epoch: int = st.session_state.v2_input_epoch
    st.subheader("地块与当天条件")
    st.caption("演示中的地块、时间和田况均为模拟；未知值保持待确认，不作为否定条件。")
    with st.form("v2_inputs"):
        plot_values: dict[str, dict[str, object]] = {}
        for plot in request.plots:
            with st.expander(plot.name, expanded=plot.id == request.plots[0].id):
                province_options = [None, "湖北", "其他省份"]
                province = st.selectbox(
                    "所在省份",
                    province_options,
                    index=province_options.index(
                        plot.province
                        if plot.province in province_options
                        else "其他省份"
                        if plot.province
                        else None
                    ),
                    format_func=lambda value: {
                        None: "未知",
                        "湖北": "湖北",
                        "其他省份": "其他省份",
                    }[value],
                    key=f"v2_province_{plot.id}_{epoch}",
                )
                province_confirmed = st.checkbox(
                    "地区已确认",
                    value=plot.province_confirmed,
                    key=f"v2_province_confirmed_{plot.id}_{epoch}",
                )
                stage_options = [None, "苗期", "花薹期", "其他"]
                stage = st.selectbox(
                    "生育期（现场确认）",
                    stage_options,
                    index=stage_options.index(
                        plot.stage
                        if plot.stage in stage_options
                        else "其他"
                        if plot.stage
                        else None
                    ),
                    format_func=lambda value: {
                        None: "未知",
                        "苗期": "苗期",
                        "花薹期": "花薹期",
                        "其他": "其他",
                    }[value],
                    key=f"v2_stage_{plot.id}_{epoch}",
                )
                stage_confirmed = st.checkbox(
                    "生育期已由现场确认",
                    value=plot.stage_confirmed,
                    key=f"v2_stage_confirmed_{plot.id}_{epoch}",
                )
                type_options = list(PLOT_TYPES)
                field_type = st.selectbox(
                    "田型",
                    type_options,
                    index=type_options.index(plot.field_type),
                    format_func=PLOT_TYPES.get,
                    key=f"v2_type_{plot.id}_{epoch}",
                )
                area = st.number_input(
                    "面积（亩，可选）",
                    min_value=0.0,
                    value=float(plot.area_mu or 0),
                    step=0.5,
                    key=f"v2_area_{plot.id}_{epoch}",
                )
                inspection = _tri_select(
                    "是否需要巡田",
                    plot.inspection_requested,
                    f"v2_inspection_{plot.id}_{epoch}",
                )
                need = _tri_select(
                    "是否确认需要沟渠维护",
                    plot.maintenance_need_confirmed,
                    f"v2_maintenance_need_{plot.id}_{epoch}",
                )
                problem = _tri_select(
                    "是否确认存在维护问题",
                    plot.maintenance_problem_confirmed,
                    f"v2_maintenance_problem_{plot.id}_{epoch}",
                )
                connectivity_options = [None, "clear", "impaired"]
                connectivity = st.selectbox(
                    "沟渠连通情况",
                    connectivity_options,
                    index=connectivity_options.index(plot.ditch_connectivity),
                    format_func=CONNECTIVITY.get,
                    key=f"v2_connectivity_{plot.id}_{epoch}",
                )
                moisture_options = [None, "dry", "normal", "wet"]
                moisture = st.selectbox(
                    "土壤干湿",
                    moisture_options,
                    index=moisture_options.index(plot.moisture),
                    format_func=MOISTURE.get,
                    key=f"v2_moisture_{plot.id}_{epoch}",
                )
                water = _tri_select("田面积水", plot.standing_water, f"v2_water_{plot.id}_{epoch}")
                rain = _tri_select(
                    "是否有实际降雨记录",
                    plot.actual_rain_observed,
                    f"v2_rain_{plot.id}_{epoch}",
                )
                leaf_count_known = st.checkbox(
                    "叶片数已确认",
                    value=plot.leaf_count is not None,
                    key=f"v2_leaf_known_{plot.id}_{epoch}",
                )
                leaf_count = st.number_input(
                    "叶片数（可选观察）",
                    min_value=0.0,
                    value=float(plot.leaf_count if plot.leaf_count is not None else 0),
                    step=1.0,
                    disabled=not leaf_count_known,
                    key=f"v2_leaf_{plot.id}_{epoch}",
                )
                with st.expander("任务分钟数、优先级与期限"):
                    inspection_duration = st.number_input(
                        "巡田预计分钟数（0=未知）",
                        min_value=0,
                        value=plot.inspection_duration_minutes or 0,
                        step=5,
                        key=f"v2_iduration_{plot.id}_{epoch}",
                    )
                    maintenance_duration = st.number_input(
                        "维护预计分钟数（0=未知）",
                        min_value=0,
                        value=plot.maintenance_duration_minutes or 0,
                        step=5,
                        key=f"v2_mduration_{plot.id}_{epoch}",
                    )
                    inspection_priority = st.selectbox(
                        "巡田优先级（1最高）",
                        [1, 2, 3],
                        index=plot.inspection_priority - 1,
                        key=f"v2_ipriority_{plot.id}_{epoch}",
                    )
                    maintenance_priority = st.selectbox(
                        "维护优先级（1最高）",
                        [1, 2, 3],
                        index=plot.maintenance_priority - 1,
                        key=f"v2_mpriority_{plot.id}_{epoch}",
                    )
                    inspection_earliest = st.date_input(
                        "巡田最早日期（可选）",
                        value=plot.inspection_earliest_date,
                        key=f"v2_iearliest_{plot.id}_{epoch}",
                    )
                    inspection_due = st.date_input(
                        "巡田目标日期（可选）",
                        value=plot.inspection_due_date,
                        key=f"v2_idue_{plot.id}_{epoch}",
                    )
                    maintenance_earliest = st.date_input(
                        "维护最早日期（可选）",
                        value=plot.maintenance_earliest_date,
                        key=f"v2_mearliest_{plot.id}_{epoch}",
                    )
                    maintenance_due = st.date_input(
                        "维护目标日期（可选）",
                        value=plot.maintenance_due_date,
                        key=f"v2_mdue_{plot.id}_{epoch}",
                    )
                    requires_inspection = st.checkbox(
                        "明确要求先巡田再维护",
                        value=f"{plot.id}-I" in plot.maintenance_prerequisites,
                        key=f"v2_pre_{plot.id}_{epoch}",
                    )
                plot_values[plot.id] = {
                    "province": province,
                    "province_confirmed": province_confirmed,
                    "stage": stage,
                    "stage_confirmed": stage_confirmed,
                    "field_type": field_type,
                    "area_mu": area,
                    "inspection_requested": inspection,
                    "maintenance_need_confirmed": need,
                    "maintenance_problem_confirmed": problem,
                    "ditch_connectivity": connectivity,
                    "moisture": moisture,
                    "standing_water": water,
                    "actual_rain_observed": rain,
                    "leaf_count": leaf_count,
                    "leaf_count_known": leaf_count_known,
                    "inspection_duration_minutes": inspection_duration,
                    "maintenance_duration_minutes": maintenance_duration,
                    "inspection_priority": inspection_priority,
                    "maintenance_priority": maintenance_priority,
                    "inspection_earliest_date": inspection_earliest,
                    "inspection_due_date": inspection_due,
                    "maintenance_earliest_date": maintenance_earliest,
                    "maintenance_due_date": maintenance_due,
                    "requires_inspection": requires_inspection,
                }

        st.markdown("#### 每日作业确认和可用时段")
        day_values: dict[date, dict[str, object]] = {}
        for day in request.days:
            with st.expander(day.day.strftime("%Y-%m-%d · 每日条件与资源")):
                plots_for_day: dict[str, tuple[bool | None, bool | None]] = {}
                for plot in request.plots:
                    current = plot.workability_by_date.get(day.day)
                    site_default = current.site if current else day.site_workable
                    weather_default = current.weather if current else day.weather_workable
                    with st.container(border=True):
                        st.caption(plot.name)
                        site = _tri_select(
                            "现场能否作业",
                            site_default,
                            f"v2_site_{plot.id}_{day.day.isoformat()}_{epoch}",
                        )
                        weather = _tri_select(
                            "天气能否作业",
                            weather_default,
                            f"v2_weather_{plot.id}_{day.day.isoformat()}_{epoch}",
                        )
                        plots_for_day[plot.id] = (site, weather)
                intervals = st.text_input(
                    "可用时段（可多段，用分号分隔）",
                    value=_format_intervals(day.intervals),
                    help="例如 09:00-11:00;13:00-15:00。相邻时段自动合并，重叠时段会提示错误。",
                    key=f"v2_intervals_{day.day.isoformat()}_{epoch}",
                )
                resource_known = st.checkbox(
                    "已确认当天可用分钟数",
                    value=day.resource_minutes is not None,
                    key=f"v2_resource_known_{day.day.isoformat()}_{epoch}",
                )
                resource = st.number_input(
                    "可用分钟数（单人）",
                    min_value=0,
                    value=day.resource_minutes or 0,
                    step=15,
                    disabled=not resource_known,
                    key=f"v2_resource_{day.day.isoformat()}_{epoch}",
                )
                day_values[day.day] = {
                    "plots": plots_for_day,
                    "intervals": intervals,
                    "resource_known": resource_known,
                    "resource_minutes": resource,
                }

        if st.form_submit_button("保存地块与逐日输入", key="v2_save_inputs"):
            updated = request.model_copy(deep=True)
            try:
                site_changed_by_plot: dict[str, bool] = {}
                weather_changed_by_plot: dict[str, bool] = {}
                for plot in updated.plots:
                    values = dict(plot_values[plot.id])
                    values["area_mu"] = float(values["area_mu"]) or None
                    values["inspection_duration_minutes"] = (
                        int(values["inspection_duration_minutes"]) or None
                    )
                    values["maintenance_duration_minutes"] = (
                        int(values["maintenance_duration_minutes"]) or None
                    )
                    values["leaf_count"] = (
                        float(values["leaf_count"]) if values.pop("leaf_count_known") else None
                    )
                    changed_observation = any(
                        getattr(plot, field) != values[field]
                        for field in (
                            "maintenance_problem_confirmed",
                            "ditch_connectivity",
                            "moisture",
                            "standing_water",
                            "actual_rain_observed",
                            "leaf_count",
                        )
                    )
                    site_changed_by_plot[plot.id] = any(
                        getattr(plot, field) != values[field]
                        for field in ("ditch_connectivity", "moisture", "standing_water")
                    )
                    weather_changed_by_plot[plot.id] = (
                        plot.actual_rain_observed != values["actual_rain_observed"]
                    )
                    for field, value in values.items():
                        if field in {"requires_inspection"}:
                            continue
                        setattr(plot, field, value)
                    plot.maintenance_prerequisites = (
                        [f"{plot.id}-I"] if values["requires_inspection"] else []
                    )
                    if changed_observation:
                        plot.observed_at = datetime.now(UTC)
                        plot.observation_source = "observed"
                for day in updated.days:
                    values = day_values[day.day]
                    intervals = _parse_intervals(str(values["intervals"]))
                    day.intervals = intervals
                    day.resource_minutes = (
                        int(values["resource_minutes"]) if values["resource_known"] else None
                    )
                    for plot in updated.plots:
                        site, weather = values["plots"][plot.id]
                        current = plot.workability_by_date.get(day.day)
                        old_weather = current.weather if current else day.weather_workable
                        weather_source = current.weather_source if current else day.weather_source
                        weather_snapshot_at = (
                            current.weather_snapshot_at if current else day.weather_snapshot_at
                        )
                        if (
                            st.session_state.v2_plan is not None
                            and day.day >= datetime.now(LOCAL_ZONE).date()
                        ):
                            if site_changed_by_plot[plot.id]:
                                site = None if site is True else site
                            if weather_changed_by_plot[plot.id]:
                                weather = None if weather is True else weather
                        if weather is None:
                            weather_source = "synthetic"
                            weather_snapshot_at = None
                        elif weather != old_weather:
                            weather_source = "user_estimate"
                            weather_snapshot_at = datetime.now(UTC)
                        plot.workability_by_date[day.day] = WorkabilityConfirmation(
                            site=site,
                            weather=weather,
                            weather_source=weather_source,
                            weather_snapshot_at=weather_snapshot_at,
                        )
                st.session_state.v2_request = PlanRequestV2.model_validate(
                    updated.model_dump(mode="python")
                )
            except (ValueError, TypeError) as error:
                st.error(f"输入无效：{error}")
            else:
                st.success("输入已保存。生成安排后再记录反馈。")


def _store_feedback(store: PlanStoreV2, plan: PlanVersionV2, feedback: FeedbackV2) -> None:
    try:
        _save_version(store, apply_feedback_v2(plan, feedback))
    except (ValueError, KeyError) as error:
        st.error(f"反馈未保存：{error}")


def _render_feedback(store: PlanStoreV2, plan: PlanVersionV2) -> None:
    st.markdown("#### 记录变化并重排")
    now = datetime.now(UTC)
    complete = [item for item in plan.items if item.start_at and not item.completed]
    if complete:
        with st.form("v2_completion"):
            task_id = st.selectbox(
                "已完成的任务",
                [item.task_id for item in complete],
                format_func=lambda value: next(
                    f"{item.title} · {item.plot_id}" for item in complete if item.task_id == value
                ),
                key="v2_complete_task",
            )
            if st.form_submit_button("记录完成并重排", key="v2_submit_completion"):
                _store_feedback(
                    store,
                    plan,
                    FeedbackV2(kind="completed", task_id=task_id, at=now),
                )

    with st.form("v2_observation"):
        plot_id = st.selectbox(
            "反馈地块",
            [plot.id for plot in plan.request.plots],
            format_func=lambda value: next(
                plot.name for plot in plan.request.plots if plot.id == value
            ),
            key="v2_observation_plot",
        )
        field = st.selectbox(
            "现场观察项",
            list(FIELD_LABEL),
            format_func=FIELD_LABEL.get,
            key="v2_observation_field",
        )
        value_text = st.text_input(
            "观察值",
            value="未知",
            help=(
                "布尔项填写 true/false/未知；沟系填写 clear/impaired/未知；"
                "干湿填写 dry/normal/wet/未知。"
            ),
            key="v2_observation_value",
        )
        if st.form_submit_button("保存现场观察并重排", key="v2_submit_observation"):
            try:
                value: str | bool | float | None
                if field in {
                    "maintenance_need_confirmed",
                    "maintenance_problem_confirmed",
                    "standing_water",
                    "actual_rain_observed",
                    "winter_before_context_confirmed",
                }:
                    choices = {"true": True, "false": False, "未知": None, "null": None, "": None}
                    normalized = value_text.strip().lower()
                    if normalized not in choices:
                        raise ValueError("布尔观察值请填写 true、false 或 未知")
                    value = choices[normalized]
                elif field == "leaf_count":
                    value = (
                        None if value_text.strip() in {"", "未知", "null"} else float(value_text)
                    )
                elif field == "moisture":
                    value = None if value_text in {"", "未知", "null"} else value_text
                else:
                    value = None if value_text in {"", "未知", "null"} else value_text
                _store_feedback(
                    store,
                    plan,
                    FeedbackV2(
                        kind="observation",
                        plot_id=plot_id,
                        field=field,
                        value=value,
                        at=now,
                    ),
                )
            except ValueError as error:
                st.error(f"观察值格式不正确：{error}")

    with st.form("v2_workability"):
        work_plot = st.selectbox(
            "重新确认地块",
            [plot.id for plot in plan.request.plots],
            format_func=lambda value: next(
                plot.name for plot in plan.request.plots if plot.id == value
            ),
            key="v2_work_plot",
        )
        work_day = st.selectbox(
            "重新确认日期",
            [day.day for day in plan.request.days],
            key="v2_work_day",
        )
        site = _tri_select("现场能否作业", None, "v2_work_site")
        weather = _tri_select("天气能否作业", None, "v2_work_weather")
        if st.form_submit_button("更新当天条件并重排", key="v2_submit_workability"):
            _store_feedback(
                store,
                plan,
                FeedbackV2(
                    kind="workability",
                    plot_id=work_plot,
                    day=work_day,
                    site_workable=site,
                    weather_workable=weather,
                    at=now,
                ),
            )

    with st.form("v2_resource"):
        resource_day = st.selectbox(
            "调整哪天的人力",
            [day.day for day in plan.request.days],
            key="v2_resource_day",
        )
        resource_intervals = st.text_input(
            "新的可用时段",
            value=_format_intervals(
                next(day for day in plan.request.days if day.day == resource_day).intervals
            ),
            help="多个时段用分号分隔。",
            key="v2_resource_intervals",
        )
        resource_minutes = st.number_input(
            "更新后的可用分钟数",
            min_value=0,
            value=next(day for day in plan.request.days if day.day == resource_day).resource_minutes
            or 0,
            step=15,
            key="v2_resource_minutes",
        )
        if st.form_submit_button("更新人力并重排", key="v2_submit_resource"):
            try:
                intervals = _parse_intervals(resource_intervals)
                _store_feedback(
                    store,
                    plan,
                    FeedbackV2(
                        kind="resource",
                        day=resource_day,
                        intervals=intervals,
                        resource_minutes=resource_minutes,
                        at=now,
                    ),
                )
            except ValueError as error:
                st.error(f"可用时段格式不正确：{error}")


def _render_interaction(store: PlanStoreV2) -> None:
    st.subheader("追问与现场反馈")
    st.markdown(
        '<div class="workbench-note">系统先核对范围、需求、每天的现场与天气条件，'
        "再检查分钟数、人员和前置任务。未知条件会列出需要补充的信息。</div>",
        unsafe_allow_html=True,
    )
    if st.button("生成七天安排", key="v2_generate", type="primary", width="stretch"):
        try:
            current = st.session_state.v2_plan
            version = build_plan_v2(st.session_state.v2_request, previous=current)
            store.save(version)
            st.session_state.v2_plan = version
            st.session_state.v2_request = version.request.model_copy(deep=True)
            st.session_state.v2_input_epoch += 1
            st.session_state.pop("v2_plan_head", None)
            st.rerun()
        except ValueError as error:
            st.error(f"暂时无法生成安排：{error}")

    plan: PlanVersionV2 | None = st.session_state.v2_plan
    if plan is None:
        st.info("保存地块和每日条件后，点击「生成七天安排」。")
        return
    if st.session_state.v2_request != plan.request:
        st.warning("已保存输入尚未进入当前计划。先重新生成安排，再记录反馈。")
        return
    pending = [item for item in plan.items if item.schedule_status == "pending"]
    if pending:
        st.markdown("##### 需要补充")
        for item in pending:
            missing = (
                "、".join(item.missing_fields) if item.missing_fields else "现场需求或适用范围"
            )
            st.write(f"{item.plot_id} · {item.title}：{missing}。{item.reason}")
    _render_feedback(store, plan)


def _render_results(v2_store: PlanStoreV2, legacy_store: PlanStore) -> None:
    st.subheader("七天安排与依据")
    latest: PlanVersionV2 | None = st.session_state.v2_plan
    if latest is None:
        st.info("生成计划后，这里会显示具体时段、未排入原因和证据。")
        return
    heads = v2_store.heads()
    head_by_id = {version.id: version for version in heads}
    current_head_id = latest.id
    if current_head_id not in head_by_id:
        current_head_id = next(
            (
                head.id
                for head in heads
                if any(version.id == latest.id for version in v2_store.history(head.id))
            ),
            heads[0].id,
        )
    if st.session_state.get("v2_plan_head") not in head_by_id:
        st.session_state.pop("v2_plan_head", None)
    selected_head = st.selectbox(
        "选择计划",
        list(head_by_id),
        index=list(head_by_id).index(current_head_id),
        format_func=lambda value: (
            f"{head_by_id[value].reference_version or '本地计划'} · "
            f"{head_by_id[value].created_at.astimezone(LOCAL_ZONE):%m-%d %H:%M} · "
            f"{value[:8]}"
        ),
        key="v2_plan_head",
    )
    if selected_head != latest.id:
        selected = head_by_id[selected_head]
        st.session_state.v2_plan = selected
        st.session_state.v2_request = selected.request.model_copy(deep=True)
        st.session_state.v2_input_epoch += 1
        st.rerun()
    history = v2_store.history(latest.id)
    history_by_id = {version.id: version for version in history}
    selected_id = st.selectbox(
        "查看历史版本",
        list(history_by_id),
        format_func=lambda value: (
            f"{history_by_id[value].reference_version or '计划'} · "
            f"{history_by_id[value].created_at.astimezone(LOCAL_ZONE):%m-%d %H:%M}"
        ),
        key="v2_history_select",
    )
    plan = history_by_id[selected_id]
    st.caption(
        f"C卡：{plan.request.ruleset_version} · {len(plan.request.selected_rule_ids)} 张候选卡 · "
        f"R卡：{plan.request.legacy_rule_version}（{plan.request.legacy_mapping_type}） · "
        f"D约定：{plan.request.engineering_rules_version} / "
        f"{len(plan.request.engineering_rule_ids)} 条独立工程约定 · 开发验证，适用性待复核"
    )
    if plan.request.synthetic:
        st.info("本计划使用模拟地块和模拟时间，不代表田间事实或农事处方。")
    with st.expander("逐日天气确认来源与快照时间"):
        for day in plan.request.days:
            for plot in plan.request.plots:
                confirmation = plot.workability_by_date.get(day.day)
                source = confirmation.weather_source if confirmation else day.weather_source
                snapshot = (
                    confirmation.weather_snapshot_at if confirmation else day.weather_snapshot_at
                )
                stamp = (
                    snapshot.astimezone(LOCAL_ZONE).strftime("%m-%d %H:%M") if snapshot else "无"
                )
                st.caption(f"{day.day:%m-%d} · {plot.name} · {source} · 快照时间 {stamp}")
    for day in plan.request.days:
        day_items = sorted(
            [item for item in plan.items if item.start_at and item.start_at.date() == day.day],
            key=lambda item: item.start_at or datetime.max.replace(tzinfo=LOCAL_ZONE),
        )
        capacity = day.resource_minutes if day.resource_minutes is not None else "未确认"
        st.markdown(f"**{day.day:%m月%d日}**　{capacity} 可用分钟")
        if not day_items:
            st.caption("没有已安排任务")
        for item in day_items:
            with st.container(border=True):
                st.write(
                    f"{item.start_at:%H:%M}–{item.end_at:%H:%M}　{item.title} · {item.plot_id} · "
                    f"{item.duration_minutes or '未知'} 分钟"
                )
                st.caption(f"{_v2_status(item)}｜{item.reason}")
                _render_rule_evidence(item)
    not_scheduled = [
        item
        for item in plan.items
        if item.start_at is None and item.schedule_status != "not_generated"
    ]
    if not_scheduled:
        st.markdown("#### 待确认或未排入")
        for item in not_scheduled:
            with st.container(border=True):
                st.write(f"{item.plot_id} · {item.title} · {_v2_status(item)}")
                missing = ", ".join(item.missing_fields) if item.missing_fields else "无缺项"
                st.caption(f"{item.reason} · {missing}")
                _render_rule_evidence(item)
    if plan.changes:
        with st.expander("本次调整与旧版差异", expanded=True):
            for change in plan.changes:
                st.write(change)

    with st.expander("本计划可用的候选依据"):
        catalog = load_rule_catalog()
        for rule_id in plan.request.selected_rule_ids:
            card = catalog.cards[rule_id]
            st.markdown(f"**{rule_id} · {card.title}**")
            st.caption("开发验证，适用性待复核；原始卡片 runtime_enabled=false")
            st.write(card.condition)
            st.write(card.source_supported_knowledge)
            st.caption(f"适用范围：{card.applicability}｜限制：{card.limits}")
            for evidence in catalog.evidence_for([rule_id]):
                st.write(
                    f"{evidence['source_id']} · {evidence['source_title']} · "
                    f"PDF {', '.join(map(str, evidence['pdf_pages']))}页"
                )
                st.write(evidence["quote"])
                if evidence["source_url"]:
                    st.link_button("打开原始来源", evidence["source_url"])

    legacy: PlanVersion | None = legacy_store.latest()
    if legacy:
        with st.expander("旧版 SQLite 计划（只读）"):
            st.caption("旧版只保存日期与总工时，没有具体时刻和逐日作业确认；这里仅按原样查看。")
            legacy_history = legacy_store.history(legacy.id)
            legacy_versions = {version.id: version for version in legacy_history}
            legacy_id = st.selectbox(
                "旧版历史",
                list(legacy_versions),
                format_func=lambda value: (
                    f"旧版第 {list(legacy_versions).index(value) + 1} 版 · "
                    f"{legacy_versions[value].created_at.astimezone(LOCAL_ZONE):%m-%d %H:%M}"
                ),
                key="legacy_history_select",
            )
            for item in legacy_versions[legacy_id].items:
                st.write(
                    f"{item.scheduled_date.isoformat() if item.scheduled_date else '未排入'} · "
                    f"{item.title} · {item.reason}"
                )


def _replay(store: PlanStoreV2) -> None:
    try:
        versions = replay_reference_history()
        for version in versions:
            store.save(version)
        latest = versions[-1]
        st.session_state.v2_plan = latest
        st.session_state.v2_request = latest.request.model_copy(deep=True)
        st.session_state.v2_input_epoch += 1
        st.session_state.pop("v2_plan_head", None)
        st.rerun()
    except ValueError as error:
        st.error(f"参考案例重放失败：{error}")


def main() -> None:
    st.set_page_config(page_title="农时窗口 · 规则验证版", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    settings = Settings()
    v2_store = PlanStoreV2(settings.database_path)
    legacy_store = PlanStore(settings.database_path)
    _load_state(v2_store)

    st.markdown(
        '<div class="workbench-kicker">FIELD RECORDS / SEVEN-DAY PLAN</div>', unsafe_allow_html=True
    )
    st.title("农时窗口")
    st.caption("油菜苗期农事规划 · 本地规则验证版")
    st.markdown("候选规则只提供可追溯依据；是否适用、是否需要、当天能否作业和是否排入分别判断。")
    left, middle, right = st.columns([1.1, 1.05, 1.55], gap="large")
    with left:
        _render_inputs()
    with middle:
        _render_interaction(v2_store)
        with st.expander("参考案例重放 V1 → V2 → V3"):
            st.caption("固定使用案例内的模拟时间；运行结果由规则判断和排程器重新计算。")
            if st.button("运行三版重放", key="v2_replay"):
                _replay(v2_store)
    with right:
        _render_results(v2_store, legacy_store)


if __name__ == "__main__":
    main()
