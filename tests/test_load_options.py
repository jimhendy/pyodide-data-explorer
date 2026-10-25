"""Loading a subset of columns, filtering rows with SQL, and SQL queries (DuckDB)."""
import json
import os
import sys

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
import explorer as ex  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def fx(name):
    return os.path.join(FIX, name)


def call(kind, /, **payload):
    return json.loads(ex.dispatch(kind, json.dumps(payload)))


def ok(kind, /, **payload):
    out = call(kind, **payload)
    assert out["ok"], out
    return out["result"]


def load(name, path=None, **kw):
    return ok("load", path=path or fx(name), name=name, **kw)


def frame(n, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "id": np.arange(n),
        "ts": pd.date_range("2024-01-01", periods=n, freq="min"),
        "region": rng.choice(["north", "south", "east", "west"], n),
        "value": rng.normal(size=n).round(4),
        "count": rng.integers(0, 1000, n),
    })


@pytest.fixture(scope="module")
def big_parquet(tmp_path_factory):
    p = tmp_path_factory.mktemp("opts") / "big.parquet"
    pq.write_table(pa.Table.from_pandas(frame(200_000), preserve_index=False), p, row_group_size=10_000)
    return str(p)


@pytest.fixture(scope="module")
def big_csv(tmp_path_factory):
    p = tmp_path_factory.mktemp("opts") / "big.csv"
    frame(100_000).to_csv(p, index=False)
    return str(p)


# --- schema (for the dialog) --------------------------------------------------

@pytest.mark.parametrize("name", ["sample.parquet", "sample.csv", "sample.tsv", "lines.jsonl"])
def test_inspect_returns_schema(name):
    sc = ok("inspect", path=fx(name), name=name)["schema"]
    assert [c["name"] for c in sc["columns"]] == ["id", "name", "group", "value", "count", "when", "ok"]
    assert sc["total_cols"] == 7
    if name.endswith(".parquet"):
        assert sc["rows"] == 500 and sc["rows_exact"]


def test_schema_marks_stored_index_columns():
    sc = ok("inspect", path=fx("indexed.parquet"), name="indexed.parquet")["schema"]
    idx = [c["name"] for c in sc["columns"] if c["index"]]
    assert idx == ["name"] and sc["total_cols"] == 6  # the index isn't counted as a choosable column


def test_no_schema_for_other_formats():
    for name in ["sample.h5", "records.json", "sample.xlsx"]:
        res = call("inspect", path=fx(name), name=name)
        assert res["ok"] and not res["result"].get("schema"), name


def test_packages_only_pull_duckdb_when_needed():
    assert ex.packages_for(fx("sample.parquet"), "sample.parquet") == ["pyarrow"]
    assert ex.packages_for(fx("sample.parquet"), "sample.parquet", "columns") == ["pyarrow"]  # pyarrow reads them
    assert ex.packages_for(fx("sample.parquet"), "sample.parquet", "filter") == ["pyarrow", "duckdb"]
    assert ex.packages_for(fx("sample.csv"), "sample.csv", "columns") == ["pyarrow", "duckdb"]
    assert ex.packages_for(fx("sample.csv"), "sample.csv") == []


# --- parquet column subsets (pyarrow) ---------------------------------------------

def test_parquet_column_subset():
    res = load("sample.parquet", columns=["value", "name"])
    df = ex.STATE.df
    assert list(df.columns) == ["name", "value"]  # file order
    assert len(df) == 500
    assert res["info"]["selection"] == {"columns": 2, "of": 7, "where": None, "sql": None}
    assert res["view"]["cols"] == 2


def test_parquet_column_subset_keeps_stored_index():
    load("indexed.parquet", columns=["value"])
    df = ex.STATE.df
    assert list(df.columns) == ["value"] and df.index.name == "name"


def test_parquet_column_subset_when_sampled_reads_only_those_columns(big_parquet):
    res = load("big.parquet", path=big_parquet, columns=["id", "value"], budget_mb=0.5)
    df = ex.STATE.df
    assert res["info"]["sampled"] and list(df.columns) == ["id", "value"]
    # two narrow columns: the same budget holds far more rows than with all five
    full = load("big.parquet", path=big_parquet, budget_mb=0.5)
    assert len(df) > 1.5 * full["info"]["rows_loaded"]
    assert ex.STATE.df["id"].iloc[0] == 0


