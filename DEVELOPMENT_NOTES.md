# Development notes: issues hit and decisions made

This file is for anyone rebuilding this app, possibly for a different use case.
It lists the problems we hit while building it, what caused each one, and the
fix or decision that came out of it. Read it alongside [BRIEF.md](BRIEF.md),
the original specification, and [README.md](README.md), which describes the
finished architecture, security model and tests.

## How the app evolved

These were the requests, in order. Each one changed the design, so it helps to
know them before reading the issues.

1. **Build from the brief.** Sandbox, CSP and Pyodide came first. The app had
   to be tested on a phone over the LAN and handle ~5 GB files.
2. **Wide data.** Thousands of columns as well as thousands of rows. Indexes
   are usually dates, but not always.
3. **Plot and expression changes.** Y columns are picked from dropdowns, not
   checkboxes. The plot shows every row, while the table may show a subset.
   A column added by an expression (e.g. `df['New'] = ...`) must appear
   everywhere: in the table, the column pickers and the stats. The layout uses
   the full viewport width.
4. **Video demo,** because the user was remote. This led to
   `tools/record_demo.py`.
5. **Loading part of a file.** A column subset for parquet, and DuckDB
   filtering. The UI was redesigned to use space efficiently and look more
   professional.
6. **Dark mode.**
7. **A list of the steps that produced the current `df`,** separate from the
   history of expressions across files. The user also asked: "is
   `import os; os.environ` safe?"
8. **Load options before any data is read,** like the HDF5 key picker, not
   after. Columns are a multi-select.

## Platform facts that shaped everything

- **Pyodide 314.0.7** ships Python 3.14, pandas 3.0.2, pyarrow 22, h5py and
  duckdb 1.5.1, all from the same pinned jsDelivr path. Check the Pyodide
  package list for each library; don't assume it's there.
- **There is no PyTables in Pyodide.** pandas `read_hdf` doesn't work.
  pandas-written HDF5 (fixed and table layouts) is read with h5py and a
  restricted unpickler for the metadata. `tests/fixtures/malicious.h5` checks
  that a pickle payload can't run code.
- **openpyxl isn't in Pyodide either,** and micropip would need network access
  the CSP doesn't allow. The openpyxl 3.1.5 and et_xmlfile 2.0.0 wheels are
  vendored in `src/wheels`. Python checks their SHA-256, unzips them onto
  `sys.path`, and doesn't use micropip.
- **numpy's default int is int32 in Pyodide (WASM32).** `np.arange(n)**2`
  overflows silently. Use `pd.Series(range(n), index=df.index)`, or pass
  `dtype='int64'`.
- **WASM memory is about 2–4 GB, and the browser kills the tab past it.** Never
  copy a file into WASM. Files are mounted with Emscripten **WORKERFS**, so
  Python reads only the byte ranges it asks for.
- **An opaque-origin sandbox can't fetch its own site's files.** `build.py`
  embeds `worker.js` and `explorer.py` into `sandbox.js` as strings, and the
  worker is created from a blob URL.

## Issues, grouped by area

### Security, CSP and the sandbox

| Problem | Cause | Fix |
|---|---|---|
| `sandbox.js` wouldn't load in the iframe | A `Cross-Origin-Resource-Policy: same-origin` header. The sandbox has an opaque origin, so to the browser that request is cross-origin | Removed CORP |
| Plotly's CSS was blocked | Plotly injects a `<style>` element at runtime | Vendored `plotly.css`. Added an empty `<style id="plotly.js-style-global" class="no-inline-styles">` marker so Plotly skips injecting. style-src allows the empty-string hash |
| The heatmap was blocked by the CSP | Plotly sets an inline `style="image-rendering:…"` attribute | `'unsafe-hashes'` plus the hash of that exact attribute value |
| No WebGL plots | `scattergl` needs `unsafe-eval` | SVG markers up to 30k points, then a density heatmap (see Plots) |
| A theme script in `<head>` was blocked | No inline scripts are allowed | `theme.js` loaded as an external file in `<head>`, so the theme applies before first paint |
| Inline `style=` attributes in our own HTML were blocked | style-src has no `'unsafe-inline'` | Use classes, or set styles through the CSSOM (`el.style.x = …`), which the CSP allows |
| No browser storage inside the sandbox | An opaque origin has no storage (IndexedDB raises SecurityError) | Expression history and the theme are kept in the parent page's localStorage, wrapped in try/catch |
| DuckDB settings failed in the connect config | `allowed_directories` etc. aren't accepted at connect time | Connect, then `SET allowed_directories=[folder]`, `SET enable_external_access=false`, `SET lock_configuration=true`. threads=1, autoload and autoinstall off |
| The user asked whether `import os; os.environ` is safe | — | Yes. Probes show Emscripten's fake environment (USER=web_user), a filesystem holding only the app and the mounted file, no processes, sockets that fail, and the CSP blocking XHR/WebSocket/importScripts to other hosts. The one remaining channel: a GET to the pinned jsDelivr folder can leak data into CDN logs. Tightening `connect-src` to exact Pyodide file URLs would close it (offered, not done). There's a test for this: `test_python_sees_only_the_emulated_machine` |
| Putting a password in front of the site (HTTP Basic auth) risks breaking the sandbox | Requests made from the opaque-origin sandbox count as cross-origin, so the browser may not send the password with them | `sandbox.html` and `sandbox.js` are exempt from the password. They hold only public app code; everything else, including the wheels the parent page fetches, needs the password |
| Messages from the worker could be spoofed | Python can call `postMessage` itself through `import js` | The parent accepts messages only from the sandbox frame (`e.source`, origin `'null'`), and the sandbox accepts only well-formed requests from its parent. Everything is rendered with `textContent`, so a spoofed message can't inject HTML |

