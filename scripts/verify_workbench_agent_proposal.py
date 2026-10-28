"""Verify a real assistant resource proposal and confirmation through the page."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--prepare-task", action="store_true")
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)
    tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()
    result: dict[str, object] = {"result": "failed", "date": tomorrow}
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(args.url, wait_until="domcontentloaded")
            app = page.locator("[data-app]")
            app.wait_for(timeout=20_000)
            if args.prepare_task:
                page.locator('[data-action="edit-schedule"]').click()
                row = page.locator(f'[data-form="schedule"] .schedule-day[data-day="{tomorrow}"]')
                row.locator('[name="intervals"]').fill("09:00-11:00")
                row.locator('[name="resource_minutes"]').fill("120")
                page.locator('[data-form="schedule"] button[value="save"]').click()
                page.locator("[data-dialog]").wait_for(state="hidden", timeout=20_000)
                page.locator('[data-action="task"]').click()
                task = page.locator('[data-form="task"]')
                task.locator('[name="title"]').fill("明日核对记录")
                task.locator('[name="purpose"]').fill("核对并保存记录")
                task.locator('[name="completion_standard"]').fill("记录已核对")
                task.locator('[name="category"]').select_option("offsite")
                task.locator('[name="anchor_date"]').fill(tomorrow)
                task.locator('[name="due_date"]').fill(tomorrow)
                task.locator('button[value="save"]').click()
                page.locator('[data-preview] [data-action="adopt"]').wait_for(timeout=20_000)
                page.locator('[data-preview] [data-action="adopt"]').click()
                page.locator("[data-preview]").wait_for(state="hidden", timeout=20_000)
                initial = app.evaluate("app => app._workbenchData")
                initial_task = next(
                    item for item in initial["items"] if item["title"] == "明日核对记录"
                )
                assert initial_task["start"][11:16] == "09:00"
                result["initial_task_time"] = initial_task["start"]
            before = app.evaluate("app => app._workbenchData.field")
            old_plan = app.evaluate("app => app._workbenchData.plan")
            page.locator("#message-input").fill(
                f"{tomorrow} 我只有13:00到15:00有空，请把可用时间改成120分钟。"
            )
            page.locator("[data-form=message] .send-button").click()
            proposal = page.locator('[data-action="confirm-proposal"]').first
            proposal.wait_for(timeout=45_000)
            proposed = app.evaluate("app => app._workbenchData.field")
            assert proposed["fact_version"] == before["fact_version"], "确认前事实已改变"
            result["proposal_text"] = proposal.locator("xpath=..").locator("xpath=..").inner_text()
            page.screenshot(
                path=args.screenshots / "proposal-before-confirmation.png", full_page=True
            )
            proposal.click()
            proposal.wait_for(state="detached", timeout=20_000)
            page.wait_for_timeout(1000)
            after = app.evaluate(
                "app => ({field: app._workbenchData.field, preview: app._workbenchData.preview, "
                "schedule: app._workbenchData.schedule})"
            )
            assert after["field"]["fact_version"] > before["fact_version"], "确认后事实版本未增加"
            result["before_fact_version"] = before["fact_version"]
            result["after_fact_version"] = after["field"]["fact_version"]
            result["preview_generated"] = bool(after["preview"])
            result["schedule_for_date"] = after["schedule"].get(tomorrow)
            assert result["schedule_for_date"]["intervals"] == "13:00-15:00"
            assert result["schedule_for_date"]["resource_minutes"] == 120
            if args.prepare_task:
                assert after["preview"], "时间修改未产生计划预览"
                assert app.evaluate("app => app._workbenchData.plan.id") == old_plan["id"]
                page.locator('[data-preview] [data-action="adopt"]').click()
                page.locator("[data-preview]").wait_for(state="hidden", timeout=20_000)
                adopted = app.evaluate("app => app._workbenchData")
                moved = next(item for item in adopted["items"] if item["title"] == "明日核对记录")
                assert moved["start"][11:16] == "13:00"
                result["adopted_task_time"] = moved["start"]
            page.locator('[data-action="edit-schedule"]').click()
            row = page.locator(f'[data-form="schedule"] .schedule-day[data-day="{tomorrow}"]')
            assert row.locator('[name="intervals"]').input_value() == "13:00-15:00"
            row.locator('[name="intervals"]').fill("14:00-15:00")
            row.locator('[name="resource_minutes"]').fill("60")
            page.locator('[data-form="schedule"] button[value="save"]').click()
            page.locator("[data-dialog]").wait_for(state="hidden", timeout=20_000)
            if args.prepare_task:
                page.locator('[data-preview] [data-action="adopt"]').wait_for(timeout=20_000)
                page.locator('[data-preview] [data-action="adopt"]').click()
                page.locator("[data-preview]").wait_for(state="hidden", timeout=20_000)
            page.reload(wait_until="domcontentloaded")
            app.wait_for(timeout=20_000)
            final = app.evaluate("app => app._workbenchData.schedule")
            assert final[tomorrow]["intervals"] == "14:00-15:00"
            assert final[tomorrow]["resource_minutes"] == 60
            result["schedule_after_form_edit_and_reload"] = final[tomorrow]
            if args.prepare_task:
                items = app.evaluate("app => app._workbenchData.items")
                moved = next(item for item in items if item["title"] == "明日核对记录")
                assert moved["start"][11:16] == "14:00"
                result["reloaded_task_time"] = moved["start"]
            assert not errors, f"浏览器错误：{errors}"
            page.screenshot(path=args.screenshots / "proposal-confirmed.png", full_page=True)
            result["result"] = "passed"
        except Exception as error:
            result["error"] = str(error)
            page.screenshot(path=args.screenshots / "failure.png", full_page=True)
        finally:
            result["browser_errors"] = errors
            args.report.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
