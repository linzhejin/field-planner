"""Task actions preserve facts and history while producing reviewable previews."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v2 import LOCAL_ZONE, DayWindow, FieldProfile, TimeWindow
from nongshi.schema_v3 import FactEventInputV3, FieldDraftV3
from nongshi.service_v3 import WorkbenchServiceV3

START = date(2026, 10, 23)
FIELD = "field-1"
TASK = "field-1-I"


def make_service(tmp_path: Path):
    clock = [datetime(2026, 10, 23, 8, tzinfo=LOCAL_ZONE)]
    service = WorkbenchServiceV3(WorkbenchStoreV3(tmp_path / "tasks.db"), now=lambda: clock[0])
    draft = FieldDraftV3(
        field_id=FIELD,
        start_date=START,
        profile=FieldProfile(
            id=FIELD,
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
                day=START + timedelta(days=i),
                site_workable=True,
                weather_workable=True,
                resource_minutes=120,
                intervals=[TimeWindow(start="09:00", end="11:00")],
            )
            for i in range(7)
        ],
    )
    service.save_field_draft(draft, expected_version=None, operation_id="create")
    return service, clock


def adopt(service, preview=None):
    preview = preview or service.preview_plan(FIELD)
    return service.adopt_plan(
        preview.id,
        expected_fact_version=preview.expected_fact_version,
        expected_plan_id=preview.expected_plan_id,
        operation_id=f"adopt-{preview.id}",
    ).version


def task(preview):
    return next(item for item in preview.version.items if item.task_id == TASK)


def edit(service, **updates):
    state = service.get_field(FIELD)
    args = dict(
        preferred_date=START + timedelta(days=1),
        duration_minutes=45,
        priority=1,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="edit",
    )
    return service.edit_task(FIELD, TASK, **(args | updates))


def complete(service):
    adopt(service)
    service.commit_events(
        FIELD,
        [
            FactEventInputV3(
                feedback={
                    "kind": "completed",
                    "task_id": TASK,
                    "at": "2026-10-23T09:30:00+08:00",
                }
            )
        ],
        expected_fact_version=0,
        operation_id="complete",
    )
    return adopt(service)


def test_edit_saves_confirmed_settings_but_requires_adoption(tmp_path):
    service, _ = make_service(tmp_path)
    original = adopt(service)
    preview = edit(service)
    assert task(preview).start_at.date() == START + timedelta(days=1)
    assert task(preview).duration_minutes == 45
    assert service.get_field(FIELD).draft.profile.inspection_priority == 1
    assert service.get_current_plan(FIELD).id == original.id
    assert preview.expected_draft_version == 2


@pytest.mark.parametrize("condition", [False, None])
def test_requested_date_obeys_current_fact_conditions(tmp_path, condition):
    service, _ = make_service(tmp_path)
    adopt(service)
    requested = START + timedelta(days=1)
    service.commit_events(
        FIELD,
        [
            FactEventInputV3(
                feedback={
                    "kind": "workability",
                    "plot_id": FIELD,
                    "day": requested,
                    "site_workable": condition,
                    "weather_workable": True,
                    "at": "2026-10-23T08:00:00+08:00",
                }
            )
        ],
        expected_fact_version=0,
        operation_id="condition",
    )
    preview = edit(service, preferred_date=requested)
    assert task(preview).start_at is None
    assert task(preview).schedule_status in {"pending", "unscheduled"}


@pytest.mark.parametrize(
    "updates",
    [
        {"preferred_date": START - timedelta(days=1)},
        {"duration_minutes": 0},
        {"priority": 4},
        {"expected_draft_version": 0},
        {"expected_fact_version": 1},
    ],
)
def test_invalid_or_stale_edits_do_not_save(tmp_path, updates):
    service, _ = make_service(tmp_path)
    adopt(service)
    before = service.get_field(FIELD)
    with pytest.raises(ValueError):
        edit(service, **updates)
    assert service.get_field(FIELD) == before


def test_completed_task_cannot_be_edited_or_cancelled(tmp_path):
    service, _ = make_service(tmp_path)
    original = complete(service)
    with pytest.raises(ValueError, match="完成"):
        edit(service)
    with pytest.raises(ValueError, match="完成"):
        service.cancel_task(
            FIELD,
            TASK,
            reason="改期",
            expected_draft_version=1,
            expected_fact_version=1,
            operation_id="cancel",
        )
    assert service.get_current_plan(FIELD).id == original.id
    assert len(service.list_events(FIELD)) == 1


def test_cancel_requires_reason_and_persists_through_reopen_and_fact_replay(tmp_path):
    service, clock = make_service(tmp_path)
    original = adopt(service)
    with pytest.raises(ValueError, match="原因"):
        service.cancel_task(
            FIELD,
            TASK,
            reason="  ",
            expected_draft_version=1,
            expected_fact_version=0,
            operation_id="blank",
        )
    preview = service.cancel_task(
        FIELD,
        TASK,
        reason="今日现场封闭",
        expected_draft_version=1,
        expected_fact_version=0,
        operation_id="cancel",
    )
    assert task(preview).schedule_status == "cancelled"
    assert task(preview).start_at is None
    assert "现场封闭" in task(preview).reason
    assert next(c for c in preview.version.changes if c.task_id == TASK).kind == "cancelled"
    assert service.get_current_plan(FIELD).id == original.id
    adopt(service, preview)
    reopened = WorkbenchServiceV3(service.store, now=lambda: clock[0])
    reopened.commit_events(
        FIELD,
        [
            FactEventInputV3(
                feedback={
                    "kind": "observation",
                    "plot_id": FIELD,
                    "field": "pest_signs",
                    "value": False,
                    "at": clock[0],
                }
            )
        ],
        expected_fact_version=1,
        operation_id="observation",
    )
    assert task(reopened.preview_plan(FIELD)).schedule_status == "cancelled"
    assert "今日现场封闭" in reopened.export_json(FIELD)
    assert "已取消" in reopened.export_markdown(FIELD)


def test_history_restore_preserves_current_completion_and_current_profile(tmp_path):
    service, _ = make_service(tmp_path)
    original = adopt(service)
    current = complete(service)
    state = service.get_field(FIELD)
    profile = state.draft.profile.model_copy(update={"inspection_requested": False})
    service.save_field_draft(
        state.draft.model_copy(update={"profile": profile}),
        expected_version=1,
        operation_id="current-profile",
    )
    before = service.get_field(FIELD)
    preview = service.preview_restore_plan(FIELD, original.id, operation_id="restore")
    assert task(preview).completed
    assert task(preview).start_at == current.items[0].start_at
    assert service.get_field(FIELD) == before
    assert service.get_current_plan(FIELD).id == current.id
    assert preview.expected_draft_version == 2
    assert preview.expected_fact_version == 1
    assert preview.version.id not in {original.id, current.id}


def test_history_restore_revalidates_current_capacity_and_time_floor(tmp_path):
    service, clock = make_service(tmp_path)
    original = adopt(service)
    for i in range(7):
        state = service.get_field(FIELD)
        service.commit_events(
            FIELD,
            [
                FactEventInputV3(
                    feedback={
                        "kind": "resource",
                        "day": START + timedelta(days=i),
                        "intervals": [],
                        "resource_minutes": 0,
                        "at": clock[0],
                    }
                )
            ],
            expected_fact_version=state.fact_version,
            operation_id=f"capacity-{i}",
        )
    clock[0] = clock[0].replace(hour=10)
    preview = service.preview_restore_plan(FIELD, original.id, operation_id="restore")
    assert task(preview).start_at is None
    assert service.get_current_plan(FIELD).id == original.id
    assert len(service.plan_history(FIELD)) == 1
    assert preview.version.plan.not_before >= clock[0]


def test_history_restore_rejects_another_field_version(tmp_path):
    service, _ = make_service(tmp_path)
    original = adopt(service)
    other = service.get_field(FIELD).draft.model_copy(deep=True)
    other.field_id = "field-2"
    other.profile.id = "field-2"
    service.save_field_draft(other, expected_version=None, operation_id="create-other")
    with pytest.raises(ValueError, match="地块"):
        service.preview_restore_plan("field-2", original.id, operation_id="wrong-field")


def test_pending_completion_fact_locks_task_before_adoption(tmp_path):
    service, _ = make_service(tmp_path)
    adopt(service)
    service.commit_events(
        FIELD,
        [
            FactEventInputV3(
                feedback={
                    "kind": "completed",
                    "task_id": TASK,
                    "at": "2026-10-23T09:30:00+08:00",
                }
            )
        ],
        expected_fact_version=0,
        operation_id="complete",
    )
    with pytest.raises(ValueError, match="完成"):
        edit(service)
    with pytest.raises(ValueError, match="完成"):
        service.cancel_task(
            FIELD,
            TASK,
            reason="误操作",
            expected_draft_version=1,
            expected_fact_version=1,
            operation_id="cancel",
        )


def test_pending_cancellation_cannot_be_overridden_by_completion(tmp_path):
    service, _ = make_service(tmp_path)
    adopt(service)
    service.cancel_task(
        FIELD,
        TASK,
        reason="不再执行",
        expected_draft_version=1,
        expected_fact_version=0,
        operation_id="cancel",
    )
    with pytest.raises(ValueError, match="取消"):
        service.commit_events(
            FIELD,
            [
                FactEventInputV3(
                    feedback={
                        "kind": "completed",
                        "task_id": TASK,
                        "at": "2026-10-23T09:30:00+08:00",
                    }
                )
            ],
            expected_fact_version=1,
            operation_id="complete-cancelled",
        )
    assert len(service.list_events(FIELD)) == 1


@pytest.mark.parametrize("versions", [(0, 0), (1, 1)])
def test_stale_cancellation_preserves_event_history(tmp_path, versions):
    service, _ = make_service(tmp_path)
    adopt(service)
    with pytest.raises(ValueError, match="版本冲突"):
        service.cancel_task(
            FIELD,
            TASK,
            reason="取消",
            expected_draft_version=versions[0],
            expected_fact_version=versions[1],
            operation_id="stale",
        )
    assert service.list_events(FIELD) == []


def test_history_restore_keeps_current_cancellation_and_revalidates_current_duration(tmp_path):
    service, _ = make_service(tmp_path)
    historic = adopt(service)
    edit(service, preferred_date=START, duration_minutes=150)
    preview = service.preview_restore_plan(FIELD, historic.id)
    assert task(preview).duration_minutes == 150
    assert task(preview).start_at is None
    cancelled = service.cancel_task(
        FIELD,
        TASK,
        reason="不再执行",
        expected_draft_version=2,
        expected_fact_version=0,
        operation_id="cancel",
    )
    adopt(service, cancelled)
    restored = service.preview_restore_plan(FIELD, historic.id)
    assert task(restored).schedule_status == "cancelled"
    assert task(restored).start_at is None


def test_history_restore_uses_old_slot_as_preference_when_current_conditions_allow(tmp_path):
    service, _ = make_service(tmp_path)
    original = adopt(service)
    service.commit_events(
        FIELD,
        [
            FactEventInputV3(
                feedback={
                    "kind": "resource",
                    "day": START,
                    "intervals": [{"start": "10:00", "end": "11:00"}],
                    "resource_minutes": 60,
                    "at": "2026-10-23T08:00:00+08:00",
                }
            )
        ],
        expected_fact_version=0,
        operation_id="shift-resource",
    )
    moved = adopt(service)
    assert moved.items[0].start_at.hour == 10
    event = service.list_events(FIELD)[0]
    service.commit_events(
        FIELD,
        [FactEventInputV3(correction_of=event.event_id, retracts=True)],
        expected_fact_version=1,
        operation_id="restore-resource",
    )
    preview = service.preview_restore_plan(FIELD, original.id)
    assert task(preview).start_at.hour == 9
    assert service.get_current_plan(FIELD).items[0].start_at.hour == 10


def test_atomic_task_guards_are_part_of_operation_identity(tmp_path):
    service, clock = make_service(tmp_path)
    state = service.get_field(FIELD)
    service.store.save_field_draft(
        state.draft, expected_version=1, expected_fact_version=0, operation_id="guarded-save"
    )
    with pytest.raises(ValueError, match="操作|冲突"):
        service.store.save_field_draft(
            state.draft, expected_version=1, expected_fact_version=1, operation_id="guarded-save"
        )
    event = FactEventInputV3(
        feedback={
            "kind": "observation",
            "plot_id": FIELD,
            "field": "pest_signs",
            "value": False,
            "at": clock[0],
        }
    )
    service.store.commit_events(
        FIELD,
        [event],
        expected_fact_version=0,
        expected_draft_version=2,
        operation_id="guarded-facts",
    )
    with pytest.raises(ValueError, match="操作|冲突"):
        service.store.commit_events(
            FIELD,
            [event],
            expected_fact_version=0,
            expected_draft_version=3,
            operation_id="guarded-facts",
        )


def test_restore_operation_identity_includes_selected_history(tmp_path):
    service, _ = make_service(tmp_path)
    original = adopt(service)
    changed = adopt(service, edit(service))
    first = service.preview_restore_plan(FIELD, original.id, operation_id="restore-history")
    assert first.version.plan.reference_version == original.id
    with pytest.raises(ValueError, match="操作|冲突"):
        service.preview_restore_plan(FIELD, changed.id, operation_id="restore-history")


def action_call(service, kind, *, operation_id="task-action", **changes):
    options = dict(expected_draft_version=1, expected_fact_version=0, operation_id=operation_id)
    if kind == "edit":
        options |= dict(preferred_date=START + timedelta(days=1), duration_minutes=45, priority=1)
        method = service.edit_task
    else:
        options |= dict(reason="现场封闭")
        method = service.cancel_task
    return method(FIELD, TASK, **(options | changes))


@pytest.mark.parametrize("kind", ["edit", "cancel"])
def test_task_action_retry_returns_original_preview_without_repeating_mutation(tmp_path, kind):
    service, clock = make_service(tmp_path)
    adopt(service)
    first = action_call(service, kind)
    state = service.get_field(FIELD)
    events = service.list_events(FIELD)
    clock[0] = clock[0] + timedelta(minutes=3)
    reopened = WorkbenchServiceV3(service.store, now=lambda: clock[0])
    retry = action_call(reopened, kind)
    assert retry.id == first.id
    assert service.get_field(FIELD) == state
    assert service.list_events(FIELD) == events
    assert len(service.plan_history(FIELD)) == 1


@pytest.mark.parametrize("kind", ["edit", "cancel"])
def test_task_action_retry_rejects_changed_payload(tmp_path, kind):
    service, _ = make_service(tmp_path)
    adopt(service)
    action_call(service, kind)
    changed = {"duration_minutes": 60} if kind == "edit" else {"reason": "新的原因"}
    with pytest.raises(ValueError, match="操作编号.*不同内容"):
        action_call(service, kind, **changed)


@pytest.mark.parametrize("kind", ["edit", "cancel"])
def test_task_action_retry_resumes_after_preview_failure(tmp_path, monkeypatch, kind):
    service, clock = make_service(tmp_path)
    adopt(service)

    def fail_preview(*args, **kwargs):
        raise RuntimeError("模拟计算中断")

    with monkeypatch.context() as patch:
        patch.setattr(service, "preview_plan", fail_preview)
        with pytest.raises(RuntimeError, match="计算中断"):
            action_call(service, kind)
    state = service.get_field(FIELD)
    events = service.list_events(FIELD)
    reopened = WorkbenchServiceV3(service.store, now=lambda: clock[0])
    retry = action_call(reopened, kind)
    assert retry.status == "preview"
    assert retry.id == action_call(reopened, kind).id
    assert service.get_field(FIELD) == state
    assert service.list_events(FIELD) == events
    assert len(service.plan_history(FIELD)) == 1


@pytest.mark.parametrize("kind", ["edit", "cancel"])
def test_concurrent_identical_task_actions_create_one_mutation_and_preview(tmp_path, kind):
    from concurrent.futures import ThreadPoolExecutor

    service, _ = make_service(tmp_path)
    adopt(service)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = list(workers.map(lambda _: action_call(service, kind), range(2)))
    assert first.id == second.id
    state = service.get_field(FIELD)
    assert state.draft_version == (2 if kind == "edit" else 1)
    assert state.fact_version == (1 if kind == "cancel" else 0)
    assert len(service.list_events(FIELD)) == (1 if kind == "cancel" else 0)


@pytest.mark.parametrize("kind", ["edit", "cancel"])
def test_task_action_recovers_response_after_saved_preview_return_is_interrupted(
    tmp_path, monkeypatch, kind
):
    service, _ = make_service(tmp_path)
    adopt(service)
    preview_ids = []
    original = service.preview_plan

    def save_then_fail(*args, **kwargs):
        preview_ids.append(original(*args, **kwargs).id)
        raise RuntimeError("模拟响应丢失")

    with monkeypatch.context() as patch:
        patch.setattr(service, "preview_plan", save_then_fail)
        with pytest.raises(RuntimeError, match="响应丢失"):
            action_call(service, kind)
    before = service.get_field(FIELD)
    retry = action_call(service, kind)
    assert retry.id == preview_ids[0]
    assert service.get_field(FIELD) == before


def test_cancel_intent_freezes_event_before_database_mutation_failure(tmp_path, monkeypatch):
    service, clock = make_service(tmp_path)
    adopt(service)
    original_time = clock[0]

    def fail_commit(*args, **kwargs):
        raise RuntimeError("模拟提交前中断")

    with monkeypatch.context() as patch:
        patch.setattr(service.store, "commit_events", fail_commit)
        with pytest.raises(RuntimeError, match="提交前中断"):
            action_call(service, "cancel")
    assert service.list_events(FIELD) == []
    clock[0] += timedelta(minutes=1)
    retry = action_call(service, "cancel")
    assert retry.status == "preview"
    events = service.list_events(FIELD)
    assert len(events) == 1
    assert events[0].feedback.at == original_time


@pytest.mark.parametrize("kind", ["edit", "cancel"])
def test_task_action_derived_preview_cannot_recover_unrelated_manual_preview(tmp_path, kind):
    service, _ = make_service(tmp_path)
    adopt(service)
    unrelated = service.preview_plan(FIELD, operation_id="task-action-preview")
    result = action_call(service, kind)
    assert result.id != unrelated.id
    assert result.expected_draft_version == (2 if kind == "edit" else 1)
    assert result.expected_fact_version == (1 if kind == "cancel" else 0)
    if kind == "cancel":
        assert task(result).schedule_status == "cancelled"
    else:
        assert task(result).duration_minutes == 45
    assert action_call(service, kind).id == result.id


@pytest.mark.parametrize("kind", ["edit", "cancel"])
def test_task_action_mutation_ids_do_not_collide_with_external_operations(tmp_path, kind):
    service, _ = make_service(tmp_path)
    adopt(service)
    suffix = "draft" if kind == "edit" else "fact"
    unrelated = service.preview_plan(FIELD, operation_id=f"task-action-{suffix}")
    result = action_call(service, kind)
    assert result.id != unrelated.id
    assert action_call(service, kind).id == result.id
