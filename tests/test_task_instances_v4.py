from datetime import date

import pytest

from nongshi.tasks_v4 import TaskSeriesV4, expand_task_series


def series(**changes: object) -> TaskSeriesV4:
    values: dict[str, object] = {
        "id": "series-a",
        "field_id": "field-a",
        "template": {
            "id": "inspection-v1",
            "title": "周期巡田",
            "task_type": "inspection",
            "category": "onsite",
            "purpose": "记录本周现场情况",
            "steps": ["记录实际检查情况"],
            "result_fields": ["沟渠", "积水", "苗情"],
            "completion_standard": "保存本次实际检查结果",
            "follow_up": "仅按确认的问题提出后续事项",
            "source_kind": "user_defined",
        },
        "recurrence": "dates",
        "anchor_date": date(2026, 10, 27),
        "end_date": None,
        "selected_dates": [
            date(2026, 10, 27),
            date(2026, 10, 29),
            date(2026, 11, 1),
        ],
        "interval_days": None,
        "duration_minutes": 30,
        "priority": 2,
        "earliest_date": None,
        "due_date": None,
        "prerequisites": [],
    }
    values.update(changes)
    return TaskSeriesV4.model_validate(values)


def test_selected_dates_expand_into_independent_stable_instances() -> None:
    pattern = series()

    first = expand_task_series(pattern, date(2026, 10, 27))
    again = expand_task_series(pattern, date(2026, 10, 27))
    following_week = expand_task_series(pattern, date(2026, 10, 29))

    assert [item.occurrence_date for item in first] == pattern.selected_dates
    assert [item.id for item in first] == [item.id for item in again]
    assert first[1].id == following_week[0].id
    assert len({item.id for item in first}) == 3
    assert all(item.duration_minutes == 30 for item in first)
    assert first[0].template.steps == ["记录实际检查情况"]


def test_interval_dates_remain_anchored_when_schedule_window_moves() -> None:
    pattern = series(
        recurrence="interval",
        selected_dates=[],
        interval_days=2,
        end_date=date(2026, 11, 4),
    )

    first = expand_task_series(pattern, date(2026, 10, 27))
    overlapping = expand_task_series(pattern, date(2026, 10, 29))

    assert [item.occurrence_date for item in first] == [
        date(2026, 10, 27),
        date(2026, 10, 29),
        date(2026, 10, 31),
        date(2026, 11, 2),
    ]
    by_date = {item.occurrence_date: item.id for item in first}
    assert overlapping[0].occurrence_date == date(2026, 10, 29)
    assert overlapping[0].id == by_date[date(2026, 10, 29)]
    last_window = expand_task_series(pattern, date(2026, 11, 4))
    assert last_window[0].occurrence_date == date(2026, 11, 4)


def test_missing_duration_is_preserved_as_a_pending_instance() -> None:
    item = expand_task_series(series(duration_minutes=None), date(2026, 10, 27))[0]

    assert item.duration_minutes is None
    assert item.status == "pending"


@pytest.mark.parametrize(
    ("changes",),
    [
        ({"recurrence": "interval", "interval_days": 0},),
        ({"recurrence": "dates", "selected_dates": []},),
        ({"duration_minutes": 0},),
        ({"template": {"id": "inspection-v1", "title": "   "}},),
    ],
)
def test_invalid_task_series_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        series(**changes)
