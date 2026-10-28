"""Exercise synthetic model failures through independent desktop workbenches."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "auth": ("模型认证失败", 401),
    "rate": ("模型服务限流", 429),
    "timeout": ("助手响应超时", 408),
    "connection": ("助手暂时连不上", 0),
}


class StubHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path.startswith("/timeout"):
            time.sleep(4)
        status = 401 if self.path.startswith("/auth") else 429
        payload = b'{"error":{"message":"synthetic test failure"}}'
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        for name, (expected, _) in CASES.items():
            port = unused_port()
            reserved = socket.socket()
            reserved.bind(("127.0.0.1", 0))
            closed_port = reserved.getsockname()[1]
            env = os.environ.copy()
            env.update(
                NONGSHI_DATABASE_PATH=str(args.output / f"{name}.sqlite3"),
                NONGSHI_DEFAULT_MODE="real",
                NONGSHI_LLM_API_KEY="synthetic-test-key",
                NONGSHI_LLM_TIMEOUT_SECONDS="2" if name == "timeout" else "10",
                NONGSHI_LLM_BASE_URL=(
                    f"http://127.0.0.1:{closed_port}"
                    if name == "connection"
                    else f"http://127.0.0.1:{stub.server_port}/{name}"
                ),
                NO_PROXY="127.0.0.1,localhost",
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
                page.locator("[data-app]").wait_for(timeout=20_000)
                page.locator('[data-tasks] [data-action="profile"]').click()
                page.locator('[data-form="profile"] input[name="name"]').fill(f"故障验收 {name}")
                page.locator('[data-form="profile"] button[value="save"]').click()
                page.locator("[data-field-title]").filter(has_text=f"故障验收 {name}").wait_for(
                    timeout=15_000
                )
                page.locator("#message-input").fill("请说明当前安排")
                page.locator("[data-form=message] .send-button").click()
                reply = page.locator("[data-messages] .message.assistant").last
                reply.filter(has_text=expected).wait_for(timeout=25_000)
                page.screenshot(path=args.output / f"{name}.png", full_page=True)
                page.reload(wait_until="domcontentloaded")
                page.locator("[data-field-title]").filter(has_text=f"故障验收 {name}").wait_for(
                    timeout=20_000
                )
                result.update(result="passed", visible_message=expected, field_recovered=True)
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
                reserved.close()
        browser.close()
    stub.shutdown()
    (args.output / "model-faults.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item["result"] == "passed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
