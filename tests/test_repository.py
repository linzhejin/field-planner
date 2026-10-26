"""Plan history must survive a process restart without rewriting prior versions."""

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from test_planning import demo_request
from test_planning_v2 import request_v2

from nongshi.planning import apply_feedback, build_plan
from nongshi.planning_v2 import apply_feedback_v2, build_plan_v2
from nongshi.repository import PlanStore, PlanStoreV2
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema import Feedback
from nongshi.schema_v2 import LOCAL_ZONE, FeedbackV2, PlanVersionV2


def test_plan_versions_survive_reopen_and_keep_old_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "plans.sqlite3"
    first = build_plan(demo_request())
    second = apply_feedback(
        first,
        Feedback(
            kind="observation",
            plot_id="p2",
            field="ditch_condition",
            value="blocked",
            at=datetime(2026, 10, 17, 9, tzinfo=UTC),
        ),
    )
    store = PlanStore(database)
    store.save(first)
    store.save(second)

    reopened = PlanStore(database)
    assert reopened.latest() == second
    assert reopened.load(first.id) == first
    assert [version.id for version in reopened.history(second.id)] == [first.id, second.id]
    assert reopened.load(first.id).request.plots[1].observations["ditch_condition"] == "unknown"


def test_store_rejects_version_with_missing_parent(tmp_path: Path) -> None:
    first = build_plan(demo_request())
    second = apply_feedback(
        first,
        Feedback(kind="completed", task_id="t2", at=datetime.now(UTC)),
    )
    store = PlanStore(tmp_path / "plans.sqlite3")

    try:
        store.save(second)
    except ValueError as error:
        assert "上一版本" in str(error)
    else:
        raise AssertionError("A version must not be saved without its parent")


def test_feedback_date_floor_survives_reload_and_regeneration(tmp_path: Path) -> None:
    store = PlanStore(tmp_path / "plans.sqlite3")
    first = build_plan(demo_request())
    later = apply_feedback(
        first,
        Feedback(
            kind="observation",
            plot_id="p2",
            field="ditch_condition",
            value="blocked",
            at=datetime(2026, 10, 21, 10, tzinfo=UTC),
        ),
    )
    store.save(first)
    store.save(later)

    reopened = PlanStore(tmp_path / "plans.sqlite3").latest()
    assert reopened is not None
    regenerated = build_plan(reopened.request, previous=reopened)
    assert regenerated.not_before == date(2026, 10, 21)
    assert all(
        item.scheduled_date >= regenerated.not_before
        for item in regenerated.items
        if item.scheduled_date and not item.completed
    )


def test_v2_history_persists_without_rewriting_legacy_snapshots(tmp_path: Path) -> None:
    database = tmp_path / "plans.sqlite3"
    legacy = build_plan(demo_request())
    modern = build_plan_v2(request_v2())
    store = PlanStore(database)
    store.save(legacy)
    v2_store = PlanStoreV2(database)
    v2_store.save(modern)

    assert PlanStore(database).load(legacy.id) == legacy
    assert v2_store.latest() == modern
    assert v2_store.history(modern.id) == [modern]


def test_legacy_database_is_backed_up_once_before_workbench_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    legacy = build_plan(demo_request())
    modern = build_plan_v2(request_v2())
    PlanStore(database).save(legacy)
    PlanStoreV2(database).save(modern)

    backup = database.with_name(f"{database.name}.pre-open-ga.bak")
    assert not backup.exists()
    WorkbenchStoreV3(database)
    assert backup.exists()
    first_backup = backup.read_bytes()
    with sqlite3.connect(backup) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "plan_versions" in tables
    assert "plan_versions_v2" in tables
    assert "fields_v3" not in tables

    WorkbenchStoreV3(database)
    assert backup.read_bytes() == first_backup
    assert PlanStore(database).load(legacy.id) == legacy
    assert PlanStoreV2(database).latest() == modern


def test_v2_history_rejects_missing_parent(tmp_path: Path) -> None:
    first = build_plan_v2(request_v2())
    second = build_plan_v2(first.request, previous=first)
    store = PlanStoreV2(tmp_path / "plans.sqlite3")

    try:
        store.save(second)
    except ValueError as error:
        assert "上一版本" in str(error)
    else:
        raise AssertionError("v2版本必须先保存上一版本")


def test_weather_provenance_survives_sqlite_and_old_v2_payloads_still_load(tmp_path: Path) -> None:
    first = build_plan_v2(request_v2())
    feedback_at = datetime(2026, 10, 19, 8, tzinfo=LOCAL_ZONE)
    second = apply_feedback_v2(
        first,
        FeedbackV2(
            kind="workability",
            at=feedback_at,
            plot_id="A",
            day=date(2026, 10, 19),
            site_workable=True,
            weather_workable=True,
        ),
    )
    store = PlanStoreV2(tmp_path / "plans.sqlite3")
    store.save(first)
    store.save(second)
    restored = PlanStoreV2(store.path).load(second.id)
    confirmation = restored.request.plots[0].workability_by_date[date(2026, 10, 19)]
    assert confirmation.weather_source == "user_estimate"
    assert confirmation.weather_snapshot_at == feedback_at

    old_payload = json.loads(first.model_dump_json())
    for day in old_payload["request"]["days"]:
        day.pop("weather_source")
        day.pop("weather_snapshot_at")
    old_version = PlanVersionV2.model_validate(old_payload)
    assert old_version.request.days[0].weather_source == "synthetic"
    assert old_version.request.days[0].weather_snapshot_at is None
