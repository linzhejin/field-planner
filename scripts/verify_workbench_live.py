"""Exercise live Open-Meteo and configured model through the HTML workbench."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright


def _repo_path(path: Path) -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8549")
    parser.add_argument("--field-name")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)

    browser_errors: list[str] = []
    queried_at = datetime.now(UTC).isoformat()
    result: dict[str, object] = {
        "queried_at": queried_at,
        "field_name": args.field_name,
        "weather_online_geocoding": False,
        "weather_forecast_success": False,
        "forecast_days": 0,
        "agent_response_received": False,
        "agent_tool_count": 0,
        "browser_errors": browser_errors,
        "screenshots": _repo_path(args.screenshots),
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: browser_errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.locator("[data-app]").wait_for(timeout=20_000)
        field_title = page.locator("[data-field-title]")
        field_title.wait_for(timeout=15_000)
        actual_field_name = field_title.inner_text()
        if args.field_name:
            field_title.filter(has_text=args.field_name).wait_for(timeout=15_000)
            actual_field_name = args.field_name
        result["field_name"] = actual_field_name

        page.locator('[data-action="weather"]').click()
        page.locator('[data-form="location-search"] input[name="query"]').fill("武汉")
        page.locator('[data-form="location-search"] button[type="submit"]').click()
        try:
            page.locator(".location-choice").first.wait_for(timeout=30_000)
        except PlaywrightTimeoutError:
            result["weather_error"] = page.locator("[data-toast]").inner_text()
            page.screenshot(
                path=args.screenshots / "live-weather-search-failed.png", full_page=True
            )
        online_candidates = (
            page.locator(".location-choice").filter(has_text="在线地名服务")
            if page.locator(".location-choice").count()
            else page.locator(".location-choice")
        )
        result["weather_online_geocoding"] = online_candidates.count() > 0
        if online_candidates.count() > 0:
            selected_location = online_candidates.first.inner_text().splitlines()[0]
            online_candidates.first.click()
            page.locator('[data-toast][data-show="true"]').filter(
                has_text="天气查询点已保存"
            ).wait_for(timeout=15_000)
            page.locator('[data-action="fetch-weather"]').click()
            try:
                page.locator('[data-toast][data-show="true"]').filter(has_text="已获取").wait_for(
                    timeout=30_000
                )
                result["weather_forecast_success"] = True
                result["selected_location_label"] = selected_location
                result["forecast_days"] = page.locator(".forecast-row").count()
                result["weather_component_state"] = page.locator("[data-app]").evaluate(
                    """app => {
                        const button = app.querySelector('[data-action="close-dialog"]');
                        const data = app._workbenchData || {};
                        const ui = app._workbenchUi || {};
                        return {
                            pending_operation_id: ui.pendingOperationId || '',
                            action_result_id: data.action_result?.operation_id || '',
                            close_button_disabled: Boolean(button?.disabled),
                            close_button_restore_value: button?.dataset.actionWasDisabled || ''
                        };
                    }"""
                )
                page.screenshot(path=args.screenshots / "live-weather-result.png", full_page=True)
            except PlaywrightTimeoutError:
                result["weather_error"] = page.locator("[data-toast]").inner_text()
        else:
            result.setdefault("weather_error", "地点搜索未返回在线地名候选")
        if page.locator("[data-dialog][open]").count():
            close_button = page.locator('[data-action="close-dialog"]')
            try:
                expect(close_button).to_be_enabled(timeout=5_000)
                close_button.click()
            except AssertionError:
                result["weather_close_button_disabled"] = True
                page.keyboard.press("Escape")
                page.locator("[data-dialog][open]").wait_for(state="hidden", timeout=5_000)

        message = "请读取当前计划，简要说明今天的任务。只读查询。"
        page.locator("#message-input").fill(message)
        page.locator("[data-form=message] .send-button").click()
        try:
            page.locator("[data-messages] .message.assistant").first.wait_for(timeout=60_000)
            state = page.locator("[data-app]").evaluate(
                """app => {
                    const data = app._workbenchData || {};
                    return {
                        agent_status: data.agent_status || '',
                        agent_busy: Boolean(data.agent_busy),
                        messages: (data.messages || []).map(row => ({
                            role: row.role,
                            content_length: String(row.content || '').length
                        }))
                    };
                }"""
            )
            result["agent_view_state"] = state
            status = state["agent_status"] or page.locator("[data-agent-status]").inner_text()
            match = re.search(r"(\d+) 个工具结果", status)
            result["agent_tool_count"] = int(match.group(1)) if match else 0
            assistant_text = page.locator("[data-messages] .message.assistant").last.inner_text()
            result["agent_reply_nonempty"] = bool(assistant_text.strip())
            result["agent_response_status"] = (
                state["agent_status"].rsplit("·", 1)[-1].strip() if state["agent_status"] else ""
            )
            result["agent_response_received"] = bool(
                result["agent_tool_count"]
                and assistant_text.strip()
                and result["agent_response_status"] == "success"
            )
            result["agent_status"] = status
            page.screenshot(path=args.screenshots / "live-agent-result.png", full_page=True)
        except PlaywrightTimeoutError:
            result["agent_error"] = "真实模型在 60 秒内未返回可展示结果"
            result["agent_status"] = page.locator("[data-agent-status]").inner_text()
            result["agent_view_state"] = page.locator("[data-app]").evaluate(
                """app => {
                    const data = app._workbenchData || {};
                    return {
                        agent_status: data.agent_status || '',
                        agent_busy: Boolean(data.agent_busy),
                        messages: (data.messages || []).map(row => ({
                            role: row.role,
                            content_length: String(row.content || '').length
                        }))
                    };
                }"""
            )
            page.screenshot(path=args.screenshots / "live-agent-timeout.png", full_page=True)

        result["browser_errors"] = browser_errors
        result["result"] = (
            "passed"
            if result["weather_online_geocoding"]
            and result["weather_forecast_success"]
            and result["forecast_days"] == 7
            and result["agent_response_received"]
            and not browser_errors
            else "incomplete"
        )
        browser.close()

    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.report.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
