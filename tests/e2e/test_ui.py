"""Browser test: the operator journey through the real UI against a live server.

Run with `uv run pytest -m e2e`. Needs `uv run playwright install chromium` once.
Screenshots land in reports/screenshots/ (also on failure) for review and CI artifacts.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e
playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
SHOTS = ROOT / "reports" / "screenshots"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    port = _free_port()
    env = {**os.environ, "CLOUDOPS_DB_PATH": str(tmp_path_factory.mktemp("e2e") / "runs.sqlite3"),
           "CLOUDOPS_PLANNER": "rules", "CLOUDOPS_LOG_LEVEL": "WARNING"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.api.main:app", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/healthz", timeout=0.5)
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("server did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def page(base_url, request):
    SHOTS.mkdir(parents=True, exist_ok=True)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto(base_url)
        yield pg
        rep = getattr(request.node, "rep_call", None)
        if rep is not None and rep.failed:
            pg.screenshot(path=SHOTS / f"FAILED-{request.node.name}.png", full_page=True)
        browser.close()


def test_operator_starts_run_reviews_and_approves(page):
    expect = playwright.expect
    page.get_by_test_id("fault-cloud_api_error_once").check()  # approval must survive one 429
    page.get_by_test_id("start-run").click()

    expect(page.get_by_test_id("run-status")).to_have_text("awaiting_approval", timeout=10_000)
    rows = page.get_by_test_id("proposal-row")
    expect(rows).to_have_count(3)
    for iid in ("i-dev-001", "i-dev-003", "i-stg-002"):
        expect(page.get_by_test_id("proposal")).to_contain_text(iid)
    expect(page.get_by_test_id("proposal")).not_to_contain_text("i-prd-001")
    expect(page.get_by_test_id("findings")).to_contain_text("prompt_injection")
    page.screenshot(path=SHOTS / "01-awaiting-approval.png", full_page=True)

    page.get_by_test_id("comment").fill("approved in e2e test")
    page.get_by_test_id("approve").click()

    expect(page.get_by_test_id("run-status")).to_have_text("completed", timeout=10_000)
    for iid in ("i-dev-001", "i-dev-003", "i-stg-002"):
        expect(page.get_by_test_id(f"result-{iid}")).to_have_text("stopped")
    page.screenshot(path=SHOTS / "02-approved.png", full_page=True)


def test_viewer_cannot_start_a_run(page):
    page.get_by_test_id("api-key").select_option("viewer-demo-key")
    page.get_by_test_id("start-run").click()
    playwright.expect(page.get_by_test_id("form-error")).to_contain_text("may not perform", timeout=5_000)
    page.screenshot(path=SHOTS / "03-viewer-forbidden.png", full_page=True)
