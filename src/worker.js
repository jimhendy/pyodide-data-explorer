/* Pyodide worker. sandbox.js creates it from a blob (so it shares the sandbox's
 * opaque origin and CSP) and sends {type: 'boot'} first. Requests are handled
 * strictly one at a time. Files arrive as File objects and are mounted with
 * WORKERFS, which reads byte ranges on demand: a 5 GB file is never copied into
 * WASM memory, only the slices Python asks for. */
'use strict';

let pyodide = null;
let api = null;
let mountSeq = 0;
let mountPoint = null;
const loadedPackages = new Set(['numpy', 'pandas']);

function post(msg, transfer) {
  self.postMessage(msg, transfer || []);
}

function progress(text) {
  post({ type: 'progress', text: String(text) });
}

async function boot({ indexURL, pySource }) {
  progress('Downloading Python runtime…');
  importScripts(indexURL + 'pyodide.js');
  pyodide = await self.loadPyodide({ indexURL, stdout: () => {}, stderr: () => {} });
  progress('Loading pandas…');
  await pyodide.loadPackage(['numpy', 'pandas'], { messageCallback: () => {} });
  pyodide.FS.writeFile('/home/pyodide/explorer.py', pySource);
  api = pyodide.pyimport('explorer');
  api.set_progress(progress);
  return JSON.parse(api.dispatch('ping', '{}')).result;
}

function mount(file) {
  const FS = pyodide.FS;
  if (mountPoint) {
    FS.unmount(mountPoint);
    FS.rmdir(mountPoint);
  }
  mountPoint = '/data' + ++mountSeq;
  FS.mkdirTree(mountPoint);
  const ext = (/\.[A-Za-z0-9]{1,8}$/.exec(file.name) || [''])[0].toLowerCase();
  const name = 'input' + ext;
  FS.mount(FS.filesystems.WORKERFS, { blobs: [{ name, data: file }] }, mountPoint);
  return mountPoint + '/' + name;
}

async function ensurePackages(pkgs) {
  const missing = pkgs.filter((p) => !loadedPackages.has(p));
  if (!missing.length) return;
  progress('Loading ' + missing.join(', ') + '…');
  await pyodide.loadPackage(missing, { messageCallback: () => {} });
  missing.forEach((p) => loadedPackages.add(p));
}

async function handle(type, payload) {
  let wheels = null;
  if (type === 'inspect' || type === 'load') {
    const file = payload.file;
    const path = mount(file);
    // DuckDB (~9 MB) is fetched only when a load needs it (Python decides)
    const options = type !== 'load' ? '' : payload.where || payload.sql ? 'filter' : payload.columns ? 'columns' : '';
    const pkgs = api.packages_for(path, file.name, options);
    await ensurePackages(pkgs.toJs());
    pkgs.destroy();
    if (payload.wheels) wheels = payload.wheels.map((w) => [w.name, new Uint8Array(w.data)]);
    payload = {
      path, name: file.name, key: payload.key ?? null, budget_mb: payload.budget_mb, index: payload.index ?? 'auto',
      columns: payload.columns ?? null, where: payload.where ?? null, sql: payload.sql ?? null,
    };
  }
  const pyWheels = wheels ? pyodide.toPy(wheels) : null;
  try {
    return JSON.parse(api.dispatch(type, JSON.stringify(payload), pyWheels));
  } finally {
    if (pyWheels) pyWheels.destroy();
  }
}

/** float64 arrays Python set aside for this response (plot data), copied out of WASM memory. */
function takeBuffers() {
  const list = api.take_buffers();
  const out = [];
  try {
    for (let i = 0; i < list.length; i++) {
      const arr = list.get(i);
      const view = arr.getBuffer('f64');
      out.push(view.data.slice());
      view.release();
      arr.destroy();
    }
  } finally {
    list.destroy();
  }
  return out;
}

function heapBytes() {
  try {
    return pyodide._module.HEAPU8.buffer.byteLength;
  } catch {
    return null;
  }
}

let queue = Promise.resolve();
self.onmessage = (e) => {
  const { id, type, payload } = e.data || {};
  queue = queue.then(async () => {
    try {
      if (type === 'boot') {
        post({ id, ok: true, result: await boot(payload) });
        return;
      }
      if (!api) throw new Error('Python is not ready');
      const r = await handle(type, payload || {});
      if (type === 'overall' && r.ok) r.result.wasm_heap_bytes = heapBytes();
      const buffers = takeBuffers();
      post({ id, ok: !!r.ok, result: r.result, error: r.error, detail: r.detail, buffers }, buffers.map((b) => b.buffer));
    } catch (err) {
      const msg = String((err && err.message) || err);
      post({ id, ok: false, error: msg.slice(0, 4000), fatal: /fatal error|memory access out of bounds|Aborted/i.test(msg) });
    }
  });
};
