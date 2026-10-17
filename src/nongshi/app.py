"""Streamlit entrypoint for the clearly labeled offline planning demonstration."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import streamlit as st

from nongshi.demo import make_demo_request
from nongshi.planning import apply_feedback, build_plan
from nongshi.repository import PlanStore
from nongshi.schema import Feedback, PlanItem, PlanRequest, PlanVersion

ZONE = ZoneInfo("Asia/Shanghai")
DITCH_OPTIONS = {"unknown": "未知", "clear": "未见堵塞", "blocked": "现场确认堵塞"}
ACCESS_OPTIONS = {"unknown": "未知", "yes": "可以进入", "no": "暂不可进入"}
OBSERVATION_LABELS = {"ditch_condition": "沟渠情况", "field_access": "田间可进入性"}
STATUS_LABELS = {"ready": "可安排", "blocked": "暂不可安排", "pending": "待确认"}
TASK_KIND_LABELS = {
    "inspect_seedling": "巡田苗情检查",
    "inspect_ditch": "沟渠检查",
    "maintain_ditch": "沟渠维护",
    "other": "其他任务",
}

PALETTE = """
<style>
.stApp { background: #f4f7f4; color: #193b36; }
[data-testid="stHeader"] { background: rgba(244,247,244,.92); }
h1 { font-family: 'Microsoft YaHei', 'Noto Sans SC', sans-serif;
     font-weight: 800; letter-spacing: -.055em; color: #193b36; }
h2, h3 { color: #254c41; }
.field-note { border-left: 4px solid #bd983c; padding: .55rem .85rem;
              background: #eef2e7; border-radius: 0 .45rem .45rem 0;
              font-size: .94rem; line-height: 1.6; color: #29473c; }
.field-eyebrow { color: #55766b; font-size: .75rem; letter-spacing: .12em;
                 font-weight: 700; text-transform: uppercase; }
[data-testid="stVerticalBlockBorderWrapper"] { border-color: #d6e0d6; }
[data-testid="stButton"] button[kind="primary"] { background: #2b7162; border-color: #2b7162; }
</style>
"""


def _today() -> date:
    return datetime.now(ZONE).date()


def _load_state(store: PlanStore) -> None:
    if "nongshi_loaded" in st.session_state:
        return
    latest = store.latest()
    st.session_state.plan = latest
    st.session_state.request = (
        latest.request.model_copy(deep=True) if latest else make_demo_request(_today())
    )
    if latest:
        st.session_state.history_version = latest.id
    st.session_state.input_epoch = 0
    st.session_state.nongshi_loaded = True


def _store_plan(store: PlanStore, version: PlanVersion) -> None:
    store.save(version)
    st.session_state.plan = version
    st.session_state.request = version.request.model_copy(deep=True)
    st.session_state.history_version = version.id
    st.session_state.input_epoch += 1
    st.rerun()


def _status(item: PlanItem, request: PlanRequest) -> str:
    if item.completed:
        return "已完成"
    label = STATUS_LABELS[item.window_status]
    return f"{label}（演示）" if request.demo_mode and item.window_status == "ready" else label


def _render_evidence(item: PlanItem) -> None:
    if not item.evidence:
        return
    with st.expander("查看依据与适用条件"):
        for evidence in item.evidence:
            if evidence.simulation:
                st.warning("工程演示设定，尚未经过农事来源核对。")
            st.write(f"规则：{evidence.rule_id} · 版本 {evidence.rule_version}")
            st.write(f"来源：{evidence.source_title}")
            if evidence.source_date:
                st.write(f"发布日期：{evidence.source_date.isoformat()}")
            if evidence.source_location:
                st.write(f"原文位置：{evidence.source_location}")
            st.write(f"适用范围：{evidence.region} · {evidence.stage}")
            if evidence.conditions:
                conditions = "；".join(
                    f"{OBSERVATION_LABELS.get(field, field)}＝{value}"
                    for field, value in evidence.conditions.items()
                )
                st.write(f"条件：{conditions}")
            if evidence.inapplicable_when:
                exclusions = "；".join(
                    f"{OBSERVATION_LABELS.get(field, field)}＝{value}"
                    for field, value in evidence.inapplicable_when.items()
                )
                st.write(f"不适用：{exclusions}")
            if evidence.required_prior_task_kinds:
                st.write(
                    "前置任务："
                    + "、".join(
                        TASK_KIND_LABELS[kind] for kind in evidence.required_prior_task_kinds
                    )
                )
            if evidence.confirmed_observation_fields:
                st.write(
                    "需现场记录："
                    + "、".join(
                        OBSERVATION_LABELS.get(field, field)
                        for field in evidence.confirmed_observation_fields
                    )
                )
            st.write(evidence.source_excerpt)
            if evidence.source_url:
                st.link_button("打开原始资料", evidence.source_url)


def _render_inputs() -> None:
    request: PlanRequest = st.session_state.request
    epoch: int = st.session_state.input_epoch
    st.subheader("地块与资源")
    st.caption("地块、任务和工时均为模拟；0 小时或 0 亩表示尚未填写。面积不用于估算工时。")
    with st.form("inputs"):
        plot_values: dict[str, tuple[bool, str, float, str, str]] = {}
        for plot in request.plots:
            with st.expander(plot.name, expanded=plot.id == "p2"):
                st.caption(f"{plot.region} · {plot.stage or '生育期未知'}")
                location = st.text_input(
                    "位置说明（模拟）",
                    value=plot.location_label or "",
                    max_chars=120,
                    key=f"location_{plot.id}_{epoch}",
                )
                area = st.number_input(
                    "面积（亩）",
                    min_value=0.0,
                    value=float(plot.area_mu or 0),
                    step=0.5,
                    key=f"area_{plot.id}_{epoch}",
                )
                if plot.observation_times:
                    last_observed = max(plot.observation_times.values()).astimezone(ZONE)
                    st.caption(f"最近现场记录：{last_observed:%Y-%m-%d %H:%M}")
                confirmed = st.checkbox(
                    "已由用户确认进入苗期",
                    value=plot.stage_confirmed,
                    key=f"stage_{plot.id}_{epoch}",
                )
                ditch = st.selectbox(
                    "沟渠情况",
                    options=list(DITCH_OPTIONS),
                    index=list(DITCH_OPTIONS).index(
                        plot.observations.get("ditch_condition", "unknown")
                    ),
                    format_func=DITCH_OPTIONS.get,
                    key=f"ditch_{plot.id}_{epoch}",
                )
                access = st.selectbox(
                    "田间可进入性",
                    options=list(ACCESS_OPTIONS),
                    index=list(ACCESS_OPTIONS).index(
                        plot.observations.get("field_access", "unknown")
                    ),
                    format_func=ACCESS_OPTIONS.get,
                    key=f"access_{plot.id}_{epoch}",
                )
                plot_values[plot.id] = (confirmed, location, area, ditch, access)
        with st.expander("任务估时与优先级"):
            st.caption("已完成任务的估时和优先级锁定。")
            task_values: dict[str, tuple[float, int]] = {}
            for task in request.tasks:
                st.caption(f"{task.title} · {task.plot_id}")
                hours = st.number_input(
                    "预计工时",
                    min_value=0.0,
                    max_value=24.0,
                    value=float(task.hours or 0),
                    step=0.5,
                    key=f"hours_{task.id}_{epoch}",
                    disabled=task.completed_at is not None,
                )
                priority = st.selectbox(
                    "优先级（1 最高）",
                    options=[1, 2, 3],
                    index=task.priority - 1,
                    key=f"priority_{task.id}_{epoch}",
                    disabled=task.completed_at is not None,
                )
                task_values[task.id] = (hours, priority)
        with st.expander("未来七天可用人时"):
            capacity: dict[date, float] = {}
            for day in request.days:
                capacity[day.day] = st.number_input(
                    day.day.strftime("%m月%d日"),
                    min_value=0.0,
                    max_value=48.0,
                    value=float(day.hours),
                    step=0.5,
                    key=f"capacity_{day.day.isoformat()}_{epoch}",
                )
        if st.form_submit_button("保存输入", key="save_inputs"):
            updated = request.model_copy(deep=True)
            for plot in updated.plots:
                confirmed, location, area, ditch, access = plot_values[plot.id]
                plot.stage_confirmed = confirmed
                plot.location_label = location.strip() or None
                plot.area_mu = area or None
                for field, value in (("ditch_condition", ditch), ("field_access", access)):
                    old_value = plot.observations.get(field, "unknown")
                    plot.observations[field] = value
                    if value == "unknown":
                        plot.observation_times.pop(field, None)
                    elif value != old_value:
                        plot.observation_times[field] = datetime.now(UTC)
            for task in updated.tasks:
                hours, priority = task_values[task.id]
                task.hours = hours or None
                task.priority = priority
            for day in updated.days:
                day.hours = capacity[day.day]
            try:
                st.session_state.request = PlanRequest.model_validate(updated.model_dump())
            except ValueError as error:
                st.error(f"输入有误：{error}")
            else:
                st.success("输入已保存，点击「生成七天安排」查看结果。")


def _render_feedback(store: PlanStore, plan: PlanVersion) -> None:
    st.markdown("#### 记录现场变化")
    task_by_id = {task.id: task for task in plan.request.tasks}
    open_tasks = [item.task_id for item in plan.items if item.scheduled_date and not item.completed]
    if open_tasks:
        with st.form("completion"):
            task_id = st.selectbox(
                "已完成的任务",
                open_tasks,
                format_func=lambda value: (
                    f"{task_by_id[value].title} · {task_by_id[value].plot_id}"
                ),
                key="completion_task",
            )
            if st.form_submit_button("标记完成并重排", key="submit_completion"):
                _store_plan(
                    store,
                    apply_feedback(
                        plan, Feedback(kind="completed", task_id=task_id, at=datetime.now(UTC))
                    ),
                )
    with st.form("observation"):
        plot_id = st.selectbox(
            "观察地块",
            [plot.id for plot in plan.request.plots],
            index=1 if len(plan.request.plots) > 1 else 0,
            format_func=lambda value: next(
                plot.name for plot in plan.request.plots if plot.id == value
            ),
            key="observation_plot",
        )
        value = st.selectbox(
            "沟渠现场情况",
            list(DITCH_OPTIONS),
            format_func=DITCH_OPTIONS.get,
            key="observation_value",
        )
        if st.form_submit_button("记录观察并重排", key="submit_observation"):
            _store_plan(
                store,
                apply_feedback(
                    plan,
                    Feedback(
                        kind="observation",
                        plot_id=plot_id,
                        field="ditch_condition",
                        value=value,
                        at=datetime.now(UTC),
                    ),
                ),
            )
    with st.form("availability"):
        day = st.selectbox(
            "调整哪天的人力",
            [entry.day for entry in plan.request.days],
            index=1,
            key="availability_day",
        )
        hours = st.number_input(
            "调整后的可用人时",
            min_value=0.0,
            max_value=48.0,
            value=float(next(entry.hours for entry in plan.request.days if entry.day == day)),
            step=0.5,
            key="availability_hours",
        )
        if st.form_submit_button("更新人力并重排", key="submit_availability"):
            _store_plan(
                store,
                apply_feedback(
                    plan, Feedback(kind="availability", day=day, hours=hours, at=datetime.now(UTC))
                ),
            )


def _render_flow(store: PlanStore) -> None:
    st.subheader("互动记录")
    st.markdown(
        '<div class="field-note">先核对地块与可用人时，再生成安排。'
        "二号田沟渠情况未知时，维护候选任务会保持待确认。</div>",
        unsafe_allow_html=True,
    )
    if st.button("生成七天安排", key="generate_plan", type="primary", width="stretch"):
        try:
            _store_plan(store, build_plan(st.session_state.request, previous=st.session_state.plan))
        except ValueError as error:
            st.error(f"无法生成安排：{error}")
    plan: PlanVersion | None = st.session_state.plan
    if plan is None:
        st.info("尚无计划。核对左侧输入后，点击「生成七天安排」。")
        return
    pending = [
        item for item in plan.items if item.window_status == "pending" and not item.completed
    ]
    if pending:
        with st.chat_message("assistant"):
            st.write("这些信息尚未核实，相关任务暂不进入日期安排：")
            for item in pending:
                st.write(f"• {item.title}（{item.plot_id}）：{item.reason}")
    if st.session_state.request != plan.request:
        st.warning("已保存输入尚未进入当前计划。请先生成七天安排，再记录反馈。")
        return
    _render_feedback(store, plan)


def _render_results(store: PlanStore) -> None:
    st.subheader("七天安排")
    latest: PlanVersion | None = st.session_state.plan
    if latest is None:
        st.info("生成计划后，这里会显示每日任务、待确认项与依据。")
        return
    history = store.history(latest.id)
    by_id = {version.id: version for version in history}
    selected_id = st.selectbox(
        "查看计划版本",
        [version.id for version in history],
        format_func=lambda value: (
            f"第 {list(by_id).index(value) + 1} 版 · "
            f"{by_id[value].created_at.astimezone(ZONE):%m-%d %H:%M}"
        ),
        key="history_version",
    )
    plan = by_id[selected_id]
    weather = plan.request.weather
    weather_time = (
        weather.issued_at.astimezone(ZONE).strftime("%Y-%m-%d %H:%M")
        if weather.issued_at
        else "无时间"
    )
    if weather.status == "unavailable":
        st.warning("天气不可用；依赖天气的规则保持待确认。")
    else:
        st.caption(f"天气：{weather.source_label} · {weather_time} · {weather.summary}")
    if plan.request.demo_mode:
        st.info("演示数据与规则尚未经过农事来源核对，结果只用于测试流程。")
    for conflict in plan.conflicts:
        st.error(f"人时冲突：{conflict}")

    for day in plan.request.days:
        day_items = sorted(
            (item for item in plan.items if item.scheduled_date == day.day),
            key=lambda item: item.sequence or 0,
        )
        used = sum(item.hours or 0 for item in day_items)
        st.markdown(f"**{day.day:%m月%d日}**　{used:g} / {day.hours:g} 人时")
        st.progress(min(1.0, used / day.hours) if day.hours else 0.0)
        for item in day_items:
            with st.container(border=True):
                st.write(f"{item.title} · {item.plot_id} · {item.hours:g} 人时")
                st.caption(f"{_status(item, plan.request)}｜{item.reason}")
                _render_evidence(item)
        if not day_items:
            st.caption("未安排任务")
    unscheduled = [
        item for item in plan.items if item.scheduled_date is None and not item.completed
    ]
    if unscheduled:
        st.markdown("#### 待确认或未排入")
        for item in unscheduled:
            with st.container(border=True):
                st.write(f"{item.title} · {item.plot_id} · {_status(item, plan.request)}")
                st.caption(item.reason)
                _render_evidence(item)
    if plan.changes:
        with st.expander("本次调整与旧版差异", expanded=True):
            for change in plan.changes:
                st.write(f"• {change}")


def main() -> None:
    from nongshi.app_v2 import main as main_v2

    main_v2()


if __name__ == "__main__":
    main()
