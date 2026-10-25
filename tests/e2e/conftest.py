"""Browser test harness: builds dist/, serves it with production headers and
drives the real app in headless Chrome via Playwright.

Note: the app's CSP forbids eval, so Playwright's wait_for_function (which
uses it) can't be used; App.wait polls with page.evaluate instead.
"""
import os
import socket
import subprocess
import sys
import time

import pytest
from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
SETUP_EXTS = {".parquet", ".pq", ".parq", ".csv", ".tsv", ".txt", ".jsonl", ".ndjson"}
LARGE = os.path.join(ROOT, "tests", "large")
ARTIFACTS = os.path.join(ROOT, "tests", "e2e", "artifacts")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server():
    subprocess.run([sys.executable, "build.py"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    port = free_port()
    proc = subprocess.Popen([sys.executable, "serve.py", "--port", str(port), "--host", "127.0.0.1"], cwd=ROOT,
                            stdout=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield {"base": f"http://127.0.0.1:{port}", "port": port}
    proc.terminate()
    proc.wait()


@pytest.fixture(scope="session")
def browser_ctx(tmp_path_factory):
    # One persistent profile so the ~30 MB Pyodide download is cached across tests.
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(tmp_path_factory.mktemp("profile")), channel="chrome", headless=True,
            viewport={"width": 1280, "height": 900},
        )
        yield ctx
        ctx.close()


class App:
    def __init__(self, page, base):
        self.page = page
        self.base = base

    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def wait(self, cond, timeout=60, what=None):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(cond):
                return
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what or cond}; status={self.status()!r} "
                             f"load-error={self.text('#load-error')!r}")

    def status(self):
        return self.js("document.getElementById('status-text').textContent")

    def text(self, sel):
        return self.js("(s) => { const e = document.querySelector(s); return e ? e.textContent : null }", sel)

    def visible(self, sel):
        return self.js("(s) => { const e = document.querySelector(s); return !!e && !e.closest('[hidden]') }", sel)

    def open(self, path="/"):
        self.page.goto(self.base + path)
        self.wait_ready()

    def wait_ready(self, timeout=240):
        self.wait("document.getElementById('status').dataset.state === 'ready'", timeout, "Python to start")

    def idle(self, timeout=60):
        self.wait("!document.body.classList.contains('busy') && document.getElementById('status').dataset.state === 'ready'",
                  timeout, "the app to go idle")

    def load(self, path, key=None, timeout=60, budget=None):
        if budget is not None:
            self.page.select_option("#budget", str(budget))
        name = os.path.basename(path)
        self.page.set_input_files("#file-input", path)
        if os.path.splitext(name)[1].lower() in SETUP_EXTS:
            # parquet / csv / jsonl: the "choose what to load" step comes first; take the defaults
            self.wait_setup(timeout)
            self.page.click("#opt-load")
        if key is not None:
            self.wait("!document.getElementById('key-picker').hidden || !document.getElementById('load-error').hidden",
                      timeout, "key picker")
            assert not self.visible("#load-error"), self.text("#load-error")
            self.page.select_option("#key-select", key)
            self.page.click("#key-load")
        self.wait(f"""(document.getElementById('file-name').textContent.startsWith({name!r}) &&
                      !document.getElementById('explore').hidden) || !document.getElementById('load-error').hidden""",
                  timeout, f"{name} to load")
        assert not self.visible("#load-error"), self.text("#load-error")
        self.idle(timeout)

    def wait_setup(self, timeout=60):
        """Wait for the column / row options shown before a load (inline, or a dialog when data is open)."""
        self.wait("(!document.getElementById('setup-panel').hidden || document.getElementById('options-dialog').open)"
                  " || !document.getElementById('load-error').hidden", timeout, "load options")
        assert not self.visible("#load-error"), self.text("#load-error")

    def run(self, expr, timeout=60):
        self.page.fill("#expr", expr)
        self.page.click("#run")
        self.idle(timeout)

    def tab(self, name, timeout=60):
        self.page.click(f'[data-tab="{name}"]')
        self.idle(timeout)

    def table(self, sel="#data-table"):
        return self.js("""(s) => {
            const t = document.querySelector(s);
            const head = [...t.querySelectorAll('thead th')].map(th => (th.querySelector('.sort > span') || th).childNodes[0]?.textContent ?? th.textContent);
            const rows = [...t.querySelectorAll('tbody tr')].map(tr => [...tr.children].map(td => td.textContent));
            return {head, rows};
        }""", sel)

    def set_plot_y(self, positions):
        """Choose exactly these Y columns with the dropdowns, like a user: remove
        the existing rows, then "+ Add Y" and pick a column for each."""
        while self.js("document.querySelectorAll('#plot-ys .yrow').length"):
            self.page.click("#plot-ys .yrow:first-child button")
        for k, pos in enumerate(positions, start=1):
            self.page.click("#plot-y-add")
            self.page.select_option(f"#plot-ys .yrow:nth-child({k}) select", str(pos))
        got = self.js("[...document.querySelectorAll('#plot-ys select')].map(s => Number(s.value))")
        assert got == list(positions), got

    def sandbox_frame(self):
        for f in self.page.frames:
            if f.url.endswith("/sandbox.html"):
                return f
        raise AssertionError("sandbox frame not found")


@pytest.fixture
def make_app(server, browser_ctx):
    pages = []

    def make(path="/", init_script=None, viewport=None):
        page = browser_ctx.new_page()
        if viewport:
            page.set_viewport_size(viewport)
        if init_script:
            page.add_init_script(init_script)
        errors = []
        csp = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: csp.append(m.text) if "Content Security Policy" in m.text else None)
        app = App(page, server["base"])
        app.errors = errors
        app.csp_violations = csp
        app.allow_csp_violations = False
        pages.append(app)
        app.open(path)
        return app

    yield make
    for app in pages:
        try:
            app.js("localStorage.clear()")
        except Exception:
            pass
        app.page.close()
        assert not app.errors, app.errors
        # the app itself must never trip its own CSP (tests that probe it opt out)
        assert app.allow_csp_violations or not app.csp_violations, app.csp_violations


@pytest.fixture
def app(make_app):
    return make_app()


def fixture_path(name):
    return os.path.join(FIXTURES, name)
