"""Acceptance checks for the blank-first Streamlit workbench and its saved state."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from nongshi.repository_v3 import WorkbenchStoreV3

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


def test_first_open_is_blank_and_saving_keeps_unknowns_unknown(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()

    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["地块与资源", "计划结果", "助手与历史"]
    assert WorkbenchStoreV3(database).list_fields(mode="real") == []
    field_id = app.session_state["new_field_id:real"]
    assert app.text_input(key=f"field_name:{field_id}:0").value == ""

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
