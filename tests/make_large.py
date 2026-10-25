"""Generate ~5 GB test files in tests/large/: one per streamable format, plus wide
(2,000-column, date-indexed) csv / parquet / h5.

Usage: python tests/make_large.py [--gb 5] [--only csv,parquet,...]

Files are deterministic (seeded) and skipped if they already exist at the
right size, so re-running is cheap. They are git-ignored.
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

OUT = os.path.join(os.path.dirname(__file__), "large")
CHUNK = 1_000_000
CATS = np.array(["alpha", "beta", "gamma", "delta", "eps<ilon>"])


def chunk_frame(i, rng):
    n = CHUNK
    start = i * n
    return pd.DataFrame(
        {
            "id": np.arange(start, start + n, dtype=np.int64),
            "ts": pd.Timestamp("2020-01-01") + pd.to_timedelta(np.arange(start, start + n), unit="s"),
            "category": CATS[rng.integers(0, len(CATS), n)],
            "value": rng.normal(100, 15, n).round(4),
            "count": rng.integers(0, 1000, n),
            "flag": rng.random(n) < 0.3,
        }
    )


def done(path, target):
    return os.path.exists(path) and os.path.getsize(path) >= target


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_text(path, target, fmt):
    if done(path, target):
        log(f"skip {path}")
        return
    rng = np.random.default_rng(0)
    tmp = path + ".part"
    with open(tmp, "w", newline="") as f:
        if fmt == "json":
            f.write("[\n")
        i = 0
        while f.tell() < target:
            df = chunk_frame(i, rng)
            if fmt == "csv":
                df.to_csv(f, header=(i == 0), index=False)
            else:
                body = df.to_json(orient="records", lines=True, date_format="iso")
                if fmt == "json":
                    body = (",\n" if i else "") + body.rstrip("\n").replace("\n", ",\n")
                f.write(body)
            i += 1
            log(f"{fmt}: {f.tell() / 1e9:.2f} GB")
        if fmt == "json":
            f.write("\n]\n")
    os.replace(tmp, path)


def write_parquet(path, target):
    if done(path, target):
        log(f"skip {path}")
        return
    import pyarrow as pa
    import pyarrow.parquet as pq

    rng = np.random.default_rng(0)
    tmp = path + ".part"
    writer = None
    i = 0
    while True:
        df = chunk_frame(i, rng)
        # incompressible float columns so the file really is ~target bytes
        for k in range(6):
            df[f"f{k}"] = rng.random(CHUNK)
        table = pa.Table.from_pandas(df, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(tmp, table.schema, compression="snappy")
        writer.write_table(table, row_group_size=CHUNK)
        i += 1
        size = os.path.getsize(tmp)
        log(f"parquet: {size / 1e9:.2f} GB")
        if size >= target:
            break
    writer.close()
    os.replace(tmp, path)


def write_h5(path, target):
    if done(path, target):
        log(f"skip {path}")
        return
    import h5py

    rng = np.random.default_rng(0)
    tmp = path + ".part"
    ncols = 10
    rows = target // (ncols * 8) + CHUNK
    rows -= rows % CHUNK
    dtype = np.dtype([("id", "<i8"), ("value", "<f8"), ("label", "S8")])
    with h5py.File(tmp, "w") as f:
        grid = f.create_dataset("sensors/grid", shape=(rows, ncols), dtype="<f8", chunks=(65536, ncols))
        rec = f.create_dataset("records", shape=(CHUNK * 4,), dtype=dtype)
        for i in range(rows // CHUNK):
            grid[i * CHUNK:(i + 1) * CHUNK] = rng.random((CHUNK, ncols))
            log(f"h5: {(i + 1) * CHUNK * ncols * 8 / 1e9:.2f} GB")
        for i in range(4):
            arr = np.zeros(CHUNK, dtype=dtype)
            arr["id"] = np.arange(i * CHUNK, (i + 1) * CHUNK)
            arr["value"] = rng.random(CHUNK)
            arr["label"] = np.char.encode(CATS[rng.integers(0, 4, CHUNK)])
            rec[i * CHUNK:(i + 1) * CHUNK] = arr
    os.replace(tmp, path)


WIDE_COLS = 2000
WIDE_CHUNK = 10_000


def wide_chunk(i, rng):
    """10,000 rows × 2,000 float columns with a minute-spaced date index."""
    start = pd.Timestamp("2020-01-01") + pd.Timedelta(minutes=i * WIDE_CHUNK)
    idx = pd.date_range(start, periods=WIDE_CHUNK, freq="min", name="time")
    data = rng.normal(0, 1, (WIDE_CHUNK, WIDE_COLS)).astype("float32").astype("float64").round(3)
    data[rng.random(data.shape) < 0.01] = np.nan
    return pd.DataFrame(data, index=idx, columns=[f"sensor_{c:04d}" for c in range(WIDE_COLS)])


def write_wide(path, target, fmt):
    if done(path, target):
        log(f"skip {path}")
        return
    rng = np.random.default_rng(1)
    tmp = path + ".part"
    i = 0
    if fmt == "csv":
        with open(tmp, "w", newline="") as f:
            while f.tell() < target:
                wide_chunk(i, rng).to_csv(f, header=(i == 0), float_format="%.3f")
                i += 1
                log(f"wide csv: {f.tell() / 1e9:.2f} GB")
    elif fmt == "parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq
        writer = None
        while True:
            # two chunks per row group: ~320 MB uncompressed each, far above the per-group budget
            df = pd.concat([wide_chunk(i, rng), wide_chunk(i + 1, rng)])
            table = pa.Table.from_pandas(df)
            if writer is None:
                writer = pq.ParquetWriter(tmp, table.schema, compression="snappy")
            writer.write_table(table, row_group_size=len(df))
            i += 2
            size = os.path.getsize(tmp)
            log(f"wide parquet: {size / 1e9:.2f} GB")
            if size >= target:
                break
        writer.close()
    else:
        import h5py
        rows = int(target // (WIDE_COLS * 8)) // WIDE_CHUNK * WIDE_CHUNK + WIDE_CHUNK
        with h5py.File(tmp, "w") as f:
            ds = f.create_dataset("wide", shape=(rows, WIDE_COLS), dtype="<f8", chunks=(256, WIDE_COLS))
            for i in range(rows // WIDE_CHUNK):
                ds[i * WIDE_CHUNK:(i + 1) * WIDE_CHUNK] = wide_chunk(i, rng).to_numpy()
                log(f"wide h5: {(i + 1) * WIDE_CHUNK * WIDE_COLS * 8 / 1e9:.2f} GB")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gb", type=float, default=5.0)
    ap.add_argument("--only", default="csv,jsonl,json,parquet,h5,wide_csv,wide_parquet,wide_h5")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    target = int(args.gb * 1e9)
    for fmt in args.only.split(","):
        if fmt.startswith("wide_"):
            kind = fmt[len("wide_"):]
            write_wide(os.path.join(OUT, f"big_wide.{kind}"), target, kind)
            continue
        path = os.path.join(OUT, f"big.{fmt}")
        if fmt in ("csv", "jsonl", "json"):
            write_text(path, target, fmt)
        elif fmt == "parquet":
            write_parquet(path, target)
        elif fmt == "h5":
            write_h5(path, target)
        else:
            sys.exit(f"unknown format {fmt}")
    log("all done")


if __name__ == "__main__":
    main()
