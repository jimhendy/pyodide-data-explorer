"""The "Columns & filter" dialog: column subsets, SQL row filters and SQL queries
(DuckDB inside the Pyodide sandbox)."""
from conftest import fixture_path


def open_dialog(app):
    app.page.click("#options-btn")
    app.wait("document.getElementById('options-dialog').open", 10, "options dialog")


def choose_columns(app, names):
    app.page.click("#opt-col-none")
    for n in names:
        app.page.fill("#opt-col-filter", n)
        app.wait(f"[...document.querySelectorAll('#opt-col-list input')].some(i => i.value === {n!r})", 5)
        app.page.check(f'#opt-col-list input[value="{n}"]')
    app.page.fill("#opt-col-filter", "")


def submit(app, timeout=180):
    app.page.click("#opt-load")
    app.wait("!document.getElementById('options-dialog').open", 10, "dialog to close")
    app.wait("!document.body.classList.contains('busy') && (document.getElementById('status').dataset.state === 'ready' "
             "|| document.getElementById('options-dialog').open)", timeout, "load to finish")


def chips(app):
    return app.js("[...document.querySelectorAll('#opt-col-chips .chip')].map(c => c.textContent)")


def test_choose_columns_before_loading(app):
    app.page.set_input_files("#file-input", fixture_path("sample.parquet"))
    app.wait_setup()
    # before any data is open the choices are inline, in place of the open screen
    assert app.visible("#setup-panel") and not app.visible("#load-panel")
    assert not app.js("document.getElementById('options-dialog').open")
    assert app.text("#opt-title") == "Choose what to load" and app.text("#opt-cancel") == "Choose another file"
    assert app.text("#opt-col-count") == "7 of 7 selected" and len(chips(app)) == 7
    choose_columns(app, ["value", "group"])
    assert chips(app) == ["group", "value"] and app.text("#opt-col-count") == "2 of 7 selected"
    assert app.text("#opt-col-chips") == "groupvalue"  # nothing else in the tray
    assert app.text("#opt-load") == "Load 2 columns"
    assert app.text("#opt-preview") == 'SELECT "group", "value" FROM data'
    assert "Only the chosen columns are read" in app.text("#opt-engine")
    app.page.click("#opt-load")
    app.wait("!document.getElementById('explore').hidden", 60, "data")
    app.idle()
    assert not app.visible("#setup-panel")
    assert app.table()["head"][1:] == ["group", "value"]
    assert "2 of 7 columns" in app.text("#file-meta")
    assert app.js("document.getElementById('options-btn').classList.contains('active')")


def test_multi_select_chips_search_and_ranges(app):
    app.page.set_input_files("#file-input", fixture_path("sample.parquet"))
    app.wait_setup()
    cols = app.js("[...document.querySelectorAll('#opt-col-list input')].map(i => i.value)")
    # removing a chip unticks the column
    app.page.click(f'#opt-col-chips .chip[aria-label="Remove {cols[0]}"]')
    assert not app.js(f"document.querySelector('#opt-col-list input[value=\"{cols[0]}\"]').checked")
    assert app.text("#opt-col-count") == "6 of 7 selected"
    # shift-click ticks a range
    app.page.click("#opt-col-none")
    assert app.text("#opt-col-count") == "0 of 7 selected" and "No columns selected" in app.text("#opt-col-chips")
    assert app.js("document.getElementById('opt-load').disabled")
    app.page.click(f'#opt-col-list input[value="{cols[1]}"]')
    app.page.click(f'#opt-col-list input[value="{cols[4]}"]', modifiers=["Shift"])
    assert chips(app) == cols[1:5]
    # "Only these" selects exactly the search matches
    app.page.fill("#opt-col-filter", "val")
    app.wait("!document.getElementById('opt-col-only').hidden", 5)
    app.page.click("#opt-col-only")
    app.page.fill("#opt-col-filter", "")
    assert chips(app) == ["value"]


def test_choose_another_file_goes_back_to_the_open_screen(app):
    app.page.set_input_files("#file-input", fixture_path("sample.csv"))
    app.wait_setup()
    app.page.click("#opt-cancel")
    assert app.visible("#load-panel") and not app.visible("#setup-panel") and not app.visible("#explore")
    app.load(fixture_path("sample.parquet"))   # and the next file works as normal
    assert "sample.parquet" in app.text("#file-name")


def test_wide_file_summarises_instead_of_thousands_of_chips(app):
    app.page.set_input_files("#file-input", fixture_path("wide.csv"))
    app.wait_setup()
    assert chips(app) == ["date · index"]  # the index column is always loaded
    assert "All 2,500 columns" in app.text("#opt-col-chips")


