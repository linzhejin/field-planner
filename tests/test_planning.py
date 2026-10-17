"""The offline demonstration must keep decisions traceable and schedules feasible."""

from datetime import UTC, date, datetime, timedelta

import pytest

from nongshi.planning import apply_feedback, build_plan
from nongshi.schema import DayCapacity, Feedback, PlanRequest, Plot, RuleCard, Task, WeatherSnapshot

START = date(2026, 10, 17)
NOW = datetime(2026, 10, 17, 8, tzinfo=UTC)


def demo_request() -> PlanRequest:
    return PlanRequest(
        start_date=START,
        demo_mode=True,
        plots=[
            Plot(id="p1", name="一号田", region="湖北", stage="苗期", stage_confirmed=True),
            Plot(
                id="p2",
                name="二号田",
                region="湖北",
                stage="苗期",
                stage_confirmed=True,
                observations={"ditch_condition": "unknown"},
            ),
            Plot(id="p3", name="三号田", region="湖北", stage="苗期", stage_confirmed=True),
        ],
        tasks=[
            Task(id="t1", plot_id="p1", kind="inspect_seedling", title="巡田看苗", hours=2),
            Task(id="t2", plot_id="p2", kind="inspect_ditch", title="检查沟渠", hours=1),
            Task(
                id="t3",
                plot_id="p2",
                kind="maintain_ditch",
                title="维护沟渠候选任务",
                hours=2,
                prerequisites=["t2"],
            ),
            Task(
                id="t4",
                plot_id="p3",
                kind="inspect_seedling",
                title="巡田看苗",
                hours=2,
                priority=2,
            ),
        ],
        days=[
            DayCapacity(day=START + timedelta(days=i), hours=3 if i < 4 else 0) for i in range(7)
        ],
        rules=[
            RuleCard(
                id="r-seed",
                kind="inspect_seedling",
                region="湖北",
                stage="苗期",
                source_title="工程演示设定",
                source_excerpt="仅用于测试巡田安排，不是农事指导。",
                is_demo=True,
            ),
            RuleCard(
                id="r-inspect",
                kind="inspect_ditch",
                region="湖北",
                stage="苗期",
                source_title="工程演示设定",
                source_excerpt="仅用于测试沟渠检查安排，不是农事指导。",
                is_demo=True,
            ),
            RuleCard(
                id="r-maintain",
                kind="maintain_ditch",
                region="湖北",
                stage="苗期",
                conditions={"ditch_condition": "blocked"},
                confirmed_observation_fields=["ditch_condition"],
                required_prior_task_kinds=["inspect_ditch"],
                source_title="工程演示设定",
                source_excerpt="仅用于测试确认堵塞后的流程，不是农事指导。",
                is_demo=True,
            ),
        ],
        weather=WeatherSnapshot(status="fixed_demo", source_label="固定模拟天气", issued_at=NOW),
    )


def items_by_id(plan):  # type: ignore[no-untyped-def]
    return {item.task_id: item for item in plan.items}


def test_unknown_ditch_stays_pending_and_other_tasks_respect_capacity() -> None:
    plan = build_plan(demo_request())
    items = items_by_id(plan)

    assert items["t3"].window_status == "pending"
    assert items["t3"].scheduled_date is None
    assert "沟渠情况" in items["t3"].reason
    assert items["t1"].scheduled_date == START
    assert items["t2"].scheduled_date == START
    assert items["t4"].scheduled_date == START + timedelta(days=1)
    assert sum(item.hours or 0 for item in plan.items if item.scheduled_date == START) == 3
    assert len(items) == len(demo_request().tasks)
    assert all(e.simulation for item in plan.items for e in item.evidence)


def test_missing_hours_and_unreviewed_rules_never_produce_real_schedule() -> None:
    request = demo_request()
    request.demo_mode = False
    request.tasks[0].hours = None

    plan = build_plan(request)
    items = items_by_id(plan)

    assert items["t1"].scheduled_date is None
    assert items["t1"].window_status == "pending"
    assert "工时" in items["t1"].reason
    assert all(item.scheduled_date is None for item in plan.items)


def test_weather_unavailable_blocks_weather_dependent_rule() -> None:
    request = demo_request()
    request.rules[0].weather_required = True
    request.weather = WeatherSnapshot(status="unavailable", source_label="天气不可用")

    item = items_by_id(build_plan(request))["t1"]

    assert item.window_status == "pending"
    assert item.scheduled_date is None
    assert "天气" in item.reason


