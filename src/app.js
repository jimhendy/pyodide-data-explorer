/* Data Explorer UI. Talks to the Python sandbox (sandbox.html, opaque origin)
 * only through postMessage. All data from the sandbox is rendered with
 * textContent, never as HTML. Nothing here reads location/query/hash, so a
 * link can never cause code to run. */
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const WHEELS = ['openpyxl-3.1.5-py2.py3-none-any.whl', 'et_xmlfile-2.0.0-py3-none-any.whl'];
  const HISTORY_KEY = 'data-explorer:history';
  const nf = new Intl.NumberFormat();

  // ---------------------------------------------------------------- helpers
  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'text') node.textContent = v;
        else if (k === 'class') node.className = v;
        else if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
        else node.setAttribute(k, v === true ? '' : v);
      }
    }
    for (const c of [].concat(children || [])) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  const fmtInt = (n) => (n === null || n === undefined ? '–' : nf.format(n));
  function fmtBytes(n) {
    if (n === null || n === undefined) return '–';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return (i ? n.toFixed(n < 10 ? 2 : 1) : n) + ' ' + units[i];
  }
  const fmtPct = (f) => (f === null || f === undefined ? '–' : (f * 100).toFixed(f && f < 0.001 ? 3 : 1) + '%');

  // ---------------------------------------------------------------- bridge
  const frame = $('sandbox');
  const bridge = {
    nextId: 1,
    pending: new Map(),
    ready: false,
    readyWaiters: [],
  };

  function whenReady() {
    return bridge.ready ? Promise.resolve() : new Promise((r) => bridge.readyWaiters.push(r));
  }

  window.addEventListener('message', (e) => {
    if (e.source !== frame.contentWindow || e.origin !== 'null') return;
    const m = e.data;
    if (!m || typeof m !== 'object' || m.v !== 1) return;
    if (m.type === 'progress') {
      if (typeof m.text === 'string') setStatus('busy', m.text);
    } else if (m.type === 'status') {
      onSandboxStatus(m);
    } else if (Number.isInteger(m.id) && bridge.pending.has(m.id)) {
      const { resolve } = bridge.pending.get(m.id);
      bridge.pending.delete(m.id);
      const buffers = Array.isArray(m.buffers) && m.buffers.every((b) => b instanceof Float64Array) ? m.buffers : [];
      if (buffers.length && m.result) m.result = hydrate(m.result, buffers);
      resolve(m);
      updateBusy();
      if (m.fatal) {
        // Python can't continue (usually out of memory): start a fresh one and reload the file
        showLoadError('Python ran out of memory or crashed and has been restarted. Try a smaller memory budget.');
        restartPython();
      }
    }
  });

  /** Swap the worker's {__buf__: i} / {__grid__: i, w, h} markers for the transferred Float64Arrays. */
  function hydrate(obj, buffers) {
    if (Array.isArray(obj)) return obj.map((v) => hydrate(v, buffers));
    if (!obj || typeof obj !== 'object') return obj;
    if (Number.isInteger(obj.__buf__)) return buffers[obj.__buf__] || [];
    if (obj.__grid__ && Number.isInteger(obj.w) && Number.isInteger(obj.h)) {
      const flat = hydrate(obj.__grid__, buffers);
      return Array.from({ length: obj.h }, (_, r) => flat.subarray(r * obj.w, (r + 1) * obj.w));
    }
    const out = {};
    for (const [k, v] of Object.entries(obj)) out[k] = hydrate(v, buffers);
    return out;
  }

  function onSandboxStatus(m) {
    if (m.state === 'ready') {
      bridge.ready = true;
      const v = m.versions || {};
      setStatus('ready', `Ready · Python ${v.python || '?'} · pandas ${v.pandas || '?'}`);
      bridge.readyWaiters.splice(0).forEach((r) => r());
      if (state.reloadAfterRestart) {
        state.reloadAfterRestart = false;
        // back to the data that was open (not a new file or choice whose load was cancelled)
        restoreLoaded();
        loadFile(state.key, true, state.loadOpts);
      }
    } else if (m.state === 'booting') {
      bridge.ready = false;
      setStatus('booting', 'Starting Python… (first visit downloads ~30 MB, then it is cached)');
    } else if (m.state === 'error') {
      bridge.ready = false;
      setStatus('error', 'Python failed to start');
      showLoadError(String(m.text || 'Unknown error') + '\nCheck your connection and reload the page.');
    }
  }

  /** Send a request; resolves with {ok, result, error}. */
  async function request(type, payload, { transfer } = {}) {
    await whenReady();
    const id = bridge.nextId++;
    const p = new Promise((resolve) => bridge.pending.set(id, { resolve, type }));
    frame.contentWindow.postMessage({ v: 1, id, type, payload: payload || {} }, '*', transfer || []);
    updateBusy();
    return p;
  }

  function restartPython() {
    for (const [id, { resolve }] of bridge.pending) resolve({ id, ok: false, error: 'Cancelled', cancelled: true });
    bridge.pending.clear();
    bridge.ready = false;
    state.reloadAfterRestart = !!state.loaded;
    frame.contentWindow.postMessage({ v: 1, type: 'restart' }, '*');
    setStatus('booting', 'Restarting Python…');
    updateBusy();
  }

  // ---------------------------------------------------------------- status
  let busyTimer = null;
  function setStatus(kind, text) {
    $('status').dataset.state = kind;
    $('status-text').textContent = text;
    $('status-text').title = text;
  }
  function updateBusy() {
    const busy = bridge.pending.size > 0;
    document.body.classList.toggle('busy', busy);
    clearTimeout(busyTimer);
    if (busy) {
      busyTimer = setTimeout(() => ($('cancel').hidden = false), 600);
      if ($('status').dataset.state === 'ready') setStatus('busy', 'Working…');
    } else {
      $('cancel').hidden = true;
      if (bridge.ready) setStatus('ready', 'Ready');
    }
    for (const id of ['run', 'plot-draw', 'key-load', 'scan-btn']) $(id).disabled = busy;
  }

  // ---------------------------------------------------------------- state
  const state = {
    file: null,
    key: null,
    keys: null,
    info: null,
    view: null,
    tab: 'table',
    stale: { table: true, plot: true, columns: true, overview: true },
    table: { mode: 'head', n: 50, sort: null, ascending: true, seed: 0, colStart: 0, colCount: 50, colFilter: '' },
    stats: { start: 0, limit: 200, filter: '', sort: null, ascending: true },
    plotY: [],
    history: loadHistory(),
    reloadAfterRestart: false,
    wheels: null,
    schema: null, // columns of the chosen file (parquet / csv / jsonl), for the options dialog
    loadOpts: null, // {columns, where, sql} used for the current load
  };

  function loadHistory() {
    try {
      const h = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
      return Array.isArray(h) ? h.filter((x) => typeof x === 'string').slice(0, 50) : [];
    } catch {
      return [];
    }
  }
  function saveHistory() {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(state.history));
    } catch {
      /* storage unavailable: history lasts for this page only */
    }
  }

  function svgIcon(id) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + id);
    svg.append(use);
    return svg;
  }

  // ---------------------------------------------------------------- loading
  function defaultBudget() {
    const small = (navigator.deviceMemory && navigator.deviceMemory <= 4) || window.matchMedia('(max-width: 700px)').matches;
    return small ? 100 : 200;
  }
  $('budget').value = String(defaultBudget());

  function showLoadError(text) {
    const e = $('load-error');
    e.textContent = text;
    e.hidden = !text;
  }

  const isExcel = (name) => /\.xls[xm]$/i.test(name);

  async function wheelsFor(name) {
    if (!isExcel(name)) return undefined;
    if (!state.wheels) {
      setStatus('busy', 'Fetching Excel support…');
      state.wheels = await Promise.all(
        WHEELS.map(async (w) => {
          const r = await fetch('wheels/' + w);
          if (!r.ok) throw new Error('Could not fetch ' + w);
          return { name: w, data: await r.arrayBuffer() };
        }),
      );
    }
    // copies: the originals are kept for a restart
    return state.wheels.map((w) => ({ name: w.name, data: w.data.slice(0) }));
  }

  async function selectFile(file) {
    if (!file) return;
    showLoadError('');
    state.file = file;
    state.key = null;
    state.schema = null;
    state.loadOpts = null; // the previous file's columns / filter don't apply to this one
    $('key-picker').hidden = true;
    let wheels;
    try {
      wheels = await wheelsFor(file.name);
    } catch (err) {
      showLoadError(String(err.message || err));
      return;
    }
    setStatus('busy', `Opening ${file.name}…`);
    const r = await request('inspect', { file, wheels });
    if (!r.ok) {
      if (!r.cancelled) showLoadError(r.error || 'Could not open file');
      return;
    }
    const keys = r.result.keys;
    state.keys = keys;
    state.schema = r.result.schema ? Object.assign({ format: r.result.format }, r.result.schema) : null;
    if (state.schema) {
      // columns and rows can be chosen for these: offer that before reading anything
      openOptions();
      return;
    }
    if (keys && keys.length > 1) {
      const sel = $('key-select');
      sel.replaceChildren(
        ...keys.map((k) => el('option', { value: k.key, text: `${k.key} — ${k.kind}${k.rows != null ? ` · ${fmtInt(k.rows)} rows` : ''}` })),
      );
      $('key-label').textContent = r.result.format === 'xlsx' ? 'Sheet' : 'Dataset / key';
      $('key-picker').hidden = false;
      $('load-panel').hidden = false;
      sel.focus();
      return;
    }
    if (keys && keys.length === 0) {
      showLoadError('No datasets found in this file.');
      return;
    }
    await loadFile(keys && keys.length === 1 ? keys[0].key : null);
  }

  async function loadFile(key, isReload, opts) {
    showLoadError('');
    state.key = key;
    let wheels;
    try {
      wheels = await wheelsFor(state.file.name);
    } catch (err) {
      showLoadError(String(err.message || err));
      return;
    }
    const payload = { file: state.file, key, budget_mb: Number($('budget').value), index: $('index-mode').value, wheels };
    for (const k of ['columns', 'where', 'sql']) if (opts && opts[k]) payload[k] = opts[k];
    const r = await request('load', payload);
    if (!r.ok) {
      if (!r.cancelled) {
        if (opts) {
          openOptions(r.error || 'Could not load file', opts);
        } else {
          showLoadError(r.error || 'Could not load file');
          $('load-panel').hidden = false;
        }
      }
      return r;
    }
    state.loadOpts = opts || null;
    state.info = r.result.info;
    state.loaded = { file: state.file, key, keys: state.keys, schema: state.schema, loadOpts: state.loadOpts };
    $('key-picker').hidden = true;
    $('load-panel').hidden = true;
    $('setup-panel').hidden = true;
    $('file-info').hidden = false;
    $('explore').hidden = false;
    document.body.classList.add('has-data');
    if (!isReload) {
      $('expr-out').hidden = true;
      state.table.sort = null;
      state.plotY = [];
      // time series: a line against the dated index is the natural first plot
      $('plot-kind').value = r.result.view.index_dtype.startsWith('datetime') ? 'line' : 'scatter';
    }
    renderInfo();
    setView(r.result.view, true);
    return r;
  }

  function renderInfo() {
    const i = state.info;
    const sel = i.selection;
    $('file-name').textContent = i.name + (i.key ? `  ›  ${i.key}` : '');
    $('file-name').title = $('file-name').textContent;
    const parts = [i.format.toUpperCase(), fmtBytes(i.size)];
    if (i.rows_total != null) parts.push(i.rows_exact ? `${fmtInt(i.rows_total)} rows` : `~${fmtInt(i.rows_total)} rows (estimated)`);
    if (sel && sel.columns != null && sel.of) parts.push(`${fmtInt(sel.columns)} of ${fmtInt(sel.of)} columns`);
    else parts.push(`${fmtInt(i.cols)} columns`);
    if (sel && sel.sql) parts.push('SQL query');
    else if (sel && sel.where) parts.push(`where ${sel.where.replace(/\s+/g, ' ')}`);
    parts.push(`loaded in ${i.load_seconds}s`);
    $('file-meta').textContent = parts.join(' · ');
    $('file-meta').title = $('file-meta').textContent;
    $('options-btn').hidden = false;
    $('options-label').textContent = state.schema ? 'Columns & filter' : 'Load options';
    $('options-btn').classList.toggle('active', !!sel);
    $('sample-note').hidden = !i.sampled;
    if (i.sampled) {
      const of = i.filtered
        ? `${fmtInt(i.rows_matched)} ${sel && sel.sql ? 'result' : 'matching'} rows`
        : `${i.rows_exact ? '' : '~'}${fmtInt(i.rows_total)} rows`;
      $('sample-text').textContent =
        `Sampled: ${fmtInt(i.rows_loaded)} of ${of} loaded (${i.budget_mb} MB budget) — ${i.sample_method}. ` +
        'Tables, stats, plots and expressions all use this sample.';
      $('scan-btn').hidden = i.rows_exact || i.filtered || !['csv', 'jsonl'].includes(i.format);
    }
  }

  // ---------------------------------------------------------------- columns & filter dialog
  const OPT_LIST_LIMIT = 500; // list items rendered at once; search narrows the rest
  const opt = { chosen: new Set(), filter: '', anchor: null };
  const quoteIdent = (n) => '"' + String(n).replace(/"/g, '""') + '"';

  function optMode() {
    return document.querySelector('input[name="opt-rows"]:checked').value;
  }

  /** Show the options for state.file / state.schema, with ``opts`` (default: the current load's choices).
   * Before any data is open they appear inline in place of the open screen; afterwards in a dialog over the
   * data. Formats without a schema (h5, xlsx, json) get only the budget / index settings (dialog only). */
  function openOptions(error, opts) {
    if (!state.file) return;
    const inline = !document.body.classList.contains('has-data');
    const host = inline ? $('setup-panel') : $('options-dialog');
    const form = $('options-form');
    const showing = inline ? !$('setup-panel').hidden : $('options-dialog').open;
    if (form.parentElement !== host) host.append(form);
    if (!showing) prepareOptions(opts, inline);
    if (inline) {
      $('load-panel').hidden = true;
      $('setup-panel').hidden = false;
      if (!showing) $('opt-load').focus();
    } else if (!showing) {
      $('options-dialog').showModal();
    }
    $('opt-error').textContent = error || '';
    $('opt-error').hidden = !error;
  }

  function prepareOptions(opts, inline) {
    const sc = state.schema;
    const isNew = !state.loaded || state.loaded.file !== state.file;
    $('options-dialog').classList.toggle('compact', !sc);
    $('opt-cols').hidden = !sc;
    $('opt-rowsel').hidden = !sc;
    $('opt-unsupported').hidden = !!sc;
    $('opt-title').textContent = !sc ? 'Load options' : isNew ? 'Choose what to load' : 'Columns & filter';
    $('opt-rows-title').textContent = sc ? 'Rows' : 'Settings';
    $('opt-cancel').textContent = inline ? 'Choose another file' : 'Cancel';
    $('opt-budget').value = $('budget').value;
    $('opt-index').value = $('index-mode').value;
    if (!sc) {
      $('opt-file').textContent = `${state.file.name}${state.key ? '  ›  ' + state.key : ''} · ${fmtBytes(state.file.size)}`;
      $('opt-foot-hint').textContent = '';
      $('opt-load').disabled = false;
      $('opt-load').textContent = 'Reload';
      return;
    }
    const cur = opts || (isNew ? {} : state.loadOpts || {});
    const pickable = sc.columns.filter((c) => !c.index).map((c) => c.name);
    opt.chosen = new Set(cur.columns ? cur.columns : pickable);
    opt.filter = '';
    opt.anchor = null;
    $('opt-col-filter').value = '';
    const mode = cur.sql ? 'sql' : cur.where ? 'where' : 'all';
    document.querySelector(`input[name="opt-rows"][value="${mode}"]`).checked = true;
    $('opt-where').value = cur.where || '';
    $('opt-sql').value = cur.sql || 'SELECT *\nFROM data\nLIMIT 100000';
    const rows = sc.rows != null ? `${fmtInt(sc.rows)} rows · ` : '';
    $('opt-file').textContent = `${state.file.name} · ${fmtBytes(state.file.size)} · ${rows}${fmtInt(sc.total_cols)} columns`;
    $('opt-foot-hint').textContent = isNew ? 'Only what you choose is read into memory.' : '';
    renderOptColumns();
    updateOptMode();
  }

  function renderOptColumns() {
    const sc = state.schema;
    const q = opt.filter.toLowerCase();
    const matches = q ? sc.columns.filter((c) => c.name.toLowerCase().includes(q)) : sc.columns;
    const shown = matches.slice(0, OPT_LIST_LIMIT);
    opt.anchor = null;
    const items = shown.map((c, i) => {
      const box = el('input', { type: 'checkbox', value: c.name });
      box.checked = c.index || opt.chosen.has(c.name);
      box.disabled = !!c.index;
      box.addEventListener('click', (e) => {
        // shift-click sets every column between the last one clicked and this one
        const range = e.shiftKey && opt.anchor !== null
          ? shown.slice(Math.min(opt.anchor, i), Math.max(opt.anchor, i) + 1) : [c];
        for (const col of range) {
          if (col.index) continue;
          if (box.checked) opt.chosen.add(col.name);
          else opt.chosen.delete(col.name);
        }
        opt.anchor = i;
        if (range.length > 1) {
          for (const b of $('opt-col-list').querySelectorAll('input')) if (!b.disabled) b.checked = opt.chosen.has(b.value);
        }
        updateOptSummary();
      });
      return el('li', null, el('label', { title: c.name }, [
        box,
        el('span', { class: 'name', text: c.name }),
        c.index ? el('span', { class: 'badge', text: 'index' }) : null,
        el('span', { class: 'type', text: c.dtype }),
      ]));
    });
    $('opt-col-list').replaceChildren(...items);
    const more = matches.length - items.length;
    $('opt-col-more').textContent = more > 0 ? `${fmtInt(more)} more — search to narrow the list.` : matches.length ? '' : 'No columns match.';
    $('opt-col-all').textContent = q ? `All ${fmtInt(matches.length)}` : 'All';
    $('opt-col-none').textContent = q ? 'None of these' : 'None';
    $('opt-col-only').hidden = !q || !matches.length;
    updateOptSummary();
  }

  const CHIP_LIMIT = 60;
  function renderOptChips() {
    const sc = state.schema;
    const pickable = sc.columns.filter((c) => !c.index);
    const chosen = pickable.filter((c) => opt.chosen.has(c.name));
    const locked = sc.columns.filter((c) => c.index).map((c) =>
      el('span', { class: 'chip locked', title: `${c.name} is the index and is always loaded` }, el('span', { text: `${c.name} · index` })));
    if (chosen.length === pickable.length && pickable.length > CHIP_LIMIT) {
      $('opt-col-chips').replaceChildren(...locked,
        el('span', { class: 'all', text: `All ${fmtInt(pickable.length)} columns. Search and use "Only these", or None, to choose fewer.` }));
      return;
    }
    const chips = chosen.slice(0, CHIP_LIMIT).map((c) => el('button', {
      type: 'button', class: 'chip', title: `Remove ${c.name}`, 'aria-label': `Remove ${c.name}`,
      on: { click: () => { opt.chosen.delete(c.name); renderOptColumns(); } },
    }, [el('span', { text: c.name }), svgIcon('i-x')]));
    const more = chosen.length - chips.length;
    const extra = [];
    if (more > 0) extra.push(el('span', { class: 'more', text: `+${fmtInt(more)} more` }));
    if (!chosen.length) extra.push(el('span', { class: 'all', text: 'No columns selected: tick some below.' }));
    $('opt-col-chips').replaceChildren(...locked, ...chips, ...extra);
  }

  function optColumns() {
    // null = every column (no subset sent)
    const sc = state.schema;
    const pickable = sc.columns.filter((c) => !c.index);
    if (pickable.every((c) => opt.chosen.has(c.name))) return null;
    return pickable.filter((c) => opt.chosen.has(c.name)).map((c) => c.name);
  }

  function updateOptSummary() {
    const sc = state.schema;
    if (!sc) return;
    const total = sc.columns.filter((c) => !c.index).length;
    const n = sc.columns.filter((c) => !c.index && opt.chosen.has(c.name)).length;
    const sql = optMode() === 'sql';
    $('opt-col-count').textContent = sql ? 'set by your query' : `${fmtInt(n)} of ${fmtInt(total)} selected`;
    renderOptChips();
    $('opt-cols').classList.toggle('disabled', sql);
    const cols = optColumns();
    const where = $('opt-where').value.trim();
    let preview;
    if (sql) {
      preview = $('opt-sql').value.trim() || '(enter a query)';
    } else {
      const proj = cols === null ? '*' : cols.length > 12
        ? cols.slice(0, 12).map(quoteIdent).join(', ') + `, … (${fmtInt(cols.length - 12)} more)`
        : cols.map(quoteIdent).join(', ') || '(no columns)';
      preview = `SELECT ${proj} FROM data` + (optMode() === 'where' && where ? ` WHERE ${where}` : '');
    }
    $('opt-preview').textContent = preview;
    const duck = optMode() !== 'all' || (cols !== null && sc.format !== 'parquet');
    $('opt-engine').textContent = duck
      ? 'Runs in DuckDB, inside the sandbox (a one-off ~9 MB download). Results larger than the budget are randomly sampled.'
      : cols !== null ? 'Only the chosen columns are read from the file.' : '';
    const ok = sql ? !!$('opt-sql').value.trim() : n > 0 && !(optMode() === 'where' && !where);
    $('opt-load').disabled = !ok;
    $('opt-load').textContent = sql ? 'Run query' : cols === null ? 'Load' : `Load ${fmtInt(n)} column${n === 1 ? '' : 's'}`;
  }

  function updateOptMode() {
    const mode = optMode();
    $('opt-where-box').hidden = mode !== 'where';
    $('opt-sql-box').hidden = mode !== 'sql';
    updateOptSummary();
  }

  /** Cancel: inline, go back to the open screen; in the dialog, keep the data that is open (a newly
   * chosen file that was never loaded is forgotten). */
  function closeOptions() {
    if (!$('setup-panel').hidden) {
      $('setup-panel').hidden = true;
      $('load-panel').hidden = false;
      state.file = state.schema = null;
      return;
    }
    $('options-dialog').close();
    restoreLoaded();
  }

  function restoreLoaded() {
    if (state.loaded && state.file !== state.loaded.file) {
      Object.assign(state, { file: state.loaded.file, key: state.loaded.key, keys: state.loaded.keys,
        schema: state.loaded.schema, loadOpts: state.loaded.loadOpts });
    }
  }
  $('options-dialog').addEventListener('cancel', restoreLoaded); // Escape

  $('opt-col-filter').addEventListener('input', debounce((e) => {
    opt.filter = e.target.value.trim();
    renderOptColumns();
  }, 120));
  const setMatching = (on) => {
    const q = opt.filter.toLowerCase();
    for (const c of state.schema.columns) {
      if (c.index || (q && !c.name.toLowerCase().includes(q))) continue;
      if (on) opt.chosen.add(c.name);
      else opt.chosen.delete(c.name);
    }
    renderOptColumns();
  };
  $('opt-col-all').addEventListener('click', () => setMatching(true));
  $('opt-col-only').addEventListener('click', () => {
    opt.chosen.clear();
    setMatching(true);
  });
  $('opt-col-none').addEventListener('click', () => setMatching(false));
  for (const r of document.querySelectorAll('input[name="opt-rows"]')) r.addEventListener('change', updateOptMode);
  $('opt-where').addEventListener('input', updateOptSummary);
  $('opt-sql').addEventListener('input', updateOptSummary);
  for (const id of ['opt-where', 'opt-sql']) {
    $(id).addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        $('options-form').requestSubmit();
      }
    });
  }
  $('opt-col-filter').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') e.preventDefault(); // searching, not submitting
  });
  $('opt-close').addEventListener('click', closeOptions);
  $('opt-cancel').addEventListener('click', closeOptions);
  $('options-form').addEventListener('submit', (e) => {
    e.preventDefault();
    if ($('opt-load').disabled || document.body.classList.contains('busy')) return;
    const mode = state.schema ? optMode() : 'all';
    const opts = {};
    if (mode === 'sql') {
      opts.sql = $('opt-sql').value.trim();
    } else if (state.schema) {
      const cols = optColumns();
      if (cols) opts.columns = cols;
      if (mode === 'where' && $('opt-where').value.trim()) opts.where = $('opt-where').value.trim();
    }
    $('budget').value = $('opt-budget').value;
    $('index-mode').value = $('opt-index').value;
    // inline, the form stays up while loading (and shows any error); the dialog closes
    if ($('options-dialog').open) $('options-dialog').close();
    if (state.schema) loadFile(state.key, false, Object.keys(opts).length ? opts : null);
    else loadFile(state.key, false);
  });
  $('options-btn').addEventListener('click', () => openOptions());

  // ---------------------------------------------------------------- view
  function setView(view, fresh) {
    const prev = state.view;
    state.view = view;
    for (const k of Object.keys(state.stale)) state.stale[k] = true;
    renderSteps();
    const note = $('view-note');
    note.replaceChildren();
    const shape = `${fmtInt(view.rows)} rows × ${fmtInt(view.cols)} columns`;
    const index = `index: ${view.index_name} (${view.index_dtype})`;
    if (!view.is_original) {
      note.append(el('strong', { text: 'Showing an expression result' }), ` ${shape} · ${index} · Reset to go back to the loaded data`);
    } else {
      note.textContent = `${shape} · ${index}`;
    }
    note.title = note.textContent;
    // Column choices are kept by name, so adding or removing columns keeps the
    // plot and sort selections that still exist; the column windows restart
    // only when the columns are a different set.
    const nameAt = (v, i) => (i >= 0 && v && v.columns[i] ? v.columns[i].name : null);
    const remap = (i) => {
      if (i === -1 || i === null) return i;
      const name = nameAt(prev, i);
      if (name === null) return null;
      if (nameAt(view, i) === name) return i;
      const j = view.columns.findIndex((c) => c.name === name);
      return j >= 0 ? j : null;
    };
    if (fresh || !prev) {
      state.table.colStart = 0;
      state.stats.start = 0;
    } else {
      if (prev.columns[0]?.name !== view.columns[0]?.name) {
        state.table.colStart = 0;
        state.stats.start = 0;
      }
      if (state.table.sort !== null) state.table.sort = remap(state.table.sort);
      state.plotY = state.plotY.map(remap).filter((i) => i !== null);
      for (const id of ['plot-x', 'plot-color']) {
        const v = $(id).value;
        if (v !== '' && v !== '-1') {
          const j = remap(Number(v));
          $(id).dataset.keep = j === null ? '' : String(j);
        } else {
          $(id).dataset.keep = v;
        }
      }
    }
    populatePlotControls();
    if (fresh) {
      // A new file: don't leave the previous file's plot or stats on screen
      // while the new ones are computed.
      if (window.Plotly) Plotly.purge($('plot'));
      delete $('plot').dataset.zoomBound; // purge drops the relayout listener
      plotSpec = null;
      plotDynamic = false;
      $('plot-notes').textContent = '';
      $('col-table').replaceChildren();
      $('col-note').textContent = '';
      $('overview').replaceChildren();
    } else if (plotSpec) {
      plotSpec = null;
      plotDynamic = false;
      $('plot-notes').textContent = 'The data changed: press Draw plot to update the plot.';
    }
    renderTab();
  }

  function selectTab(tab) {
    state.tab = tab;
    for (const b of document.querySelectorAll('.tabs button')) {
      const on = b.dataset.tab === tab;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
      $('tab-' + b.dataset.tab).hidden = !on;
    }
    // the plot may have been drawn at another size (window resized while hidden)
    if (tab === 'plot' && window.Plotly && $('plot').data) Plotly.Plots.resize($('plot'));
    renderTab();
  }

  function renderTab() {
    if (!state.view || $('explore').hidden) return;
    const t = state.tab;
    if (t === 'plot') {
      ensurePlotly();
      return;
    }
    if (!state.stale[t]) return;
    state.stale[t] = false;
    if (t === 'table') refreshTable();
    else if (t === 'columns') refreshColumns();
    else if (t === 'overview') refreshOverview();
  }

  // ---------------------------------------------------------------- table tab
  async function refreshTable() {
    const o = state.table;
    $('resample').hidden = o.mode !== 'sample';
    const r = await request('table', {
      mode: o.mode, n: o.n, sort: o.sort, ascending: o.ascending, seed: o.seed,
      col_start: o.colStart, col_count: o.colCount, col_filter: o.colFilter,
    });
    if (!r.ok) {
      if (!r.cancelled) $('data-table').replaceChildren(el('caption', { class: 'error', text: r.error }));
      state.stale.table = true;
      return;
    }
    renderDataTable(r.result);
  }

  function sortHeader(label, dtype, pos) {
    const active = state.table.sort === pos;
    const arrow = active ? (state.table.ascending ? '▲' : '▼') : '';
    const btn = el('button', { type: 'button', class: 'sort', title: `Sort by ${label}` }, [
      el('span', null, [label, arrow ? el('span', { class: 'arrow', 'aria-hidden': 'true', text: arrow }) : null]),
      dtype ? el('span', { class: 'dtype', text: dtype }) : null,
    ]);
    btn.addEventListener('click', () => {
      if (state.table.sort === pos) {
        if (state.table.ascending) state.table.ascending = false;
        else state.table.sort = null;
      } else {
        state.table.sort = pos;
        state.table.ascending = true;
      }
      refreshTable();
    });
    const th = el('th', { scope: 'col', 'aria-sort': active ? (state.table.ascending ? 'ascending' : 'descending') : null }, btn);
    if (pos === -1) th.className = 'idx';
    return th;
  }

  function renderDataTable(t) {
    const numeric = t.dtypes.map((d) => /^(u?int|float|Int|UInt|Float)/.test(d));
    state.table.colStart = t.col_start;
    const head = el('tr', null, [sortHeader(t.index_name || '', '', -1), ...t.columns.map((c, i) => sortHeader(c, t.dtypes[i], t.col_positions[i]))]);
    const body = document.createDocumentFragment();
    t.rows.forEach((row, ri) => {
      const tr = el('tr', null, [el('td', { class: 'idx', text: t.index[ri] ?? 'null' })]);
      row.forEach((v, ci) => {
        const isNull = v === null;
        tr.append(el('td', { class: (isNull ? 'null' : '') + (numeric[ci] ? ' num' : ''), title: !isNull && v.length > 40 ? v : null, text: isNull ? 'null' : v }));
      });
      body.append(tr);
    });
    $('data-table').replaceChildren(el('thead', null, head), el('tbody', null, body));
    const notes = [`${fmtInt(t.rows.length)} of ${fmtInt(t.total_rows)} rows`];
    const s = state.table.sort;
    if (s !== null && !t.col_positions.includes(s)) {
      notes.push(`sorted by ${s === -1 ? 'the index' : state.view.columns[s]?.name ?? 'column ' + s} (${state.table.ascending ? 'ascending' : 'descending'})`);
    }
    $('table-note').textContent = notes.join(' · ') + ' · tap a header to sort';
    renderPager('col', t.col_start, t.columns.length, t.matched_cols, t.total_cols, t.col_count);
  }

  /** "Columns 51–100 of 5,000" plus prev/next state for a column window. */
  function renderPager(prefix, start, shown, matched, total, pageSize) {
    const range = $(prefix + '-range');
    const filtered = matched < total ? ` (${fmtInt(total)} total)` : '';
    range.textContent = matched === 0 ? 'No matching columns' : `Columns ${fmtInt(start + 1)}–${fmtInt(start + shown)} of ${fmtInt(matched)}${filtered}`;
    $(prefix + '-prev').disabled = start <= 0;
    $(prefix + '-next').disabled = start + pageSize >= matched;
    $(prefix + '-prev').closest('.col-window').hidden = total <= pageSize && !$(prefix === 'col' ? 'col-filter' : 'stats-filter').value;
  }

  function debounce(fn, ms) {
    let t = null;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  $('table-mode').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-mode]');
    if (!b) return;
    state.table.mode = b.dataset.mode;
    for (const x of $('table-mode').querySelectorAll('button')) x.setAttribute('aria-checked', String(x === b));
    refreshTable();
  });
  $('table-n').addEventListener('change', (e) => {
    state.table.n = Number(e.target.value);
    refreshTable();
  });
  $('resample').addEventListener('click', () => {
    state.table.seed = (state.table.seed + 1) % 2 ** 31;
    refreshTable();
  });
  $('col-prev').addEventListener('click', () => {
    state.table.colStart = Math.max(0, state.table.colStart - state.table.colCount);
    refreshTable();
  });
  $('col-next').addEventListener('click', () => {
    state.table.colStart += state.table.colCount;
    refreshTable();
  });
  $('col-filter').addEventListener('input', debounce((e) => {
    state.table.colFilter = e.target.value.trim();
    state.table.colStart = 0;
    refreshTable();
  }, 300));

  // ---------------------------------------------------------------- column stats tab
  async function refreshColumns() {
    const o = state.stats;
    const r = await request('colstats', { start: o.start, limit: o.limit, col_filter: o.filter, sort: o.sort, ascending: o.ascending });
    if (!r.ok) {
      if (!r.cancelled) $('col-table').replaceChildren(el('caption', { class: 'error', text: r.error }));
      state.stale.columns = true;
      return;
    }
    const cs = r.result;
    const cols = [
      ['dtype', 'dtype'], ['count', 'count'], ['nulls', 'nulls'], ['null %', 'null_frac'], ['unique', 'unique'],
      ['min', 'min'], ['max', 'max'], ['mean', 'mean'], ['std', 'std'], ['idxmin', 'idxmin'], ['idxmax', 'idxmax'],
    ];
    const fileCols = cs.has_file_stats ? [['file nulls', 'nulls'], ['file min', 'min'], ['file max', 'max']] : [];
    const statHeader = (label, key, cls) => {
      const active = o.sort === key;
      const btn = el('button', { type: 'button', class: 'sort', title: `Sort by ${label}` },
        el('span', null, [label, active ? el('span', { class: 'arrow', 'aria-hidden': 'true', text: o.ascending ? '▲' : '▼' }) : null]));
      btn.addEventListener('click', () => {
        if (o.sort !== key) {
          o.sort = key;
          o.ascending = !['nulls', 'null_frac', 'unique', 'std'].includes(key); // most interesting first
        } else if (o.ascending === !['nulls', 'null_frac', 'unique', 'std'].includes(key)) {
          o.ascending = !o.ascending;
        } else {
          o.sort = null;
        }
        o.start = 0;
        refreshColumns();
      });
      return el('th', { scope: 'col', class: cls, 'aria-sort': active ? (o.ascending ? 'ascending' : 'descending') : null }, btn);
    };
    const head = el('tr', null, [statHeader('column', 'name', 'idx'), ...cols.map(([l, k]) => statHeader(l, k)), ...fileCols.map(([l]) => el('th', { scope: 'col', text: l }))]);
    const body = document.createDocumentFragment();
    for (const c of cs.columns) {
      const tr = el('tr', null, el('td', { class: 'idx', text: c.name }));
      for (const [, k] of cols) {
        let v = c[k];
        if (k === 'null_frac') v = fmtPct(v);
        else if (['count', 'nulls', 'unique'].includes(k)) v = fmtInt(v);
        tr.append(el('td', { class: (v === undefined || v === null ? 'null' : '') + (k === 'dtype' ? '' : ' num'), text: v ?? '–' }));
      }
      for (const [, k] of fileCols) {
        const v = c.file ? c.file[k] : null;
        tr.append(el('td', { class: 'num' + (v === null || v === undefined ? ' null' : ''), text: k === 'nulls' && v != null ? fmtInt(v) : v ?? '–' }));
      }
      body.append(tr);
    }
    $('col-table').replaceChildren(el('thead', null, head), el('tbody', null, body));
    const sampled = state.info.sampled && state.view.is_original;
    $('col-note').textContent =
      `Over ${fmtInt(cs.rows)} rows${sampled ? ' (the loaded sample)' : ''}` +
      (cs.has_file_stats ? ' · “file” columns: exact whole-file values from parquet metadata' : '') + ' · tap a header to sort';
    o.start = cs.start;
    renderPager('stats', cs.start, cs.columns.length, cs.matched, cs.total_cols, cs.limit);
  }

  $('stats-prev').addEventListener('click', () => {
    state.stats.start = Math.max(0, state.stats.start - state.stats.limit);
    refreshColumns();
  });
  $('stats-next').addEventListener('click', () => {
    state.stats.start += state.stats.limit;
    refreshColumns();
  });
  $('stats-filter').addEventListener('input', debounce((e) => {
    state.stats.filter = e.target.value.trim();
    state.stats.start = 0;
    refreshColumns();
  }, 300));

  // ---------------------------------------------------------------- overview tab
  function card(k, v, s) {
    return el('div', { class: 'card' }, [el('div', { class: 'k', text: k }), el('div', { class: 'v', text: v }), s ? el('div', { class: 's', text: s }) : null]);
  }

  async function refreshOverview() {
    const r = await request('overall');
    if (!r.ok) {
      if (!r.cancelled) $('overview').replaceChildren(el('p', { class: 'error', text: r.error }));
      state.stale.overview = true;
      return;
    }
    const o = r.result;
    const i = o.info;
    const cards = el('div', { class: 'cards' }, [
      card('Rows', fmtInt(o.shape[0]), o.is_original && i.sampled ? `sample of ${i.rows_exact ? '' : '~'}${fmtInt(i.rows_total)}` : null),
      card('Columns', fmtInt(o.shape[1])),
      card('Memory', fmtBytes(o.memory_bytes), 'deep, in the browser'),
      card('Null cells', fmtInt(o.total_nulls), fmtPct(o.null_frac)),
      card('File size', fmtBytes(i.size), i.format.toUpperCase()),
      o.wasm_heap_bytes ? card('Python heap', fmtBytes(o.wasm_heap_bytes), 'WASM memory in use') : null,
    ]);
    const dtypes = el('table', { class: 'data' }, [
      el('thead', null, el('tr', null, [el('th', { text: 'dtype' }), el('th', { text: 'columns' })])),
      el('tbody', null, o.dtypes.map((d) => el('tr', null, [el('td', { text: d.dtype }), el('td', { class: 'num', text: fmtInt(d.count) })]))),
    ]);
    const kv = (pairs) => el('dl', { class: 'kv' }, pairs.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
    const ix = o.index;
    const indexInfo = kv([
      ['Name', ix.name ?? '(unnamed)'],
      ['Type', ix.is_dates ? `dates (${ix.dtype})` : ix.dtype],
      ['Range', ix.min !== undefined ? `${ix.min} → ${ix.max}` : '–'],
      ...(ix.is_dates ? [['Frequency', ix.freq ? `${ix.freq} (inferred)` : 'irregular']] : []),
      ['Order', ix.monotonic ? `${ix.monotonic}` : 'unsorted'],
      ['Unique', ix.unique ? 'yes' : 'no — has duplicates'],
    ]);
    const sel = i.selection;
    const source = kv([
      ['File', i.name],
      ['Key / sheet', i.key || '–'],
      ['Rows in file', i.rows_total == null ? 'unknown (not counted)' : `${fmtInt(i.rows_total)} ${i.rows_exact ? '(exact)' : '(estimated)'}`],
      ...(sel && sel.columns != null ? [['Columns', `${fmtInt(sel.columns)} of ${fmtInt(sel.of)} chosen`]] : []),
      ...(sel && sel.where ? [['Row filter', sel.where], ['Matching rows', fmtInt(i.rows_matched)]] : []),
      ...(sel && sel.sql ? [['SQL query', sel.sql], ['Result rows', fmtInt(i.rows_matched)]] : []),
      ['Rows loaded', fmtInt(i.rows_loaded)],
      ...(i.reader ? [['Read with', i.reader === 'pyarrow' ? 'pyarrow stream, filtered by DuckDB' : 'DuckDB']] : []),
      ['Sampling', i.sampled ? i.sample_method : 'none — whole file loaded'],
      ['Load time', `${i.load_seconds}s`],
      ['View', o.is_original ? 'original data' : 'expression result'],
    ]);
    $('overview').replaceChildren(
      cards,
      el('div', { class: 'overview-grid' }, [
        el('div', { class: 'overview-section' }, [el('h3', { text: 'Source' }), source]),
        el('div', { class: 'overview-section' }, [el('h3', { text: 'Index' }), indexInfo]),
        el('div', { class: 'overview-section' }, [el('h3', { text: 'Column types' }), el('div', { class: 'table-wrap' }, dtypes)]),
      ]),
    );
  }

  // ---------------------------------------------------------------- expression
  async function runExpression() {
    const expr = $('expr').value;
    if (!expr.trim() || !state.view) return;
    state.history = [expr, ...state.history.filter((h) => h !== expr)].slice(0, 50);
    saveHistory();
    renderHistory();
    const out = $('expr-out');
    const r = await request('eval', { expr });
    if (r.cancelled) {
      showExprOut('Cancelled. Python was restarted and the file is being reloaded.', true);
      return;
    }
    if (!r.ok) {
      showExprOut(r.error || 'Error', true);
      return;
    }
    const res = r.result;
    if (!res.ok) {
      showExprOut((res.stdout || '') + res.error, true);
      return;
    }
    if (res.kind === 'frame') {
      const msg = (res.stdout || '') + (res.message || '');
      out.hidden = !msg;
      if (msg) showExprOut(msg, false);
      setView(res.view);
    } else {
      const text = (res.stdout || '') + (res.text === null || res.text === undefined ? '(no value)' : res.text);
      showExprOut(text, false);
      // the data is unchanged, but the run may have defined names (a step)
      state.view.steps = res.view.steps;
      renderSteps();
    }
  }

  // ---------------------------------------------------------------- applied steps
  // What produced the current df: the load choices, then every run that
  // changed it, made a result the view, or defined names it later used.
  const STEP_LABELS = { update: 'changed df', view: 'result became the view', define: 'defined names' };

  function loadDescription() {
    const i = state.info;
    const sel = i.selection;
    const parts = [el('strong', { text: i.name + (i.key ? ` › ${i.key}` : '') })];
    if (sel && sel.sql) parts.push(' with SQL ', el('code', { text: sel.sql }));
    else {
      if (sel && sel.columns != null && sel.of) parts.push(` · ${fmtInt(sel.columns)} of ${fmtInt(sel.of)} columns`);
      if (sel && sel.where) parts.push(' · where ', el('code', { text: sel.where }));
    }
    parts.push(` · ${fmtInt(i.rows_loaded)} rows${i.sampled ? ' (sampled)' : ''} · index: ${i.index_mode}`);
    return parts;
  }

  function renderSteps() {
    const steps = state.view.steps || [];
    $('reset').disabled = state.view.is_original && !steps.length;
    $('steps-count').hidden = !steps.length;
    $('steps-count').textContent = String(steps.length);
    $('steps-copy').disabled = !steps.length;
    $('steps-empty').hidden = steps.length > 0;
    $('steps-loaded').replaceChildren('Loaded ', ...loadDescription());
    $('steps-list').replaceChildren(...steps.map((s) => el('li', { class: s.kind }, [
      el('pre', { text: s.expr }),
      el('div', { class: 'step-meta', text: `${STEP_LABELS[s.kind] || s.kind} · ${fmtInt(s.shape[0])} × ${fmtInt(s.shape[1])}` }),
    ])));
  }

  const READERS = {
    csv: (n) => `pd.read_csv(${JSON.stringify(n)})`,
    parquet: (n) => `pd.read_parquet(${JSON.stringify(n)})`,
    jsonl: (n) => `pd.read_json(${JSON.stringify(n)}, lines=True)`,
    json: (n) => `pd.read_json(${JSON.stringify(n)})`,
    xlsx: (n, k) => `pd.read_excel(${JSON.stringify(n)}${k ? `, sheet_name=${JSON.stringify(k)}` : ''})`,
    h5: (n, k) => `pd.read_hdf(${JSON.stringify(n)}${k ? `, key=${JSON.stringify(k)}` : ''})`,
  };

  function stepsScript() {
    const i = state.info;
    const sel = i.selection;
    const comment = (text) => text.split('\n').map((l) => `# ${l}`).join('\n');
    const lines = [`# Steps applied in Data Explorer to ${i.name}${i.key ? ` (${i.key})` : ''}`];
    if (sel && sel.sql) lines.push('# Loaded with SQL (the file is the table "data"):', comment(sel.sql));
    if (sel && !sel.sql && sel.columns != null) lines.push(`# Loaded ${sel.columns} of ${sel.of} columns`);
    if (sel && !sel.sql && sel.where) lines.push('# Loaded rows where:', comment(sel.where));
    if (i.sampled) lines.push(`# Sampled to ${i.rows_loaded} rows to fit the memory budget`);
    const reader = (READERS[i.format] || READERS.csv)(i.name, i.key);
    lines.push('import numpy as np', 'import pandas as pd', '',
      `df = ${reader}  # approximately: the app also picked the index (${i.index_mode}) and parsed dates`, 'df0 = df');
    state.view.steps.forEach((s, n) => lines.push('', `# ${n + 1}. ${STEP_LABELS[s.kind] || s.kind}`, s.code));
    return lines.join('\n') + '\n';
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // navigator.clipboard needs a secure context (not plain http on a LAN)
      const ta = el('textarea', { class: 'offscreen', readonly: true });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    }
  }

  $('steps-copy').addEventListener('click', async () => {
    const btn = $('steps-copy');
    const label = btn.lastChild;
    const done = await copyText(stepsScript());
    label.textContent = done ? 'Copied' : 'Copy failed';
    setTimeout(() => { label.textContent = 'Copy as Python'; }, 1500);
  });

  function showExprOut(text, isError) {
    const out = $('expr-out');
    out.textContent = text;
    out.classList.toggle('is-error', isError);
    out.setAttribute('role', isError ? 'alert' : 'status');
    out.hidden = false;
  }

  function renderHistory() {
    const list = $('history-list');
    list.replaceChildren(
      ...state.history.map((h) =>
        el('li', null, el('button', {
          type: 'button',
          text: h,
          on: { click: () => { $('expr').value = h; sizeExpr(); $('expr').focus(); $('history').open = false; } },
        })),
      ),
    );
    $('history-empty').hidden = state.history.length > 0;
  }

  function sizeExpr() {
    const lines = $('expr').value.split('\n').length;
    $('expr').rows = Math.min(10, Math.max(1, lines));
  }
  $('expr').addEventListener('input', sizeExpr);
  $('run').addEventListener('click', runExpression);
  $('expr').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      runExpression();
    }
  });
  $('reset').addEventListener('click', async () => {
    const r = await request('reset');
    if (r.ok) {
      $('expr-out').hidden = true;
      setView(r.result.view);
    }
  });

  // ---------------------------------------------------------------- plot tab
  let plotlyPromise = null;
  function ensurePlotly() {
    if (!plotlyPromise) {
      plotlyPromise = new Promise((resolve, reject) => {
        const s = el('script', { src: 'vendor/plotly-cartesian.min.js' });
        s.onload = () => resolve(window.Plotly);
        s.onerror = () => reject(new Error('Could not load the plotting library'));
        document.head.append(s);
      });
    }
    return plotlyPromise;
  }

  let optionTemplate = null; // one <option> per column, cloned into every column dropdown

  function columnOptions() {
    if (!optionTemplate) {
      optionTemplate = document.createDocumentFragment();
      state.view.columns.forEach((c, i) => optionTemplate.append(el('option', { value: String(i), text: `${c.name}  · ${c.dtype}` })));
    }
    return optionTemplate.cloneNode(true);
  }

  function populatePlotControls() {
    optionTemplate = null;
    for (const [id, none] of [['plot-x', '(none)'], ['plot-color', '(none)']]) {
      const sel = $(id);
      const prev = sel.dataset.keep !== undefined ? sel.dataset.keep : sel.value;
      delete sel.dataset.keep;
      sel.replaceChildren(el('option', { value: '', text: none }), el('option', { value: '-1', text: `${state.view.index_name} (index)` }), columnOptions());
      if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
    }
    state.plotY = state.plotY.filter((i) => i < state.view.cols);
    if (!state.plotY.length) {
      const firstNumeric = state.view.columns.findIndex((c) => c.numeric);
      if (firstNumeric >= 0) state.plotY.push(firstNumeric);
    }
    renderYSelects();
    updatePlotFields();
  }

  const MAX_Y = 20;
  function renderYSelects() {
    const rows = state.plotY.map((pos, k) => {
      const sel = el('select', { 'aria-label': `Y column ${k + 1}` });
      sel.append(columnOptions());
      sel.value = String(pos);
      sel.addEventListener('change', () => {
        state.plotY[k] = Number(sel.value);
      });
      const remove = el('button', { type: 'button', class: 'btn small', 'aria-label': `Remove Y column ${k + 1}`, title: 'Remove', text: '✕' });
      remove.addEventListener('click', () => {
        state.plotY.splice(k, 1);
        renderYSelects();
      });
      return el('div', { class: 'yrow' }, [sel, remove]);
    });
    $('plot-ys').replaceChildren(...rows);
    $('plot-y-add').disabled = state.plotY.length >= MAX_Y;
  }

  $('plot-y-add').addEventListener('click', () => {
    if (!state.view || state.plotY.length >= MAX_Y) return;
    const cols = state.view.columns;
    const used = new Set(state.plotY);
    let next = cols.findIndex((c, i) => c.numeric && !used.has(i));
    if (next < 0) next = cols.findIndex((c, i) => !used.has(i));
    if (next < 0) return;
    state.plotY.push(next);
    renderYSelects();
    $('plot-ys').lastElementChild.querySelector('select').focus();
  });

  function updatePlotFields() {
    const kind = $('plot-kind').value;
    $('plot-agg-field').hidden = kind !== 'bar';
    $('plot-bins-field').hidden = kind !== 'histogram';
    const hints = {
      scatter: 'Pick X and one or more Y columns.',
      line: 'Pick X (sorted automatically) and one or more Y columns.',
      histogram: 'Pick one or more Y columns.',
      bar: 'Pick a category X; Y values are aggregated per category (no Y = counts).',
      box: 'Pick numeric Y columns; optional X splits them into categories.',
    };
    $('plot-hint').textContent = hints[kind];
  }
  $('plot-kind').addEventListener('change', updatePlotFields);

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  let plotSpec = null; // last drawn spec, reused when a density plot is zoomed
  let plotDynamic = false;
  let ignoreRelayout = false;

  function plotSpecFromControls() {
    const optInt = (v) => (v === '' ? null : Number(v));
    return {
      kind: $('plot-kind').value,
      x: optInt($('plot-x').value),
      ys: [...new Set(state.plotY)].slice(0, MAX_Y),
      color: optInt($('plot-color').value),
      agg: $('plot-agg').value,
      bins: Math.max(2, Math.min(500, Math.round(Number($('plot-bins').value) || 40))),
    };
  }

  async function drawPlot(view) {
    const div = $('plot');
    const fromZoom = view !== undefined;
    if (!fromZoom || !plotSpec) plotSpec = plotSpecFromControls();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const spec = Object.assign({}, plotSpec, {
      px_w: Math.max(100, Math.min(2400, Math.round((div.clientWidth - 80) * dpr))),
      px_h: Math.max(100, Math.min(1400, Math.round((div.clientHeight - 80) * dpr))),
    });
    if (view) spec.view = view;
    const [Plotly, r] = await Promise.all([ensurePlotly().catch((e) => e), request('plot', spec)]);
    if (Plotly instanceof Error) {
      $('plot-notes').textContent = Plotly.message;
      return;
    }
    if (!r.ok) {
      if (!r.cancelled) $('plot-notes').textContent = r.error;
      return;
    }
    const res = r.result;
    const grid = cssVar('--border');
    const axis = (a) => Object.assign({ gridcolor: grid, zerolinecolor: grid, linecolor: grid, automargin: true }, a || {});
    const layout = Object.assign({}, res.layout, {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: cssVar('--text'), family: 'system-ui, sans-serif', size: 12 },
      margin: { l: 56, r: 16, t: res.layout.showlegend ? 48 : 24, b: 48 },
      legend: { orientation: 'h', x: 0, y: 1.02, yanchor: 'bottom' },
      xaxis: axis(res.layout.xaxis),
      yaxis: axis(res.layout.yaxis),
    });
    ignoreRelayout = true;
    try {
      await Plotly.react(div, res.traces, layout, { responsive: true, displaylogo: false, modeBarButtonsToRemove: ['sendDataToCloud', 'lasso2d', 'select2d'] });
    } finally {
      ignoreRelayout = false;
    }
    plotDynamic = !!res.dynamic;
    if (!div.dataset.zoomBound) {
      div.dataset.zoomBound = '1';
      div.on('plotly_relayout', onPlotRelayout);
    }
    const notes = res.notes.slice();
    if (state.info.sampled && state.view.is_original) notes.push(`Plotted from all ${fmtInt(res.rows)} loaded rows (the file is sampled)`);
    $('plot-notes').textContent = notes.join(' · ');
  }

  // ------------------------------------------------------------------ theme
  // System -> light -> dark. theme.js applies the saved choice before paint;
  // the plot takes its colours from the CSS tokens, so recolour it in place
  // (relayout keeps the zoom).
  const THEME_KEY = 'data-explorer:theme';
  const THEME_LABELS = { system: 'match system', light: 'light', dark: 'dark' };
  function applyTheme(mode) {
    if (mode === 'system') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    const btn = $('theme-btn');
    btn.dataset.mode = mode;
    const label = `Theme: ${THEME_LABELS[mode]}`;
    btn.title = label;
    btn.setAttribute('aria-label', label);
    recolourPlot();
  }
  function recolourPlot() {
    const div = $('plot');
    if (!window.Plotly || !div.data) return;
    const grid = cssVar('--border');
    const update = { 'font.color': cssVar('--text') };
    for (const ax of ['xaxis', 'yaxis']) {
      update[`${ax}.gridcolor`] = grid;
      update[`${ax}.zerolinecolor`] = grid;
      update[`${ax}.linecolor`] = grid;
    }
    ignoreRelayout = true;
    Promise.resolve(Plotly.relayout(div, update)).finally(() => { ignoreRelayout = false; });
  }
  $('theme-btn').addEventListener('click', () => {
    const order = ['system', 'light', 'dark'];
    const mode = order[(order.indexOf($('theme-btn').dataset.mode) + 1) % order.length];
    try {
      if (mode === 'system') localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, mode);
    } catch {
      /* storage unavailable: applies to this visit only */
    }
    applyTheme(mode);
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', recolourPlot);
  applyTheme(document.documentElement.dataset.theme || 'system');

  // Density plots are drawn for the visible region, so redraw after a zoom or
  // pan; a double-click (autorange) goes back to the full extent.
  // Events are merged until the debounce fires: a resize right after a zoom
  // (e.g. the notes line under the plot changing height) must not swallow it.
  let relayoutPending = {};
  function onPlotRelayout(ev) {
    if (!plotDynamic || ignoreRelayout || !ev) return;
    const isRange = (k) => k.startsWith('xaxis.range') || k.startsWith('yaxis.range');
    const isAuto = (k) => k.endsWith('autorange');
    const keys = Object.keys(ev);
    // a newer zoom replaces an older reset and vice versa
    if (keys.some(isRange)) for (const k of Object.keys(relayoutPending)) if (isAuto(k)) delete relayoutPending[k];
    if (keys.some(isAuto)) for (const k of Object.keys(relayoutPending)) if (isRange(k)) delete relayoutPending[k];
    Object.assign(relayoutPending, ev);
    flushRelayout();
  }
  const flushRelayout = debounce(() => {
    const ev = relayoutPending;
    relayoutPending = {};
    if (!plotDynamic) return;
    const keys = Object.keys(ev);
    if (keys.some((k) => k.endsWith('autorange'))) {
      drawPlot(null);
      return;
    }
    if (!keys.some((k) => k.startsWith('xaxis.range') || k.startsWith('yaxis.range'))) return;
    const fl = $('plot')._fullLayout;
    const [x0, x1] = fl.xaxis.range.map(fl.xaxis.r2l);
    const [y0, y1] = fl.yaxis.range.map(fl.yaxis.r2l);
    if ([x0, x1, y0, y1].every(Number.isFinite)) drawPlot({ x0, x1, y0, y1 });
  }, 250);

  $('plot-draw').addEventListener('click', () => drawPlot());

  // ---------------------------------------------------------------- wiring
  for (const b of document.querySelectorAll('.tabs button')) b.addEventListener('click', () => selectTab(b.dataset.tab));
  document.querySelector('.tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const tabs = [...document.querySelectorAll('.tabs button')];
    const i = tabs.findIndex((t) => t.dataset.tab === state.tab);
    const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    selectTab(next.dataset.tab);
    next.focus();
  });

  $('file-input').addEventListener('change', (e) => {
    selectFile(e.target.files[0]);
    e.target.value = '';
  });
  $('key-load').addEventListener('click', () => loadFile($('key-select').value));
  $('change-file').addEventListener('click', () => $('file-input').click());
  // close the history and steps popovers on an outside click
  document.addEventListener('click', (e) => {
    for (const id of ['history', 'steps']) if ($(id).open && !$(id).contains(e.target)) $(id).open = false;
  });
  $('scan-btn').addEventListener('click', async () => {
    const r = await request('scan');
    if (r.ok) {
      state.info = r.result.info;
      renderInfo();
      state.stale.overview = true;
    } else if (!r.cancelled) {
      $('sample-text').textContent = r.error;
    }
  });
  $('budget').addEventListener('change', () => {
    if (state.file && state.info) loadFile(state.key, true, state.loadOpts);
  });
  $('index-mode').addEventListener('change', () => {
    if (state.file && state.info) loadFile(state.key, false, state.loadOpts);
  });
  $('cancel').addEventListener('click', restartPython);

  // drag and drop anywhere on the page
  let dragDepth = 0;
  window.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragDepth++;
    $('dropzone').classList.add('drag');
  });
  window.addEventListener('dragleave', () => {
    if (--dragDepth <= 0) $('dropzone').classList.remove('drag');
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    $('dropzone').classList.remove('drag');
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if (f) selectFile(f);
  });

  renderHistory();
  setStatus('booting', 'Starting Python… (first visit downloads ~30 MB, then it is cached)');
})();