### Loading large files (~5 GB)

| Problem | Cause | Fix |
|---|---|---|
| CSV parsing ran out of memory | — | `low_memory`, the sampled blocks joined into a single `BytesIO` buffer, `parse_dates` |
| The JSON-lines heap reached 2.75 GB | Reading the whole sample at once | Read in chunks |
| JSON arrays used too much memory | — | Read in batches. An array can only be read from the start, so the sample is the first records |
| Full parquet reads got a meaningless `row` index instead of the saved one | — | Restore the index from the pandas metadata (`use_pandas_metadata=True` for column subsets too) |
| Wide parquet took 50 s | Every row group reads whole column chunks | A cap on bytes read (~3× the memory budget). `pre_buffer` was tried and reverted because it buffered gigabytes for huge row groups. ~25 s for wide parquet is inherent |
| DuckDB's own file readers were very slow in Pyodide (parquet 206 s, CSV 1015 s for a WHERE over 5 GB) | Single-threaded WASM, and DuckDB's readers don't suit it | pyarrow streams record batches and DuckDB queries the Arrow stream (`con.register` of a RecordBatchReader): parquet 43 s, CSV 90 s. DuckDB's own readers are kept as a fallback, e.g. for a query that reads `data` twice |
| The CSV filter heap reached 1.7 GB | pyarrow keeps ~35 blocks in flight, and blocks were 8 MB | 2 MB blocks brought it to 692 MB |
| SQL streamed every column | — | The query is tokenised to find the columns it mentions. `*` (except `count(*)`) or `COLUMNS(` means all columns. If a guessed column is "not found", the query is retried with all columns |
| Duplicate row-number columns appeared after a filter | A `file_row_number` column was added alongside our own | `EXCLUDE` the extra one |
| A CSV's date index disappeared when the user picked "None" | The index column was treated as a normal pickable column | Index columns are detected when the schema is read, then always loaded and shown locked |
| int64 values became float in stats | Columns with different dtypes were combined | Stats are computed per dtype group |
| A CSV written with its index wasn't detected | — | A "labelled matrix" rule: a blank first header cell plus a unique first column means that column is the index |
| The worker dropped the `index` parameter | A plumbing bug between parent, sandbox and worker | Fixed. When adding a request field, check all three hops |

Measured on 5 GB files with a 200 MB budget: CSV 7 s, JSON lines 19 s, JSON
29 s, parquet 6 s, h5 2 s. The peak heap stayed at 0.5–0.85 GB. The UI never
blocked for more than 170 ms.

### Plots

| Problem | Cause | Fix |
|---|---|---|
| The plot showed only the sample the table used | — | The plot sends every row. Numeric and date columns cross from the worker as transferred float64 buffers, not JSON. Dates are epoch ms on a `type:'date'` axis |
| SVG markers were unusably slow above ~30k points | DOM size | Above 30k points the plot draws a heatmap of point density (log counts, transparent where empty). Zooming redraws it, and once 30k or fewer points are in view it switches to markers |
| Zooming to markers sometimes did nothing | A resize relayout replaced the zoom event within the debounce window | Relayout events are merged across the debounce |
| A new file showed the previous file's plot and stats | State wasn't cleared | `setView(fresh)` purges Plotly, clears the zoom binding, stats and overview |
| The plot kept its old colours after a theme change | Plotly colours are fixed when it draws | `Plotly.relayout` with the new token colours, ignored by the zoom handler |

