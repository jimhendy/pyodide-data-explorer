"""Native tests for src/explorer.py (the same module runs in Pyodide).

Large-file behaviour is exercised here with tiny memory budgets so the
sampling paths run in milliseconds; tests/e2e covers real ~5 GB files in the
browser.
"""
import json
import os
import sys

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import explorer as ex  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def fx(name):
    return os.path.join(FIX, name)


def call(kind, /, **payload):
    out = hydrate(json.loads(ex.dispatch(kind, json.dumps(payload))), ex.take_buffers())
    return out


def hydrate(obj, bufs):
    """Replace the worker's binary buffer markers with lists, as the page does."""
    if isinstance(obj, dict):
        if "__buf__" in obj:
            return [None if v != v else v for v in bufs[obj["__buf__"]].tolist()]
        if "__grid__" in obj:
            flat = hydrate(obj["__grid__"], bufs)
            return [flat[r * obj["w"]:(r + 1) * obj["w"]] for r in range(obj["h"])]
        return {k: hydrate(v, bufs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [hydrate(v, bufs) for v in obj]
    return obj


def ok(kind, /, **payload):
    out = call(kind, **payload)
    assert out["ok"], out
    return out["result"]


def load(name, **kw):
    return ok("load", path=fx(name), name=name, **kw)


def expected():
    return pd.read_csv(fx("sample.csv"))


# --- loading every format ---------------------------------------------------

@pytest.mark.parametrize("name", ["sample.csv", "sample.tsv", "records.json", "lines.jsonl", "sample.parquet",
                                  "sample.xlsx"])
def test_load_each_format(name):
    if name.endswith(".xlsx"):
        pytest.importorskip("openpyxl")
    res = load(name)
    info = res["info"]
    want_rows = 200 if name.endswith(".xlsx") else 500
    assert info["rows_loaded"] == want_rows
    assert info["rows_total"] == want_rows and info["rows_exact"]
    assert not info["sampled"]
    assert [c["name"] for c in res["view"]["columns"]][:4] == ["id", "name", "group", "value"]
    assert ex.STATE.df["count"].sum() == expected()["count"].head(want_rows).sum()


def test_detect_format_by_magic(tmp_path):
    p = tmp_path / "noext"
    p.write_bytes(open(fx("sample.parquet"), "rb").read())
    assert ex.detect_format(str(p)) == "parquet"
    p.write_bytes(open(fx("sample.h5"), "rb").read())
    assert ex.detect_format(str(p)) == "h5"
    p.write_bytes(b'[{"a": 1}]')
    assert ex.detect_format(str(p)) == "json"


def test_parquet_keeps_pandas_index():
    load("indexed.parquet")
    assert ex.STATE.df.index.name == "name"


def test_xlsx_sheets_and_key():
    pytest.importorskip("openpyxl")
    res = ok("inspect", path=fx("sample.xlsx"), name="sample.xlsx")
    assert [k["key"] for k in res["keys"]] == ["Data", "Small"]
    res = load("sample.xlsx", key="Small")
    assert res["info"]["rows_loaded"] == 2


def test_h5_keys_listing():
    res = ok("inspect", path=fx("sample.h5"), name="sample.h5")
    keys = {k["key"]: k for k in res["keys"]}
    assert set(keys) >= {"/pandas_fixed", "/nested/pandas_table", "/pandas_table_dc", "/pandas_series",
                         "/raw/matrix", "/raw/vector", "/raw/records"}
    assert not any("_i_table" in k for k in keys)
    assert keys["/pandas_fixed"]["rows"] == 500


@pytest.mark.parametrize("key", ["/pandas_fixed", "/nested/pandas_table", "/pandas_table_dc"])
def test_h5_pandas_frames_roundtrip(key):
    load("sample.h5", key=key)
    got = ex.STATE.df
    want = expected()
    assert list(got.columns) == list(want.columns)
    assert got["count"].tolist() == want["count"].tolist()
    np.testing.assert_allclose(got["value"].astype(float), want["value"], equal_nan=True)
    assert got["name"].iloc[1] == "item 1"
    assert pd.isna(got["name"].iloc[0]) or got["name"].iloc[0] in (None, "nan")
    assert str(got["when"].iloc[3]) == "2024-01-01 03:00:00"


def test_h5_series_and_raw():
    load("sample.h5", key="/pandas_series")
    assert ex.STATE.df.shape == (500, 1)
    assert str(ex.STATE.df.index[1]) == "2024-01-01 01:00:00"
    load("sample.h5", key="/raw/matrix")
    assert ex.STATE.df.shape == (20, 3) and ex.STATE.df.iloc[19, 2] == 59
    load("sample.h5", key="/raw/records")
    assert ex.STATE.df["label"].tolist() == ["x", "yy", "zzz", "w", "v"]
    load("sample.h5", key="/raw/vector")
    assert ex.STATE.df.iloc[:, 0].tolist() == list(range(10))


def test_malicious_pickles_are_refused(capfd):
    # table attrs: bad pickle falls back to field names, nothing executes
    load("malicious.h5", key="/t")
    out = call("load", path=fx("malicious.h5"), name="malicious.h5", key="/f")
    assert not out["ok"] and "refusing to load" in out["error"]
    assert "pwned" not in capfd.readouterr().out
    with pytest.raises(Exception, match="refusing"):
        ex.safe_unpickle(__import__("pickle").dumps(ex.UserError("x")))


# --- large-file sampling (tiny budgets) -------------------------------------

def big_frame(n):
    return pd.DataFrame({"id": np.arange(n), "v": np.arange(n) * 0.5, "s": np.array(["a", "bb", "ccc"])[np.arange(n) % 3]})


@pytest.fixture(scope="module")
def big_csv(tmp_path_factory):
    p = tmp_path_factory.mktemp("big") / "big.csv"
    big_frame(300_000).to_csv(p, index=False)
    return str(p)


def test_csv_sampling_spreads_across_file(big_csv):
    res = ok("load", path=big_csv, name="big.csv", budget_mb=1)
    info = res["info"]
    df = ex.STATE.df
    assert info["sampled"] and not info["rows_exact"]
    assert 0 < len(df) < 300_000
    assert ex.mem_bytes(df) < 2.5 * ex.MB
    assert df["id"].iloc[0] == 0 and df["id"].iloc[-1] == 299_999  # head and tail included
    ids = df["id"].to_numpy()
    assert (np.diff(ids) > 0).all()  # blocks in file order, no duplicates
    hist, _ = np.histogram(ids, bins=8, range=(0, 300_000))
    assert (hist > 0).all()  # every eighth of the file is represented
    assert abs(info["rows_total"] - 300_000) / 300_000 < 0.05
    assert (df["v"] == df["id"] * 0.5).all()  # no torn lines
    res = ok("scan")
    assert res["info"]["rows_total"] == 300_000 and res["info"]["rows_exact"]


def test_csv_small_budget_still_full_when_it_fits(big_csv):
    res = ok("load", path=big_csv, name="big.csv", budget_mb=500)
    assert not res["info"]["sampled"] and res["info"]["rows_loaded"] == 300_000


def test_jsonl_sampling(tmp_path):
    p = tmp_path / "big.jsonl"
    big_frame(100_000).to_json(p, orient="records", lines=True)
    res = ok("load", path=str(p), name="big.jsonl", budget_mb=0.5)
    df = ex.STATE.df
    assert res["info"]["sampled"]
    assert df["id"].iloc[0] == 0 and df["id"].iloc[-1] == 99_999
    assert ok("scan")["info"]["rows_total"] == 100_000


def test_json_array_head_sampling(tmp_path):
    p = tmp_path / "big.json"
    big_frame(100_000).to_json(p, orient="records")
    res = ok("load", path=str(p), name="big.json", budget_mb=0.5)
    df = ex.STATE.df
    assert res["info"]["sampled"]
    assert df["id"].tolist() == list(range(len(df)))
    assert 1000 < len(df) < 100_000
    assert abs(res["info"]["rows_total"] - 100_000) / 100_000 < 0.1


def test_json_array_iter_handles_chunk_boundaries():
    import io
    recs = [{"a": i, "s": "é" * (i % 5), "f": i / 3} for i in range(3000)]
    data = json.dumps(recs).encode()
    got = [r for r, _ in ex._iter_json_array(io.BytesIO(data), chunk_size=97)]
    assert got == recs


def test_parquet_row_group_sampling(tmp_path):
    p = tmp_path / "big.parquet"
    pq.write_table(pa.Table.from_pandas(big_frame(200_000), preserve_index=False), p, row_group_size=10_000)
    res = ok("load", path=str(p), name="big.parquet", budget_mb=1)
    info = res["info"]
    df = ex.STATE.df
    assert info["sampled"] and info["rows_exact"] and info["rows_total"] == 200_000
    assert df["id"].iloc[0] == 0 and df["id"].iloc[-1] == 199_999
    assert (df.index.to_numpy() == df["id"].to_numpy()).all()  # index = row number in file
    assert (np.diff(df["id"].to_numpy()) > 0).all()
    hist, _ = np.histogram(df["id"], bins=8, range=(0, 200_000))
    assert (hist > 0).all()  # slices come from across the file
    assert ex.mem_bytes(df) < 1.5 * ex.MB
    cs = ok("colstats")
    assert cs["has_file_stats"]
    idcol = cs["columns"][0]
    assert idcol["file"]["min"] == "0" and idcol["file"]["max"] == "199999"


def test_parquet_huge_single_row_group_streams(tmp_path):
    p = tmp_path / "one.parquet"
    pq.write_table(pa.Table.from_pandas(big_frame(300_000), preserve_index=False), p, row_group_size=300_000)
    res = ok("load", path=str(p), name="one.parquet", budget_mb=0.5)
    assert res["info"]["sampled"]
    assert ex.STATE.df["id"].iloc[0] == 0 and len(ex.STATE.df) < 300_000


def test_h5_block_sampling(tmp_path):
    import h5py
    p = tmp_path / "big.h5"
    with h5py.File(p, "w") as f:
        f["d"] = np.arange(2_000_000, dtype=np.int64).reshape(-1, 2)
    res = ok("load", path=str(p), name="big.h5", key="/d", budget_mb=1)
    df = ex.STATE.df
    assert res["info"]["sampled"] and res["info"]["rows_total"] == 1_000_000
    assert df.index[0] == 0 and df.index[-1] == 999_999
    assert (df[0].to_numpy() == df.index.to_numpy() * 2).all()


def test_h5_pandas_table_sampling(tmp_path):
    import warnings
    p = tmp_path / "t.h5"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        big_frame(200_000).to_hdf(p, key="t", format="table")
    res = ok("load", path=str(p), name="t.h5", key="/t", budget_mb=1)
    df = ex.STATE.df
    assert res["info"]["sampled"]
    assert df["id"].iloc[-1] == 199_999 and (df.index == df["id"]).all()


def test_stratified_starts_cover_range():
    import random
    s = ex.stratified_starts(1000, 10, 10, random.Random(0))
    assert s[0] == 0 and s[-1] == 990 and len(s) == 10
    assert ex.stratified_starts(5, 10, 10, random.Random(0)) == [0]


# --- views -----------------------------------------------------------------

@pytest.fixture
def sample():
    load("sample.csv")


def test_csv_dates_inferred(sample):
    assert str(ex.STATE.df["when"].dtype).startswith("datetime64")
    assert str(ex.STATE.df["name"].dtype) != "datetime64"


def test_table_modes(sample):
    t = ok("table", mode="head", n=5)
    assert t["index"] == ["0", "1", "2", "3", "4"] and t["columns"][0] == "id"
    assert t["rows"][0][1] is None  # null name rendered as null
    t = ok("table", mode="tail", n=3)
    assert t["index"] == ["497", "498", "499"]
    t = ok("table", mode="sample", n=10)
    idx = [int(i) for i in t["index"]]
    assert len(idx) == 10 and idx == sorted(idx)
    t = ok("table", mode="head", n=3, sort=3, ascending=False)
    vals = [float(r[3]) for r in t["rows"]]
    assert vals == sorted(vals, reverse=True)
    t = ok("table", mode="tail", n=3, sort=3)
    assert all(r[3] is None for r in t["rows"])  # NaNs sort last
    t = ok("table", n=2, sort=-1, ascending=False)
    assert t["index"] == ["499", "498"]


def test_table_mixed_type_sort():
    ex.STATE.df = ex.STATE.df0 = pd.DataFrame({"m": [3, "a", None, 1.5]})
    t = ok("table", sort=0)
    assert len(t["rows"]) == 4


def test_colstats(sample):
    cs = ok("colstats")["columns"]
    by = {c["name"]: c for c in cs}
    want = expected()
    v = by["value"]
    assert v["nulls"] == int(want["value"].isna().sum())
    assert float(v["mean"]) == pytest.approx(want["value"].mean())
    assert v["idxmax"] == str(want["value"].idxmax())
    assert by["name"]["unique"] == want["name"].nunique()
    assert by["group"]["min"] == "blue"
    assert "mean" in by["ok"]


def test_overall(sample):
    o = ok("overall")
    assert o["shape"] == [500, 7]
    assert o["total_nulls"] == int(expected().isna().sum().sum())
    assert o["memory_bytes"] > 0 and o["info"]["format"] == "csv"


# --- expressions -----------------------------------------------------------

def test_eval_frame_series_value_reset(sample):
    r = ok("eval", expr='df[df.group.eq("eg")].T')
    assert r["ok"] and r["kind"] == "frame" and r["view"]["cols"] == int((expected().group == "eg").sum())
    assert not r["view"]["is_original"]
    r = ok("reset")
    assert r["view"]["is_original"] and r["view"]["rows"] == 500
    r = ok("eval", expr="df.value")
    assert r["kind"] == "frame" and r["view"]["cols"] == 1
    ok("reset")
    r = ok("eval", expr="len(df)")
    assert r["kind"] == "value" and r["text"] == "500"
    assert r["view"]["rows"] == 500  # non-frame results leave the view alone


def test_eval_chains_on_current_view_and_df0(sample):
    ok("eval", expr="df.head(10)")
    assert ok("eval", expr="len(df), len(df0)")["text"] == "(10, 500)"


def test_eval_statements_and_print(sample):
    r = ok("eval", expr="x = df['count'] * 2\nprint('hi')\ndf.assign(double=x)")
    assert r["kind"] == "frame" and r["stdout"] == "hi\n"
    assert r["view"]["columns"][-1]["name"] == "double"


def test_eval_errors_are_reported_not_raised(sample):
    r = ok("eval", expr="df.nope")
    assert not r["ok"] and "AttributeError" in r["error"]
    r = ok("eval", expr="df[")
    assert not r["ok"] and "SyntaxError" in r["error"]
    assert ok("eval", expr="1/0")["ok"] is False
    assert ex.STATE.df is ex.STATE.df0


def test_load_without_file_is_user_error():
    ex.STATE.__init__()
    out = call("table")
    assert not out["ok"] and "Load a file" in out["error"]


# --- plots -----------------------------------------------------------------

@pytest.mark.parametrize("spec", [
    dict(kind="scatter", x=3, ys=[4]),
    dict(kind="line", x=5, ys=[3, 4]),
    dict(kind="scatter", x=3, ys=[4], color=2),
    dict(kind="histogram", ys=[3]),
    dict(kind="histogram", ys=[5]),
    dict(kind="histogram", ys=[2], color=6),
    dict(kind="bar", x=2, ys=[4], agg="sum"),
    dict(kind="bar", x=2, ys=[], agg="count"),
    dict(kind="bar", ys=[4]),
    dict(kind="box", ys=[3, 4]),
    dict(kind="box", x=2, ys=[3], color=6),
])
def test_plot_specs(sample, spec):
    r = ok("plot", **spec)
    assert r["traces"], r
    json.dumps(r, allow_nan=False)


def test_plot_bar_sum_correct(sample):
    r = ok("plot", kind="bar", x=2, ys=[4], agg="sum")
    want = expected().groupby("group")["count"].sum()
    t = r["traces"][0]
    assert dict(zip(t["x"], t["y"])) == {k: float(v) for k, v in want.items()}


def test_plot_draws_every_point(sample):
    r = ok("plot", kind="scatter", x=0, ys=[3])
    want = int(expected()["value"].notna().sum())
    assert len(r["traces"][0]["x"]) == want and not r["notes"]


def big_xy(n=200_000):
    rng = np.random.default_rng(3)
    return pd.DataFrame({"x": rng.normal(size=n), "y": rng.normal(size=n),
                         "g": np.array(["a", "b"])[rng.integers(0, 2, n)]})


def test_large_scatter_density_then_markers_when_zoomed():
    ex.STATE.df = ex.STATE.df0 = big_xy()
    r = ok("plot", kind="scatter", x=0, ys=[1], color=2)
    assert r["dynamic"] and r["points"] == 200_000
    heat = [t for t in r["traces"] if t["type"] == "heatmap"]
    assert len(heat) == 2 and len(heat[0]["z"]) == 450 and len(heat[0]["z"][0]) == 1200
    assert any("All 200,000 points" in n for n in r["notes"])
    assert r["layout"]["showlegend"]  # legend entries for the two coloured layers
    # zoom into a small window: every point in it comes back as a marker
    view = {"x0": 0.0, "x1": 0.1, "y0": 0.0, "y1": 0.1}
    r = ok("plot", kind="scatter", x=0, ys=[1], view=view)
    df = ex.STATE.df
    inside = int(((df.x >= 0) & (df.x <= 0.1) & (df.y >= 0) & (df.y <= 0.1)).sum())
    assert r["traces"][0]["type"] == "scatter" and len(r["traces"][0]["x"]) == inside
    assert r["layout"]["xaxis"]["range"] == [0.0, 0.1]


def test_line_and_bar_use_every_row():
    n = 120_000
    df = pd.DataFrame({"v": np.arange(n, dtype=float)}, index=pd.date_range("2020-01-01", periods=n, freq="min"))
    ex.STATE.df = ex.STATE.df0 = df
    r = ok("plot", kind="line", x=-1, ys=[0])
    t = r["traces"][0]
    assert len(t["x"]) == n and r["layout"]["xaxis"]["type"] == "date"
    assert t["x"][0] == pd.Timestamp("2020-01-01").value / 1e6  # epoch ms on a date axis
    r = ok("plot", kind="bar", ys=[0])
    assert r["traces"][0]["type"] == "scatter" and len(r["traces"][0]["y"]) == n  # filled step line


def test_bar_categories_beyond_limit_fold_into_other():
    n = 6000
    df = pd.DataFrame({"k": [f"k{i % 3000}" for i in range(n)], "v": 1.0})
    ex.STATE.df = ex.STATE.df0 = df
    r = ok("plot", kind="bar", x=0, ys=[1], agg="sum")
    t = r["traces"][0]
    assert len(t["x"]) == ex.MAX_CATEGORIES + 1 and t["x"][-1] == "(other)"
    assert sum(t["y"]) == n  # every row counted
    assert any("(other)" in note for note in r["notes"])


def test_colour_groups_beyond_limit_fold_into_other():
    df = pd.DataFrame({"x": np.arange(100.0), "y": np.arange(100.0), "g": [f"g{i}" for i in range(100)]})
    ex.STATE.df = ex.STATE.df0 = df
    r = ok("plot", kind="scatter", x=0, ys=[1], color=2)
    names = [t["name"] for t in r["traces"]]
    assert len(names) == ex.MAX_GROUPS + 1 and names[-1] == "(other)"
    assert sum(len(t["x"]) for t in r["traces"]) == 100


# --- expressions that change df ------------------------------------------------

def test_assigning_a_column_updates_the_view(sample):
    r = ok("eval", expr="df['New'] = df['count'] ** 2")
    assert r["ok"] and r["kind"] == "frame" and "New column: New" in r["message"]
    assert r["view"]["columns"][-1]["name"] == "New" and not r["view"]["is_original"]
    assert "New" not in ex.STATE.df0.columns  # the loaded data is untouched
    cs = ok("colstats")
    assert cs["columns"][-1]["name"] == "New" and cs["total_cols"] == 8
    assert ok("plot", kind="scatter", x=4, ys=[7])["traces"]
    t = ok("table", n=2, col_start=7, col_count=1)
    assert t["columns"] == ["New"] and t["rows"][0][0] == str(expected()["count"][0] ** 2)
    assert ok("reset")["view"]["cols"] == 7


def test_user_example_with_range_index(sample):
    r = ok("eval", expr="df['New'] = pd.Series(range(df.shape[1])) ** 2")
    df = ex.STATE.df
    assert df["New"].iloc[:7].tolist() == [i ** 2 for i in range(7)] and df["New"].iloc[7:].isna().all()
    assert "Note" not in r["message"]


def test_misaligned_assignment_gets_a_hint():
    ex.STATE.df = ex.STATE.df0 = pd.DataFrame({"a": [1.0, 2.0]}, index=pd.date_range("2024-01-01", periods=2))
    r = ok("eval", expr="df['New'] = pd.Series(range(2))")
    assert "entirely empty" in r["message"] and ".to_numpy()" in r["message"]


@pytest.mark.parametrize("expr,cols", [
    ("del df['name']", 6),
    ("df.insert(0, 'first', 1)", 8),
    ("df.drop(columns=['name'], inplace=True)", 6),
    ("df = df.dropna()", 7),
    ("df.loc[:, 'value'] = 0", 7),
    ("df['a'], df['b'] = 1, 2", 9),
])
def test_other_df_changes(sample, expr, cols):
    r = ok("eval", expr=expr)
    assert r["kind"] == "frame" and r["view"]["cols"] == cols and not r["view"]["is_original"]
    assert ex.STATE.df0.shape == (500, 7)


def test_statements_that_dont_touch_df_leave_view_alone(sample):
    r = ok("eval", expr="x = df['count'].sum()\nx * 2")
    assert r["kind"] == "value" and r["view"]["is_original"]
    r = ok("eval", expr="y = df.head()")
    assert r["kind"] == "value" and r["view"]["is_original"]


def test_changes_build_on_each_other(sample):
    ok("eval", expr="df['a'] = 1")
    ok("eval", expr="df['b'] = df['a'] + 1")
    assert list(ex.STATE.df.columns[-2:]) == ["a", "b"] and ex.STATE.df["b"].iloc[0] == 2


def test_plot_labels_are_sanitised():
    load("xss.csv")
    r = ok("plot", kind="bar", x=0, ys=[1], agg="sum")
    blob = json.dumps(r, ensure_ascii=False)
    assert "<img" not in blob and "‹img" in blob


def test_box_rejects_text(sample):
    out = call("plot", kind="box", ys=[2])
    assert not out["ok"]


# --- protocol --------------------------------------------------------------

def test_dispatch_is_strict_json(sample):
    ok("eval", expr="df.assign(z=np.inf)")
    raw = ex.dispatch("table", json.dumps({"n": 3}))
    json.loads(raw, parse_constant=lambda c: pytest.fail(f"non-JSON constant {c}"))
    assert json.loads(raw)["result"]["rows"][0][-1] == "inf"


def test_wheel_integrity_check(monkeypatch):
    monkeypatch.setattr(ex, "_have_openpyxl", lambda: False)
    with pytest.raises(ex.UserError, match="integrity"):
        ex.install_wheels([("openpyxl-3.1.5-py2.py3-none-any.whl", b"tampered")])
    with pytest.raises(ex.UserError, match="Unexpected"):
        ex.install_wheels([("evil.whl", b"")])


# --- applied steps -----------------------------------------------------------

def steps():
    return ex.STATE.steps


def test_steps_list_what_produced_the_view(sample):
    ok("eval", expr="len(df)")                       # inspection: not a step
    ok("eval", expr="print(df.shape)")
    assert steps() == []
    ok("eval", expr="k = 2")                         # defines a name used later
    ok("eval", expr="df['v2'] = df.value * k")
    r = ok("eval", expr="df[df['count'] > 50]")      # the result becomes the view
    s = r["view"]["steps"]
    assert [x["kind"] for x in s] == ["define", "update", "view"]
    assert s[2]["code"] == "df = df[df['count'] > 50]" and s[2]["expr"] == "df[df['count'] > 50]"
    assert s[2]["shape"] == list(ex.STATE.df.shape) and s[1]["shape"] == [500, 8]


def test_view_steps_become_assignments_that_replay(sample):
    ok("eval", expr="x = 1\n# keep the big ones\ndf.value.where(df.value > 9)  # trailing note")
    ok("eval", expr="df.index")
    ok("eval", expr="é = 'ü'; df.T.values")
    code = [x["code"] for x in steps()]
    assert code[0] == "x = 1\n# keep the big ones\ndf = (df.value.where(df.value > 9)).to_frame()  # trailing note"
    assert code[1] == "df = pd.DataFrame(df.index)"
    assert code[2] == "é = 'ü'; df = pd.DataFrame(df.T.values)"
    # replaying the steps from the loaded data gives the same shape
    ns = {"df": ex.STATE.df0.copy(), "pd": pd, "np": np}
    for c in code:
        exec(c, ns)
    assert ns["df"].shape == ex.STATE.df.shape


def test_failed_runs_are_not_steps_and_reset_clears_them(sample):
    ok("eval", expr="df['z'] = 1")
    ok("eval", expr="df['y'] = undefined_name")
    assert [x["expr"] for x in steps()] == ["df['z'] = 1"]
    ok("eval", expr="k = 3")
    r = ok("reset")
    assert r["view"]["steps"] == [] and r["view"]["is_original"]
    # reset also starts a fresh namespace, so nothing outside the list affects later runs
    assert not ok("eval", expr="k")["ok"]


def test_loading_a_file_clears_the_steps(sample):
    ok("eval", expr="df['z'] = 1")
    load("sample.csv")
    assert steps() == []
