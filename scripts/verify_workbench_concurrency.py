"""Verify double-click and stale edits through two independent browser sessions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--field-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"result": "failed"}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context_a = browser.new_context(viewport={"width": 1440, "height": 900})
        context_b = browser.new_context(viewport={"width": 1440, "height": 900})
        first, second = context_a.new_page(), context_b.new_page()
        try:
            for page in (first, second):
                page.goto(args.url, wait_until="domcontentloaded")
                page.locator("[data-app]").wait_for(timeout=20_000)
                if args.field_name not in page.locator("[data-field-title]").inner_text():
                    page.locator('[data-action="select-field"]').filter(
                        has_text=args.field_name
                    ).click()
                page.locator("[data-field-title]").filter(has_text=args.field_name).wait_for(
                    timeout=20_000
                )
            before = first.locator("[data-app]").evaluate("app => app._workbenchData.field")
            for page, name in ((first, "并发验收 A"), (second, "并发验收 B")):
                page.locator('[data-action="profile"]').first.click()
                page.locator('[data-form="profile"] input[name="name"]').fill(name)
            first.locator('[data-form="profile"] button[value="save"]').click(click_count=2)
            first.locator("[data-field-title]").filter(has_text="并发验收 A").wait_for(
                timeout=20_000
            )
            second.locator('[data-form="profile"] button[value="save"]').click()
            second.locator("[data-dialog-error]").filter(has_text="版本冲突").wait_for(
                timeout=20_000
            )
            assert "重试" in second.locator("[data-dialog-error]").inner_text()
            assert (
                second.locator('[data-form="profile"] input[name="name"]').input_value()
                == "并发验收 B"
            )
            second.screenshot(path=args.output / "stale-edit.png", full_page=True)
            first.reload(wait_until="domcontentloaded")
            first.locator("[data-field-title]").filter(has_text="并发验收 A").wait_for(
                timeout=20_000
            )
            after = first.locator("[data-app]").evaluate("app => app._workbenchData.field")
            assert after["draft_version"] == before["draft_version"] + 1
            result.update(
                result="passed", double_click_writes_once=True, stale_input_preserved=True
            )
        except Exception as error:
            result["error"] = str(error)
            result["page_state"] = second.locator("[data-app]").evaluate(
                "app => ({field: app._workbenchData?.field, "
                "result: app._workbenchData?.action_result, "
                "pending: app._workbenchUi?.pendingOperationId, "
                "input: app.querySelector('[data-form=profile] input[name=name]')?.value, "
                "toast: app.querySelector('[data-toast]')?.textContent})"
            )
            second.screenshot(path=args.output / "failure.png", full_page=True)
        finally:
            (args.output / "concurrency.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            browser.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
