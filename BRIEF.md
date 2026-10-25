# Data Explorer (Pyodide) — project brief

## Goal
A static, fully client-side data explorer that non-technical users can open in a
browser. It replaces locally run versions of the same tool. All Python runs in the
user's browser via Pyodide, so the host serves static files only and executes no
user code. Data files never leave the user's machine.

## Features
- **Load:** local file picker / drag-and-drop for parquet, h5/hdf5, csv, Excel
  (xlsx), json (records and lines). No uploads to any server.
- **Table tab:** sample of rows (head / tail / random sample), sortable.
- **Plot tab:** pick x, y, colour/group columns; line, scatter, histogram, bar,
  box. Combine multiple columns.
- **Column stats tab:** per-column dtype, count, null count and fraction, unique
  count, min/max, mean/std, idxmin/idxmax.
- **Overall stats tab:** shape, memory usage, total nulls, dtypes summary.
- **Expression box:** free-text pandas expression evaluated against `df`
  (e.g. `df[df.name.eq("eg")].T`). Result replaces the current view, with a
  "reset to original" button and a history of past expressions.
- For h5 files: let the user choose a dataset/key before loading.

## Security model (build this FIRST, before the UI)
The threat is "attacker gets code run in a victim's browser on this origin".
Pyodide's `import js` bridge means Python can do anything JS on the page can.
1. Run Pyodide inside a **sandboxed iframe with an opaque origin**
   (`sandbox="allow-scripts"`, never `allow-same-origin`), or at minimum a Web
   Worker. Communicate only via `postMessage`, and validate message shapes.
2. **Never auto-execute expressions from the URL**, query strings, hash, or any
   shared link. If a share feature is added, it pre-fills the expression box
   and requires an explicit click to run.
3. **Strict CSP:** `default-src 'self'`; `connect-src` limited to self and the
   pinned Pyodide CDN; no inline scripts except via hashes; `frame-ancestors 'none'`.
4. No cookies, no auth, no secrets, no analytics. Deploy on a dedicated origin.
5. Pin Pyodide and all JS dependencies to exact versions.

## Stack
- Pyodide (pinned version) with pandas, numpy, h5py, pyarrow. Verify each is in
  the current Pyodide package list; install openpyxl via `micropip`.
- Consider DuckDB-WASM for fast parquet/csv/json reads and sampling of large files.
- Plotting: Plotly.js (pandas results passed across as JSON) or similar.
- Plain HTML/JS or a light framework; must build to static files.

## Constraints and gotchas
- WASM memory is capped at a few GB: sample large files and read parquet by row group.
- Cold start is tens of MB: show a clear loading state; rely on browser caching.
- Single-threaded: keep the UI responsive by running Python off the main thread.
- Show Python errors from expressions clearly; never crash the app.

## Suggested order
1. Sandbox + CSP + Pyodide boot with a trivial round-trip message.
2. File loading for each format, with a format test fixture for each.
3. Table and stats tabs.
4. Expression box with history and reset.
5. Plot tab.
6. Static build and deploy instructions.

## Testing
- Small fixture files for every format in `tests/fixtures/`.
- Tests that confirm URL parameters never trigger execution and that the
  sandboxed frame cannot read the parent's DOM or storage.
