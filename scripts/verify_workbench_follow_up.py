"""Seed one public issue fixture, then schedule its follow-up through the real UI."""

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


def _repo_path(path: Path) -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def seed_open_issue(database: Path, field_name: str) -> tuple[str, str]:
    local_zone = ZoneInfo("Asia/Shanghai")
    today = _today()
    store = WorkbenchStoreV3(database)
    service = WorkbenchServiceV3(
        store,
        today=lambda: today,
        now=lambda: datetime.combine(today, time(7), tzinfo=local_zone),
    )
    field_id = "field-follow-up-browser"
    start = today
    days = [
        DayWindow(
            day=start + timedelta(days=offset),
            site_workable=True,
            weather_workable=True,
            resource_minutes=120,
            intervals=[TimeWindow(start=time(8), end=time(10))],
        )
        for offset in range(7)
    ]
    state = service.save_field_draft(
        FieldDraftV3(
            field_id=field_id,
            mode="real",
            profile={"id": field_id, "name": field_name, "province": "湖北省"},
            start_date=start,
            days=days,
        ),
        expected_version=None,
        operation_id="follow-up-browser-seed-field",
    )
    series = TaskSeriesV4(
        id="series-follow-up-browser-source",
        field_id=field_id,
        template=TaskTemplateV4(
            id="template-follow-up-browser-source",
            title="现场观察演示",
            task_type="custom",
            category="onsite",
            purpose="采集公开模拟的现场观察结果",
            result_fields=["积水情况"],
            completion_standard="已记录本次现场观察结果",
            source_kind="user_defined",
        ),
        recurrence="once",
        anchor_date=start,
        duration_minutes=30,
        earliest_date=start,
        due_date=start,
        conditions_confirmed=True,
    )
    saved = service.save_task_series(
        series,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        expected_task_version=state.task_version,
        operation_id="follow-up-browser-seed-series",
    )
    state = service.get_field(field_id)
    preview = service.preview_plan(field_id, operation_id="follow-up-browser-seed-preview")
    service.adopt_plan(
        preview.id,
        expected_fact_version=state.fact_version,
        expected_plan_id=state.latest_plan_id,
        operation_id="follow-up-browser-seed-adopt",
    )
    state = service.get_field(field_id)
    service = WorkbenchServiceV3(
        store,
        today=lambda: today,
        now=lambda: datetime.combine(today, time(9), tzinfo=local_zone),
    )
    service.record_task_result(
        field_id,
        saved.created_instance_ids[0],
        {"积水情况": "issue"},
        notes="公开模拟：田角有少量积水，安排复查确认变化。",
        completed=False,
        expected_draft_version=state.draft_version,
        expected_fact_version=state.fact_version,
        operation_id="follow-up-browser-seed-observation",
    )
    issue = next(issue for issue in service.list_field_issues(field_id) if issue.status == "open")
    return field_id, issue.id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8551")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--field-name", default="复查流程公开模拟地块")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)
    field_id, issue_id = seed_open_issue(args.database, args.field_name)
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.locator("[data-app]").wait_for(timeout=20_000)
        page.locator("[data-field-title]").filter(has_text=args.field_name).wait_for(timeout=15_000)
        page.locator('[data-action="task-management"]').click()
        row = page.locator(".issue-row").filter(has_text="积水")
        row.locator(".issue-followup > summary").click()
        form = row.locator('form[data-form="follow-up"]')
        form.wait_for(timeout=5_000)
        form.locator('input[name="purpose"]').fill("复查田角积水是否消退并记录现场结果")
        form.locator('input[name="duration_minutes"]').fill("35")
        form.locator('button[value="save"]').click()
        page.locator('[data-toast][data-show="true"]').filter(
            has_text="复查任务已保存并关联现场问题"
        ).wait_for(timeout=15_000)
        page.locator("[data-preview] [data-action=adopt]").wait_for(timeout=15_000)
        page.screenshot(path=args.screenshots / "follow-up-preview-desktop.png", full_page=True)
        page.locator("[data-preview] [data-action=adopt]").click()
        page.get_by_text("已采用 · V2", exact=True).wait_for(timeout=15_000)
        page.wait_for_timeout(750)
        state = page.locator("[data-app]").evaluate(
            """app => ({
                items: (app._workbenchData?.items || []).map(item => ({
                    id: item.id, title: item.title, type: item.type,
                    related_issue_id: item.related_issue_id, duration: item.duration
                })),
                history: app._workbenchData?.history || []
            })"""
        )
        follow_up = next(item for item in state["items"] if item["type"] == "follow_up")
        assert follow_up["related_issue_id"] == issue_id
        assert follow_up["duration"] == 35
        assert state["history"], "采用复查安排后应有版本历史"

        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-action="task-management"]').click()
        page.locator(".issue-row").filter(has_text="积水").wait_for(timeout=5_000)
        page.screenshot(path=args.screenshots / "follow-up-management-mobile.png", full_page=True)
        dims = page.locator("[data-app]").evaluate(
            """node => ({
                client: node.clientWidth,
                scroll: node.scrollWidth,
                document: document.documentElement.scrollWidth
            })"""
        )
        assert dims["scroll"] <= dims["client"] + 1
        assert dims["document"] <= 391
        assert not errors, f"浏览器脚本错误：{errors}"
        browser.close()

    report = {
        "result": "passed",
        "fixture": "公开模拟现场问题；通过服务层预先生成一次问题，页面操作仅负责创建和采用复查",
        "field_id": field_id,
        "issue_id": issue_id,
        "follow_up": follow_up,
        "adopted_history_versions": [item["version"] for item in state["history"]],
        "mobile_dimensions": dims,
        "browser_errors": errors,
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
