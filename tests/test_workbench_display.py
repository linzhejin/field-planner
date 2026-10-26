"""显示层：候选卡用中文状态胶囊，主界面不直接暴露英文枚举或任务编号。"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

import nongshi.workbench as workbench
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import FeedbackV2, PlanItemV2, TimeWindow
from nongshi.schema_v3 import FactEventInputV3, FieldDraftV3, FieldStateV3, PlanPreviewV3
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.workbench import (
    _adopted_display_items,
    _day_workability_label,
    _item_detail_rows,
    _preview_matches_state,
    _proposal_event_description,
)

APP = Path(__file__).resolve().parents[1] / "src" / "nongshi" / "app.py"


def test_adopted_view_shows_saved_completion_before_new_plan_is_adopted() -> None:
    item = PlanItemV2.model_construct(
        task_id="task-1", schedule_status="scheduled", completed=False
    )
    plan = type("Plan", (), {"items": [item], "plan": None})()
    service = type("Service", (), {"_completion_facts": lambda *_: {"task-1": datetime.now()}})()

    shown = _adopted_display_items(service, "field-1", plan)

    assert shown[0].schedule_status == "completed"
    assert shown[0].completed
    assert item.schedule_status == "scheduled"


def test_follow_up_detail_uses_user_labels_and_hides_issue_identifier() -> None:
    item = PlanItemV2.model_construct(
        task_id="task-1",
        title="积水复查",
        task_type="follow_up",
        start_at=None,
        end_at=None,
        duration_minutes=30,
        window_by_date={},
        applicability="matched",
        necessity="required",
        schedule_status="pending",
        reason="待确认",
        missing_fields=[],
        execution_group_id=None,
        template_snapshot={"related_issue_id": "secret-issue-id", "purpose": "核查积水"},
    )

    detail = "\n".join(value for _, value in _item_detail_rows(item))

    assert "问题复查" in detail
    assert "follow_up" not in detail
    assert "secret-issue-id" not in detail


def test_finished_tasks_do_not_mark_another_day_as_unconfirmed() -> None:
    day = date(2026, 10, 29)
    finished = PlanItemV2.model_construct(
        category="onsite",
        schedule_status="completed",
        applicability="matched",
        necessity="required",
        window_by_date={day: "pending"},
    )
    scheduled = PlanItemV2.model_construct(
        category="onsite",
        schedule_status="scheduled",
        applicability="matched",
        necessity="required",
        window_by_date={day: "available"},
    )

    assert _day_workability_label([finished, scheduled], day) == "可作业"


def test_old_preview_is_not_shown_after_a_new_plan_is_adopted() -> None:
    # A previously saved preview may still say "preview" after unrelated edits.
    # It must not reappear beside a later adopted version after page refresh.
    state = FieldStateV3(
        id="field-1",
        mode="real",
        draft_version=2,
        fact_version=1,
        draft=FieldDraftV3(field_id="field-1"),
        latest_plan_id="plan-v2",
        updated_at=datetime(2026, 10, 24, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert not _preview_matches_state(
        PlanPreviewV3.model_construct(
            status="preview",
            expected_draft_version=1,
            expected_fact_version=1,
            expected_plan_id=None,
        ),
        state,
    )


def test_feedback_proposal_shows_exact_resource_change_before_confirmation() -> None:
    event = FactEventInputV3(
        feedback=FeedbackV2(
            kind="resource",
            at=datetime(2026, 10, 24, 9, 35, tzinfo=ZoneInfo("Asia/Shanghai")),
            day=date(2026, 10, 25),
            resource_minutes=60,
            intervals=[TimeWindow(start=time(14), end=time(15))],
        )
    )

    description = _proposal_event_description(event)

    assert "2026-10-25" in description
    assert "14:00–15:00" in description
    assert "60 分钟" in description
    assert "人员" in description


def _render(tmp_path: Path, monkeypatch) -> AppTest:  # type: ignore[no-untyped-def]
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


def test_candidate_card_uses_chinese_status_chip(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app = _render(tmp_path, monkeypatch)
    assert not app.exception

    markdown = "\n".join(element.value for element in app.markdown)
    captions = "\n".join(element.value for element in app.caption)
    joined = markdown + "\n" + captions

    # 状态用带颜色的中文胶囊呈现。
    assert "status-chip" in markdown
    assert "status-green" in markdown or "status-amber" in markdown or "status-red" in markdown

    # 主界面不再直接显示英文枚举或反引号任务编号。
    for token in ("适用性：matched", "适用性：pending", "需求：required", "需求：no_need"):
        assert token not in joined
    assert "`field-" not in markdown

    # 四层状态与任务编号移入「查看详情」对话框，主卡片只保留可扫描的摘要。
    assert any(button.label == "查看详情" for button in app.button)
    assert not re.search(r"任务 (巡田检查|沟渠维护)", joined)
    assert not re.search(r"适用性 (适用|待确认|超出范围)", joined)
    assert not re.search(r"窗口 (可安排|暂不可安排|待确认|未提供)", joined)
    assert not re.search(r"编号 field-", joined)


def test_week_view_renders_unresolved_task_once(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.setenv("NONGSHI_DEFAULT_MODE", "demo")
    demo_database = database.with_name("plans.demo.sqlite3")
    service = WorkbenchServiceV3(WorkbenchStoreV3(demo_database), catalog=load_rule_catalog())
    field_id = workbench._seed_demo(service, scenario="sparse")
    app = AppTest.from_file(APP, default_timeout=30).run()

    app.session_state[f"plan_view:{field_id}"] = "week"
    app.run()
    pending = next(item for item in app.expander if "待确认或未排入" in item.label)
    pending.proto.expanded = True
    app.run()

    assert not app.exception
    pending = next(item for item in app.expander if "待确认或未排入" in item.label)
    card_titles = [
        str(element.value)
        for children in pending.children.values()
        for element in children
        if getattr(element, "type", None) == "markdown"
    ]
    assert card_titles.count("**待确认：要安排沟渠维护吗？**") == 1


def test_task_series_form_always_shows_the_duration_input(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.setenv("NONGSHI_DEFAULT_MODE", "demo")
    service = WorkbenchServiceV3(
        WorkbenchStoreV3(database.with_name("plans.demo.sqlite3")),
        catalog=load_rule_catalog(),
    )
    workbench._seed_demo(service, scenario="sparse")
    app = AppTest.from_file(APP, default_timeout=30).run()

    assert any(item.label == "预计时长（分钟）" for item in app.number_input)


def test_saved_task_edit_is_not_reported_as_unsaved_when_preview_fails() -> None:
    app = AppTest.from_string(
        """
import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo
from nongshi.schema_v3 import FieldDraftV3, FieldStateV3
from nongshi.workbench import _report_task_action_failure

previous = FieldStateV3(
    id="field-1", mode="real", draft_version=1, fact_version=0, task_version=0,
    draft=FieldDraftV3(field_id="field-1"),
    updated_at=datetime(2026, 10, 26, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
)
current = previous.model_copy(update={"task_version": 1})

class FakeService:
    def get_field(self, field_id):
        return current

if not st.session_state.get("failure_report_called"):
    st.session_state["failure_report_called"] = True
    _report_task_action_failure(
        FakeService(), "field-1", previous, RuntimeError("preview unavailable"), "任务修改"
    )
"""
    ).run()

    assert app.session_state.get("notice:field-1") == (
        "任务修改已保存，但预览未生成：preview unavailable。请点「重新计算计划」后核对。"
    )
