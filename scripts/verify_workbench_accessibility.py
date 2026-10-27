"""Check core workbench keyboard behavior, visible text contrast, zoom, and mobile width."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

CONTRAST_AUDIT = """app => {
    const parse = value => {
        const match = String(value).match(/rgba?\\(([^)]+)\\)/);
        if (!match) return null;
        const parts = match[1].split(',').map(x => Number.parseFloat(x.trim()));
        return {rgb: parts.slice(0, 3), alpha: parts.length > 3 ? parts[3] : 1};
    };
    const composite = (top, bottom) => top
        ? top.rgb.map((value, index) => value * top.alpha + bottom[index] * (1 - top.alpha))
        : bottom;
    const luminance = rgb => {
        const linear = rgb.map(value => {
            const channel = value / 255;
            return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        });
        return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
    };
    const contrast = (foreground, background) => {
        const a = luminance(foreground), b = luminance(background);
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    };
    const rows = [];
    for (const selector of ['.eyebrow', '.day-week', '.day-count', '.muted-copy',
                            '.task-purpose', '.task-time small', '.composer-bottom']) {
        const element = [...app.querySelectorAll(selector)].find(item => {
            const box = item.getBoundingClientRect();
            return box.width > 0 && box.height > 0;
        });
        if (!element) continue;
        const style = getComputedStyle(element);
        const ancestors = [];
        for (let node = element; node && node instanceof Element; node = node.parentElement) {
            ancestors.push(parse(getComputedStyle(node).backgroundColor));
        }
        let background = [255, 255, 255];
        for (const layer of ancestors.reverse()) background = composite(layer, background);
        const foreground = parse(style.color);
        if (!foreground) continue;
        rows.push({
            selector,
            text: element.textContent.trim().slice(0, 40),
            contrast: Number(
                contrast(composite(foreground, background), background).toFixed(2)
            )
        });
    }
    const placeholder = app.querySelector('.composer textarea');
    if (placeholder) {
        const style = getComputedStyle(placeholder, '::placeholder');
        const foreground = parse(style.color);
        if (foreground) rows.push({
            selector: 'textarea::placeholder',
            contrast: Number(
                contrast(composite(foreground, [255,255,255]), [255,255,255]).toFixed(2)
            )
        });
    }
    return rows;
}"""


def _repo_path(path: Path) -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8551")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path, required=True)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.screenshots.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="domcontentloaded")
        app = page.locator("[data-app]")
        app.wait_for(timeout=20_000)

        history_button = app.locator('[data-action="history"]')
        history_button.focus()
        page.keyboard.press("Enter")
        dialog = app.locator("[data-dialog]")
        dialog.wait_for(state="visible", timeout=5_000)
        focus_inside_dialog = dialog.evaluate(
            "dialog => dialog.contains(dialog.getRootNode().activeElement)"
        )
        page.screenshot(path=args.screenshots / "keyboard-history-dialog.png", full_page=True)
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden", timeout=5_000)
        focus_returned = history_button.evaluate(
            "button => button === button.getRootNode().activeElement"
        )

        contrast_rows = app.evaluate(CONTRAST_AUDIT)
        low_contrast = [row for row in contrast_rows if row["contrast"] < 4.5]
        page.evaluate("document.documentElement.style.zoom = '200%'")
        page.wait_for_timeout(250)
        zoom_dimensions = page.evaluate(
            """() => ({client: document.documentElement.clientWidth,
                        scroll: document.documentElement.scrollWidth})"""
        )
        page.screenshot(path=args.screenshots / "zoom-200.png", full_page=True)
        zoom_overflow = zoom_dimensions["scroll"] > zoom_dimensions["client"] + 1

        page.evaluate("document.documentElement.style.zoom = '100%'")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(250)
        mobile_dimensions = app.evaluate(
            """node => ({client: node.clientWidth, scroll: node.scrollWidth,
                         document: document.documentElement.scrollWidth})"""
        )
        mobile_overflow = (
            mobile_dimensions["scroll"] > mobile_dimensions["client"] + 1
            or mobile_dimensions["document"] > 391
        )
        page.screenshot(path=args.screenshots / "mobile-390.png", full_page=True)
        assert focus_inside_dialog, "打开历史面板后键盘焦点没有进入面板"
        assert focus_returned, "关闭历史面板后键盘焦点没有返回触发按钮"
        assert contrast_rows and not low_contrast, f"存在低于 4.5:1 的可见文字：{low_contrast}"
        assert not zoom_overflow, f"200% 缩放后页面横向溢出：{zoom_dimensions}"
        assert not mobile_overflow, f"390px 页面横向溢出：{mobile_dimensions}"
        assert not errors, f"浏览器错误：{errors}"
        browser.close()

    result = {
        "result": "passed",
        "checks": {
            "keyboard_focus_entered_dialog": focus_inside_dialog,
            "keyboard_focus_returned_to_trigger": focus_returned,
            "visible_text_contrast_minimum": min(row["contrast"] for row in contrast_rows),
            "visible_text_contrast_rows": contrast_rows,
            "zoom_200_dimensions": zoom_dimensions,
            "zoom_200_horizontal_overflow": zoom_overflow,
            "mobile_390_dimensions": mobile_dimensions,
            "mobile_horizontal_overflow": mobile_overflow,
        },
        "browser_errors": errors,
        "screenshots": _repo_path(args.screenshots),
    }
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
