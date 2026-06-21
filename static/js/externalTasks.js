/**
 * externalTasks.js — External Tasks Hub panel (Todoist + template engine).
 * Side-panel UI: source management, task list, conflict resolution.
 */

const API = window.location.origin;
let _open = false;
let _tasks = [];
let _sources = [];
let _filterSourceId = null;
let _filterStatus = 'open';
let _syncing = false;

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function _get(path) {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(`GET ${path} → ${r.status}`);
  return r.json();
}

async function _post(path, body) {
  const r = await fetch(`${API}${path}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`POST ${path} → ${r.status}`);
  return r.json();
}

async function _patch(path, body) {
  const r = await fetch(`${API}${path}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`PATCH ${path} → ${r.status}`);
  return r.json();
}

async function _del(path) {
  const r = await fetch(`${API}${path}`, {method: 'DELETE'});
  if (!r.ok && r.status !== 204) throw new Error(`DELETE ${path} → ${r.status}`);
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function _loadAll() {
  const [tasksData, sourcesData] = await Promise.all([
    _get('/api/external-tasks'),
    _get('/api/external-tasks/sources'),
  ]);
  _tasks = tasksData.tasks || [];
  _sources = sourcesData.sources || [];
}

// ---------------------------------------------------------------------------
// Panel open / close
// ---------------------------------------------------------------------------

export function openPanel() {
  if (_open) { _renderTasks(); return; }
  _open = true;

  document.getElementById('tool-ext-tasks-btn')?.classList.add('active');

  const pane = document.createElement('div');
  pane.id = 'ext-tasks-pane';
  pane.className = 'tool-panel slide-in-right';
  pane.innerHTML = _buildPanelHTML();
  document.body.appendChild(pane);

  _bindEvents(pane);
  _loadAll().then(() => _renderAll(pane)).catch(e => _showError(pane, e.message));
}

export function closePanel() {
  if (!_open) return;
  _open = false;
  document.getElementById('tool-ext-tasks-btn')?.classList.remove('active');
  document.getElementById('ext-tasks-pane')?.remove();
}

export function isPanelOpen() { return _open; }

// ---------------------------------------------------------------------------
// HTML builders
// ---------------------------------------------------------------------------

function _buildPanelHTML() {
  return `
<div class="tool-panel-header">
  <span class="tool-panel-title">External Tasks</span>
  <div style="display:flex;gap:6px;align-items:center;">
    <button class="icon-btn" id="ext-tasks-sync-btn" title="Sync now">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>
        <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
      </svg>
    </button>
    <button class="icon-btn" id="ext-tasks-sources-btn" title="Manage sources">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M4.93 4.93a10 10 0 0 0 0 14.14"/>
      </svg>
    </button>
    <button class="icon-btn" id="ext-tasks-close-btn" title="Close">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
      </svg>
    </button>
  </div>
</div>
<div id="ext-tasks-filters" class="ext-tasks-filters"></div>
<div id="ext-tasks-list" class="ext-tasks-list">
  <div class="ext-tasks-loading">Loading...</div>
</div>
<div id="ext-tasks-sources-drawer" class="ext-tasks-sources-drawer" style="display:none;"></div>
`;
}

function _renderAll(pane) {
  _renderFilters(pane);
  _renderTasks(pane);
}

function _renderFilters(pane) {
  const el = (pane || document).getElementById('ext-tasks-filters');
  if (!el) return;

  const sourceOpts = _sources.map(s =>
    `<option value="${_esc(s.id)}" ${_filterSourceId === s.id ? 'selected' : ''}>${_esc(s.label || s.type)}</option>`
  ).join('');

  el.innerHTML = `
<select id="ext-tasks-filter-source" class="ext-tasks-filter-select">
  <option value="">All sources</option>
  ${sourceOpts}
</select>
<select id="ext-tasks-filter-status" class="ext-tasks-filter-select">
  <option value="open" ${_filterStatus === 'open' ? 'selected' : ''}>Open</option>
  <option value="completed" ${_filterStatus === 'completed' ? 'selected' : ''}>Completed</option>
  <option value="" ${_filterStatus === '' ? 'selected' : ''}>All</option>
</select>`;

  el.querySelector('#ext-tasks-filter-source').addEventListener('change', e => {
    _filterSourceId = e.target.value || null;
    _renderTasks();
  });
  el.querySelector('#ext-tasks-filter-status').addEventListener('change', e => {
    _filterStatus = e.target.value;
    _renderTasks();
  });
}

function _renderTasks(pane) {
  const el = (pane || document).getElementById('ext-tasks-list');
  if (!el) return;

  let tasks = _tasks.filter(t => !t.deleted_at);
  if (_filterSourceId) tasks = tasks.filter(t => t.source_id === _filterSourceId);
  if (_filterStatus) tasks = tasks.filter(t => t.status === _filterStatus);

  if (!tasks.length) {
    el.innerHTML = `<div class="ext-tasks-empty">${_sources.length ? 'No tasks.' : 'Add a source to get started.'}</div>`;
    return;
  }

  el.innerHTML = tasks.map(t => _taskCard(t)).join('');

  el.querySelectorAll('.ext-task-complete-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      await _post(`/api/external-tasks/${id}/complete`, {});
      await _reload();
    });
  });

  el.querySelectorAll('.ext-task-conflict-btn').forEach(btn => {
    btn.addEventListener('click', () => _showConflictDialog(btn.dataset.id));
  });
}