def test_filter_rows_with_sql_where(app):
    app.load(fixture_path("sample.csv"))
    open_dialog(app)
    app.page.check('input[name="opt-rows"][value="where"]')
    assert app.js("document.getElementById('opt-load').disabled")  # nothing to filter by yet
    app.page.fill("#opt-where", "\"group\" = 'red' AND value > 5")
    assert "WHERE \"group\" = 'red' AND value > 5" in app.text("#opt-preview")
    assert "DuckDB" in app.text("#opt-engine")
    submit(app)
    t = app.table()
    g = t["head"].index("group")
    assert t["rows"] and all(r[g] == "red" for r in t["rows"])
    assert "where \"group\" = 'red' AND value > 5" in app.text("#file-meta")
    app.tab("overview")
    assert "Row filter" in app.text("#overview")
    assert "pyarrow stream, filtered by DuckDB" in app.text("#overview")  # the fast path works in Pyodide
    # reopening shows the current choices
    open_dialog(app)
    assert app.js("document.getElementById('opt-where').value") == "\"group\" = 'red' AND value > 5"
    app.page.keyboard.press("Escape")
    app.wait("!document.getElementById('options-dialog').open", 5)


def test_sql_query(app):
    app.load(fixture_path("sample.parquet"))
    open_dialog(app)
    app.page.check('input[name="opt-rows"][value="sql"]')
    assert app.text("#opt-col-count") == "set by your query"
    app.page.fill("#opt-sql", 'SELECT "group", count(*) AS n FROM data GROUP BY 1 ORDER BY n DESC')
    assert app.text("#opt-load") == "Run query"
    submit(app)
    t = app.table()
    assert t["head"][1:] == ["group", "n"] and len(t["rows"]) == 4
    assert "SQL query" in app.text("#file-meta")


def test_bad_filter_keeps_data_and_shows_error_in_dialog(app):
    app.load(fixture_path("sample.csv"))
    open_dialog(app)
    app.page.check('input[name="opt-rows"][value="where"]')
    app.page.fill("#opt-where", "no_such_column > 1")
    submit(app)
    app.wait("document.getElementById('options-dialog').open", 30, "dialog to reopen with the error")
    assert "no_such_column" in app.text("#opt-error")
    # the attempted filter is still there to fix
    assert app.js("document.getElementById('opt-where').value") == "no_such_column > 1"
    app.page.click("#opt-cancel")
    assert len(app.table()["rows"]) == 50 and "where" not in app.text("#file-meta")


def test_sql_cannot_read_other_files_in_the_sandbox(app):
    app.load(fixture_path("sample.csv"))
    open_dialog(app)
    app.page.check('input[name="opt-rows"][value="sql"]')
    app.page.fill("#opt-sql", "SELECT * FROM read_text('/home/pyodide/explorer.py')")
    submit(app)
    app.wait("document.getElementById('options-dialog').open", 30, "error")
    assert "SQL query failed" in app.text("#opt-error")


def test_dialog_budget_and_index_apply_to_the_load(app):
    app.load(fixture_path("sample.csv"))
    open_dialog(app)
    app.page.select_option("#opt-budget", "50")
    app.page.select_option("#opt-index", "none")
    choose_columns(app, ["value", "when"])
    submit(app)
    assert app.js("document.getElementById('budget').value") == "50"
    assert app.js("document.getElementById('index-mode').value") == "none"
    assert app.table()["head"][1:] == ["value", "when"]


def test_other_formats_get_load_settings_only(app):
    app.load(fixture_path("records.json"))
    assert app.text("#options-label") == "Load options"
    open_dialog(app)
    assert not app.visible("#opt-cols") and not app.visible("#opt-rowsel") and app.visible("#opt-unsupported")
    assert app.text("#opt-load") == "Reload"
    app.page.select_option("#opt-index", "first")
    submit(app)
    assert app.js("document.getElementById('index-mode').value") == "first"
    assert app.table()["head"][0] == "id"  # first column is now the index
    app.load(fixture_path("lines.jsonl"))
    assert app.text("#options-label") == "Columns & filter"


def test_a_new_file_starts_with_fresh_choices_and_cancel_keeps_the_data(app):
    app.load(fixture_path("sample.csv"))
    open_dialog(app)
    app.page.check('input[name="opt-rows"][value="sql"]')
    app.page.fill("#opt-sql", "SELECT id FROM data")
    submit(app)
    # with data open, a new file's choices appear in the dialog, starting fresh
    app.page.set_input_files("#file-input", fixture_path("sample.parquet"))
    app.wait("document.getElementById('options-dialog').open", 60, "options dialog")
    assert app.text("#opt-title") == "Choose what to load"
    assert app.js("document.querySelector('input[name=\"opt-rows\"]:checked').value") == "all"
    assert app.text("#opt-col-count") == "7 of 7 selected"
    # cancelling keeps the open data, and its options are still the ones reopened
    app.page.click("#opt-cancel")
    assert "sample.csv" in app.text("#file-name") and app.table()["head"][1:] == ["id"]
    open_dialog(app)
    assert app.text("#opt-title") == "Columns & filter"
    assert app.js("document.getElementById('opt-sql').value") == "SELECT id FROM data"
    app.page.keyboard.press("Escape")
    # Escape on a new file's dialog also keeps the open data
    app.page.set_input_files("#file-input", fixture_path("sample.parquet"))
    app.wait("document.getElementById('options-dialog').open", 60, "options dialog")
    app.page.keyboard.press("Escape")
    app.wait("!document.getElementById('options-dialog').open", 5)
    open_dialog(app)
    assert "sample.csv" in app.text("#opt-file")
