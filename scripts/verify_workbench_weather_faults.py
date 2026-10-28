"""Check weather failures using a test-only HTTP transport and the real desktop page."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HOOK = """import os
import httpx
from nongshi.weather import OpenMeteoService
original = OpenMeteoService.__init__
def response(request):
    if "geocoding-api" in request.url.host:
        result = {"id": 1, "name": "验收地点", "latitude": 30.5,
                  "longitude": 114.3, "country_code": "CN", "feature_code": "PPLA"}
        return httpx.Response(200, json={"results": [result]})
    if os.environ["NONGSHI_WEATHER_TEST"] == "timeout":
        raise httpx.ReadTimeout("synthetic timeout", request=request)
    return httpx.Response(200, json={"timezone": "Asia/Shanghai", "daily": {"time": []}})
def init(self, *args, **kwargs):
    kwargs["transport"] = httpx.MockTransport(response)
    original(self, *args, **kwargs)
OpenMeteoService.__init__ = init
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    hook_dir = args.output / "test-hook"
    hook_dir.mkdir(exist_ok=True)
    (hook_dir / "sitecustomize.py").write_text(HOOK, encoding="utf-8")
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        for name, expected in (("timeout", "天气查询超时"), ("missing", "天气预报缺少日期")):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = os.environ.copy()
            env.update(
                NONGSHI_DATABASE_PATH=str(args.output / f"{name}.sqlite3"),
                NONGSHI_DEFAULT_MODE="real",
                NONGSHI_WEATHER_TEST=name,
                PYTHONPATH=str(hook_dir),
            )
            process = subprocess.Popen(
                [
                    str(ROOT / ".venv" / "Scripts" / "python.exe"),
                    "-m",
                    "streamlit",
                    "run",
                    str(ROOT / "src" / "nongshi" / "app.py"),
                    "--server.port",
                    str(port),
                    "--server.headless",
                    "true",
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            result = {"case": name, "result": "failed"}
            try:
                for attempt in range(40):
                    try:
                        page.goto(
                            f"http://127.0.0.1:{port}", wait_until="domcontentloaded", timeout=1500
                        )
                        break
                    except Exception:
                        if attempt == 39:
                            raise
                        time.sleep(0.5)
                app = page.locator("[data-app]")
                app.wait_for(timeout=20_000)
                page.locator('[data-tasks] [data-action="profile"]').click()
                page.locator('[data-form="profile"] input[name="name"]').fill(f"天气故障 {name}")
                page.locator('[data-form="profile"] button[value="save"]').click()
                page.locator("[data-field-title]").filter(has_text=f"天气故障 {name}").wait_for(
                    timeout=15_000
                )
                page.locator('[data-action="weather"]').click()
                page.locator('[data-form="location-search"] input[name="query"]').fill("testplace")
                page.locator('[data-form="location-search"] button[type="submit"]').click()
                page.locator(".location-choice").first.wait_for(timeout=15_000)
                page.locator(".location-choice").first.click()
                page.locator('[data-action="fetch-weather"]').wait_for(timeout=15_000)
                page.locator('[data-action="fetch-weather"]').click()
                page.locator('[data-toast][data-show="true"]').filter(has_text=expected).wait_for(
                    timeout=20_000
                )
                data = app.evaluate("app => app._workbenchData")
                assert not data["forecast"] and data["field"]["fact_version"] == 0
                page.screenshot(path=args.output / f"{name}.png", full_page=True)
                result.update(result="passed", visible_message=expected, no_facts_created=True)
            except Exception as error:
                result["error"] = str(error)
                page.screenshot(path=args.output / f"{name}-failure.png", full_page=True)
            finally:
                results.append(result)
                page.close()
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        browser.close()
    (args.output / "weather-faults.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item["result"] == "passed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
