"""Drive the ported Git workspace in a REAL browser against a live server.

Upstream's gitworkspace_browser_probe.py intercepts every request and forwards
to a TestClient; it also builds work items, so it cannot run in this tree. This
probe is the blunter thing that still answers the question the ported unit
tests cannot: does the panel actually open, paint and read a real repository
when a person clicks the button?

Uses the Chrome already on the machine (channel="chrome") rather than
downloading a Chromium.

    python frontend/tests/gitworkspace_live_probe.py --base http://127.0.0.1:7361 \
        --slug gitcheck --out <dir>

Exits non-zero on the first failed expectation, and always writes a screenshot
so a failure is inspectable rather than merely reported.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright


def run(base: str, slug: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    console: list[str] = []

    def check(ok: bool, why: str) -> None:
        if not ok:
            problems.append(why)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        page.goto(f"{base}/o/{slug}", wait_until="networkidle")
        page.screenshot(path=str(out / "01-canvas.png"))

        # the ⑂ button in the org header is this tree's entry point; upstream
        # opens from a docket button that does not exist here
        opener = page.locator("header button", has_text="⑂").first
        check(opener.count() > 0, "no ⑂ button in the org header")
        if opener.count() == 0:
            browser.close()
            return report(problems, console, out)

        opener.click()
        page.wait_for_selector(".git-workspace", timeout=15_000)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "02-opened.png"))

        panel = page.locator(".git-workspace")
        check(panel.count() == 1, "the .git-workspace panel did not open")
        # §1 of githost.test.tsx, now against real CSS: both classes would race
        check("settings" not in (panel.get_attribute("class") or ""),
              ".settings is applied alongside .git-workspace")

        box = panel.bounding_box()
        check(bool(box) and box["width"] > 600 and box["height"] > 400,
              f"the panel has no usable size: {box}")

        # the repository the API registered must be selectable, and the graph
        # must actually paint — an empty viewport is the failure this is for
        check(page.locator(".git-head select").count() > 0,
              "no repository selector in .git-head")
        options = page.locator(".git-head select option").all_inner_texts()
        check(any("opentree" in o for o in options),
              f"the registered repository is not offered: {options}")

        page.wait_for_timeout(3000)
        page.screenshot(path=str(out / "03-graph.png"), full_page=False)
        nodes = page.locator(".git-node").count()
        check(nodes > 0, "the graph painted no commit nodes")

        viewport = page.locator(".git-viewport")
        check(viewport.count() > 0, "no .git-viewport")

        # the close button the frame deliberately does NOT draw itself
        close = page.locator(".git-close")
        check(close.count() == 1, "no .git-close button in the workspace header")
        if close.count() == 1:
            close.click()
            page.wait_for_timeout(600)
            check(page.locator(".git-workspace").count() == 0,
                  "the panel did not close on its own close button")
            page.screenshot(path=str(out / "04-closed.png"))

        print(f"commit nodes painted: {nodes}")
        browser.close()

    return report(problems, console, out)


def report(problems: list[str], console: list[str], out: Path) -> int:
    bad = [c for c in console
           if c.startswith("pageerror") or c.startswith("error:")]
    if bad:
        print("\nconsole errors:")
        for c in bad[:20]:
            print("   ", c)
    if problems:
        print("\nFAILED:")
        for w in problems:
            print("   ✗", w)
        print(f"\nscreenshots in {out}")
        return 1
    print(f"\nall checks passed — screenshots in {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:7361")
    ap.add_argument("--slug", default="gitcheck")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    sys.exit(run(a.base, a.slug, a.out))