def test_unknown_or_empty_column_choice_is_an_error():
    err = call("load", path=fx("sample.parquet"), name="sample.parquet", columns=["nope"])
    assert not err["ok"] and "nope" in err["error"]
    err = call("load", path=fx("sample.parquet"), name="sample.parquet", columns=[])
    assert not err["ok"] and "at least one column" in err["error"]


# --- row filters (DuckDB) -----------------------------------------------------------

@pytest.mark.parametrize("name", ["sample.parquet", "sample.csv", "sample.tsv", "lines.jsonl"])
def test_where_filter_each_format(name):
    want = pd.read_csv(fx("sample.csv"))
    want = want[(want["value"] > 5) & (want["group"] == "red")]
    res = load(name, where="value > 5 AND \"group\" = 'red'")
    df = ex.STATE.df
    assert res["info"]["filtered"] and res["info"]["rows_matched"] == len(want)
    assert not res["info"]["sampled"]
    assert sorted(df["id"]) == sorted(want["id"])
    assert df.index.name == "row" and list(df.index) == list(want.index)  # row number in the file


def test_where_with_column_subset_can_filter_on_other_columns():
    load("sample.csv", columns=["value"], where="count > 50 AND id < 100")
    df = ex.STATE.df
    want = pd.read_csv(fx("sample.csv")).query("count > 50 and id < 100")
    assert list(df.columns) == ["value"] and len(df) == len(want)


def test_where_keeps_stored_parquet_index():
    load("indexed.parquet", where="value > 9")
    df = ex.STATE.df
    assert df.index.name == "name" and (df["value"] > 9).all()


def test_filtered_load_too_big_is_sampled_in_file_order(big_parquet):
    res = load("big.parquet", path=big_parquet, where="region = 'south'", budget_mb=0.5)
    info, df = res["info"], ex.STATE.df
    expected = (frame(200_000)["region"] == "south").sum()
    assert info["sampled"] and info["rows_matched"] == expected
    assert len(df) < expected and (df["region"] == "south").all()
    assert df.index.is_monotonic_increasing and (df.index == df["id"]).all()
    assert "random sample" in info["sample_method"]
    assert ex.mem_bytes(df) < 1.5 * ex.MB
    # whole-file parquet statistics don't describe filtered data
    assert not ok("colstats")["has_file_stats"]


def test_csv_filter_and_auto_dated_index(big_csv):
    res = load("big.csv", path=big_csv, where="count < 10")
    df = ex.STATE.df
    want = frame(100_000).query("count < 10")
    assert res["info"]["rows_matched"] == len(want) and len(df) == len(want)
    assert df["count"].lt(10).all()


def test_seeded_sample_is_repeatable(big_parquet):
    load("big.parquet", path=big_parquet, where="value > 0", budget_mb=0.3)
    a = ex.STATE.df["id"].to_numpy()
    load("big.parquet", path=big_parquet, where="value > 0", budget_mb=0.3)
    assert (ex.STATE.df["id"].to_numpy() == a).all()


# --- SQL queries -----------------------------------------------------------------

def test_sql_aggregate_query():
    load("sample.csv", sql='SELECT "group", count(*) AS n, avg(value) AS mean FROM data GROUP BY 1 ORDER BY n DESC')
    df = ex.STATE.df
    want = pd.read_csv(fx("sample.csv")).groupby("group").size().sort_values(ascending=False)
    assert list(df["n"]) == list(want)
    assert ex.STATE.info["selection"]["sql"].startswith("SELECT")


def test_sql_order_is_kept_when_sampled(big_parquet):
    res = load("big.parquet", path=big_parquet, sql="SELECT id, value FROM data ORDER BY value DESC", budget_mb=0.3)
    df = ex.STATE.df
    assert res["info"]["sampled"] and res["info"]["rows_matched"] == 200_000
    assert df["value"].is_monotonic_decreasing


def test_sql_trailing_semicolon_is_fine():
    load("sample.parquet", sql="SELECT * FROM data WHERE id < 3;")
    assert len(ex.STATE.df) == 3


# --- errors and the DuckDB sandbox ------------------------------------------------

def test_bad_filter_reports_duckdb_error():
    err = call("load", path=fx("sample.csv"), name="sample.csv", where="nope > 1")
    assert not err["ok"] and err["error"].startswith("Row filter failed") and "nope" in err["error"]
    err = call("load", path=fx("sample.csv"), name="sample.csv", sql="SELEC 1")
    assert not err["ok"] and err["error"].startswith("SQL query failed")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_text('" + os.path.abspath(__file__) + "')",
    "SELECT * FROM 'https://example.com/x.parquet'",
])
def test_sql_cannot_read_outside_the_files_folder(sql):
    err = call("load", path=fx("sample.csv"), name="sample.csv", sql=sql)
    assert not err["ok"], sql


