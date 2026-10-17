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

START = date(2026, 10, 19)


def request_v2(**field_overrides: object) -> PlanRequestV2:
    field_values: dict[str, object] = {
        "id": "A",
        "name": "模拟地块A",
        "province": "湖北",
        "province_confirmed": True,
        "stage": "苗期",
        "stage_confirmed": True,
        "field_type": "lowland_rice_stubble",
        "inspection_requested": True,
        "maintenance_need_confirmed": True,
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


def test_no_maintenance_need_is_not_generated_and_not_a_window_failure() -> None:
    plan = build_plan_v2(request_v2(maintenance_need_confirmed=False))
    maintenance = items(plan)["A-M"]

    assert maintenance.necessity == "no_need"
    assert maintenance.window_by_date == {}
    assert maintenance.schedule_status == "not_generated"


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