### UI

| Problem | Cause | Fix |
|---|---|---|
| The table didn't render | `setView` ran while the panel was still `hidden`, so its width measured as zero | Show the panel first, then render |
| Y-column chips collapsed | A checkbox `width:100%` rule | Replaced with dropdowns (the user preferred them anyway) |
| The table's last column stretched across the screen | `width:100%` on the table | The table is only as wide as its content |
| The index setting was unreachable after loading | It only existed before the load | The options dialog works for every format. h5/xlsx/json get a compact settings-only version |
| The options dialog showed the previous file's choices | `loadOpts` wasn't reset | Reset in `selectFile`. A new file starts with fresh choices |
| Load options were offered only after the data had loaded, which defeats the purpose | Original design | Parquet/CSV/JSONL always show a "Choose what to load" step after the file is picked, using only the schema. Before the first load it's inline; with data open it's a dialog, and Cancel keeps the current data |
| The chip area showed the text "nullnull" | `replaceChildren(a, null, null)` renders `null` as text | Pass only real nodes |
| Thousands of chips for very wide files | — | When every column is chosen and there are more than 60, show one summary line ("All 2,500 columns") |
| The footer hint was squashed on phones | — | It wraps to its own line at ≤640 px |
| Cancelling a slow load reloaded the cancelled file | Restart used the new file rather than the last loaded one | Keep a snapshot of the last successful load. Restart restores it, then reloads |
| There was no way to see which expressions produced the current `df` | History spans every file and every run | A separate Steps list (update / view / define), with "Copy as Python", which turns view expressions into `df = …` assignments. Reset clears both the steps and the namespace |
| Copy to clipboard failed on the LAN | `navigator.clipboard` needs a secure context, and `http://192.168…` isn't one | Fall back to an offscreen textarea plus `execCommand('copy')` |
| Escape bypasses a dialog's Cancel button | A `<dialog>` closed by Escape fires `cancel`, not your button's click | Handle `cancel` the same way as the Cancel button (restore the last load's choices) |

### Expressions

- Each run works on `df = STATE.df.copy(deep=False)`, with pandas
  copy-on-write. Static analysis of the AST decides whether the expression
  changed `df`: an assignment or delete rooted at `df`, `df = …`, a mutating
  method, or `inplace=True`. If it did, the copy becomes the new view. A new
  column then appears in the table, the pickers and the stats.
- A bare expression that returns a DataFrame or Series replaces the view. The
  original stays one click away with Reset.
- Expressions are never run from the URL, hash or shared links. There's a test
  for it.

### Testing

| Problem | Cause | Fix |
|---|---|---|
| Playwright's `wait_for_function("…")` failed | A string predicate runs through `eval`, which the app's CSP blocks | Poll with `page.evaluate("() => …")` in a Python loop |
| Playwright's bundled Chromium was missing | — | `channel="chrome"` (the system Chrome) |
| The 5 GB suite hung until it was killed (exit 143) | One test opened files itself and never clicked through the new load step | Shared `wait_setup()` and `load()` helpers in `conftest.py`. Every test goes through them |
| Tests depended on file order or attribute names | e.g. `df.count` is a method, not the column | Use `df['count']` and order-independent assertions |
| Prototyping in a scratch page failed | `importScripts` behaves differently outside the real sandbox | Prototype inside the real app |

Final state: 138 native tests, 69 browser tests and 14 tests on ~5 GB files,
all passing. The large files come from `tests/make_large.py`; they're
git-ignored and take ~38 GB.

## Advice for a rebuild

- Build the sandbox, CSP and test harness first, exactly as the brief says.
  Almost every surprise came from the CSP. A browser test that loads the real
  built `dist/` with the real headers catches these straight away.
- Serve with the production headers locally (`serve.py` reads
  `dist/headers.json`), and restart the server after changing them.
- Decide early how the data will be cut down (columns, filter, sample), and
  offer that before reading any data.
- Measure the peak WASM heap in the large-file tests, not just time. Memory is
  what kills the tab.
- Vendor anything that isn't in Pyodide's package list, and check its hash.
  micropip needs network access the CSP shouldn't allow.
