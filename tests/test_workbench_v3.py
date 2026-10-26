"""Durable field facts, previews, and adopted plans share one service boundary."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import DayWindow, FieldProfile, TimeWindow
from nongshi.schema_v3 import (
    FactEventInputV3,
    FieldDraftV3,
    WeatherDayV3,
    WeatherLocationV3,
    WeatherSnapshotV3,
)
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.tasks_v4 import TaskExecutionGroupV4, TaskSeriesV4


def make_service(path: Path) -> WorkbenchServiceV3:
    return WorkbenchServiceV3(
        WorkbenchStoreV3(path),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 18),
        now=lambda: datetime(2026, 10, 18, tzinfo=UTC),
    )


def draft(field_id: str = "field-1") -> FieldDraftV3:
    profile = FieldProfile(
        id=field_id,
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
    )
    start = date(2026, 10, 19)
    days = [
        DayWindow(
            day=start + timedelta(days=offset),
            site_workable=True,
            weather_workable=True,
            resource_minutes=120,
            intervals=[TimeWindow(start="09:00", end="11:00")],
        )
        for offset in range(7)
    ]
    return FieldDraftV3(field_id=field_id, profile=profile, start_date=start, days=days)


def save_outing_candidates(service: WorkbenchServiceV3, state, issue_id: str) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    candidates = [
        TaskSeriesV4(
            id="outing-inspection",
            field_id="field-1",
            template={
                "id": "outing-inspection-template",
                "title": "周期巡田",
                "task_type": "inspection",
                "category": "onsite",
                "purpose": "查看田间情况",
                "result_fields": ["现场情况"],
                "completion_standard": "记录本次检查结果",
            },
            recurrence="once",
            anchor_date=date(2026, 10, 20),
            duration_minutes=20,
            earliest_date=date(2026, 10, 20),
            due_date=date(2026, 10, 22),
        ),
        TaskSeriesV4(
            id="outing-follow-up",
            field_id="field-1",
            template={
                "id": "outing-follow-up-template",
                "title": "问题复查",
                "task_type": "follow_up",
                "category": "onsite",
                "related_issue_id": issue_id,
                "purpose": "复查已记录问题",
                "result_fields": ["复查情况"],
                "completion_standard": "记录问题是否解决",
            },
            recurrence="once",
            anchor_date=date(2026, 10, 20),
            duration_minutes=30,
            earliest_date=date(2026, 10, 20),
            due_date=date(2026, 10, 22),
        ),
    ]
    for index, series in enumerate(candidates):
        service.save_task_series(
            series,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            operation_id=f"save-outing-candidate-{index}",
        )
    return "outing-inspection:000001", "outing-follow-up:000001"


def create_open_issue(service: WorkbenchServiceV3, state) -> tuple[WorkbenchServiceV3, object]:  # type: ignore[no-untyped-def]
    seed = TaskSeriesV4(
        id="outing-issue-seed",
        field_id="field-1",
        template={
            "id": "outing-issue-seed-template",
            "title": "问题来源巡田",
            "task_type": "inspection",
            "category": "onsite",
            "purpose": "检查问题来源",
            "result_fields": ["现场情况"],
            "completion_standard": "保存现场观察",
        },
        recurrence="once",
        anchor_date=date(2026, 10, 19),
        duration_minutes=10,
    )
    service.save_task_series(
        seed,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-issue-seed",
    )
    preview = service.preview_plan("field-1", operation_id="preview-issue-seed")
    service.adopt_plan(
        preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=None,
        operation_id="adopt-issue-seed",
    )
    later = WorkbenchServiceV3(
        WorkbenchStoreV3(service.store.path),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 19),
        now=lambda: datetime(2026, 10, 19, 4, tzinfo=UTC),
    )
    current = later.get_field("field-1")
    later.record_task_result(
        "field-1",
        "outing-issue-seed:000001",
        {"现场情况": "issue"},
        notes="记录一项待复查的问题",
        expected_draft_version=current.draft_version,
        expected_fact_version=current.fact_version,
        operation_id="record-issue-seed",
    )
    return later, later.list_field_issues("field-1")[0]


def test_blank_field_draft_survives_reopen_and_conflicting_edits_are_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite3"
    first = make_service(database).save_field_draft(
        FieldDraftV3(field_id="field-1"), expected_version=None, operation_id="create-field"
    )
    assert first.draft.profile is None

    service = make_service(database)
    saved = service.save_field_draft(
        draft(), expected_version=first.draft_version, operation_id="save-field"
    )
    assert make_service(database).get_field("field-1") == saved

    with pytest.raises(ValueError, match="版本冲突"):
        service.save_field_draft(
            draft(), expected_version=first.draft_version, operation_id="stale-edit"
        )


def test_periodic_task_instances_persist_and_new_tasks_stale_old_previews(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite3"
    service = make_service(database)
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    series = TaskSeriesV4(
        id="scout-rounds",
        field_id="field-1",
        template={
            "id": "inspection-v1",
            "title": "周期巡田",
            "task_type": "inspection",
            "category": "onsite",
            "purpose": "检查现场变化",
            "steps": ["观察田间情况"],
            "result_fields": ["实际观察"],
            "completion_standard": "保存本次结果",
        },
        recurrence="dates",
        anchor_date=date(2026, 10, 19),
        selected_dates=[date(2026, 10, 19), date(2026, 10, 21), date(2026, 10, 23)],
        duration_minutes=20,
    )

    saved = service.save_task_series(
        series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-series",
    )
    preview = service.preview_plan("field-1", operation_id="preview-series")
    restored = make_service(database)

    assert saved.task_version == 1
    assert len(saved.created_instance_ids) == 3
    assert [
        item.task_id for item in preview.version.items if item.task_id.startswith("scout-rounds")
    ]
    assert (
        len(
            restored.store.list_task_instances(
                "field-1", start_date=date(2026, 10, 19), end_date=date(2026, 10, 25)
            )
        )
        == 3
    )
    assert (
        restored.save_task_series(
            series,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            operation_id="save-series",
        )
        == saved
    )

    second_series = TaskSeriesV4(
        id="custom-office",
        field_id="field-1",
        template={"id": "custom-v1", "title": "整理记录", "category": "offsite"},
        recurrence="once",
        anchor_date=date(2026, 10, 19),
        duration_minutes=15,
    )
    restored.save_task_series(
        second_series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-second-series",
    )
    with pytest.raises(ValueError, match="预览已过期"):
        restored.adopt_plan(
            preview.id,
            expected_fact_version=state.fact_version,
            expected_plan_id=None,
            operation_id="adopt-stale-preview",
        )


def test_execution_group_persists_idempotently_and_shares_one_resource_slot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite3"
    service = make_service(database)
    initial_draft = draft()
    second_day = initial_draft.days[1].model_copy(
        update={
            "resource_minutes": 35,
            "intervals": [TimeWindow(start="09:00", end="09:35")],
        }
    )
    initial_draft = initial_draft.model_copy(
        update={"days": [initial_draft.days[0], second_day, *initial_draft.days[2:]]}
    )
    state = service.save_field_draft(
        initial_draft, expected_version=None, operation_id="create-field"
    )
    service, issue = create_open_issue(service, state)
    members = save_outing_candidates(service, service.get_field("field-1"), issue.id)
    state = service.get_field("field-1")

    args = dict(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=list(members),
        execution_date=date(2026, 10, 20),
        duration_minutes=35,
        expected_task_version=state.task_version,
        operation_id="confirm-outing",
    )
    saved = service.save_task_execution_group(**args)
    retried = make_service(database).save_task_execution_group(**args)
    preview = make_service(database).preview_plan("field-1", operation_id="preview-outing")
    items = {item.task_id: item for item in preview.version.items}

    assert retried == saved
    assert len(make_service(database).store.list_task_execution_groups("field-1")) == 1
    first, second = (items[task_id] for task_id in members)
    assert first.schedule_status == second.schedule_status == "scheduled"
    assert first.start_at == second.start_at
    assert first.end_at == second.end_at
    assert (first.end_at - first.start_at).total_seconds() == 35 * 60
    assert first.duration_minutes == 20
    assert second.duration_minutes == 30
    assert first.group_duration_minutes == second.group_duration_minutes == 35


def test_changed_execution_group_member_requires_reconfirmation_and_cancel_releases_tasks(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service, issue = create_open_issue(service, state)
    members = save_outing_candidates(service, service.get_field("field-1"), issue.id)
    state = service.get_field("field-1")
    service.save_task_execution_group(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=list(members),
        execution_date=date(2026, 10, 20),
        duration_minutes=35,
        expected_task_version=state.task_version,
        operation_id="confirm-outing",
    )
    state = service.get_field("field-1")
    inspection_series = service.store.get_task_series("field-1", "outing-inspection")
    service.update_task_series(
        inspection_series.model_copy(
            update={
                "duration_minutes": 25,
                "template": inspection_series.template.model_copy(update={"version": 2}),
            }
        ),
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="revise-outing-member",
    )

    stale_group = service.list_task_execution_groups("field-1")[0]
    stale_preview = service.preview_plan("field-1", operation_id="preview-stale-outing")
    stale_items = {item.task_id: item for item in stale_preview.version.items}

    assert stale_group.status == "needs_reconfirmation"
    assert all(stale_items[task_id].schedule_status == "pending" for task_id in members)
    refreshed_state = service.get_field("field-1")
    with pytest.raises(ValueError, match="版本冲突"):
        service.save_task_execution_group(
            field_id="field-1",
            group_id="outing-1",
            member_task_ids=list(members),
            execution_date=date(2026, 10, 20),
            duration_minutes=45,
            expected_task_version=state.task_version,
            operation_id="stale-reconfirm",
        )

    service.save_task_execution_group(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=list(members),
        execution_date=date(2026, 10, 20),
        duration_minutes=45,
        expected_task_version=refreshed_state.task_version,
        operation_id="reconfirm-outing",
    )
    reconfirmed = service.preview_plan("field-1", operation_id="preview-reconfirmed-outing")
    confirmed_items = {item.task_id: item for item in reconfirmed.version.items}
    assert confirmed_items[members[0]].schedule_status == "scheduled"
    assert confirmed_items[members[1]].schedule_status == "scheduled"
    assert confirmed_items[members[0]].start_at == confirmed_items[members[1]].start_at

    state_before_adoption = service.get_field("field-1")
    service.adopt_plan(
        reconfirmed.id,
        expected_fact_version=state_before_adoption.fact_version,
        expected_plan_id=state_before_adoption.latest_plan_id,
        operation_id="adopt-reconfirmed-outing",
    )
    final_state = service.get_field("field-1")
    cancel_args = dict(
        field_id="field-1",
        group_id="outing-1",
        expected_task_version=final_state.task_version,
        operation_id="cancel-outing",
    )
    cancelled = service.cancel_task_execution_group(**cancel_args)
    assert (
        WorkbenchServiceV3(
            WorkbenchStoreV3(service.store.path),
            catalog=load_rule_catalog(),
            today=lambda: date(2026, 10, 20),
            now=lambda: datetime(2026, 10, 20, 4, tzinfo=UTC),
        ).cancel_task_execution_group(**cancel_args)
        == cancelled
    )
    released = service.preview_plan("field-1", operation_id="preview-released-tasks")
    released_items = {item.task_id: item for item in released.version.items}
    assert released_items[members[0]].execution_group_id is None
    assert released_items[members[1]].execution_group_id is None
    assert released_items[members[0]].schedule_status == "scheduled"
    assert released_items[members[1]].schedule_status == "scheduled"


def test_execution_group_members_keep_shared_slot_for_independent_results(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service, issue = create_open_issue(service, state)
    members = save_outing_candidates(service, service.get_field("field-1"), issue.id)
    state = service.get_field("field-1")
    service.save_task_execution_group(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=list(members),
        execution_date=date(2026, 10, 20),
        duration_minutes=35,
        expected_task_version=state.task_version,
        operation_id="confirm-outing",
    )
    preview = service.preview_plan("field-1", operation_id="preview-outing")
    state = service.get_field("field-1")
    service.adopt_plan(
        preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=state.latest_plan_id,
        operation_id="adopt-outing",
    )

    after_visit = WorkbenchServiceV3(
        WorkbenchStoreV3(service.store.path),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 20),
        now=lambda: datetime(2026, 10, 20, 4, tzinfo=UTC),
    )
    for index, task_id in enumerate(members):
        state = after_visit.get_field("field-1")
        after_visit.record_task_result(
            "field-1",
            task_id,
            {"现场情况" if index == 0 else "复查情况": "normal"},
            completed=True,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            operation_id=f"complete-group-member-{index}",
        )
        preview = after_visit.preview_plan(
            "field-1", operation_id=f"preview-after-group-member-{index}"
        )
        preview_items = {item.task_id: item for item in preview.version.items}
        assert preview_items[task_id].completed
        if index == 0:
            sibling = preview_items[members[1]]
            assert sibling.schedule_status == "scheduled"
            assert sibling.start_at == preview_items[members[0]].start_at
            assert after_visit.list_task_execution_groups("field-1")[0].status == "confirmed"

    final = after_visit.preview_plan("field-1", operation_id="preview-both-group-members")
    final_items = {item.task_id: item for item in final.version.items}
    assert all(final_items[task_id].completed for task_id in members)
    state = after_visit.get_field("field-1")
    after_visit.adopt_plan(
        final.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=state.latest_plan_id,
        operation_id="adopt-after-both-group-members",
    )
    result_events = [
        event
        for event in after_visit.list_events("field-1")
        if event.feedback and event.feedback.kind == "task_result"
    ]
    assert set(members).issubset({event.feedback.task_id for event in result_events})


def test_stale_execution_group_still_blocks_overlapping_replacement(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service, issue = create_open_issue(service, state)
    members = save_outing_candidates(service, service.get_field("field-1"), issue.id)
    state = service.get_field("field-1")
    service.save_task_execution_group(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=list(members),
        execution_date=date(2026, 10, 20),
        duration_minutes=35,
        expected_task_version=state.task_version,
        operation_id="confirm-outing",
    )
    state = service.get_field("field-1")
    series = service.store.get_task_series("field-1", "outing-inspection")
    service.update_task_series(
        series.model_copy(
            update={
                "duration_minutes": 25,
                "template": series.template.model_copy(update={"version": 2}),
            }
        ),
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="stale-outing-member",
    )
    state = service.get_field("field-1")

    with pytest.raises(ValueError, match="另一合并出行"):
        service.save_task_execution_group(
            field_id="field-1",
            group_id="outing-2",
            member_task_ids=list(members),
            execution_date=date(2026, 10, 20),
            duration_minutes=45,
            expected_task_version=state.task_version,
            operation_id="overlapping-outing",
        )


def test_execution_group_id_cannot_be_reused_across_fields(tmp_path: Path) -> None:
    store = WorkbenchStoreV3(tmp_path / "workbench.sqlite3")
    first = make_service(store.path).save_field_draft(
        FieldDraftV3(field_id="field-1"), expected_version=None, operation_id="create-field-1"
    )
    second = make_service(store.path).save_field_draft(
        FieldDraftV3(field_id="field-2"), expected_version=None, operation_id="create-field-2"
    )
    group = TaskExecutionGroupV4(
        id="same-id",
        field_id="field-1",
        member_task_ids=["task-a", "task-b"],
        execution_date=date(2026, 10, 20),
        duration_minutes=30,
        confirmed_at=datetime(2026, 10, 18, tzinfo=UTC),
    )
    store.save_task_execution_group(
        group,
        expected_task_version=first.task_version,
        operation_id="save-group-field-1",
    )

    with pytest.raises(ValueError, match="其他地块"):
        store.save_task_execution_group(
            group.model_copy(update={"field_id": "field-2"}),
            expected_task_version=second.task_version,
            operation_id="save-group-field-2",
        )
    assert [item.id for item in store.list_task_execution_groups("field-1")] == ["same-id"]
    assert store.list_task_execution_groups("field-2") == []


def test_reconfirmed_group_reserves_new_slot_after_member_completion(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service, issue = create_open_issue(service, state)
    members = save_outing_candidates(service, service.get_field("field-1"), issue.id)
    state = service.get_field("field-1")
    service.save_task_execution_group(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=list(members),
        execution_date=date(2026, 10, 20),
        duration_minutes=35,
        expected_task_version=state.task_version,
        operation_id="confirm-outing",
    )
    preview = service.preview_plan("field-1", operation_id="preview-outing")
    state = service.get_field("field-1")
    service.adopt_plan(
        preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=state.latest_plan_id,
        operation_id="adopt-outing",
    )
    after_visit = WorkbenchServiceV3(
        WorkbenchStoreV3(service.store.path),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 20),
        now=lambda: datetime(2026, 10, 20, 4, tzinfo=UTC),
    )
    state = after_visit.get_field("field-1")
    after_visit.record_task_result(
        "field-1",
        members[0],
        {"现场情况": "normal"},
        completed=True,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="complete-first-member",
    )

    state = after_visit.get_field("field-1")
    after_visit.save_task_series(
        TaskSeriesV4(
            id="outing-inspection-extra",
            field_id="field-1",
            template={
                "id": "outing-inspection-extra-template",
                "title": "补充巡田",
                "task_type": "inspection",
                "category": "onsite",
                "purpose": "确认另一处田间情况",
                "result_fields": ["现场情况"],
                "completion_standard": "记录本次检查结果",
            },
            recurrence="once",
            anchor_date=date(2026, 10, 21),
            duration_minutes=20,
            earliest_date=date(2026, 10, 21),
            due_date=date(2026, 10, 21),
        ),
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-extra-inspection",
    )
    state = after_visit.get_field("field-1")
    after_visit.save_task_series(
        TaskSeriesV4(
            id="offsite-follow-up",
            field_id="field-1",
            template={
                "id": "offsite-follow-up-template",
                "title": "整理巡田记录",
                "task_type": "custom",
                "category": "offsite",
                "purpose": "汇总巡田记录",
            },
            recurrence="once",
            anchor_date=date(2026, 10, 21),
            duration_minutes=20,
            earliest_date=date(2026, 10, 21),
            due_date=date(2026, 10, 21),
            conditions_confirmed=True,
        ),
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-offsite-task",
    )
    state = after_visit.get_field("field-1")
    after_visit.save_task_execution_group(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=[members[1], "outing-inspection-extra:000001"],
        execution_date=date(2026, 10, 21),
        duration_minutes=45,
        expected_task_version=state.task_version,
        operation_id="reconfirm-outing-after-completion",
    )

    changed = after_visit.preview_plan("field-1", operation_id="preview-changed-outing")
    items = {item.task_id: item for item in changed.version.items}
    grouped = [items[members[1]], items["outing-inspection-extra:000001"]]
    other = items["offsite-follow-up:000001"]
    assert all(item.schedule_status == "scheduled" for item in grouped), (
        after_visit.list_task_execution_groups("field-1")[0].model_dump(),
        [(item.task_id, item.schedule_status, item.reason_code, item.reason) for item in grouped],
    )
    assert grouped[0].start_at == grouped[1].start_at
    assert other.schedule_status == "scheduled"
    assert other.end_at <= grouped[0].start_at or other.start_at >= grouped[0].end_at


def test_locked_group_slot_does_not_override_current_workability(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service, issue = create_open_issue(service, state)
    members = save_outing_candidates(service, service.get_field("field-1"), issue.id)
    state = service.get_field("field-1")
    service.save_task_execution_group(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=list(members),
        execution_date=date(2026, 10, 20),
        duration_minutes=35,
        expected_task_version=state.task_version,
        operation_id="confirm-outing",
    )
    preview = service.preview_plan("field-1", operation_id="preview-outing")
    state = service.get_field("field-1")
    service.adopt_plan(
        preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=state.latest_plan_id,
        operation_id="adopt-outing",
    )
    after_visit = WorkbenchServiceV3(
        WorkbenchStoreV3(service.store.path),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 20),
        now=lambda: datetime(2026, 10, 20, 4, tzinfo=UTC),
    )
    state = after_visit.get_field("field-1")
    after_visit.record_task_result(
        "field-1",
        members[0],
        {"现场情况": "normal"},
        completed=True,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="complete-first-member",
    )
    state = after_visit.get_field("field-1")
    updated_days = [
        day.model_copy(update={"site_workable": None}) if day.day == date(2026, 10, 20) else day
        for day in state.draft.days
    ]
    after_visit.save_field_draft(
        state.draft.model_copy(update={"days": updated_days}),
        expected_version=state.draft_version,
        operation_id="clear-site-workability",
    )

    updated = after_visit.preview_plan("field-1", operation_id="preview-unknown-workability")
    items = {item.task_id: item for item in updated.version.items}
    assert items[members[0]].completed
    assert items[members[1]].schedule_status == "pending"
    assert items[members[1]].reason_code == "execution_group_workability_pending"


def test_reconfirmed_group_with_new_member_cannot_reuse_completed_past_slot(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service, issue = create_open_issue(service, state)
    members = save_outing_candidates(service, service.get_field("field-1"), issue.id)
    state = service.get_field("field-1")
    service.save_task_execution_group(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=list(members),
        execution_date=date(2026, 10, 20),
        duration_minutes=35,
        expected_task_version=state.task_version,
        operation_id="confirm-outing",
    )
    preview = service.preview_plan("field-1", operation_id="preview-outing")
    state = service.get_field("field-1")
    service.adopt_plan(
        preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=state.latest_plan_id,
        operation_id="adopt-outing",
    )
    after_visit = WorkbenchServiceV3(
        WorkbenchStoreV3(service.store.path),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 20),
        now=lambda: datetime(2026, 10, 20, 4, tzinfo=UTC),
    )
    state = after_visit.get_field("field-1")
    after_visit.record_task_result(
        "field-1",
        members[0],
        {"现场情况": "normal"},
        completed=True,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="complete-first-member",
    )
    state = after_visit.get_field("field-1")
    after_visit.save_task_series(
        TaskSeriesV4(
            id="outing-inspection-extra",
            field_id="field-1",
            template={
                "id": "outing-inspection-extra-template",
                "title": "补充巡田",
                "task_type": "inspection",
                "category": "onsite",
                "purpose": "补充确认田间情况",
                "result_fields": ["现场情况"],
                "completion_standard": "记录本次检查结果",
            },
            recurrence="once",
            anchor_date=date(2026, 10, 20),
            duration_minutes=20,
            earliest_date=date(2026, 10, 20),
            due_date=date(2026, 10, 20),
        ),
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-extra-inspection",
    )
    state = after_visit.get_field("field-1")
    after_visit.save_task_execution_group(
        field_id="field-1",
        group_id="outing-1",
        member_task_ids=[members[1], "outing-inspection-extra:000001"],
        execution_date=date(2026, 10, 20),
        duration_minutes=35,
        expected_task_version=state.task_version,
        operation_id="reconfirm-outing-new-members",
    )

    changed = after_visit.preview_plan("field-1", operation_id="preview-new-members")
    items = {item.task_id: item for item in changed.version.items}
    assert items[members[0]].completed
    assert items[members[1]].schedule_status == "unscheduled"
    assert items["outing-inspection-extra:000001"].schedule_status == "unscheduled"
    assert all(items[task_id].start_at is None for task_id in members[1:])


def test_v4_task_edit_retry_recovers_the_same_preview_and_instance_version(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    series = TaskSeriesV4(
        id="scout-rounds",
        field_id="field-1",
        template={
            "id": "inspection-v1",
            "title": "周期巡田",
            "task_type": "inspection",
            "category": "onsite",
            "purpose": "检查现场变化",
            "steps": ["记录检查情况"],
            "result_fields": ["苗情"],
            "completion_standard": "保存检查结果",
        },
        recurrence="dates",
        anchor_date=date(2026, 10, 19),
        selected_dates=[date(2026, 10, 19), date(2026, 10, 21)],
        duration_minutes=20,
    )
    service.save_task_series(
        series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-series",
    )
    initial = service.preview_plan("field-1", operation_id="preview-series")
    adopted = service.adopt_plan(
        initial.id,
        expected_fact_version=initial.expected_fact_version,
        expected_plan_id=initial.expected_plan_id,
        operation_id="adopt-series",
    )
    task_id = next(
        item.task_id for item in adopted.version.items if item.task_id.startswith("scout-rounds:")
    )
    before = service.get_field("field-1")

    def edit(target: WorkbenchServiceV3):
        return target.edit_task(
            "field-1",
            task_id,
            preferred_date=date(2026, 10, 20),
            duration_minutes=25,
            priority=1,
            expected_draft_version=before.draft_version,
            expected_fact_version=before.fact_version,
            expected_task_version=before.task_version,
            operation_id="edit-v4-task",
        )

    first = edit(service)
    after_first = service.get_field("field-1")
    retry = edit(make_service(tmp_path / "workbench.sqlite3"))

    assert retry.id == first.id
    assert service.get_field("field-1").task_version == after_first.task_version
    assert service.store.get_task_instance("field-1", task_id).occurrence_date == date(2026, 10, 20)
    assert len(service.plan_history("field-1")) == 1


def test_task_execution_results_and_completion_are_saved_as_separate_events(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite3"
    service = make_service(database)
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    series = TaskSeriesV4(
        id="record-review",
        field_id="field-1",
        template={
            "id": "custom-v1",
            "title": "整理现场记录",
            "task_type": "custom",
            "category": "offsite",
            "purpose": "整理已完成的巡田记录",
            "result_fields": ["记录完整性"],
            "completion_standard": "记录检查结果并保存整理说明",
        },
        recurrence="once",
        anchor_date=date(2026, 10, 19),
        duration_minutes=20,
    )
    service.save_task_series(
        series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-series",
    )
    preview = service.preview_plan("field-1", operation_id="preview-task")
    service.adopt_plan(
        preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=None,
        operation_id="adopt-task",
    )
    later = WorkbenchServiceV3(
        WorkbenchStoreV3(database),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 19),
        now=lambda: datetime(2026, 10, 19, 2, tzinfo=UTC),
    )

    saved = later.record_task_result(
        "field-1",
        "record-review:000001",
        {"记录完整性": "normal"},
        notes="检查表已归档",
        completed=True,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-task-result",
    )
    restored_events = make_service(database).list_events("field-1")

    assert len(saved.events) == 2
    assert [event.feedback.kind for event in restored_events] == ["task_result", "completed"]
    assert restored_events[0].feedback.result_values == {"记录完整性": "normal"}
    assert restored_events[0].feedback.result_notes == "检查表已归档"


def test_completed_task_instance_remains_valid_after_plan_week_moves(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite3"
    service = make_service(database)
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    series = TaskSeriesV4(
        id="one-time-review",
        field_id="field-1",
        template={
            "id": "custom-v1",
            "title": "整理巡田记录",
            "category": "offsite",
            "purpose": "归档当天观察",
            "result_fields": ["归档状态"],
            "completion_standard": "记录已归档",
        },
        recurrence="dates",
        anchor_date=date(2026, 10, 19),
        selected_dates=[date(2026, 10, 19), date(2026, 10, 27)],
        duration_minutes=20,
    )
    service.save_task_series(
        series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-cross-week-series",
    )
    first_preview = service.preview_plan("field-1", operation_id="preview-first-week")
    first = next(item for item in first_preview.version.items if item.task_id.endswith(":000001"))
    assert first.start_at is not None
    service.adopt_plan(
        first_preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=None,
        operation_id="adopt-first-week",
    )
    after_work = WorkbenchServiceV3(
        WorkbenchStoreV3(database),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 19),
        now=lambda: datetime(2026, 10, 19, 3, tzinfo=UTC),
    )
    after_work.record_task_result(
        "field-1",
        "one-time-review:000001",
        {"归档状态": "normal"},
        completed=True,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="complete-first-occurrence",
    )
    latest = after_work.get_field("field-1")
    next_start = date(2026, 10, 26)
    shifted_draft = latest.draft.model_copy(
        update={
            "start_date": next_start,
            "days": [
                DayWindow(
                    day=next_start + timedelta(days=offset),
                    site_workable=True,
                    weather_workable=True,
                    resource_minutes=120,
                    intervals=[TimeWindow(start="09:00", end="11:00")],
                )
                for offset in range(7)
            ],
        }
    )
    shifted = after_work.save_field_draft(
        shifted_draft,
        expected_version=latest.draft_version,
        operation_id="move-plan-week",
    )

    next_preview = after_work.preview_plan("field-1", operation_id="preview-next-week")
    completed = next(
        item for item in next_preview.version.items if item.task_id == "one-time-review:000001"
    )
    following_week = next(
        item for item in next_preview.version.items if item.task_id == "one-time-review:000002"
    )

    assert shifted.draft.start_date == date(2026, 10, 26)
    assert completed.completed
    assert completed.start_at == first.start_at
    assert following_week.start_at is not None


def test_period_edit_revises_open_occurrences_without_deleting_history(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    series = TaskSeriesV4(
        id="revisable-rounds",
        field_id="field-1",
        template={
            "id": "inspection-v1",
            "title": "周期巡田",
            "task_type": "inspection",
            "category": "onsite",
            "purpose": "记录田间变化",
            "result_fields": ["现场情况"],
            "completion_standard": "保存本次观察结果",
        },
        recurrence="dates",
        anchor_date=date(2026, 10, 19),
        selected_dates=[date(2026, 10, 19), date(2026, 10, 21), date(2026, 10, 23)],
        duration_minutes=20,
    )
    service.save_task_series(
        series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-revisable-series",
    )
    before = service.preview_plan("field-1", operation_id="preview-before-edit")
    revised = series.model_copy(
        update={
            "selected_dates": [date(2026, 10, 19), date(2026, 10, 22)],
            "template": series.template.model_copy(update={"version": 2}),
        }
    )

    result = service.update_task_series(
        revised,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="revise-open-series",
    )
    preview = service.preview_plan("field-1", operation_id="preview-after-edit")
    instances = service.store.list_task_instances("field-1")
    edited = next(item for item in preview.version.items if item.task_id.endswith(":000002"))
    superseded = next(item for item in preview.version.items if item.task_id.endswith(":000003"))

    assert result.task_version == 2
    assert result.updated_instance_ids == ["revisable-rounds:000001", "revisable-rounds:000002"]
    assert result.superseded_instance_ids == ["revisable-rounds:000003"]
    assert before.id != preview.id
    assert edited.start_at is not None
    assert edited.earliest_date == date(2026, 10, 22)
    assert superseded.schedule_status == "not_generated"
    assert len(instances) == 3
    assert instances[-1].status == "superseded"


def test_issue_from_inspection_links_to_follow_up_and_closes_only_after_recheck(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite3"
    service = make_service(database)
    state = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    inspection_series = TaskSeriesV4(
        id="issue-inspection",
        field_id="field-1",
        template={
            "id": "inspection-v1",
            "title": "现场巡田",
            "task_type": "inspection",
            "category": "onsite",
            "purpose": "检查积水情况",
            "result_fields": ["积水情况"],
            "completion_standard": "记录本次检查结果",
        },
        recurrence="once",
        anchor_date=date(2026, 10, 19),
        duration_minutes=20,
    )
    service.save_task_series(
        inspection_series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="save-inspection-series",
    )
    first_preview = service.preview_plan("field-1", operation_id="preview-inspection")
    service.adopt_plan(
        first_preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=None,
        operation_id="adopt-inspection",
    )
    at_field = WorkbenchServiceV3(
        WorkbenchStoreV3(database),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 19),
        now=lambda: datetime(2026, 10, 19, 2, tzinfo=UTC),
    )
    at_field.record_task_result(
        "field-1",
        "issue-inspection:000001",
        {"积水情况": "issue"},
        notes="田块低洼处有积水",
        completed=True,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="record-water-issue",
    )
    issue = at_field.list_field_issues("field-1")[0]
    after_observation = at_field.get_field("field-1")
    follow_up = TaskSeriesV4(
        id="water-recheck",
        field_id="field-1",
        template={
            "id": "follow-up-v1",
            "title": "复查积水问题",
            "task_type": "follow_up",
            "category": "onsite",
            "related_issue_id": issue.id,
            "purpose": "确认积水问题处理后的现场情况",
            "result_fields": ["复查情况"],
            "completion_standard": "记录复查结果",
        },
        recurrence="once",
        anchor_date=date(2026, 10, 20),
        duration_minutes=20,
    )
    at_field.save_task_series(
        follow_up,
        expected_draft_version=after_observation.draft_version,
        expected_fact_version=after_observation.fact_version,
        operation_id="save-water-recheck",
    )
    recheck_preview = at_field.preview_plan("field-1", operation_id="preview-water-recheck")
    at_field.adopt_plan(
        recheck_preview.id,
        expected_fact_version=after_observation.fact_version,
        expected_plan_id=first_preview.version.id,
        operation_id="adopt-water-recheck",
    )
    after_recheck_plan = at_field.get_field("field-1")
    reviewer = WorkbenchServiceV3(
        WorkbenchStoreV3(database),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 20),
        now=lambda: datetime(2026, 10, 20, 2, tzinfo=UTC),
    )
    reviewer.record_task_result(
        "field-1",
        "water-recheck:000001",
        {"复查情况": "normal"},
        notes="本次复查未见积水",
        completed=True,
        resolve_issue_id=issue.id,
        expected_draft_version=after_recheck_plan.draft_version,
        expected_fact_version=after_recheck_plan.fact_version,
        operation_id="close-water-issue",
    )

    resolved = reviewer.list_field_issues("field-1")[0]
    history = reviewer.store.list_field_issue_events("field-1")
    source_event = next(
        event
        for event in reviewer.list_events("field-1")
        if event.feedback
        and event.feedback.kind == "task_result"
        and event.feedback.task_id == "issue-inspection:000001"
    )

    assert source_event.feedback.issue_ids == [issue.id]
    assert resolved.related_task_ids == ["issue-inspection:000001", "water-recheck:000001"]
    assert resolved.status == "resolved"
    assert any(event["type"] == "resolved" for event in history)


def test_confirmed_fact_is_saved_idempotently_before_any_plan_exists(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    event = FactEventInputV3(
        event_id="event-observation-1",
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "field": "ditch_connectivity",
            "value": "impaired",
        },
    )

    saved = service.commit_events(
        "field-1", [event], operation_id="feedback-1", expected_fact_version=0
    )
    replayed = service.commit_events(
        "field-1", [event], operation_id="feedback-1", expected_fact_version=0
    )

    assert saved.fact_version == 1
    assert replayed.replayed is True
    assert len(service.list_events("field-1")) == 1
    assert service.get_current_plan("field-1") is None


def test_plan_preview_must_be_adopted_and_stale_preview_cannot_overwrite_facts(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1")
    assert service.get_current_plan("field-1") is None

    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="event-water-1",
                feedback={
                    "kind": "observation",
                    "at": "2026-10-18T10:00:00+08:00",
                    "plot_id": "field-1",
                    "field": "standing_water",
                    "value": True,
                },
            )
        ],
        operation_id="feedback-water",
        expected_fact_version=0,
    )

    with pytest.raises(ValueError, match="预览已过期"):
        service.adopt_plan(
            preview.id,
            expected_fact_version=0,
            expected_plan_id=None,
            operation_id="adopt-stale",
        )
    assert len(service.list_events("field-1")) == 1
    assert service.get_current_plan("field-1") is None


def test_fact_survives_cancelled_preview_and_marks_the_old_plan_stale(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    first_preview = service.preview_plan("field-1")
    first = service.adopt_plan(
        first_preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    ).version
    event = FactEventInputV3(
        event_id="event-condition-1",
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "field": "standing_water",
            "value": True,
        },
    )
    service.commit_events(
        "field-1", [event], operation_id="feedback-condition", expected_fact_version=0
    )
    second_preview = service.preview_plan("field-1")
    service.cancel_preview(second_preview.id)

    refreshed = make_service(tmp_path / "workbench.sqlite3")
    state = refreshed.get_field("field-1")
    assert state.fact_version == 1
    assert state.plan_stale is True
    assert refreshed.get_current_plan("field-1").id == first.id
    assert len(refreshed.list_events("field-1")) == 1


def test_unchanged_recalculation_does_not_create_an_empty_plan_version(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    first_preview = service.preview_plan("field-1")
    first = service.adopt_plan(
        first_preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    ).version
    second_preview = service.preview_plan("field-1")
    result = service.adopt_plan(
        second_preview.id,
        expected_fact_version=0,
        expected_plan_id=first.id,
        operation_id="adopt-unchanged",
    )

    assert result.created_new_version is False
    assert result.version.id == first.id
    assert service.plan_history("field-1") == [first]


def test_unchanged_adoption_clears_waiting_for_update_state(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    created = service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    first_preview = service.preview_plan("field-1")
    first = service.adopt_plan(
        first_preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    ).version
    service.save_field_draft(
        created.draft.model_copy(update={"notes": "已核对天气查询点"}),
        expected_version=created.draft_version,
        operation_id="update-note",
    )
    assert service.get_field("field-1").plan_stale is True

    second_preview = service.preview_plan("field-1")
    adopted = service.adopt_plan(
        second_preview.id,
        expected_fact_version=0,
        expected_plan_id=first.id,
        operation_id="adopt-no-change",
    )

    assert adopted.created_new_version is False
    assert service.get_field("field-1").plan_stale is False
    assert service.plan_history("field-1") == [first]


def test_adoption_is_append_only_and_repeated_operation_returns_same_version(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1")

    adopted = service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    )
    replayed = service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-v1",
    )

    assert adopted.version.id == replayed.version.id
    assert replayed.replayed is True
    assert service.get_current_plan("field-1").id == adopted.version.id
    assert service.plan_history("field-1") == [adopted.version]


def test_weather_and_rule_adoption_records_are_separate_from_source_cards(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service.record_rule_adoption(
        "field-1", "C11", status="pending_review", reviewer="许继哲", notes="待复核"
    )
    adoption = service.list_rule_adoptions("field-1")[0]
    assert adoption.rule_id == "C11"
    assert adoption.status == "pending_review"
    assert load_rule_catalog().cards["C11"].runtime_enabled is False


def test_rule_adoption_status_shapes_evidence_before_planning(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    service.record_rule_adoption("field-1", "C23", status="rejected", reviewer="许继哲")
    service.record_rule_adoption("field-1", "C31", status="adopted", reviewer="许继哲")

    evaluation = service.evaluate_field("field-1")
    items = {item.task_id: item for item in evaluation.items}
    maintenance_rules = {evidence.rule_id for evidence in items["field-1-M"].evidence}
    inspection = {evidence.rule_id: evidence for evidence in items["field-1-I"].evidence}

    assert "C23" not in maintenance_rules
    assert inspection["C31"].status_label == "已采用（限用途）"
    assert inspection["C11"].status_label == "当前限用途依据（AI辅助复核已交付，正式采用待确认）"
    assert "暂缓" in inspection["C19"].status_label

    preview = service.preview_plan("field-1")
    assert preview.version.rule_adoption_ids
    assert any("暂缓规则不作为计划依据" in basis for basis in preview.version.calculation_basis)


def test_standing_water_adds_onsite_verification_basis_without_threshold(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1")
    assert not any("农技人员" in basis for basis in preview.version.calculation_basis)

    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="obs-standing-water",
                feedback={
                    "kind": "observation",
                    "at": "2026-10-18T10:00:00+08:00",
                    "plot_id": "field-1",
                    "field": "standing_water",
                    "value": True,
                },
            )
        ],
        operation_id="obs-water",
        expected_fact_version=0,
    )
    preview = service.preview_plan("field-1")
    assert any("农技人员" in basis for basis in preview.version.calculation_basis)


def test_growth_observation_events_validate_and_survive_reopen(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    with pytest.raises(ValueError, match="pest_signs"):
        FactEventInputV3(
            feedback={
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": "pest_signs",
                "value": "少量",
            }
        )
    events = [
        FactEventInputV3(
            event_id=f"obs-{field}",
            feedback={
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": field,
                "value": True,
            },
        )
        for field in ("pest_signs", "missing_seedlings", "crowding")
    ]
    service.commit_events("field-1", events, operation_id="obs-growth", expected_fact_version=0)

    reloaded = make_service(tmp_path / "workbench.sqlite3")
    assert [event.feedback.field for event in reloaded.list_events("field-1")] == [
        "pest_signs",
        "missing_seedlings",
        "crowding",
    ]
    evaluation = reloaded.evaluate_field("field-1")
    assert evaluation.fact_version == 1
    maintenance = next(item for item in evaluation.items if item.task_type == "maintenance")
    assert maintenance.necessity == "unknown"


def test_expired_weather_snapshot_makes_preview_stale_and_blocks_adoption(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite3"
    now = [datetime(2026, 10, 18, 8, tzinfo=UTC)]
    store = WorkbenchStoreV3(database)
    service = WorkbenchServiceV3(
        store,
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 18),
        now=lambda: now[0],
    )
    location = WeatherLocationV3(
        query="武汉市洪山区",
        display_name="湖北省 · 武汉市 · 洪山区",
        latitude=30.5,
        longitude=114.3,
        resolution="county",
    )
    saved_draft = draft().model_copy(update={"location": location})
    service.save_field_draft(
        saved_draft, expected_version=None, operation_id="create-with-location"
    )
    snapshot = WeatherSnapshotV3(
        field_id="field-1",
        location=location,
        requested_latitude=location.latitude,
        requested_longitude=location.longitude,
        grid_latitude=30.51,
        grid_longitude=114.31,
        fetched_at=now[0],
        coverage_start=date(2026, 10, 19),
        coverage_end=date(2026, 10, 25),
        units={},
        days=[WeatherDayV3(day=date(2026, 10, 19) + timedelta(days=i)) for i in range(7)],
        cache_expires_at=now[0] + timedelta(hours=1),
    )
    service.store_weather(snapshot)
    preview = service.preview_plan("field-1", operation_id="preview-with-weather")
    assert preview.version.weather_snapshot_id == snapshot.id

    now[0] += timedelta(hours=2)
    with pytest.raises(ValueError, match="天气快照已过期"):
        service.adopt_plan(
            preview.id,
            expected_fact_version=0,
            expected_plan_id=None,
            operation_id="adopt-expired-weather",
        )

    assert store.load_preview(preview.id).status == "stale"
    assert service.get_current_plan("field-1") is None


def test_forecast_rain_never_becomes_an_observed_fact(tmp_path: Path) -> None:
    database = tmp_path / "workbench.sqlite3"
    service = make_service(database)
    location = WeatherLocationV3(
        query="武汉市洪山区",
        display_name="湖北省 · 武汉市 · 洪山区",
        latitude=30.5,
        longitude=114.3,
        resolution="county",
    )
    service.save_field_draft(
        draft().model_copy(update={"location": location}),
        expected_version=None,
        operation_id="create-with-location",
    )
    service.store_weather(
        WeatherSnapshotV3(
            field_id="field-1",
            location=location,
            requested_latitude=location.latitude,
            requested_longitude=location.longitude,
            grid_latitude=30.51,
            grid_longitude=114.31,
            fetched_at=datetime(2026, 10, 18, 8, tzinfo=UTC),
            coverage_start=date(2026, 10, 19),
            coverage_end=date(2026, 10, 25),
            units={"precipitation_sum": "mm"},
            days=[
                WeatherDayV3(
                    day=date(2026, 10, 19) + timedelta(days=offset),
                    precipitation_sum=25.0,
                )
                for offset in range(7)
            ],
            cache_expires_at=datetime(2026, 10, 18, 20, tzinfo=UTC),
        )
    )

    state = service.get_field("field-1")
    assert state.draft.profile is not None
    assert state.draft.profile.actual_rain_observed is None

    evaluation = service.evaluate_field("field-1")
    rule_ids = {row.rule_id for item in evaluation.items for row in item.evidence}
    assert "C24" not in rule_ids


def test_adopting_another_field_plan_leaves_the_first_field_plan_untouched(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft("field-1"), expected_version=None, operation_id="create-1")
    preview_one = service.preview_plan("field-1", operation_id="preview-1")
    adopted_one = service.adopt_plan(
        preview_one.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-1",
    ).version

    service.save_field_draft(draft("field-2"), expected_version=None, operation_id="create-2")
    preview_two = service.preview_plan("field-2", operation_id="preview-2")
    service.adopt_plan(
        preview_two.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-2",
    )

    assert service.get_current_plan("field-1").id == adopted_one.id
    markdown = service.export_markdown("field-1")
    assert "单地块；尚未统筹其他地块时间" in markdown
    assert "不代表合并后可执行" in markdown


def test_duplicate_or_unknown_completion_event_is_rejected_without_new_version(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1", operation_id="preview")
    adopted = service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt",
    ).version
    inspection = next(item for item in adopted.items if item.task_type == "inspection")

    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="complete-1",
                feedback={
                    "kind": "completed",
                    "task_id": inspection.task_id,
                    "at": (inspection.end_at + timedelta(minutes=1)).isoformat(),
                },
            )
        ],
        operation_id="complete-1",
        expected_fact_version=0,
    )
    history_after_first = len(service.plan_history("field-1"))

    with pytest.raises(ValueError, match="已记录完成"):
        service.commit_events(
            "field-1",
            [
                FactEventInputV3(
                    event_id="complete-2",
                    feedback={
                        "kind": "completed",
                        "task_id": inspection.task_id,
                        "at": (inspection.end_at + timedelta(minutes=2)).isoformat(),
                    },
                )
            ],
            operation_id="complete-2",
            expected_fact_version=1,
        )
    assert len(service.plan_history("field-1")) == history_after_first


def test_exports_carry_mode_version_tasks_basis_and_reasons(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    preview = service.preview_plan("field-1", operation_id="preview")
    service.adopt_plan(
        preview.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt",
    )

    payload = json.loads(service.export_json("field-1"))
    assert payload["schema_version"] == 4
    assert payload["adopted_plan"] is not None
    assert payload["adopted_plan"]["id"] == preview.version.id
    assert payload["field"]["draft"]["mode"] == "real"
    assert payload["adopted_plan"]["plan"]["items"], "导出必须包含任务明细"
    assert payload["history"], "导出必须包含已采用版本历史"

    markdown = service.export_markdown("field-1")
    assert "开发验证，适用性待复核" in markdown
    assert "单地块；尚未统筹其他地块时间" in markdown
    assert "不代表合并后可执行" in markdown
    assert "## 七天安排" in markdown
    assert "依据：" in markdown
    assert "依据：依据：" not in markdown
    assert "任务：" in markdown
    assert "## 缺项和未排入原因" in markdown
    for item in payload["adopted_plan"]["plan"]["items"]:
        for evidence in item["evidence"]:
            assert f"{evidence['rule_id']}/{evidence['source_id']}" in markdown
            assert evidence["project_use_limit"] in markdown
            for page in evidence["pdf_pages"]:
                assert f"PDF第{page}页" in markdown
            if evidence["usage_role"] == "historical_reference":
                assert f"暂缓参考：{evidence['rule_id']}/{evidence['source_id']}" in markdown


def test_feedback_values_are_field_validated_before_any_fact_is_saved(
    tmp_path: Path,
) -> None:
    from nongshi.schema_v3 import FactEventInputV3

    with pytest.raises(ValueError, match="沟渠连通情况"):
        FactEventInputV3(
            feedback={
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": "ditch_connectivity",
                "value": "堵塞程度很严重",
            }
        )
    with pytest.raises(ValueError, match="叶片数不能小于零"):
        FactEventInputV3(
            feedback={
                "kind": "observation",
                "at": "2026-10-18T10:00:00+08:00",
                "plot_id": "field-1",
                "field": "leaf_count",
                "value": -1,
            }
        )


def test_correction_keeps_original_event_and_recomputes_from_corrected_fact(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    original = FactEventInputV3(
        event_id="water-observation-original",
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "field": "standing_water",
            "value": True,
        },
    )
    service.commit_events(
        "field-1", [original], operation_id="event-original", expected_fact_version=0
    )
    correction = FactEventInputV3(
        event_id="water-observation-correction",
        correction_of=original.event_id,
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:05:00+08:00",
            "plot_id": "field-1",
            "field": "standing_water",
            "value": False,
        },
    )
    service.commit_events(
        "field-1", [correction], operation_id="event-correction", expected_fact_version=1
    )

    events = service.list_events("field-1")
    assert [event.event_id for event in events] == [
        "water-observation-original",
        "water-observation-correction",
    ]
    assert service._active_events("field-1") == [events[1]]
    assert service.evaluate_field("field-1").fact_version == 2


def test_a_failed_preview_does_not_rollback_previously_committed_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    service.save_field_draft(draft(), expected_version=None, operation_id="create-field")
    event = FactEventInputV3(
        event_id="saved-before-calculation",
        feedback={
            "kind": "observation",
            "at": "2026-10-18T10:00:00+08:00",
            "plot_id": "field-1",
            "field": "standing_water",
            "value": True,
        },
    )
    service.commit_events(
        "field-1", [event], operation_id="commit-before-preview", expected_fact_version=0
    )

    def fail_evaluation(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated calculation failure")

    monkeypatch.setattr(service, "_evaluate_request", fail_evaluation)
    with pytest.raises(RuntimeError, match="simulated calculation failure"):
        service.preview_plan("field-1")

    assert service.get_field("field-1").fact_version == 1
    assert service.list_events("field-1")[0].event_id == event.event_id
    assert service.get_current_plan("field-1") is None


def test_fourth_field_keeps_its_draft_events_and_plan_independent(tmp_path: Path) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    for index in range(4):
        field_id = f"field-{index + 1}"
        service.save_field_draft(
            draft(field_id).model_copy(
                update={
                    "profile": draft(field_id).profile.model_copy(
                        update={"name": f"第{index + 1}块地"}
                    )
                }
            ),
            expected_version=None,
            operation_id=f"create-{field_id}",
        )
    fourth = service.get_field("field-4")
    assert fourth.draft.profile.name == "第4块地"
    assert service.get_field("field-1").draft.profile.name == "第1块地"
    assert len(service.list_fields(mode="real")) == 4


def test_completed_maintenance_remains_historical_when_a_new_need_is_confirmed(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path / "workbench.sqlite3")
    new_draft = draft().model_copy(
        update={
            "profile": draft().profile.model_copy(
                update={
                    "inspection_requested": False,
                    "maintenance_need_confirmed": True,
                    "maintenance_problem_confirmed": True,
                    "maintenance_purpose": "clear_existing_ditch",
                    "maintenance_duration_minutes": 60,
                }
            )
        }
    )
    service.save_field_draft(new_draft, expected_version=None, operation_id="create-field")
    first = service.preview_plan("field-1", operation_id="preview-maintenance-1")
    adopted_v1 = service.adopt_plan(
        first.id,
        expected_fact_version=0,
        expected_plan_id=None,
        operation_id="adopt-maintenance-1",
    ).version
    prior_task = next(item for item in adopted_v1.items if item.task_type == "maintenance")
    assert prior_task.start_at is not None
    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="maintenance-completed",
                feedback={
                    "kind": "completed",
                    "task_id": prior_task.task_id,
                    "at": (prior_task.end_at + timedelta(minutes=5)).isoformat(),
                },
            )
        ],
        operation_id="complete-maintenance",
        expected_fact_version=0,
    )
    after_completion = service.preview_plan("field-1", operation_id="preview-completed")
    adopted_v2 = service.adopt_plan(
        after_completion.id,
        expected_fact_version=1,
        expected_plan_id=adopted_v1.id,
        operation_id="adopt-completed",
    ).version
    assert next(item for item in adopted_v2.items if item.task_id == prior_task.task_id).completed
    service.commit_events(
        "field-1",
        [
            FactEventInputV3(
                event_id="maintenance-demand-again",
                feedback={
                    "kind": "observation",
                    "plot_id": "field-1",
                    "field": "maintenance_need_confirmed",
                    "value": True,
                    "at": (prior_task.end_at + timedelta(days=1)).isoformat(),
                },
            )
        ],
        operation_id="new-maintenance-need",
        expected_fact_version=1,
    )

    first_evaluation = service.evaluate_field("field-1")
    second_evaluation = service.evaluate_field("field-1")
    maintenance = [item for item in first_evaluation.items if item.task_type == "maintenance"]
    by_id = {item.task_id: item for item in maintenance}
    assert by_id[prior_task.task_id].completed is True
    assert len(by_id) == 2
    new_ids = set(by_id) - {prior_task.task_id}
    assert len(new_ids) == 1
    assert new_ids == {
        item.task_id
        for item in second_evaluation.items
        if item.task_type == "maintenance" and item.task_id != prior_task.task_id
    }


def test_copying_v1_plan_preserves_unknown_agronomic_inputs_and_source(
    tmp_path: Path,
) -> None:
    from nongshi.demo import make_demo_request
    from nongshi.planning import build_plan
    from nongshi.repository import PlanStore

    database = tmp_path / "legacy.sqlite3"
    legacy_store = PlanStore(database)
    original = build_plan(make_demo_request(date(2026, 10, 19)))
    legacy_store.save(original)

    service = make_service(database)
    copied = service.copy_legacy_plan(original.id, operation_id="copy-v1-once")

    assert copied.draft.input_source == "copied_legacy"
    assert copied.draft.source_plan_id == original.id
    assert copied.draft.profile is not None
    assert copied.draft.profile.crop is None
    assert copied.draft.profile.field_type is None
    assert copied.draft.days == []
    assert legacy_store.load(original.id) == original
