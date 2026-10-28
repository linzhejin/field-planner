"""Check that a temporary SQLite write lock preserves the desktop form for retry."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"result": "failed"}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        lock = None
        try:
            page.goto(args.url, wait_until="domcontentloaded")
            page.locator("[data-app]").wait_for(timeout=20_000)
            page.locator('[data-tasks] [data-action="profile"]').click()
            name = page.locator('[data-form="profile"] input[name="name"]')
            name.fill("数据库锁定验收地块")
            lock = sqlite3.connect(args.database, timeout=2)
            lock.execute("BEGIN IMMEDIATE")
            page.locator('[data-form="profile"] button[value="save"]').click()
            page.locator("[data-dialog-error]").filter(has_text="没有保存").wait_for(timeout=30_000)
            assert name.input_value() == "数据库锁定验收地块"
            page.screenshot(path=args.output / "locked-save.png", full_page=True)
            lock.rollback()
            lock.close()
            lock = None
            page.locator('[data-form="profile"] button[value="save"]').click()
            page.locator("[data-field-title]").filter(has_text="数据库锁定验收地块").wait_for(
                timeout=20_000
            )
            page.reload(wait_until="domcontentloaded")
            page.locator("[data-field-title]").filter(has_text="数据库锁定验收地块").wait_for(
                timeout=20_000
            )
            page.screenshot(path=args.output / "retry-saved.png", full_page=True)
            result.update(result="passed", input_preserved=True, retry_saved=True)
        except Exception as error:
            result["error"] = str(error)
            page.screenshot(path=args.output / "failure.png", full_page=True)
        finally:
            if lock is not None:
                lock.rollback()
                lock.close()
            browser.close()
    (args.output / "db-failure.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