function _taskCard(t) {
  const conflictBadge = t.sync_conflict
    ? `<button class="ext-task-conflict-btn badge-conflict" data-id="${_esc(t.id)}" title="Resolve conflict">conflict</button>`
    : '';
  const pendingBadge = t.sync_pending
    ? `<span class="badge-pending">${_esc(t.sync_pending)}</span>`
    : '';
  const due = t.due_date ? `<span class="ext-task-due">${_esc(t.due_date)}</span>` : '';
  const done = t.status === 'completed';

  return `
<div class="ext-task-card ${done ? 'ext-task-done' : ''} ${t.sync_conflict ? 'ext-task-conflict' : ''}">
  <button class="ext-task-complete-btn icon-btn" data-id="${_esc(t.id)}" title="${done ? 'Completed' : 'Mark complete'}" ${done ? 'disabled' : ''}>
    <svg width="14" height="14" viewBox="0 0 24 24" fill="${done ? 'currentColor' : 'none'}" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="10"/>
      ${done ? '<polyline points="9 12 11 14 15 10"/>' : ''}
    </svg>
  </button>
  <div class="ext-task-body">
    <div class="ext-task-title">${_esc(t.title)}</div>
    <div class="ext-task-meta">${due}${conflictBadge}${pendingBadge}</div>
  </div>
</div>`;
}

// ---------------------------------------------------------------------------
// Sources drawer
// ---------------------------------------------------------------------------

function _renderSourcesDrawer(pane) {
  const el = (pane || document).getElementById('ext-tasks-sources-drawer');
  if (!el) return;

  const rows = _sources.map(s => `
<div class="ext-source-row">
  <span class="ext-source-label">${_esc(s.label || s.type)}</span>
  <span class="ext-source-type badge-type">${_esc(s.type)}</span>
  <button class="icon-btn ext-source-delete-btn" data-id="${_esc(s.id)}" title="Remove source">
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/>
      <path d="M10 11v6"/><path d="M14 11v6"/>
      <path d="M9 6V4h6v2"/>
    </svg>
  </button>
</div>`).join('') || '<div class="ext-tasks-empty">No sources configured.</div>';

  el.innerHTML = `
<div class="ext-sources-header">
  <span>Sources</span>
  <button class="btn-small" id="ext-source-add-btn">+ Add</button>
</div>
${rows}
<div id="ext-source-add-form" style="display:none;" class="ext-source-add-form">
  <select id="ext-source-type-sel" class="ext-tasks-filter-select">
    <option value="todoist">Todoist</option>
  </select>
  <input id="ext-source-token-inp" class="ext-source-input" type="password" placeholder="API token" autocomplete="off"/>
  <input id="ext-source-proj-inp" class="ext-source-input" type="text" placeholder="Project ID (optional)"/>
  <div style="display:flex;gap:6px;">
    <button class="btn-small" id="ext-source-save-btn">Save</button>
    <button class="btn-small btn-ghost" id="ext-source-cancel-btn">Cancel</button>
  </div>
  <div id="ext-source-err" class="ext-source-err" style="display:none;"></div>
</div>`;

  el.querySelectorAll('.ext-source-delete-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('Remove this source? Tasks will remain locally.')) return;
      await _del(`/api/external-tasks/sources/${btn.dataset.id}`);
      await _reload();
      _renderSourcesDrawer();
    });
  });

  el.querySelector('#ext-source-add-btn').addEventListener('click', () => {
    el.querySelector('#ext-source-add-form').style.display = 'flex';
    el.querySelector('#ext-source-add-form').style.flexDirection = 'column';
    el.querySelector('#ext-source-add-form').style.gap = '6px';
  });

  el.querySelector('#ext-source-cancel-btn').addEventListener('click', () => {
    el.querySelector('#ext-source-add-form').style.display = 'none';
  });

  el.querySelector('#ext-source-save-btn').addEventListener('click', async () => {
    const type = el.querySelector('#ext-source-type-sel').value;
    const token = el.querySelector('#ext-source-token-inp').value.trim();
    const proj = el.querySelector('#ext-source-proj-inp').value.trim();
    const errEl = el.querySelector('#ext-source-err');
    if (!token) { _showInlineError(errEl, 'API token is required'); return; }
    try {
      await _post('/api/external-tasks/sources', {type, api_token: token, project_id: proj});
      el.querySelector('#ext-source-add-form').style.display = 'none';
      await _reload();
      _renderSourcesDrawer();
      _renderFilters();
    } catch (e) {
      _showInlineError(errEl, e.message);
    }
  });
}

