"""Reliability contracts use explicit clocks and isolated stores."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from nongshi.planning_v2 import apply_feedback_v2
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v2 import (
    LOCAL_ZONE,
    DayWindow,
    FeedbackV2,
    FieldProfile,
    TimeWindow,
    WorkabilityConfirmation,
)
from nongshi.schema_v3 import FactEventInputV3, FieldDraftV3
from nongshi.service_v3 import WorkbenchServiceV3

START = date(2026, 10, 19)


def service_at(tmp_path: Path, hour: int = 8, minute: int = 0, second: int = 0):
    clock = [datetime(2026, 10, 19, hour, minute, second, tzinfo=LOCAL_ZONE)]
    service = WorkbenchServiceV3(WorkbenchStoreV3(tmp_path / "workbench.db"), now=lambda: clock[0])
    draft = FieldDraftV3(
        field_id="field-1",
        start_date=START,
        profile=FieldProfile(
            id="field-1",
            name="一号田",
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
        days=[
            DayWindow(
                day=START + timedelta(days=offset),
                site_workable=True,
                weather_workable=True,
                resource_minutes=120,
                intervals=[TimeWindow(start="09:00", end="11:00")],
            )
            for offset in range(7)
        ],
    )
    service.save_field_draft(draft, expected_version=None, operation_id="create")
    return service, clock


def adopt(service: WorkbenchServiceV3):
    preview = service.preview_plan("field-1")
    service.adopt_plan(
        preview.id,
        expected_fact_version=preview.expected_fact_version,
        expected_plan_id=preview.expected_plan_id,
        operation_id=f"adopt-{preview.id}",
    )
    return preview


def complete(service: WorkbenchServiceV3):
    adopt(service)
    event = FactEventInputV3(
        feedback={"kind": "completed", "task_id": "field-1-I", "at": "2026-10-19T09:30:00+08:00"}
    )
    service.commit_events("field-1", [event], expected_fact_version=0, operation_id="complete")
    adopt(service)
    return event


def test_live_clock_rounds_up_and_never_assigns_new_work_to_elapsed_minutes(tmp_path: Path):
    service, clock = service_at(tmp_path, 10, 5, 1)
    preview = service.preview_plan("field-1")
    item = next(item for item in preview.version.items if item.task_id == "field-1-I")
    assert item.start_at == clock[0].replace(minute=6, second=0)
    assert preview.version.plan.not_before == item.start_at


def test_live_replan_retains_completed_original_slot_after_time_has_passed(tmp_path: Path):
    service, clock = service_at(tmp_path)
    complete(service)
    clock[0] = clock[0].replace(hour=16)
    item = service.preview_plan("field-1").version.items[0]
    assert item.completed
    assert item.start_at == clock[0].replace(hour=9)


def test_adopted_completion_can_be_corrected_then_retracted_without_erasing_history(tmp_path: Path):
    service, _ = service_at(tmp_path)
    original = complete(service)
    corrected = FactEventInputV3(
        correction_of=original.event_id,
        feedback={"kind": "completed", "task_id": "field-1-I", "at": "2026-10-19T09:40:00+08:00"},
    )
    service.commit_events("field-1", [corrected], expected_fact_version=1, operation_id="correct")
    corrected_plan = adopt(service)
    assert corrected_plan.version.plan.request.completed_at["field-1-I"].minute == 40
    assert service.get_current_plan("field-1").plan.request.completed_at["field-1-I"].minute == 40
    retraction = FactEventInputV3(correction_of=corrected.event_id, retracts=True)
    service.commit_events("field-1", [retraction], expected_fact_version=2, operation_id="retract")
    reopened = adopt(service)
    assert "field-1-I" not in reopened.version.plan.request.completed_at
    assert not reopened.version.items[0].completed
    assert [event.event_id for event in service.list_events("field-1")] == [
        original.event_id,
        corrected.event_id,
        retraction.event_id,
    ]
    assert service.plan_history("field-1")[0].plan.request.completed_at == {}
    assert service.plan_history("field-1")[1].plan.request.completed_at["field-1-I"].minute == 30


def test_completion_correction_requires_matching_active_chain_and_duplicate_is_rejected(
    tmp_path: Path,
):
    service, _ = service_at(tmp_path)
    original = complete(service)
    with pytest.raises(ValueError, match="重复"):
        service.commit_events(
            "field-1",
            [FactEventInputV3(feedback=original.feedback)],
            expected_fact_version=1,
            operation_id="duplicate",
        )
    retraction = FactEventInputV3(correction_of=original.event_id, retracts=True)
    service.commit_events("field-1", [retraction], expected_fact_version=1, operation_id="retract")
    with pytest.raises(ValueError, match="最新|有效"):
        service.commit_events(
            "field-1",
            [FactEventInputV3(correction_of=original.event_id, retracts=True)],
            expected_fact_version=2,
            operation_id="fork",
        )
    revived = FactEventInputV3(correction_of=retraction.event_id, feedback=original.feedback)
    service.commit_events("field-1", [revived], expected_fact_version=2, operation_id="revive")
    assert service.preview_plan("field-1").version.items[0].completed


def test_latest_actionable_preview_survives_reopen_and_drops_stale_or_cancelled(tmp_path: Path):
    service, clock = service_at(tmp_path)
    preview = service.preview_plan("field-1", operation_id="manual-preview")
    reopened = WorkbenchServiceV3(service.store, now=lambda: clock[0])
    assert reopened.latest_actionable_preview("field-1").id == preview.id
    assert service.store.latest_actionable_preview("field-1").id == preview.id
    service.cancel_preview(preview.id)
    assert reopened.latest_actionable_preview("field-1") is None
    next_preview = service.preview_plan("field-1")
    state = service.get_field("field-1")
    service.save_field_draft(
        state.draft.model_copy(update={"notes": "修改"}),
        expected_version=state.draft_version,
        operation_id="edit",
    )
    assert reopened.latest_actionable_preview("field-1") is None
    with pytest.raises(ValueError, match="过期"):
        service.adopt_plan(
            next_preview.id,
            expected_fact_version=0,
            expected_plan_id=None,
            operation_id="adopt-stale",
        )


def test_start_change_preserves_absolute_dates_and_blanks_added_days(tmp_path: Path):
    service, _ = service_at(tmp_path)
    state = service.get_field("field-1")
    shifted = state.draft.model_copy(update={"start_date": START + timedelta(days=2)})
    saved = service.save_field_draft(shifted, expected_version=1, operation_id="shift")
    assert saved.draft.days[0] == state.draft.days[2]
    assert saved.draft.days[-1].day == START + timedelta(days=8)
    for day in saved.draft.days[-2:]:
        assert day.site_workable is None
        assert day.weather_workable is None
        assert day.resource_minutes is None
        assert day.intervals == []


@pytest.mark.parametrize("consume", ["cancel", "adopt"])
def test_consumed_latest_preview_never_revives_older_preview(tmp_path: Path, consume: str):
    service, _ = service_at(tmp_path)
    service.preview_plan("field-1")
    latest = service.preview_plan("field-1")
    if consume == "cancel":
        service.cancel_preview(latest.id)
    else:
        service.adopt_plan(
            latest.id, expected_fact_version=0, expected_plan_id=None, operation_id="adopt-latest"
        )
    assert service.latest_actionable_preview("field-1") is None
    assert service.store.latest_actionable_preview("field-1") is None


def test_elapsed_preview_is_not_restored_or_adopted_after_reload(tmp_path: Path):
    service, clock = service_at(tmp_path)
    preview = service.preview_plan("field-1")
    clock[0] = clock[0].replace(hour=10)
    assert service.latest_actionable_preview("field-1") is None
    with pytest.raises(ValueError, match="过期|已过"):
        service.adopt_plan(
            preview.id, expected_fact_version=0, expected_plan_id=None, operation_id="late-adoption"
        )
    assert service.get_current_plan("field-1") is None


def test_duplicate_completion_in_one_batch_is_atomic(tmp_path: Path):
    service, _ = service_at(tmp_path)
    adopt(service)
    completion = {"kind": "completed", "task_id": "field-1-I", "at": "2026-10-19T09:30:00+08:00"}
    with pytest.raises(ValueError, match="重复"):
        service.commit_events(
            "field-1",
            [FactEventInputV3(feedback=completion), FactEventInputV3(feedback=completion)],
            expected_fact_version=0,
            operation_id="twice",
        )
    assert service.list_events("field-1") == []


def test_ordinary_profile_edit_cannot_release_adopted_completion(tmp_path: Path):
    service, _ = service_at(tmp_path)
    complete(service)
    state = service.get_field("field-1")
    profile = state.draft.profile.model_copy(update={"inspection_requested": False})
    service.save_field_draft(
        state.draft.model_copy(update={"profile": profile}),
        expected_version=state.draft_version,
        operation_id="ordinary-edit",
    )
    assert service.preview_plan("field-1").version.items[0].completed


def test_new_date_range_ignores_out_of_range_daily_events_but_preserves_history(tmp_path: Path):
    service, _ = service_at(tmp_path)
    event = FactEventInputV3(
        feedback={
            "kind": "resource",
            "day": START,
            "at": "2026-10-19T08:00:00+08:00",
            "intervals": [{"start": "10:00", "end": "11:00"}],
            "resource_minutes": 60,
        }
    )
    service.commit_events("field-1", [event], expected_fact_version=0, operation_id="resource")
    state = service.get_field("field-1")
    service.save_field_draft(
        state.draft.model_copy(update={"start_date": START + timedelta(days=2)}),
        expected_version=state.draft_version,
        operation_id="later",
    )
    preview = service.preview_plan("field-1")
    assert preview.version.plan.request.days[0].day == START + timedelta(days=2)
    assert service.list_events("field-1")[0].event_id == event.event_id


def test_completion_correction_accepts_explicit_equivalent_field_id(tmp_path: Path):
    service, _ = service_at(tmp_path)
    original = complete(service)
    corrected = FactEventInputV3(
        correction_of=original.event_id,
        feedback=original.feedback.model_copy(update={"plot_id": "field-1"}),
    )
    service.commit_events("field-1", [corrected], expected_fact_version=1, operation_id="correct")
    assert service.preview_plan("field-1").version.items[0].completed


def test_legacy_completion_without_event_stays_locked_under_ordinary_edit(tmp_path: Path):
    service, _ = service_at(tmp_path)
    preview = service.preview_plan("field-1")
    legacy_plan = apply_feedback_v2(
        preview.version.plan,
        FeedbackV2(
            kind="completed",
            task_id="field-1-I",
            at=datetime(2026, 10, 19, 9, 30, tzinfo=LOCAL_ZONE),
        ),
    )
    legacy = preview.model_copy(
        update={
            "id": "legacy-preview",
            "version": preview.version.model_copy(update={"plan": legacy_plan}),
        }
    )
    service.store.save_preview(legacy, operation_id="import-legacy")
    service.adopt_plan(
        legacy.id, expected_fact_version=0, expected_plan_id=None, operation_id="adopt-legacy"
    )
    state = service.get_field("field-1")
    service.save_field_draft(
        state.draft.model_copy(update={"notes": "普通修改"}),
        expected_version=state.draft_version,
        operation_id="edit",
    )
    assert service.preview_plan("field-1").version.items[0].completed
    assert service.list_events("field-1") == []
    with pytest.raises(ValueError, match="重复"):
        service.commit_events(
            "field-1",
            [FactEventInputV3(feedback=legacy_plan.feedback)],
            expected_fact_version=0,
            operation_id="repeat-legacy",
        )


def test_alignment_keeps_profile_confirmations_on_actual_dates(tmp_path: Path):
    service, _ = service_at(tmp_path)
    state = service.get_field("field-1")
    profile = state.draft.profile.model_copy(
        update={
            "workability_by_date": {
                START: WorkabilityConfirmation(site=True, weather=True),
                START + timedelta(days=2): WorkabilityConfirmation(site=False, weather=True),
            }
        }
    )
    shifted = state.draft.model_copy(
        update={"start_date": START + timedelta(days=2), "profile": profile}
    )
    saved = service.save_field_draft(shifted, expected_version=1, operation_id="shift-profile")
    assert list(saved.draft.profile.workability_by_date) == [START + timedelta(days=2)]
    assert saved.draft.profile.workability_by_date[START + timedelta(days=2)].site is False


def test_reviving_old_retraction_cannot_override_another_active_completion_chain(tmp_path: Path):
    service, _ = service_at(tmp_path)
    original = complete(service)
    retracted = FactEventInputV3(correction_of=original.event_id, retracts=True)
    service.commit_events(
        "field-1", [retracted], expected_fact_version=1, operation_id="retract-original"
    )
    adopt(service)
    second = FactEventInputV3(
        feedback=original.feedback.model_copy(
            update={"at": datetime(2026, 10, 19, 9, 45, tzinfo=LOCAL_ZONE)}
        )
    )
    service.commit_events("field-1", [second], expected_fact_version=2, operation_id="complete-new")
    current = adopt(service)
    history_count = len(service.plan_history("field-1"))
    revival = FactEventInputV3(correction_of=retracted.event_id, feedback=original.feedback)
    with pytest.raises(ValueError, match="重复"):
        service.commit_events(
            "field-1", [revival], expected_fact_version=3, operation_id="revive-old-chain"
        )
    assert len(service.list_events("field-1")) == 3
    assert service.get_field("field-1").fact_version == 3
    assert len(service.plan_history("field-1")) == history_count
    assert service.get_current_plan("field-1").id == current.version.id
    assert service.get_current_plan("field-1").plan.request.completed_at["field-1-I"].minute == 45
