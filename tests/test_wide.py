"""Wide data (thousands of columns) and index handling, natively.

Dated indexes are the common case but not the only one, so both are covered.
"""
import json
import os
import sys
import time
import warnings

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import explorer as ex  # noqa: E402


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
    out = hydrate(json.loads(ex.dispatch(kind, json.dumps(payload))), ex.take_buffers())
    assert out["ok"], out
    return out["result"]


def wide(n=400, m=3000, dated=True, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2015-01-01", periods=n, freq="D", name="date") if dated else pd.Index(
        [f"id-{i:05d}" for i in range(n)], name="key")
    df = pd.DataFrame(rng.normal(size=(n, m)).round(4), index=idx, columns=[f"c{i:04d}" for i in range(m)])
    df.iloc[::5, ::4] = np.nan
    return df


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("wide")
    out = {}
    dated, keyed = wide(), wide(dated=False)
    dated.to_csv(d / "dated.csv")
    keyed.to_csv(d / "keyed.csv")
    dated.reset_index().to_csv(d / "datecol.csv", index=False)  # dates as a named first column
    dated.to_csv(d / "noindex.csv", index=False)  # no index at all
    dated.to_parquet(d / "dated.parquet")
    keyed.to_parquet(d / "keyed.parquet")
    dated.reset_index(drop=True).to_parquet(d / "plain.parquet")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dated.iloc[:, :500].to_hdf(d / "w.h5", key="fixed_dated", format="fixed")
        dated.iloc[:, :500].to_hdf(d / "w.h5", key="table_dated", format="table")
        keyed.iloc[:, :500].to_hdf(d / "w.h5", key="fixed_keyed", format="fixed")
    import h5py
    with h5py.File(d / "w.h5", "a") as f:
        f["raw2d"] = np.arange(400 * 3000, dtype="f8").reshape(400, 3000)
    with pd.ExcelWriter(d / "w.xlsx") as xw:
        dated.iloc[:60, :300].to_excel(xw, sheet_name="S")
    dated.reset_index().to_json(d / "w.jsonl", orient="records", lines=True, date_format="iso")
    for p in d.iterdir():
        out[p.name] = str(p)
    return out


def load(files, name, **kw):
    return ok("load", path=files[name], name=name, budget_mb=kw.pop("budget_mb", 500), **kw)


# --- index choice ------------------------------------------------------------

@pytest.mark.parametrize("name", ["dated.csv", "datecol.csv", "dated.parquet", "w.jsonl"])
def test_dated_index_detected(files, name):
    res = load(files, name)
    df = ex.STATE.df
    assert isinstance(df.index, pd.DatetimeIndex), (name, df.index.dtype)
    assert df.shape == (400, 3000)
    assert res["view"]["cols"] == 3000 and len(res["view"]["columns"]) == 3000
    assert df.index[0] == pd.Timestamp("2015-01-01")


@pytest.mark.parametrize("name", ["keyed.csv", "keyed.parquet"])
def test_non_dated_saved_index(files, name):
    load(files, name)
    df = ex.STATE.df
    assert df.index.name == "key" or name.endswith(".csv")
    assert df.index[0] == "id-00000" and df.shape == (400, 3000)


def test_no_index_csv_keeps_row_numbers(files):
    load(files, "noindex.csv")
    assert isinstance(ex.STATE.df.index, pd.RangeIndex) and ex.STATE.df.shape == (400, 3000)


def test_parquet_without_index_uses_row_numbers(files):
    load(files, "plain.parquet")
    assert isinstance(ex.STATE.df.index, pd.RangeIndex)


def test_index_mode_override(files):
    load(files, "dated.csv", index="none")
    assert isinstance(ex.STATE.df.index, pd.RangeIndex) and ex.STATE.df.shape[1] == 3001
    load(files, "noindex.csv", index="first")
    assert ex.STATE.df.index.name == "c0000"
    load(files, "dated.parquet", index="none")
    assert ex.STATE.df.columns[0] == "date"


@pytest.mark.parametrize("key", ["/fixed_dated", "/table_dated"])
def test_h5_dated_index(files, key):
    load(files, "w.h5", key=key)
    df = ex.STATE.df
    assert isinstance(df.index, pd.DatetimeIndex) and df.index[1] == pd.Timestamp("2015-01-02")
    assert df.shape == (400, 500)


def test_h5_string_index(files):
    load(files, "w.h5", key="/fixed_keyed")
    assert ex.STATE.df.index[0] == "id-00000"


def test_xlsx_saved_index(files):
    pytest.importorskip("openpyxl")
    load(files, "w.xlsx", key="S")
    df = ex.STATE.df
    assert isinstance(df.index, pd.DatetimeIndex) and df.shape == (60, 300)


