"""Visible wording must not change the underlying three-state field values."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from nongshi.presentation import (
    field_value_label,
    proposal_change_rows,
    task_series_confirmation_rows,
)
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v3 import AgentProposalV3, AgentRunV3, FactEventInputV3
from nongshi.tasks_v4 import TaskSeriesV4
from nongshi.workbench import _proposal_event_description

APP = Path(__file__).resolve().parents[1] / "src/nongshi/app.py"


def test_periodic_task_confirmation_shows_every_requested_date_and_execution_content() -> None:
    series = TaskSeriesV4.model_validate(
        {
            "id": "series-1",
            "field_id": "field-1",
            "template": {
                "id": "template-1",
                "title": "周期巡田",
                "task_type": "inspection",
                "category": "onsite",
                "purpose": "检查苗情和沟渠",
                "result_fields": ["苗情", "积水", "沟渠是否堵塞"],
                "completion_standard": "三项均实际检查并记录",
            },
            "recurrence": "interval",
            "anchor_date": "2026-10-27",
            "end_date": "2026-11-02",
            "interval_days": 2,
            "duration_minutes": 30,
        }
    )

    rows = "\n".join(task_series_confirmation_rows(series))

    assert "每隔 2 天" in rows
    assert "2026-10-27、2026-10-29、2026-10-31、2026-11-02" in rows
    assert "30 分钟" in rows
    assert "苗情、积水、沟渠是否堵塞" in rows
    assert "三项均实际检查并记录" in rows
    assert "interval" not in rows


def test_proposal_rows_show_old_and_new_values_without_dropping_unknown_fields() -> None:
    rows = proposal_change_rows(
        {
            "maintenance_problem_confirmed": {"old": None, "new": True},
            "maintenance_need_confirmed": {"old": False, "new": True},
            "unmapped_future_field": {"old": 1, "new": 2},
        }
    )
    assert rows[0] == "沟渠问题：暂不确定 → 有"
    assert rows[1] == "沟渠维护：不需要 → 需要"
    assert "unmapped_future_field" in rows[2]
    assert "1 → 2" in rows[2]


def test_supported_agent_changes_all_have_readable_labels() -> None:
    fields = [
        "name",
        "province",
        "province_confirmed",
        "crop",
        "crop_confirmed",
        "stage",
        "stage_confirmed",
        "field_type",
        "area_mu",
        "inspection_requested",
        "maintenance_need_confirmed",
        "maintenance_problem_confirmed",
        "maintenance_purpose",
        "moisture",
        "ditch_connectivity",
        "standing_water",
        "leaf_count",
        "actual_rain_observed",
        "winter_before_context_confirmed",
        "inspection_duration_minutes",
        "maintenance_duration_minutes",
        "inspection_priority",
        "maintenance_priority",
        "inspection_earliest_date",
        "inspection_due_date",
        "maintenance_earliest_date",
        "maintenance_due_date",
    ]
    rows = proposal_change_rows({field: {"old": None, "new": True} for field in fields})
    assert len(rows) == len(fields)
    assert all("其他信息（" not in row for row in rows)
    assert field_value_label("maintenance_need_confirmed", False) == "不需要"
    assert field_value_label("maintenance_problem_confirmed", False) == "没有"


def test_feedback_summary_uses_task_name_and_field_specific_choices() -> None:
    completed = FactEventInputV3(
        feedback={"kind": "completed", "task_id": "internal-1", "at": "2026-10-25T10:00:00+08:00"}
    )
    problem = FactEventInputV3(
        feedback={
            "kind": "observation",
            "plot_id": "field-1",
            "field": "maintenance_problem_confirmed",
            "value": True,
            "at": "2026-10-25T10:00:00+08:00",
        }
    )
    assert "巡田与沟渠检查" in _proposal_event_description(
        completed, task_names={"internal-1": "巡田与沟渠检查"}
    )
    assert "沟渠问题：有" in _proposal_event_description(problem)


def test_field_form_uses_specific_choices_and_preserves_unanswered_values(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "interaction.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    field_id = app.session_state["new_field_id:real"]
    assert app.selectbox(key=f"maintenance_need:{field_id}:0").options == [
        "暂不确定",
        "需要",
        "不需要",
    ]
    assert app.selectbox(key=f"maintenance_problem:{field_id}:0").options == [
        "暂不确定",
        "有",
        "没有",
    ]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.button(key=f"save_field:{field_id}:0").click().run()
    assert not app.exception
    saved = WorkbenchStoreV3(database).get_field(field_id).draft.profile
    assert saved is not None
    assert saved.maintenance_need_confirmed is None
    assert saved.maintenance_problem_confirmed is None


def test_confirmed_ditch_problem_survives_reopening_editor(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "ditch.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.selectbox(key=f"maintenance_need:{field_id}:0").set_value("需要")
    app.selectbox(key=f"maintenance_problem:{field_id}:0").set_value("有")
    app.button(key=f"save_field:{field_id}:0").click().run()
    assert not app.exception
    saved = WorkbenchStoreV3(database).get_field(field_id).draft.profile
    assert saved is not None
    assert saved.maintenance_need_confirmed is True
    assert saved.maintenance_problem_confirmed is True
    app.button(key=f"edit_field:{field_id}").click().run()
    assert not app.exception
    assert app.selectbox(key=f"maintenance_problem:{field_id}:1").value == "有"


@pytest.mark.parametrize(
    ("need_label", "need_value"),
    [("暂不确定", None), ("需要", True), ("不需要", False)],
)
@pytest.mark.parametrize(
    ("problem_label", "problem_value"),
    [("暂不确定", None), ("有", True), ("没有", False)],
)
def test_ditch_questions_save_independent_three_state_values(
    tmp_path: Path,
    monkeypatch,
    need_label: str,
    need_value: bool | None,
    problem_label: str,
    problem_value: bool | None,
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "ditch-combinations.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.selectbox(key=f"maintenance_need:{field_id}:0").set_value(need_label)
    app.selectbox(key=f"maintenance_problem:{field_id}:0").set_value(problem_label)
    app.button(key=f"save_field:{field_id}:0").click().run()
    assert not app.exception
    profile = WorkbenchStoreV3(database).get_field(field_id).draft.profile
    assert profile is not None
    assert profile.maintenance_need_confirmed is need_value
    assert profile.maintenance_problem_confirmed is problem_value


def test_water_and_blockage_do_not_infer_maintenance_need(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "no-inference.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.selectbox(key=f"field_water:{field_id}:0").set_value("是")
    app.selectbox(key=f"field_ditch:{field_id}:0").set_value("受阻")
    app.button(key=f"save_field:{field_id}:0").click().run()
    assert not app.exception
    profile = WorkbenchStoreV3(database).get_field(field_id).draft.profile
    assert profile is not None
    assert profile.standing_water is True
    assert profile.ditch_connectivity == "impaired"
    assert profile.maintenance_need_confirmed is None
    assert profile.maintenance_problem_confirmed is None


def test_assistant_change_is_readable_and_saves_only_after_confirmation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "assistant-copy.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.button(key=f"save_field:{field_id}:0").click().run()
    store = WorkbenchStoreV3(database)
    state = store.get_field(field_id)
    updated_profile = state.draft.profile.model_copy(  # type: ignore[union-attr]
        update={"maintenance_problem_confirmed": True}
    )
    proposal = AgentProposalV3(
        kind="field_update",
        field_id=field_id,
        expected_draft_version=state.draft_version,
        draft=state.draft.model_copy(update={"profile": updated_profile}),
        summary="请核对修改",
        changes={"maintenance_problem_confirmed": {"old": None, "new": True}},
    )
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
        [proposal],
    )
    app.run()
    text = "\n".join(item.value for item in app.markdown)
    assert "沟渠问题：暂不确定 → 有" in text
    assert store.get_field(field_id).draft.profile.maintenance_problem_confirmed is None  # type: ignore[union-attr]
    app.button(key=f"confirm_proposal:{proposal.id}").click().run()
    assert not app.exception
    assert store.get_field(field_id).draft.profile.maintenance_problem_confirmed is True  # type: ignore[union-attr]


def test_abandoned_assistant_change_leaves_field_unchanged(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "assistant-ignore.sqlite3"
    monkeypatch.setenv("NONGSHI_DATABASE_PATH", str(database))
    app = AppTest.from_file(APP, default_timeout=30).run()
    field_id = app.session_state["new_field_id:real"]
    app.text_input(key=f"field_name:{field_id}:0").set_value("一号田")
    app.button(key=f"save_field:{field_id}:0").click().run()
    store = WorkbenchStoreV3(database)
    state = store.get_field(field_id)
    proposal = AgentProposalV3(
        kind="field_update",
        field_id=field_id,
        expected_draft_version=state.draft_version,
        draft=state.draft.model_copy(
            update={
                "profile": state.draft.profile.model_copy(  # type: ignore[union-attr]
                    update={"maintenance_need_confirmed": True}
                )
            }
        ),
        summary="请核对修改",
        changes={"maintenance_need_confirmed": {"old": None, "new": True}},
    )
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
        [proposal],
    )
    app.run()
    app.button(key=f"ignore_proposal:{proposal.id}").click().run()
    assert not app.exception
    assert store.get_field(field_id).draft.profile.maintenance_need_confirmed is None  # type: ignore[union-attr]
