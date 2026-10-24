"""任务详情对话框、单一「下一步」主按钮与无障碍样式的显示层测试。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

from nongshi import workbench
from nongshi.schema_v2 import PlanItemV2
from nongshi.workbench import PALETTE, ZONE, _item_detail_rows, _primary_action

APP = Path(__file__).resolve().parents[1] / "src" / "nongshi" / "app.py"

START = datetime(2026, 10, 21, 9, 0, tzinfo=ZONE)
END = datetime(2026, 10, 21, 9, 30, tzinfo=ZONE)
DETAIL_LABELS = {
    "任务",
    "编号",
    "日期与时间",
    "预计时长",
    "窗口",
    "适用性",
    "需求",
    "执行状态",
    "安排原因",
    "待确认项",
}


def _scheduled_item() -> PlanItemV2:
    return PlanItemV2(
        task_id="field-abc-I",
        plot_id="field-abc",
        title="巡田苗情墒情虫情与沟渠检查",
        task_type="inspection",
        applicability="matched",
        necessity="required",
        window_by_date={date(2026, 10, 21): "available"},
        schedule_status="scheduled",
        duration_minutes=30,
        start_at=START,
        end_at=END,
        reason="依据已满足，按优先级、期限和可用工时安排",
        missing_fields=[],
    )


def _pending_item() -> PlanItemV2:
    return PlanItemV2(
        task_id="field-abc-M",
        plot_id="field-abc",
        title="既有沟渠维护候选",
        task_type="maintenance",
        applicability="pending",
        necessity="unknown",
        window_by_date={},
        schedule_status="pending",
        duration_minutes=None,
        reason="需求或现场问题尚未确认",
        missing_fields=["maintenance_problem_confirmed"],
    )


def test_item_detail_rows_lists_required_labels_and_values() -> None:
    rows = dict(_item_detail_rows(_scheduled_item()))
    assert DETAIL_LABELS <= set(rows)
    # 调整原因没有数据时不编造，直接省略。
    assert "调整原因" not in rows
    assert "巡田检查" in rows["任务"]
    assert rows["编号"] == "field-abc-I"
    assert rows["窗口"] == "可安排"
    assert rows["适用性"] == "适用"
    assert rows["需求"] == "需要"
    assert rows["执行状态"] == "已安排"
    assert rows["预计时长"] == "30 分钟"
    assert rows["待确认项"] == "无"
    assert "2026-10-21" in rows["日期与时间"]


def test_item_detail_rows_marks_unscheduled_and_missing_fields() -> None:
    rows = dict(_item_detail_rows(_pending_item()))
    assert rows["日期与时间"] == "未排入"
    assert rows["预计时长"] == "未提供"
    assert rows["窗口"] == "未提供"
    assert rows["适用性"] == "待确认"
    assert rows["执行状态"] == "待确认"
    assert rows["待确认项"] == "是否存在需处理的沟渠问题"


def test_completed_task_is_not_counted_as_remaining_scheduled_work(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    markup: list[str] = []
    captions: list[str] = []
    monkeypatch.setattr(workbench.st, "markdown", lambda value, **_: markup.append(value))
    monkeypatch.setattr(workbench.st, "caption", captions.append)

    workbench._render_count_strip(
        [_scheduled_item().model_copy(update={"schedule_status": "completed", "completed": True})]
    )

    assert 'count-number count-green">0' in markup[0]
    assert "已完成 1 项" in captions[0]


def test_primary_action_priority() -> None:
    # 采用预览 > 生成/重算计划 > 保存档案。
    assert _primary_action(True, True) == "adopt"
    assert _primary_action(True, False) == "adopt"
    assert _primary_action(False, True) == "plan"
    assert _primary_action(False, False) == "save"


def test_palette_defines_focus_visible_and_reduced_motion() -> None:
    assert ":focus-visible" in PALETTE
    assert "prefers-reduced-motion" in PALETTE
    # 正文基准字号保持 16px。
    assert "font-size: 16px" in PALETTE


def _render_field(tmp_path: Path, monkeypatch) -> AppTest:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("验收一号田")
    app.text_input(key=f"field_province:{field_id}:0").set_value("湖北")
    app.checkbox(key=f"province_confirmed:{field_id}:0").set_value(True)
    app.selectbox(key=f"field_crop:{field_id}:0").set_value("油菜")
    app.checkbox(key=f"crop_confirmed:{field_id}:0").set_value(True)
    app.selectbox(key=f"field_stage:{field_id}:0").set_value("苗期")
    app.checkbox(key=f"stage_confirmed:{field_id}:0").set_value(True)
    app.selectbox(key=f"field_type:{field_id}:0").set_value("低洼/稻茬田")
    app.selectbox(key=f"inspection_requested:{field_id}:0").set_value("是")
    app.number_input(key=f"inspection_minutes:{field_id}:0").set_value(30)
    app.button(key=f"save_field:{field_id}:0").click().run()
    return app


def test_detail_button_opens_dialog_with_task_fields(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app = _render_field(tmp_path, monkeypatch)
    assert not app.exception

    buttons = [element for element in app.button if element.label == "查看详情"]
    assert buttons, "任务卡主区应提供「查看详情」按钮"

    buttons[0].click().run()
    assert not app.exception

    markdown = "\n".join(element.value for element in app.markdown)
    for label in DETAIL_LABELS:
        assert label in markdown
    # 依据：有依据时显示来源折叠区，无依据时如实提示，不编造来源。
    expander_labels = [element.label for element in app.expander]
    captions = "\n".join(element.value for element in app.caption)
    assert "规则依据与来源" in expander_labels or "当前资料不足以支持这项建议" in captions


def test_result_area_marks_single_primary_action(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app = _render_field(tmp_path, monkeypatch)
    assert not app.exception

    field_id = app.session_state["new_field_id:real"]
    # 已有档案：档案表单改由「编辑当前地块」对话框承载，先打开它。
    app.button(key=f"edit_field:{field_id}").click().run()
    assert not app.exception
    # 仍有待确认信息时，补充该信息是唯一主操作。
    generate = app.button(key=f"preview_plan:{field_id}")
    save = app.button(key=f"save_field:{field_id}:1")
    clarify = app.button(key=f"next_question:{field_id}")
    assert generate.proto.type == "secondary"
    assert clarify.proto.type == "primary"
    assert save.proto.type == "secondary"
