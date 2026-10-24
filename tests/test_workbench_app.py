"""Acceptance checks for the blank-first Streamlit workbench and its saved state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from streamlit.testing.v1 import AppTest

import nongshi.workbench as workbench
from nongshi.agent import AgentServiceV3
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v3 import AgentProposalV3, AgentResponseV3, AgentRunV3

APP = Path(__file__).resolve().parents[1] / "src" / "nongshi" / "app.py"


def _create_field(app: AppTest, field_id: str) -> None:
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.text_input(key=f"field_province:{field_id}:0").set_value("湖北")
    app.checkbox(key=f"province_confirmed:{field_id}:0").set_value(True)
    app.selectbox(key=f"field_crop:{field_id}:0").set_value("油菜")
    app.checkbox(key=f"crop_confirmed:{field_id}:0").set_value(True)
    app.selectbox(key=f"field_stage:{field_id}:0").set_value("苗期")
    app.checkbox(key=f"stage_confirmed:{field_id}:0").set_value(True)
    app.selectbox(key=f"field_type:{field_id}:0").set_value("低洼/稻茬田")
    app.selectbox(key=f"inspection_requested:{field_id}:0").set_value("是")
    app.number_input(key=f"inspection_minutes:{field_id}:0").set_value(30)


def test_chat_receipt_uses_the_workbench_clock(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    received = datetime(2026, 10, 24, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    monkeypatch.setattr(workbench, "_now", lambda: received)
    captured: list[datetime | None] = []

    def fake_run(self, field_id, user_message, *, conversation=None, received_at=None):  # type: ignore[no-untyped-def]
        captured.append(received_at)
        return AgentResponseV3(
            run_id="clock-test", status="success", model="test", request_count=0, text="已读取"
        )

    monkeypatch.setattr(AgentServiceV3, "run", fake_run)
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.button(key=f"save_field:{field_id}:0").click().run()
    app.chat_input(key=f"assistant_input:{field_id}").set_value("巡田已完成").run()

    assert not app.exception
    assert captured == [received]


def test_first_open_is_blank_and_saving_keeps_unknowns_unknown(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()

    assert not app.exception
    # 尚未建档时只展示建档入口；计划与助手在保存后才进入工作台。
    assert not app.tabs
    assert WorkbenchStoreV3(database).list_fields(mode="real") == []
    field_id = app.session_state["new_field_id:real"]
    assert app.text_input(key=f"field_name:{field_id}:0").value == ""
    # 空白优先：首次进入直接显示内联空表单，无需点开「编辑当前地块」对话框。
    assert app.button(key=f"save_field:{field_id}:0").label == "保存地块档案"
    assert not any(button.key == f"edit_field:{field_id}" for button in app.button)

    app.text_input(key=f"field_name:{field_id}:0").set_value("河边一号")
    app.button(key=f"save_field:{field_id}:0").click().run()

    assert not app.exception
    saved = WorkbenchStoreV3(database).get_field(field_id)
    assert saved.draft.profile is not None
    assert saved.draft.profile.name == "河边一号"
    assert saved.draft.profile.crop is None
    assert saved.draft.profile.stage is None
    assert saved.draft.start_date is not None
    assert saved.draft.profile.observation_source == "synthetic"
    assert saved.draft.profile.observed_at is None


def test_new_blank_field_requires_explicit_discard_to_return(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    first_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{first_id}:0").set_value("一号田")
    app.button(key=f"save_field:{first_id}:0").click().run()

    app.button(key="add_field:real").click().run()
    assert not app.exception
    assert not any(box.key == "field_selection:real" for box in app.selectbox)
    new_id = app.session_state["new_field_id:real"]
    assert new_id != first_id
    assert app.button(key="discard_new_field:real").label == "放弃未保存内容并返回"
    app.button(key="discard_new_field:real").click().run()
    assert not app.exception
    assert app.session_state["field_selection:real"] == first_id
    assert "一号田" in "\n".join(element.value for element in app.markdown)
    assert len(WorkbenchStoreV3(database).list_fields(mode="real")) == 1


def test_results_surface_pending_agent_change_until_it_is_dismissed(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.button(key=f"save_field:{field_id}:0").click().run()
    store = WorkbenchStoreV3(database)
    store.save_agent_run(
        AgentRunV3(
            field_id=field_id,
            model="test",
            started_at=datetime.now(UTC),
            elapsed_ms=0,
            request_count=1,
            tool_count=0,
            status="success",
        ),
        [AgentProposalV3(kind="field_update", field_id=field_id, summary="调整地块信息")],
    )
    app.run()
    assert any("助手有 1 条待确认修改" in info.value for info in app.info)

    proposal_id = store.agent_proposals(field_id)[0].proposal.id
    store.update_agent_proposal(
        store.agent_proposal(proposal_id).model_copy(update={"status": "ignored"})
    )
    app.run()
    assert not any("助手有 1 条待确认修改" in info.value for info in app.info)


def test_narrow_assistant_quick_observation_saves_fact(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.button(key=f"save_field:{field_id}:0").click().run()

    app.button(key=f"quick_observe:{field_id}").click().run()
    assert not app.exception
    assert app.session_state[f"quick_feedback_kind:{field_id}"] == "observation"
    app.selectbox(key=f"quick_observation_field:{field_id}").set_value("积水情况").run()
    app.selectbox(key=f"quick_observation_value:{field_id}:积水情况").set_value("是")
    app.button(key=f"quick_save_observation:{field_id}:0").click().run()

    assert not app.exception
    assert WorkbenchStoreV3(database).get_field(field_id).fact_version == 1


def test_saving_confirmed_field_observations_records_their_source_and_time(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.selectbox(key=f"field_moisture:{field_id}:0").set_value("偏湿")
    app.button(key=f"save_field:{field_id}:0").click().run()

    saved = WorkbenchStoreV3(database).get_field(field_id)
    assert not app.exception
    assert saved.draft.profile is not None
    assert saved.draft.profile.observation_source == "observed"
    assert saved.draft.profile.observed_at is not None


def test_observation_type_selector_is_reactive_outside_the_submit_form(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    _create_field(app, field_id)
    app.button(key=f"save_field:{field_id}:0").click().run()

    observation_type = app.selectbox(key=f"observation_field:{field_id}:0")
    assert observation_type.proto.form_id == ""
    observation_type.set_value("是否有实际降雨").run()
    assert app.selectbox(key=f"observation_value:{field_id}:0").options == ["未知", "是", "否"]


def test_blank_to_adopted_plan_then_fact_feedback_survives_reload(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    _create_field(app, field_id)
    app.button(key=f"save_field:{field_id}:0").click().run()
    assert not app.exception

    # 逐日条件与可用时段已移入「调整可用时段」对话框：先点按钮打开表单。
    app.button(key=f"edit_schedule:{field_id}").click().run()
    assert not app.exception
    app.checkbox(key=f"resources_enabled:{field_id}:1").set_value(True).run()
    for offset in range(7):
        app.selectbox(key=f"site:{field_id}:1:{offset}").set_value("可作业")
        app.selectbox(key=f"weather:{field_id}:1:{offset}").set_value("可作业")
        app.number_input(key=f"minutes:{field_id}:1:{offset}").set_value(120)
        app.text_input(key=f"intervals:{field_id}:1:{offset}").set_value("09:00-11:00")
    app.button(key=f"save_schedule:{field_id}:1").click().run()
    assert not app.exception

    app.button(key=f"preview_plan:{field_id}").click().run()
    preview_id = app.session_state[f"current_preview:{field_id}"]
    assert preview_id
    app.button(key=f"adopt:{preview_id}").click().run()
    assert not app.exception

    store = WorkbenchStoreV3(database)
    current = store.get_current_plan(field_id)
    assert current is not None
    inspection = next(item for item in current.items if item.task_type == "inspection")
    assert inspection.start_at is not None and inspection.end_at is not None
    assert len(store.plan_history(field_id)) == 1
    # 顶栏计划版本随采用版更新为 V1。
    top_markdown = "\n".join(element.value for element in app.markdown)
    assert "计划 V1" in top_markdown

    app.selectbox(key=f"observation_field:{field_id}:0").set_value("沟渠连通")
    app.selectbox(key=f"observation_value:{field_id}:0").set_value("受阻")
    app.button(key=f"save_observation:{field_id}:0").click().run()
    assert not app.exception
    updated = store.get_field(field_id)
    assert updated.fact_version == 1
    assert updated.plan_stale is True
    assert store.get_current_plan(field_id).id == current.id

    reopened = AppTest.from_file(APP, default_timeout=30).run()
    assert not reopened.exception
    restored = WorkbenchStoreV3(database).get_field(field_id)
    assert restored.fact_version == 1
    assert restored.plan_stale is True
    assert len(WorkbenchStoreV3(database).list_events(field_id)) == 1


def test_simulation_uses_a_separate_database_from_real_fields(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    app.radio(key="workspace_mode").set_value("独立模拟示例").run()
    app.button(key="seed_demo").click().run()

    demo_database = database.with_name("plans.demo.sqlite3")
    assert not app.exception
    assert WorkbenchStoreV3(database).list_fields(mode="real") == []
    demo_fields = WorkbenchStoreV3(demo_database).list_fields(mode="demo")
    assert len(demo_fields) == 1
    assert demo_fields[0].draft.profile.name == "演示地块"


def test_field_type_facts_save_independently_and_survive_reopen(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    _create_field(app, field_id)
    app.selectbox(key=f"field_rice_stubble:{field_id}:0").set_value("是")
    app.selectbox(key=f"field_cold_waterlogged:{field_id}:0").set_value("否")
    app.selectbox(key=f"field_terrain:{field_id}:0").set_value("低洼")
    app.button(key=f"save_field:{field_id}:0").click().run()

    assert not app.exception
    profile = WorkbenchStoreV3(database).get_field(field_id).draft.profile
    assert profile is not None
    assert profile.field_type == "lowland_rice_stubble"
    assert profile.rice_stubble is True
    assert profile.cold_waterlogged is False
    assert profile.terrain == "lowland"

    reopened = AppTest.from_file(APP, default_timeout=30).run()
    assert not reopened.exception
    restored = WorkbenchStoreV3(database).get_field(field_id).draft.profile
    assert restored is not None
    assert restored.rice_stubble is True
    assert restored.cold_waterlogged is False
    assert restored.terrain == "lowland"


def test_standing_water_warning_is_shown_from_saved_facts(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    _create_field(app, field_id)
    app.button(key=f"save_field:{field_id}:0").click().run()
    assert not app.exception
    assert not any("农技人员" in warning.value for warning in app.warning)

    app.selectbox(key=f"observation_field:{field_id}:0").set_value("积水情况").run()
    app.selectbox(key=f"observation_value:{field_id}:0").set_value("是")
    app.button(key=f"save_observation:{field_id}:0").click().run()

    assert not app.exception
    assert any("农技人员" in warning.value for warning in app.warning)


def test_a04_supplementary_registry_is_visible_with_prefix(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    _create_field(app, field_id)
    app.button(key=f"save_field:{field_id}:0").click().run()

    assert not app.exception
    # 规则依据与 A04 补充来源已移入「查看详情」对话框，卡片主区不再铺开。
    detail_buttons = [element for element in app.button if element.label == "查看详情"]
    assert detail_buttons
    detail_buttons[0].click().run()
    assert not app.exception
    texts = [element.value for element in app.markdown]
    texts += [element.value for element in app.caption]
    assert any("C11/Z15" in text for text in texts)
    assert any("A04-S01" in text for text in texts)
    assert any("不改写规则卡原文" in text or "补充来源" in text for text in texts)


def test_top_bar_shows_range_version_mode_and_history_export(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert not any(button.key == "top_history" for button in app.button)
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.button(key=f"save_field:{field_id}:0").click().run()
    assert not app.exception

    markdown = "\n".join(element.value for element in app.markdown)
    captions = "\n".join(element.value for element in app.caption)

    # 保存后顶栏才出现：日期范围 + 计划版本 + 模式徽标 + 保存状态。
    start = WorkbenchStoreV3(database).get_field(field_id).draft.start_date
    assert start is not None
    assert f"{start.month}月{start.day}日" in markdown
    assert "计划 未采用" in markdown
    assert 'status-chip status-neutral">真实地块' in markdown
    assert "已保存" in captions

    # 顶栏提供历史版本与导出计划两个按钮。
    button_keys = {button.key for button in app.button}
    assert "top_history" in button_keys
    assert "top_export" in button_keys

    # 尚无采用版时点「历史版本」可在对话框内看到空态提示。
    app.button(key="top_history").click().run()
    assert not app.exception
    dialog_text = "\n".join(element.value for element in app.caption)
    assert "尚无已采用的计划版本" in dialog_text


def test_result_count_strip_shows_status_counts(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    _create_field(app, field_id)
    app.button(key=f"save_field:{field_id}:0").click().run()
    assert not app.exception

    markdown = "\n".join(element.value for element in app.markdown)
    # 三块计数卡沿用状态胶囊配色：已安排绿、待确认琥珀、未排入灰。
    assert 'status-chip status-green">已安排' in markdown
    assert 'status-chip status-amber">待确认' in markdown
    assert 'status-chip status-neutral">未排入' in markdown
    # 计数条上方有天气摘要行。
    assert any(caption.value.startswith("天气：") for caption in app.caption)


def test_quick_action_buttons_present(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.button(key=f"save_field:{field_id}:0").click().run()

    button_keys = {button.key for button in app.button}
    assert f"quick_observe:{field_id}" in button_keys
    assert f"quick_complete:{field_id}" in button_keys
    assert f"quick_resource:{field_id}" in button_keys


def test_edit_field_button_opens_archive_dialog(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    _create_field(app, field_id)
    app.button(key=f"save_field:{field_id}:0").click().run()
    assert not app.exception

    # 已有档案后表单不再常驻，需点「编辑当前地块」在对话框中打开。
    assert not any(button.key == f"save_field:{field_id}:1" for button in app.button)
    app.button(key=f"edit_field:{field_id}").click().run()
    assert not app.exception
    assert any(button.key == f"save_field:{field_id}:1" for button in app.button)
