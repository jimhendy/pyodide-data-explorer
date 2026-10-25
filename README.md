# Data Explorer (Pyodide)

A static, fully client-side data explorer. Python (pandas, pyarrow, h5py) runs
in the browser via [Pyodide](https://pyodide.org) 314.0.7; the host serves
static files only and data files never leave the user's machine.

**Formats:** parquet, h5/hdf5 (pandas fixed/table layouts and plain datasets,
read with h5py), csv/tsv, xlsx, json (records), json lines.
**Views:** table (head / tail / random, sortable), plot (scatter, line,
histogram, bar, box; colour/group), per-column stats, overview, and a
pandas expression box with history and reset.
**Loading options:** for parquet, CSV and JSON lines, choose which columns to
load, filter rows with an SQL condition, or run any SQL query over the file
(DuckDB, also running inside the sandbox).

The original brief is in [BRIEF.md](BRIEF.md). [DEVELOPMENT_NOTES.md](DEVELOPMENT_NOTES.md)
lists the problems hit during development and how each was solved; read it
before rebuilding or adapting the app.

## Run it

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python pandas pyarrow h5py openpyxl tables duckdb==1.5.1 pytest playwright
.venv/bin/python build.py          # -> dist/
.venv/bin/python serve.py          # http://localhost:8765 and your LAN address
```

To reach it from a phone on the same network, allow the port through the
firewall, e.g. `sudo ufw allow from 192.168.0.0/24 to any port 8765 proto tcp`.

## Large files

Files are never copied into WASM memory. The worker mounts the browser `File`
with Emscripten's WORKERFS, so Python reads only the byte ranges it asks for.
Each loader probes the first rows to estimate in-memory size; if the whole
file would exceed the **memory budget** (selectable, default 200 MB, 100 MB on
phones) it loads a sample instead:

| Format | Sample |
|---|---|
| csv, jsonl | 64 blocks at evenly spaced random offsets (first and last always included), cut to whole lines |
| parquet | contiguous slices from up to 16 row groups spread across the file; exact null counts / min / max for the whole file from metadata |
| h5 | 64 row blocks spread across the dataset; index = row number in file |
| json array | the first records (an array can only be read from the start) |
| xlsx | the first rows of the sheet |

The UI always says when it is showing a sample. Row counts are exact for
parquet/h5; for csv/jsonl they are estimated, with a button to count exactly
(streams the whole file, ~10–17 s for 5 GB). Loading briefly uses about 3–4× the
budget in WASM memory; WASM memory never shrinks, so the budget options stop at
500 MB.

Measured in headless Chrome on the ~5 GB test files (200 MB budget):

| File | Load | Rows sampled | Peak Python heap |
|---|---|---|---|
| big.csv (5.0 GB) | 7 s | 3.1 M of 94 M | 0.7 GB |
| big.jsonl (5.0 GB) | 21 s | 2.0 M of 47 M | 0.8 GB |
| big.json (5.1 GB) | 31 s | 2.1 M (head) | 0.7 GB |
| big.parquet (5.1 GB) | 6 s | 1.9 M of 74 M | 0.7 GB |
| big.h5 grid (5.1 GB) | 2 s | 2.6 M of 63 M | 0.5 GB |
| wide csv, 2,000 cols (4.8 GB) | 9 s | 13 k of 390 k | 0.5 GB |
| wide parquet, 2,000 cols, 320 MB row groups (5.1 GB) | 28 s | 13 k of 960 k | 1.0 GB |
| wide h5, 2,000 cols (5.1 GB) | 3 s | 13 k of 320 k | 0.5 GB |

Wide parquet is the slowest: even a few rows of a row group require reading
the start of every column chunk in it, so the sampler caps the bytes read at
about 3× the budget (5 of 48 row groups for the file above).

## Wide data and indexes

Frames with thousands of columns are handled without sending them all to the
page:

- **Table**: 50 columns at a time with a pager and a name filter; only the
  visible cells are copied. Sorting works on any column, including ones on
  other pages.
- **Column stats**: computed once per view (vectorised per dtype: count,
  nulls, unique, min/max/mean/std, idxmin/idxmax), then served 200 at a time;
  filter by name and sort by any stat (e.g. most nulls first).
- **Plot**: X, colour and each Y are dropdowns ("+ Add Y", up to 20 series).
- **Sampling** sizes its first read and its blocks by row width, so a
  2,000-column row doesn't blow the budget.

**Index.** Indexes are usually dates but not always. With *Index: Auto* the
app keeps an index the file stores (parquet, pandas HDF5), and for csv / json /
xlsx uses the first column when it is a saved pandas index (`Unnamed: 0`),
holds dates, or is a unique text label in front of all-numeric columns. ISO
date strings in the index become a `DatetimeIndex`. *First column* and *Row
numbers* override this. Dates with no time part display as `2024-01-31`, and
the Overall stats tab shows the index range, inferred frequency, order and
uniqueness. A dated index makes Line the default plot.

## Choosing columns and filtering rows

Opening a parquet, CSV or JSON lines file first shows **Choose what to load**,
before anything is read into memory. It appears in place of the open screen,
or as a dialog when data is already open; Cancel then keeps the open data.
Load is one click if you want everything. **Columns & filter** in the top bar
reopens it for the loaded file. It lists the file's columns, read cheaply from
the parquet footer or the first rows of a text file, and offers:

- **Columns:** a multi-select. Chosen columns collect as removable chips, or
  as a one-line summary when thousands are chosen. You can search and tick,
  shift-click a range, and use All / None / Only these on the search matches.
  Index columns (a parquet file's stored index, or a text file's first date
  column) are always loaded. Parquet subsets are
  read with pyarrow, so only those columns' chunks are read: four of 2,000
  columns of a 5 GB file load in seconds, with far more rows in the budget.
- **Filter rows:** an SQL `WHERE` condition, e.g. `region = 'south' AND price > 100`.
- **SQL query:** any DuckDB query, with the file as the table `data`.

Filters and queries run in DuckDB (Pyodide's `duckdb` package, ~9 MB, fetched
on first use) over a stream of Arrow batches read by pyarrow, so only matching
rows reach pandas. In Pyodide, pyarrow's parquet / CSV / JSON readers are
several times faster than DuckDB's own (a 5 GB CSV: minutes rather than a
quarter of an hour), and only the columns the filter or query mentions are
decoded. If pyarrow can't read a file consistently (e.g. a CSV column whose
values stop matching the type inferred from the first 8 MB), or a query reads
`data` twice, DuckDB's own readers are used instead.
If they still exceed the memory budget, a seeded reservoir sample is taken in
the same pass and kept in file order (or the query's `ORDER BY`), and the
matching rows are counted. The SQL it will run is shown before loading. DuckDB is
locked down even inside the sandbox: it can read only the folder holding the
mounted file, can't install or load extensions, runs in one thread with
bounded memory, and its configuration is locked so a query can't undo any of
this.

## Plots use every row

Nothing is downsampled: every loaded row is plotted (for a sampled file, every
row of the sample). Numeric and date axes travel from Python as binary float64
buffers rather than JSON, so a line with a million points draws in about a
second. SVG markers get slow past ~30,000 points, and Plotly's WebGL mode needs
`unsafe-eval`, so larger scatter plots are drawn as a density image computed
from every point (one coloured layer per group) and recomputed as you zoom;
once 30,000 or fewer points are in view they are drawn as individual,
hoverable markers. Bars with no X past 5,000 rows become a filled step line
(one step per row). Categories or colour groups beyond the display limit are
drawn together as "(other)" rather than dropped.

## Expressions that change `df`

`df['New'] = …`, `del df['x']`, `df.insert(…)`, `inplace=True` calls and
`df = …` all update the view: the table, column choices, plot dropdowns and
column stats pick up the change, and later expressions build on it. Each run
works on a copy-on-write copy, so the loaded data is untouched and "Reset to
original" always works. If a new column comes out entirely empty because
pandas aligned it on index labels (e.g. assigning `pd.Series(range(n))` to a
date-indexed frame), the app says so and suggests `.to_numpy()`.

**Steps** (next to Reset) lists what produced the current `df`, with the row
and column count after each step:

- how the file was loaded: columns, filter or SQL query, and whether it was sampled;
- every run that changed `df`, made its result the view, or defined names a
  later run might use (`x = …`, `import`, `def`).

Runs that only look at the data (`df.shape`, `print(…)`) aren't listed.
"Copy as Python" gives a script that replays the steps. A result that became
the view is written as `df = …`. The reader line at the top is approximate,
since the app also picks the index, parses dates and may sample. Reset clears
the steps and starts a fresh namespace, so the list always covers everything
that affects the next run. History is separate: it keeps every expression
across files.

## Light and dark mode

The app follows the system theme. The button at the right of the top bar
cycles through system, light and dark, and the choice is remembered in
`localStorage`. It is applied by `theme.js` in `<head>`, before the first paint,
since an inline script would need a CSP hash. A drawn plot is recoloured in
place, so it keeps its zoom.

## Security model

The threat is "attacker gets code run in a victim's browser on this origin".

1. **Sandboxed Python.** Pyodide runs in a Web Worker created inside
   `sandbox.html`, which is framed with `sandbox="allow-scripts"` (no
   `allow-same-origin`) *and* served with CSP `sandbox allow-scripts`, so it
   has an opaque origin even if opened directly. It cannot touch the app's DOM,
   storage or cookies. The page and sandbox talk only via `postMessage`; the
   sandbox drops any message that doesn't match a strict per-type schema, and
   the page only accepts messages from the frame's window with origin `null`.
2. **Nothing runs from a URL.** The app never reads `location`, query or hash.
   Expressions run only when the user presses Run (or Ctrl/⌘+Enter); history
   items only fill the box.
3. **Strict CSP.** App page: `default-src 'none'`, scripts/styles/connections
   to `'self'` only, `frame-ancestors 'none'`. Sandbox: scripts and fetches
   limited to the pinned Pyodide path on jsDelivr, plus `'wasm-unsafe-eval'`.
   No inline scripts anywhere; inline styles are allowed only by hash: the
   empty string (the marker that stops Plotly injecting styles) and Plotly's
   fixed `image-rendering` attribute for heatmap pixels (`'unsafe-hashes'`).
4. No cookies, auth, secrets or analytics. Deploy on a dedicated origin.
5. Pinned versions: Pyodide 314.0.7, Plotly 4.1.2 (vendored), openpyxl 3.1.5
   and et_xmlfile 2.0.0 (vendored wheels, SHA-256 checked before install).
6. Untrusted file content is always rendered as text; labels sent to Plotly
   have `<`/`>` replaced. pandas HDF5 attributes are pickles: they are read
   with an unpickler that refuses every class except numpy's array
   reconstructors, so a malicious .h5 can't execute code.
7. Long or runaway expressions and queries can be cancelled: the worker is
   terminated, Python restarts and the file is reloaded.
8. SQL (DuckDB) runs in the same sandboxed worker, restricted to the mounted
   file's folder with extensions disabled and its configuration locked.
   Loading options travel as validated message fields, never from a URL.
9. **Running your own Python is the point, and it stays in the sandbox.**
   Expressions are arbitrary Python (`import os`, `open(…)`, `js.…` all
   work), but they run in an Emscripten VM:
   - `os.environ` is Emscripten's fixed fake environment (`USER=web_user`,
     `HOME=/home/pyodide`), not your machine's.
   - The file system is in memory and holds only the app's module and the
     file you picked, read through a read-only browser `File`. Nothing can be
     written back to disk.
   - There are no processes, and sockets can't connect.
   - The CSP blocks fetches, `importScripts` and WebSockets to anywhere except
     the pinned Pyodide folder on jsDelivr. There is no access to the page,
     its storage or cookies.

   What this doesn't protect is the loaded data from code you paste: such
   code can read it, as you can. The one way out is a request to the pinned
   jsDelivr folder, which would put data in a URL in jsDelivr's logs. An
   attacker couldn't read those logs, but only run expressions you understand.
   `tests/e2e/test_app.py` checks these limits.

## Deploy

`dist/` is the whole site. Header rules are generated in `dist/_headers`
(Netlify / Cloudflare Pages format) and `dist/headers.json`; serve the
`Content-Security-Policy` for `/`, `/index.html` and `/sandbox.html` exactly as
given, or the sandbox guarantees are lost. Use HTTPS in production.

## Tests

```bash
.venv/bin/python -m pytest tests/test_explorer.py tests/test_wide.py tests/test_load_options.py  # Python core, natively (~45 s)
.venv/bin/python -m pytest tests/e2e --ignore=tests/e2e/test_large.py  # real app in headless Chrome (~12 min)
.venv/bin/python tests/make_large.py                   # ~40 GB of 5 GB test files, once
.venv/bin/python -m pytest tests/e2e/test_large.py     # 5 GB files in the browser (~6 min)
```

Fixtures are regenerated with `python tests/make_fixtures.py`. The e2e tests
use the system Chrome (`channel="chrome"`).