def test_feedback_creates_versions_and_does_not_move_completed_work() -> None:
    first = build_plan(demo_request())
    completed = apply_feedback(first, Feedback(kind="completed", task_id="t2", at=NOW))
    observed = apply_feedback(
        completed,
        Feedback(
            kind="observation", plot_id="p2", field="ditch_condition", value="blocked", at=NOW
        ),
    )
    reduced = apply_feedback(
        observed,
        Feedback(kind="availability", day=START + timedelta(days=1), hours=1, at=NOW),
    )

    before = items_by_id(first)
    after = items_by_id(reduced)
    assert completed.parent_id == first.id
    assert observed.parent_id == completed.id
    assert reduced.parent_id == observed.id
    assert after["t2"].completed is True
    assert after["t2"].scheduled_date == before["t2"].scheduled_date
    assert after["t3"].window_status == "ready"
    assert after["t3"].scheduled_date is not None
    assert after["t4"].scheduled_date == START + timedelta(days=3)
    assert any("t4" in change for change in reduced.changes)
    assert items_by_id(first)["t3"].window_status == "pending"


def test_no_capacity_keeps_task_visible_with_conflict_reason() -> None:
    request = demo_request()
    for day in request.days:
        day.hours = 0

    item = items_by_id(build_plan(request))["t1"]

    assert item.window_status == "ready"
    assert item.scheduled_date is None
    assert "工时" in item.reason


def test_weather_snapshot_alone_does_not_approve_weather_dependent_rule() -> None:
    request = demo_request()
    request.rules[0].weather_required = True

    item = items_by_id(build_plan(request))["t1"]

    assert item.window_status == "pending"
    assert item.scheduled_date is None
    assert "天气条件" in item.reason


def test_reviewed_rule_requires_traceable_original_source() -> None:
    with pytest.raises(ValueError, match="发布日期|原文位置"):
        RuleCard(
            id="untraceable",
            kind="inspect_ditch",
            region="湖北",
            stage="苗期",
            source_title="某资料",
            source_excerpt="原文片段",
            reviewed=True,
        )


def test_reviewed_rule_exposes_source_and_only_matches_its_region() -> None:
    request = demo_request()
    request.demo_mode = False
    request.rules = [
        RuleCard(
            id="test-card",
            kind="inspect_seedling",
            region="湖北",
            stage="苗期",
            source_title="单元测试资料",
            source_excerpt="仅用于测试规则来源字段。",
            source_date=START,
            source_location="第 1 段",
            reviewed=True,
        )
    ]

    item = items_by_id(build_plan(request))["t1"]
    assert item.window_status == "ready"
    assert item.evidence[0].source_date == START
    assert item.evidence[0].region == "湖北"
    assert item.evidence[0].simulation is False

    request.plots[0].region = "湖南"
    assert items_by_id(build_plan(request))["t1"].window_status == "pending"


def test_completed_task_stays_locked_even_if_input_is_edited() -> None:
    first = build_plan(demo_request())
    completed = apply_feedback(first, Feedback(kind="completed", task_id="t2", at=NOW))
    edited = completed.request.model_copy(deep=True)
    edited.tasks[1].hours = 4

    with pytest.raises(ValueError, match="已完成任务不可修改"):
        build_plan(edited, previous=completed)

    removed = completed.request.model_copy(deep=True)
    removed.tasks = [task for task in removed.tasks if task.id != "t2"]
    removed.tasks[1].prerequisites = []
    with pytest.raises(ValueError, match="已完成任务不可删除"):
        build_plan(removed, previous=completed)


def test_completed_task_keeps_its_original_evidence() -> None:
    first = build_plan(demo_request())
    completed = apply_feedback(first, Feedback(kind="completed", task_id="t2", at=NOW))
    changed_rules = completed.request.model_copy(deep=True)
    changed_rules.rules = []

    newer = build_plan(changed_rules, previous=completed)

    assert items_by_id(newer)["t2"].evidence == items_by_id(completed)["t2"].evidence


def test_same_day_predecessor_is_numbered_before_dependent_task() -> None:
    request = demo_request()
    request.plots[1].observations["ditch_condition"] = "blocked"
    request.plots[1].observation_times["ditch_condition"] = NOW
    request.days[0].hours = 5
    request.tasks = [request.tasks[2], *request.tasks[:2], request.tasks[3]]

    items = items_by_id(build_plan(request))

    assert items["t2"].scheduled_date == START
    assert items["t3"].scheduled_date == START
    assert items["t2"].sequence < items["t3"].sequence


def test_maintenance_needs_recorded_observation_and_rule_predecessor() -> None:
    request = demo_request()
    request.plots[1].observations["ditch_condition"] = "blocked"
    request.rules[2].confirmed_observation_fields = ["ditch_condition"]
    request.rules[2].required_prior_task_kinds = ["inspect_ditch"]

    unconfirmed = items_by_id(build_plan(request))["t3"]
    assert unconfirmed.window_status == "pending"
    assert unconfirmed.scheduled_date is None
    assert "现场观察" in unconfirmed.reason

    request.plots[1].observation_times["ditch_condition"] = NOW
    request.tasks[2].prerequisites = []
    missing_check = items_by_id(build_plan(request))["t3"]
    assert missing_check.window_status == "pending"
    assert "前置任务" in missing_check.reason

    request.tasks[2].prerequisites = ["t2"]
    assert items_by_id(build_plan(request))["t3"].window_status == "ready"


