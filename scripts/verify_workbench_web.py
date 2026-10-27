"""Exercise the HTML workbench through a real browser and save review screenshots."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

VIEWPORTS = ((1440, 900), (1158, 714), (900, 844), (390, 844), (360, 800))


def _repo_path(path: Path) -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8531")
    parser.add_argument("--screenshots", type=Path, required=True)
    parser.add_argument("--field-name", default="网页验收演示地块")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    args.screenshots.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.date()
    interval_start = now.replace(second=0, microsecond=0) + timedelta(minutes=2)
    interval_end = interval_start + timedelta(hours=1)

    browser_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.locator("[data-app]").wait_for(timeout=20_000)
        page.wait_for_timeout(700)

        assert page.locator("[data-field-title]").inner_text() == "先从一块地开始"
        page.screenshot(path=args.screenshots / "blank-first-screen.png", full_page=True)

        page.locator("[data-tasks] [data-action=profile]").click()
        page.locator("[data-form=profile] input[name=name]").fill("未完成建档草稿")
        page.keyboard.press("Escape")
        page.locator("[data-unsaved-prompt]").wait_for(timeout=5_000)
        page.locator("[data-action=unsaved-save]").click()
        page.locator("[data-toast][data-show=true]").filter(
            has_text="草稿已保存在本地数据库"
        ).wait_for(timeout=15_000)
        page.locator("[data-dialog]").wait_for(state="hidden", timeout=15_000)
        page.reload(wait_until="domcontentloaded")
        page.locator("[data-app]").wait_for(timeout=20_000)
        page.locator("[data-tasks] [data-action=profile]").click()
        draft_name = page.locator("[data-form=profile] input[name=name]")
        draft_name.wait_for(timeout=5_000)
        assert draft_name.input_value() == "未完成建档草稿"
        page.screenshot(path=args.screenshots / "unsaved-draft-restored.png", full_page=True)
        draft_name.fill(args.field_name)
        page.locator("[data-form=profile] input[name=start_date]").fill(today.isoformat())
        page.locator('[data-form="profile"] input[name="province"]').fill("湖北省")
        page.locator('[data-form="profile"] input[name="province_confirmed"]').check()
        page.locator('[data-form="profile"] select[name="crop"]').select_option("油菜")
        page.locator('[data-form="profile"] input[name="crop_confirmed"]').check()
        page.locator('[data-form="profile"] select[name="stage"]').select_option("苗期")
        page.locator('[data-form="profile"] input[name="stage_confirmed"]').check()
        page.locator('[data-form="profile"] input[name="inspection_duration_minutes"]').fill("35")
        page.locator('[data-form="profile"] input[name="maintenance_duration_minutes"]').fill("50")
        page.locator("[data-form=profile] button[value=save]").click()
        page.locator("[data-field-title]").filter(has_text=args.field_name).wait_for(timeout=15_000)
        page.locator("[data-toast][data-show=true]").filter(has_text="地块资料已保存").wait_for(
            timeout=15_000
        )
        page.locator("[data-dialog]").wait_for(state="hidden", timeout=15_000)
        profile = page.locator("[data-app]").evaluate("app => app._workbenchData.field.profile")
        assert (
            profile["province_confirmed"]
            and profile["crop_confirmed"]
            and profile["stage_confirmed"]
        )
        assert profile["inspection_duration_minutes"] == 35
        assert profile["maintenance_duration_minutes"] == 50
        page.screenshot(path=args.screenshots / "profile-saved.png", full_page=True)

        page.locator("[data-action=task]").click()
        page.locator("[data-form=task] input[name=title]").fill("整理采购记录")
        page.locator("[data-form=task] textarea[name=purpose]").fill("核对本周采购记录并保存结果")
        page.locator("[data-form=task] input[name=completion_standard]").fill(
            "采购记录已核对并保存"
        )
        page.locator("[data-form=task] select[name=category]").select_option("offsite")
        page.locator("[data-form=task] select[name=recurrence]").select_option("interval")
        page.locator("[data-form=task] input[name=anchor_date]").fill(today.isoformat())
        page.locator("[data-form=task] input[name=due_date]").fill(
            (today + timedelta(days=6)).isoformat()
        )
        page.locator("[data-form=task] input[name=interval_days]").fill("3")
        page.locator("[data-form=task] button[value=save]").click()
        page.locator("[data-toast][data-show=true]").filter(has_text="待办已保存").wait_for(
            timeout=15_000
        )
        page.locator("[data-dialog]").wait_for(state="hidden", timeout=15_000)

        page.locator("[data-action=edit-schedule]").click()
        first_day = page.locator("[data-form=schedule] .schedule-day").first
        first_day.locator("input[name=resource_minutes]").fill("60")
        first_day.locator("input[name=intervals]").fill(
            f"{interval_start:%H:%M}-{interval_end:%H:%M}"
        )
        page.locator("[data-form=schedule] button[value=save]").click()
        page.locator("[data-toast][data-show=true]").filter(has_text="调整预览已生成").wait_for(
            timeout=15_000
        )
        page.locator("[data-action=preview]").click()
        page.locator("[data-toast][data-show=true]").filter(has_text="七天安排预览已生成").wait_for(
            timeout=15_000
        )
        page.locator("[data-preview] [data-action=adopt]").wait_for(timeout=15_000)
        page.screenshot(path=args.screenshots / "plan-preview.png", full_page=True)
        page.locator("[data-preview] [data-action=adopt]").click()
        page.get_by_text("已采用 · V1", exact=True).wait_for(timeout=15_000)

        delay = (interval_start - datetime.now(ZoneInfo("Asia/Shanghai"))).total_seconds() + 2
        if delay > 0:
            sleep(delay)
            page.reload(wait_until="domcontentloaded")
            page.locator("[data-app]").wait_for(timeout=20_000)
            page.get_by_text("已采用 · V1", exact=True).wait_for(timeout=15_000)
        result_action = page.locator("[data-tasks] [data-action=open-result]").first
        result_action.wait_for(timeout=15_000)
        result_action.click()
        page.locator("[data-form=result] select[name=result_0]").select_option("normal")
        page.locator("[data-form=result] input[name=completed]").check()
        page.locator("[data-form=result] button[value=save]").click()
        page.locator("[data-toast][data-show=true]").filter(has_text="新的安排预览已生成").wait_for(
            timeout=15_000
        )
        page.screenshot(path=args.screenshots / "result-saved-preview.png", full_page=True)
        page.locator("[data-preview] [data-action=adopt]").wait_for(timeout=15_000)
        page.locator("[data-preview] [data-action=adopt]").click()
        page.get_by_text("已采用 · V2", exact=True).wait_for(timeout=15_000)
        assert page.get_by_text("整理采购记录", exact=True).count() >= 1

        page.locator("[data-action=task-management]").click()
        editor = page.locator("[data-dialog][open] .series-editor")
        editor.wait_for(timeout=15_000)
        editor.locator("summary").click()
        editor.locator("input[name=title]").fill("整理采购单据")
        editor.locator("button[value=save]").click()
        page.locator("[data-toast][data-show=true]").filter(has_text="后续周期已更新").wait_for(
            timeout=15_000
        )
        page.locator("[data-preview] [data-action=adopt]").wait_for(timeout=15_000)
        page.screenshot(path=args.screenshots / "series-update-preview.png", full_page=True)
        page.locator("[data-preview] [data-action=adopt]").click()
        page.get_by_text("已采用 · V3", exact=True).wait_for(timeout=15_000)
        page.locator("[data-action=task-management]").click()
        page.locator("[data-dialog][open] .series-editor").wait_for(timeout=15_000)
        page.screenshot(path=args.screenshots / "task-management.png", full_page=True)
        page.locator("[data-action=close-dialog]").click()
        page.locator("[data-dialog]").wait_for(state="hidden", timeout=15_000)

        page.reload(wait_until="domcontentloaded")
        page.locator("[data-app]").wait_for(timeout=20_000)
        page.locator("[data-field-title]").filter(has_text=args.field_name).wait_for(timeout=15_000)
        page.get_by_text("已采用 · V3", exact=True).wait_for(timeout=15_000)

        viewport_results = []
        for width, height in VIEWPORTS:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(250)
            root = page.locator("[data-app]").evaluate(
                "node => ({width: node.clientWidth, scrollWidth: node.scrollWidth, "
                "documentWidth: document.documentElement.clientWidth, "
                "documentScrollWidth: document.documentElement.scrollWidth})"
            )
            viewport_results.append(
                {
                    "width": width,
                    "height": height,
                    **root,
                    "horizontal_overflow": (
                        root["scrollWidth"] > root["width"] + 1
                        or root["documentScrollWidth"] > root["documentWidth"] + 1
                    ),
                }
            )
            assert not viewport_results[-1]["horizontal_overflow"], (
                f"视口 {width}x{height} 出现页面横向溢出：{viewport_results[-1]}"
            )
            date_buttons = page.locator("[data-days] .day-button")
            initial_date = date_buttons.first.get_attribute("data-date")
            next_date = date_buttons.nth(1).get_attribute("data-date")
            assert initial_date and next_date and initial_date != next_date
            date_buttons.nth(1).click()
            assert date_buttons.nth(1).get_attribute("aria-selected") == "true"
            date_buttons.first.click()
            assert date_buttons.first.get_attribute("aria-selected") == "true"
            page.locator('[data-action="week"]').click()
            assert page.locator("[data-week]").is_visible()
            page.locator('[data-action="week"]').click()
            assert not page.locator("[data-week]").is_visible()
            page.screenshot(path=args.screenshots / f"adopted-{width}x{height}.png", full_page=True)
            if width < 1000:
                assert page.locator(".mobile-nav").is_visible()
                page.locator("[data-action=tab-assistant]").click()
                assert page.locator("[data-rail=assistant]").is_visible()
                page.locator("[data-action=tab-schedule]").click()
                assert page.locator("[data-rail=schedule]").is_visible()
                if width == 390:
                    page.locator("[data-action=task-management]").click()
                    page.locator("[data-dialog][open] .series-editor").wait_for(timeout=15_000)
                    dialog_box = page.locator("[data-dialog]").bounding_box()
                    assert dialog_box and dialog_box["width"] >= 389
                    page.screenshot(
                        path=args.screenshots / "task-management-390.png", full_page=True
                    )
                    page.locator("[data-action=close-dialog]").click()

        if browser_errors:
            raise AssertionError(f"浏览器脚本错误：{browser_errors}")
        result = {
            "result": "passed",
            "flow": [
                "关闭未保存表单时保存草稿，刷新后恢复并完成建档",
                "通过三个确认框保存省份、作物、生育期和任务时长",
                "V1 预览与采用",
                "保存周期非现场待办",
                "从任务卡记录完成结果并采用 V2",
                "修改后续周期并采用 V3",
                "刷新恢复",
            ],
            "viewports": viewport_results,
            "browser_errors": browser_errors,
            "screenshots": _repo_path(args.screenshots),
        }
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
