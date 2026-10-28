"""Verify that the adopted workbench history and exports survive an app restart."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def _repo_path(path: Path) -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8549")
    parser.add_argument("--field-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)
    browser_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.locator("[data-app]").wait_for(timeout=20_000)
        if args.field_name not in page.locator("[data-field-title]").inner_text():
            page.locator('[data-action="select-field"]').filter(has_text=args.field_name).click()
        page.locator("[data-field-title]").filter(has_text=args.field_name).wait_for(timeout=15_000)
        state = page.locator("[data-app]").evaluate(
            """app => ({
                field: app._workbenchData?.field || null,
                plan: app._workbenchData?.plan || null,
                history: app._workbenchData?.history || []
            })"""
        )
        assert state["plan"] is not None, "重启后未恢复已采用计划"
        latest_version = state["plan"]["version"]
        if state["field"]["plan_stale"]:
            page.get_by_text("地块情况变化，计划待更新", exact=True).wait_for(timeout=15_000)
        else:
            page.get_by_text(f"已采用 · V{latest_version}", exact=True).wait_for(timeout=15_000)
        page.screenshot(path=args.screenshots / "restarted-adopted-plan.png", full_page=True)

        page.locator('[data-action="history"]').click()
        page.locator("[data-dialog][open]").wait_for(timeout=5_000)
        history_text = page.locator("[data-dialog-body]").inner_text()
        for version in (f"V{item['version']} ·" for item in state["history"]):
            assert version in history_text, f"重启后历史中缺少 {version}"
        page.screenshot(path=args.screenshots / "restarted-history.png", full_page=True)
        page.locator('[data-action="close-dialog"]').click()

        page.locator('[data-action="export"]').click()
        page.locator("[data-dialog][open]").wait_for(timeout=5_000)
        page.screenshot(path=args.screenshots / "restarted-export-panel.png", full_page=True)
        with page.expect_download() as download_info:
            page.locator('[data-action="download-json"]').click()
        json_download = download_info.value
        json_path = args.output.with_name(f"{args.output.stem}_export.json")
        json_download.save_as(json_path)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["field"]["draft"]["profile"]["name"] == args.field_name
        assert payload["adopted_plan"] is not None
        assert len(payload["history"]) == len(state["history"])

        with page.expect_download() as download_info:
            page.locator('[data-action="download-markdown"]').click()
        markdown_download = download_info.value
        markdown_path = args.output.with_name(f"{args.output.stem}_export.md")
        markdown_download.save_as(markdown_path)
        markdown = markdown_path.read_text(encoding="utf-8")
        assert args.field_name in markdown
        assert "## 七天安排" in markdown
        assert not browser_errors, f"浏览器脚本错误：{browser_errors}"

        result = {
            "result": "passed",
            "restarted_app": True,
            "field_name": args.field_name,
            "history_versions": [item["version"] for item in state["history"]],
            "plan_stale_after_weather_refresh": state["field"]["plan_stale"],
            "json_export": _repo_path(json_path),
            "markdown_export": _repo_path(markdown_path),
            "browser_errors": browser_errors,
            "screenshots": _repo_path(args.screenshots),
        }
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
