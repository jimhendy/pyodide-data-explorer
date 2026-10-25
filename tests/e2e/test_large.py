"""~5 GB files in the real browser app.

Generate the files first (≈25 GB of disk, a few minutes):
    python tests/make_large.py
Then:
    pytest tests/e2e/test_large.py

Each test checks that the file loads as a bounded sample (the WASM heap stays
far below the file size), that the sample really spans the file (head and tail
rows are the file's first and last), that the page stays responsive while
Python works, and that every view still works on the result. Timings are
written to tests/e2e/artifacts/large_report.json.
"""
import json
import os
import re
import time

import pytest

from conftest import ARTIFACTS, LARGE, SETUP_EXTS

GB = 1024 ** 3
REPORT = {}


def big(name):
    path = os.path.join(LARGE, name)
    if not os.path.exists(path) or os.path.getsize(path) < 4.5e9:
        pytest.skip(f"{name} missing; run python tests/make_large.py")
    return path


def last_line(path):
    with open(path, "rb") as f:
        f.seek(-4096, 2)
        return f.read().rstrip(b"\n").rsplit(b"\n", 1)[-1]


def expected_last_id(name, path):
    if name.endswith(".csv"):
        return int(last_line(path).split(b",", 1)[0])
    if name.endswith(".jsonl"):
        return json.loads(last_line(path))["id"]
    if name.endswith(".parquet"):
        import pyarrow.parquet as pq
        return pq.ParquetFile(path).metadata.num_rows - 1
    return None


