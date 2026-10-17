"""Exercise the rendered Streamlit workflow, not only its pure planning functions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

from nongshi.repository import PlanStoreV2

APP = Path(__file__).resolve().parents[1] / "src" / "nongshi" / "app.py"


def _set_daily_confirmations(app: AppTest) -> None:
    request = app.session_state.v2_request
    for plot in request.plots:
        for day in request.days:
            app.selectbox(key=f"v2_site_{plot.id}_{day.day.isoformat()}_0").set_value(True)
            app.selectbox(key=f"v2_weather_{plot.id}_{day.day.isoformat()}_0").set_value(True)


def test_page_saves_daily_confirmations_and_generates_minutes_schedule(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=20).run()
    assert not app.exception

    _set_daily_confirmations(app)
    app.selectbox(key="v2_maintenance_problem_A_0").set_value(True)
    app.selectbox(key="v2_connectivity_A_0").set_value("impaired")
    app.button(key="v2_save_inputs").click().run()
    app.button(key="v2_generate").click().run()

    assert not app.exception
    plan = app.session_state.v2_plan
    assert plan is not None
    assert all(
        item.start_at is not None for item in plan.items if item.task_id in {"A-I", "B-I", "C-I"}
    )
    assert all(item.duration_minutes == 30 for item in plan.items if item.task_type == "inspection")
    assert any(
        item.end_at and item.end_at.minute != item.start_at.minute
        for item in plan.items
        if item.start_at
    )
    assert PlanStoreV2(database).latest() == plan


def test_partial_daily_confirmation_stays_in_sync_with_generated_plan(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    request = app.session_state.v2_request
    first_day = request.days[0].day
    for plot in request.plots:
        app.selectbox(key=f"v2_site_{plot.id}_{first_day.isoformat()}_0").set_value(True)
        app.selectbox(key=f"v2_weather_{plot.id}_{first_day.isoformat()}_0").set_value(True)

    app.button(key="v2_save_inputs").click().run()
    app.button(key="v2_generate").click().run()

    assert not app.exception
    assert app.session_state.v2_plan.request == app.session_state.v2_request


def test_saving_untouched_demo_inputs_does_not_claim_field_observation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.button(key="v2_save_inputs").click().run()

    assert not app.exception
    assert all(plot.observed_at is None for plot in app.session_state.v2_request.plots)
    assert all(
        plot.observation_source == "synthetic" for plot in app.session_state.v2_request.plots
    )


def test_confirming_maintenance_demand_does_not_claim_new_field_observation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.selectbox(key="v2_maintenance_need_B_0").set_value(True)
    app.button(key="v2_save_inputs").click().run()

    plot = next(plot for plot in app.session_state.v2_request.plots if plot.id == "B")
    assert plot.maintenance_need_confirmed is True
    assert plot.observed_at is None
    assert plot.observation_source == "synthetic"


def test_zero_leaf_observation_is_not_converted_to_unknown_on_save(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    observed_at = datetime(2026, 10, 17, 8, tzinfo=UTC)
    plot = next(plot for plot in app.session_state.v2_request.plots if plot.id == "A")
    plot.leaf_count = 0.0
    plot.observed_at = observed_at
    plot.observation_source = "observed"
    app.run()
    app.checkbox(key="v2_leaf_known_A_0").set_value(True)
    app.button(key="v2_save_inputs").click().run()

    saved = next(plot for plot in app.session_state.v2_request.plots if plot.id == "A")
    assert saved.leaf_count == 0.0
    assert saved.observed_at == observed_at


def test_invalid_boolean_observation_is_rejected_without_saving_version(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    _set_daily_confirmations(app)
    app.button(key="v2_save_inputs").click().run()
    app.button(key="v2_generate").click().run()
    previous_id = app.session_state.v2_plan.id

    app.selectbox(key="v2_observation_field").set_value("standing_water")
    app.text_input(key="v2_observation_value").set_value("tru")
    app.button(key="v2_submit_observation").click().run()

    assert not app.exception
    assert app.session_state.v2_plan.id == previous_id
    assert any("观察值格式不正确" in error.value for error in app.error)


def test_editing_water_in_input_form_invalidates_old_site_confirmation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    _set_daily_confirmations(app)
    app.button(key="v2_save_inputs").click().run()
    app.button(key="v2_generate").click().run()
    plan = app.session_state.v2_plan
    first_day = plan.request.start_date
    assert next(item for item in plan.items if item.task_id == "A-I").start_at is not None

    epoch = app.session_state.v2_input_epoch
    app.selectbox(key=f"v2_water_A_{epoch}").set_value(True)
    app.button(key="v2_save_inputs").click().run()
    app.button(key="v2_generate").click().run()

    assert not app.exception
    item = next(item for item in app.session_state.v2_plan.items if item.task_id == "A-I")
    assert item.window_by_date[first_day] == "pending"


def test_page_feedback_rechecks_one_field_and_keeps_other_plot_schedule(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    _set_daily_confirmations(app)
    app.button(key="v2_save_inputs").click().run()
    app.button(key="v2_generate").click().run()
    before = app.session_state.v2_plan
    before_items = {item.task_id: item for item in before.items}
    assert before_items["C-I"].start_at is not None

    app.selectbox(key="v2_work_plot").set_value("A")
    app.selectbox(key="v2_work_day").set_value(before.request.start_date)
    app.selectbox(key="v2_work_site").set_value(False)
    app.selectbox(key="v2_work_weather").set_value(None)
    app.button(key="v2_submit_workability").click().run()

    assert not app.exception
    after = {item.task_id: item for item in app.session_state.v2_plan.items}
    assert after["A-I"].window_by_date[before.request.start_date] == "unavailable"
    assert after["C-I"].start_at == before_items["C-I"].start_at
    assert PlanStoreV2(tmp_path / "plans.sqlite3").latest().id == app.session_state.v2_plan.id


def test_page_replays_reference_v1_to_v3_and_restores_latest_after_reload(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.button(key="v2_replay").click().run()

    assert not app.exception
    plan = app.session_state.v2_plan
    assert plan.reference_version == "V3"
    assert plan.request.completed_at.keys() >= {"A-I", "C-I", "B-I"}
    assert (
        date(2026, 10, 20)
        in next(item for item in plan.items if item.task_id == "B-M").window_by_date
    )
    stored = PlanStoreV2(database)
    assert stored.history(plan.id)[0].reference_version == "V1"
    assert stored.history(plan.id)[-1] == plan

    reopened = AppTest.from_file(APP, default_timeout=20).run()
    assert not reopened.exception
    assert reopened.session_state.v2_plan.id == plan.id


def test_reference_replay_does_not_hide_previous_plan_from_page(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(tmp_path / "plans.sqlite3"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    _set_daily_confirmations(app)
    app.button(key="v2_save_inputs").click().run()
    app.button(key="v2_generate").click().run()
    original_id = app.session_state.v2_plan.id

    app.button(key="v2_replay").click().run()
    assert app.session_state.v2_plan.reference_version == "V3"
    assert any(widget.key == "v2_plan_head" for widget in app.selectbox)
    app.selectbox(key="v2_plan_head").set_value(original_id).run()

    assert not app.exception
    assert app.session_state.v2_plan.id == original_id


def test_page_recovers_when_another_session_extends_current_plan(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "plans.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=20).run()
    _set_daily_confirmations(app)
    app.button(key="v2_save_inputs").click().run()
    app.button(key="v2_generate").click().run()
    original = app.session_state.v2_plan

    from nongshi.planning_v2 import build_plan_v2

    extension = build_plan_v2(original.request, previous=original)
    PlanStoreV2(database).save(extension)
    app.run()

    assert not app.exception
    assert app.session_state.v2_plan.id == extension.id
