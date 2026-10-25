"""Generate the small fixture files in tests/fixtures/ (one or more per format).

Run once (the outputs are committed); requires pandas, pyarrow, h5py, tables, openpyxl.
"""
import os
import pickle
import warnings

import h5py
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

OUT = os.path.join(os.path.dirname(__file__), "fixtures")
XSS = '<img src=x onerror="document.title=\'pwned\'">'


def frame(n=500):
    rng = np.random.default_rng(42)
    df = pd.DataFrame({
        "id": np.arange(n),
        "name": [f"item {i}" for i in range(n)],
        "group": np.array(["red", "green", "blue", "eg"])[rng.integers(0, 4, n)],
        "value": rng.normal(10, 2, n).round(3),
        "count": rng.integers(0, 100, n),
        "when": pd.date_range("2024-01-01", periods=n, freq="h"),
        "ok": rng.random(n) < 0.5,
    })
    df.loc[df.index % 7 == 0, "value"] = np.nan
    df.loc[df.index % 11 == 0, "name"] = None
    return df


class Evil:
    def __reduce__(self):
        return (os.system, ("echo pwned",))


def main():
    os.makedirs(OUT, exist_ok=True)
    df = frame()
    df.to_csv(f"{OUT}/sample.csv", index=False)
    df.to_csv(f"{OUT}/sample.tsv", index=False, sep="\t")
    df.to_json(f"{OUT}/records.json", orient="records", date_format="iso")
    df.to_json(f"{OUT}/lines.jsonl", orient="records", lines=True, date_format="iso")
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), f"{OUT}/sample.parquet", row_group_size=100)
    df.set_index("name").to_parquet(f"{OUT}/indexed.parquet")
    with pd.ExcelWriter(f"{OUT}/sample.xlsx") as xw:
        df.head(200).to_excel(xw, sheet_name="Data", index=False)
        pd.DataFrame({"k": ["a", "b"], "v": [1, 2]}).to_excel(xw, sheet_name="Small", index=False)

    pd.DataFrame({XSS: [XSS, "safe"], "n": [1, 2]}).to_csv(f"{OUT}/xss.csv", index=False)

    # wide: 2,500 columns, date index (the usual case) and a text-keyed variant
    rng = np.random.default_rng(7)
    wide = pd.DataFrame(rng.normal(size=(120, 2500)).round(3),
                        index=pd.date_range("2020-01-01", periods=120, freq="D", name="date"),
                        columns=[f"c{i:04d}" for i in range(2500)])
    wide.iloc[::4, ::5] = np.nan
    wide.to_csv(f"{OUT}/wide.csv")
    keyed = wide.iloc[:, :1200].copy()
    keyed.index = pd.Index([f"well-{i:03d}" for i in range(120)], name="well")
    keyed.to_parquet(f"{OUT}/wide_keyed.parquet")

    h5 = f"{OUT}/sample.h5"
    if os.path.exists(h5):
        os.remove(h5)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df.to_hdf(h5, key="pandas_fixed", format="fixed")
        df.to_hdf(h5, key="nested/pandas_table", format="table")
        df.to_hdf(h5, key="pandas_table_dc", format="table", data_columns=True)
        df.set_index("when")["value"].to_hdf(h5, key="pandas_series")
    with h5py.File(h5, "a") as f:
        f["raw/matrix"] = np.arange(60, dtype=float).reshape(20, 3)
        f["raw/vector"] = np.arange(10, dtype=np.int32)
        rec = np.zeros(5, dtype=[("a", "<i4"), ("b", "<f8"), ("label", "S5")])
        rec["a"] = np.arange(5)
        rec["b"] = np.linspace(0, 1, 5)
        rec["label"] = [b"x", b"yy", b"zzz", b"w", b"v"]
        f["raw/records"] = rec

    # HDF5 whose pickled attributes try to run code: must be refused, not executed.
    evil = f"{OUT}/malicious.h5"
    if os.path.exists(evil):
        os.remove(evil)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pd.DataFrame({"a": [1, 2, 3]}).to_hdf(evil, key="t", format="table")
        pd.DataFrame({"s": ["x", "y"]}).to_hdf(evil, key="f", format="fixed")
    with h5py.File(evil, "a") as f:
        f["t/table"].attrs["values_block_0_kind"] = np.bytes_(pickle.dumps(Evil(), protocol=0))
        del f["f/block0_values"]
        dt = h5py.vlen_dtype(np.uint8)
        ds = f["f"].create_dataset("block0_values", shape=(1,), dtype=dt)
        ds[0] = np.frombuffer(pickle.dumps(Evil()), dtype=np.uint8)
        ds.attrs["PSEUDOATOM"] = np.bytes_(b"object")
    print("fixtures written to", OUT)


if __name__ == "__main__":
    main()