def test_observation_feedback_records_time_and_unknown_clears_confirmation() -> None:
    first = build_plan(demo_request())
    observed = apply_feedback(
        first,
        Feedback(
            kind="observation", plot_id="p2", field="ditch_condition", value="blocked", at=NOW
        ),
    )
    assert observed.request.plots[1].observation_times["ditch_condition"] == NOW

    unknown = apply_feedback(
        observed,
        Feedback(
            kind="observation", plot_id="p2", field="ditch_condition", value="unknown", at=NOW
        ),
    )
    assert "ditch_condition" not in unknown.request.plots[1].observation_times
    assert items_by_id(unknown)["t3"].window_status == "pending"


def test_rule_inapplicability_has_explicit_blocked_reason_and_evidence() -> None:
    request = demo_request()
    request.plots[0].observations["field_access"] = "no"
    request.rules[0].inapplicable_when = {"field_access": "no"}

    item = items_by_id(build_plan(request))["t1"]
    assert item.window_status == "blocked"
    assert "不适用" in item.reason
    assert item.evidence[0].inapplicable_when == {"field_access": "no"}


def test_recorded_observation_cannot_make_task_ready_in_the_past() -> None:
    request = demo_request()
    request.plots[1].observations["ditch_condition"] = "blocked"
    request.plots[1].observation_times["ditch_condition"] = datetime(2026, 10, 21, 10, tzinfo=UTC)
    request.days[4].hours = 3
    request.days[5].hours = 3

    maintenance = items_by_id(build_plan(request))["t3"]

    assert maintenance.window_status == "ready"
    assert maintenance.scheduled_date >= date(2026, 10, 21)


def test_known_observations_and_feedback_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="沟渠情况"):
        Plot(
            id="bad",
            name="错误输入",
            region="湖北",
            observations={"ditch_condition": "probably"},
        )
    with pytest.raises(ValueError, match="田间可进入性"):
        Feedback(kind="observation", plot_id="p2", field="field_access", value="maybe", at=NOW)
    with pytest.raises(ValueError, match="未知"):
        Plot(
            id="bad-time",
            name="错误时间",
            region="湖北",
            observations={"ditch_condition": "unknown"},
            observation_times={"ditch_condition": NOW},
        )
    with pytest.raises(ValueError, match="timezone|时区"):
        Feedback(kind="completed", task_id="t1", at=datetime(2026, 10, 17, 10))


def test_feedback_never_reschedules_unfinished_work_before_observation_day() -> None:
    request = demo_request()
    request.days[4].hours = 3
    request.days[5].hours = 3
    first = build_plan(request)
    observed = apply_feedback(
        first,
        Feedback(
            kind="observation",
            plot_id="p2",
            field="ditch_condition",
            value="blocked",
            at=datetime(2026, 10, 21, 10, tzinfo=UTC),
        ),
    )

    for item in observed.items:
        if item.scheduled_date and not item.completed:
            assert item.scheduled_date >= date(2026, 10, 21)
    assert items_by_id(observed)["t3"].scheduled_date >= date(2026, 10, 21)


def test_completed_work_over_capacity_is_reported_without_moving_history() -> None:
    first = build_plan(demo_request())
    completed = apply_feedback(first, Feedback(kind="completed", task_id="t2", at=NOW))
    reduced = apply_feedback(
        completed,
        Feedback(kind="availability", day=START, hours=0, at=NOW),
    )

    assert items_by_id(reduced)["t2"].scheduled_date == START
    assert any(START.isoformat() in conflict and "1" in conflict for conflict in reduced.conflicts)


def test_feedback_after_seven_day_horizon_keeps_tasks_visible_without_backdating() -> None:
    first = build_plan(demo_request())
    later = apply_feedback(
        first,
        Feedback(
            kind="observation",
            plot_id="p2",
            field="ditch_condition",
            value="blocked",
            at=datetime(2026, 10, 24, 8, tzinfo=UTC),
        ),
    )

    assert all(item.scheduled_date is None for item in later.items if not item.completed)
    assert all("新一期" in item.reason for item in later.items if item.window_status == "ready")

    regenerated = build_plan(later.request, previous=later)
    assert all(item.scheduled_date is None for item in regenerated.items if not item.completed)
    assert regenerated.not_before == later.not_before