def parse_bytes(text):
    m = re.match(r"([\d.]+) (B|KB|MB|GB)", text)
    return float(m.group(1)) * {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": GB}[m.group(2)]


def load_measuring_latency(app, path, key=None, timeout=600):
    """Load a file while pinging the page; returns (seconds, worst main-thread latency)."""
    app.page.select_option("#budget", "200")
    t0 = time.time()
    app.page.set_input_files("#file-input", path)
    if os.path.splitext(path)[1].lower() in SETUP_EXTS:
        # the "choose what to load" step needs only the schema, so it must come up quickly
        app.wait_setup(60)
        app.page.click("#opt-load")
    if key:
        app.wait("!document.getElementById('key-picker').hidden", timeout, "key picker")
        app.page.select_option("#key-select", key)
        app.page.click("#key-load")
    name = os.path.basename(path)
    worst = 0.0
    end = t0 + timeout
    while time.time() < end:
        s = time.time()
        done = app.js(f"""!document.body.classList.contains('busy') &&
            document.getElementById('file-name').textContent.startsWith({name!r}) &&
            document.querySelectorAll('#data-table tbody tr').length > 0""")
        worst = max(worst, time.time() - s)
        assert not app.visible("#load-error"), app.text("#load-error")
        if done:
            return time.time() - t0, worst
        time.sleep(0.25)
    raise AssertionError(f"load of {name} timed out; status={app.status()!r}")


def id_column(app):
    head = app.table()["head"]
    return head.index("id") if "id" in head else None


def overview_numbers(app):
    app.tab("overview")
    cards = app.js("""Object.fromEntries([...document.querySelectorAll('#overview .card')].map(c =>
        [c.querySelector('.k').textContent, c.querySelector('.v').textContent]))""")
    return cards


CASES = [
    ("big.csv", None),
    ("big.jsonl", None),
    ("big.json", None),
    ("big.parquet", None),
    ("big.h5", "/sensors/grid"),
    ("big.h5", "/records"),
]


@pytest.mark.parametrize("name,key", CASES, ids=[f"{n}{k or ''}" for n, k in CASES])
def test_large_file(app, name, key):
    path = big(name)
    seconds, worst = load_measuring_latency(app, path, key)
    report = REPORT.setdefault(f"{name}{key or ''}", {})
    report.update(file_gb=round(os.path.getsize(path) / 1e9, 2), load_seconds=round(seconds, 1),
                  worst_main_thread_ms=round(worst * 1000))
    meta = app.text("#file-meta")
    report["meta"] = meta

    # the main thread stayed responsive while Python worked
    assert worst < 1.0, f"page blocked for {worst:.1f}s during load"

    if key != "/records":  # the 4M-row records dataset may fit the budget
        assert app.visible("#sample-note"), "a 5 GB file must be sampled"
        report["sample"] = app.text("#sample-text")

    # head of the sample = head of the file
    col = id_column(app)
    rows = app.table()["rows"]
    if col is not None:
        assert rows[0][col] == "0"
    elif key == "/sensors/grid":
        assert rows[0][0] == "0"  # index = row number in file

    # tail of the sample = tail of the file (not possible for a JSON array, read from the start)
    want_last = expected_last_id(name, path)
    app.page.click('[data-mode="tail"]')
    app.idle()
    rows = app.table()["rows"]
    if want_last is not None:
        assert rows[-1][col] == str(want_last)
    if key == "/sensors/grid":
        total = int(re.search(r"([\d,]+) rows", meta).group(1).replace(",", ""))
        assert rows[-1][0] == str(total - 1)

    # memory stays bounded: the sample, not the file, lives in WASM memory
    cards = overview_numbers(app)
    heap = parse_bytes(cards["Python heap"])
    mem = parse_bytes(cards["Memory"])
    report.update(python_heap_mb=round(heap / 2 ** 20), dataframe_mb=round(mem / 2 ** 20),
                  rows_loaded=cards["Rows"])
    assert mem < 400 * 2 ** 20
    assert heap < 1.25 * GB

    # every view works on the sample
    t0 = time.time()
    app.tab("columns")
    report["colstats_seconds"] = round(time.time() - t0, 1)
    assert len(app.table("#col-table")["rows"]) >= 2
    app.tab("plot")
    app.page.select_option("#plot-kind", "histogram")
    app.page.click("#plot-draw")
    app.idle(120)
    app.wait("document.querySelector('#plot .main-svg') !== null", 60, "plot")
    app.run("df.describe()", timeout=120)
    assert "Showing an expression result" in app.text("#view-note")
    app.page.click("#reset")
    app.idle()


def test_large_csv_exact_row_count_and_cancel(app):
    path = big("big.csv")
    load_measuring_latency(app, path)
    want = expected_last_id("big.csv", path) + 1

    # exact count streams the whole 5 GB through the worker
    t0 = time.time()
    app.page.click("#scan-btn")
    app.wait("document.getElementById('file-meta').textContent.includes(' rows ·') && "
             "!document.getElementById('file-meta').textContent.includes('estimated')", 900, "row count")
    REPORT.setdefault("big.csv", {})["exact_count_seconds"] = round(time.time() - t0, 1)
    assert f"{want:,} rows" in app.text("#file-meta")
    app.idle()

    # cancel a long-running job: Python restarts and the file is re-sampled
    app.run("df.head(5)")
    app.page.fill("#expr", "sum(iter(int, 1))")
    app.page.click("#run")
    app.wait("!document.getElementById('cancel').hidden", 10, "cancel button")
    app.page.click("#cancel")
    app.wait_ready()
    app.wait("document.querySelectorAll('#data-table tbody tr').length === 50 && "
             "!document.body.classList.contains('busy')", 300, "reload after cancel")
    assert app.visible("#sample-note")


@pytest.fixture(scope="module", autouse=True)
def write_report():
    yield
    if REPORT:
        os.makedirs(ARTIFACTS, exist_ok=True)
        with open(os.path.join(ARTIFACTS, "large_report.json"), "w") as f:
            json.dump(REPORT, f, indent=2)


WIDE_CASES = [
    ("big_wide.parquet", None),
    ("big_wide.h5", None),  # single dataset: loads without a key picker
    ("big_wide.csv", None),
]


@pytest.mark.parametrize("name,key", WIDE_CASES, ids=[n for n, _ in WIDE_CASES])
def test_large_wide_file(app, name, key):
    """~5 GB with 2,000 columns and a minute-spaced date index (h5: raw matrix)."""
    path = big(name)
    seconds, worst = load_measuring_latency(app, path, key)
    report = REPORT.setdefault(name, {})
    report.update(file_gb=round(os.path.getsize(path) / 1e9, 2), load_seconds=round(seconds, 1),
                  worst_main_thread_ms=round(worst * 1000), meta=app.text("#file-meta"))
    assert worst < 1.0
    assert "2,000 columns" in app.text("#file-meta")
    assert app.visible("#sample-note")
    report["sample"] = app.text("#sample-text")
    dated = not name.endswith(".h5")
    if dated:
        assert "index: time (datetime64" in app.text("#view-note")
    rows = app.table()["rows"]
    assert rows[0][0] == ("2020-01-01 00:00:00" if dated else "0")
    assert app.text("#col-range") == "Columns 1–50 of 2,000"

    # the last page of columns, sorted, still responds quickly
    t0 = time.time()
    for _ in range(39):
        app.page.click("#col-next")
        app.idle()
    report["page_through_columns_seconds"] = round(time.time() - t0, 1)
    assert app.text("#col-range") == "Columns 1,951–2,000 of 2,000"
    app.page.click("#data-table thead th:last-child button")
    app.idle()

    cards = overview_numbers(app)
    heap = parse_bytes(cards["Python heap"])
    report.update(python_heap_mb=round(heap / 2 ** 20), dataframe_mb=round(parse_bytes(cards["Memory"]) / 2 ** 20),
                  rows_loaded=cards["Rows"])
    assert heap < 1.25 * GB

    t0 = time.time()
    app.tab("columns", timeout=300)
    report["colstats_seconds"] = round(time.time() - t0, 1)
    assert app.text("#stats-range") == "Columns 1–200 of 2,000"
    app.page.click("#col-table thead th:nth-child(4) button")  # sort by nulls
    app.idle(120)

    app.tab("plot")
    app.page.select_option("#plot-kind", "line")
    app.page.select_option("#plot-x", "-1")
    app.set_plot_y([0, 1999])
    app.page.click("#plot-draw")
    app.idle(120)
    app.wait("document.querySelector('#plot .main-svg') !== null", 60, "plot")
    assert app.js("document.getElementById('plot').data.length") == 2


# --- choosing columns / filtering rows of ~5 GB files ----------------------------

def load_with_options(app, path, setup, timeout=900):
    """Open the file through the "choose what to load" step; returns (seconds, worst main-thread latency)."""
    app.page.select_option("#budget", "200")
    app.page.set_input_files("#file-input", path)
    app.wait_setup(120)
    setup()
    t0 = time.time()
    app.page.click("#opt-load")
    name = os.path.basename(path)
    worst = 0.0
    while time.time() < t0 + timeout:
        s = time.time()
        done = app.js(f"""!document.body.classList.contains('busy') &&
            document.getElementById('file-name').textContent.startsWith({name!r}) &&
            document.querySelectorAll('#data-table tbody tr').length > 0""")
        worst = max(worst, time.time() - s)
        assert not app.js("document.getElementById('options-dialog').open"), app.text("#opt-error")
        if done:
            return time.time() - t0, worst
        time.sleep(0.25)
    raise AssertionError(f"filtered load of {name} timed out; status={app.status()!r}")


def record(app, key, path, seconds, worst):
    cards = overview_numbers(app)
    report = REPORT.setdefault(key, {})
    report.update(file_gb=round(os.path.getsize(path) / 1e9, 2), load_seconds=round(seconds, 1),
                  worst_main_thread_ms=round(worst * 1000), meta=app.text("#file-meta"),
                  rows_loaded=cards["Rows"], python_heap_mb=round(parse_bytes(cards["Python heap"]) / 2 ** 20))
    app.tab("table")
    assert worst < 1.0
    assert parse_bytes(cards["Python heap"]) < 1.25 * GB
    return report


def test_large_wide_parquet_column_subset(app):
    """4 of 2,000 columns: only their column chunks are read (pyarrow, no DuckDB)."""
    path = big("big_wide.parquet")
    COLS = ["sensor_0000", "sensor_0001", "sensor_0500", "sensor_1999"]

    def setup():
        app.page.click("#opt-col-none")
        for n in COLS:
            app.page.fill("#opt-col-filter", n)
            app.wait(f"!!document.querySelector('#opt-col-list input[value=\"{n}\"]')", 10)
            app.page.check(f'#opt-col-list input[value="{n}"]')
        app.page.fill("#opt-col-filter", "")

    seconds, worst = load_with_options(app, path, setup)
    assert app.table()["head"][1:] == COLS
    assert "4 of 2,000 columns" in app.text("#file-meta")
    report = record(app, "big_wide.parquet [4 columns]", path, seconds, worst)
    # the 4 narrow columns fit far more rows in the budget than all 2,000 do
    assert int(report["rows_loaded"].replace(",", "")) > 100_000
    assert seconds < 60


@pytest.mark.parametrize("name", ["big.parquet", "big.csv"])
def test_large_sql_row_filter(app, name):
    """A WHERE filter streams the whole file through DuckDB; only matching rows are kept."""
    path = big(name)
    cond = "category = 'delta' AND value > 140"

    def setup():
        app.page.check('input[name="opt-rows"][value="where"]')
        app.page.fill("#opt-where", cond)

    seconds, worst = load_with_options(app, path, setup, timeout=1500)
    t = app.table()
    cat, val = t["head"].index("category"), t["head"].index("value")
    assert t["rows"] and all(r[cat] == "delta" and float(r[val]) > 140 for r in t["rows"])
    report = record(app, f"{name} [WHERE {cond}]", path, seconds, worst)
    app.tab("overview")
    text = app.text("#overview")
    assert "Row filter" in text and "Matching rows" in text
    report["sample"] = app.text("#sample-text") if app.visible("#sample-note") else "not sampled"


def test_large_sql_aggregate_query(app):
    path = big("big.parquet")

    def setup():
        app.page.check('input[name="opt-rows"][value="sql"]')
        app.page.fill("#opt-sql", "SELECT category, count(*) AS n, avg(value) AS mean_value FROM data GROUP BY 1 ORDER BY n DESC")

    seconds, worst = load_with_options(app, path, setup, timeout=1500)
    t = app.table()
    # a unique text column in front of numbers becomes the index (labelled matrix)
    assert t["head"] == ["category", "n", "mean_value"] and len(t["rows"]) > 1
    import pyarrow.parquet as pq
    total = pq.ParquetFile(path).metadata.num_rows
    assert sum(int(r[1].replace(",", "")) for r in t["rows"]) == total  # every row counted
    record(app, "big.parquet [SQL GROUP BY]", path, seconds, worst)
