"""The saved-field first screen should make its next action and plan state clear."""

from datetime import UTC, datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v3 import AgentProposalV3, AgentRunV3
from nongshi.service_v3 import WorkbenchServiceV3

APP = Path(__file__).resolve().parents[1] / "src" / "nongshi" / "app.py"


def test_first_field_has_one_clear_save_action_without_empty_navigation(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=30).run()

    assert not app.exception
    buttons = {button.key: button for button in app.button}
    field_id = app.session_state["new_field_id:real"]
    assert f"save_field:{field_id}:0" in buttons
    assert buttons[f"save_field:{field_id}:0"].proto.type == "primary"
    assert "add_field:real" not in buttons
    assert "top_history" not in buttons
    assert "top_export" not in buttons


def test_first_field_requires_a_name_without_exposing_validation_internals(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]

    app.button(key=f"save_field:{field_id}:0").click().run()

    errors = "\n".join(error.value for error in app.error)
    assert "请填写地块名称" in errors
    assert "validation error" not in errors


def test_new_field_return_explains_that_unsaved_input_is_discarded(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, service, original_id = _saved_field(tmp_path, monkeypatch)

    app.button(key="add_field:real").click().run()

    assert not app.exception
    assert app.button(key="cancel_new_field:real").label == "放弃未保存内容并返回"
    app.button(key="cancel_new_field:real").click().run()
    assert service.get_field(original_id).draft.profile is not None
    assert len(service.list_fields(mode="real")) == 1


def _saved_field(tmp_path: Path, monkeypatch) -> tuple[AppTest, WorkbenchServiceV3, str]:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("验收一号田")
    app.button(key=f"save_field:{field_id}:0").click().run()
    return app, WorkbenchServiceV3(WorkbenchStoreV3(database)), field_id


def _add_pending_assistant_change(service: WorkbenchServiceV3, field_id: str) -> str:
    state = service.get_field(field_id)
    run = AgentRunV3(
        field_id=field_id,
        model="test",
        started_at=datetime.now(UTC),
        elapsed_ms=0,
        request_count=1,
        tool_count=0,
        status="success",
    )
    proposal = AgentProposalV3(
        kind="field_update",
        field_id=field_id,
        expected_draft_version=state.draft_version,
        draft=state.draft,
        summary="请核对地块信息",
    )
    service.store.save_agent_run(run, [proposal])
    return proposal.id


def test_pending_assistant_change_is_the_only_primary_action(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, service, field_id = _saved_field(tmp_path, monkeypatch)
    proposal_id = _add_pending_assistant_change(service, field_id)

    app.run()

    primary_keys = {
        button.key
        for button in app.button
        if button.proto.type == "primary" and not button.key.startswith("assistant_send:")
    }
    assert primary_keys == {f"confirm_proposal:{proposal_id}"}


def test_pending_assistant_change_does_not_compete_with_adopt_action(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, service, field_id = _saved_field(tmp_path, monkeypatch)
    preview = service.preview_plan(field_id)
    proposal_id = _add_pending_assistant_change(service, field_id)

    app.run()

    primary_keys = {
        button.key
        for button in app.button
        if button.proto.type == "primary" and not button.key.startswith("assistant_send:")
    }
    assert f"adopt:{preview.id}" in {button.key for button in app.button}
    assert primary_keys == {f"confirm_proposal:{proposal_id}"}


def test_multiple_pending_assistant_changes_still_have_one_primary_action(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app, service, field_id = _saved_field(tmp_path, monkeypatch)
    older_id = _add_pending_assistant_change(service, field_id)
    newer_id = _add_pending_assistant_change(service, field_id)

    app.run()

    buttons = {button.key: button for button in app.button}
    assert f"confirm_proposal:{older_id}" in buttons
    assert f"confirm_proposal:{newer_id}" in buttons
    primary_keys = {
        button.key
        for button in app.button
        if button.proto.type == "primary" and not button.key.startswith("assistant_send:")
    }
    assert primary_keys == {f"confirm_proposal:{newer_id}"}


def test_adopted_plan_starting_tomorrow_explicitly_explains_today(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, service, field_id = _saved_field(tmp_path, monkeypatch)
    preview = service.preview_plan(field_id)
    service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-for-today-label",
    )

    app.run()

    captions = "\n".join(item.value for item in app.caption)
    assert "今天暂无安排；七天计划从" in captions
    assert "今日与后续安排" in "\n".join(item.value for item in app.markdown)


def test_saved_field_first_screen_explains_missing_plan_and_next_step(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    app, _, field_id = _saved_field(tmp_path, monkeypatch)

    assert not app.exception
    text = "\n".join(item.value for item in [*app.markdown, *app.caption, *app.info])
    keys = {button.key for button in app.button}
    assert "当前没有已采用计划" in text
    assert "今日与后续安排" in text
    assert "还没有已采用的日程" in text
    assert f"next_question:{field_id}" in keys
    assert f"preview_plan:{field_id}" in keys
    assert f"quick_complete:{field_id}" not in keys
    assert "top_export" not in keys
    assert "采用后可导出" in text


def test_pending_preview_keeps_one_adopt_action_and_export_hidden(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, service, field_id = _saved_field(tmp_path, monkeypatch)
    preview = service.preview_plan(field_id)
    app.run()

    assert not app.exception
    keys = {button.key for button in app.button}
    text = "\n".join(item.value for item in [*app.markdown, *app.caption, *app.info])
    assert "待采用" in text
    assert f"adopt:{preview.id}" in keys
    assert f"next_question:{field_id}" not in keys
    assert "top_export" not in keys


def test_creating_preview_refreshes_first_screen_status(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, _, field_id = _saved_field(tmp_path, monkeypatch)

    app.button(key=f"preview_plan:{field_id}").click().run()

    assert not app.exception
    text = "\n".join(item.value for item in [*app.markdown, *app.caption])
    assert "有一版计划预览等待核对" in text
    assert not any(button.key == f"next_question:{field_id}" for button in app.button)
