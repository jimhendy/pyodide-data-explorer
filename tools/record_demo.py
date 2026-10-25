"""Record a narrated walkthrough of the app as an MP4 (for reviewing changes remotely).

Usage (with serve.py running on port 8765):
    uv pip install --python .venv/bin/python imageio-ffmpeg && .venv/bin/playwright install ffmpeg
    .venv/bin/python tools/record_demo.py [out.mp4]

Captions and click highlights are drawn with CSSOM (el.style.x = ...), which the
app's CSP allows; nothing about the app itself is changed.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np
import pandas as pd
from playwright.sync_api import sync_playwright

BASE = os.environ.get("DEMO_URL", "http://127.0.0.1:8765/")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(tempfile.gettempdir(), "data-explorer-demo")
ENERGY = os.path.join(WORK, "energy.csv")
WIDE = os.path.join(ROOT, "tests", "fixtures", "wide.csv")
VIDEO_DIR = os.path.join(WORK, "video")
W, H = 1440, 900
T0 = 0.0
READY_AT = 0.0


def make_energy():
    """150,000 ten-minute readings with a date index, four regions and a few numeric columns."""
    if os.path.exists(ENERGY):
        return
    os.makedirs(WORK, exist_ok=True)
    rng = np.random.default_rng(7)
    n = 150_000
    idx = pd.date_range("2023-01-01", periods=n, freq="10min", name="timestamp")
    t = np.arange(n)
    day = np.sin(2 * np.pi * t / 144)
    temp = 12 + 8 * np.sin(2 * np.pi * t / (144 * 365)) + 4 * day + rng.normal(0, 1.2, n)
    load = 300 + 60 * day - 3 * temp + rng.normal(0, 15, n)
    region = rng.choice(["north", "south", "east", "west"], n, p=[.4, .3, .2, .1])
    price = 40 + 0.15 * load + np.where(region == "south", 8, 0) + rng.gamma(2, 3, n)
    pd.DataFrame({"temp_c": temp.round(2), "load_mw": load.round(1), "price": price.round(2),
                  "region": region, "outage": rng.random(n) < 0.01}, index=idx).to_csv(ENERGY)


OVERLAY = """() => {
  if (document.getElementById('demo-cap')) return;
  const cap = document.createElement('div');
  cap.id = 'demo-cap';
  Object.assign(cap.style, {position: 'fixed', left: '50%', bottom: '28px', transform: 'translateX(-50%)',
    maxWidth: '80%', padding: '12px 22px', borderRadius: '12px', background: 'rgba(15,23,42,.92)',
    color: '#fff', font: '600 20px/1.35 system-ui, sans-serif', zIndex: 99999, textAlign: 'center',
    boxShadow: '0 6px 24px rgba(0,0,0,.35)', transition: 'opacity .3s', opacity: '0', pointerEvents: 'none'});
  document.body.append(cap);
  const dot = document.createElement('div');
  dot.id = 'demo-dot';
  Object.assign(dot.style, {position: 'fixed', width: '26px', height: '26px', borderRadius: '50%',
    border: '3px solid #f43f5e', background: 'rgba(244,63,94,.25)', zIndex: 99998, pointerEvents: 'none',
    transform: 'translate(-50%,-50%)', transition: 'left .35s ease, top .35s ease, opacity .3s', opacity: '0',
    left: '50%', top: '50%'});
  document.body.append(dot);
}"""


class Demo:
    def __init__(self, page):
        self.page = page

    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg)

    def wait(self, cond, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(cond):
                return
            time.sleep(0.1)
        raise RuntimeError(f"timed out: {cond}")

    def idle(self, timeout=120):
        self.wait("!document.body.classList.contains('busy') && document.getElementById('status').dataset.state === 'ready'",
                  timeout)

    def caption(self, text, hold=0.0):
        self.js(OVERLAY)
        self.js("""(t) => { const c = document.getElementById('demo-cap');
                   c.textContent = t; c.style.opacity = t ? '1' : '0'; }""", text)
        if hold:
            time.sleep(hold)

    def point(self, sel):
        self.js(OVERLAY)
        loc = self.page.locator(sel).first
        loc.scroll_into_view_if_needed()
        box = loc.bounding_box()
        self.js("""([x, y]) => { const d = document.getElementById('demo-dot');
                   d.style.left = x + 'px'; d.style.top = y + 'px'; d.style.opacity = '1'; }""",
                [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
        time.sleep(0.5)

    def click(self, sel, pause=0.6):
        self.point(sel)
        self.page.click(sel)
        time.sleep(pause)

    def type(self, sel, text, delay=35):
        self.point(sel)
        self.page.fill(sel, "")
        self.page.type(sel, text, delay=delay)
        time.sleep(0.4)

    def run(self, expr):
        self.type("#expr", expr)
        self.click("#run", 0.2)
        self.idle()
        time.sleep(0.8)

    def option(self, sel, name):
        """Option value for the column whose label starts with name."""
        return self.js("""([s, n]) => [...document.querySelector(s).options]
                         .find(o => o.textContent.split('  ·')[0] === n)?.value""", [sel, name])

    def select(self, sel, name=None, value=None):
        self.point(sel)
        self.page.select_option(sel, value if value is not None else self.option(sel, name))
        time.sleep(0.6)

    def tab(self, name):
        self.click(f'[data-tab="{name}"]', 0.2)
        self.idle()
        time.sleep(0.6)

    def scroll_to(self, sel):
        self.page.locator(sel).first.scroll_into_view_if_needed()
        time.sleep(0.3)

    def top(self):
        self.js("window.scrollTo({top: 0, behavior: 'smooth'})")
        time.sleep(0.6)

    def load(self, path):
        name = os.path.basename(path)
        self.page.set_input_files("#file-input", path)
        # parquet / csv / jsonl open the "choose what to load" step first; take the defaults
        self.wait("!document.getElementById('setup-panel').hidden || document.getElementById('options-dialog').open")
        time.sleep(1.5)
        self.click("#opt-load", 0.3)
        self.wait(f"document.getElementById('file-name').textContent.startsWith({name!r}) && "
                  "!document.getElementById('explore').hidden")
        self.idle()

    def set_ys(self, names):
        while self.js("document.querySelectorAll('#plot-ys .yrow').length"):
            self.click("#plot-ys .yrow:first-child button", 0.3)
        for k, n in enumerate(names, start=1):
            self.click("#plot-y-add", 0.3)
            self.select(f"#plot-ys .yrow:nth-child({k}) select", n)

    def drag_plot(self, fx0, fy0, fx1, fy1):
        box = self.page.locator("#plot .nsewdrag").first.bounding_box()
        x0, y0 = box["x"] + box["width"] * fx0, box["y"] + box["height"] * fy0
        x1, y1 = box["x"] + box["width"] * fx1, box["y"] + box["height"] * fy1
        self.js("""([x, y]) => { const d = document.getElementById('demo-dot');
                   d.style.left = x + 'px'; d.style.top = y + 'px'; d.style.opacity = '1'; }""", [x0, y0])
        time.sleep(0.5)
        self.page.mouse.move(x0, y0)
        self.page.mouse.down()
        steps = 25
        for i in range(1, steps + 1):
            x, y = x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps
            self.page.mouse.move(x, y)
            self.js("""([x, y]) => { const d = document.getElementById('demo-dot');
                       d.style.transition = 'none'; d.style.left = x + 'px'; d.style.top = y + 'px'; }""", [x, y])
            time.sleep(0.03)
        self.page.mouse.up()
        self.js("() => { document.getElementById('demo-dot').style.transition = 'left .35s ease, top .35s ease, opacity .3s'; }")
        time.sleep(0.4)
        self.idle()




def opt_columns(d, names):
    d.click("#opt-col-none", 0.4)
    for n in names:
        d.type("#opt-col-filter", n, delay=60)
        time.sleep(0.3)
        d.click(f'#opt-col-list input[value="{n}"]', 0.3)
    d.page.fill("#opt-col-filter", "")
    time.sleep(0.4)


def submit_options(d):
    d.click("#opt-load", 0.3)
    d.wait("!document.getElementById('options-dialog').open")
    d.idle(300)
    time.sleep(0.8)


def script(d):
    global READY_AT
    page = d.page
    page.goto(BASE)
    d.idle(300)
    READY_AT = time.time() - T0
    d.caption("Redesigned: denser, full-height layout. Python + pandas run in a sandboxed browser worker.", 4)

    d.caption("Open a 150,000-row dated CSV: you first choose what to load (here, everything)", 1)
    d.point("#dropzone")
    d.load(ENERGY)
    time.sleep(0.8)
    d.caption("File details live in the top bar; the table fills the rest of the screen", 4)
    d.run("df['cost'] = df['load_mw'] * df['price'] / 1000")
    d.caption("The expression bar is one line; results show directly beneath it", 3)

    d.tab("plot")
    d.caption("Plot options sit in a sidebar, so the chart gets the full width and height", 1)
    d.select("#plot-kind", value="line")
    d.select("#plot-x", value="-1")
    d.set_ys(["temp_c", "load_mw"])
    d.click("#plot-draw", 0.2)
    d.idle()
    time.sleep(3)

    # --- Columns & filter: SQL WHERE
    d.caption("New: Columns & filter: choose columns and filter rows with SQL as the file is read", 1)
    d.click("#options-btn", 0.8)
    opt_columns(d, ["load_mw", "price", "region"])
    d.click('input[name="opt-rows"][value="where"]', 0.5)
    d.type("#opt-where", "region = 'south' AND price > 100", delay=40)
    d.caption("The dialog shows the exact SQL it will run. DuckDB applies the filter while reading.", 4)
    submit_options(d)
    d.caption("Only matching rows are loaded: the top bar shows the column subset and filter", 4)
    d.tab("overview")
    d.caption("The overview records the filter and how many rows matched", 3.5)

    # --- SQL query
    d.caption("Or run any SQL query against the file (it's the table 'data'). Here: daily averages", 1)
    d.click("#options-btn", 0.8)
    d.click('input[name="opt-rows"][value="sql"]', 0.5)
    page.fill("#opt-sql", "")
    d.type("#opt-sql", "SELECT date_trunc('day', timestamp) AS day,\n       avg(load_mw) AS load_mw,\n       avg(price) AS price\nFROM data\nGROUP BY 1 ORDER BY 1", delay=18)
    time.sleep(1.5)
    submit_options(d)
    d.tab("table")
    d.caption("1,042 daily rows: day is detected as the index", 3)
    d.tab("plot")
    d.select("#plot-kind", value="line")
    d.select("#plot-x", value="-1")
    d.set_ys(["load_mw", "price"])
    d.click("#plot-draw", 0.2)
    d.idle()
    time.sleep(3.5)

    # --- choose columns before loading a wide file
    d.caption("Opening a file always offers columns and rows first: here a 2,500-column CSV", 1)
    # (Open file shows the system file picker, which can't be recorded headless)
    page.set_input_files("#file-input", WIDE)
    d.wait("document.getElementById('options-dialog').open")
    time.sleep(0.6)
    d.caption("Search 2,500 columns and tick the few you need: they collect as chips", 1)
    opt_columns(d, ["c0001", "c0002", "c1200", "c2499"])
    time.sleep(1.5)
    submit_options(d)
    d.tab("table")
    d.caption("Just those columns are loaded, with the dated index", 3.5)
    d.tab("plot")
    d.select("#plot-kind", value="line")
    d.select("#plot-x", value="-1")
    d.set_ys(["c0001", "c1200"])
    d.click("#plot-draw", 0.2)
    d.idle()
    time.sleep(3)
    d.caption("SQL runs inside the sandbox too: DuckDB can only read the file you chose.", 4)
    d.caption("")
    time.sleep(0.5)


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(WORK, "data_explorer_demo.mp4"))
    make_energy()
    shutil.rmtree(VIDEO_DIR, ignore_errors=True)
    profile = os.path.join(WORK, "profile")
    global T0
    with sync_playwright() as p:
        # Warm the Pyodide cache without recording so the video doesn't open on a long download.
        ctx = p.chromium.launch_persistent_context(profile, channel="chrome", headless=True,
                                                   viewport={"width": W, "height": H})
        warm = ctx.new_page()
        warm.goto(BASE)
        Demo(warm).idle(300)
        warm.evaluate("localStorage.clear()")  # start from default settings
        ctx.close()

        ctx = p.chromium.launch_persistent_context(profile, channel="chrome", headless=True,
                                                   viewport={"width": W, "height": H},
                                                   record_video_dir=VIDEO_DIR,
                                                   record_video_size={"width": W, "height": H})
        T0 = time.time()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            script(Demo(page))
        finally:
            ctx.close()
        if errors:
            print("page errors:", errors)
    webm = max(glob.glob(os.path.join(VIDEO_DIR, "*.webm")), key=os.path.getsize)
    import imageio_ffmpeg
    trim = max(0.0, READY_AT - 2)
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", f"{trim:.1f}", "-i", webm,
                    "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", out], check=True)
    print(out)


if __name__ == "__main__":
    main()