def test_duckdb_settings_are_locked():
    con = ex._duck_connect(fx("sample.csv"), 10 * ex.MB)
    for stmt in ["SET enable_external_access = true", "SET allowed_directories = ['/']", "SET threads = 4"]:
        with pytest.raises(Exception):
            con.execute(stmt)
    with pytest.raises(Exception):
        con.execute("INSTALL httpfs")
    con.close()


def test_options_only_for_supported_formats():
    err = call("load", path=fx("records.json"), name="records.json", where="1=1")
    assert not err["ok"] and "parquet, CSV and JSON lines" in err["error"]


def test_plain_load_has_no_selection():
    res = load("sample.parquet")
    assert res["info"]["selection"] is None and not res["info"]["filtered"]


# --- the fast path (pyarrow streams, DuckDB filters) and its fallback --------------

@pytest.mark.parametrize("name", ["sample.parquet", "sample.csv", "sample.tsv", "lines.jsonl"])
def test_filters_stream_through_pyarrow(name):
    res = load(name, where="id < 10")
    assert res["info"]["reader"] == "pyarrow" and len(ex.STATE.df) == 10
    res = load(name, sql="SELECT \"group\", count(*) AS n FROM data GROUP BY 1")
    assert res["info"]["reader"] == "pyarrow"


def test_csv_type_change_after_first_block_falls_back_to_duckdb(tmp_path, monkeypatch):
    p = tmp_path / "drift.csv"
    lines = ["id,code"] + [f"{i},{i}" for i in range(5000)] + ["5000,not-a-number"]
    p.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(ex, "ARROW_BLOCK", 4096)  # first block sees only integers
    res = load("drift.csv", path=str(p), where="id >= 4998")
    assert res["info"]["reader"] == "duckdb"
    assert list(ex.STATE.df["code"].astype(str)) == ["4998", "4999", "not-a-number"]


def test_referenced_columns():
    names = ["id", "Value", "my col", "group"]
    assert ex._referenced_columns('value > 1 AND "my col" = \'x\'', names) == ["Value", "my col"]
    assert ex._referenced_columns("\"group\" IN ('a')", names) == ["group"]
    assert ex._needs_all_columns("SELECT * FROM data")
    assert not ex._needs_all_columns("SELECT count(*), avg(value) FROM data")
    assert ex._needs_all_columns("SELECT COLUMNS('val.*') FROM data")


def test_sql_columns_regex_and_self_join():
    load("sample.parquet", sql="SELECT COLUMNS('^(id|value)$') FROM data WHERE id < 3")
    assert list(ex.STATE.df.columns) == ["id", "value"]
    res = load("sample.csv", sql="SELECT a.id, b.value FROM data a JOIN data b ON a.id = b.id WHERE a.id < 4")
    assert res["info"]["reader"] == "duckdb" and len(ex.STATE.df) == 4  # a stream can't be read twice


def test_sql_only_streams_mentioned_columns(monkeypatch):
    seen = []
    orig = ex._ArrowSource.table

    def spy(self, columns=None, row=False):
        seen.append(columns)
        return orig(self, columns, row)

    monkeypatch.setattr(ex._ArrowSource, "table", spy)
    load("sample.parquet", sql='SELECT "group", avg(value) FROM data GROUP BY 1')
    assert seen and all(sorted(c) == ["group", "value"] for c in seen)


def test_text_file_index_column_is_always_loaded():
    sc = ok("inspect", path=fx("wide.csv"), name="wide.csv")["schema"]
    assert sc["columns"][0] == {"name": "date", "dtype": sc["columns"][0]["dtype"], "index": True}
    assert sc["total_cols"] == 2500
    res = load("wide.csv", columns=["c0001", "c2499"])
    df = ex.STATE.df
    assert df.index.name == "date" and str(df.index.dtype).startswith("datetime64")
    assert list(df.columns) == ["c0001", "c2499"]
    assert res["info"]["selection"]["columns"] == 2 and res["info"]["selection"]["of"] == 2500
    # with "Row numbers" it's left to the user
    load("wide.csv", columns=["c0001"], index="none")
    assert list(ex.STATE.df.columns) == ["c0001"]
