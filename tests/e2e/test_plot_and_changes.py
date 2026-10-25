"""Plots draw every row; expressions that change df update the whole app; layout uses the full width."""
from conftest import fixture_path

POINTS = ("df = pd.DataFrame({'x': np.random.default_rng(0).normal(size=100_000), "
          "'y': np.random.default_rng(1).normal(size=100_000)})")


def draw(app, kind, x, ys):
    app.tab("plot")
    app.page.select_option("#plot-kind", kind)
    app.page.select_option("#plot-x", str(x))
    app.set_plot_y(ys)
    app.page.click("#plot-draw")
    app.idle(120)
    app.wait("document.querySelector('#plot .main-svg') !== null", 60, "plot")


def test_new_column_from_expression_appears_everywhere(app):
    app.load(fixture_path("sample.csv"))
    app.run("df['New'] = df['count'] ** 2")
    assert "df updated: 500 rows × 8 columns. New column: New." in app.text("#expr-out")
    assert "Showing an expression result" in app.text("#view-note")
    t = app.table()
    assert t["head"][-1] == "New"
    count, new = t["head"].index("count"), t["head"].index("New")
    assert all(int(r[new]) == int(r[count]) ** 2 for r in t["rows"])
    app.tab("columns")
    assert app.table("#col-table")["rows"][-1][0] == "New"  # stats recalculated
    app.tab("plot")
    assert "New  · int64" in app.js("[...document.querySelector('#plot-ys select').options].map(o => o.textContent)")
    assert "New  · int64" in app.js("[...document.getElementById('plot-x').options].map(o => o.textContent)")
    app.page.click("#reset")
    app.idle()
    app.tab("table")
    assert "New" not in app.table()["head"]


def test_users_example_expression(app):
    app.load(fixture_path("sample.csv"))
    app.run("df['New'] = pd.Series(range(df.shape[1])) ** 2")
    assert "New column: New" in app.text("#expr-out")
    t = app.table()
    new = t["head"].index("New")
    assert [r[new] for r in t["rows"][:8]] == ["0", "1", "4", "9", "16", "25", "36", "null"]


def test_misaligned_index_hint_in_ui(app):
    app.load(fixture_path("wide.csv"))  # dated index
    app.run("df['New'] = pd.Series(range(df.shape[1])) ** 2")
    assert "entirely empty" in app.text("#expr-out")


def test_selections_survive_adding_a_column(app):
    app.load(fixture_path("sample.csv"))
    app.tab("plot")
    app.set_plot_y([3, 4])
    app.run("df.insert(0, 'first', 1)")
    app.tab("plot")
    names = app.js("[...document.querySelectorAll('#plot-ys select')].map(s => s.selectedOptions[0].textContent)")
    assert names == ["value  · float64", "count  · int64"]  # same columns, shifted positions


def test_line_plot_draws_every_row(app):
    app.load(fixture_path("sample.csv"))
    app.run("df = pd.DataFrame({'v': np.arange(50_000.0)}, index=pd.date_range('2020-01-01', periods=50_000, freq='min'))")
    draw(app, "line", -1, [0])
    assert app.js("document.getElementById('plot').data[0].x.length") == 50_000
    assert app.js("document.getElementById('plot').data[0].x instanceof Float64Array")


def test_big_scatter_is_a_density_of_every_point_then_markers_when_zoomed(app):
    app.load(fixture_path("sample.csv"))
    app.run(POINTS)
    draw(app, "scatter", 0, [1])
    assert app.js("document.getElementById('plot').data[0].type") == "heatmap"
    assert "All 100,000 points drawn as a density image" in app.text("#plot-notes")
    # zoom into a small region (as a drag-zoom would): it is redrawn as individual points
    app.js("Plotly.relayout('plot', {'xaxis.range': [0, 0.2], 'yaxis.range': [0, 0.2]})")
    app.wait("document.getElementById('plot').data[0].type === 'scatter'", 30, "zoomed markers")
    app.idle()
    n = app.js("document.getElementById('plot').data[0].x.length")
    assert 500 < n < 2000 and "Zoomed in: all" in app.text("#plot-notes")
    # double-click (autorange) returns to the full density view
    app.js("Plotly.relayout('plot', {'xaxis.autorange': true, 'yaxis.autorange': true})")
    app.wait("document.getElementById('plot').data[0].type === 'heatmap'", 30, "density again")


def test_stale_plot_is_flagged_after_data_change(app):
    app.load(fixture_path("sample.csv"))
    draw(app, "scatter", 3, [4])
    app.run("df['z'] = 1")
    assert "press Draw plot" in app.text("#plot-notes")


def test_opening_another_file_clears_the_old_plot_and_stats(app):
    app.load(fixture_path("sample.csv"))
    app.run(POINTS)
    app.tab("columns")
    draw(app, "scatter", 0, [1])
    app.page.click("#change-file")
    app.load(fixture_path("records.json"))
    app.tab("plot")
    assert app.js("document.querySelector('#plot .main-svg') === null")
    assert app.text("#plot-notes") == ""
    # and zoom-to-redraw still works on the next file's plot (purge drops the listener)
    app.page.click("#change-file")
    app.load(fixture_path("sample.csv"))
    app.run(POINTS)
    app.tab("columns")
    assert app.table("#col-table")["rows"][0][0] == "x"
    draw(app, "scatter", 0, [1])
    assert app.js("document.getElementById('plot').data[0].type") == "heatmap"
    app.js("Plotly.relayout('plot', {'xaxis.range': [0, 0.2], 'yaxis.range': [0, 0.2]})")
    app.wait("document.getElementById('plot').data[0].type === 'scatter'", 30, "zoomed markers")


def test_uses_full_viewport_width(make_app):
    app = make_app(viewport={"width": 1920, "height": 1000})
    app.load(fixture_path("sample.csv"))
    w = app.js("document.getElementById('explore').getBoundingClientRect().width")
    assert w > 1920 - 2 * 48 - 20  # only the page margin either side
    assert app.js("document.documentElement.scrollWidth - window.innerWidth") <= 0
