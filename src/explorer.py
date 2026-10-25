"""Data Explorer core: file loading, sampling, stats, plotting data, expressions.

Runs inside Pyodide (in a Web Worker inside a sandboxed iframe) and also
natively for tests. Nothing here touches the JS bridge; the worker calls
``dispatch(kind, payload_json)`` and gets a JSON string back.

Large files are never read whole. Each loader estimates the in-memory size of
the DataFrame from a small probe and, if the file would exceed the memory
budget, reads evenly spaced blocks across the file instead (the first and last
block are always included, so head/tail of the data are the file's real
head/tail).
"""
import ast
import codecs
import contextlib
import hashlib
import io
import json
import math
import os
import pickle
import random
import re
import sys
import time
import traceback
import warnings
import zipfile

import numpy as np
import pandas as pd

MB = 1024 * 1024
DEFAULT_BUDGET_MB = 200
MAX_TABLE_ROWS = 1000
MAX_TABLE_COLS = 200
DEFAULT_TABLE_COLS = 50
MAX_VIEW_COLS = 50000
MAX_COLSTATS_PAGE = 1000
MAX_CELL_CHARS = 300
MAX_VALUE_CHARS = 20000
MAX_SAMPLE_BLOCKS = 64
PROBE_BYTES = 4 * MB
SCAN_CHUNK = 16 * MB

# Pinned pure-Python wheels for xlsx support (served from our own origin and
# handed over by the page; verified here before being unpacked).
WHEEL_SHA256 = {
    "openpyxl-3.1.5-py2.py3-none-any.whl": "5282c12b107bffeef825f4617dc029afaf41d0ea60823bbb665ef3079dc79de2",
    "et_xmlfile-2.0.0-py3-none-any.whl": "7a91720bc756843502c3b7504c77b8fe44217c85c537d85037f0f536151b2caa",
}
WHEEL_DIR = "/tmp/explorer-wheels"

FORMATS_BY_EXT = {
    ".csv": "csv", ".tsv": "csv", ".txt": "csv",
    ".parquet": "parquet", ".pq": "parquet", ".parq": "parquet",
    ".h5": "h5", ".hdf5": "h5", ".hdf": "h5", ".he5": "h5",
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".json": "json",
    ".jsonl": "jsonl", ".ndjson": "jsonl",
}
PACKAGES = {"parquet": ["pyarrow"], "h5": ["h5py"]}


class UserError(Exception):
    """An error message meant for the user (no traceback)."""


def _noop_progress(msg):
    pass


_progress = _noop_progress


def set_progress(fn):
    global _progress
    _progress = fn


def progress(msg):
    try:
        _progress(str(msg))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_format(path, name=None):
    ext = os.path.splitext((name or path).lower())[1]
    if ext in FORMATS_BY_EXT:
        return FORMATS_BY_EXT[ext]
    with open(path, "rb") as f:
        head = f.read(8)
        f.seek(0, 2)
        size = f.tell()
        if size >= 4:
            f.seek(size - 4)
            tail = f.read(4)
        else:
            tail = b""
    if head.startswith(b"PAR1") and tail == b"PAR1":
        return "parquet"
    if head.startswith(b"\x89HDF"):
        return "h5"
    if head.startswith(b"PK"):
        return "xlsx"
    stripped = head.lstrip()
    if stripped.startswith(b"["):
        return "json"
    if stripped.startswith(b"{"):
        return "jsonl"
    return "csv"


def packages_for(path, name=None, options=""):
    """Pyodide packages needed to read this file. ``options`` is "filter" (a
    row filter or SQL query) or "columns" (a column subset): both need DuckDB,
    except parquet columns, which pyarrow reads directly."""
    fmt = detect_format(path, name)
    pkgs = list(PACKAGES.get(fmt, []))
    if options == "filter" or (options == "columns" and fmt != "parquet"):
        # pyarrow streams the file, DuckDB filters the stream
        pkgs += [p for p in ("pyarrow", "duckdb") if p not in pkgs]
    return pkgs


# ---------------------------------------------------------------------------
# Sampling helpers
# ---------------------------------------------------------------------------

def mem_bytes(df):
    try:
        return int(df.memory_usage(deep=True).sum())
    except Exception:
        return int(df.memory_usage().sum())


