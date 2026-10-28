/* ushi - Beautify prompts page: the project's own copies of the Beautify prompts.
 * Saved to projects/<project>/prompts/beautify/<name>.txt; a prompt without its own copy uses
 * the built-in default. {name} is a placeholder the app fills in on every run. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(method, url, body) {
  const res = await fetch(url, {
    method, headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || `${res.status} ${res.statusText}`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}
function toast(text, level = 'info', ms = 4200) {
  const el = document.createElement('div');
  el.className = `toast ${level}`;
  el.innerHTML = `<span>${esc(text)}</span>`;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, ms);
}
const fail = e => toast(e.message || String(e), 'error', 7000);

// Theme: same switch and storage as the main window
function applyTheme(t) { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
try { applyTheme(localStorage.getItem('vr-theme')); } catch (_) { /* */ }
$('#themeBtn').onclick = () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  const t = dark ? 'light' : 'dark';
  applyTheme(t);
  try { localStorage.setItem('vr-theme', t); } catch (_) { /* */ }
};

const project = new URLSearchParams(location.search).get('project') || '';
const st = { prompts: [], sel: 'main', drafts: {} };   // drafts: name → unsaved text
const cur = () => st.prompts.find(p => p.name === st.sel) || st.prompts[0];
const textOf = p => (p.name in st.drafts ? st.drafts[p.name] : p.text);
const dirty = p => p.name in st.drafts && st.drafts[p.name] !== p.text;
const usedIn = text => new Set([...text.matchAll(/\{(\w+)\}/g)].map(m => m[1]));

function renderNav() {
  $('#nav').innerHTML = st.prompts.map(p => `<button class="p-item ${p.name === st.sel ? 'sel' : ''}" data-name="${p.name}">
      <span class="p-name">${esc(p.title)}${dirty(p) ? '<span class="p-tag dirty">unsaved</span>' : p.custom ? '<span class="p-tag">project</span>' : ''}</span>
      <span class="p-help">${esc(p.help)}</span></button>`).join('')
    + `<div class="p-nav-foot">Changes apply to Beautify runs of <b>${esc(project)}</b> only. They are saved in the project folder:
       <code>prompts/beautify/</code>. Marked <span class="p-tag">project</span>: this project has its own version.</div>`;
}

// The text area is built once per prompt (so typing keeps the cursor); the rest follows the text.
function renderEdit() {
  const p = cur();
  if (!p) return;
  $('#edit').innerHTML = `<div id="pMeta" style="display:contents"></div>
    <textarea class="p-text" id="pText" spellcheck="false"></textarea>
    <div class="p-note" id="pNote"></div>`;
  const ta = $('#pText');
  ta.value = textOf(p);
  ta.oninput = () => { st.drafts[p.name] = ta.value; renderNav(); renderMeta(); };
  renderMeta();
}

function renderMeta() {
  const p = cur();
  const text = textOf(p);
  const used = usedIn(text);
  const known = Object.keys(p.placeholders);
  const missing = known.filter(k => !used.has(k) && p.default.includes(`{${k}}`));
  const unknown = [...used].filter(k => !known.includes(k));
  $('#pMeta').innerHTML = `
    <div class="p-head">
      <div><h2>${esc(p.title)}</h2><p>${esc(p.help)}</p></div>
      <div class="spacer"></div>
      <div class="p-actions">
        <button class="ghost-btn small" id="pReset" ${text === p.default ? 'disabled' : ''} title="Put the built-in prompt back (saved when you press Save)">Reset to default</button>
        <button class="ghost-btn small" id="pRevert" ${dirty(p) ? '' : 'disabled'} title="Throw away the unsaved changes">Revert</button>
        <button class="primary-btn" id="pSave" ${dirty(p) ? '' : 'disabled'} title="⌘S">Save</button>
      </div>
    </div>
    ${known.length ? `<div class="p-ph"><span class="p-ph-label">Placeholders</span>${known.map(k =>
      `<button class="p-chip ${missing.includes(k) ? 'missing' : ''}" data-ph="${k}" title="${esc(p.placeholders[k])}${missing.includes(k) ? ' - not in the prompt; click to insert' : ' - click to insert'}">{${k}}</button>`).join('')}</div>` : ''}
    ${missing.length ? `<div class="p-warn">Not used: ${missing.map(k => `<b>{${esc(k)}}</b>`).join(', ')}. The model won't get that part of the input.</div>` : ''}
    ${unknown.length ? `<div class="p-warn">Unknown: ${unknown.map(k => `<b>{${esc(k)}}</b>`).join(', ')}. They are sent as they are, not filled in.</div>` : ''}`;
  $('#pNote').innerHTML = `${p.custom ? 'This project uses its own version of this prompt.' : 'This project uses the built-in prompt.'}
    Text in <code>{braces}</code> that isn't a placeholder above is sent as is.`;
  const setText = t => { const ta = $('#pText'); ta.value = t; st.drafts[p.name] = t; renderNav(); renderMeta(); };
  $('#pSave').onclick = save;
  $('#pRevert').onclick = () => { delete st.drafts[p.name]; setText(p.text); delete st.drafts[p.name]; renderNav(); renderMeta(); };
  $('#pReset').onclick = () => setText(p.default);
  $('#pMeta').querySelectorAll('[data-ph]').forEach(b => b.onclick = () => {
    const t = $('#pText');
    t.focus();
    t.setRangeText(`{${b.dataset.ph}}`, t.selectionStart, t.selectionEnd, 'end');
    t.dispatchEvent(new Event('input'));
  });
}

function renderAll() { renderNav(); renderEdit(); }

async function save() {
  const p = cur();
  if (!dirty(p)) return;
  try {
    const d = await api('PUT', `/api/projects/${encodeURIComponent(project)}/prompts`, { [p.name]: st.drafts[p.name] });
    st.prompts = d.prompts;
    delete st.drafts[p.name];
    renderNav(); renderMeta();
    toast('Saved. The next Beautify run of this project uses it.', 'ok');
  } catch (e) { fail(e); }
}

$('#nav').onclick = e => {
  const b = e.target.closest('[data-name]');
  if (!b) return;
  st.sel = b.dataset.name;
  renderAll();
};
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); save(); }
});
window.addEventListener('beforeunload', e => {
  if (st.prompts.some(dirty)) { e.preventDefault(); e.returnValue = ''; }
});

(async () => {
  $('#projSub').textContent = project ? `project ${project}` : '';
  if (!project) { $('#edit').innerHTML = '<div class="empty"><h3>No project</h3><p>Open this page from the Beautify menu.</p></div>'; return; }
  try {
    const d = await api('GET', `/api/projects/${encodeURIComponent(project)}/prompts`);
    st.prompts = d.prompts;
    renderAll();
  } catch (e) { fail(e); }
})();
