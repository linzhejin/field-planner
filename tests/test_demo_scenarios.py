from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from nongshi import workbench
from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.rules import load_rule_catalog
from nongshi.service_v3 import WorkbenchServiceV3


def _service(database: Path) -> WorkbenchServiceV3:
    return WorkbenchServiceV3(
        WorkbenchStoreV3(database),
        catalog=load_rule_catalog(),
        today=lambda: date(2026, 10, 26),
        now=lambda: datetime(2026, 10, 26, 0, tzinfo=UTC),
    )


def test_rich_demo_has_supported_work_across_all_seven_dates(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(workbench, "_today", lambda: date(2026, 10, 26))
    monkeypatch.setattr(workbench, "_now", lambda: datetime(2026, 10, 26, tzinfo=UTC))
    service = _service(tmp_path / "rich.sqlite3")

    field_id = workbench._seed_demo(service, scenario="rich")
    plan = service.get_current_plan(field_id)

    assert plan is not None
    assert {
        item.start_at.date()
        for item in plan.items
        if item.start_at is not None and not item.completed
    } == {date(2026, 10, 27) + timedelta(days=offset) for offset in range(7)}
    assert all(
        item.template_snapshot["source_kind"] == "development_example"
        for item in plan.items
        if item.template_snapshot and item.task_type == "management"
    )


def test_sparse_demo_does_not_fill_empty_days_with_fake_tasks(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(workbench, "_today", lambda: date(2026, 10, 26))
    monkeypatch.setattr(workbench, "_now", lambda: datetime(2026, 10, 26, tzinfo=UTC))
    service = _service(tmp_path / "sparse.sqlite3")

    field_id = workbench._seed_demo(service, scenario="sparse")
    plan = service.get_current_plan(field_id)

    assert plan is not None
    scheduled_days = {
        item.start_at.date()
        for item in plan.items
        if item.start_at is not None and not item.completed
    }
    assert scheduled_days == {date(2026, 10, 27)}