def test_sampled_text_index_is_labelled(tmp_path):
    p = tmp_path / "n.csv"
    pd.DataFrame({"a": np.arange(200_000), "b": 1.5}).to_csv(p, index=False)
    ok("load", path=str(p), name="n.csv", budget_mb=1)
    assert ex.STATE.df.index.name == "sample_row"


# --- table on wide frames -------------------------------------------------------

def test_table_column_paging_and_filter(files):
    load(files, "dated.csv")
    t = ok("table", n=10, col_start=2950, col_count=100)
    assert t["columns"][0] == "c2950" and len(t["columns"]) == 50  # last partial page
    assert t["col_positions"][0] == 2950 and t["matched_cols"] == 3000
    assert t["index"][:2] == ["2015-01-01", "2015-01-02"]  # dates without times
    assert len(t["rows"]) == 10 and len(t["rows"][0]) == 50
    t = ok("table", n=5, col_filter="C29", col_count=50)
    assert t["matched_cols"] == 100 and all(c.startswith("c29") for c in t["columns"])
    t = ok("table", n=5, col_filter="nope")
    assert t["matched_cols"] == 0 and t["columns"] == [] and len(t["rows"]) == 5


def test_table_sort_by_far_column(files):
    load(files, "dated.csv")
    t = ok("table", n=20, sort=2999, ascending=False, col_start=2990, col_count=10)
    j = t["col_positions"].index(2999)
    vals = [float(r[j]) for r in t["rows"]]
    assert vals == sorted(vals, reverse=True)
    t = ok("table", n=3, sort=-1, ascending=False)
    assert t["index"][0] == ex.STATE.df.index.max().strftime("%Y-%m-%d")


def test_table_is_fast_on_wide(files):
    load(files, "dated.csv")
    t0 = time.time()
    for start in range(0, 3000, 500):
        ok("table", n=100, col_start=start, col_count=200)
    assert time.time() - t0 < 2


def test_transposed_wide_view(files):
    load(files, "dated.csv")
    r = ok("eval", expr="df.T")
    assert r["view"]["cols"] == 400 and r["view"]["rows"] == 3000
    t = ok("table", n=5, col_count=3)
    assert t["columns"] == ["2015-01-01", "2015-01-02", "2015-01-03"]
    assert t["index"][0] == "c0000"


def test_date_slicing_expression(files):
    load(files, "dated.csv")
    r = ok("eval", expr='df.loc["2015-02"]')
    assert r["view"]["rows"] == 28


# --- column stats on wide frames ----------------------------------------------------

def test_colstats_wide_paged_filtered_sorted(files):
    load(files, "dated.csv")
    t0 = time.time()
    c = ok("colstats", limit=200)
    assert time.time() - t0 < 10
    assert c["matched"] == 3000 and c["total_cols"] == 3000 and len(c["columns"]) == 200
    df = ex.STATE.df
    first = c["columns"][0]
    s = df["c0000"]
    assert first["nulls"] == int(s.isna().sum()) and first["unique"] == s.nunique()
    assert first["idxmin"] == s.idxmin().strftime("%Y-%m-%d")
    assert float(first["mean"]) == pytest.approx(s.mean())
    c = ok("colstats", start=2900, limit=200)
    assert c["columns"][0]["name"] == "c2900" and len(c["columns"]) == 100
    c = ok("colstats", col_filter="c000", limit=50)
    assert [r["name"] for r in c["columns"]] == [f"c{i:04d}" for i in range(10)]
    c = ok("colstats", sort="nulls", ascending=False, limit=5)
    assert all(r["nulls"] == 80 for r in c["columns"])  # every 4th column has every 5th row null
    c = ok("colstats", sort="mean", ascending=True, limit=3000)
    means = [float(r["mean"]) for r in c["columns"]]
    assert means == sorted(means)


def test_colstats_cached_until_view_changes(files):
    load(files, "dated.csv")
    ok("colstats")
    t0 = time.time()
    ok("colstats", start=1000)
    assert time.time() - t0 < 0.5
    ok("eval", expr="df.iloc[:, :10]")
    assert ok("colstats")["total_cols"] == 10


def test_colstats_vectorised_matches_pandas_on_mixed():
    df = pd.DataFrame({
        "i": [3, 1, 2, None], "f": [1.5, np.nan, -2.0, 4.0], "b": [True, False, True, None],
        "s": ["x", "y", None, "x"], "d": pd.to_datetime(["2020-01-01", None, "2019-05-05", "2021-01-01"]),
        "big": np.array([2 ** 60, 1, 2, 3], dtype="int64"), "allnan": np.nan,
    }, index=pd.date_range("2024-01-01", periods=4, name="t"))
    ex.STATE.df = ex.STATE.df0 = df
    ex.STATE.format = "csv"
    ex.STATE.info = {}
    rows = {r["name"]: r for r in ok("colstats")["columns"]}
    assert rows["i"]["min"] == "1" and rows["i"]["idxmin"] == "2024-01-02" and rows["i"]["unique"] == 3
    assert rows["f"]["idxmax"] == "2024-01-04" and rows["f"]["nulls"] == 1
    assert rows["big"]["max"] == str(2 ** 60)
    assert rows["s"]["unique"] == 2 and rows["d"]["idxmin"] == "2024-01-03"
    assert rows["allnan"]["count"] == 0 and "mean" not in rows["allnan"]


