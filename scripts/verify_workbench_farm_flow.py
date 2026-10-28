"""Exercise the inspection, issue, maintenance and follow-up flow through the page."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

ZONE = ZoneInfo("Asia/Shanghai")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"result": "failed", "completed": []}
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.on("pageerror", lambda error: errors.append(str(error)))
        app = page.locator("[data-app]")

        def data():
            return app.evaluate("app => app._workbenchData")

        def adopt():
            previous = (data().get("plan") or {}).get("version", 0)
            page.locator('[data-preview] [data-action="adopt"]').wait_for(timeout=20_000)
            page.locator('[data-preview] [data-action="adopt"]').click()
            page.locator("[data-preview]").wait_for(state="hidden", timeout=20_000)
            assert data()["plan"]["version"] > previous

        def wait_task(task_type):
            current = data()
            task = next(
                item
                for item in current["items"]
                if item["type"] == task_type and item["status"] == "scheduled"
            )
            starts = datetime.fromisoformat(task["start"])
            delay = (starts - datetime.now(ZONE)).total_seconds() + 2
            assert delay <= 180, f"任务需等待过久才能记录：{starts.isoformat()}"
            if delay > 0:
                time.sleep(delay)
                page.reload(wait_until="domcontentloaded")
                app.wait_for(timeout=20_000)
            page.locator(
                f'[data-action="select-date"][data-date="{starts.date().isoformat()}"]'
            ).click()
            card = page.locator(
                f'[data-tasks] [data-action="open-result"][data-task-id="{task["id"]}"]'
            )
            card.wait_for(timeout=20_000)
            card.click()
            return task

        def record(issue: bool, resolve: bool = False):
            form = page.locator('[data-form="result"]')
            form.wait_for(timeout=10_000)
            for select in form.locator('select[name^="result_"]').all():
                select.select_option("issue" if issue else "normal")
            if resolve:
                form.locator('[name="resolve_issue"]').select_option("resolved")
            form.locator('[name="notes"]').fill(
                "公开模拟：沟渠受阻并有积水" if issue else "公开模拟：复查正常"
            )
            form.locator('[name="completed"]').check()
            form.locator('button[value="save"]').click()
            page.locator('[data-toast][data-show="true"]').filter(
                has_text="现场结果已保存"
            ).wait_for(timeout=20_000)
            adopt()

        try:
            page.goto(args.url, wait_until="domcontentloaded")
            app.wait_for(timeout=20_000)
            page.locator('[data-tasks] [data-action="profile"]').click()
            form = page.locator('[data-form="profile"]')
            today = datetime.now(ZONE).date()
            form.locator('[name="name"]').fill("完整农事流程模拟地块")
            form.locator('[name="start_date"]').fill(today.isoformat())
            form.locator('[name="crop"]').select_option("油菜")
            form.locator('[name="crop_confirmed"]').check()
            form.locator('[name="stage"]').select_option("苗期")
            form.locator('[name="stage_confirmed"]').check()
            form.locator('[name="field_type"]').select_option("lowland_rice_stubble")
            form.locator('[name="inspection_requested"]').select_option("true")
            form.locator('[name="inspection_duration_minutes"]').fill("1")
            form.locator('[name="maintenance_duration_minutes"]').fill("1")
            form.locator('button[value="save"]').click()
            page.locator("[data-field-title]").filter(has_text="完整农事流程模拟地块").wait_for(
                timeout=20_000
            )
            result["completed"].append("页面空白建档")

            page.locator("#message-input").fill(
                "省份已确认是湖北省，请把省份设为湖北省并标记已确认。"
            )
            page.locator("[data-form=message] .send-button").click()
            proposal = page.locator('[data-action="confirm-proposal"]').first
            proposal.wait_for(timeout=45_000)
            assert not data()["field"]["profile"]["province"]
            proposal.click()
            proposal.wait_for(state="detached", timeout=20_000)
            assert "湖北" in data()["field"]["profile"]["province"]
            assert data()["field"]["profile"]["province_confirmed"]
            result["completed"].append("真实模型提出档案修改并经用户确认")

            page.locator('[data-action="weather"]').click()
            page.locator('[data-form="location-search"] input[name="query"]').fill("武汉")
            page.locator('[data-form="location-search"] button[type="submit"]').click()
            page.locator(".location-choice").first.wait_for(timeout=30_000)
            page.locator(".location-choice").first.click()
            page.locator('[data-action="fetch-weather"]').wait_for(timeout=20_000)
            page.locator('[data-action="fetch-weather"]').click()
            page.locator(".forecast-row").last.wait_for(timeout=30_000)
            assert page.locator(".forecast-row").count() == 7
            page.locator('[data-action="close-dialog"]').click()
            result["completed"].append("确认地点并查询真实七天天气")

            page.locator('[data-action="task"]').click()
            recurring = page.locator('[data-form="task"]')
            recurring.locator('[name="title"]').fill("定期核对农资")
            recurring.locator('[name="purpose"]').fill("核对并保存农资记录")
            recurring.locator('[name="completion_standard"]').fill("农资记录已核对")
            recurring.locator('[name="category"]').select_option("offsite")
            recurring.locator('[name="recurrence"]').select_option("interval")
            recurring.locator('[name="anchor_date"]').fill((today + timedelta(days=1)).isoformat())
            recurring.locator('[name="due_date"]').fill((today + timedelta(days=6)).isoformat())
            recurring.locator('[name="interval_days"]').fill("2")
            recurring.locator('[name="priority"]').select_option("3")
            recurring.locator('button[value="save"]').click()
            page.locator("[data-dialog]").wait_for(state="hidden", timeout=20_000)
            assert any(row["title"] == "定期核对农资" for row in data()["task_series"])
            result["completed"].append("页面建立周期待办")

            page.locator('[data-action="edit-schedule"]').click()
            row = page.locator('[data-form="schedule"] .schedule-day').first
            start = datetime.now(ZONE).replace(second=0, microsecond=0) + timedelta(minutes=2)
            end = start + timedelta(minutes=30)
            row.locator('[name="site_workable"]').select_option("true")
            row.locator('[name="weather_workable"]').select_option("true")
            row.locator('[name="resource_minutes"]').fill("30")
            row.locator('[name="intervals"]').fill(f"{start:%H:%M}-{end:%H:%M}")
            page.locator('[data-form="schedule"] button[value="save"]').click()
            page.locator("[data-dialog]").wait_for(state="hidden", timeout=20_000)
            adopt()
            result["completed"].append("确认资源并采用 V1")

            wait_task("inspection")
            record(issue=True)
            issues = data()["issues"]
            assert any(issue["status"] == "open" for issue in issues), "巡田结果未形成现场问题"
            result["completed"].append("巡田完成并记录问题")

            for field, value in (
                ("standing_water", "true"),
                ("ditch_connectivity", "impaired"),
                ("maintenance_problem_confirmed", "true"),
                ("maintenance_need_confirmed", "true"),
                ("maintenance_purpose", "clear_existing_ditch"),
            ):
                page.locator('[data-action="observation"]').click()
                observation = page.locator('[data-form="observation"]')
                observation.locator('[name="field"]').select_option(field)
                observation.locator('[name="value"]').select_option(value)
                observation.locator('button[value="save"]').click()
                page.locator("[data-dialog]").wait_for(state="hidden", timeout=20_000)
            page.locator('[data-action="edit-schedule"]').click()
            row = page.locator('[data-form="schedule"] .schedule-day').first
            row.locator('[name="site_workable"]').select_option("true")
            row.locator('[name="weather_workable"]').select_option("true")
            page.locator('[data-form="schedule"] button[value="save"]').click()
            page.locator("[data-dialog]").wait_for(state="hidden", timeout=20_000)
            adopt()
            result["completed"].append("确认沟渠维护需求并采用安排")

            wait_task("maintenance")
            record(issue=True)
            assert any(issue["status"] == "open" for issue in data()["issues"])
            result["completed"].append("维护完成后问题仍保留")

            page.locator('[data-action="task-management"]').click()
            row = page.locator(".issue-row").filter(has_text="待解决").first
            assert "task_result" not in row.inner_text()
            row.locator(".issue-followup > summary").click()
            follow = row.locator('[data-form="follow-up"]')
            follow.locator('[name="duration_minutes"]').fill("1")
            follow.locator('button[value="save"]').click()
            page.locator("[data-dialog]").wait_for(state="hidden", timeout=20_000)
            adopt()
            wait_task("follow_up")
            record(issue=False, resolve=True)
            assert any(issue["status"] == "resolved" for issue in data()["issues"])
            exported = json.loads(data()["export_json"])
            assert all(issue["title"] != "task_result" for issue in exported["issues_v4"])
            result["completed"].append("确认复查并关闭问题")
            page.screenshot(path=args.output / "farm-flow-complete.png", full_page=True)
            assert not errors, f"浏览器错误：{errors}"
            result["result"] = "passed"
        except Exception as error:
            result["error"] = str(error)
            page.screenshot(path=args.output / "failure.png", full_page=True)
        finally:
            result["browser_errors"] = errors
            (args.output / "farm-flow.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
