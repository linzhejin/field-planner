"""Read-only browser smoke check for the current seven-day workbench.

Requires Playwright and an already running Streamlit app with a saved field.
Uses Playwright Chromium, or installed Microsoft Edge when its browser bundle is absent.
It does not create fields, call the model, or change saved plans. Example:

    python scripts/check_workbench_viewports.py --url http://127.0.0.1:8501 \
        --output-dir viewport-evidence
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

VIEWPORTS = [(1440, 900), (1158, 714), (900, 844), (390, 844), (360, 800)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Running Streamlit workbench URL")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            if "Executable doesn't exist" not in str(exc):
                raise
            browser = playwright.chromium.launch(headless=True, channel="msedge")
        for width, height in VIEWPORTS:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(args.url, wait_until="networkidle")
            if width < 1000:
                page.get_by_role("tab", name="计划结果").click()
            page.get_by_text("今日执行台", exact=False).first.wait_for()
            body = page.locator("body").inner_text()
            has_workbench = "今日执行台" in body
            day_heading = page.get_by_role(
                "heading", name=re.compile(r"^(今日|\d{2}月\d{2}日)安排$")
            )
            day_heading.wait_for(state="visible", timeout=15000)
            dates = page.locator('[class*="st-key-date_strip"] [data-testid="stButton"] button')
            date_count = dates.count()
            if date_count >= 2:
                dates.nth(1).click()
                page.wait_for_timeout(800)
                day_heading.wait_for(state="visible", timeout=15000)
            page.screenshot(path=str(args.output_dir / f"workbench_{width}x{height}.png"))
            geometry = page.evaluate(
                """() => ({
                  content: document.documentElement.scrollWidth,
                  viewport: document.documentElement.clientWidth,
                  smallButtons: [...document.querySelectorAll('button')]
                    .filter(el => !el.disabled && el.getBoundingClientRect().width > 0
                      && el.getBoundingClientRect().height > 0
                      && (el.getBoundingClientRect().width < 44
                        || el.getBoundingClientRect().height < 44))
                    .map(el => (el.innerText || el.ariaLabel || '').slice(0, 40))
                })"""
            )
            page.close()

            # A fresh browser session avoids relying on Streamlit's one-pass
            # button labels while switching back and forth between the views.
            week_page = browser.new_page(viewport={"width": width, "height": height})
            week_page.goto(args.url, wait_until="networkidle")
            if width < 1000:
                week_page.get_by_role("tab", name="计划结果").click()
            week_heading = week_page.get_by_role(
                "heading", name=re.compile(r"^(今日|\d{2}月\d{2}日)安排$")
            )
            week_heading.wait_for(state="visible", timeout=15000)
            week_page.get_by_role("button", name="整周清单").click()
            week_heading.wait_for(state="hidden", timeout=15000)
            week_page.wait_for_timeout(600)
            has_week = not week_heading.is_visible()
            week_page.close()
            rows.append(
                {
                    "viewport": f"{width}x{height}",
                    "workbench_visible": has_workbench,
                    "week_view_visible": has_week,
                    "date_buttons": date_count,
                    "ui_retries": 0,
                    "horizontal_overflow": geometry["content"] > geometry["viewport"] + 1,
                    "small_button_labels": geometry["smallButtons"],
                }
            )
        browser.close()

    (args.output_dir / "viewport_results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return int(
        any(
            not row["workbench_visible"]
            or not row["week_view_visible"]
            or row["date_buttons"] != 7
            or row["horizontal_overflow"]
            for row in rows
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
