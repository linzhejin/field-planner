"""Verify desktop field isolation and cancellation through the HTML workbench."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1158)
    parser.add_argument("--height", type=int, default=714)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    result: dict[str, object] = {"result": "failed", "completed": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(args.url, wait_until="domcontentloaded")
            app = page.locator("[data-app]")
            app.wait_for(timeout=20_000)
            original = app.evaluate(
                "app => ({field: app._workbenchData.field, plan: app._workbenchData.plan})"
            )
            assert original["field"] and original["plan"], "缺少已采用的初始地块"
            original_id = original["field"]["id"]
            original_plan = original["plan"]["id"]
            original_name = original["field"]["profile"]["name"]

            page.locator('[data-action="profile"]').first.click()
            name = page.locator('[data-form="profile"] input[name="name"]')
            name.fill("未保存的修改")
            page.keyboard.press("Escape")
            page.locator('[data-action="unsaved-discard"]').click()
            page.locator("[data-dialog]").wait_for(state="hidden")
            page.locator('[data-action="profile"]').first.click()
            assert name.input_value() == original_name, "放弃后错误保留了未保存的名称"
            page.locator('[data-action="close-dialog"]').click()
            result["completed"].append("放弃未保存的档案修改")

            for number in range(2, 5):
                field_name = f"隔离验收地块{number}"
                page.locator('[data-action="new-field"]').last.click()
                page.locator('[data-tasks] [data-action="profile"]').wait_for(timeout=15_000)
                page.locator('[data-tasks] [data-action="profile"]').click()
                page.locator('[data-form="profile"] input[name="name"]').fill(field_name)
                page.locator('[data-form="profile"] button[value="save"]').click()
                page.locator("[data-field-title]").filter(has_text=field_name).wait_for(
                    timeout=15_000
                )
            assert page.locator('[data-action="select-field"]').count() >= 4
            page.screenshot(path=args.screenshots / "four-fields.png", full_page=True)
            result["completed"].append("通过页面新建第四块地")

            page.locator(f'[data-action="select-field"][data-field-id="{original_id}"]').click()
            page.locator("[data-field-title]").filter(has_text=original_name).wait_for(
                timeout=15_000
            )
            restored = app.evaluate("app => app._workbenchData.plan")
            assert restored and restored["id"] == original_plan, "切换地块改变了原计划"
            result["completed"].append("切回原地块保留原采用计划")

            page.locator('[data-action="preview"]').click()
            page.locator('[data-preview] [data-action="cancel-preview"]').wait_for(timeout=15_000)
            page.locator('[data-preview] [data-action="cancel-preview"]').click()
            page.locator("[data-preview]").wait_for(state="hidden", timeout=15_000)
            after_cancel = app.evaluate("app => app._workbenchData.plan")
            assert after_cancel and after_cancel["id"] == original_plan, "放弃预览改变了采用计划"
            result["completed"].append("放弃预览保留已采用计划")
            assert not errors, f"浏览器错误：{errors}"
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
