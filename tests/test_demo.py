"""The bundled case is visibly simulated and supports the agreed walkthrough."""

from datetime import date, timedelta

from nongshi.demo import make_demo_request
from nongshi.planning import build_plan


def test_three_plot_case_has_unknown_ditch_and_fixed_weather() -> None:
    start = date(2026, 10, 17)
    request = make_demo_request(start)
    plan = build_plan(request)
    items = {item.task_id: item for item in plan.items}

    assert request.demo_mode is True
    assert len(request.plots) == 3
    assert len(request.days) == 7
    assert request.days[-1].day == start + timedelta(days=6)
    assert request.weather.status == "fixed_demo"
    assert all(rule.is_demo and not rule.reviewed for rule in request.rules)
    assert items["p2-ditch-maintain"].window_status == "pending"
    assert "沟渠情况" in items["p2-ditch-maintain"].reason
    assert any(item.scheduled_date for item in plan.items)


def test_demo_field_access_controls_field_tasks() -> None:
    request = make_demo_request(date(2026, 10, 17))
    request.plots[0].observations["field_access"] = "no"

    item = next(item for item in build_plan(request).items if item.task_id == "p1-seedling")

    assert item.window_status == "blocked"
    assert item.scheduled_date is None
    assert "田间可进入性" in item.reason
