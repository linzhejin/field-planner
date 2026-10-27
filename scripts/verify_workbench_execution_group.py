"""Verify grouping and releasing two field tasks through the real HTML workbench."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from nongshi.repository_v3 import WorkbenchStoreV3
from nongshi.schema_v2 import DayWindow, TimeWindow
from nongshi.schema_v3 import FieldDraftV3
from nongshi.service_v3 import WorkbenchServiceV3
from nongshi.tasks_v4 import TaskSeriesV4, TaskTemplateV4
from nongshi.workbench import _today

ZONE = ZoneInfo("Asia/Shanghai")


def _repo_path(path: Path) -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def seed_tasks(database: Path) -> tuple[str, list[str], str]:
    today = _today()
    field_id = "field-execution-group-browser"
    store = WorkbenchStoreV3(database)
    service = WorkbenchServiceV3(
        store,
        today=lambda: today,
        now=lambda: datetime.combine(today, time(7), tzinfo=ZONE),
    )
    days = [
        DayWindow(
            day=today + timedelta(days=offset),
            site_workable=True,
            weather_workable=True,
            resource_minutes=180,
            intervals=[TimeWindow(start=time(8), end=time(12))],
        )
        for offset in range(7)
    ]
    state = service.save_field_draft(
        FieldDraftV3(
            field_id=field_id,
            mode="real",
            profile={
                "id": field_id,
                "name": "合并出行公开模拟地块",
                "province": "湖北省",
                "province_confirmed": True,
                "crop": "油菜",
                "crop_confirmed": True,
                "stage": "苗期",
                "stage_confirmed": True,
                "inspection_requested": True,
            },
            start_date=today,
            days=days,
        ),
        expected_version=None,
        operation_id="group-browser-seed-field",
    )
    task_date = today + timedelta(days=1)
    task_ids = []
    for index, title in enumerate(("东侧巡田", "西侧巡田"), start=1):
        series_id = f"group-browser-series-{index}"
        series = TaskSeriesV4(
            id=series_id,
            field_id=field_id,
            template=TaskTemplateV4(
                id=f"{series_id}-template",
                title=title,
                task_type="inspection",
                category="onsite",
                purpose=f"记录第 {index} 块区域的现场检查结果",
                result_fields=["苗情"],
                completion_standard="已完成本次巡田并记录结果",
                source_kind="user_defined",
            ),
            recurrence="once",
            anchor_date=task_date,
            duration_minutes=30,
            earliest_date=task_date,
            due_date=task_date,
            conditions_confirmed=True,
        )
        result = service.save_task_series(
            series,
            expected_draft_version=state.draft_version,
            expected_fact_version=state.fact_version,
            expected_task_version=state.task_version,
            operation_id=f"group-browser-save-series-{index}",
        )
        task_ids.extend(result.created_instance_ids)
        state = service.get_field(field_id)
    preview = service.preview_plan(field_id, operation_id="group-browser-initial-preview")
    state = service.get_field(field_id)
    service.adopt_plan(
        preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=state.latest_plan_id,
        operation_id="group-browser-initial-adopt",
    )
    return field_id, task_ids, task_date.isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8551")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)
    field_id, task_ids, task_date = seed_tasks(args.database)
    browser_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.locator("[data-app]").wait_for(timeout=20_000)
        page.locator('[data-action="task-management"]').click()
        form = page.locator('form[data-form="outing"]')
        form.wait_for(timeout=10_000)
        assert form.locator(".group-candidate").count() == 2
        for task_id in task_ids:
            form.locator(f'input[name="member_task_ids"][value="{task_id}"]').check()
        form.locator('select[name="execution_date"]').select_option(task_date)
        form.locator('input[name="duration_minutes"]').fill("45")
        form.locator('button[value="save"]').click()
        page.locator('[data-toast][data-show="true"]').filter(has_text="合并出行已保存").wait_for(
            timeout=15_000
        )
        preview = page.locator("[data-preview]")
        preview.wait_for(timeout=15_000)
        preview_text = preview.inner_text()
        assert "东侧巡田" in preview_text and "西侧巡田" in preview_text
        page.screenshot(path=args.screenshots / "group-preview-desktop.png", full_page=True)
        preview.locator('[data-action="adopt"]').click()
        page.get_by_text("已采用 · V2", exact=True).wait_for(timeout=15_000)
        grouped = page.locator("[data-app]").evaluate(
            """app => ({
                items: app._workbenchData.items,
                groups: app._workbenchData.execution_groups,
                history: app._workbenchData.history
            })"""
        )
        member_items = [item for item in grouped["items"] if item["id"] in task_ids]
        assert len(member_items) == 2
        group_id = member_items[0]["execution_group_id"]
        assert group_id and all(item["execution_group_id"] == group_id for item in member_items)
        assert all(item["status"] == "scheduled" for item in member_items)
        group = next(row for row in grouped["groups"] if row["id"] == group_id)
        assert group["duration_minutes"] == 45

        page.locator('[data-action="task-management"]').click()
        group_row = page.locator(".group-row").filter(has_text="东侧巡田")
        group_row.locator('[data-action="cancel-group"]').click()
        page.locator('[data-toast][data-show="true"]').filter(
            has_text="这次合并出行已取消"
        ).wait_for(timeout=15_000)
        page.locator("[data-preview] [data-action=adopt]").wait_for(timeout=15_000)
        page.screenshot(path=args.screenshots / "group-cancel-preview-desktop.png", full_page=True)
        page.locator("[data-preview] [data-action=adopt]").click()
        page.get_by_text("已采用 · V3", exact=True).wait_for(timeout=15_000)
        released = page.locator("[data-app]").evaluate(
            """app => ({
                items: app._workbenchData.items,
                groups: app._workbenchData.execution_groups,
                history: app._workbenchData.history
            })"""
        )
        released_items = [item for item in released["items"] if item["id"] in task_ids]
        assert len(released_items) == 2
        assert all(not item["execution_group_id"] for item in released_items)
        assert all(item["status"] == "scheduled" for item in released_items)
        assert len({item["start"] for item in released_items}) == 2
        assert any(
            row["id"] == group_id and row["status"] == "cancelled" for row in released["groups"]
        )

        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-action="task-management"]').click()
        page.locator(".group-row").filter(has_text="东侧巡田").wait_for(timeout=5_000)
        page.screenshot(path=args.screenshots / "group-cancelled-mobile.png", full_page=True)
        dimensions = page.locator("[data-app]").evaluate(
            """app => ({client: app.clientWidth, scroll: app.scrollWidth,
                         document: document.documentElement.scrollWidth})"""
        )
        assert dimensions["scroll"] <= dimensions["client"] + 1
        assert dimensions["document"] <= 391
        assert not browser_errors, f"浏览器报错：{browser_errors}"
        browser.close()

    report = {
        "result": "passed",
        "fixture": "两个公开模拟巡田任务由服务层创建；分组合并、采用、取消和释放均由页面真实操作",
        "field_id": field_id,
        "task_ids": task_ids,
        "task_date": task_date,
        "execution_group_id": group_id,
        "confirmed_group_duration_minutes": group["duration_minutes"],
        "member_schedule_after_group": [item["start"] for item in member_items],
        "member_schedule_after_cancellation": [item["start"] for item in released_items],
        "history_versions": [row["version"] for row in released["history"]],
        "mobile_dimensions": dimensions,
        "browser_errors": browser_errors,
        "screenshots": _repo_path(args.screenshots),
        "database": "external temporary SQLite database; not distributed",
    }
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