def test_overall_index_info(files):
    load(files, "dated.csv")
    o = ok("overall")["index"]
    assert o["is_dates"] and o["freq"] == "D" and o["monotonic"] == "increasing" and o["unique"]
    assert o["min"] == "2015-01-01" and o["max"] == "2016-02-04"
    load(files, "keyed.csv")
    o = ok("overall")["index"]
    assert not o["is_dates"] and o["min"] == "id-00000" and "freq" not in o


# --- sampling wide files stays inside the budget -------------------------------------

def test_wide_csv_sampling(tmp_path):
    # 2,000 columns ≈ 18 KB per line: blocks must still contain whole lines
    df = wide(n=3000, m=2000)
    p = tmp_path / "w.csv"
    df.to_csv(p)
    res = ok("load", path=str(p), name="w.csv", budget_mb=10)
    got = ex.STATE.df
    assert res["info"]["sampled"] and got.shape[1] == 2000
    assert ex.mem_bytes(got) < 20 * ex.MB
    assert isinstance(got.index, pd.DatetimeIndex)
    assert got.index[0] == df.index[0] and got.index[-1] == df.index[-1]
    assert got.index.is_monotonic_increasing and got.index.is_unique
    np.testing.assert_allclose(got.loc[got.index[5]].to_numpy(dtype=float), df.loc[got.index[5]].to_numpy(dtype=float),
                               equal_nan=True)
    months = got.index.to_period("Q").nunique()
    assert months >= 10  # spread over (almost) every quarter of the ~8 years


def test_wide_parquet_huge_row_groups(tmp_path):
    df = wide(n=4000, m=1000)
    p = tmp_path / "w.parquet"
    pq.write_table(pa.Table.from_pandas(df), p, row_group_size=1000)  # 4 groups of ~8 MB each
    res = ok("load", path=str(p), name="w.parquet", budget_mb=12)
    got = ex.STATE.df
    assert res["info"]["sampled"] and got.shape[1] == 1000
    assert isinstance(got.index, pd.DatetimeIndex) and got.index[0] == df.index[0]
    assert ex.mem_bytes(got) < 18 * ex.MB
    assert got.index.year.nunique() >= 4  # rows from every row group
    # a small budget reads fewer of the big groups, but still the first and last
    res = ok("load", path=str(p), name="w.parquet", budget_mb=2)
    got = ex.STATE.df
    assert res["info"]["sampled"] and ex.mem_bytes(got) < 4 * ex.MB
    assert got.index[0] == df.index[0] and got.index.max() >= df.index[3000]


def test_wide_h5_probe_is_small(tmp_path):
    import h5py
    p = tmp_path / "w.h5"
    with h5py.File(p, "w") as f:
        f["x"] = np.arange(20000 * 2000, dtype="f8").reshape(20000, 2000)  # 320 MB
    calls = []
    orig = ex.H5Reader.read

    def spy(self, a, b):
        calls.append(b - a)
        return orig(self, a, b)

    ex.H5Reader.read = spy
    try:
        res = ok("load", path=str(p), name="w.h5", key="/x", budget_mb=8)
    finally:
        ex.H5Reader.read = orig
    df = ex.STATE.df
    assert res["info"]["sampled"]
    assert calls[0] <= 600  # first read ~8 MB, not 10,000 rows (160 MB)
    assert ex.mem_bytes(df) < 12 * ex.MB
    assert df.index[0] == 0 and df.index[-1] == 19999
    assert len(np.unique(df.index // 2000)) >= 8  # blocks from across the dataset


def test_wide_json_array_batches(tmp_path):
    df = wide(n=3000, m=500).reset_index()
    p = tmp_path / "w.json"
    df.to_json(p, orient="records", date_format="iso")
    res = ok("load", path=str(p), name="w.json", budget_mb=2)
    assert res["info"]["sampled"] and ex.mem_bytes(ex.STATE.df) < 12 * ex.MB
    assert isinstance(ex.STATE.df.index, pd.DatetimeIndex)


# --- plots with dated index ---------------------------------------------------------

def test_line_plot_against_dated_index(files):
    load(files, "dated.csv")
    r = ok("plot", kind="line", x=-1, ys=[0, 2999])
    assert len(r["traces"]) == 2 and len(r["traces"][0]["x"]) == 400
    assert r["layout"]["xaxis"]["type"] == "date" and r["traces"][0]["x"][0] == pd.Timestamp("2015-01-01").value / 1e6
    assert r["layout"]["xaxis"]["title"]["text"] == "date"
