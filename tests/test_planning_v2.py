"""Acceptance tests for the rule-backed, minute-level prototype."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from nongshi.planning_v2 import apply_feedback_v2, build_plan_v2
from nongshi.rules import load_rule_catalog
from nongshi.schema_v2 import (
    LOCAL_ZONE,
    DayWindow,
    FeedbackV2,
    FieldProfile,
    PlanRequestV2,
    TimeWindow,
)
from nongshi.tasks_v4 import TaskSeriesV4, expand_task_series

START = date(2026, 10, 19)


def request_v2(**field_overrides: object) -> PlanRequestV2:
    field_values: dict[str, object] = {
        "id": "A",
        "name": "模拟地块A",
        "province": "湖北",
        "province_confirmed": True,
        "crop": "油菜",
        "crop_confirmed": True,
        "stage": "苗期",
        "stage_confirmed": True,
        "field_type": "lowland_rice_stubble",
        "inspection_requested": True,
        "maintenance_need_confirmed": True,
        "maintenance_problem_confirmed": True,
        "maintenance_purpose": "clear_existing_ditch",
        "moisture": "wet",
        "ditch_connectivity": "impaired",
        "standing_water": False,
        "inspection_duration_minutes": 30,
        "maintenance_duration_minutes": 90,
        "inspection_due_date": START,
        "maintenance_due_date": START,
        "observed_at": datetime(2026, 10, 18, 16, tzinfo=UTC),
        **field_overrides,
    }
    return PlanRequestV2(
        start_date=START,
        plots=[FieldProfile(**field_values)],
        days=[
            DayWindow(
                day=START + timedelta(days=offset),
                site_workable=True,
                weather_workable=True,
                resource_minutes=120,
                intervals=[TimeWindow(start=time(9), end=time(11))],
            )
            for offset in range(7)
        ],
        **load_rule_catalog().plan_metadata(),
        adoption_profile="development_validation",
        synthetic=True,
    )


def items(plan):  # type: ignore[no-untyped-def]
    return {item.task_id: item for item in plan.items}


def test_new_plan_snapshots_are_v4_and_legacy_snapshots_remain_readable() -> None:
    plan = build_plan_v2(request_v2())
    legacy = plan.model_copy(update={"schema_version": 2})
    restored_legacy = type(plan).model_validate(legacy.model_dump(mode="json"))

    assert plan.schema_version == 4
    assert plan.request.schema_version == 4
    assert restored_legacy.schema_version == 2


def test_unknown_observations_do_not_block_requested_inspection() -> None:
    plan = build_plan_v2(
        request_v2(
            leaf_count=None,
            moisture=None,
            ditch_connectivity=None,
            standing_water=None,
            maintenance_need_confirmed=None,
        )
    )

    inspection = items(plan)["A-I"]
    maintenance = items(plan)["A-M"]
    assert inspection.necessity == "required"
    assert inspection.window_by_date[START] == "available"
    assert inspection.schedule_status == "scheduled"
    assert maintenance.necessity == "unknown"
    assert maintenance.schedule_status == "pending"
    assert "maintenance_need_confirmed" in maintenance.missing_fields


def test_custom_offsite_task_uses_time_without_requiring_field_workability() -> None:
    series = TaskSeriesV4(
        id="series-office",
        field_id="A",
        template={
            "id": "custom-v1",
            "title": "整理巡田记录",
            "task_type": "custom",
            "category": "offsite",
            "purpose": "整理已经获得的现场记录",
            "completion_standard": "保存整理后的记录",
        },
        recurrence="once",
        anchor_date=START,
        duration_minutes=30,
        earliest_date=START,
        due_date=START,
    )
    instance = expand_task_series(series, START)[0]
    request = request_v2(maintenance_need_confirmed=False)
    request.task_instances_v4 = [instance]
    for day in request.days:
        day.site_workable = None
        day.weather_workable = None

    planned = items(build_plan_v2(request))

    assert planned["A-I"].schedule_status == "pending"
    assert planned[instance.id].schedule_status == "scheduled"
    assert planned[instance.id].category == "offsite"
    assert "现场与天气均已确认" not in planned[instance.id].reason
    assert planned[instance.id].template_snapshot["purpose"] == "整理已经获得的现场记录"


def test_custom_task_missing_duration_remains_visible_but_unplanned() -> None:
    series = TaskSeriesV4(
        id="series-draft",
        field_id="A",
        template={"id": "custom-v1", "title": "用户自定农事", "category": "offsite"},
        recurrence="once",
        anchor_date=START,
        duration_minutes=None,
    )
    instance = expand_task_series(series, START)[0]

    request = request_v2()
    request.task_instances_v4 = [instance]
    planned = items(build_plan_v2(request))

    assert planned[instance.id].schedule_status == "pending"
    assert planned[instance.id].reason_code == "missing_duration"
    assert "duration_minutes" in planned[instance.id].missing_fields


def test_custom_task_missing_purpose_or_completion_standard_is_not_scheduled() -> None:
    series = TaskSeriesV4(
        id="series-incomplete-details",
        field_id="A",
        template={"id": "custom-v1", "title": "待补充信息的农事", "category": "offsite"},
        recurrence="once",
        anchor_date=START,
        duration_minutes=30,
        earliest_date=START,
        due_date=START,
    )
    instance = expand_task_series(series, START)[0]
    request = request_v2(inspection_requested=False, maintenance_need_confirmed=False)
    request.task_instances_v4 = [instance]

    planned = items(build_plan_v2(request))

    assert planned[instance.id].schedule_status == "pending"
    assert planned[instance.id].reason_code == "task_details_pending"
    assert {"purpose", "completion_standard"} <= set(planned[instance.id].missing_fields)


@pytest.mark.parametrize(
    ("preference", "different_days"),
    [("early", False), ("balanced", True), ("concentrated", False)],
)
def test_schedule_preferences_change_distribution_without_losing_feasible_tasks(
    preference: str, different_days: bool
) -> None:
    task_instances = []
    for index in range(2):
        series = TaskSeriesV4(
            id=f"preference-task-{index}",
            field_id="A",
            template={
                "id": "custom-v1",
                "title": f"现场任务{index + 1}",
                "category": "onsite",
                "purpose": "完成用户确认的现场事项",
                "completion_standard": "保存本次结果",
            },
            recurrence="once",
            anchor_date=START,
            duration_minutes=60,
            earliest_date=START,
            due_date=START + timedelta(days=6),
        )
        task_instances.extend(expand_task_series(series, START))

    request = request_v2(inspection_requested=False, maintenance_need_confirmed=False)
    request.scheduling_preference = preference  # type: ignore[assignment]
    request.task_instances_v4 = task_instances
    planned = items(build_plan_v2(request))
    scheduled = [
        item
        for item in planned.values()
        if item.task_id.startswith("preference-task-") and item.start_at is not None
    ]

    assert len(scheduled) == 2
    assert len({item.start_at.date() for item in scheduled}) == (2 if different_days else 1)


def test_recurring_maintenance_keeps_existing_business_confirmation_gates() -> None:
    series = TaskSeriesV4(
        id="ditch-maintenance-rounds",
        field_id="A",
        template={
            "id": "maintenance-v1",
            "title": "沟渠维护",
            "task_type": "maintenance",
            "category": "onsite",
            "purpose": "清理既有沟渠",
            "completion_standard": "记录实际清理结果",
        },
        recurrence="dates",
        anchor_date=START,
        selected_dates=[START],
        duration_minutes=45,
    )
    instance = expand_task_series(series, START)[0]
    request = request_v2(maintenance_need_confirmed=None)
    request.task_instances_v4 = [instance]

    planned = items(build_plan_v2(request))

    assert planned["A-M"].schedule_status == "not_generated"
    assert planned[instance.id].schedule_status == "pending"
    assert "maintenance_need_confirmed" in planned[instance.id].missing_fields


def test_user_created_inspection_series_confirms_need_when_profile_is_blank() -> None:
    series = TaskSeriesV4(
        id="requested-inspection",
        field_id="A",
        template={
            "id": "inspection-v1",
            "title": "周期巡田",
            "task_type": "inspection",
            "category": "onsite",
            "purpose": "检查苗情与排水情况",
            "completion_standard": "实际检查并记录结果",
        },
        recurrence="once",
        anchor_date=START,
        duration_minutes=30,
    )
    instance = expand_task_series(series, START)[0]
    request = request_v2(inspection_requested=None, maintenance_need_confirmed=False)
    request.task_instances_v4 = [instance]
    for later_day in request.days[1:]:
        later_day.site_workable = None
        later_day.weather_workable = None

    planned = items(build_plan_v2(request))

    assert planned[instance.id].necessity == "required"
    assert planned[instance.id].schedule_status == "scheduled"
    assert "inspection_requested" not in planned[instance.id].missing_fields
    assert not any(":site_workability" in value for value in planned[instance.id].missing_fields)
    assert planned["A-I"].schedule_status == "not_generated"


def test_confirmed_hubei_province_with_suffix_remains_in_scope() -> None:
    plan = build_plan_v2(request_v2(province="湖北省"))
    inspection = items(plan)["A-I"]
    assert inspection.applicability == "matched"
    assert inspection.schedule_status == "scheduled"


def test_no_maintenance_need_is_not_generated_and_not_a_window_failure() -> None:
    plan = build_plan_v2(request_v2(maintenance_need_confirmed=False))
    maintenance = items(plan)["A-M"]

    assert maintenance.necessity == "no_need"
    assert maintenance.window_by_date == {}
    assert maintenance.schedule_status == "not_generated"


def test_standing_water_or_impaired_ditch_does_not_auto_confirm_a_problem() -> None:
    plan = build_plan_v2(
        request_v2(
            maintenance_problem_confirmed=None,
            ditch_connectivity="impaired",
            standing_water=True,
        )
    )
    maintenance = items(plan)["A-M"]

    assert maintenance.necessity == "unknown"
    assert maintenance.schedule_status == "pending"
    assert "maintenance_problem_confirmed" in maintenance.missing_fields

    clear_but_unconfirmed = items(
        build_plan_v2(
            request_v2(
                maintenance_problem_confirmed=None,
                ditch_connectivity="clear",
                standing_water=False,
            )
        )
    )["A-M"]
    assert clear_but_unconfirmed.necessity == "unknown"
    assert "maintenance_problem_confirmed" in clear_but_unconfirmed.missing_fields


def test_unknown_scope_and_unknown_daily_workability_remain_distinct() -> None:
    plan = build_plan_v2(request_v2(stage=None))
    inspection = items(plan)["A-I"]
    assert inspection.applicability == "pending"
    assert inspection.schedule_status == "pending"
    assert "stage" in inspection.missing_fields

    request = request_v2()
    request.days[0].site_workable = None
    daily_plan = build_plan_v2(request)
    inspection = items(daily_plan)["A-I"]
    assert inspection.window_by_date[START] == "pending"
    assert inspection.window_by_date[START + timedelta(days=1)] == "available"


def test_crop_must_be_confirmed_and_non_rapeseed_is_out_of_scope() -> None:
    missing = items(build_plan_v2(request_v2(crop=None, crop_confirmed=False)))["A-I"]
    assert missing.applicability == "pending"
    assert "crop" in missing.missing_fields

    other_crop = items(build_plan_v2(request_v2(crop="小麦", crop_confirmed=True)))["A-I"]
    assert other_crop.applicability == "out_of_scope"


def test_unknown_scope_does_not_erase_user_demand_and_out_of_scope_stays_unplanned() -> None:
    unknown = items(build_plan_v2(request_v2(stage=None)))
    assert unknown["A-I"].necessity == "required"
    assert unknown["A-I"].applicability == "pending"

    outside = items(build_plan_v2(request_v2(province="湖南")))
    assert outside["A-I"].necessity == "required"
    assert outside["A-I"].applicability == "out_of_scope"
    assert outside["A-I"].schedule_status == "not_generated"


def test_known_false_workability_dominates_unknown_without_losing_missing_item() -> None:
    request = request_v2()
    request.days[0].site_workable = False
    request.days[0].weather_workable = None

    inspection = items(build_plan_v2(request))["A-I"]

    assert inspection.window_by_date[START] == "unavailable"
    assert any(field.endswith(":weather_workability") for field in inspection.missing_fields)


def test_capacity_shortage_does_not_change_available_work_window() -> None:
    request = request_v2(inspection_duration_minutes=90, maintenance_need_confirmed=False)
    request.days[0].intervals = [TimeWindow(start=time(9), end=time(9, 30))]
    inspection = items(build_plan_v2(request))["A-I"]

    assert inspection.window_by_date[START] == "available"
    assert inspection.schedule_status == "unscheduled"
    assert inspection.reason_code == "time_slot"


def test_unconfirmed_daily_resource_keeps_schedule_pending_not_window_false() -> None:
    request = request_v2(maintenance_need_confirmed=False)
    for day in request.days:
        day.resource_minutes = None

    inspection = items(build_plan_v2(request))["A-I"]

    assert all(status == "available" for status in inspection.window_by_date.values())
    assert inspection.schedule_status == "pending"
    assert inspection.reason_code == "resource_pending"
    assert any(field.endswith(":resource_minutes") for field in inspection.missing_fields)


def test_minute_scheduler_places_contiguous_tasks_without_overlap_and_respects_predecessor() -> (
    None
):
    plan = build_plan_v2(
        request_v2(
            maintenance_prerequisites=["A-I"],
            inspection_priority=3,
            maintenance_priority=1,
        )
    )
    by_id = items(plan)

    assert by_id["A-I"].start_at == datetime(2026, 10, 19, 9, tzinfo=LOCAL_ZONE)
    assert by_id["A-M"].start_at == datetime(2026, 10, 19, 9, 30, tzinfo=LOCAL_ZONE)
    assert by_id["A-M"].end_at == datetime(2026, 10, 19, 11, tzinfo=LOCAL_ZONE)
    assert by_id["A-I"].end_at <= by_id["A-M"].start_at
    scheduled = sorted((i.start_at, i.end_at) for i in plan.items if i.start_at and i.end_at)
    assert all(left[1] <= right[0] for left, right in zip(scheduled, scheduled[1:], strict=False))


def test_new_confirmed_maintenance_issue_gets_a_new_id_after_prior_work_is_completed() -> None:
    first = build_plan_v2(request_v2(maintenance_due_date=None))
    completed_at = datetime(2026, 10, 19, 11, tzinfo=LOCAL_ZONE)
    completed = apply_feedback_v2(
        first,
        FeedbackV2(kind="completed", at=completed_at, task_id="A-M"),
    )
    request = request_v2(
        maintenance_due_date=None,
        maintenance_task_id="A-M-issue-2",
        completed_maintenance_task_ids=["A-M"],
        completed_at={"A-M": completed_at},
    )

    updated = build_plan_v2(request, previous=completed)
    by_id = items(updated)

    assert by_id["A-M"].completed is True
    assert by_id["A-M-issue-2"].completed is False
    assert by_id["A-M-issue-2"].schedule_status in {"scheduled", "unscheduled"}


def test_feedback_never_backdates_and_invalidates_only_affected_plot_day() -> None:
    first = build_plan_v2(request_v2())
    changed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="workability",
            at=datetime(2026, 10, 19, 9, 45, tzinfo=LOCAL_ZONE),
            plot_id="A",
            day=START,
            site_workable=False,
            weather_workable=None,
        ),
    )
    by_id = items(changed)
    assert by_id["A-I"].window_by_date[START] == "unavailable"
    assert by_id["A-M"].window_by_date[START] == "unavailable"
    assert by_id["A-M"].start_at is None or by_id["A-M"].start_at >= datetime(
        2026, 10, 19, 9, 45, tzinfo=LOCAL_ZONE
    )


def test_new_standing_water_requires_site_reconfirmation_before_reusing_old_slot() -> None:
    request = request_v2()
    other = request.plots[0].model_copy(deep=True)
    other.id = "B"
    other.name = "模拟地块B"
    other.maintenance_need_confirmed = False
    request.plots.append(other)
    request.days[0].resource_minutes = 240
    request.days[0].intervals = [TimeWindow(start=time(9), end=time(13))]
    first = build_plan_v2(request)
    before = items(first)
    assert before["A-I"].start_at is not None
    assert before["B-I"].start_at is not None

    changed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="observation",
            at=datetime(2026, 10, 19, 8, tzinfo=LOCAL_ZONE),
            plot_id="A",
            field="standing_water",
            value=True,
        ),
    )
    after = items(changed)
    assert after["A-I"].window_by_date[START] == "pending"
    assert after["A-I"].start_at is None
    assert changed.request.plots[0].workability_by_date[START].site is None
    assert changed.request.plots[0].workability_by_date[START].weather is True
    assert after["B-I"].start_at == before["B-I"].start_at


def test_new_rain_requires_weather_reconfirmation_without_erasing_site_confirmation() -> None:
    first = build_plan_v2(request_v2())
    changed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="observation",
            at=datetime(2026, 10, 19, 8, tzinfo=LOCAL_ZONE),
            plot_id="A",
            field="actual_rain_observed",
            value=True,
        ),
    )
    inspection = items(changed)["A-I"]
    assert inspection.window_by_date[START] == "pending"
    assert inspection.start_at is None
    assert changed.request.plots[0].workability_by_date[START].site is True
    assert changed.request.plots[0].workability_by_date[START].weather is None


@pytest.mark.parametrize(
    ("field", "value", "site", "weather"),
    [
        ("standing_water", True, False, True),
        ("actual_rain_observed", True, True, False),
    ],
)
def test_observation_keeps_known_unworkable_day(
    field: str, value: bool, site: bool, weather: bool
) -> None:
    request = request_v2()
    request.days[0].site_workable = site
    request.days[0].weather_workable = weather
    first = build_plan_v2(request)
    assert items(first)["A-I"].window_by_date[START] == "unavailable"

    changed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="observation",
            at=datetime(2026, 10, 19, 8, tzinfo=LOCAL_ZONE),
            plot_id="A",
            field=field,
            value=value,
        ),
    )
    assert items(changed)["A-I"].window_by_date[START] == "unavailable"


def test_changed_task_dates_disqualify_old_slot() -> None:
    first = build_plan_v2(request_v2())
    edited = first.request.model_copy(deep=True)
    edited.plots[0].inspection_earliest_date = START + timedelta(days=1)
    edited.plots[0].inspection_due_date = START + timedelta(days=1)
    changed = build_plan_v2(edited, previous=first)
    inspection = items(changed)["A-I"]
    assert inspection.start_at is not None
    assert inspection.start_at.date() == START + timedelta(days=1)


def test_feedback_second_rounds_next_slot_up_to_whole_minute() -> None:
    request = request_v2(maintenance_need_confirmed=False)
    first = build_plan_v2(request)
    changed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="resource",
            at=datetime(2026, 10, 19, 9, 0, 30, tzinfo=LOCAL_ZONE),
            day=START,
            intervals=[TimeWindow(start=time(9), end=time(11))],
            resource_minutes=120,
        ),
    )
    inspection = items(changed)["A-I"]
    assert inspection.start_at == datetime(2026, 10, 19, 9, 1, tzinfo=LOCAL_ZONE)
    assert inspection.end_at == datetime(2026, 10, 19, 9, 31, tzinfo=LOCAL_ZONE)


def test_weather_confirmation_records_source_and_snapshot_time_separately() -> None:
    first = build_plan_v2(request_v2())
    feedback_at = datetime(2026, 10, 19, 8, tzinfo=LOCAL_ZONE)
    changed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="workability",
            at=feedback_at,
            plot_id="A",
            day=START,
            site_workable=True,
            weather_workable=True,
        ),
    )
    confirmation = changed.request.plots[0].workability_by_date[START]
    assert confirmation.weather_source == "user_estimate"
    assert confirmation.weather_snapshot_at == feedback_at
    assert changed.request.plots[0].observed_at == first.request.plots[0].observed_at
    assert changed.feedback.at == feedback_at
    assert changed.request.days[0].weather_source == "synthetic"


def test_resource_feedback_changes_capacity_without_changing_workability() -> None:
    first = build_plan_v2(request_v2())
    changed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="resource",
            at=datetime(2026, 10, 19, 8, tzinfo=LOCAL_ZONE),
            day=START,
            intervals=[TimeWindow(start=time(9), end=time(9, 30))],
        ),
    )

    assert items(changed)["A-I"].window_by_date[START] == "available"
    assert items(changed)["A-M"].schedule_status == "unscheduled"


def test_completion_and_effect_observation_are_separate_records() -> None:
    first = build_plan_v2(request_v2())
    completed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="completed",
            at=datetime(2026, 10, 19, 9, 35, tzinfo=LOCAL_ZONE),
            task_id="A-M",
        ),
    )
    observed = apply_feedback_v2(
        completed,
        FeedbackV2(
            kind="observation",
            at=datetime(2026, 10, 19, 10, tzinfo=LOCAL_ZONE),
            plot_id="A",
            field="standing_water",
            value=True,
        ),
    )

    assert items(observed)["A-M"].completed is True
    assert observed.request.plots[0].standing_water is True
    assert items(observed)["A-M"].start_at == items(completed)["A-M"].start_at


def test_completed_task_keeps_original_slot_duration_and_evidence() -> None:
    first = build_plan_v2(request_v2())
    completed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="completed",
            task_id="A-M",
            at=datetime(2026, 10, 19, 11, tzinfo=LOCAL_ZONE),
        ),
    )
    edited = completed.request.model_copy(deep=True)
    edited.plots[0].maintenance_duration_minutes = 150
    edited_plan = build_plan_v2(edited, previous=completed)

    before = items(completed)["A-M"]
    after = items(edited_plan)["A-M"]
    assert after.completed is True
    assert after.duration_minutes == before.duration_minutes == 90
    assert after.start_at == before.start_at
    assert after.evidence == before.evidence


def test_completion_feedback_cannot_precede_planned_start() -> None:
    first = build_plan_v2(request_v2())
    with pytest.raises(ValueError, match="早于任务计划开始"):
        apply_feedback_v2(
            first,
            FeedbackV2(
                kind="completed",
                task_id="A-I",
                at=datetime(2026, 10, 19, 8, 59, tzinfo=LOCAL_ZONE),
            ),
        )


def test_intervals_merge_adjacent_and_reject_overlap() -> None:
    day = DayWindow(
        day=START,
        site_workable=True,
        weather_workable=True,
        intervals=[
            TimeWindow(start=time(9), end=time(10)),
            TimeWindow(start=time(10), end=time(11)),
        ],
    )
    assert len(day.intervals) == 1
    assert day.intervals[0].start == time(9)
    assert day.intervals[0].end == time(11)

    with pytest.raises(ValueError, match="重叠"):
        DayWindow(
            day=START,
            site_workable=True,
            weather_workable=True,
            intervals=[
                TimeWindow(start=time(9), end=time(10, 30)),
                TimeWindow(start=time(10), end=time(11)),
            ],
        )

    with pytest.raises(ValueError, match="精确到分钟"):
        TimeWindow(start=time(9, 0, 1), end=time(10))


def test_explicit_prerequisite_cycle_is_rejected() -> None:
    with pytest.raises(ValueError, match="形成循环"):
        request_v2(maintenance_prerequisites=["A-M"])


def test_growth_observations_are_stored_as_facts_without_driving_tasks() -> None:
    first = build_plan_v2(request_v2())
    changed = first
    for offset, field in enumerate(("pest_signs", "missing_seedlings", "crowding")):
        changed = apply_feedback_v2(
            changed,
            FeedbackV2(
                kind="observation",
                at=datetime(2026, 10, 19, 8, offset, tzinfo=LOCAL_ZONE),
                plot_id="A",
                field=field,
                value=True,
            ),
        )
    plot = changed.request.plots[0]
    assert plot.pest_signs is True
    assert plot.missing_seedlings is True
    assert plot.crowding is True
    assert plot.workability_by_date == {}
    assert items(changed)["A-M"].start_at == items(first)["A-M"].start_at


def test_unknown_field_type_or_leaf_age_does_not_block_requested_inspection() -> None:
    plan = build_plan_v2(
        request_v2(
            field_type=None,
            leaf_count=None,
            moisture=None,
            ditch_connectivity=None,
            standing_water=None,
        )
    )
    by_id = items(plan)

    inspection = by_id["A-I"]
    assert inspection.applicability == "matched"
    assert inspection.necessity == "required"
    assert inspection.schedule_status == "scheduled"
    assert {evidence.rule_id for evidence in inspection.evidence} == {"C11", "C31"}

    maintenance = by_id["A-M"]
    assert maintenance.applicability == "pending"
    assert "field_type" in maintenance.missing_fields


def test_completed_maintenance_never_rewrites_standing_water_fact() -> None:
    first = build_plan_v2(
        request_v2(
            standing_water=True,
            ditch_connectivity="impaired",
            maintenance_problem_confirmed=True,
            maintenance_need_confirmed=True,
        )
    )
    assert items(first)["A-M"].schedule_status == "scheduled"

    completed = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="completed",
            task_id="A-M",
            at=datetime(2026, 10, 19, 11, tzinfo=LOCAL_ZONE),
        ),
    )

    assert items(completed)["A-M"].completed is True
    assert completed.request.plots[0].standing_water is True


def test_confirmed_inspection_and_follow_up_group_uses_one_shared_time_slot() -> None:
    from nongshi.tasks_v4 import TaskExecutionGroupV4, TaskSeriesV4, expand_task_series

    inspection = expand_task_series(
        TaskSeriesV4(
            id="series-inspection",
            field_id="A",
            template={
                "id": "inspection-template",
                "title": "巡田检查",
                "task_type": "inspection",
                "category": "onsite",
                "purpose": "确认现场情况",
                "completion_standard": "记录巡田结果",
            },
            recurrence="once",
            anchor_date=START,
            duration_minutes=30,
            earliest_date=START,
            due_date=START,
        ),
        START,
    )[0]
    follow_up = expand_task_series(
        TaskSeriesV4(
            id="series-follow-up",
            field_id="A",
            template={
                "id": "follow-up-template",
                "title": "复查已记录的问题",
                "task_type": "follow_up",
                "category": "onsite",
                "related_issue_id": "issue-1",
                "purpose": "核对问题是否解决",
                "completion_standard": "记录复查结果",
            },
            recurrence="once",
            anchor_date=START,
            duration_minutes=20,
            earliest_date=START,
            due_date=START,
        ),
        START,
    )[0]
    request = request_v2(maintenance_need_confirmed=False)
    request.task_instances_v4 = [inspection, follow_up]
    request.days[0].resource_minutes = 40
    request.days[0].intervals = [TimeWindow(start=time(9), end=time(10))]
    request.execution_groups_v4 = [
        TaskExecutionGroupV4(
            id="outing-1",
            field_id="A",
            member_task_ids=[inspection.id, follow_up.id],
            execution_date=START,
            duration_minutes=40,
            member_signature="confirmed-members",
            confirmed_at=datetime(2026, 10, 18, 12, tzinfo=UTC),
        )
    ]

    plan_items = items(build_plan_v2(request))
    inspection_item = plan_items[inspection.id]
    follow_up_item = plan_items[follow_up.id]

    assert inspection_item.schedule_status == follow_up_item.schedule_status == "scheduled"
    assert inspection_item.start_at == follow_up_item.start_at
    assert inspection_item.end_at == follow_up_item.end_at
    assert inspection_item.execution_group_id == follow_up_item.execution_group_id == "outing-1"
    assert inspection_item.group_duration_minutes == follow_up_item.group_duration_minutes == 40
    assert inspection_item.duration_minutes == 30
    assert follow_up_item.duration_minutes == 20


def test_reconfirmed_group_keeps_its_feasible_old_start_when_total_duration_changes() -> None:
    from nongshi.tasks_v4 import TaskExecutionGroupV4

    inspection = expand_task_series(
        TaskSeriesV4(
            id="b-inspection",
            field_id="A",
            template={
                "id": "inspection-template",
                "title": "巡田检查",
                "task_type": "inspection",
                "category": "onsite",
                "purpose": "确认现场情况",
                "completion_standard": "记录巡田结果",
            },
            recurrence="once",
            anchor_date=START,
            duration_minutes=20,
        ),
        START,
    )[0]
    follow_up = expand_task_series(
        TaskSeriesV4(
            id="c-follow-up",
            field_id="A",
            template={
                "id": "follow-up-template",
                "title": "复查已记录的问题",
                "task_type": "follow_up",
                "category": "onsite",
                "related_issue_id": "issue-1",
                "purpose": "核对问题是否解决",
                "completion_standard": "记录复查结果",
            },
            recurrence="once",
            anchor_date=START,
            duration_minutes=30,
        ),
        START,
    )[0]
    office_task = expand_task_series(
        TaskSeriesV4(
            id="a-office-task",
            field_id="A",
            template={
                "id": "office-template",
                "title": "整理记录",
                "task_type": "custom",
                "category": "offsite",
                "purpose": "整理已有记录",
                "completion_standard": "保存整理结果",
            },
            recurrence="once",
            anchor_date=START,
            duration_minutes=15,
        ),
        START,
    )[0]
    group = TaskExecutionGroupV4(
        id="outing-keep-time",
        field_id="A",
        member_task_ids=[inspection.id, follow_up.id],
        execution_date=START,
        duration_minutes=35,
        member_signature="members-v1",
        confirmed_at=datetime(2026, 10, 18, 12, tzinfo=UTC),
    )
    request = request_v2(maintenance_need_confirmed=False)
    request.task_instances_v4 = [office_task, inspection, follow_up]
    request.execution_groups_v4 = [group]
    previous = build_plan_v2(request)
    previous_group_item = items(previous)[inspection.id]
    assert previous_group_item.start_at == datetime(2026, 10, 19, 9, 15, tzinfo=LOCAL_ZONE)

    revised_request = previous.request.model_copy(deep=True)
    revised_request.task_instances_v4 = [inspection, follow_up]
    revised_request.execution_groups_v4 = [
        group.model_copy(update={"duration_minutes": 45, "version": 2})
    ]
    revised = build_plan_v2(revised_request, previous=previous)
    revised_items = items(revised)

    assert revised_items[inspection.id].start_at == previous_group_item.start_at
    assert revised_items[inspection.id].end_at == datetime(2026, 10, 19, 10, tzinfo=LOCAL_ZONE)
    assert revised_items[follow_up.id].start_at == previous_group_item.start_at
