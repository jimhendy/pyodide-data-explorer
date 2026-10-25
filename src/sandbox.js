/* Sandbox bridge. Runs in sandbox.html, which is framed with
 * sandbox="allow-scripts" (and served with CSP `sandbox allow-scripts`), so it
 * has an opaque origin: no access to the app's DOM, storage or cookies.
 *
 * build.py prepends: PYODIDE_INDEX, WORKER_SOURCE, PY_SOURCE.
 *
 * Messages from the parent are shape-checked against SCHEMA before being
 * forwarded to the Pyodide worker; anything else is dropped. */
'use strict';

(() => {
  const isInt = (v) => Number.isInteger(v);
  const intIn = (lo, hi) => (v) => isInt(v) && v >= lo && v <= hi;
  const numIn = (lo, hi) => (v) => typeof v === 'number' && Number.isFinite(v) && v >= lo && v <= hi;
  const optional = (check) => (v) => v === undefined || v === null || check(v);
  const oneOf = (...xs) => (v) => xs.includes(v);
  const str = (max) => (v) => typeof v === 'string' && v.length <= max;
  const isFile = (v) => v instanceof Blob && typeof v.name === 'string' && v.name.length <= 1024;
  const wheels = (v) =>
    Array.isArray(v) &&
    v.length <= 4 &&
    v.every((w) => w && typeof w === 'object' && /^[\w.+-]{1,120}\.whl$/.test(w.name) && w.data instanceof ArrayBuffer && w.data.byteLength < 16e6);
  const column = intIn(-1, 1e6);

  const SCHEMA = {
    ping: {},
    inspect: { file: isFile, wheels: optional(wheels) },
    load: {
      file: isFile,
      key: optional(str(1024)),
      budget_mb: numIn(1, 1024),
      index: optional(oneOf('auto', 'first', 'none')),
      wheels: optional(wheels),
      columns: optional((v) => Array.isArray(v) && v.length >= 1 && v.length <= 50000 && v.every(str(1024))),
      where: optional(str(20000)),
      sql: optional(str(50000)),
    },
    table: {
      mode: oneOf('head', 'tail', 'sample'),
      n: intIn(1, 1000),
      sort: optional(column),
      ascending: (v) => typeof v === 'boolean',
      seed: optional(intIn(0, 2 ** 31)),
      col_start: optional(intIn(0, 1e7)),
      col_count: optional(intIn(1, 200)),
      col_filter: optional(str(200)),
    },
    colstats: {
      start: optional(intIn(0, 1e7)),
      limit: optional(intIn(1, 1000)),
      col_filter: optional(str(200)),
      sort: optional(oneOf('pos', 'name', 'dtype', 'count', 'nulls', 'null_frac', 'unique', 'min', 'max', 'mean', 'std', 'idxmin', 'idxmax')),
      ascending: optional((v) => typeof v === 'boolean'),
    },
    overall: {},
    eval: { expr: str(20000) },
    reset: {},
    scan: {},
    plot: {
      kind: oneOf('line', 'scatter', 'histogram', 'bar', 'box'),
      x: optional(column),
      ys: (v) => Array.isArray(v) && v.length <= 20 && v.every(column),
      color: optional(column),
      agg: oneOf('sum', 'mean', 'median', 'min', 'max', 'count'),
      bins: intIn(2, 500),
      view: optional((v) => typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 4 &&
        ['x0', 'x1', 'y0', 'y1'].every((k) => typeof v[k] === 'number' && Number.isFinite(v[k]))),
      px_w: optional(intIn(100, 4000)),
      px_h: optional(intIn(100, 3000)),
    },
  };
  const REQUIRED = { inspect: ['file'], load: ['file', 'budget_mb'], eval: ['expr'], plot: ['kind', 'ys'] };

  function validRequest(m) {
    if (!m || typeof m !== 'object' || m.v !== 1) return false;
    if (m.type === 'restart') return true;
    const schema = Object.prototype.hasOwnProperty.call(SCHEMA, m.type) ? SCHEMA[m.type] : null;
    if (!schema || !isInt(m.id) || m.id <= 0) return false;
    const p = m.payload === undefined ? {} : m.payload;
    if (!p || typeof p !== 'object' || Array.isArray(p)) return false;
    for (const k of Object.keys(p)) {
      if (!Object.prototype.hasOwnProperty.call(schema, k) || !schema[k](p[k])) return false;
    }
    return (REQUIRED[m.type] || []).every((k) => p[k] !== undefined);
  }

  // The parent is the only legitimate embedder (sandbox.html sends
  // `frame-ancestors 'self'`), and our opaque origin can't name it, so '*'.
  const toParent = (msg, transfer) => window.parent.postMessage(Object.assign({ v: 1 }, msg), '*', transfer || []);
  const isBuffers = (b) => Array.isArray(b) && b.every((x) => x instanceof Float64Array);

  let worker = null;
  let workerURL = null;
  const pending = new Set();

  function startWorker() {
    if (worker) worker.terminate();
    for (const id of pending) toParent({ id, ok: false, error: 'Cancelled', cancelled: true });
    pending.clear();
    workerURL = workerURL || URL.createObjectURL(new Blob([WORKER_SOURCE], { type: 'text/javascript' }));
    worker = new Worker(workerURL);
    worker.onmessage = onWorkerMessage;
    worker.onerror = (e) => toParent({ type: 'status', state: 'error', text: 'Python worker crashed: ' + (e.message || 'unknown error') });
    toParent({ type: 'status', state: 'booting' });
    worker.postMessage({ id: 0, type: 'boot', payload: { indexURL: PYODIDE_INDEX, pySource: PY_SOURCE } });
  }

  function onWorkerMessage(e) {
    const m = e.data;
    if (!m || typeof m !== 'object') return;
    if (m.type === 'progress') {
      toParent({ type: 'progress', text: String(m.text).slice(0, 500) });
    } else if (m.id === 0) {
      toParent(m.ok ? { type: 'status', state: 'ready', versions: m.result } : { type: 'status', state: 'error', text: String(m.error) });
    } else if (pending.delete(m.id)) {
      const buffers = isBuffers(m.buffers) ? m.buffers : [];
      toParent({ id: m.id, ok: !!m.ok, result: m.result, error: m.error, detail: m.detail, fatal: !!m.fatal, buffers },
        buffers.map((b) => b.buffer));
    }
  }

  window.addEventListener('message', (e) => {
    if (e.source !== window.parent || !validRequest(e.data)) return;
    const m = e.data;
    if (m.type === 'restart') {
      startWorker();
      return;
    }
    pending.add(m.id);
    worker.postMessage({ id: m.id, type: m.type, payload: m.payload || {} });
  });

  startWorker();
})();