def stratified_starts(total, block, nblocks, rng, first=0):
    """Start offsets for ``nblocks`` blocks of length ``block`` in [first, total).

    One block per equal-width stratum, at a random position within it; the
    first block starts at ``first`` and the last ends at ``total``.
    """
    span = total - first
    nblocks = max(1, min(nblocks, span // max(block, 1)))
    if nblocks <= 1:
        return [first]
    width = span / nblocks
    starts = []
    for i in range(nblocks):
        lo = first + int(i * width)
        hi = first + int((i + 1) * width) - block
        if i == 0:
            s = first
        elif i == nblocks - 1:
            s = total - block
        else:
            s = rng.randint(lo, max(lo, hi))
        starts.append(max(first, min(s, total - block)))
    return sorted(set(starts))


def plan_blocks(target_units, total_units, min_block):
    """How many blocks and how big, given a target sample size."""
    nblocks = int(max(2, min(MAX_SAMPLE_BLOCKS, target_units // max(min_block, 1))))
    block = int(max(1, target_units // nblocks))
    return nblocks, min(block, total_units)


# ---------------------------------------------------------------------------
# Line-based text formats (csv, jsonl)
# ---------------------------------------------------------------------------

def _file_size(path):
    return os.path.getsize(path)


def _read_header_line(f, limit=16 * MB):
    buf = b""
    while b"\n" not in buf:
        chunk = f.read(64 * 1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise UserError("First line is longer than 16 MB; is this really a CSV file?")
    nl = buf.find(b"\n")
    return buf if nl < 0 else buf[: nl + 1]


def _sniff_sep(header, name):
    if name.lower().endswith(".tsv"):
        return "\t"
    text = header.decode("utf-8", "replace")
    counts = {sep: text.count(sep) for sep in [",", "\t", ";", "|"]}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _read_aligned(f, start, nbytes, size, body_start):
    """Read ~nbytes from start, trimmed to whole lines."""
    f.seek(start)
    data = f.read(nbytes)
    if start > body_start:
        nl = data.find(b"\n")
        data = b"" if nl < 0 else data[nl + 1:]
    if start + nbytes < size:
        nl = data.rfind(b"\n")
        data = b"" if nl < 0 else data[: nl + 1]
    elif data and not data.endswith(b"\n"):
        data += b"\n"
    return data


def _parse_text(fmt, data, header=b"", sep=",", dates=None):
    """Parse csv/jsonl from bytes or from a BytesIO (positioned at the start)."""
    if isinstance(data, bytes):
        data = io.BytesIO(header + data if fmt == "csv" else data)
    if fmt == "csv":
        return _read_csv(data, sep=sep, on_bad_lines="skip", dates=dates)
    return _read_jsonl(data)


def _read_jsonl(src):
    # in chunks: parsing all lines at once holds every record as Python objects
    chunks = list(pd.read_json(src, lines=True, chunksize=100_000))
    return pd.concat(chunks, ignore_index=True) if len(chunks) != 1 else chunks[0]


def _read_csv(src, dates=None, **kw):
    # low_memory parses in chunks: far lower peak memory (WASM heap never shrinks).
    # Date columns found in the probe are converted while parsing, so the
    # sample never holds millions of date strings at once.
    if dates:
        kw.update(parse_dates=dates, date_format="ISO8601")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pd.errors.DtypeWarning)
        warnings.simplefilter("ignore", UserWarning)
        return pd.read_csv(src, low_memory=True, **kw)


def load_text(path, name, fmt, budget, seed=0):
    size = _file_size(path)
    with open(path, "rb") as f:
        header = _read_header_line(f) if fmt == "csv" else b""
        sep = _sniff_sep(header, name) if fmt == "csv" else ","
        body_start = len(header)
        # Probe enough bytes for a few dozen rows, however wide they are.
        probe_len = int(min(max(64 * 1024, min(PROBE_BYTES, size // 50), len(header) * 64), 64 * MB))
        while True:
            probe = _read_aligned(f, body_start, probe_len, size, body_start)
            if probe.count(b"\n") >= 20 or body_start + len(probe) >= size or probe_len >= 64 * MB:
                break
            probe_len *= 4
        if not probe and fmt == "csv":
            return pd.read_csv(io.BytesIO(header), sep=sep), {"rows_total": 0, "rows_exact": True}
        probe_df = _parse_text(fmt, probe, header, sep)
        dates = _infer_datetimes(probe_df) if fmt == "csv" else None
        rows = max(len(probe_df), 1)
        bytes_per_line = len(probe) / rows
        mem_per_row = mem_bytes(probe_df) / rows
        est_rows = (size - body_start) / bytes_per_line
        if est_rows * mem_per_row <= budget:
            if body_start + len(probe) >= size:
                return probe_df, {"rows_total": len(probe_df), "rows_exact": True}
            progress(f"Reading whole file ({size / MB:,.0f} MB)…")
            if fmt == "csv":
                df = _read_csv(path, sep=sep, dates=dates)
            else:
                df = _read_jsonl(path)
            return df, {"rows_total": len(df), "rows_exact": True}

        target_rows = budget / mem_per_row
        target_bytes = int(target_rows * bytes_per_line)
        # blocks must hold several whole lines, even for very wide rows
        nblocks, block = plan_blocks(target_bytes, size - body_start, max(32 * 1024, int(bytes_per_line * 16)))
        block = max(block, int(bytes_per_line * 4))
        rng = random.Random(seed)
        starts = stratified_starts(size, block, nblocks, rng, first=body_start)
        # one buffer, written block by block, so the sampled text exists once
        buf = io.BytesIO()
        buf.write(header)
        lines = 0
        for i, s in enumerate(starts):
            progress(f"Sampling block {i + 1}/{len(starts)} at {s / size:.0%} of file…")
            chunk = _read_aligned(f, s, block, size, body_start)
            lines += chunk.count(b"\n")
            buf.write(chunk)
            del chunk
        sampled_bytes = buf.tell() - len(header)
        buf.seek(0)
        progress(f"Parsing {sampled_bytes / MB:,.0f} MB sample…")
        df = _parse_text(fmt, buf, header, sep, dates)
        del buf
        est_total = int((size - body_start) / (sampled_bytes / max(lines, 1)))
        return df, {
            "rows_total": est_total,
            "rows_exact": False,
            "sampled": True,
            "sample_method": f"{len(starts)} blocks of ~{block / MB:.1f} MB spread evenly across the file "
                             f"(includes the first and last rows)",
        }


def count_lines(path, fmt):
    size = _file_size(path)
    n = 0
    last = b"\n"
    with open(path, "rb") as f:
        done = 0
        while True:
            chunk = f.read(SCAN_CHUNK)
            if not chunk:
                break
            n += chunk.count(b"\n")
            last = chunk[-1:]
            done += len(chunk)
            progress(f"Counting rows… {done / size:.0%}")
    if last != b"\n":
        n += 1
    if fmt == "csv":
        n -= 1
    return max(n, 0)


# ---------------------------------------------------------------------------
# JSON (array of records, or other pandas-readable JSON)
# ---------------------------------------------------------------------------

def _iter_json_array(f, chunk_size=1 * MB):
    """Yield (record, bytes_consumed) from a top-level JSON array, incrementally."""
    dec = json.JSONDecoder()
    utf8 = codecs.getincrementaldecoder("utf-8")("replace")
    buf = ""
    consumed = 0
    eof = False

    def fill():
        nonlocal buf, eof, consumed
        chunk = f.read(chunk_size)
        if not chunk:
            eof = True
            buf += utf8.decode(b"", final=True)
        else:
            consumed += len(chunk)
            buf += utf8.decode(chunk)

    while not buf.lstrip() and not eof:
        fill()
    buf = buf.lstrip()
    if not buf.startswith("["):
        raise UserError("Expected a JSON array of records")
    pos = 1
    while True:
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf) or eof:
                break
            fill()
        if pos >= len(buf) or buf[pos] == "]":
            return
        try:
            obj, end = dec.raw_decode(buf, pos)
        except json.JSONDecodeError:
            if eof:
                raise
            buf = buf[pos:]
            pos = 0
            fill()
            continue
        # don't trust a number that touches the end of the buffer: it may continue
        if end >= len(buf) and not eof:
            buf = buf[pos:]
            pos = 0
            fill()
            continue
        pos = end
        yield obj, consumed - (len(buf) - pos)  # approximate (chars ≈ bytes); used for estimates only
        if pos > chunk_size:
            buf = buf[pos:]
            pos = 0


def load_json(path, budget):
    size = _file_size(path)
    with open(path, "rb") as f:
        first = f.read(64).lstrip()[:1]
    if first != b"[" or size * 3 <= budget:
        progress("Reading JSON…")
        try:
            df = pd.read_json(path)
        except ValueError:
            df = pd.read_json(path, lines=True)
        return df, {"rows_total": len(df), "rows_exact": True}

    progress("Large JSON array: reading records from the start of the file…")
    # Records are turned into DataFrames in batches so only one batch of
    # Python dicts exists at a time.
    batch, frames = [], []
    nrows = 0
    mem = 0
    pos = batch_pos = 0
    complete = True
    with open(path, "rb") as f:
        for obj, pos in _iter_json_array(f):
            batch.append(obj)
            if len(batch) == 50_000 or pos - batch_pos >= 8 * MB:
                batch_pos = pos
                frames.append(pd.DataFrame.from_records(batch))
                batch = []
                nrows += len(frames[-1])
                mem += mem_bytes(frames[-1])
                progress(f"Read {nrows:,} records ({pos / size:.0%} of file)…")
                if mem >= budget:
                    complete = False
                    break
    if batch:
        frames.append(pd.DataFrame.from_records(batch))
    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else (frames[0] if frames else pd.DataFrame())
    if complete:
        return df, {"rows_total": len(df), "rows_exact": True}
    return df, {
        "rows_total": int(len(df) * size / max(pos, 1)),
        "rows_exact": False,
        "sampled": True,
        "sample_method": "the first records of the file (a JSON array can only be read from the start; "
                         "convert to JSON lines or parquet for a spread-out sample)",
    }


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------

def _parquet_file(path):
    import pyarrow.parquet as pq
    # no pre-buffering: it would hold whole column chunks in memory, which for a
    # file with one huge row group means gigabytes
    return pq.ParquetFile(path, pre_buffer=False, buffer_size=8 * MB)


def _arrow_to_pandas(table):
    return table.to_pandas()


def _parquet_index_columns(pf):
    """Columns holding the pandas index the file was written with (if any)."""
    meta = pf.schema_arrow.pandas_metadata or {}
    return [c for c in meta.get("index_columns", []) if isinstance(c, str)]


def _parquet_columns(pf, columns):
    """The columns to read for a user's column subset (None = all): always
    including the stored index columns, in file order."""
    if columns is None:
        return None
    names = pf.schema_arrow.names
    missing = [c for c in columns if c not in set(names)]
    if missing:
        raise UserError(f"Column not in file: {missing[0]!r}" + (f" (and {len(missing) - 1} more)" if len(missing) > 1 else ""))
    if not columns:
        raise UserError("Choose at least one column")
    want = set(columns) | set(_parquet_index_columns(pf))
    return [n for n in names if n in want]


def load_parquet(path, budget, seed=0, columns=None):
    import pyarrow as pa
    pf = _parquet_file(path)
    md = pf.metadata
    nrg = md.num_row_groups
    total = md.num_rows
    cols = _parquet_columns(pf, columns)
    info = {"rows_total": total, "rows_exact": True, "row_groups": nrg}
    if nrg == 0 or total == 0:
        return _arrow_to_pandas(pf.read(columns=cols, use_pandas_metadata=True)), info

    starts = np.concatenate([[0], np.cumsum([md.row_group(i).num_rows for i in range(nrg)])])
    sizes = [md.row_group(i).total_byte_size for i in range(nrg)]
    has_pandas_index = bool(pf.schema_arrow.pandas_metadata and any(
        not isinstance(c, dict) for c in pf.schema_arrow.pandas_metadata.get("index_columns", [])))
    ncols = md.num_columns if cols is None else len(cols)
    # ~4 MB per decoded batch, so thousands of columns don't mean huge batches
    batch_rows = int(max(16, min(65536, 4 * MB // max(ncols * 8, 1))))

    def finish(df, offsets):
        if not has_pandas_index and isinstance(df.index, pd.RangeIndex) and offsets is not None:
            df.index = pd.Index(offsets, name="row")
        return df

    def head_rows(i, n):
        """First n rows of row group i, streamed so the rest of the group isn't decoded."""
        batches, got = [], 0
        for b in pf.iter_batches(batch_size=max(1, min(batch_rows, n)), row_groups=[i], columns=cols):
            batches.append(b)
            got += b.num_rows
            if got >= n:
                break
        t = pa.Table.from_batches(batches, schema=batches[0].schema if batches else None)
        t = t.replace_schema_metadata(pf.schema_arrow.metadata)
        return t.slice(0, n)

    progress(f"Reading the first rows of row group 1/{nrg}…")
    probe = _arrow_to_pandas(head_rows(0, min(batch_rows, int(starts[1]))))
    mem_per_row = mem_bytes(probe) / max(len(probe), 1)
    if total * mem_per_row <= budget:
        progress(f"Reading whole file ({nrg} row group{'s' if nrg > 1 else ''})…")
        return _arrow_to_pandas(pf.read(columns=cols, use_pandas_metadata=True)), info

    # Too big: take rows from up to 16 row groups spread across the file. A
    # group is decoded whole (to take a random slice, or the true tail for the
    # last group) only when that's cheap; otherwise its first rows are streamed.
    target_rows = max(1, int(budget / mem_per_row))
    rng = random.Random(seed)
    # Even a few rows of a row group mean reading the start of every column
    # chunk in it; for wide files that's most of the group. Keep the bytes read
    # to ~3x the budget by sampling fewer groups when they are big.
    # (only the chosen columns' chunks are read)
    chosen_idx = range(md.num_columns) if cols is None else [
        c for c in range(md.num_columns) if md.schema.column(c).path.split(".")[0] in set(cols)]
    comp = [sum(md.row_group(i).column(c).total_compressed_size for c in chosen_idx)
            for i in range(min(nrg, 3))]
    per_group_read = max(sum(comp) / len(comp), 1)
    k = int(max(2, min(nrg, 16, (3 * budget) // per_group_read)))
    if k >= nrg:
        chosen = list(range(nrg))
    else:
        chosen = sorted(set([0, nrg - 1] + [rng.randrange(int(i * nrg / k), max(int(i * nrg / k) + 1, int((i + 1) * nrg / k)))
                                           for i in range(1, k - 1)]))
    per_group = max(1, target_rows // len(chosen))
    frames, offsets = [], []
    tail_included = False
    for n, i in enumerate(chosen, start=1):
        progress(f"Reading row group {i + 1}/{nrg} ({n}/{len(chosen)} chosen)…")
        rows_i = int(starts[i + 1] - starts[i])
        take = min(rows_i, per_group)
        group_mem = rows_i * mem_per_row
        last = i == nrg - 1
        if i == 0 and not (last and group_mem <= budget):
            off, t = 0, head_rows(0, take)
        elif (last and group_mem <= budget) or group_mem * len(chosen) <= 4 * budget:
            off = rows_i - take if last else rng.randint(0, rows_i - take)
            t = pf.read_row_group(i, columns=cols, use_pandas_metadata=True).slice(off, take)
            tail_included = tail_included or last
        else:
            off, t = 0, head_rows(i, take)
        frames.append(_arrow_to_pandas(t))
        offsets.append(np.arange(starts[i] + off, starts[i] + off + take))
    df = pd.concat(frames, ignore_index=not has_pandas_index) if len(frames) > 1 else frames[0]
    where = "includes the first and last rows" if tail_included else "includes the first rows"
    if len(chosen) == 1:
        method = "the first rows of the file's only row group (a spread-out sample would mean decoding all of it)"
    else:
        method = (f"{per_group:,} contiguous rows from each of {len(chosen)} of {nrg} row groups spread across the "
                  f"file ({where}; index = row number in file unless the file stores its own)")
    info.update(sampled=True, sample_method=method)
    return finish(df, np.concatenate(offsets)), info


def parquet_file_stats(path, names=None):
    """Exact per-column null counts / min / max from parquet metadata (optionally only for ``names``)."""
    pf = _parquet_file(path)
    md = pf.metadata
    want = None if names is None else set(names)
    out = {}
    for c in range(md.num_columns):
        col = md.schema.column(c)
        if "." in col.path or (want is not None and col.path not in want):
            continue
        nulls, lo, hi, ok = 0, None, None, True
        for r in range(md.num_row_groups):
            st = md.row_group(r).column(c).statistics
            if st is None:
                ok = False
                break
            if st.has_null_count:
                nulls += st.null_count
            else:
                ok = False
            if st.has_min_max:
                try:
                    lo = st.min if lo is None else min(lo, st.min)
                    hi = st.max if hi is None else max(hi, st.max)
                except TypeError:
                    pass
        out[col.path] = {"nulls": nulls if ok else None, "min": _cell(lo), "max": _cell(hi)}
    return out


# ---------------------------------------------------------------------------
# DuckDB: row filters and SQL queries, applied while reading
# ---------------------------------------------------------------------------

DUCK_FORMATS = ("parquet", "csv", "jsonl")
DUCK_PROBE_ROWS = 2000
ROW = "__explorer_row__"


def _qi(name):
    """Quote an SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def _duck_connect(path, budget):
    import duckdb
    # Defence in depth (all of this already runs inside the sandbox): the query
    # can read only the folder holding this file, can't load extensions or
    # change these settings, and runs in one thread with bounded memory.
    folder = os.path.dirname(path).rstrip("/") + "/"
    config = {
        "threads": 1,
        "memory_limit": f"{int(min(max(4 * budget, 512 * MB), 1536 * MB) // MB)}MB",
        "preserve_insertion_order": True,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
    }
    con = duckdb.connect(":memory:", config=config)
    lit = "'" + folder.replace("'", "''") + "'"
    con.execute(f"SET allowed_directories = [{lit}]")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


def _duck_source(path, fmt, row_numbers=False):
    lit = "'" + path.replace("'", "''") + "'"
    if fmt == "parquet":
        return f"read_parquet({lit}{', file_row_number=true' if row_numbers else ''})"
    if fmt == "csv":
        sep = ", delim='\\t'" if path.lower().endswith(".tsv") else ""
        return f"read_csv({lit}, header=true{sep})"
    if fmt == "jsonl":
        return f"read_json({lit}, format='newline_delimited')"
    raise UserError(f"SQL filtering isn't available for {fmt} files")


def _duck_df(con, sql):
    df = con.execute(sql).df()
    # match the other loaders: text columns as pandas' string dtype
    for c in df.columns[df.dtypes == object]:
        head = df[c].dropna().head(200)
        if len(head) and all(isinstance(v, str) for v in head):
            with contextlib.suppress(Exception):
                df[c] = df[c].astype("str")
    return df


def _duck_error(e, what):
    msg = str(e).strip()
    # DuckDB errors start with their class ("Binder Error: ..."); keep the first lines
    msg = "\n".join(msg.splitlines()[:6])
    return UserError(f"{what} failed: {msg}")


ARROW_BLOCK = 2 * MB  # pyarrow reads ahead ~35 blocks: bigger blocks cost memory, not speed


class _FastPathFailed(Exception):
    """pyarrow couldn't stream the file (e.g. a CSV value later in the file that
    doesn't fit the type inferred from its first block)."""


class _NativeSource:
    """The file through DuckDB's own readers: handles anything DuckDB can parse,
    but in Pyodide it is several times slower than pyarrow's readers."""
    fast = False
    failed = None

    def __init__(self, con, path, fmt):
        self.con, self.path, self.fmt = con, path, fmt
        self.names = None

    def table(self, columns=None, row=False):
        """A FROM expression for the file, with the file row number as ROW (0-based) if ``row``."""
        if not row:
            return _duck_source(self.path, self.fmt)
        if self.fmt == "parquet":
            return (f"(SELECT file_row_number AS {ROW}, * EXCLUDE (file_row_number) "
                    f"FROM {_duck_source(self.path, self.fmt, True)})")
        # counted before any filter, so they are row numbers in the file
        return f"(SELECT row_number() OVER () - 1 AS {ROW}, * FROM {_duck_source(self.path, self.fmt)})"


class _ArrowSource:
    """The file streamed as Arrow record batches read by pyarrow, which DuckDB
    queries directly. In Pyodide pyarrow's parquet / CSV / JSON readers are
    several times faster than DuckDB's, and only the columns a query mentions
    are decoded. A stream can be read once, so each query gets a fresh one."""
    fast = True

    def __init__(self, con, path, fmt):
        self.con, self.path, self.fmt = con, path, fmt
        self.failed = None
        self.seq = 0
        if fmt == "parquet":
            self.pf = _parquet_file(path)
            self.schema = self.pf.schema_arrow
        elif fmt == "csv":
            import pyarrow.csv as pacsv
            with open(path, "rb") as f:
                header = _read_header_line(f)
            self.read_opts = pacsv.ReadOptions(block_size=ARROW_BLOCK, use_threads=False)
            self.parse_opts = pacsv.ParseOptions(delimiter=_sniff_sep(header, path))
            # column types come from the first block and are kept for the rest of the file
            self.schema = pacsv.open_csv(path, read_options=self.read_opts, parse_options=self.parse_opts,
                                         convert_options=pacsv.ConvertOptions(strings_can_be_null=True)).schema
        elif fmt == "jsonl":
            import pyarrow.json as pajson
            self.read_opts = pajson.ReadOptions(block_size=ARROW_BLOCK, use_threads=False)
            self.schema = pajson.open_json(path, read_options=self.read_opts).schema
        else:
            raise UserError(f"SQL filtering isn't available for {fmt} files")
        self.names = self.schema.names

    def _batches(self, cols):
        if self.fmt == "parquet":
            rows = int(max(1024, min(65536, 4 * MB // max(8 * len(cols or self.names), 1))))
            yield from self.pf.iter_batches(batch_size=rows, columns=cols)
        elif self.fmt == "csv":
            import pyarrow.csv as pacsv
            types = {n: self.schema.field(n).type for n in (cols or self.names)}
            yield from pacsv.open_csv(self.path, read_options=self.read_opts, parse_options=self.parse_opts,
                                      convert_options=pacsv.ConvertOptions(include_columns=cols or [], column_types=types,
                                                                           strings_can_be_null=True))
        else:
            import pyarrow as pa
            import pyarrow.json as pajson
            sub = pa.schema([self.schema.field(n) for n in (cols or self.names)])
            yield from pajson.open_json(self.path, read_options=self.read_opts, parse_options=pajson.ParseOptions(
                explicit_schema=sub, unexpected_field_behavior="ignore"))

    def table(self, columns=None, row=False):
        """Register a fresh stream of ``columns`` (all if None) and return its name."""
        import pyarrow as pa
        want = None if columns is None else set(columns)
        cols = None if want is None else ([n for n in self.names if n in want] or self.names[:1])
        schema = pa.schema([self.schema.field(n) for n in (cols or self.names)]
                           + ([pa.field(ROW, pa.int64())] if row else []))

        def batches():
            start = 0
            try:
                for b in self._batches(cols):
                    arrays = [b.column(n) for n in (cols or self.names)]
                    if row:
                        arrays.append(pa.array(np.arange(start, start + b.num_rows, dtype=np.int64)))
                    start += b.num_rows
                    yield pa.RecordBatch.from_arrays(arrays, schema=schema)
            except pa.ArrowInvalid as e:
                self.failed = e
                raise

        self.seq += 1
        name = f"__explorer_src{self.seq}"
        self.con.register(name, pa.RecordBatchReader.from_batches(schema, batches()))
        return name


def _referenced_columns(text, names):
    """Columns of ``names`` that SQL ``text`` mentions (identifiers are
    case-insensitive). A string literal that happens to match a name only costs
    a little extra decoding."""
    if not text or not names:
        return []
    by_lower = {}
    for n in names:
        by_lower.setdefault(str(n).lower(), n)
    toks = {t.replace('""', '"').lower() for t in re.findall(r'"((?:[^"]|"")+)"', text)}
    toks |= {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", text)}
    return [n for n in names if str(n).lower() in toks and by_lower[str(n).lower()] == n]


def _needs_all_columns(sql):
    s = re.sub(r"count\s*\(\s*\*\s*\)", "", sql, flags=re.I)
    return "*" in s or re.search(r"\bcolumns\s*\(", s, re.I) is not None


def _estimate_row_bytes(con, sql):
    """In-memory bytes per result row from the query's column types (binding only, nothing runs)."""
    total = 8
    for r in con.execute(f"DESCRIBE SELECT * FROM ({sql}) AS q").fetchall():
        t = str(r[1]).upper()
        if t.startswith(("VARCHAR", "BLOB", "JSON", "STRUCT", "MAP", "UNION")) or t.endswith("]"):
            total += 72
        elif t in ("BOOLEAN", "TINYINT", "UTINYINT"):
            total += 1
        else:
            total += 8
    return total


def load_duckdb(path, fmt, budget, columns=None, where=None, sql=None, seed=0):
    """Load the rows matching ``where`` (or the result of ``sql``, where the
    file is the table ``data``), sampled to the memory budget.

    pyarrow streams the file (only the columns needed) and DuckDB filters the
    stream, so only matching rows ever reach pandas. If they'd need more memory
    than the budget a reservoir sample is taken in the same pass (kept in file
    order). Falls back to DuckDB's own readers if pyarrow can't read the file."""
    con = _duck_connect(path, budget)
    try:
        src = None
        # a stream can be read once: a query reading ``data`` twice needs DuckDB's reader
        if not (sql and len(re.findall(r"\bdata\b", sql, re.I)) > 1):
            with contextlib.suppress(Exception):
                src = _ArrowSource(con, path, fmt)
        try:
            return _run_duckdb(con, src or _NativeSource(con, path, fmt), fmt, budget, columns, where, sql, seed)
        except _FastPathFailed:
            progress("pyarrow couldn't read the whole file; using DuckDB's reader (slower)…")
            return _run_duckdb(con, _NativeSource(con, path, fmt), fmt, budget, columns, where, sql, seed)
    finally:
        con.close()


def _run_duckdb(con, src, fmt, budget, columns, where, sql, seed):
    index_cols = []
    if fmt == "parquet":
        pf = _parquet_file(src.path)
        index_cols = _parquet_index_columns(pf)
        if columns is not None:
            columns = [c for c in _parquet_columns(pf, columns) if c not in index_cols]
    what = "SQL query" if sql else "Row filter"

    def run(q, fetch=True):
        try:
            return _duck_df(con, q) if fetch else con.execute(q).fetchall()
        except Exception as e:
            if src.failed is not None:
                raise _FastPathFailed() from e
            raise _duck_error(e, what) from e

    def reservoir(base, k):
        return run(f"SELECT * FROM ({base}) AS t USING SAMPLE reservoir({k} ROWS) REPEATABLE ({int(seed)})")

    if sql:
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            raise UserError("Enter an SQL query")
        # stream only the columns the query mentions; if that guess misses one, retry with all of them
        need = None if (not src.fast or _needs_all_columns(sql)) else _referenced_columns(sql, src.names)
        for attempt in ([need, None] if need is not None else [None]):
            def make_data():
                run(f"CREATE OR REPLACE VIEW data AS SELECT * FROM {src.table(attempt)}", fetch=False)
            try:
                make_data()
                progress("Checking the query…")
                try:
                    mem_per_row = _estimate_row_bytes(con, sql)
                except Exception as e:
                    raise _duck_error(e, what) from e
                k = max(1, int(budget // mem_per_row))
                make_data()
                progress("Running the SQL query with DuckDB…")
                # row_number() over the query's output keeps its own ORDER BY through sampling
                df = reservoir(f"SELECT row_number() OVER () AS {ROW}, * FROM ({sql}) AS q", k)
                break
            except UserError as e:
                if attempt is None or "not found" not in str(e):
                    raise
        matched, sampled = len(df), len(df) >= k
        if sampled:
            progress("Counting result rows…")
            make_data()
            matched = int(run(f"SELECT count(*) FROM ({sql}) AS q", fetch=False)[0][0])
            sampled = matched > len(df)
    else:
        if columns is not None and not columns:
            raise UserError("Choose at least one column")
        proj_cols = None if columns is None else index_cols + columns
        proj = "*" if columns is None else ", ".join(_qi(c) for c in proj_cols)
        cond = f" WHERE ({where})" if where else ""
        where_cols = _referenced_columns(where, src.names)
        need = None if proj_cols is None else list(dict.fromkeys(proj_cols + where_cols))
        progress("Reading the first rows…")
        # memory per row from the first (unfiltered) rows: instant, even when the filter matches few rows
        probe = run(f"SELECT {proj} FROM {src.table(proj_cols)} AS t LIMIT {DUCK_PROBE_ROWS}")
        mem_per_row = mem_bytes(probe) / len(probe) if len(probe) else 64
        k = max(1, int(budget // max(mem_per_row, 1)))
        progress("Filtering rows with DuckDB…" if where else "Reading the chosen columns…")
        sel = f"* EXCLUDE ({ROW})" if columns is None else proj
        df = reservoir(f"SELECT {ROW}, {sel} FROM {src.table(need, row=True)} AS t{cond}", k)
        matched, sampled = len(df), len(df) >= k
        if sampled:
            progress("Counting matching rows…")
            count_src = src.table(where_cols or None) if src.fast else src.table()
            matched = int(run(f"SELECT count(*) FROM {count_src} AS t{cond}", fetch=False)[0][0])
            sampled = matched > len(df)
    df = df.sort_values(ROW, kind="stable")

    rows = df.pop(ROW).to_numpy() if ROW in df.columns else None
    present = [c for c in index_cols if c in df.columns]
    if present:
        df = df.set_index(present)
        df.index.names = [None if str(n).startswith("__index_level_") else n for n in df.index.names]
    elif rows is not None and not sql and (where or sampled):
        df.index = pd.Index(rows, name="row")
    else:
        df = df.reset_index(drop=True)
    info = {"rows_matched": matched, "filtered": True if (where or sql) else False,
            "reader": "pyarrow" if src.fast else "duckdb"}
    if sampled:
        info.update(sampled=True, sample_method=(
            f"a random sample of the {matched:,} {'result' if sql else 'matching'} rows "
            f"(DuckDB reservoir sample, kept in {'query' if sql else 'file'} order)"))
    return df, info


# ---------------------------------------------------------------------------
# HDF5 (via h5py; understands pandas fixed/table layouts without PyTables)
# ---------------------------------------------------------------------------

class _SafeUnpickler(pickle.Unpickler):
    """Unpickler for PyTables/pandas attributes.

    Only allows the numpy array reconstructors used by pandas' fixed-format
    object blocks; anything else (e.g. os.system) is refused.
    """
    ALLOWED = {
        ("numpy.core.multiarray", "_reconstruct"),
        ("numpy._core.multiarray", "_reconstruct"),
        ("numpy", "ndarray"),
        ("numpy", "dtype"),
    }

    def find_class(self, module, name):
        if (module, name) in self.ALLOWED:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"refusing to load {module}.{name} from file")


def safe_unpickle(data):
    if isinstance(data, np.ndarray):
        data = data.tobytes()
    if isinstance(data, np.bytes_):
        data = bytes(data)
    return _SafeUnpickler(io.BytesIO(data), encoding="bytes").load()


def _attr_str(attrs, key, default=None):
    v = attrs.get(key, default)
    if isinstance(v, (bytes, np.bytes_)):
        return bytes(v).decode("utf-8", "replace")
    return v


def _attr_list(attrs, key):
    v = attrs.get(key)
    if v is None:
        return None
    try:
        out = safe_unpickle(v)
    except Exception:
        return None
    return [x.decode("utf-8", "replace") if isinstance(x, bytes) else x for x in out]


def _decode_bytes_array(arr):
    if arr.dtype.kind == "S":
        return pd.array(np.char.decode(arr, "utf-8", "replace"), dtype=object)
    if arr.dtype.kind == "O":
        return pd.array([x.decode("utf-8", "replace") if isinstance(x, bytes) else x for x in arr], dtype=object)
    return arr


def _guess_dt_unit(values):
    v = np.abs(values[: 1000][values[:1000] != np.iinfo(np.int64).min]) if len(values) else values
    m = float(np.median(v)) if len(v) else 0
    if m > 1e17:
        return "ns"
    if m > 1e14:
        return "us"
    if m > 1e11:
        return "ms"
    return "s"


def _to_datetime(values, dtype_name=None):
    values = np.asarray(values).astype("int64")
    unit = None
    if dtype_name and dtype_name.startswith("datetime64[") and dtype_name.endswith("]"):
        unit = dtype_name[len("datetime64["):-1]
    unit = unit or _guess_dt_unit(values)
    return values.view(f"datetime64[{unit}]")


def _decode_index(ds, sl):
    arr = ds[sl]
    kind = _attr_str(ds.attrs, "kind", "")
    if kind and kind.startswith("datetime"):
        return pd.Index(_to_datetime(arr, _attr_str(ds.attrs, "value_type")))
    if arr.dtype.kind in "SO":
        return pd.Index(_decode_bytes_array(arr))
    return pd.Index(arr)


class H5Reader:
    """Row-sliceable reader for one key of an HDF5 file."""

    def __init__(self, f, key):
        self.f = f
        self.key = key
        node = f[key]
        self.node = node
        self.kind = self._kind(node)
        if self.kind == "pandas_frame":
            self.nrows = node["axis1"].shape[0]
            self._pickled = {}
        elif self.kind == "pandas_series":
            self.nrows = node["values"].shape[0]
        elif self.kind == "pandas_table":
            self.nrows = node["table"].shape[0]
        else:
            self.nrows = node.shape[0] if node.ndim else 1

    @staticmethod
    def _kind(node):
        import h5py
        if isinstance(node, h5py.Group):
            pt = _attr_str(node.attrs, "pandas_type", "")
            if pt == "frame":
                return "pandas_frame"
            if pt == "series":
                return "pandas_series"
            if pt in ("frame_table", "series_table", "appendable_frame") and "table" in node:
                return "pandas_table"
            raise UserError(f"Unsupported HDF5 group layout at {node.name} (pandas_type={pt!r})")
        return "dataset"

    def read(self, start, stop):
        sl = slice(start, stop)
        return getattr(self, "_read_" + self.kind)(sl)

    def row_bytes(self):
        """Approximate stored bytes per row (sizes the first read for wide data)."""
        n = self.node
        if self.kind == "dataset":
            return n.dtype.itemsize * int(np.prod(n.shape[1:])) if n.ndim else 8
        if self.kind == "pandas_table":
            return n["table"].dtype.itemsize
        if self.kind == "pandas_series":
            return max(n["values"].dtype.itemsize, 8)
        total = 8
        for i in range(int(n.attrs.get("nblocks", 0))):
            ds = n[f"block{i}_values"]
            k = n[f"block{i}_items"].shape[0]
            total += k * (64 if ds.dtype.kind == "O" or _attr_str(ds.attrs, "PSEUDOATOM") == "object" else ds.dtype.itemsize)
        return total

    def _read_dataset(self, sl):
        ds = self.node
        if ds.ndim == 0:
            return pd.DataFrame({ds.name.rsplit("/", 1)[-1]: [ds[()]]})
        arr = ds[sl]
        idx = pd.RangeIndex(sl.start, sl.start + len(arr), name="row")
        if arr.dtype.names:
            cols = {n: _decode_bytes_array(arr[n]) if arr[n].ndim == 1 else list(arr[n]) for n in arr.dtype.names}
            return pd.DataFrame(cols, index=idx)
        if arr.ndim == 1:
            return pd.DataFrame({ds.name.rsplit("/", 1)[-1]: _decode_bytes_array(arr)}, index=idx)
        if arr.ndim > 2:
            arr = arr.reshape(arr.shape[0], -1)
        if arr.dtype.kind in "SO":
            return pd.DataFrame({i: _decode_bytes_array(arr[:, i]) for i in range(arr.shape[1])}, index=idx)
        return pd.DataFrame(arr, index=idx)

    def _block(self, i, sl):
        g = self.node
        ds = g[f"block{i}_values"]
        if _attr_str(ds.attrs, "PSEUDOATOM") == "object" or ds.dtype.kind == "O":
            if i not in self._pickled:
                nbytes = sum(np.asarray(x).nbytes for x in ds[()])
                if nbytes > 512 * MB:
                    raise UserError(f"Column block {i} is a {nbytes / MB:,.0f} MB pickled object array; "
                                    f"too large to load in the browser")
                arr = safe_unpickle(ds[0])
                n = self.nrows
                if arr.ndim == 2 and arr.shape[1] == n and arr.shape[0] != n:
                    arr = arr.T
                self._pickled[i] = arr.reshape(n, -1)
            return self._pickled[i][sl]
        vals = ds[sl]
        vt = _attr_str(ds.attrs, "value_type")
        if vt and vt.startswith("datetime64"):
            vals = _to_datetime(vals, vt)
        if vals.ndim == 1:
            vals = vals.reshape(-1, 1)
        return vals

    def _read_pandas_frame(self, sl):
        g = self.node
        index = _decode_index(g["axis1"], sl)
        columns = list(_decode_index(g["axis0"], slice(None)))
        data = {}
        for i in range(int(g.attrs.get("nblocks", 0))):
            items = list(_decode_index(g[f"block{i}_items"], slice(None)))
            vals = self._block(i, sl)
            for j, name in enumerate(items):
                col = vals[:, j]
                data[name] = _decode_bytes_array(col) if col.dtype.kind == "S" else col
        return pd.DataFrame({c: data[c] for c in columns if c in data}, index=index)

    def _read_pandas_series(self, sl):
        g = self.node
        index = _decode_index(g["index"], sl)
        vals = g["values"][sl]
        vt = _attr_str(g["values"].attrs, "value_type")
        if vt and vt.startswith("datetime64"):
            vals = _to_datetime(vals, vt)
        name = _attr_str(g.attrs, "name") or "values"
        if name == "N.":
            name = "values"
        return pd.DataFrame({name: _decode_bytes_array(vals)}, index=index)

    def _read_pandas_table(self, sl):
        ds = self.node["table"]
        arr = ds[sl]
        attrs = ds.attrs
        data = {}
        index = None
        for field in arr.dtype.names:
            col = arr[field]
            if field == "index":
                ik = _attr_str(attrs, "index_kind", "")
                if ik.startswith("datetime"):
                    index = pd.Index(_to_datetime(col))
                else:
                    index = pd.Index(_decode_bytes_array(col))
                continue
            names = _attr_list(attrs, f"{field}_kind") or [field]
            dtype_name = _attr_str(attrs, f"{field}_dtype", "") or ""
            nan_rep = _attr_str(attrs, f"{field}_nan_rep")
            if col.ndim == 1:
                col = col.reshape(-1, 1)
            for j, name in enumerate(names):
                v = col[:, j]
                if dtype_name.startswith("datetime64"):
                    v = _to_datetime(v, dtype_name)
                elif dtype_name == "bool":
                    v = v.astype(bool)
                elif v.dtype.kind == "S":
                    v = _decode_bytes_array(v)
                    if nan_rep is not None:
                        v = pd.array([None if x == nan_rep else x for x in v], dtype=object)
                data[name] = v
        if index is None:
            index = pd.RangeIndex(sl.start, sl.start + len(arr), name="row")
        order = list(data)
        try:
            axes = safe_unpickle(self.node.attrs["non_index_axes"])
            names = [n.decode() if isinstance(n, bytes) else n for n in axes[0][1]]
            if sorted(map(str, names)) == sorted(map(str, order)):
                order = names
        except Exception:
            pass
        return pd.DataFrame({k: data[k] for k in order}, index=index)


def h5_keys(path):
    import h5py
    keys = []
    with h5py.File(path, "r") as f:
        def walk(group):
            for name, node in group.items():
                if isinstance(node, h5py.Group):
                    if "pandas_type" in node.attrs:
                        try:
                            r = H5Reader(f, node.name)
                            keys.append({"key": node.name, "kind": r.kind.replace("_", " "), "rows": r.nrows})
                        except UserError:
                            pass
                    elif name != "_i_table":
                        walk(node)
                elif isinstance(node, h5py.Dataset):
                    shape = "×".join(str(s) for s in node.shape) or "scalar"
                    keys.append({"key": node.name, "kind": f"dataset {shape} {node.dtype}",
                                 "rows": node.shape[0] if node.ndim else 1})
        walk(f)
    return keys


def load_h5(path, key, budget, seed=0):
    import h5py
    with h5py.File(path, "r") as f:
        if not key:
            keys = h5_keys(path)
            if not keys:
                raise UserError("No datasets found in this HDF5 file")
            key = keys[0]["key"]
        if key not in f:
            raise UserError(f"Key {key!r} not found in file")
        r = H5Reader(f, key)
        return _sample_rows(r.read, r.nrows, budget, seed, r.row_bytes())


def _sample_rows(read, nrows, budget, seed, row_bytes=None):
    """Generic row-range sampler for formats with cheap random row access."""
    info = {"rows_total": nrows, "rows_exact": True}
    # ~8 MB first read: 10,000 rows of a 5,000-column float table would be 400 MB
    probe_n = int(min(nrows, 10000, max(20, 8 * MB // max(row_bytes or 1, 1))))
    progress("Reading first rows…")
    probe = read(0, probe_n)
    if probe_n >= nrows:
        return probe, info
    mem_per_row = mem_bytes(probe) / max(probe_n, 1)
    if nrows * mem_per_row <= budget:
        progress(f"Reading all {nrows:,} rows…")
        parts = [probe]
        step = max(probe_n, int(64 * MB / max(mem_per_row, 1)))
        for s in range(probe_n, nrows, step):
            parts.append(read(s, min(nrows, s + step)))
            progress(f"Reading rows… {min(nrows, s + step) / nrows:.0%}")
        return pd.concat(parts), info
    target = max(1, int(budget / mem_per_row))
    nblocks, block = plan_blocks(target, nrows, max(1, min(1000, target // MAX_SAMPLE_BLOCKS)))
    starts = stratified_starts(nrows, block, nblocks, random.Random(seed))
    parts = []
    for i, s in enumerate(starts):
        progress(f"Sampling block {i + 1}/{len(starts)} at row {s:,}…")
        parts.append(read(s, min(nrows, s + block)))
    info.update(sampled=True, sample_method=f"{len(starts)} blocks of {block:,} rows spread evenly across the "
                f"data (includes the first and last rows; index = row number in file)")
    return pd.concat(parts), info


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def install_wheels(wheels):
    """wheels: list of (filename, bytes). Verified against pinned hashes."""
    if _have_openpyxl():
        return
    os.makedirs(WHEEL_DIR, exist_ok=True)
    for name, data in wheels:
        data = bytes(data)
        want = WHEEL_SHA256.get(name)
        if want is None:
            raise UserError(f"Unexpected wheel {name}")
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            raise UserError(f"Wheel {name} failed its integrity check")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(WHEEL_DIR)
    if WHEEL_DIR not in sys.path:
        sys.path.insert(0, WHEEL_DIR)


def _have_openpyxl():
    try:
        import openpyxl  # noqa: F401
        return True
    except ImportError:
        return False


def xlsx_sheets(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        out = []
        for ws in wb.worksheets:
            out.append({"key": ws.title, "kind": "sheet", "rows": ws.max_row})
        return out
    finally:
        wb.close()


def load_xlsx(path, key, budget):
    sheets = xlsx_sheets(path)
    if not sheets:
        raise UserError("Workbook has no sheets")
    sheet = key or sheets[0]["key"]
    match = [s for s in sheets if s["key"] == sheet]
    if not match:
        raise UserError(f"Sheet {sheet!r} not found")
    total = match[0]["rows"]
    total = (total - 1) if total else None
    progress("Reading sheet…")
    width = pd.read_excel(path, sheet_name=sheet, engine="openpyxl", nrows=5).shape[1]
    probe_n = int(max(20, min(2000, 200_000 // max(width, 1))))
    probe = pd.read_excel(path, sheet_name=sheet, engine="openpyxl", nrows=probe_n)
    if len(probe) < probe_n:
        return probe, {"rows_total": len(probe), "rows_exact": True}
    mem_per_row = mem_bytes(probe) / len(probe)
    limit = int(budget / mem_per_row)
    if total is not None and total <= limit:
        df = pd.read_excel(path, sheet_name=sheet, engine="openpyxl")
        return df, {"rows_total": len(df), "rows_exact": True}
    df = pd.read_excel(path, sheet_name=sheet, engine="openpyxl", nrows=limit)
    if len(df) < limit:
        return df, {"rows_total": len(df), "rows_exact": True}
    return df, {"rows_total": total, "rows_exact": False, "sampled": True,
                "sample_method": "the first rows of the sheet"}


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")


def _infer_datetimes(df):
    """Convert text columns whose values all look like ISO dates (in place).

    Returns the labels of the converted columns."""
    converted = []
    for i in range(df.shape[1]):
        s = df.iloc[:, i]
        if not (pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)):
            continue
        sample = s.dropna().head(200)
        if not len(sample) or not all(isinstance(v, str) and _ISO_DATE.match(v) for v in sample):
            continue
        try:
            conv = pd.to_datetime(s, format="ISO8601", errors="coerce")
        except (ValueError, TypeError):
            continue
        if conv.isna().sum() == s.isna().sum():
            df.isetitem(i, conv)
            converted.append(df.columns[i])
    return converted


_SAVED_INDEX_NAMES = {"unnamed: 0", "", "index"}


def _is_dates(values):
    return pd.api.types.is_datetime64_any_dtype(values.dtype)


def _looks_like_index(df, fmt):
    first = df.columns[0]
    col = df.iloc[:, 0]
    if isinstance(first, str) and first.strip().lower() in _SAVED_INDEX_NAMES and fmt in ("csv", "xlsx"):
        return True
    if _is_dates(col):
        return True
    if (pd.api.types.is_object_dtype(col) or pd.api.types.is_string_dtype(col)) and df.shape[1] > 2:
        rest = df.dtypes.iloc[1:]
        return bool(all(_is_numeric_dtype(t) for t in rest) and col.notna().all() and col.is_unique)
    return False


def choose_index(df, mode, fmt, sampled):
    """Pick the DataFrame index.

    mode "auto": keep an index the file stores (parquet/h5); otherwise use the
    first column if it is a saved pandas index ("Unnamed: 0"), holds dates, or
    is a unique text label in front of all-numeric columns (a labelled matrix).
    "first": always use the first column. "none": row numbers.
    """
    if df.shape[1] == 0:
        return df
    stored = not isinstance(df.index, pd.RangeIndex) and df.index.name != "row"
    first = df.columns[0]
    if mode == "first":
        df = df.set_index(first)
    elif mode == "none":
        if stored:
            df = df.reset_index()
    elif not stored and df.shape[1] > 1 and _looks_like_index(df, fmt):
        df = df.set_index(first)
    if isinstance(df.index.name, str) and df.index.name.startswith("Unnamed: "):
        df.index.name = None
    if not _is_dates(df.index) and (pd.api.types.is_object_dtype(df.index) or pd.api.types.is_string_dtype(df.index)):
        sample = df.index[:200]
        if len(sample) and all(isinstance(v, str) and _ISO_DATE.match(v) for v in sample):
            conv = pd.to_datetime(df.index, format="ISO8601", errors="coerce")
            if conv.isna().sum() == 0:
                df.index = conv.rename(df.index.name)
    if sampled and isinstance(df.index, pd.RangeIndex) and fmt in ("csv", "jsonl", "json", "xlsx"):
        df.index = df.index.rename("sample_row")
    return df


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.path = None
        self.name = None
        self.format = None
        self.df0 = None
        self.df = None
        self.info = {}
        self.ns = {}
        self.steps = []
        self.file_stats = None
        self.colstats_for = None
        self.colstats_rows = None


STATE = State()


def _require_df():
    if STATE.df is None:
        raise UserError("Load a file first")
    return STATE.df


SCHEMA_PROBE_ROWS = 200


def file_schema(path, name, fmt):
    """Column names and types, read cheaply (parquet footer, or the first rows
    of a text file), for choosing columns and writing filters before loading."""
    if fmt == "parquet":
        pf = _parquet_file(path)
        idx = set(_parquet_index_columns(pf))
        cols = [{"name": f.name, "dtype": str(f.type), "index": f.name in idx} for f in pf.schema_arrow]
        rows, exact = pf.metadata.num_rows, True
    elif fmt in ("csv", "jsonl"):
        with open(path, "rb") as f:
            head = f.read(PROBE_BYTES)
        if len(head) == PROBE_BYTES:
            head = head[: head.rfind(b"\n") + 1]
        sep = _sniff_sep(head[: head.find(b"\n") + 1] or head, name) if fmt == "csv" else ","
        try:
            df = _parse_text(fmt, head, sep=sep)
        except Exception as e:
            raise UserError(f"Could not read this file as {fmt}: {type(e).__name__}: {e}") from e
        df = df.head(SCHEMA_PROBE_ROWS)
        _infer_datetimes(df)
        # the first column, if it would become the index under "Auto" (dates, a saved index…), is always loaded
        first_is_index = df.shape[1] > 1 and _looks_like_index(df, fmt)
        cols = [{"name": _name(c), "dtype": str(t), "index": bool(first_is_index and i == 0)}
                for i, (c, t) in enumerate(zip(df.columns, df.dtypes))]
        rows, exact = None, False
    else:
        return None
    # (index columns aren't counted: they're always loaded, as the index)
    return {"columns": cols[:MAX_VIEW_COLS], "total_cols": sum(not c["index"] for c in cols), "rows": rows,
            "rows_exact": exact}


def inspect(path, name):
    fmt = detect_format(path, name)
    out = {"format": fmt, "size": _file_size(path), "keys": None}
    if fmt in DUCK_FORMATS:
        out["schema"] = file_schema(path, name, fmt)
    if fmt == "h5":
        out["keys"] = h5_keys(path)
    elif fmt == "xlsx":
        if not _have_openpyxl():
            out["needs_wheels"] = True
            return out
        out["keys"] = xlsx_sheets(path)
    return out


def load(path, name, key=None, budget_mb=DEFAULT_BUDGET_MB, seed=0, index="auto", columns=None, where=None, sql=None):
    """Load a file (sampled to the budget). Optionally only some ``columns``,
    only rows matching an SQL ``where`` clause, or the result of an ``sql``
    query over the file (as table ``data``); the last two use DuckDB."""
    budget = max(1, float(budget_mb)) * MB
    fmt = detect_format(path, name)
    where = (where or "").strip() or None
    sql = (sql or "").strip() or None
    if columns is not None:
        columns = [str(c) for c in columns]
    t0 = time.time()
    progress(f"Opening {fmt} file…")
    if (columns is not None or where or sql) and fmt not in DUCK_FORMATS:
        raise UserError("Choosing columns and filtering rows work for parquet, CSV and JSON lines files")
    total_cols, added = None, 0
    if columns is not None or where or sql:
        schema = None
        with contextlib.suppress(Exception):
            schema = file_schema(path, name, fmt)
        if schema:
            total_cols = schema["total_cols"]
            if columns is not None and fmt != "parquet" and index == "auto":
                # keep the text file's index column (parquet's stored index is handled by its loader)
                idx = [c["name"] for c in schema["columns"] if c["index"] and c["name"] not in columns]
                columns, added = idx + columns, len(idx)
    try:
        if where or sql or (columns is not None and fmt != "parquet"):
            df, info = load_duckdb(path, fmt, budget, columns, where, sql, seed)
            if fmt == "parquet":
                info.update(rows_total=_parquet_file(path).metadata.num_rows, rows_exact=True)
            else:
                info.update(rows_total=None, rows_exact=False)
            info["engine"] = "duckdb"
        elif fmt == "parquet":
            df, info = load_parquet(path, budget, seed, columns)
        elif fmt in ("csv", "jsonl"):
            df, info = load_text(path, name, fmt, budget, seed)
        elif fmt == "json":
            df, info = load_json(path, budget)
        elif fmt == "parquet":
            df, info = load_parquet(path, budget, seed)
        elif fmt == "h5":
            df, info = load_h5(path, key, budget, seed)
        elif fmt == "xlsx":
            if not _have_openpyxl():
                raise UserError("Excel support is not installed yet")
            df, info = load_xlsx(path, key, budget)
        else:
            raise UserError(f"Unsupported format {fmt}")
    except (UserError, MemoryError):
        raise
    except Exception as e:
        raise UserError(f"Could not read this file as {fmt}: {type(e).__name__}: {e}") from e

    if fmt in ("csv", "jsonl", "json"):
        _infer_datetimes(df)
    if index not in ("auto", "first", "none"):
        raise UserError("Unknown index option")
    df = choose_index(df, index, fmt, info.get("sampled", False))
    STATE.path, STATE.name, STATE.format = path, name, fmt
    STATE.df0 = STATE.df = df
    STATE.ns = {}
    STATE.steps = []
    STATE.file_stats = None
    STATE.colstats_for = STATE.colstats_rows = None
    info = dict(info)
    info.setdefault("sampled", False)
    info.setdefault("filtered", False)
    info["selection"] = None if (columns is None and not where and not sql) else {
        "columns": None if (columns is None or sql) else len(columns) - added,
        "of": total_cols, "where": where, "sql": sql}
    info.update(
        name=name, format=fmt, key=key, size=_file_size(path),
        rows_loaded=len(df), cols=df.shape[1], load_seconds=round(time.time() - t0, 2),
        budget_mb=budget_mb, index_mode=index,
    )
    STATE.info = info
    return {"info": info, "view": view_info()}


def view_info():
    df = STATE.df
    ncols = min(df.shape[1], MAX_VIEW_COLS)
    dtypes = df.dtypes.iloc[:ncols]
    return {
        "rows": int(df.shape[0]),
        "cols": int(df.shape[1]),
        "is_original": STATE.df is STATE.df0,
        "columns": [{"name": n, "dtype": str(t), "numeric": _is_numeric_dtype(t)}
                    for n, t in zip(_fmt_values(df.columns[:ncols], missing="nan"), dtypes)],
        "columns_truncated": df.shape[1] > ncols,
        "index_name": _name(df.index.name) if df.index.name is not None else "index",
        "index_dtype": str(df.index.dtype),
        "steps": STATE.steps,
    }


def scan_rows():
    if STATE.format not in ("csv", "jsonl"):
        raise UserError("Exact row counting is only needed for CSV / JSON lines files")
    n = count_lines(STATE.path, STATE.format)
    STATE.info.update(rows_total=n, rows_exact=True)
    return {"info": STATE.info}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _name(v):
    """A column/index name for the page (rendered as text, so no escaping needed)."""
    return v if isinstance(v, str) else str(v)


def _label(v):
    """A display string that is safe to hand to Plotly (no pseudo-HTML)."""
    s = v if isinstance(v, str) else str(v)
    return s.replace("<", "‹").replace(">", "›")


def _cell(v):
    if v is None:
        return None
    if isinstance(v, float) or isinstance(v, np.floating):
        if math.isnan(v):
            return None
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return f"{float(v):.10g}"
    if isinstance(v, (bool, np.bool_)):
        return "True" if v else "False"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    try:
        if v is pd.NaT or (not isinstance(v, (list, dict, tuple, np.ndarray)) and pd.isna(v)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, bytes):
        s = repr(v)
    elif isinstance(v, str):
        s = v
    else:
        s = str(v)
    if len(s) > MAX_CELL_CHARS:
        s = s[:MAX_CELL_CHARS] + "…"
    return s


def _is_numeric(s):
    return bool(pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s))


def _is_numeric_dtype(t):
    return bool(pd.api.types.is_numeric_dtype(t) and not pd.api.types.is_bool_dtype(t))


def _fmt_values(values, missing=None):
    """Display strings for a column, index or column labels.

    Datetimes with no time part show as plain dates (2024-01-31)."""
    if _is_dates(values):
        idx = pd.DatetimeIndex(values)
        na = idx.isna()
        valid = idx[~na]
        if len(valid) and (valid == valid.normalize()).all():
            strs = idx.strftime("%Y-%m-%d")
        else:
            strs = idx.astype(str)
        return [missing if m else s_ for m, s_ in zip(na, strs)]
    out = [_cell(v) for v in values]
    return out if missing is None else [missing if v is None else v for v in out]


def _clean(obj):
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_clean(v) for v in obj.tolist()]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    return str(obj)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _sorted_positions(df, sort, ascending):
    if sort == -1:
        s = pd.Series(df.index)
    else:
        s = df.iloc[:, sort].reset_index(drop=True)
    try:
        return s.sort_values(ascending=ascending, na_position="last", kind="stable").index.to_numpy()
    except TypeError:
        return s.astype(str).sort_values(ascending=ascending, kind="stable").index.to_numpy()


def _column_positions(df, col_filter):
    if not col_filter:
        return np.arange(df.shape[1])
    q = str(col_filter).lower()
    return np.array([i for i, c in enumerate(_fmt_values(df.columns, missing="nan")) if q in c.lower()], dtype=int)


def table(mode="head", n=50, sort=None, ascending=True, seed=0, col_start=0, col_count=DEFAULT_TABLE_COLS,
          col_filter=""):
    """One page of the current view: n rows × a window of columns.

    Only the requested cells are copied, so a 5,000-column frame costs the
    same as a narrow one."""
    df = _require_df()
    nrows = len(df)
    n = int(max(1, min(int(n), MAX_TABLE_ROWS)))
    col_count = int(max(1, min(int(col_count), MAX_TABLE_COLS)))
    positions = _column_positions(df, col_filter)
    col_start = int(max(0, min(int(col_start), max(len(positions) - 1, 0))))
    page = positions[col_start:col_start + col_count]
    order = None
    if sort is not None:
        sort = int(sort)
        if not (-1 <= sort < df.shape[1]):
            raise UserError("Bad sort column")
        order = _sorted_positions(df, sort, bool(ascending))
    k = min(n, nrows)
    if mode == "tail":
        rows = order[nrows - k:] if order is not None else np.arange(nrows - k, nrows)
    elif mode == "sample":
        pick = np.sort(np.random.default_rng(int(seed)).choice(nrows, size=k, replace=False)) if k else np.arange(0)
        rows = order[pick] if order is not None else pick
    else:
        rows = order[:k] if order is not None else np.arange(k)
    sub = df.iloc[rows, page]
    cols = [_fmt_values(sub.iloc[:, j]) for j in range(sub.shape[1])]
    out_rows = [list(r) for r in zip(*cols)] if cols else [[] for _ in range(len(sub))]
    return {
        "columns": _fmt_values(sub.columns, missing="nan"),
        "col_positions": [int(p) for p in page],
        "dtypes": [str(t) for t in sub.dtypes],
        "index_name": _name(df.index.name) if df.index.name is not None else "",
        "index": _fmt_values(sub.index),
        "rows": out_rows,
        "total_rows": int(nrows),
        "total_cols": int(df.shape[1]),
        "matched_cols": int(len(positions)),
        "col_start": col_start,
        "col_count": col_count,
    }


def _col_stats(s):
    n = len(s)
    nulls = int(s.isna().sum())
    out = {"dtype": str(s.dtype), "count": n - nulls, "nulls": nulls,
           "null_frac": (nulls / n) if n else None}
    try:
        out["unique"] = int(s.nunique(dropna=True))
    except TypeError:
        out["unique"] = int(s.astype(str).nunique(dropna=True))
    valid = s.dropna()
    if not len(valid):
        return out
    if pd.api.types.is_bool_dtype(s):
        out["mean"] = _cell(float(valid.astype(float).mean()))
        out["min"], out["max"] = _cell(bool(valid.min())), _cell(bool(valid.max()))
        return out
    if _is_numeric(s) or pd.api.types.is_datetime64_any_dtype(s) or pd.api.types.is_timedelta64_dtype(s):
        out["min"], out["max"] = _cell(valid.min()), _cell(valid.max())
        out["idxmin"], out["idxmax"] = _fmt_values(valid.index[[valid.argmin(), valid.argmax()]])
        try:
            out["mean"] = _cell(valid.mean())
            out["std"] = _cell(valid.std()) if len(valid) > 1 else None
        except TypeError:
            pass
        return out
    try:
        out["min"], out["max"] = _cell(valid.min()), _cell(valid.max())
    except TypeError:
        out["min"] = out["max"] = "(mixed types)"
    return out


COLSTAT_SORT_KEYS = ("pos", "name", "dtype", "count", "nulls", "null_frac", "unique", "min", "max", "mean", "std",
                     "idxmin", "idxmax")


def _compute_colstats(df):
    """Stats for every column. Numeric columns are done in vectorised chunks,
    so thousands of columns take seconds, not minutes."""
    n = len(df)
    m = df.shape[1]
    names = _fmt_values(df.columns, missing="nan")
    dtypes = list(df.dtypes)
    nulls = df.isna().sum().to_numpy()
    rows = [{"pos": i, "name": names[i], "dtype": str(dtypes[i]), "count": int(n - nulls[i]), "nulls": int(nulls[i]),
             "null_frac": (float(nulls[i]) / n) if n else None, "_raw": {}} for i in range(m)]
    numeric = [i for i, t in enumerate(dtypes) if _is_numeric_dtype(t)]
    other = [i for i, t in enumerate(dtypes) if not _is_numeric_dtype(t)]
    # chunks never mix dtypes, so int64 min/max aren't upcast to float
    by_dtype = {}
    for i in numeric:
        by_dtype.setdefault(str(dtypes[i]), []).append(i)
    step = 256
    chunks = [cols[c0:c0 + step] for cols in by_dtype.values() for c0 in range(0, len(cols), step)]
    done = 0
    for pos in chunks:
        if m > step:
            progress(f"Column stats… {done:,}/{m:,} columns")
        done += len(pos)
        sub = df.iloc[:, pos]
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore", RuntimeWarning)
            mins, maxs = sub.min().to_numpy(object), sub.max().to_numpy(object)
            means, stds = sub.mean().to_numpy(object), sub.std().to_numpy(object)
            arr = sub.to_numpy(dtype="float64", na_value=np.nan)
            have = ~np.isnan(arr).all(axis=0) if len(arr) else np.zeros(len(pos), dtype=bool)
            # distinct values per column: sort each column, count changes (NaNs sort last)
            if len(arr):
                srt = arr.T.copy(order="C")  # one row per column: contiguous sorts are much faster
                srt.sort(axis=1)
                uniq = ((np.diff(srt, axis=1) != 0) & ~np.isnan(srt[:, 1:])).sum(axis=1) + have
                del srt
            else:
                uniq = np.zeros(len(pos), dtype=int)
            filled = np.where(np.isnan(arr), np.inf, arr)
            amin = filled.argmin(axis=0) if len(arr) else np.zeros(len(pos), dtype=int)
            filled = np.where(np.isnan(arr), -np.inf, arr)
            amax = filled.argmax(axis=0) if len(arr) else np.zeros(len(pos), dtype=int)
            del arr, filled
        idx_min = _fmt_values(df.index[amin]) if n else []
        idx_max = _fmt_values(df.index[amax]) if n else []
        for j, i in enumerate(pos):
            r = rows[i]
            r["unique"] = None if uniq[j] is None else int(uniq[j])
            if not have[j]:
                continue
            mn, mx, me, sd = mins[j], maxs[j], means[j], stds[j]
            r.update(min=_cell(mn), max=_cell(mx), mean=_cell(me), std=_cell(sd),
                     idxmin=idx_min[j], idxmax=idx_max[j])
            r["_raw"] = {k: (float(v) if v is not None and not pd.isna(v) else None)
                         for k, v in (("min", mn), ("max", mx), ("mean", me), ("std", sd))}
    for c, i in enumerate(other):
        if c % 200 == 0 and len(other) > 400:
            progress(f"Column stats… {len(numeric) + c:,}/{m:,} columns")
        st = _col_stats(df.iloc[:, i])
        rows[i].update({k: v for k, v in st.items() if k not in ("dtype", "count", "nulls", "null_frac")})
    return rows


def _stat_sort_key(key):
    if key in ("count", "nulls", "null_frac", "unique", "pos"):
        return lambda r: r.get(key)
    if key in ("min", "max", "mean", "std"):
        return lambda r: r["_raw"].get(key)
    return lambda r: (r.get(key) or "").lower() if isinstance(r.get(key), str) else r.get(key)


def colstats(start=0, limit=200, col_filter="", sort=None, ascending=True):
    """A page of per-column stats (computed once per view, then filtered/sorted/paged)."""
    df = _require_df()
    if STATE.colstats_for is not df:
        STATE.colstats_rows = _compute_colstats(df)
        STATE.colstats_for = df
    rows = STATE.colstats_rows
    if col_filter:
        q = str(col_filter).lower()
        rows = [r for r in rows if q in r["name"].lower()]
    if sort is not None:
        if sort not in COLSTAT_SORT_KEYS:
            raise UserError("Unknown sort key")
        key = _stat_sort_key(sort)
        have = [r for r in rows if key(r) is not None]
        missing = [r for r in rows if key(r) is None]
        try:
            have.sort(key=key, reverse=not ascending)
        except TypeError:
            have.sort(key=lambda r: str(key(r)), reverse=not ascending)
        rows = have + missing
    limit = int(max(1, min(int(limit), MAX_COLSTATS_PAGE)))
    start = int(max(0, min(int(start), max(len(rows) - 1, 0))))
    page = [{k: v for k, v in r.items() if k != "_raw"} for r in rows[start:start + limit]]

    exact = (STATE.format == "parquet" and STATE.df is STATE.df0 and STATE.info.get("sampled")
             and not STATE.info.get("filtered"))
    if exact:
        if STATE.file_stats is None:
            STATE.file_stats = {}
        need = [r["name"] for r in page if r["name"] not in STATE.file_stats]
        if need:
            try:
                got = parquet_file_stats(STATE.path, need)
            except Exception:
                got = {}
            for name_ in need:
                STATE.file_stats[name_] = got.get(name_)
        for r in page:
            if STATE.file_stats.get(r["name"]):
                r["file"] = STATE.file_stats[r["name"]]
    return {"columns": page, "rows": int(df.shape[0]), "total_cols": int(df.shape[1]), "matched": len(rows),
            "start": start, "limit": limit, "has_file_stats": bool(exact)}


def _index_info(idx):
    out = {"name": _name(idx.name) if idx.name is not None else None, "dtype": str(idx.dtype),
           "unique": bool(idx.is_unique), "is_dates": bool(_is_dates(idx))}
    if idx.is_monotonic_increasing:
        out["monotonic"] = "increasing"
    elif idx.is_monotonic_decreasing:
        out["monotonic"] = "decreasing"
    else:
        out["monotonic"] = None
    valid = idx.dropna() if len(idx) else idx
    if len(valid):
        try:
            lo, hi = valid.min(), valid.max()
            out["min"], out["max"] = _fmt_values(pd.Index([lo, hi]))
        except TypeError:
            pass
    if out["is_dates"] and len(valid) >= 3:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                freq = pd.infer_freq(valid[:1000])
            out["freq"] = getattr(freq, "freqstr", freq)
        except (TypeError, ValueError):
            out["freq"] = None
    return out


def overall():
    df = _require_df()
    nulls = int(df.isna().sum().sum())
    cells = int(df.shape[0] * df.shape[1])
    dtypes = df.dtypes.astype(str).value_counts()
    return {
        "shape": [int(df.shape[0]), int(df.shape[1])],
        "memory_bytes": mem_bytes(df),
        "total_nulls": nulls,
        "null_frac": nulls / cells if cells else None,
        "dtypes": [{"dtype": k, "count": int(v)} for k, v in dtypes.items()],
        "index_dtype": str(df.index.dtype),
        "index": _index_info(df.index),
        "is_original": STATE.df is STATE.df0,
        "info": STATE.info,
    }


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------

def _short_tb(e):
    tb = traceback.extract_tb(e.__traceback__)
    user = [fr for fr in tb if fr.filename == "<expression>"]
    lines = []
    if user:
        lines.append("".join(traceback.format_list(user[-3:])).rstrip())
    lines.append("".join(traceback.format_exception_only(type(e), e)).rstrip())
    return "\n".join(lines)


_MUTATING_METHODS = {"insert", "pop", "update", "__setitem__", "__delitem__"}


def _changes_df(tree):
    """Does this code assign to / delete from / mutate ``df``?"""
    def rooted_at_df(node):
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        return isinstance(node, ast.Name) and node.id == "df"

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if rooted_at_df(sub) and isinstance(getattr(sub, "ctx", None), (ast.Store, ast.Del)):
                        return True
        elif isinstance(node, ast.NamedExpr) and node.target.id == "df":
            return True
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and rooted_at_df(node.func.value):
            if node.func.attr in _MUTATING_METHODS or any(
                    k.arg == "inplace" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in node.keywords):
                return True
    return False


_BINDS = (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete, ast.NamedExpr, ast.Import, ast.ImportFrom,
          ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
MAX_STEPS = 500


def _defines_names(tree):
    """Does this code bind names a later expression could use (x = …, import, def)?"""
    return any(isinstance(node, _BINDS) for node in ast.walk(tree))


def _as_assignment(expr, node, result):
    """The expression with its final value assigned to df, as it became the view."""
    lines = expr.encode().splitlines(keepends=True)
    start = b"".join(lines[:node.lineno - 1]) + lines[node.lineno - 1][:node.col_offset]
    end = b"".join(lines[:node.end_lineno - 1]) + lines[node.end_lineno - 1][:node.end_col_offset]
    head, value, tail = (expr.encode()[:len(start)].decode(), ast.get_source_segment(expr, node),
                         expr.encode()[len(end):].decode())
    if isinstance(result, pd.DataFrame):
        value = f"df = {value}"
    elif isinstance(result, pd.Series):
        value = f"df = ({value}).to_frame()"
    else:
        value = f"df = pd.DataFrame({value})"
    return head + value + tail


def _record_step(expr, kind, code=None):
    """Remember a run that changed df or defined names, so the page can list
    exactly what produced the current data (and copy it as a script)."""
    STATE.steps.append({"expr": expr, "code": code or expr, "kind": kind,
                        "shape": [int(STATE.df.shape[0]), int(STATE.df.shape[1])]})
    del STATE.steps[:-MAX_STEPS]


def evaluate(expr):
    base = _require_df()
    if not isinstance(expr, str) or not expr.strip():
        raise UserError("Type a pandas expression, e.g. df.describe()")
    if len(expr) > 20000:
        raise UserError("Expression is too long")
    ns = STATE.ns
    # A shallow copy (copy-on-write) per run: the expression may change df
    # freely, and the loaded data stays intact for "Reset to original".
    work = base.copy(deep=False)
    ns.update(df=work, df0=STATE.df0, pd=pd, np=np)
    try:
        tree = ast.parse(expr, "<expression>", "exec")
    except SyntaxError as e:
        return {"ok": False, "error": _short_tb(e)}
    changes_df = _changes_df(tree)
    defines = _defines_names(tree)
    last = last_node = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_node = tree.body.pop().value
        last = ast.Expression(last_node)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            if tree.body:
                exec(compile(tree, "<expression>", "exec"), ns)
            result = eval(compile(last, "<expression>", "eval"), ns) if last else None
    except Exception as e:
        return {"ok": False, "error": _short_tb(e), "stdout": out.getvalue()[-MAX_VALUE_CHARS:]}

    stdout = out.getvalue()[-MAX_VALUE_CHARS:]
    frame = None
    if isinstance(result, pd.DataFrame):
        frame = result
    elif isinstance(result, pd.Series):
        frame = result.to_frame(result.name if result.name is not None else "value")
    elif isinstance(result, pd.Index):
        frame = result.to_frame(index=False, name=result.name if result.name is not None else "value")
    elif isinstance(result, np.ndarray) and result.ndim in (1, 2):
        frame = pd.DataFrame(result)
    if frame is not None:
        STATE.df = frame
        _record_step(expr, "view", _as_assignment(expr, last_node, result))
        return {"ok": True, "kind": "frame", "stdout": stdout, "view": view_info()}

    updated = ns.get("df")
    if changes_df and isinstance(updated, pd.DataFrame):
        STATE.df = updated
        _record_step(expr, "update")
        message = f"df updated: {updated.shape[0]:,} rows × {updated.shape[1]:,} columns."
        added = [c for c in updated.columns if c not in set(base.columns)]
        if added:
            message += f" New column{'s' if len(added) > 1 else ''}: {', '.join(map(_name, added[:5]))}"
            message += "…" if len(added) > 5 else "."
            empty = [c for c in added if updated[c].isna().all()] if updated.columns.is_unique else []
            if empty and len(updated):
                message += (f"\nNote: {', '.join(map(_name, empty[:5]))} is entirely empty: pandas matched the new "
                            f"values to df's index labels and none lined up. Use .to_numpy() (or index=df.index) "
                            f"to assign by position.")
        return {"ok": True, "kind": "frame", "stdout": stdout, "message": message, "view": view_info()}
    if defines:
        _record_step(expr, "define")
    text = None if result is None else repr(result)
    if text and len(text) > MAX_VALUE_CHARS:
        text = text[:MAX_VALUE_CHARS] + "…"
    return {"ok": True, "kind": "value", "text": text, "stdout": stdout, "view": view_info()}


def reset():
    """Back to the loaded data with a fresh namespace, so the (now empty) list
    of applied steps is exactly what produced the view."""
    _require_df()
    STATE.df = STATE.df0
    STATE.ns = {}
    STATE.steps = []
    return {"view": view_info()}


# ---------------------------------------------------------------------------
# Plot data (Plotly traces). Every row is plotted: numeric axes travel as
# binary float64 buffers, and scatter plots too big for SVG markers become a
# density image built from every point, redrawn as the user zooms.
# ---------------------------------------------------------------------------

MAX_GROUPS = 20          # beyond this, the rest are drawn together as "(other)"
MAX_CATEGORIES = 2000    # bar/box/histogram categories; the rest become "(other)"
MAX_BOX_CATEGORIES = 200
MARKER_LIMIT = 30_000    # SVG markers get too slow past this: draw a density image
BAR_LIMIT = 5000         # bars per series before drawing a filled step line instead
OTHER = "(other)"
PALETTE = [(47, 111, 222), (232, 116, 59), (25, 169, 121), (237, 74, 123), (148, 94, 207), (19, 164, 180),
           (212, 162, 11), (108, 117, 125)]

_BUFFERS = []


def _buf(arr):
    """Hand a float64 array to the worker as a binary buffer (NaN = missing)."""
    _BUFFERS.append(np.ascontiguousarray(arr, dtype=np.float64))
    return {"__buf__": len(_BUFFERS) - 1}


def take_buffers():
    out = list(_BUFFERS)
    _BUFFERS.clear()
    return out


def _series(df, pos):
    if pos is None:
        return None
    pos = int(pos)
    if pos == -1:
        return pd.Series(df.index, index=df.index, name=df.index.name or "index")
    if not (0 <= pos < df.shape[1]):
        raise UserError("Unknown column")
    return df.iloc[:, pos]


def _numeric(s):
    """(float64 values, axis type) for numeric or date series; (None, "category") otherwise.

    Dates become epoch milliseconds, which Plotly date axes take directly."""
    if _is_dates(s):
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_localize(None)
        arr = s.to_numpy(dtype="datetime64[ns]")
        out = arr.view("int64").astype(np.float64) / 1e6
        out[np.isnat(arr)] = np.nan
        return out, "date"
    if _is_numeric(s):
        return pd.to_numeric(s, errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan), "linear"
    return None, "category"


def _categories(s):
    return [None if v is None else _label(v) for v in _fmt_values(s)]


def _axis_payload(values, labels):
    return _buf(values) if values is not None else labels


def _fold(keys, limit, notes, what):
    """Keep the ``limit`` most common keys; the rest become "(other)" (still plotted)."""
    counts = keys.value_counts()
    if len(counts) <= limit:
        return keys, list(counts.index)
    top = set(counts.index[:limit])
    notes.append(f"{len(counts) - limit:,} less common {what} are drawn together as “{OTHER}”")
    keys = keys.where(keys.isin(top), OTHER)
    return keys, list(counts.index[:limit]) + [OTHER]


def _groups(color, notes):
    if color is None:
        return [(None, None)]
    keys = color.astype(str).where(color.notna(), "(null)")
    keys, order = _fold(keys, MAX_GROUPS, notes, "groups")
    return [(k, (keys == k).to_numpy()) for k in order]


def _rgba(c, a):
    return f"rgba({c[0]},{c[1]},{c[2]},{a})"


def _density_traces(series, view, px):
    """One transparent-background heatmap per series, from every point.

    series: [(name, color, x, y)] with finite float arrays. Pixels are grown by
    one so isolated points stay visible; colour depth is log(count)."""
    x0, x1, y0, y1 = view
    w, h = px
    traces = []
    xc = x0 + (np.arange(w) + 0.5) * (x1 - x0) / w
    yc = y0 + (np.arange(h) + 0.5) * (y1 - y0) / h
    for name, color, x, y in series:
        counts, _, _ = np.histogram2d(y, x, bins=[h, w], range=[[y0, y1], [x0, x1]])
        z = np.log1p(counts)
        grown = z.copy()
        grown[1:, :] = np.maximum(grown[1:, :], z[:-1, :])
        grown[:-1, :] = np.maximum(grown[:-1, :], z[1:, :])
        z2 = grown.copy()
        z2[:, 1:] = np.maximum(z2[:, 1:], grown[:, :-1])
        z2[:, :-1] = np.maximum(z2[:, :-1], grown[:, 1:])
        zmax = float(z2.max()) if z2.size else 1.0
        z2[z2 == 0] = np.nan
        traces.append({
            "type": "heatmap", "name": name, "x": _buf(xc), "y": _buf(yc),
            "z": {"__grid__": _buf(z2.ravel()), "w": int(w), "h": int(h)},
            "zmin": 0, "zmax": max(zmax, 1e-9), "showscale": False, "hoverinfo": "skip",
            "colorscale": [[0, _rgba(color, 0.45)], [1, _rgba(color, 1)]],
        })
    return traces


def _view_of(series, view):
    if view:
        return float(view["x0"]), float(view["x1"]), float(view["y0"]), float(view["y1"])
    xs = [x for _, _, x, _ in series if len(x)]
    ys = [y for _, _, _, y in series if len(y)]
    if not xs:
        return 0.0, 1.0, 0.0, 1.0
    x0, x1 = min(float(a.min()) for a in xs), max(float(a.max()) for a in xs)
    y0, y1 = min(float(a.min()) for a in ys), max(float(a.max()) for a in ys)
    px, py = (x1 - x0) * 0.02 or 1.0, (y1 - y0) * 0.02 or 1.0
    return x0 - px, x1 + px, y0 - py, y1 + py


def plot(kind="scatter", x=None, ys=(), color=None, agg="mean", bins=40, view=None, px_w=1200, px_h=450):
    df = _require_df()
    _BUFFERS.clear()
    ys = [int(y) for y in (ys or [])][:20]
    kind = str(kind)
    if kind not in ("line", "scatter", "histogram", "bar", "box"):
        raise UserError("Unknown plot type")
    bins = int(max(2, min(int(bins), 500)))
    px = (int(max(100, min(int(px_w), 2400))), int(max(100, min(int(px_h), 1400))))
    notes = []
    traces = []
    extra = {}
    xs = _series(df, x)
    cs = _series(df, color)
    if not ys and kind in ("line", "scatter", "box"):
        raise UserError("Choose at least one Y column")
    groups = _groups(cs, notes)
    xname = _label(xs.name) if xs is not None and xs.name is not None else ("index" if x == -1 else "")
    colors = iter(PALETTE * 10)

    def gname(yname, g):
        if g is None:
            return yname
        return f"{g}" if len(ys) <= 1 else f"{yname} · {g}"

    if kind in ("line", "scatter"):
        if xs is None:
            xs = _series(df, -1)
            xname = _label(df.index.name or "index")
        xv, xtype = _numeric(xs)
        xl = None if xv is not None else _categories(xs)
        yv = {y: _numeric(df.iloc[:, y]) for y in ys}
        ytype = "linear"
        if any(t == "date" for _, t in yv.values()):
            ytype = "date"
        if any(v is None for v, _ in yv.values()):
            ytype = "category"
        # one entry per (y, group): (name, colour, positions)
        parts = []
        for g, mask in groups:
            pos = np.arange(len(df)) if mask is None else np.flatnonzero(mask)
            if kind == "line" and xv is not None:
                pos = pos[np.argsort(xv[pos], kind="stable")]
            for y in ys:
                parts.append((gname(_label(df.columns[y]), g), next(colors), y, pos))
        numeric_xy = xv is not None and all(v is not None for v, _ in yv.values())
        total = 0
        if kind == "scatter" and numeric_xy:
            series = []
            for name, col, y, pos in parts:
                xa, ya = xv[pos], yv[y][0][pos]
                ok = np.isfinite(xa) & np.isfinite(ya)
                series.append((name, col, xa[ok], ya[ok]))
            total = sum(len(s[2]) for s in series)
            if total > MARKER_LIMIT:
                extra["dynamic"] = True
                vx0, vx1, vy0, vy1 = _view_of(series, view)
                inview = [(n, c, a[(a >= vx0) & (a <= vx1) & (b >= vy0) & (b <= vy1)],
                           b[(a >= vx0) & (a <= vx1) & (b >= vy0) & (b <= vy1)]) for n, c, a, b in series]
                shown = sum(len(s[2]) for s in inview)
                if shown <= MARKER_LIMIT:
                    for name, col, a, b in inview:
                        traces.append({"type": "scatter", "mode": "markers", "name": name, "x": _buf(a), "y": _buf(b),
                                       "marker": {"size": 5, "opacity": 0.7, "color": _rgba(col, 1)}})
                    notes.append(f"Zoomed in: all {shown:,} points in view drawn individually")
                else:
                    traces = _density_traces(inview, (vx0, vx1, vy0, vy1), px)
                    if len(inview) > 1:  # legend entries for the coloured layers
                        traces += [{"type": "scatter", "mode": "markers", "x": [None], "y": [None], "name": n,
                                    "marker": {"color": _rgba(c, 1), "size": 9}} for n, c, _, _ in inview]
                    notes.append(f"All {shown:,} points drawn as a density image (darker = more points). "
                                 f"Zoom in to see individual points (≤ {MARKER_LIMIT:,} in view).")
                extra["view"] = {"x0": vx0, "x1": vx1, "y0": vy0, "y1": vy1}
                extra["points"] = total
            else:
                for name, col, a, b in series:
                    traces.append({"type": "scatter", "mode": "markers", "name": name, "x": _buf(a), "y": _buf(b),
                                   "marker": {"size": 5, "opacity": 0.7, "color": _rgba(col, 1)}})
        else:
            for name, col, y, pos in parts:
                yvals, _ = yv[y]
                ylab = None if yvals is not None else _categories(df.iloc[pos, y])
                total += len(pos)
                tr = {"type": "scatter", "mode": "lines" if kind == "line" else "markers", "name": name,
                      "x": _axis_payload(None if xv is None else xv[pos], None if xl is None else [xl[i] for i in pos]),
                      "y": _axis_payload(None if yvals is None else yvals[pos], ylab)}
                if kind == "line":
                    tr["line"] = {"width": 1.5, "color": _rgba(col, 1)}
                else:
                    tr["marker"] = {"size": 5, "opacity": 0.7, "color": _rgba(col, 1)}
                traces.append(tr)
            if kind == "scatter" and total > MARKER_LIMIT:
                notes.append(f"{total:,} points with a text axis are drawn individually, which can be slow")
        layout = {"xaxis": {"title": {"text": xname}, "type": xtype},
                  "yaxis": {"title": {"text": ", ".join(_label(df.columns[y]) for y in ys)[:120]}, "type": ytype}}
        if extra.get("view"):
            v = extra["view"]
            layout["xaxis"]["range"] = [v["x0"], v["x1"]]
            layout["yaxis"]["range"] = [v["y0"], v["y1"]]

    elif kind == "histogram":
        cols = ys or ([x] if x is not None else [])
        if not cols:
            raise UserError("Choose a column to histogram (Y)")
        for c in cols:
            s = _series(df, c)
            for g, mask in groups:
                v = s if mask is None else s[mask]
                v = v.dropna()
                name = gname(_label(s.name), g)
                col = next(colors)
                vals, vtype = _numeric(v)
                if vals is not None:
                    vals = vals[np.isfinite(vals)]
                    if not len(vals):
                        continue
                    counts, edges = np.histogram(vals, bins=bins)
                    traces.append({"type": "bar", "name": name, "x": _buf((edges[:-1] + edges[1:]) / 2),
                                   "y": _buf(counts), "width": _buf(np.diff(edges)),
                                   "marker": {"color": _rgba(col, 0.75 if len(groups) * len(cols) > 1 else 1)}})
                    extra["x_type"] = vtype
                else:
                    keys, order = _fold(v.astype(str), 50, notes, "values")
                    vc = keys.value_counts().reindex(order)
                    traces.append({"type": "bar", "name": name, "x": [_label(k) for k in order],
                                   "y": _buf(vc.to_numpy(dtype=float)), "marker": {"color": _rgba(col, 1)}})
        layout = {"barmode": "overlay", "bargap": 0, "yaxis": {"title": {"text": "count"}},
                  "xaxis": {"title": {"text": ", ".join(_label(_series(df, c).name) for c in cols)[:120]},
                            "type": extra.pop("x_type", "category")}}

    elif kind == "bar":
        if agg not in ("sum", "mean", "median", "min", "max", "count"):
            raise UserError("Unknown aggregation")
        if not ys and xs is None:
            raise UserError("Choose a Y column, or an X column to count")
        if xs is None:
            # every row against the index; past BAR_LIMIT rows a filled step line draws the same shape fast
            ix = _series(df, -1)
            iv, itype = _numeric(ix)
            il = None if iv is not None else _categories(ix)
            for y in ys:
                col = next(colors)
                vals, _ = _numeric(df.iloc[:, y])
                if vals is None:
                    raise UserError(f"Bar heights need numbers ({_label(df.columns[y])} is {df.dtypes.iloc[y]})")
                tr = {"name": _label(df.columns[y]), "x": _axis_payload(iv, il), "y": _buf(vals)}
                if len(df) <= BAR_LIMIT:
                    tr.update(type="bar", marker={"color": _rgba(col, 1)})
                else:
                    tr.update(type="scatter", mode="lines", fill="tozeroy", line={"shape": "hvh", "width": 1, "color": _rgba(col, 1)})
                traces.append(tr)
            if len(df) > BAR_LIMIT:
                notes.append(f"{len(df):,} rows: drawn as a filled step line (one step per row)")
            layout = {"barmode": "group", "xaxis": {"title": {"text": _label(df.index.name or "index")}, "type": itype}}
        else:
            if _is_numeric(xs) and xs.nunique() <= MAX_CATEGORIES:
                keys = xs
                cats = sorted(keys.dropna().unique().tolist())
                if keys.isna().any():
                    keys = keys.astype(object).where(keys.notna(), "(null)")
                    cats.append("(null)")
            else:
                keys = xs.astype(str).where(xs.notna(), "(null)")
                keys, cats = _fold(keys, MAX_CATEGORIES, notes, "X values")
                head = [c_ for c_ in cats if c_ != OTHER]
                try:
                    head = sorted(head)
                except TypeError:
                    pass
                cats = head + ([OTHER] if OTHER in cats else [])
            for g, mask in groups:
                m = np.ones(len(df), dtype=bool) if mask is None else mask
                for y in (ys or [None]):
                    col = next(colors)
                    if y is None:
                        series = pd.Series(1, index=df.index)[m]
                        yname = "count"
                    else:
                        series = df.iloc[:, y][m]
                        yname = _label(df.columns[y])
                    grouped = series.groupby(keys[m].to_numpy())
                    try:
                        vals = grouped.count() if agg == "count" or y is None else getattr(grouped, agg)(numeric_only=False)
                    except TypeError:
                        raise UserError(f"Can't take the {agg} of column {yname}")
                    vals = pd.to_numeric(vals.reindex(cats), errors="coerce")
                    traces.append({"type": "bar", "name": gname(f"{agg}({yname})" if y is not None else yname, g),
                                   "x": [_label(_cell(c_)) for c_ in cats], "y": _buf(vals.to_numpy(dtype=float)),
                                   "marker": {"color": _rgba(col, 1)}})
            layout = {"barmode": "group", "xaxis": {"title": {"text": xname}, "type": "category"},
                      "yaxis": {"title": {"text": agg if ys else "count"}}}

    else:  # box: precomputed quartiles so only five numbers per box cross over
        if xs is not None:
            keys = xs.astype(str).where(xs.notna(), "(null)")
            keys, cats = _fold(keys, MAX_BOX_CATEGORIES, notes, "X values")
            keys = keys.to_numpy()
            parts = [(c_, keys == c_) for c_ in cats]
        for y in ys:
            s = df.iloc[:, y]
            if not _is_numeric(s):
                raise UserError(f"Box plots need numeric columns ({_label(df.columns[y])} is {s.dtype})")
            vals = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)
            if xs is None:
                parts = [(df.columns[y], np.ones(len(df), dtype=bool))]
            for g, mask in groups:
                col = next(colors)
                q1, med, q3, lo, hi, mean, xcat = [], [], [], [], [], [], []
                for cname, pm in parts:
                    v = vals[pm if mask is None else (pm & mask)]
                    v = v[np.isfinite(v)]
                    if not len(v):
                        continue
                    a_, m_, b_ = np.percentile(v, [25, 50, 75])
                    iqr = b_ - a_
                    lo.append(float(v[v >= a_ - 1.5 * iqr].min()))
                    hi.append(float(v[v <= b_ + 1.5 * iqr].max()))
                    q1.append(float(a_))
                    med.append(float(m_))
                    q3.append(float(b_))
                    mean.append(float(v.mean()))
                    xcat.append(_label(cname))
                if xcat:
                    traces.append({"type": "box", "name": gname(_label(df.columns[y]), g), "x": xcat,
                                   "q1": q1, "median": med, "q3": q3, "lowerfence": lo, "upperfence": hi,
                                   "mean": mean, "boxpoints": False, "marker": {"color": _rgba(col, 1)}})
        layout = {"boxmode": "group", "xaxis": {"title": {"text": xname}, "type": "category"}}

    visible = [t for t in traces if t["type"] != "heatmap"]
    layout.update({"showlegend": len(visible) > 1})
    return {"traces": traces, "layout": layout, "notes": sorted(set(notes)), "rows": int(len(df)), **extra}


# ---------------------------------------------------------------------------
# Dispatch (called from the worker)
# ---------------------------------------------------------------------------

def _payload_int(p, key, default=None):
    v = p.get(key, default)
    return None if v is None else int(v)


def dispatch(kind, payload_json, wheel_data=None):
    _BUFFERS.clear()
    try:
        p = json.loads(payload_json) if payload_json else {}
        if kind == "inspect":
            if wheel_data:
                install_wheels(wheel_data)
            res = inspect(p["path"], p["name"])
            if res.get("needs_wheels") and _have_openpyxl():
                res = inspect(p["path"], p["name"])
        elif kind == "load":
            if wheel_data:
                install_wheels(wheel_data)
            res = load(p["path"], p["name"], p.get("key"), p.get("budget_mb", DEFAULT_BUDGET_MB),
                       index=p.get("index") or "auto", columns=p.get("columns"), where=p.get("where"),
                       sql=p.get("sql"))
        elif kind == "table":
            res = table(p.get("mode", "head"), p.get("n", 50), _payload_int(p, "sort"), p.get("ascending", True),
                        p.get("seed", 0), p.get("col_start", 0), p.get("col_count", DEFAULT_TABLE_COLS),
                        p.get("col_filter", ""))
        elif kind == "colstats":
            res = colstats(p.get("start", 0), p.get("limit", 200), p.get("col_filter", ""), p.get("sort"),
                           p.get("ascending", True))
        elif kind == "overall":
            res = overall()
        elif kind == "eval":
            res = evaluate(p.get("expr", ""))
        elif kind == "reset":
            res = reset()
        elif kind == "plot":
            res = plot(p.get("kind", "scatter"), _payload_int(p, "x"), p.get("ys", []), _payload_int(p, "color"),
                       p.get("agg", "mean"), p.get("bins", 40), p.get("view"), p.get("px_w", 1200), p.get("px_h", 450))
        elif kind == "scan":
            res = scan_rows()
        elif kind == "ping":
            res = {"pong": True, "python": sys.version.split()[0], "pandas": pd.__version__}
        else:
            raise UserError(f"Unknown request {kind!r}")
        return json.dumps({"ok": True, "result": _clean(res)}, allow_nan=False)
    except UserError as e:
        return json.dumps({"ok": False, "error": str(e)})
    except MemoryError:
        return json.dumps({"ok": False, "error": "Out of memory. Try a smaller sample budget or reset the view."})
    except Exception as e:
        return json.dumps({"ok": False, "error": f"Internal error: {type(e).__name__}: {e}",
                           "detail": traceback.format_exc()[-4000:]})
