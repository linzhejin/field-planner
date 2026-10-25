"""Regression checks for the product-audit navigation and preview failures."""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from nongshi import workbench
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v2 import DayWindow, FieldProfile, TimeWindow
from nongshi.schema_v3 import AgentResponseV3, FieldDraftV3
from nongshi.service_v3 import WorkbenchServiceV3, align_draft_dates

APP = Path(__file__).resolve().parents[1] / "src/nongshi/app.py"


def ready_app(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    database = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.setenv("NONGSHI_LLM_API_KEY", "")
    service = WorkbenchServiceV3(WorkbenchStoreV3(database))
    start = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    service.save_field_draft(
        FieldDraftV3(
            field_id="audit-field",
            profile=FieldProfile(
                id="audit-field",
                name="审查田",
                province="湖北",
                province_confirmed=True,
                crop="油菜",
                crop_confirmed=True,
                stage="苗期",
                stage_confirmed=True,
                field_type="lowland_rice_stubble",
                inspection_requested=True,
                inspection_duration_minutes=30,
            ),
            start_date=start,
            days=[
                DayWindow(
                    day=start + timedelta(days=i),
                    site_workable=True,
                    weather_workable=True,
                    resource_minutes=120,
                    intervals=[TimeWindow(start="09:00", end="11:00")],
                )
                for i in range(7)
            ],
        ),
        expected_version=None,
        operation_id="setup",
    )
    return AppTest.from_file(APP, default_timeout=30).run(), service


def test_compact_layout_groups_existing_workbench_into_three_tabs(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, _ = ready_app(tmp_path, monkeypatch)
    labels = [tab.label for tab in app.tabs]
    assert labels[:3] == ["地块与资源", "计划结果", "助手与历史"]


def test_assistant_preview_rerender_has_only_one_adopt_control(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    preview = service.preview_plan("audit-field")
    app.session_state["agent_result:audit-field"] = AgentResponseV3(
        run_id="fixture",
        model="stub",
        request_count=1,
        status="success",
        business_outcome="preview_ready",
        previews=[preview],
    )
    app.run().run()
    assert not app.exception
    assert len([b for b in app.button if b.label == "采用这版安排"]) == 1


def test_new_assistant_preview_replaces_cached_manual_preview(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="preview_plan:audit-field").click().run()
    old_id = app.session_state["current_preview:audit-field"]
    preview = service.preview_plan("audit-field")
    app.session_state["agent_result:audit-field"] = AgentResponseV3(
        run_id="fixture",
        model="stub",
        request_count=1,
        status="success",
        business_outcome="preview_ready",
        previews=[preview],
    )
    app.run().run()
    assert not app.exception
    assert app.session_state["current_preview:audit-field"] == preview.id
    assert not any(b.key == f"adopt:{old_id}" for b in app.button)


def test_dialog_navigation_opens_only_requested_dialog(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, _ = ready_app(tmp_path, monkeypatch)
    app.button(key="edit_field:audit-field").click().run()
    app.button(key="top_history").click().run()
    assert not app.exception
    assert not any(b.label == "保存地块档案" for b in app.button)


def test_new_second_field_has_return_to_saved_field(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="add_field:real").click().run()
    assert not app.exception
    app.button(key="cancel_new_field:real").click().run()
    assert app.button(key="edit_field:audit-field")
    assert len(service.list_fields(mode="real")) == 1


def test_manual_preview_survives_new_session(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, _ = ready_app(tmp_path, monkeypatch)
    app.button(key="preview_plan:audit-field").click().run()
    reopened = AppTest.from_file(APP, default_timeout=30).run()
    assert not reopened.exception
    assert len([b for b in reopened.button if b.label == "采用这版安排"]) == 1


def test_new_start_date_does_not_shift_existing_confirmations(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    state = service.get_field("audit-field")
    app.button(key="edit_field:audit-field").click().run()
    app.date_input(key="start_date:audit-field:1").set_value(
        state.draft.start_date + timedelta(days=8)
    )
    app.button(key="save_field:audit-field:1").click().run()
    assert not app.exception
    saved = service.get_field("audit-field")
    assert all(
        day.site_workable is None and day.weather_workable is None for day in saved.draft.days
    )
    assert all(day.resource_minutes is None and not day.intervals for day in saved.draft.days)


def test_edit_buffer_survives_leaving_and_marks_unsaved(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="edit_field:audit-field").click().run()
    app.text_input(key="field_name:audit-field:1").set_value("尚未保存的名字").run()
    app.button(key="top_history").click().run()
    assert not app.exception
    assert "未保存" in "\n".join(c.value for c in [*app.markdown, *app.caption])
    app.button(key="edit_field:audit-field").click().run()
    assert app.text_input(key="field_name:audit-field:1").value == "尚未保存的名字"
    assert service.get_field("audit-field").draft.profile.name == "审查田"
    app.button(key="save_field:audit-field:1").click().run()
    assert service.get_field("audit-field").draft.profile.name == "尚未保存的名字"


def test_plan_lists_seven_dates_and_readable_missing_information(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="preview_plan:audit-field").click().run()
    preview_id = app.session_state["current_preview:audit-field"]
    app.button(key=f"adopt:{preview_id}").click().run()
    app.button(key="top_history").click().run()
    text = "\n".join(x.value for x in [*app.markdown, *app.caption, *app.info])
    start = service.get_field("audit-field").draft.start_date
    for offset in range(7):
        assert f"{start + timedelta(days=offset):%m月%d日}" in text
    assert "暂无安排" in text
    assert "尚未统筹其他地块时间" in text
    for internal in ("maintenance_problem_confirmed", "maintenance_need_confirmed", "added"):
        assert internal not in text


def test_batch_schedule_changes_only_selected_days_and_keeps_basis(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="edit_schedule:audit-field").click().run()
    app.selectbox(key="batch_site:audit-field").set_value("不可作业")
    app.text_input(key="batch_basis:audit-field").set_value("当日现场观察")
    app.button(key="apply_batch:audit-field").click().run()
    app.button(key="save_schedule:audit-field:1").click().run()
    assert not app.exception
    days = service.get_field("audit-field").draft.days
    assert days[0].site_workable is False
    assert days[1].site_workable is True
    assert "当日现场观察" in days[0].confirmation_basis


def test_batch_time_preserves_unknown_resources_outside_range(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    state = service.get_field("audit-field")
    blank_days = [
        day.model_copy(update={"resource_minutes": None, "intervals": []})
        for day in state.draft.days
    ]
    service.save_field_draft(
        state.draft.model_copy(update={"days": blank_days}),
        expected_version=state.draft_version,
        operation_id="clear-resources-for-test",
    )
    app.run()
    app.button(key="edit_schedule:audit-field").click().run()
    app.checkbox(key="batch_time:audit-field").set_value(True).run()
    app.text_input(key="batch_intervals:audit-field").set_value("09:00-11:00")
    app.button(key="apply_batch:audit-field").click().run()
    app.button(key="save_schedule:audit-field:2").click().run()
    assert not app.exception
    days = service.get_field("audit-field").draft.days
    assert days[0].resource_minutes == 120
    assert [day.resource_minutes for day in days[1:]] == [None] * 6


def test_batch_time_preserves_partial_intervals_outside_range(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    state = service.get_field("audit-field")
    partial_days = [day.model_copy(update={"resource_minutes": None}) for day in state.draft.days]
    service.save_field_draft(
        state.draft.model_copy(update={"days": partial_days}),
        expected_version=state.draft_version,
        operation_id="partial-resources-for-test",
    )
    app.run()
    app.button(key="edit_schedule:audit-field").click().run()
    app.checkbox(key="batch_time:audit-field").set_value(True).run()
    app.text_input(key="batch_intervals:audit-field").set_value("09:00-10:00")
    app.button(key="apply_batch:audit-field").click().run()
    app.button(key="save_schedule:audit-field:2").click().run()
    assert not app.exception
    days = service.get_field("audit-field").draft.days
    assert days[0].resource_minutes == 60
    assert [day.resource_minutes for day in days[1:]] == [None] * 6
    assert [day.intervals for day in days[1:]] == [day.intervals for day in partial_days[1:]]


def test_questions_offer_skip_without_changing_facts(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    skip_buttons = [b for b in app.button if b.label == "暂时跳过"]
    assert 1 <= len(skip_buttons) <= 3
    key = skip_buttons[0].key
    skip_buttons[0].click().run()
    assert not any(b.key == key for b in app.button)
    assert service.get_field("audit-field").fact_version == 0


def test_observation_question_opens_matching_fact_editor(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="next_question:audit-field").click().run()
    assert not app.exception
    assert app.selectbox(key="quick_observation_field:audit-field").value == "维护问题"
    app.selectbox(key="quick_observation_value:audit-field:维护问题").set_value("有")
    app.button(key="quick_save_observation:audit-field:0").click().run()
    assert not app.exception
    assert service.get_field("audit-field").fact_version == 1


def test_task_detail_edits_preference_then_requires_preview_adoption(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="preview_plan:audit-field").click().run()
    preview_id = app.session_state["current_preview:audit-field"]
    app.button(key=f"adopt:{preview_id}").click().run()
    old = service.get_current_plan("audit-field")
    item = next(item for item in old.items if item.task_type == "inspection")
    app.button(key=f"detail:adopted-{old.id}:{item.task_id}").click().run()
    desired = old.plan.request.start_date + timedelta(days=1)
    app.date_input(key=f"task_date:{item.task_id}:1").set_value(desired)
    app.number_input(key=f"task_duration:{item.task_id}:1").set_value(45)
    app.button(key=f"task_edit:{item.task_id}").click().run()
    assert not app.exception
    assert service.get_current_plan("audit-field").id == old.id
    assert service.get_field("audit-field").draft.profile.inspection_due_date == desired
    assert service.latest_actionable_preview("audit-field") is not None
    app.button(key=f"detail:adopted-{old.id}:{item.task_id}").click().run()
    assert app.date_input(key=f"task_date:{item.task_id}:2").value == desired
    assert app.number_input(key=f"task_duration:{item.task_id}:2").value == 45
    app.button(key=f"task_edit:{item.task_id}").click().run()
    assert service.get_field("audit-field").draft.profile.inspection_duration_minutes == 45


def test_task_edit_preserves_existing_priority(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    state = service.get_field("audit-field")
    profile = state.draft.profile.model_copy(update={"inspection_priority": 1})
    service.save_field_draft(
        state.draft.model_copy(update={"profile": profile}),
        expected_version=1,
        operation_id="priority-setup",
    )
    app.run()
    app.button(key="preview_plan:audit-field").click().run()
    app.button(key=f"adopt:{app.session_state['current_preview:audit-field']}").click().run()
    current = service.get_current_plan("audit-field")
    task = next(item for item in current.items if item.task_type == "inspection")
    app.button(key=f"detail:adopted-{current.id}:{task.task_id}").click().run()
    assert app.selectbox(key=f"task_priority:{task.task_id}:2").value == 1
    app.button(key=f"task_edit:{task.task_id}").click().run()
    assert not app.exception
    assert service.get_field("audit-field").draft.profile.inspection_priority == 1


def test_task_detail_clamps_old_date_after_week_changes(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="preview_plan:audit-field").click().run()
    app.button(key=f"adopt:{app.session_state['current_preview:audit-field']}").click().run()
    current = service.get_current_plan("audit-field")
    task = next(item for item in current.items if item.task_type == "inspection")
    state = service.get_field("audit-field")
    new_start = state.draft.start_date + timedelta(days=8)
    service.save_field_draft(
        align_draft_dates(state.draft, start_date=new_start),
        expected_version=1,
        operation_id="week-change",
    )
    app.run()
    app.button(key=f"detail:adopted-{current.id}:{task.task_id}").click().run()
    assert not app.exception
    assert app.date_input(key=f"task_date:{task.task_id}:2").value == new_start


def test_expired_week_still_allows_task_cancellation(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="preview_plan:audit-field").click().run()
    app.button(key=f"adopt:{app.session_state['current_preview:audit-field']}").click().run()
    current = service.get_current_plan("audit-field")
    task = next(item for item in current.items if item.task_type == "inspection")
    monkeypatch.setattr(
        workbench, "_today", lambda: current.plan.request.start_date + timedelta(days=8)
    )
    app.button(key=f"detail:adopted-{current.id}:{task.task_id}").click().run()
    assert not app.exception
    assert app.button(key=f"task_cancel:{task.task_id}")


def test_task_edit_preview_failure_reports_saved_change_and_recovery(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="preview_plan:audit-field").click().run()
    app.button(key=f"adopt:{app.session_state['current_preview:audit-field']}").click().run()
    current = service.get_current_plan("audit-field")
    task = next(item for item in current.items if item.task_type == "inspection")
    original_preview_plan = WorkbenchServiceV3.preview_plan

    def fail_task_preview(self, field_id, *, operation_id=None, source=None):  # type: ignore[no-untyped-def]
        if operation_id and operation_id.startswith("task-action:"):
            raise ValueError("模拟预览服务不可用")
        return original_preview_plan(self, field_id, operation_id=operation_id, source=source)

    monkeypatch.setattr(WorkbenchServiceV3, "preview_plan", fail_task_preview)
    app.button(key=f"detail:adopted-{current.id}:{task.task_id}").click().run()
    app.number_input(key=f"task_duration:{task.task_id}:1").set_value(45)
    app.button(key=f"task_edit:{task.task_id}").click().run()
    assert service.get_field("audit-field").draft.profile.inspection_duration_minutes == 45
    assert not app.exception
    assert "偏好已保存" in "\n".join(x.value for x in app.success)
    assert app.button(key="preview_plan:audit-field")


def test_task_detail_completion_can_be_explicitly_retracted(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="preview_plan:audit-field").click().run()
    app.button(key=f"adopt:{app.session_state['current_preview:audit-field']}").click().run()
    current = service.get_current_plan("audit-field")
    item = next(item for item in current.items if item.task_type == "inspection")
    completed_at = item.start_at + timedelta(minutes=45)
    monkeypatch.setattr(workbench, "_now", lambda: completed_at)
    context = f"workbench_context:real:{(tmp_path / 'ui.sqlite3').resolve()}"
    app.session_state[context][0]._now = lambda: completed_at
    app.button(key=f"detail:adopted-{current.id}:{item.task_id}").click().run()
    app.button(key=f"task_complete:{item.task_id}").click().run()
    assert not app.exception
    event = next(
        event
        for event in service.list_events("audit-field")
        if event.feedback and event.feedback.kind == "completed"
    )
    app.button(key=f"detail:adopted-{current.id}:{item.task_id}").click().run()
    next(box for box in app.checkbox if box.label == "我确认要更正这条完成记录").set_value(True)
    next(button for button in app.button if button.label == "撤回误报完成").click().run()
    assert not app.exception
    retraction = service.list_events("audit-field")[-1]
    assert retraction.correction_of == event.event_id and retraction.retracts
    assert service.latest_actionable_preview("audit-field") is not None


def test_history_restore_creates_reviewable_preview_without_overwriting_plan(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    app, service = ready_app(tmp_path, monkeypatch)
    app.button(key="preview_plan:audit-field").click().run()
    app.button(key=f"adopt:{app.session_state['current_preview:audit-field']}").click().run()
    first = service.get_current_plan("audit-field")
    state = service.get_field("audit-field")
    service.edit_task(
        "audit-field",
        "audit-field-I",
        preferred_date=state.draft.start_date + timedelta(days=1),
        duration_minutes=30,
        priority=2,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="history-setup",
    )
    second_preview = service.latest_actionable_preview("audit-field")
    service.adopt_plan(
        second_preview.id,
        expected_fact_version=second_preview.expected_fact_version,
        expected_plan_id=second_preview.expected_plan_id,
        operation_id="history-adopt",
    )
    app.button(key="top_history").click().run()
    app.selectbox(key="history_version:audit-field").set_value(first.id).run()
    app.button(key=f"restore_history:{first.id}").click().run()
    assert not app.exception
    assert service.get_current_plan("audit-field").id != first.id
    assert service.latest_actionable_preview("audit-field") is not None
