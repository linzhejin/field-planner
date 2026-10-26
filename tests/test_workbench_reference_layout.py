"""The reference workbench opens with a usable, isolated demonstration."""

from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from nongshi import workbench
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v2 import FieldProfile
from nongshi.schema_v3 import FieldDraftV3
from nongshi.service_v3 import WorkbenchServiceV3

APP = Path(__file__).resolve().parents[1] / "src" / "nongshi" / "app.py"


def test_new_visitor_sees_persisted_demo_plan_without_touching_real_fields(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.delenv("NONGSHI_DEFAULT_MODE", raising=False)

    app = AppTest.from_file(APP, default_timeout=30).run()
    demo_database = tmp_path / "plans.demo.sqlite3"
    demo = WorkbenchServiceV3(WorkbenchStoreV3(demo_database))
    real = WorkbenchServiceV3(WorkbenchStoreV3(database))

    assert not app.exception
    assert app.session_state["workspace_mode"] == "独立模拟示例"
    fields = demo.list_fields(mode="demo")
    assert len(fields) == 1
    assert len(fields[0].draft.days) == 7
    assert demo.get_current_plan(fields[0].id) is not None
    assert demo.latest_weather(fields[0].id).source == "模拟天气"
    assert not real.list_fields(mode="real")

    app.run()
    assert len(demo.list_fields(mode="demo")) == 1
    assert len(demo.plan_history(fields[0].id)) == 1


def test_start_real_use_opens_blank_form_and_preserves_demo(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.delenv("NONGSHI_DEFAULT_MODE", raising=False)
    app = AppTest.from_file(APP, default_timeout=30).run()

    app.button(key="start_real_use").click().run()

    assert not app.exception
    assert app.session_state["workspace_mode"] == "真实地块"
    assert app.button(key=f"save_field:{app.session_state['new_field_id:real']}:0")
    assert not WorkbenchServiceV3(WorkbenchStoreV3(database)).list_fields(mode="real")
    assert (
        len(
            WorkbenchServiceV3(WorkbenchStoreV3(tmp_path / "plans.demo.sqlite3")).list_fields(
                mode="demo"
            )
        )
        == 1
    )


def test_demo_assistant_keeps_recent_activity_and_explicit_send_visible(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    monkeypatch.delenv("NONGSHI_DEFAULT_MODE", raising=False)

    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["field_selection:demo"]

    assert not app.exception
    assert app.button(key=f"assistant_send:{field_id}").label == "发送"
    assert app.text_area(key=f"assistant_draft:{field_id}")
    assert "最近操作" in "\n".join(item.value for item in app.markdown)


def test_returning_real_user_opens_real_workspace(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.delenv("NONGSHI_DEFAULT_MODE", raising=False)
    service = WorkbenchServiceV3(WorkbenchStoreV3(database))
    service.save_field_draft(
        FieldDraftV3(field_id="saved-real", profile=FieldProfile(id="saved-real", name="我的田")),
        expected_version=None,
        operation_id="saved-real",
    )

    app = AppTest.from_file(APP, default_timeout=30).run()

    assert not app.exception
    assert app.session_state["workspace_mode"] == "真实地块"
    assert "我的田" in "\n".join(item.value for item in app.markdown)


def test_old_incomplete_demo_is_preserved_when_complete_example_is_added(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    demo_database = tmp_path / "plans.demo.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.delenv("NONGSHI_DEFAULT_MODE", raising=False)
    demo = WorkbenchServiceV3(WorkbenchStoreV3(demo_database))
    demo.save_field_draft(
        FieldDraftV3(
            field_id="old-demo",
            mode="demo",
            profile=FieldProfile(id="old-demo", name="旧模拟田"),
            input_source="demo",
        ),
        expected_version=None,
        operation_id="old-demo",
    )

    app = AppTest.from_file(APP, default_timeout=30).run()

    assert not app.exception
    assert len(demo.list_fields(mode="demo")) == 2
    assert demo.get_field("old-demo").draft.profile.name == "旧模拟田"
    assert any(demo.get_current_plan(field.id) for field in demo.list_fields(mode="demo"))


def test_cross_tab_success_notice_is_announced_as_a_toast(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    monkeypatch.delenv("NONGSHI_DEFAULT_MODE", raising=False)

    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["field_selection:demo"]
    message = "计划已采用并保存新版本。"
    app.session_state[f"notice:{field_id}"] = message

    app.run()

    assert not app.exception
    assert message in [item.value for item in app.toast]


def test_confirmed_preview_cancellation_announces_result(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.delenv("NONGSHI_DEFAULT_MODE", raising=False)
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["field_selection:demo"]
    service = WorkbenchServiceV3(WorkbenchStoreV3(tmp_path / "plans.demo.sqlite3"))
    preview = service.preview_plan(field_id)

    app.run()
    app.button(key=f"cancel:{preview.id}").click().run()
    assert app.button(key=f"cancel_yes:{preview.id}")
    app.button(key=f"cancel_yes:{preview.id}").click().run()

    assert not app.exception
    assert service.latest_actionable_preview(field_id) is None
    assert "计划预览已取消，已采用的计划保持不变。" in [item.value for item in app.toast]


def test_selected_compact_tab_uses_accessible_workbench_green() -> None:
    assert '[role="tab"][aria-selected="true"]' in workbench.PALETTE
    assert ".react-aria-SelectionIndicator" in workbench.PALETTE


def test_narrow_desktop_header_metadata_uses_compact_readable_type() -> None:
    assert ".header-date { font-size:14px;" in workbench.PALETTE


def test_task_card_captions_do_not_collapse_card_height() -> None:
    assert '[class*="st-key-task_"] [data-testid="stCaptionContainer"]' in (workbench.PALETTE)


def test_idle_assistant_input_is_not_a_periodic_fragment() -> None:
    assert not hasattr(workbench._render_chat_input, "__wrapped__")
    assert hasattr(workbench._render_agent_job_progress, "__wrapped__")


def test_active_agent_job_keeps_progress_and_cancel_controls_visible(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.setenv("NONGSHI_DEFAULT_MODE", "real")
    service = WorkbenchServiceV3(WorkbenchStoreV3(database))
    service.save_field_draft(
        FieldDraftV3(
            field_id="progress-field", profile=FieldProfile(id="progress-field", name="进度田")
        ),
        expected_version=None,
        operation_id="progress-field-seed",
    )
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["field_selection:real"]
    snapshot = SimpleNamespace(done=False, cancelled=False, response=None, error="", events=())
    app.session_state["active_agent_job"] = SimpleNamespace(
        field_id=field_id,
        snapshot=lambda: snapshot,
    )
    app.session_state["active_agent_database"] = str(database)

    app.run()

    assert not app.exception
    assert any(button.key == f"cancel_agent:{field_id}" for button in app.button)
    assert not any(item.key == f"assistant_draft:{field_id}" for item in app.text_area)


def test_compact_assistant_empty_message_uses_natural_height() -> None:
    assert ".st-key-compact_workbench .st-key-assistant_rail .workbench-empty" in (
        workbench.PALETTE
    )
    assert '[data-testid="stMarkdownContainer"]:has(.workbench-empty)' in workbench.PALETTE
