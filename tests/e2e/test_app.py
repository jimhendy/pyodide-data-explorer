"""End-to-end tests of the real app in headless Chrome (fixtures: tests/fixtures)."""
import os
import re
import time
import urllib.request

import pytest

from conftest import ARTIFACTS, fixture_path


def headers(url):
    with urllib.request.urlopen(url) as r:
        return dict(r.headers)


# --- boot, headers ----------------------------------------------------------

def test_security_headers(server):
    h = headers(server["base"] + "/")
    csp = h["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "style-src 'self' 'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='" in csp  # empty-string hash only
    s = headers(server["base"] + "/sandbox.html")["Content-Security-Policy"]
    assert "sandbox allow-scripts" in s and "allow-same-origin" not in s
    assert "connect-src https://cdn.jsdelivr.net/pyodide/v314.0.7/full/" in s
    assert "frame-ancestors 'self'" in s
    for path in ("/", "/app.js", "/sandbox.js"):
        assert "Set-Cookie" not in headers(server["base"] + path)


def test_boots_python(app):
    assert re.match(r"Ready · Python 3\.\d+\.\d+ · pandas 3\.", app.status())


# --- loading every format ---------------------------------------------------

@pytest.mark.parametrize("name,key,rows", [
    ("sample.csv", None, 500),
    ("sample.tsv", None, 500),
    ("records.json", None, 500),
    ("lines.jsonl", None, 500),
    ("sample.parquet", None, 500),
    ("indexed.parquet", None, 500),
    ("sample.xlsx", "Data", 200),
    ("sample.h5", "/pandas_fixed", 500),
    ("sample.h5", "/nested/pandas_table", 500),
    ("sample.h5", "/raw/records", 5),
])
def test_load_format(app, name, key, rows):
    app.load(fixture_path(name), key=key)
    assert f"{rows:,} rows" in app.text("#file-meta")
    t = app.table()
    assert len(t["rows"]) == min(rows, 50)
    if key != "/raw/records":
        assert "value" in t["head"]
    assert not app.visible("#sample-note")


def test_h5_key_picker_lists_datasets(app):
    app.page.set_input_files("#file-input", fixture_path("sample.h5"))
    app.wait("!document.getElementById('key-picker').hidden")
    options = app.js("[...document.querySelectorAll('#key-select option')].map(o => o.value)")
    assert {"/pandas_fixed", "/pandas_series", "/raw/matrix", "/raw/vector"} <= set(options)
    assert not any("_i_table" in o for o in options)


def test_malicious_h5_pickle_refused(app):
    app.page.set_input_files("#file-input", fixture_path("malicious.h5"))
    app.wait("!document.getElementById('key-picker').hidden")
    app.page.select_option("#key-select", "/f")
    app.page.click("#key-load")
    app.wait("!document.getElementById('load-error').hidden")
    assert "refusing to load" in app.text("#load-error")


# --- table ------------------------------------------------------------------

def test_table_sort_tail_random(app):
    app.load(fixture_path("sample.csv"))
    head = app.table()["head"]
    col = head.index("value")
    sel = f"#data-table thead th:nth-child({col + 1}) button"
    app.page.click(sel)
    app.idle()
    assert app.js(f"document.querySelector('#data-table thead th:nth-child({col + 1})').getAttribute('aria-sort')") == "ascending"
    vals = [float(r[col]) for r in app.table()["rows"]]
    assert vals == sorted(vals)
    app.page.click(sel)
    app.idle()
    vals = [float(r[col]) for r in app.table()["rows"]]
    assert vals == sorted(vals, reverse=True)
    app.page.click(sel)  # third click clears sort
    app.idle()
    app.page.click('[data-mode="tail"]')
    app.idle()
    assert app.table()["rows"][-1][0] == "499"
    app.page.click('[data-mode="sample"]')
    app.idle()
    assert app.visible("#resample")
    first = app.table()["rows"]
    app.page.click("#resample")
    app.idle()
    assert app.table()["rows"] != first
    app.page.select_option("#table-n", "10")
    app.idle()
    assert len(app.table()["rows"]) == 10


# --- expressions --------------------------------------------------------------

def test_expression_flow(app):
    app.load(fixture_path("sample.csv"))
    app.run('df[df.group.eq("eg")].T')
    assert "Showing an expression result" in app.text("#view-note")
    assert not app.js("document.getElementById('reset').disabled")
    t = app.table()
    assert t["rows"][0][0] == "id"  # transposed: index is the old columns
    # chained on the current view
    app.run("df.shape")
    assert app.text("#expr-out").strip().startswith("(7, ")
    # history: newest first, clicking fills but does not run
    items = app.js("[...document.querySelectorAll('#history-list button')].map(b => b.textContent)")
    assert items == ["df.shape", 'df[df.group.eq("eg")].T']
    app.page.click("#history summary")
    app.page.click("#history-list li:nth-child(2) button")
    assert app.js("document.getElementById('expr').value") == 'df[df.group.eq("eg")].T'
    assert app.text("#expr-out").strip().startswith("(7, ")  # nothing ran
    app.page.click("#reset")
    app.idle()
    assert "500 rows × 7 columns ·" in app.text("#view-note")
    assert app.js("document.getElementById('reset').disabled")


def test_expression_errors_are_shown(app):
    app.load(fixture_path("sample.csv"))
    app.run("df.no_such_column")
    assert app.js("document.getElementById('expr-out').classList.contains('is-error')")
    assert "AttributeError" in app.text("#expr-out")
    app.run("df[")
    assert "SyntaxError" in app.text("#expr-out")
    app.run("print('hello')\nlen(df)")
    assert app.text("#expr-out") == "hello\n500"
    assert len(app.table()["rows"]) == 50  # app still works


def test_ctrl_enter_runs(app):
    app.load(fixture_path("sample.csv"))
    app.page.fill("#expr", "df.head(3)")
    app.page.press("#expr", "Control+Enter")
    app.idle()
    assert len(app.table()["rows"]) == 3


# --- stats ------------------------------------------------------------------

def test_column_stats(app):
    app.load(fixture_path("sample.csv"))
    app.tab("columns")
    t = app.table("#col-table")
    assert t["head"][:6] == ["column", "dtype", "count", "nulls", "null %", "unique"]
    assert {"idxmin", "idxmax", "mean", "std"} <= set(t["head"])
    rows = {r[0]: r for r in t["rows"]}
    assert rows["value"][1] == "float64"
    assert rows["value"][3] == "72"  # every 7th of 500 is NaN
    assert rows["when"][1].startswith("datetime64")


def test_parquet_shows_exact_file_stats_when_sampled(app):
    # 1 MB budget isn't selectable in the UI; use the smallest option on a
    # file that doesn't fit, checked in test_large. Here: no file stats unsampled.
    app.load(fixture_path("sample.parquet"))
    app.tab("columns")
    assert "file min" not in app.table("#col-table")["head"]


def test_overall_stats(app):
    app.load(fixture_path("sample.csv"))
    app.tab("overview")
    text = app.text("#overview")
    assert "Rows500" in text.replace(",", "") and "Columns7" in text
    assert "Null cells" in text and "Python heap" in text
    assert "none — whole file loaded" in text


# --- plots ------------------------------------------------------------------

@pytest.mark.parametrize("kind,x,ys,color", [
    ("scatter", "value", ["count"], None),
    ("line", "when", ["value", "count"], None),
    ("histogram", None, ["value"], "ok"),
    ("bar", "group", ["count"], None),
    ("box", "group", ["value"], None),
])
def test_plots(app, kind, x, ys, color):
    app.load(fixture_path("sample.csv"))
    app.tab("plot")
    cols = app.js("[...document.querySelectorAll('#plot-x option')].slice(2).map(o => o.textContent.split('  · ')[0])")

    def opt(name):
        return str(cols.index(name)) if name else ""

    app.page.select_option("#plot-kind", kind)
    app.page.select_option("#plot-x", opt(x))
    app.page.select_option("#plot-color", opt(color))
    app.set_plot_y([cols.index(y) for y in ys])
    app.page.click("#plot-draw")
    app.idle()
    app.wait("document.querySelector('#plot .main-svg') !== null", 30, "plotly svg")
    ntraces = app.js("document.getElementById('plot').data.length")
    assert ntraces >= len(ys)
    assert app.js("document.querySelectorAll('#plot .trace, #plot .point, #plot .bars, #plot .boxes').length") > 0


# --- security -----------------------------------------------------------------

RECORD_MESSAGES = """
if (location.pathname.endsWith('/sandbox.html')) {
  window.__seen = [];
  window.addEventListener('message', (e) => { window.__seen.push(e.data && e.data.type); }, true);
}
"""


def test_url_parameters_never_execute(make_app):
    app = make_app("/?expr=df.head()&run=1&code=__import__('os')#df.describe()", init_script=RECORD_MESSAGES)
    app.load(fixture_path("sample.csv"))
    assert app.js("document.getElementById('expr').value") == ""
    assert app.js("document.querySelectorAll('#history-list li').length") == 0
    assert app.js("document.getElementById('expr-out').hidden")
    seen = app.sandbox_frame().evaluate("window.__seen")
    assert "eval" not in seen and "inspect" in seen and "load" in seen
    # and the sandbox frame itself never carries the query string
    assert app.sandbox_frame().url.endswith("/sandbox.html")


def test_sandbox_frame_is_isolated(app):
    f = app.sandbox_frame()
    r = f.evaluate("""() => {
        const out = {};
        out.origin = self.origin;
        try { out.parentDoc = !!window.parent.document; } catch (e) { out.parentDoc = e.name; }
        try { out.storage = typeof localStorage.length; } catch (e) { out.storage = e.name; }
        try { out.cookie = document.cookie; } catch (e) { out.cookie = e.name; }
        try { window.parent.location.href; out.parentLoc = 'readable'; } catch (e) { out.parentLoc = e.name; }
        return out;
    }""")
    assert r == {"origin": "null", "parentDoc": "SecurityError", "storage": "SecurityError",
                 "cookie": "SecurityError", "parentLoc": "SecurityError"}


def test_python_cannot_reach_page_or_network(app, server):
    app.allow_csp_violations = True  # this test deliberately trips the sandbox CSP
    app.load(fixture_path("sample.csv"))
    app.run("__import__('js').self.origin")
    assert app.text("#expr-out") == "'null'"
    # sync XHR from the worker to the app origin and to an outside host: CSP blocks both
    for url in (server["base"] + "/index.html", "https://example.com/"):
        app.run(f"import js\nx = js.XMLHttpRequest.new()\nx.open('GET', '{url}', False)\nx.send()\nx.status")
        assert app.js("document.getElementById('expr-out').classList.contains('is-error')"), app.text("#expr-out")
        assert "NetworkError" in app.text("#expr-out") or "Failed" in app.text("#expr-out")
    # no access to the page's DOM from the worker
    app.run("hasattr(__import__('js'), 'document')")
    assert app.text("#expr-out") == "False"


def test_python_sees_only_the_emulated_machine(app):
    app.allow_csp_violations = True
    app.load(fixture_path("sample.csv"))
    app.run("import os; os.environ['USER'], os.environ['HOME']")
    assert app.text("#expr-out") == "('web_user', '/home/pyodide')"  # Emscripten's fake environment
    app.run("import glob; sorted(p for p in glob.glob('/**/*', recursive=True) if os.path.isfile(p) and not p.startswith(('/lib', '/dev', '/proc')))")
    files = eval(app.text("#expr-out"))
    assert len(files) == 2 and "/home/pyodide/explorer.py" in files and any(f.endswith("/input.csv") for f in files)
    app.run("import subprocess; subprocess.run(['ls'])")
    assert "does not support processes" in app.text("#expr-out")
    app.run("import socket; s = socket.socket(); s.connect(('example.com', 80)); s.send(b'x')")
    assert "not connected" in app.text("#expr-out")
    app.run("import js\nws = js.WebSocket.new('wss://echo.websocket.org/')\n'opened'")
    time.sleep(3)
    app.run("ws.readyState")
    assert app.text("#expr-out") == "3"  # CLOSED: connect-src blocks it


def test_sandbox_rejects_malformed_messages(app):
    replies = app.page.evaluate("""async () => {
        const frame = document.getElementById('sandbox').contentWindow;
        const got = [];
        const onMsg = (e) => { if (e.data && e.data.id >= 9000) got.push(e.data); };
        window.addEventListener('message', onMsg);
        const bad = [
          {v: 1, id: 9001, type: 'eval', payload: {expr: 123}},
          {v: 1, id: 9002, type: 'exec', payload: {}},
          {v: 1, id: 9003, type: 'eval', payload: {expr: '1', extra: 1}},
          {v: 2, id: 9004, type: 'ping'},
          {v: 1, id: -1, type: 'ping'},
          {v: 1, id: 9005, type: 'load', payload: {file: 'not a file', budget_mb: 10}},
          {v: 1, id: 9006, type: 'table', payload: {mode: 'head', n: 1e9, ascending: true}},
          {v: 1, id: 9007, type: '__proto__'},
          'ping',
        ];
        bad.forEach((m) => frame.postMessage(m, '*'));
        frame.postMessage({v: 1, id: 9100, type: 'ping'}, '*');
        await new Promise((r) => setTimeout(r, 2000));
        window.removeEventListener('message', onMsg);
        return got;
    }""")
    assert [r["id"] for r in replies] == [9100]
    assert replies[0]["ok"] is True


def test_untrusted_content_rendered_as_text(app):
    title = app.js("document.title")
    app.load(fixture_path("xss.csv"))
    assert app.js("document.querySelectorAll('img').length") == 1  # just the logo
    assert app.js("document.title") == title
    assert '<img src=x onerror="document.title=\'pwned\'">' in app.table()["head"]
    app.tab("plot")
    app.page.select_option("#plot-kind", "bar")
    app.page.select_option("#plot-x", "0")
    app.page.click("#plot-draw")
    app.idle()
    app.wait("document.querySelector('#plot .main-svg') !== null", 30)
    assert app.js("document.querySelectorAll('#plot img, #plot a[href]').length") == 0
    assert app.js("document.title") == title


def test_app_cannot_be_framed(server, browser_ctx):
    page = browser_ctx.new_page()
    try:
        page.set_content(f'<iframe id="f" src="{server["base"]}/"></iframe>')  # about:blank embedder
        page.wait_for_timeout(1500)
        child = [f for f in page.frames if f is not page.main_frame][0]
        try:
            rendered = child.query_selector("#status") is not None
        except Exception:
            rendered = False
        assert not rendered and child.url.startswith("chrome-error://")  # blocked by frame-ancestors
    finally:
        page.close()


def test_sandbox_opened_directly_is_still_opaque(server, browser_ctx):
    page = browser_ctx.new_page()
    try:
        page.goto(server["base"] + "/sandbox.html")
        assert page.evaluate("self.origin") == "null"
    finally:
        page.close()


# --- robustness ----------------------------------------------------------------

def test_cancel_restarts_python_and_reloads_file(app):
    app.load(fixture_path("sample.csv"))
    app.page.fill("#expr", "sum(iter(int, 1))")  # never finishes
    app.page.click("#run")
    app.wait("!document.getElementById('cancel').hidden", 10, "cancel button")
    app.page.click("#cancel")
    app.wait("document.getElementById('expr-out').textContent.startsWith('Cancelled')", 10)
    app.wait_ready()
    app.wait("document.querySelectorAll('#data-table tbody tr').length === 50", 60, "file reload")
    app.run("len(df)")
    assert app.text("#expr-out") == "500"


def test_mobile_layout(make_app):
    app = make_app(viewport={"width": 390, "height": 844})
    app.load(fixture_path("sample.csv"))
    overflow = "document.documentElement.scrollWidth - window.innerWidth"
    assert app.js(overflow) <= 0
    os.makedirs(ARTIFACTS, exist_ok=True)
    app.page.screenshot(path=os.path.join(ARTIFACTS, "mobile-table.png"), full_page=True)
    for tab in ("columns", "overview", "plot"):
        app.tab(tab)
        assert app.js(overflow) <= 0, tab
    app.page.click("#plot-draw")
    app.idle()
    app.wait("document.querySelector('#plot .main-svg') !== null", 30)
    assert app.js(overflow) <= 0
    app.page.screenshot(path=os.path.join(ARTIFACTS, "mobile-plot.png"), full_page=True)