// ---------------------------------------------------------------------------
// Conflict resolution dialog
// ---------------------------------------------------------------------------

function _showConflictDialog(taskId) {
  const existing = document.getElementById('ext-conflict-dialog');
  if (existing) existing.remove();

  const task = _tasks.find(t => t.id === taskId);
  if (!task) return;

  const dlg = document.createElement('div');
  dlg.id = 'ext-conflict-dialog';
  dlg.className = 'ext-conflict-overlay';
  dlg.innerHTML = `
<div class="ext-conflict-box">
  <div class="ext-conflict-title">Sync Conflict</div>
  <div class="ext-conflict-desc">
    <b>${_esc(task.title)}</b> was modified both locally and remotely.
    Which version should win?
  </div>
  <div style="display:flex;gap:8px;margin-top:12px;">
    <button class="btn-small" id="ext-conflict-local">Keep local</button>
    <button class="btn-small btn-ghost" id="ext-conflict-remote">Use remote</button>
    <button class="btn-small btn-ghost" id="ext-conflict-cancel">Cancel</button>
  </div>
</div>`;
  document.body.appendChild(dlg);

  dlg.querySelector('#ext-conflict-cancel').addEventListener('click', () => dlg.remove());
  dlg.querySelector('#ext-conflict-local').addEventListener('click', async () => {
    dlg.remove();
    await _post(`/api/external-tasks/${taskId}/resolve-conflict`, {keep: 'local'});
    await _reload();
  });
  dlg.querySelector('#ext-conflict-remote').addEventListener('click', async () => {
    dlg.remove();
    await _post(`/api/external-tasks/${taskId}/resolve-conflict`, {keep: 'remote'});
    await _reload();
  });
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

function _bindEvents(pane) {
  pane.querySelector('#ext-tasks-close-btn').addEventListener('click', closePanel);

  pane.querySelector('#ext-tasks-sync-btn').addEventListener('click', async () => {
    if (_syncing) return;
    _syncing = true;
    const btn = pane.querySelector('#ext-tasks-sync-btn');
    btn.classList.add('spinning');
    try {
      await _post('/api/external-tasks/sync', {});
      await _reload();
    } catch (e) {
      _showError(pane, e.message);
    } finally {
      _syncing = false;
      btn.classList.remove('spinning');
    }
  });

  pane.querySelector('#ext-tasks-sources-btn').addEventListener('click', () => {
    const drawer = pane.querySelector('#ext-tasks-sources-drawer');
    const visible = drawer.style.display !== 'none';
    drawer.style.display = visible ? 'none' : 'block';
    if (!visible) _renderSourcesDrawer(pane);
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function _reload() {
  await _loadAll();
  _renderTasks();
}

function _esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _showError(pane, msg) {
  const el = (pane || document).getElementById('ext-tasks-list');
  if (el) el.innerHTML = `<div class="ext-tasks-error">${_esc(msg)}</div>`;
}

function _showInlineError(el, msg) {
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
}

// ---------------------------------------------------------------------------
// CSS injection (scoped — no new color values, reuses CSS vars)
// ---------------------------------------------------------------------------

(function _injectStyles() {
  if (document.getElementById('ext-tasks-styles')) return;
  const style = document.createElement('style');
  style.id = 'ext-tasks-styles';
  style.textContent = `
#ext-tasks-pane { display:flex; flex-direction:column; height:100%; overflow:hidden; }
.ext-tasks-filters { display:flex; gap:6px; padding:8px 12px; border-bottom:1px solid var(--border); flex-shrink:0; }
.ext-tasks-filter-select { flex:1; background:var(--card); color:var(--fg); border:1px solid var(--border); border-radius:4px; padding:3px 6px; font-family:inherit; font-size:11px; }
.ext-tasks-list { flex:1; overflow-y:auto; padding:8px 12px; display:flex; flex-direction:column; gap:4px; }
.ext-tasks-loading, .ext-tasks-empty, .ext-tasks-error { color:var(--fg); opacity:0.5; font-size:12px; padding:16px 0; text-align:center; }
.ext-tasks-error { color:var(--red); opacity:1; }
.ext-task-card { display:flex; align-items:flex-start; gap:8px; padding:7px 6px; border-radius:5px; border:1px solid var(--border); background:var(--card); }
.ext-task-card:hover { border-color:var(--fg); }
.ext-task-card.ext-task-done { opacity:0.45; }
.ext-task-card.ext-task-conflict { border-color:var(--red); }
.ext-task-body { flex:1; min-width:0; }
.ext-task-title { font-size:12px; line-height:1.4; word-break:break-word; }
.ext-task-done .ext-task-title { text-decoration:line-through; }
.ext-task-meta { display:flex; align-items:center; gap:5px; margin-top:3px; flex-wrap:wrap; }
.ext-task-due { font-size:10px; opacity:0.55; }
.badge-conflict { font-size:9px; padding:1px 5px; border-radius:3px; background:var(--red); color:#fff; border:none; cursor:pointer; }
.badge-pending { font-size:9px; padding:1px 5px; border-radius:3px; background:var(--border); color:var(--fg); opacity:0.7; }
.ext-tasks-sources-drawer { border-top:1px solid var(--border); padding:10px 12px; flex-shrink:0; max-height:40%; overflow-y:auto; }
.ext-sources-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; font-size:11px; font-weight:600; opacity:0.7; text-transform:uppercase; letter-spacing:.05em; }
.ext-source-row { display:flex; align-items:center; gap:6px; padding:5px 0; border-bottom:1px solid var(--border); }
.ext-source-label { flex:1; font-size:12px; }
.ext-source-type, .badge-type { font-size:9px; padding:1px 5px; border-radius:3px; background:var(--border); color:var(--fg); opacity:0.7; }
.ext-source-add-form { margin-top:8px; }
.ext-source-input { width:100%; background:var(--card); color:var(--fg); border:1px solid var(--border); border-radius:4px; padding:4px 7px; font-family:inherit; font-size:11px; box-sizing:border-box; }
.ext-source-err { color:var(--red); font-size:11px; }
.btn-small { padding:3px 10px; border-radius:4px; border:1px solid var(--border); background:var(--card); color:var(--fg); font-family:inherit; font-size:11px; cursor:pointer; }
.btn-small:hover { background:var(--fg); color:var(--bg); }
.btn-ghost { background:transparent; }
.ext-conflict-overlay { position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:9999; display:flex; align-items:center; justify-content:center; }
.ext-conflict-box { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:20px; max-width:340px; width:90%; }
.ext-conflict-title { font-size:13px; font-weight:600; margin-bottom:8px; }
.ext-conflict-desc { font-size:12px; line-height:1.5; opacity:0.8; }
@keyframes spin { to { transform:rotate(360deg); } }
.spinning svg { animation:spin .8s linear infinite; }
`;
  document.head.appendChild(style);
})();

// ---------------------------------------------------------------------------
// Default export
// ---------------------------------------------------------------------------

export default { openPanel, closePanel, isPanelOpen };
export { openPanel as openExternalTasks, closePanel as closeExternalTasks };
