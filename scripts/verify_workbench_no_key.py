"""Exercise missing-key recovery through the HTML workbench."""

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
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"result": "failed"}
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(args.url, wait_until="domcontentloaded")
            app = page.locator("[data-app]")
            app.wait_for(timeout=20_000)
            page.locator('[data-tasks] [data-action="profile"]').click()
            page.locator('[data-form="profile"] input[name="name"]').fill("无密钥验收地块")
            page.locator('[data-form="profile"] button[value="save"]').click()
            page.locator("[data-field-title]").filter(has_text="无密钥验收地块").wait_for(
                timeout=15_000
            )
            page.locator("#message-input").fill("请帮我说明当前安排")
            page.locator("[data-form=message] .send-button").click()
            page.locator("[data-messages] .message.assistant").filter(
                has_text="还没配置模型密钥"
            ).wait_for(timeout=30_000)
            status = app.evaluate("app => app._workbenchData.agent_status")
            page.screenshot(path=args.screenshots / "missing-key.png", full_page=True)
            assert "missing_credentials" in status
            page.locator('[data-action="task"]').click()
            page.locator('[data-form="task"] input[name="title"]').fill("无密钥时核对记录")
            page.locator('[data-form="task"] textarea[name="purpose"]').fill("核对并记录")
            page.locator('[data-form="task"] input[name="completion_standard"]').fill("已核对")
            page.locator('[data-form="task"] select[name="category"]').select_option("offsite")
            page.locator('[data-form="task"] button[value="save"]').click()
            page.locator('[data-toast][data-show="true"]').filter(has_text="待办已保存").wait_for(
                timeout=20_000
            )
            page.reload(wait_until="domcontentloaded")
            app.wait_for(timeout=20_000)
            assert page.locator("[data-field-title]").inner_text() == "无密钥验收地块"
            data = app.evaluate("app => app._workbenchData")
            assert any(item["title"] == "无密钥时核对记录" for item in data["items"])
            assert not errors, f"浏览器错误：{errors}"
            result.update(result="passed", model_status=status, structured_task_saved=True)
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
