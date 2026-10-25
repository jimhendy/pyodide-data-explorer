"""Wide data in the real app: thousands of columns, dated and non-dated indexes."""
import os

from conftest import ARTIFACTS, fixture_path

WIDE = "wide.csv"  # 120 rows × 2,500 columns, daily date index
KEYED = "wide_keyed.parquet"  # 120 rows × 1,200 columns, text index "well"


def header_names(app):
    return app.table()["head"][1:]


def test_wide_table_pages_through_columns(app):
    app.load(fixture_path(WIDE))
    assert "2,500 columns" in app.text("#file-meta")
    assert "index: date (datetime64" in app.text("#view-note")
    t = app.table()
    assert len(t["head"]) == 51  # index + 50 columns per page
    assert t["head"][1] == "c0000" and t["rows"][0][0] == "2020-01-01"
    assert app.text("#col-range") == "Columns 1–50 of 2,500"
    assert app.js("document.getElementById('col-prev').disabled")
    app.page.click("#col-next")
    app.idle()
    assert header_names(app)[0] == "c0050"
    assert app.text("#col-range") == "Columns 51–100 of 2,500"
    # sort by a column on page 2: its values come back ordered
    app.page.click("#data-table thead th:nth-child(4) button")  # c0052
    app.idle()
    vals = [float(r[3]) for r in app.table()["rows"] if r[3] != "null"]
    assert vals == sorted(vals)
    # filter by name, then the pager follows the matches
    app.page.fill("#col-filter", "c24")
    app.wait("document.getElementById('col-range').textContent.includes('of 100 (2,500 total)')", 10, "filtered range")
    app.idle()
    assert all(n.startswith("c24") for n in header_names(app))
    assert "sorted by c0052" in app.text("#table-note")  # sorted column is off this page
    app.page.fill("#col-filter", "zzz")
    app.wait("document.getElementById('col-range').textContent === 'No matching columns'", 10)


def test_wide_column_stats_paged_sorted_filtered(app):
    app.load(fixture_path(WIDE))
    app.tab("columns", timeout=120)
    t = app.table("#col-table")
    assert len(t["rows"]) == 200 and t["rows"][0][0] == "c0000"
    assert app.text("#stats-range") == "Columns 1–200 of 2,500"
    assert t["rows"][0][t["head"].index("idxmin")].startswith("2020-")  # dates as index labels
    # sort by nulls (most first)
    app.page.click("#col-table thead th:nth-child(4) button")
    app.idle()
    t = app.table("#col-table")
    assert t["head"][3] == "nulls"
    assert t["rows"][0][3] == "30" and t["rows"][-1][3] == "30"  # every 5th column has every 4th row null
    app.page.fill("#stats-filter", "c249")
    app.wait("document.getElementById('stats-range').textContent.includes('of 10 (2,500 total)')", 10)
    app.idle()
    # the nulls sort is kept while filtering
    rows = app.table("#col-table")["rows"]
    assert sorted(r[0] for r in rows) == [f"c249{i}" for i in range(10)]
    assert rows[0][0] in ("c2490", "c2495") and rows[0][3] == "30"
    assert app.js("document.getElementById('stats-next').disabled")


def test_wide_plot_y_dropdowns_and_dated_line(app):
    app.load(fixture_path(WIDE))
    app.tab("plot")
    assert app.js("document.querySelectorAll('#plot-ys select').length") == 1
    assert app.js("document.querySelector('#plot-ys select').options.length") == 2500  # every column
    assert app.js("document.querySelectorAll('#plot-ys input[type=checkbox]').length") == 0
    assert app.js("document.getElementById('plot-kind').value") == "line"  # dated index: line by default
    app.page.select_option("#plot-x", "-1")
    app.set_plot_y([1234, 2499])
    app.page.click("#plot-draw")
    app.idle()
    app.wait("document.querySelector('#plot .main-svg') !== null", 30)
    data = app.js("document.getElementById('plot').data.map(t => [t.name, t.x.length, t.x[0]])")
    assert [d[0] for d in data] == ["c1234", "c2499"]
    assert all(d[1] == 120 for d in data)  # every row
    assert data[0][2] == 1577836800000  # 2020-01-01 as epoch ms on a date axis
    assert app.js("document.getElementById('plot')._fullLayout.xaxis.type") == "date"
    assert app.js("document.getElementById('plot').layout.xaxis.title.text") == "date"


def test_wide_overview_index_and_expression(app):
    app.load(fixture_path(WIDE))
    app.tab("overview")
    text = app.text("#overview")
    assert "Range2020-01-01 → 2020-04-29" in text and "FrequencyD (inferred)" in text
    assert "Orderincreasing" in text and "Uniqueyes" in text
    app.run('df.loc["2020-02", "c0100":"c0199"]')
    assert "29 rows × 100 columns" in app.text("#view-note")
    app.run("df.T")
    assert "100 rows × 29 columns" in app.text("#view-note")
    app.tab("table")
    assert header_names(app)[0] == "2020-02-01"  # dated column labels shown as dates


def test_index_option_row_numbers(app):
    app.load(fixture_path(WIDE))
    app.page.click("#options-btn")
    app.page.select_option("#opt-index", "none")
    app.page.click("#opt-load")
    app.wait("document.getElementById('file-meta').textContent.includes('2,501 columns')", 30, "reload with row numbers")
    app.idle()
    assert app.table()["rows"][0][0] == "0"
    assert header_names(app)[0] == "date"


def test_non_dated_wide_index(app):
    app.load(fixture_path(KEYED))
    assert "index: well (" in app.text("#view-note")
    t = app.table()
    assert t["head"][0] == "well" and t["rows"][0][0] == "well-000"
    app.tab("overview")
    text = app.text("#overview")
    assert "Rangewell-000 → well-119" in text and "Frequency" not in text
    app.tab("columns", timeout=120)
    assert app.table("#col-table")["rows"][0][-1].startswith("well-")  # idxmax uses the text index


def test_wide_mobile_no_page_overflow(make_app):
    app = make_app(viewport={"width": 390, "height": 844})
    app.load(fixture_path(WIDE))
    overflow = "document.documentElement.scrollWidth - window.innerWidth"
    assert app.js(overflow) <= 0
    for tab in ("columns", "overview", "plot"):
        app.tab(tab, timeout=120)
        assert app.js(overflow) <= 0, tab
    os.makedirs(ARTIFACTS, exist_ok=True)
    app.page.screenshot(path=os.path.join(ARTIFACTS, "mobile-wide-plot.png"), full_page=True)
    app.tab("table")
    app.page.screenshot(path=os.path.join(ARTIFACTS, "mobile-wide-table.png"), full_page=True)
