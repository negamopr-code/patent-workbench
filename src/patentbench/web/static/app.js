/* Patent Workbench — vanilla JS SPA. State: active tab in URL hash, prefs in localStorage. */
'use strict';

const $ = id => document.getElementById(id);
const api = async (path, opts = {}) => {
  let res;
  try {
    res = await fetch(path, {
      headers: opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
      ...opts,
    });
  } catch {
    // connection dropped — e.g. the server restarted or a long job's worker was
    // killed. Return an error instead of throwing, so callers still clear their
    // spinner (a stuck "running…" forever is worse than a visible failure).
    return { error: 'connection lost — the server may have restarted or the job '
                   + 'was interrupted. Please retry.' };
  }
  let data;
  try { data = await res.json(); } catch { data = {}; }
  if (!res.ok && !data.error) {
    // surface FastAPI's validation detail instead of a bare "HTTP 422"
    const d = data.detail;
    data.error = Array.isArray(d) ? d.map(x => x.msg || JSON.stringify(x)).join('; ')
               : (typeof d === 'string' ? d : `HTTP ${res.status}`);
  }
  return data;
};

let tabs = [];
let activeTab = null;
let docsPoll = null;
let bmPoll = null;
let ratePoll = null;
let readPoll = null;
let readWasRunning = false;
let docSelection = new Set();   // candidate ids picked for a scoped deep compare
let currentBm = null;           // last-rendered benchmark (for weighted feature ranking)
let skillsMeta = { skills: [], models: [], default_model: '' };
let nbState = { notebooks: [], chosen: null, sources: [], selected: new Set() };
// id → title for EVERY NotebookLM notebook, so a candidate's per-doc badge can name
// the exact notebook it lives in (incl. rollover siblings, not just the connected one).
// Loaded lazily (server-cached) and re-rendered into the docs when it arrives.
let nbTitleById = {};
async function loadNbTitles() {
  try {
    const res = await api('/api/notebooks');
    if (res && res.notebooks) {
      nbTitleById = {};
      for (const n of res.notebooks) nbTitleById[n.id] = n.title;
      if (lastDocs.length) renderDocs(lastDocs);   // backfill the badges now that titles are known
    }
  } catch (_) { /* notebooks may be unavailable; badge falls back to the short id */ }
}
const nbTitle = id => nbTitleById[id] || (id ? id.slice(0, 8) + '…' : '');
// When opening the notebook modal to add SPECIFIC documents (the per-row 📓➕),
// addPrefill holds {ids:Set, label} so the picker pre-checks exactly those and
// shows a "choose a destination" hint. null = normal open (pre-check from the
// candidate-list selection, benchmark on).
let addPrefill = null;
let consolidateIds = [];   // doc ids the consolidate modal will copy (resolved on open)
let lessonDefaultText = '';

/* ---------- reading / OCR model (shared by the benchmark + candidates panes) ---------- */
const READ_SELECT_IDS = ['bm-read-model', 'cand-read-model'];
const readSelects = () => READ_SELECT_IDS.map($).filter(Boolean);
function readModelValue() {
  const s = readSelects()[0];
  return s ? s.value : (skillsMeta.default_read_model || 'claude-haiku-4-5');
}
function setReadModel(v) { for (const s of readSelects()) s.value = v; }
// Reading-model strength: LOWER index in the server's MODELS list = stronger.
// Unknown / not-yet-read → weakest, so an upgrade re-surfaces it as "still to read".
function modelRank(m) {
  const models = skillsMeta.models || [];
  const i = models.indexOf(m);
  return i < 0 ? models.length : i;
}

/* ---------- health ---------- */
async function loadHealth() {
  const h = await api('/api/health');
  const el = $('health');
  el.innerHTML = '';
  for (const [name, icon] of [['claude', '🤖'], ['nlm', '📓'], ['lessons', '🧠']]) {
    const ok = h[name] && h[name].available;
    const chip = document.createElement('span');
    chip.className = 'chip ' + (ok ? 'ok' : 'err');
    chip.textContent = `${icon} ${name}`;
    chip.title = ok ? `${name}: available` : (h[name] ? h[name].reason : 'unknown');
    el.appendChild(chip);
  }
}

/* ---------- tabs ---------- */
async function loadTabs() {
  tabs = (await api('/api/tabs')).tabs || [];
  renderTabs();
  const wanted = parseInt(location.hash.slice(1), 10);
  const found = tabs.find(t => t.id === wanted) || tabs[0];
  if (found) selectTab(found.id); else showEmpty();
}

function renderTabs() {
  const wrap = $('tabs');
  wrap.innerHTML = '';
  for (const t of tabs) {
    const el = document.createElement('div');
    el.className = 'tab' + (activeTab === t.id ? ' active' : '');
    el.dataset.tabId = t.id;
    el.title = 'Click to open · ✎ or double-click to rename';
    const name = document.createElement('span');
    name.className = 'tab-name'; name.textContent = t.name;
    el.appendChild(name);
    const ren = document.createElement('span');
    ren.className = 'ren'; ren.textContent = '✎'; ren.title = 'Rename tab';
    ren.onclick = e => { e.stopPropagation(); startRename(t); };
    el.appendChild(ren);
    const x = document.createElement('span');
    x.className = 'x'; x.textContent = '×'; x.title = 'Delete tab';
    x.onclick = async e => {
      e.stopPropagation();
      if (!confirm(`Delete tab "${t.name}" with all its documents and chat history?`)) return;
      await api(`/api/tabs/${t.id}`, { method: 'DELETE' });
      if (activeTab === t.id) activeTab = null;
      loadTabs();
    };
    el.appendChild(x);
    el.onclick = () => selectTab(t.id);
    el.ondblclick = e => { e.preventDefault(); startRename(t); };
    wrap.appendChild(el);
  }
}

// Re-query the LIVE tab element by id: selectTab()→renderTabs() may have rebuilt the
// DOM between the click that fired this and now, so a captured node would be stale.
function startRename(t) {
  const tabEl = $('tabs').querySelector(`.tab[data-tab-id="${t.id}"]`);
  if (!tabEl) return;
  const nameEl = tabEl.querySelector('.tab-name');
  if (!nameEl) return;                       // already editing this tab
  const input = document.createElement('input');
  input.className = 'tab-rename'; input.value = t.name;
  tabEl.replaceChild(input, nameEl);
  input.onclick = e => e.stopPropagation();  // clicking the field shouldn't re-select the tab
  input.focus(); input.select();
  let done = false;
  const commit = async () => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    if (name && name !== t.name) await api(`/api/tabs/${t.id}`, { method: 'PATCH', body: JSON.stringify({ name }) });
    loadTabs();
  };
  input.onblur = commit;
  input.onkeydown = e => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { done = true; loadTabs(); }
  };
}

$('tab-add').onclick = async () => {
  const name = prompt('Tab name (project name):', `Project ${tabs.length + 1}`);
  if (name === null) return;
  const t = await api('/api/tabs', { method: 'POST', body: JSON.stringify({ name: name || 'Untitled' }) });
  // Point the hash at the new tab BEFORE reloading so loadTabs() selects it directly.
  // (Calling loadTabs() — which auto-selects — and selectTab() together raced two
  //  concurrent /state fetches, letting the old tab's content paint into the new one.)
  activeTab = null;
  if (t.id) location.hash = t.id;
  await loadTabs();
};

function showEmpty() {
  $('main').classList.add('hidden');
  $('empty').classList.remove('hidden');
}

// Clear the transient, non-persisted UI that belongs to the tab we are leaving
// (open OCR candidate list, status lines, draft inputs) so a freshly-selected
// project never shows leftovers from the previous one.
function resetTransientUI() {
  clearTimeout(bmPoll);
  clearTimeout(docsPoll);
  clearTimeout(ratePoll);
  clearTimeout(readPoll);
  readWasRunning = false;
  $('candidates').classList.add('hidden');
  $('cand-list').innerHTML = '';
  $('upload-status').textContent = '';
  $('bm-status').textContent = '';
  $('rate-status').textContent = '';
  $('read-status').textContent = '';
  for (const id of ['q', 'in-text', 'bm-text']) { const el = $(id); if (el) el.value = ''; }
  updateDocSelChip();   // docSelection was reset by selectTab → hides chip/hint/deep-bar
}

async function selectTab(id) {
  if (activeTab !== id) docSelection = new Set();
  activeTab = id;
  location.hash = id;
  $('main').classList.remove('hidden');
  $('empty').classList.add('hidden');
  renderTabs();
  loadPrefs();
  resetTransientUI();
  const st = await api(`/api/tabs/${id}/state`);
  if (activeTab !== id) return;   // a newer selectTab() won the race — don't clobber its render
  if (st.error) { alert(st.error); return; }
  renderBenchmark(st.benchmark);
  renderDocs(st.documents || []);
  renderChat(st.messages || []);
  renderNbChip(st.notebook);
  loadNbTitles();                 // fill in per-doc "in which notebook" badges (non-blocking)
  scheduleDocsPoll(st.documents || []);
  pollRate();                     // resume showing progress if an NLM rating job is in flight
  pollRead();                     // resume showing progress if a Claude deep-read is in flight
  attachPipeline();               // re-attach / offer ▶️ Resume if a pipeline job is in flight
}

/* ---------- benchmark (reference document) ---------- */
function renderBenchmark(bm) {
  clearTimeout(bmPoll);
  currentBm = bm;
  const card = $('bm-card');
  const setup = $('bm-setup');
  if (!bm) {
    card.classList.add('hidden');
    setup.classList.remove('hidden');
    $('bm-status').textContent = '';
    return;
  }
  setup.classList.add('hidden');
  card.classList.remove('hidden');
  card.innerHTML = '';

  const row = document.createElement('div');
  row.className = 'doc-row';
  const name = document.createElement('span');
  name.className = 'num';
  name.textContent = bm.source === 'features'
    ? `🧩 ${bm.title || 'Feature combination'}`
    : (bm.number || `📷 ${(bm.files || []).length} uploaded file(s)`);
  row.appendChild(name);
  const st = document.createElement('span');
  st.className = 'status ' + (bm.status === 'ready' ? 'fetched' : bm.status);
  st.textContent = bm.status === 'pending' && bm.progress
    ? `transcribing ${bm.progress}` : bm.status;
  if (bm.error) st.title = bm.error;
  row.appendChild(st);
  if (bm.status === 'ready') {
    const view = document.createElement('button');
    view.className = 'btn small'; view.textContent = '👁 view';
    view.title = 'Show the stored content of the benchmark';
    view.onclick = async () => {
      const full = await api(`/api/tabs/${activeTab}/benchmark/full`);
      openViewer(full.number || full.title || 'Benchmark', full);
    };
    row.appendChild(view);
  }
  if (bm.source === 'features') {
    const edit = document.createElement('button');
    edit.className = 'btn small'; edit.textContent = '✏️ Edit features';
    edit.title = 'Add / remove / re-weight the target features and re-save';
    edit.onclick = () => openFeatureEditor(bm);
    row.appendChild(edit);
  }
  const del = document.createElement('button');
  del.className = 'btn small del'; del.textContent = '🗑';
  del.title = 'Remove benchmark';
  del.onclick = async () => {
    if (!confirm('Remove the benchmark document (uploaded files are deleted)?')) return;
    await api(`/api/tabs/${activeTab}/benchmark`, { method: 'DELETE' });
    renderBenchmark(null);
  };
  row.appendChild(del);
  card.appendChild(row);

  if (bm.title) {
    const t = document.createElement('div');
    t.className = 'title'; t.textContent = bm.title;
    card.appendChild(t);
  }
  if ((bm.features || []).length) {
    const fl = document.createElement('div');
    fl.className = 'bm-feat-list';
    fl.title = 'Weighted target features. Weight = how decisive that feature is in the ranking.';
    for (const f of bm.features) {
      const chip = document.createElement('span');
      chip.className = 'chip feat-chip';
      chip.textContent = `${f.name} ·${'★'.repeat(f.weight)}`;
      fl.appendChild(chip);
    }
    card.appendChild(fl);
  }
  // Always-available "add a feature" window: APPENDS one weighted feature without
  // touching the existing benchmark (non-destructive).
  if (bm.source === 'features') card.appendChild(buildAddFeatureBox());
  for (const f of bm.files || []) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = (f.kind === 'pdf' ? '📄 ' : '🖼 ') + f.name;
    card.appendChild(chip);
  }
  if (bm.links) {
    const row2 = document.createElement('div');
    row2.className = 'doc-row';
    for (const [label, url] of [['Google Patents', bm.links.google], ['Espacenet', bm.links.espacenet]]) {
      const a = document.createElement('a');
      a.href = url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = label;
      row2.appendChild(a);
    }
    card.appendChild(row2);
  }
  if (bm.status === 'error') {
    const e = document.createElement('div');
    e.className = 'muted'; e.textContent = `Error: ${bm.error}`;
    card.appendChild(e);
  }
  if (bm.status === 'pending') {
    const tabAtPoll = activeTab;
    bmPoll = setTimeout(async () => {
      if (activeTab !== tabAtPoll) return;
      const st2 = await api(`/api/tabs/${tabAtPoll}/state`);
      if (activeTab === tabAtPoll) renderBenchmark(st2.benchmark);
    }, 3000);
  }
}

$('bm-set').onclick = async () => {
  const text = $('bm-text').value.trim();
  if (!text) return;
  const res = await api(`/api/tabs/${activeTab}/benchmark`, {
    method: 'PUT', body: JSON.stringify({ text }) });
  if (res.error) { $('bm-status').textContent = res.error; return; }
  $('bm-text').value = '';
  $('bm-status').textContent = '';
  renderBenchmark(res.benchmark);
};

const BM_FEATURE_TEMPLATE =
`TARGET FEATURE COMBINATION (a document must disclose ALL of A–C):

A) <feature A — what it is>.
   Surface forms to treat as the same component: "<synonym>", "<synonym>".
B) <feature B>.
   Surface forms: "<synonym>", "<synonym>".
C) <feature C>.
   Surface forms: "<synonym>", "<synonym>".

IMPLICIT MATCHES COUNT: if a document physically realizes an element above
without using the literal word, treat it as a match for that element.

SEED / SIMILAR DOCUMENTS already known to be near this space (use to anchor the
search and to pull their families and citing/cited art — do NOT just return these):
<WO…, CN…, EP…>`;

$('bm-feat-tpl').onclick = () => {
  const ta = $('bm-feat-spec');
  if (ta.value.trim() && !confirm('Replace the current feature spec with the template?')) return;
  ta.value = BM_FEATURE_TEMPLATE;
  ta.focus();
};

/* one-by-one weighted feature rows */
function addFeatureRow(name = '', weight = 1) {
  const wrap = $('bm-feat-rows');
  const row = document.createElement('div');
  row.className = 'bm-feat-row';
  const txt = document.createElement('input');
  txt.type = 'text'; txt.className = 'feat-name'; txt.maxLength = 500; txt.value = name;
  txt.placeholder = 'A feature a matching document must disclose…';
  const sel = document.createElement('select');
  sel.className = 'feat-weight';
  sel.title = 'Importance weight — decisive when candidates tie on points';
  for (let w = 1; w <= 5; w++) {
    const o = document.createElement('option');
    o.value = w; o.textContent = '★'.repeat(w) + ` (${w})`;
    if (w === weight) o.selected = true;
    sel.appendChild(o);
  }
  const del = document.createElement('button');
  del.className = 'btn small del'; del.textContent = '🗑'; del.title = 'Remove this feature';
  del.onclick = () => { row.remove(); if (!wrap.children.length) addFeatureRow(); };
  row.append(txt, sel, del);
  wrap.appendChild(row);
  return txt;
}
function collectFeatureRows() {
  return [...document.querySelectorAll('#bm-feat-rows .bm-feat-row')]
    .map(r => ({ name: r.querySelector('.feat-name').value.trim(),
                 weight: parseInt(r.querySelector('.feat-weight').value, 10) || 1 }))
    .filter(f => f.name);
}
$('bm-feat-add').onclick = () => { addFeatureRow().focus(); };
addFeatureRow();   // start with one empty row

// A self-contained "➕ Add a feature" window rendered on the benchmark card. It
// APPENDS a single weighted feature to the current benchmark and never clears or
// replaces what is already there.
function buildAddFeatureBox() {
  const box = document.createElement('div');
  box.className = 'bm-add-feat';
  const lbl = document.createElement('div');
  lbl.className = 'muted'; lbl.textContent = '➕ Add a feature to this benchmark:';
  const row = document.createElement('div');
  row.className = 'bm-feat-row';
  const txt = document.createElement('input');
  txt.type = 'text'; txt.maxLength = 500; txt.className = 'feat-name add-feat-name';
  txt.placeholder = 'A feature a matching document must disclose…';
  const sel = document.createElement('select');
  sel.className = 'feat-weight';
  sel.title = 'Importance weight — decisive when candidates tie on points';
  for (let w = 1; w <= 5; w++) {
    const o = document.createElement('option');
    o.value = w; o.textContent = '★'.repeat(w) + ` (${w})`;
    sel.appendChild(o);
  }
  const add = document.createElement('button');
  add.className = 'btn small primary'; add.textContent = 'Add';
  const submit = async () => {
    const name = txt.value.trim();
    if (!name) { txt.focus(); return; }
    const res = await api(`/api/tabs/${activeTab}/benchmark/features/add`, {
      method: 'POST', body: JSON.stringify({ name, weight: parseInt(sel.value, 10) || 1 }) });
    if (res.error) { $('bm-status').textContent = res.error; return; }
    $('bm-status').textContent = '';
    renderBenchmark(res.benchmark);   // re-render shows the appended feature + a fresh empty input
  };
  add.onclick = submit;
  txt.onkeydown = e => { if (e.key === 'Enter') submit(); };
  row.append(txt, sel, add);
  box.append(lbl, row);
  return box;
}

// Re-open the feature editor pre-filled from an existing feature benchmark, so
// features can be added / removed / re-weighted one by one without deleting it.
async function openFeatureEditor(bm) {
  $('bm-setup').classList.remove('hidden');
  $('bm-features').open = true;
  $('bm-feat-title').value =
    (bm.title && bm.title !== '🧩 Feature combination') ? bm.title : '';
  const rows = $('bm-feat-rows');
  rows.innerHTML = '';
  const feats = bm.features || [];
  if (feats.length) {
    for (const f of feats) addFeatureRow(f.name, f.weight);
  } else {
    addFeatureRow();
    // freeform spec (no weighted rows) — pull the text so it stays editable
    const full = await api(`/api/tabs/${activeTab}/benchmark/full`);
    if (full && full.text) {
      $('bm-feat-spec').value = full.text;
      const ff = document.querySelector('.bm-feat-freeform');
      if (ff) ff.open = true;
    }
  }
  $('bm-features').scrollIntoView({ behavior: 'smooth', block: 'center' });
  rows.querySelector('.feat-name')?.focus();
}

$('bm-feat-set').onclick = async () => {
  const title = $('bm-feat-title').value.trim();
  const features = collectFeatureRows();
  const spec = $('bm-feat-spec').value.trim();
  let payload;
  if (features.length) {
    payload = { features, title: title || null };
  } else if (spec.length >= 10) {
    payload = { spec, title: title || null };
  } else {
    $('bm-status').textContent = 'Add at least one feature, or describe the combination in the window.';
    return;
  }
  const res = await api(`/api/tabs/${activeTab}/benchmark/features`, {
    method: 'POST', body: JSON.stringify(payload) });
  if (res.error) { $('bm-status').textContent = res.error; return; }
  $('bm-feat-spec').value = '';
  $('bm-feat-title').value = '';
  $('bm-feat-rows').innerHTML = ''; addFeatureRow();
  $('bm-status').textContent = '';
  renderBenchmark(res.benchmark);
};

const bmDz = $('bm-dropzone');
bmDz.ondragover = e => { e.preventDefault(); bmDz.classList.add('drag'); };
bmDz.ondragleave = () => bmDz.classList.remove('drag');
bmDz.ondrop = e => {
  e.preventDefault(); bmDz.classList.remove('drag');
  if (e.dataTransfer.files.length) uploadBenchmark(e.dataTransfer.files);
};
$('bm-file').onchange = e => { if (e.target.files.length) uploadBenchmark(e.target.files); };

async function uploadBenchmark(fileList) {
  const fd = new FormData();
  for (const f of fileList) fd.append('files', f);
  fd.append('reading_model', readModelValue());
  $('bm-status').textContent =
    `Uploading ${fileList.length} file(s)… (pictures transcribed by ` +
    `${readModelValue().replace('claude-', '')}, 4 pages in parallel — the card shows progress)`;
  const res = await api(`/api/tabs/${activeTab}/benchmark/upload`, { method: 'POST', body: fd });
  $('bm-file').value = '';
  if (res.error) { $('bm-status').textContent = `Error: ${res.error}`; return; }
  $('bm-status').textContent = '';
  renderBenchmark(res.benchmark);
}

/* ---------- prefs (model/skills/toggles per tab) ---------- */
function prefsKey() { return `pb-prefs-${activeTab}`; }
function savePrefs() {
  if (!activeTab) return;
  localStorage.setItem(prefsKey(), JSON.stringify({
    model: $('model').value,
    readModel: readModelValue(),
    skills: [...document.querySelectorAll('#skills input:checked')].map(i => i.value),
    useDocs: $('use-docs').checked,
    askNb: $('ask-nb').checked,
    full: $('full-analysis').checked,
    answerFormat: $('answer-format').value,
  }));
}
function loadPrefs() {
  let p = {};
  try { p = JSON.parse(localStorage.getItem(prefsKey()) || '{}'); } catch {}
  if (p.model) $('model').value = p.model;
  else $('model').value = skillsMeta.default_model;
  setReadModel(p.readModel || skillsMeta.default_read_model || 'claude-haiku-4-5');
  const want = new Set(p.skills || defaultSkills());
  document.querySelectorAll('#skills input').forEach(i => { i.checked = want.has(i.value); });
  $('use-docs').checked = p.useDocs !== false;
  $('ask-nb').checked = !!p.askNb;
  $('full-analysis').checked = !!p.full;
  $('answer-format').value = p.answerFormat || '';
  updateSkillsSummary();
}
function defaultSkills() {
  return skillsMeta.skills.map(s => s.name)
    .filter(n => n === 'patent-analyzer' || n === 'patent-search-pipeline');
}

/* ---------- skills + models ---------- */
async function loadSkills() {
  skillsMeta = await api('/api/skills');
  const sel = $('model');
  const modelTargets = [sel, ...readSelects()];
  for (const target of modelTargets) target.innerHTML = '';
  for (const m of skillsMeta.models || []) {
    for (const target of modelTargets) {
      const o = document.createElement('option');
      o.value = m; o.textContent = m.replace('claude-', '');
      target.appendChild(o);
    }
  }
  sel.value = skillsMeta.default_model;
  setReadModel(skillsMeta.default_read_model || 'claude-haiku-4-5');
  const fmt = $('answer-format');
  fmt.innerHTML = '';
  for (const f of skillsMeta.answer_formats || [{ key: '', label: 'Default answer' }]) {
    const o = document.createElement('option');
    o.value = f.key; o.textContent = f.label;
    fmt.appendChild(o);
  }
  const wrap = $('skills');
  wrap.innerHTML = '';
  const lessonSel = $('lesson-skill');
  lessonSel.innerHTML = '';
  for (const s of skillsMeta.skills || []) {
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = s.name;
    cb.onchange = () => { savePrefs(); updateSkillsSummary(); };
    label.appendChild(cb);
    label.appendChild(document.createTextNode(' ' + s.name));
    const d = document.createElement('div');
    d.className = 'desc'; d.textContent = s.description;
    label.appendChild(d);
    wrap.appendChild(label);
    const o = document.createElement('option');
    o.value = s.name; o.textContent = s.name;
    lessonSel.appendChild(o);
  }
  sel.onchange = savePrefs;
  for (const s of readSelects()) s.onchange = () => {
    setReadModel(s.value); savePrefs();
    if (lastDocs.length) renderDocs(lastDocs);   // refresh the model-aware Continue count
  };
  $('use-docs').onchange = savePrefs;
  $('ask-nb').onchange = savePrefs;
  $('full-analysis').onchange = savePrefs;
  fmt.onchange = savePrefs;
}
function updateSkillsSummary() {
  const n = document.querySelectorAll('#skills input:checked').length;
  $('skills-summary').textContent = `🧠 Skills${n ? ` (${n})` : ''}`;
}

/* ---------- documents ---------- */
let docsFilter = 'all';      // 'all' | 'unfetched' — quick view of pending/error only
let docsSort = 'combined';   // 'combined' | 'claude' | 'nlm' | 'delta' | 'weighted' — ranking key
let docsSortTouched = false; // did the user pick a sort? (else feature mode defaults to weighted)
// Combined ("common") score: average of the two engines when both rated, else the
// single available score. Ranking by this puts documents BOTH engines like on top.
function combinedScore(d) {
  if (d.score != null && d.nlm_score != null) return (d.score + d.nlm_score) / 2;
  return d.score ?? d.nlm_score ?? null;
}
function featureMode() {
  return !!(currentBm && currentBm.source === 'features' && (currentBm.features || []).length);
}
// Weighted points a candidate earns from the benchmark's weighted features, using
// the CURRENT weights (re-weighting re-ranks without re-reading). YES = full weight,
// PARTIAL = half. {weighted, matched (#YES), total (Σ weight), count} | null.
function featureStats(d) {
  const feats = (currentBm && currentBm.features) || [];
  const fs = d.feature_scores;
  if (!featureMode() || !fs) return null;
  let weighted = 0, matched = 0, total = 0;
  for (let i = 0; i < feats.length; i++) {
    const w = feats[i].weight || 1;
    total += w;
    let s = fs[i];
    if (!s || s.name !== feats[i].name) s = fs.find(x => x.name === feats[i].name) || s;
    const status = s ? s.status : 'no';
    if (status === 'yes') { weighted += w; matched++; }
    else if (status === 'partial') { weighted += w * 0.5; }
  }
  return { weighted, matched, total, count: feats.length };
}
function scoreSortValue(d, key) {
  if (key === 'weighted') {
    const fst = featureStats(d);
    if (!fst) return -1;
    // primary = weighted points, tiebreak = #matched features, then combined score
    return fst.weighted * 1e6 + fst.matched * 1e3 + (combinedScore(d) ?? 0) * 10;
  }
  if (key === 'nlm') return d.nlm_score ?? -1;
  if (key === 'delta') return (d.score != null && d.nlm_score != null) ? Math.abs(d.score - d.nlm_score) : -1;
  if (key === 'claude') return d.score ?? -1;
  return combinedScore(d) ?? -1;   // 'combined' (default)
}
let lastDocs = [];
function renderDocs(allDocs) {
  lastDocs = allDocs;
  const wrap = $('docs');
  wrap.innerHTML = '';
  $('doc-count').textContent = allDocs.length || '';

  // "Nutshell" summary: counts by status + a one-click filter to see ONLY the
  // not-fetched ones (pending/error) without scrolling the whole list.
  const counts = { fetched: 0, pending: 0, error: 0 };
  for (const d of allDocs) counts[d.status] = (counts[d.status] || 0) + 1;
  const unfetched = (counts.pending || 0) + (counts.error || 0);
  // surface the "Why the gap?" explainer only when there ARE disagreements (≥2)
  const disagree = allDocs.filter(d => d.score != null && d.nlm_score != null && Math.abs(d.score - d.nlm_score) >= 2).length;
  const rcl = $('reconcile');
  if (rcl) { rcl.classList.toggle('hidden', !disagree); rcl.textContent = `🔍 Why the gap? (${disagree})`; }
  // "Continue / Re-rank" is MODEL-AWARE: a candidate counts as done only if it was
  // read by the currently-chosen 📖 model OR a stronger one. So raising the reading
  // model (e.g. haiku → sonnet) re-surfaces the weaker-read ones as "left", letting an
  // interrupted sonnet read of all 221 resume on just the leftovers — never re-reading
  // what sonnet already did, never downgrading an opus read. If nothing is left at the
  // chosen level, re-rank the WHOLE corpus from stored assessments (zero re-reading).
  const rm = readModelValue();
  const hasRead = d => d.status === 'fetched' && (d.verdict_len || d.score != null);
  const readAtLevel = d => hasRead(d) && modelRank(d.score_model) <= modelRank(rm);
  const unread = allDocs.filter(d => d.status === 'fetched' && !readAtLevel(d)).length;
  const assessed = allDocs.filter(hasRead).length;
  const cont = $('claude-continue');
  if (cont) {
    cont.classList.toggle('hidden', !(unread || assessed));
    cont.textContent = unread ? `▶️ Continue ${rm.replace('claude-', '')} read (${unread} left)`
                              : `📊 Re-rank ${assessed} stored`;
  }
  if (!unfetched && docsFilter === 'unfetched') docsFilter = 'all';
  // NLM coverage: fetched candidates that are NOT a source in any notebook — these are
  // invisible to the 📓 NLM shortlist, so surface + bulk-add them.
  const notInNlm = allDocs.filter(d => d.status === 'fetched' && !d.nlm_source_notebook);
  const inNlm = (counts.fetched || 0) - notInNlm.length;
  if (!notInNlm.length && docsFilter === 'no-nlm') docsFilter = 'all';
  if (allDocs.length) {
    const bar = document.createElement('div');
    bar.className = 'docs-summary';
    bar.innerHTML =
      `<span class="chip ok" title="fetched & ready">✓ ${counts.fetched || 0}</span>`
      + (counts.pending ? `<span class="chip warn" title="still fetching">⏳ ${counts.pending}</span>` : '')
      + (counts.error ? `<span class="chip err" title="failed to fetch — check the number/kind code">⚠ ${counts.error}</span>` : '')
      + (counts.fetched ? `<span class="chip" title="fetched candidates that ARE a source in some NotebookLM notebook">📓 ${inNlm} in NLM</span>` : '');
    if (unfetched) {
      const t = document.createElement('button');
      t.className = 'btn small';
      t.textContent = docsFilter === 'unfetched' ? '↩ show all' : `🔎 show ${unfetched} not-fetched`;
      t.onclick = () => { docsFilter = docsFilter === 'unfetched' ? 'all' : 'unfetched'; renderDocs(allDocs); };
      bar.appendChild(t);
    }
    if (notInNlm.length) {
      const t = document.createElement('button');
      t.className = 'btn small';
      t.textContent = docsFilter === 'no-nlm' ? '↩ show all' : `📭 show ${notInNlm.length} not in NLM`;
      t.title = 'These fetched candidates are NOT a source in any NotebookLM notebook, so the 📓 NLM shortlist cannot see them. Filter to them, then add to a notebook.';
      t.onclick = () => { docsFilter = docsFilter === 'no-nlm' ? 'all' : 'no-nlm'; renderDocs(allDocs); };
      bar.appendChild(t);
      const add = document.createElement('button');
      add.className = 'btn small';
      add.textContent = `📓➕ add ${notInNlm.length} to a notebook`;
      add.title = 'Open the notebook picker pre-loaded with every candidate not yet in NLM — choose the destination notebook (or create one) and add them in one go.';
      add.onclick = () => openAddToNotebook(notInNlm.map(d => d.id), `${notInNlm.length} candidate(s) not in NLM`);
      bar.appendChild(add);
      const split = document.createElement('button');
      split.className = 'btn small';
      split.textContent = `📚 auto-split ${notInNlm.length} across free notebooks`;
      split.title = 'Fill the not-in-NLM candidates into your notebooks that still have room (most-free first), spilling into the next as each fills — no need to pick one destination. For a manual split (e.g. 40+37), use 📓➕ and the per-notebook picker instead.';
      split.onclick = () => autoSplitNotInNlm(notInNlm.map(d => d.id));
      bar.appendChild(split);
    }
    // sort the palmares — feature mode adds the weighted key (and defaults to it)
    const fmode = featureMode();
    const effSort = (!docsSortTouched && fmode) ? 'weighted' : docsSort;
    if (fmode || allDocs.some(d => d.nlm_score != null)) {
      const sortSel = document.createElement('select');
      sortSel.className = 'sort-sel';
      sortSel.title = 'Rank candidates by';
      const opts = [['combined', '🥇 by combined'], ['claude', '🤖 by Claude'], ['nlm', '📓 by NLM'], ['delta', 'Δ by disagreement']];
      if (fmode) opts.unshift(['weighted', '⚖ by weighted features']);
      for (const [v, label] of opts) {
        const o = document.createElement('option'); o.value = v; o.textContent = label;
        if (v === effSort) o.selected = true;
        sortSel.appendChild(o);
      }
      sortSel.onchange = () => { docsSort = sortSel.value; docsSortTouched = true; renderDocs(allDocs); };
      bar.appendChild(sortSel);
    }
    wrap.appendChild(bar);
  }

  // ranking ("palmares"): chosen score first, ties/unscored after (by insertion)
  const sortKey = (!docsSortTouched && featureMode()) ? 'weighted' : docsSort;
  let docs = [...allDocs].sort((a, b) =>
    scoreSortValue(b, sortKey) - scoreSortValue(a, sortKey) || a.id - b.id);
  if (docsFilter === 'unfetched') docs = docs.filter(d => d.status !== 'fetched');
  if (docsFilter === 'no-nlm') docs = docs.filter(d => d.status === 'fetched' && !d.nlm_source_notebook);
  for (const d of docs) {
    const el = document.createElement('div');
    el.className = 'doc';
    const row1 = document.createElement('div');
    row1.className = 'doc-row';
    if (d.status === 'fetched') {
      const sel = document.createElement('input');
      sel.type = 'checkbox';
      sel.checked = docSelection.has(d.id);
      sel.title = 'Select a candidate to (a) load its FULL primary text into the chat so Claude can quote real claims/[paragraphs], and (b) scope 🏆 Deep compare to it. None selected = whole list, clipped.';
      sel.onchange = () => {
        sel.checked ? docSelection.add(d.id) : docSelection.delete(d.id);
        updateDocSelChip();
      };
      row1.appendChild(sel);
    }
    const num = document.createElement('span');
    num.className = 'num'; num.textContent = d.number;
    row1.appendChild(num);
    const st = document.createElement('span');
    st.className = 'status ' + d.status;
    st.textContent = d.status;
    if (d.error) st.title = d.error;
    row1.appendChild(st);
    if (d.status === 'fetched') {
      // NLM membership badge: which exact notebook this candidate is a source in (so
      // you can see at a glance what the 📓 shortlist can/can't see), or "not in NLM".
      const nbm = document.createElement('span');
      if (d.nlm_source_notebook) {
        nbm.className = 'chip nlm-in';
        nbm.textContent = `📓 ${nbTitle(d.nlm_source_notebook)}`;
        nbm.title = `In NotebookLM notebook: ${nbTitleById[d.nlm_source_notebook] || d.nlm_source_notebook}`;
      } else {
        nbm.className = 'chip nlm-out';
        nbm.textContent = '📭 not in NLM';
        nbm.title = 'Not a source in any NotebookLM notebook — the 📓 NLM shortlist cannot see this candidate. Use 📓➕ to add it.';
      }
      row1.appendChild(nbm);
      const addNb = document.createElement('button');
      addNb.className = 'btn small'; addNb.textContent = '📓➕';
      addNb.title = 'Add this candidate to a NotebookLM notebook — pick which one (or create one)';
      addNb.onclick = () => openAddToNotebook([d.id], `${d.number}`);
      row1.appendChild(addNb);
    }
    const del = document.createElement('button');
    del.className = 'btn small del'; del.textContent = '🗑';
    del.title = 'Remove document';
    del.onclick = async () => {
      await api(`/api/tabs/${activeTab}/documents/${d.id}`, { method: 'DELETE' });
      refreshDocs();
    };
    row1.appendChild(del);
    el.appendChild(row1);
    if (d.title) {
      const t = document.createElement('div');
      t.className = 'title'; t.textContent = d.title;
      el.appendChild(t);
    }
    if (d.score != null || d.nlm_score != null) {
      const sc = document.createElement('div');
      sc.className = 'score';
      const parts = [];
      // combined ("common") score leads when both engines rated it
      if (d.score != null && d.nlm_score != null) parts.push(`<span class="combined">🥇 ${combinedScore(d).toFixed(1)}</span>`);
      if (d.score != null) parts.push(`🤖 ${d.score}/10`);
      if (d.nlm_score != null) parts.push(`📓 ${d.nlm_score}/10`);
      // Δ flags where the two engines disagree (≥2 points apart)
      if (d.score != null && d.nlm_score != null) {
        const delta = Math.abs(d.score - d.nlm_score);
        parts.push(`<span class="delta ${delta >= 2 ? 'big' : ''}">Δ ${delta.toFixed(1)}</span>`);
      }
      sc.innerHTML = parts.join(' · ');
      const note = d.score_note || d.nlm_score_note;
      if (note) { const n = document.createElement('span'); n.className = 'score-note'; n.textContent = ` — ${note}`; sc.appendChild(n); }
      sc.title = '🤖 Claude (deep compare) vs 📓 NotebookLM rating, both 0–10 vs the benchmark.'
        + (d.scored_at ? `\nClaude scored ${new Date(d.scored_at * 1000).toLocaleString()}` : '')
        + (d.nlm_scored_at ? `\nNLM scored ${new Date(d.nlm_scored_at * 1000).toLocaleString()}` : '');
      el.appendChild(sc);
      // when + which model did the last full read (so you know what's stale)
      if (d.scored_at || d.score_model) {
        const r = document.createElement('div');
        r.className = 'read-meta';
        const when = d.scored_at ? new Date(d.scored_at * 1000).toLocaleString() : '—';
        r.textContent = `🤖 full-read ${when}` + (d.score_model ? ` · ${d.score_model.replace('claude-', '')}` : '');
        el.appendChild(r);
      }
    } else if (d.status === 'fetched') {
      // fetched but never full-read — make that visible so it's clearly pending a read
      const r = document.createElement('div');
      r.className = 'read-meta muted';
      r.textContent = '🤖 not yet full-read';
      el.appendChild(r);
    }
    const fst = featureStats(d);
    if (fst) {
      const fw = document.createElement('div');
      fw.className = 'feat-scores';
      const head = document.createElement('div');
      head.className = 'feat-weighted';
      head.innerHTML = `⚖ <b>${fst.weighted.toFixed(1)}</b>/${fst.total} weighted `
        + `· ${fst.matched}/${fst.count} features`;
      head.title = 'Weighted points (Σ weight of disclosed features; partial = half) '
        + 'out of the max, then how many features matched. Primary ranking key; '
        + 'matched-count breaks ties.';
      fw.appendChild(head);
      const chips = document.createElement('div');
      chips.className = 'feat-chip-row';
      const mark = { yes: '✓', partial: '~', no: '✗' };
      for (const f of (d.feature_scores || [])) {
        const c = document.createElement('span');
        c.className = 'chip feat-mark ' + f.status;
        c.textContent = `${mark[f.status] || '?'} ${f.name} ·${'★'.repeat(f.weight || 1)}`;
        if (f.note) c.title = f.note;
        chips.appendChild(c);
      }
      fw.appendChild(chips);
      el.appendChild(fw);
    }
    if (d.status === 'fetched') {
      const sz = document.createElement('div');
      sz.className = 'sizes';
      const fmt = n => !n ? '—' : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
      sz.textContent = `abstract ${fmt(d.abstract_len)} · claims ${fmt(d.claims_len)} · description ${fmt(d.description_len)} chars · ` +
        (d.digest_len ? 'full-text digest ✓' : 'digesting full text…');
      sz.title = d.digest_len
        ? 'Stored text sizes; a cheap model has read the FULL document and stored a digest the chat always sees'
        : 'Stored text sizes; the full-text digest is still being generated';
      el.appendChild(sz);
    }
    const row2 = document.createElement('div');
    row2.className = 'doc-row';
    if (d.source === 'notebook-text') {
      const badge = document.createElement('span');
      badge.className = 'chip'; badge.textContent = '📓 imported text';
      badge.title = 'Imported from a NotebookLM source (raw text, not a patent)';
      row2.appendChild(badge);
    }
    if (d.links) {
      for (const [label, url] of [['Google Patents', d.links.google], ['Espacenet', d.links.espacenet]]) {
        const a = document.createElement('a');
        a.href = url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = label;
        row2.appendChild(a);
      }
    }
    if (d.status === 'fetched') {
      const view = document.createElement('button');
      view.className = 'btn small'; view.textContent = '👁 view';
      view.title = 'Show the stored full text';
      view.onclick = async () => {
        const full = await api(`/api/tabs/${activeTab}/documents/${d.id}`);
        openViewer(full.number, full);
      };
      row2.appendChild(view);
    }
    if (d.status === 'error') {
      const retry = document.createElement('button');
      retry.className = 'btn small'; retry.textContent = '↻ retry';
      retry.onclick = async () => {
        await api(`/api/tabs/${activeTab}/documents/${d.id}/refetch`, { method: 'POST' });
        refreshDocs();
      };
      row2.appendChild(retry);
      const edit = document.createElement('button');
      edit.className = 'btn small'; edit.textContent = '✏️ fix number';
      edit.title = 'Edit an OCR-damaged number and refetch';
      edit.onclick = async () => {
        const n = prompt(`Correct the patent number:`, d.number);
        if (!n || n === d.number) return;
        const res = await api(`/api/tabs/${activeTab}/documents/${d.id}`, {
          method: 'PATCH', body: JSON.stringify({ number: n }) });
        if (res.error) alert(res.error);
        refreshDocs();
      };
      row2.appendChild(edit);
    }
    el.appendChild(row2);
    wrap.appendChild(el);
  }
  // prune selection of deleted/refetched-away docs
  const ids = new Set(docs.map(d => d.id));
  docSelection = new Set([...docSelection].filter(id => ids.has(id)));
  updateDocSelChip();
}

function updateDocSelChip() {
  const chip = $('doc-sel');
  const bar = $('deep-bar');
  if (docSelection.size) {
    chip.textContent = `🏆 ${docSelection.size} selected for deep analysis`;
    chip.classList.remove('hidden');
    chip.style.cursor = 'default';
    chip.onclick = null;
    if (bar) {
      bar.classList.remove('hidden');
      $('deep-selected').textContent = `🏆 Deep-analyse ${docSelection.size} selected`;
      const nr = $('nlm-rate-selected');
      if (nr) nr.textContent = `📓 NLM-rate ${docSelection.size} selected`;
    }
  } else {
    chip.classList.add('hidden');
    if (bar) bar.classList.add('hidden');
  }
  updateFocusHint();
}

function updateFocusHint() {
  const hint = $('focus-hint');
  if (!hint) return;
  if (docSelection.size) {
    hint.textContent = `🎯 Chat is focused on ${docSelection.size} selected candidate(s) — `
      + `their full primary text is loaded, so Claude can quote real claims/[paragraphs].`;
    hint.classList.remove('hidden');
  } else {
    hint.classList.add('hidden');
  }
}

// Auto-split: fill the not-in-NLM candidates across notebooks that already have room.
async function autoSplitNotInNlm(ids) {
  if (!ids.length) return;
  if (!confirm(`Auto-split ${ids.length} candidate(s) across your notebooks that have free space `
    + `(most-free first, spilling over as each fills)?`)) return;
  const r = await api(`/api/tabs/${activeTab}/notebook/distribute`,
    { method: 'POST', body: JSON.stringify({ doc_ids: ids }) });
  if (r.error && !r.ok) { alert(`Couldn't distribute: ${r.error}`); return; }
  const where = (r.placements || []).map(p => `${p.added} → «${p.notebook_title}»`).join('\n') || 'none';
  alert(`Placed ${r.placed} candidate(s):\n${where}`
    + (r.remaining ? `\n\n${r.remaining} didn't fit — all notebooks are full. ♻️ Resync + 🗑 delete duplicate sources to free space, then retry.` : '')
    + ((r.errors || []).length ? `\n\nErrors: ${r.errors.join('; ')}` : ''));
  await refreshDocs(); reloadChat(); loadNbTitles();
}

async function refreshDocs() {
  if (!activeTab) return;
  const res = await api(`/api/tabs/${activeTab}/documents`);
  renderDocs(res.documents || []);
  scheduleDocsPoll(res.documents || []);
}

function scheduleDocsPoll(docs) {
  clearTimeout(docsPoll);
  const now = Date.now() / 1000;
  const busy = docs.some(d => d.status === 'pending'
    || (d.status === 'fetched' && !d.digest_len && now - (d.fetched_at || 0) < 1800));
  if (busy) docsPoll = setTimeout(refreshDocs, 4000);
}

$('in-add').onclick = async () => {
  const text = $('in-text').value.trim();
  if (!text) return;
  const res = await api(`/api/tabs/${activeTab}/documents`, {
    method: 'POST', body: JSON.stringify({ text, reading_model: readModelValue() }) });
  if (res.error) { $('upload-status').textContent = res.error; return; }
  $('in-text').value = '';
  $('upload-status').textContent =
    `Added ${res.inserted.length}` + (res.skipped.length ? `, already present: ${res.skipped.join(', ')}` : '');
  refreshDocs();
};

/* ---------- upload ---------- */
const dz = $('dropzone');
dz.ondragover = e => { e.preventDefault(); dz.classList.add('drag'); };
dz.ondragleave = () => dz.classList.remove('drag');
dz.ondrop = e => {
  e.preventDefault(); dz.classList.remove('drag');
  if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
};
$('in-file').onchange = e => { if (e.target.files.length) uploadFiles(e.target.files); };

async function uploadFiles(fileList) {
  const files = [...fileList];
  const label = files.length === 1 ? files[0].name : `${files.length} files`;
  $('upload-status').textContent =
    `Reading numbers from ${label}… (each photo gets two parallel Claude OCR passes, ` +
    `files are read concurrently — the rest of the app stays usable while it works)`;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('reading_model', readModelValue());
  const res = await api(`/api/tabs/${activeTab}/upload`, { method: 'POST', body: fd });
  $('in-file').value = '';
  if (res.error) { $('upload-status').textContent = `Error: ${res.error}`; return; }
  if (!res.numbers.length) { $('upload-status').textContent = `No patent numbers found in ${label}.`; return; }
  const unc = (res.uncertain || []).length;
  const via = res.model ? ` via ${res.model.replace('claude-', '')}` : '';
  const fileNote = files.length > 1 ? ` across ${files.length} files` : '';
  let msg = `Found ${res.numbers.length} number(s)${fileNote}${via}` +
    (unc ? ` — ⚠ ${unc} read inconsistently between the two OCR passes, verify them against the photo:` : ':');
  // When MOST numbers disagree between passes the model can't actually read these
  // images — escalating to a stronger reading model (📖, e.g. opus) and re-uploading
  // beats hand-fixing dozens of wrong numbers.
  if (unc >= Math.max(3, res.numbers.length * 0.5)) {
    msg += ` Many numbers are uncertain — these photos are hard to read. Pick a stronger 📖 reading model (e.g. opus) and re-upload, or crop/zoom the images.`;
  }
  if ((res.errors || []).length) msg += ` (couldn't read: ${res.errors.join('; ')})`;
  $('upload-status').textContent = msg;
  showCandidates(res.numbers, res.uncertain || []);
}

function showCandidates(numbers, uncertain) {
  const wrap = $('cand-list');
  const uncSet = new Set(uncertain || []);
  wrap.innerHTML = '';
  for (const n of numbers) {
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = !uncSet.has(n); cb.value = n;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(' ' + n + (uncSet.has(n) ? ' ⚠' : '')));
    if (uncSet.has(n)) label.title = 'The two OCR passes disagreed on this number — check the photo';
    wrap.appendChild(label);
  }
  $('candidates').classList.remove('hidden');
}
$('cand-all').onclick = () => document.querySelectorAll('#cand-list input').forEach(i => i.checked = true);
$('cand-none').onclick = () => document.querySelectorAll('#cand-list input').forEach(i => i.checked = false);
$('cand-cancel').onclick = () => $('candidates').classList.add('hidden');
$('cand-add').onclick = async () => {
  const nums = [...document.querySelectorAll('#cand-list input:checked')].map(i => i.value);
  if (!nums.length) return;
  const res = await api(`/api/tabs/${activeTab}/documents`, {
    method: 'POST', body: JSON.stringify({ numbers: nums, source: 'image',
                                           reading_model: readModelValue() }) });
  $('candidates').classList.add('hidden');
  $('upload-status').textContent = res.error || `Added ${res.inserted.length} document(s).`;
  refreshDocs();
};

/* ---------- chat ---------- */
function renderChat(messages) {
  const wrap = $('chat');
  wrap.innerHTML = '';
  for (const m of messages) appendMsg(m);
  wrap.scrollTop = wrap.scrollHeight;
}

// Minimal, XSS-safe markdown: escape HTML first, then render **bold** and
// *italic* only. Newlines stay literal (the .msg has white-space: pre-wrap).
// Used so the feature-map preset can show CLAIM text in bold and the disclosure
// parentheticals in italic — claim-language vs mapping at a glance.
function renderMarkdown(text) {
  const esc = (text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*([^*\n]+?)\*/g, '<em>$1</em>');
}

function appendMsg(m) {
  const wrap = $('chat');
  const el = document.createElement('div');
  el.className = 'msg ' + m.role;
  if (m.role === 'c' || m.role === 'a') {
    const meta = document.createElement('div');
    meta.className = 'meta';
    const who = document.createElement('span');
    who.textContent = m.role === 'c' ? '🤖 Claude' : '📓 NotebookLM';
    meta.appendChild(who);
    for (const p of m.participants || []) {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = ({ model: '🧬 ', skill: '🧠 ', notebook: '📓 ', documents: '📚 ', benchmark: '🎯 ' }[p.kind] || '') + p.title;
      meta.appendChild(chip);
    }
    el.appendChild(meta);
  }
  const body = document.createElement('div');
  body.innerHTML = renderMarkdown(m.text);     // **bold** + *italic*, rest escaped
  el.appendChild(body);
  if (m.role === 'c') {
    const btn = document.createElement('button');
    btn.className = 'btn small lesson-btn';
    btn.textContent = '💾 Save as lesson';
    btn.onclick = () => openLessonModal(m.text);
    el.appendChild(btn);
  }
  wrap.appendChild(el);
  wrap.scrollTop = wrap.scrollHeight;
}

function setBusy(busy, label) {
  $('ask-claude').disabled = busy;
  $('ask-notebook').disabled = busy;
  let th = document.querySelector('.thinking');
  if (busy) {
    if (!th) {
      th = document.createElement('div');
      th.className = 'thinking';
      $('chat').appendChild(th);
    }
    th.textContent = label;
    $('chat').scrollTop = $('chat').scrollHeight;
  } else if (th) th.remove();
}

async function sendChat(notebookOnly) {
  const q = $('q').value.trim();
  if (!q || !activeTab) return;
  appendMsg({ role: 'q', text: q });
  $('q').value = '';
  const tabAtSend = activeTab;
  setBusy(true, notebookOnly ? 'Asking NotebookLM' : 'Asking Claude');
  let res;
  if (notebookOnly) {
    res = await api(`/api/tabs/${tabAtSend}/ask-notebook`, {
      method: 'POST', body: JSON.stringify({ question: q }) });
  } else {
    res = await api(`/api/tabs/${tabAtSend}/chat`, {
      method: 'POST', body: JSON.stringify({
        question: q,
        model: $('model').value,
        skills: [...document.querySelectorAll('#skills input:checked')].map(i => i.value),
        use_documents: $('use-docs').checked,
        ask_notebook: $('ask-nb').checked,
        full: $('full-analysis').checked,
        answer_format: $('answer-format').value,
        focus_ids: [...docSelection],          // selected candidates → loaded full-text
      }) });
  }
  setBusy(false);
  if (activeTab !== tabAtSend) return;            // user switched tabs meanwhile
  if (res.error && !(res.messages || []).length) {
    appendMsg({ role: 's', text: `Error: ${res.error}` });
    return;
  }
  for (const m of res.messages || []) appendMsg(m);
}

$('ask-claude').onclick = () => sendChat(false);
$('ask-notebook').onclick = () => sendChat(true);

async function runDeepCompare(idsArg, skipScored, readModelOverride) {
  // idsArg: array of doc ids → those candidates; null/[] → EVERY candidate
  // skipScored: CONTINUE mode — read only candidates not yet full-read this batch
  // readModelOverride: use this reading model for THIS run only (never mutates the
  //   tab's 📖 dropdown). Default = the tab's chosen reading model.
  if (!activeTab) return;
  const ids = idsArg || [];
  const readModel = readModelOverride || readModelValue();   // the model that ASSESSES each candidate
  const answerModel = $('model').value;                      // the model that COMPILES the ranking
  const short = m => m.replace('claude-', '');
  let scope = ids.length ? `the ${ids.length} SELECTED candidate(s)` : 'EVERY candidate';
  let rerankOnly = false;
  if (skipScored) {
    // model-aware: "to do" = candidates not yet read by `readModel` or a stronger one
    const hasRead = d => d.status === 'fetched' && (d.verdict_len || d.score != null);
    const readAtLevel = d => hasRead(d) && modelRank(d.score_model) <= modelRank(readModel);
    const todo = lastDocs.filter(d => d.status === 'fetched' && !readAtLevel(d)).length;
    const have = lastDocs.filter(hasRead).length;
    if (!todo && !have) { alert('No candidate has been full-read yet. Use 🤖 Claude deep-read all first.'); return; }
    rerankOnly = !todo;
    scope = todo ? `the ${todo} candidate(s) not yet read by ${short(readModel)} (most promising first)`
                 : `all ${have} already-read candidate(s) — RE-RANK from stored assessments, no re-reading`;
  }
  const ask = rerankOnly
    ? `Re-rank ${scope}.\n\n💬 compiles the ranking with: ${short(answerModel)}\n\nNo candidates are re-read. Start?`
    : `Assess ${scope} in FULL against the benchmark, most-promising first`
        + (skipScored ? ', skipping the ones already read' : '') + '.\n\n'
        + `📖 reads/matches each candidate with: ${short(readModel)}\n`
        + `💬 compiles the ranking with: ${short(answerModel)}\n\n`
        + (ids.length ? '' : 'Takes a few minutes. ') + 'Start?';
  if (!confirm(ask)) return;
  const q = $('q').value.trim();          // optional custom task; default ranking otherwise
  $('q').value = '';
  const tabAtSend = activeTab;
  const res = await api(`/api/tabs/${tabAtSend}/deep-compare`, {
    method: 'POST', body: JSON.stringify({
      model: answerModel,
      skills: [...document.querySelectorAll('#skills input:checked')].map(i => i.value),
      question: q || null,
      doc_ids: ids.length ? ids : null,
      reading_model: readModel,
      skip_scored: !!skipScored,
    }) });
  if (activeTab !== tabAtSend) return;
  if (res.error && !res.started) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
  for (const m of res.messages || []) appendMsg(m);     // "nothing to continue" case
  if (res.started || res.running) {
    await reloadChat();                  // show the [Deep …] line the job logged
    pollRead();                          // live progress + reload-safe (re-attaches on page load)
  }
}

// Reload-safe background deep-read: poll status, fill scores live, post the ranking
// to chat when done. Re-attaches on page load via selectTab, so a reload never
// interrupts it (the job runs server-side regardless).
async function pollRead() {
  clearTimeout(readPoll);
  if (!activeTab) return;
  const tabAt = activeTab;
  const s = await api(`/api/tabs/${activeTab}/deep-compare/status`);
  if (activeTab !== tabAt) return;
  const el = $('read-status'); el.classList.remove('muted');
  const pauseBtn = $('claude-pause');
  if (s.running) {
    readWasRunning = true;
    const pending = Math.max(0, s.total - s.done);
    const mdl = s.read_model ? ` with ${s.read_model.replace('claude-', '')}` : '';
    el.textContent = s.paused
      ? `⏸ pausing… assessed ${s.done}/${s.total}${mdl} (finishing in-flight; scores saved)`
      : `🤖 assessing ${s.done}/${s.total}${mdl} vs benchmark… ${pending} to go (scores land below; safe to reload)`;
    if (pauseBtn) pauseBtn.classList.toggle('hidden', s.paused);   // hide once a pause is requested
    refreshDocs();
    readPoll = setTimeout(pollRead, s.paused ? 2000 : 5000);
  } else if (readWasRunning) {
    readWasRunning = false;
    if (pauseBtn) pauseBtn.classList.add('hidden');
    el.textContent = `✓ assessment stopped — see chat (▶️ Continue assesses any left)`;
    refreshDocs();
    reloadChat();
  } else {
    if (pauseBtn) pauseBtn.classList.add('hidden');
    el.classList.add('muted'); el.textContent = '';
  }
}
$('claude-pause').onclick = async () => {
  if (!activeTab) return;
  $('claude-pause').classList.add('hidden');
  await api(`/api/tabs/${activeTab}/deep-compare/pause`, { method: 'POST' });
  pollRead();
};
async function reloadChat() {
  if (!activeTab) return;
  const tabAt = activeTab;
  const st = await api(`/api/tabs/${activeTab}/state`);
  if (activeTab === tabAt && !st.error) renderChat(st.messages || []);
}

$('best-match').onclick = () => runDeepCompare(docSelection.size ? [...docSelection] : null);
$('claude-rate-all').onclick = () => runDeepCompare(null);            // re-read EVERY candidate
$('claude-continue').onclick = () => runDeepCompare(null, true);      // only the not-yet-read ones
$('deep-selected').onclick = () => {
  if (!docSelection.size) { alert('No candidates are checked. Tick the box on the candidates you want analysed.'); return; }
  runDeepCompare([...docSelection]);
};
$('deep-clear').onclick = () => { docSelection = new Set(); refreshDocs(); };

/* ---------- funnel: 📓 NLM shortlist (free, broad) → 🤖 opus verify (precise, narrow) ---------- */
const VERIFY_MODEL = 'claude-opus-4-8';   // shortlist is tiny, so default the precise read to opus
// One fan-out NLM question → shortlist + ranked best/second-best + per-feature map.
// notebookId restricts it to ONE notebook (e.g. the just-consolidated one → a true
// single global pick); omitted = fan across every notebook the candidates live in.
async function runShortlist({ confirmFirst = true, statusEl = 'funnel-status', notebookId = null } = {}) {
  if (!activeTab) return;
  const fetched = lastDocs.filter(d => d.status === 'fetched').length;
  if (!fetched) { alert('No fetched candidates yet. Add and let some candidates fetch first.'); return; }
  if (confirmFirst && !confirm(`Ask NotebookLM (free) — in one fan-out question — which of the ${fetched} `
    + 'candidate(s) disclose the benchmark\'s FULL feature combination, and which are the best + '
    + 'second-best? It auto-checks the ones it names so you can then 🤖 Verify shortlist on just those.')) return;
  const fs = $(statusEl); if (fs) fs.textContent = '📓 asking NotebookLM…';
  const tabAt = activeTab;
  const res = await api(`/api/tabs/${activeTab}/nlm-shortlist`, {
    method: 'POST', body: JSON.stringify(notebookId ? { notebook_id: notebookId } : {}) });
  if (activeTab !== tabAt) return res;
  if (res.error && !(res.messages || []).length) { if (fs) fs.textContent = `Error: ${res.error}`; return res; }
  await reloadChat();
  const ids = res.shortlist_ids || [];
  if (ids.length) {
    docSelection = new Set(ids);          // auto-check the picks for stage 2
    if (fs) fs.textContent = `✓ ${ids.length}/${res.total} picked — best first: ${res.matched.join(', ')}; now 🤖 Verify shortlist`;
  } else if (fs) {
    fs.textContent = 'NotebookLM named none of your candidates — see chat.';
  }
  refreshDocs();
  return res;
}
$('nlm-shortlist').onclick = () => runShortlist();
$('verify-shortlist').onclick = () => {
  if (!docSelection.size) {
    alert('No shortlist is checked. Run 📓 NLM shortlist first, or tick the candidates you want opus to verify.');
    return;
  }
  // one-shot opus for THIS verify only — do NOT mutate the tab's 📖 model choice
  runDeepCompare([...docSelection], false, VERIFY_MODEL);
};

// 🧺 Consolidate → copy ONLY the best (checked) candidates into ONE new notebook so
// 📓 NLM shortlist can compare them in a single query. Uses an IN-PAGE modal (not
// native prompt/confirm — browsers silently suppress those after a few dialogs, which
// looked like "nothing happens"). Best-only by design.
$('nlm-consolidate').onclick = () => {
  if (!activeTab) return;
  // use the checked candidates if any, else fall back to the PERSISTED shortlist picks
  // (📓 NLM shortlist remembers them, so no need to re-check after a reload / tab switch)
  const shortlisted = (lastDocs || []).filter(d => d.shortlisted).map(d => d.id);
  const usingShortlist = !docSelection.size && shortlisted.length > 0;
  consolidateIds = docSelection.size ? [...docSelection] : shortlisted;
  const n = consolidateIds.length;
  $('consolidate-title').value = `Best picks — ${currentTabName()}`;
  $('consolidate-bm').checked = true;
  $('consolidate-status').textContent = '';
  const go = $('consolidate-go');
  if (!n) {
    $('consolidate-info').textContent = 'No best candidates yet. Run 📓 NLM shortlist first (it picks '
      + 'and remembers the best), or tick candidates by hand, then reopen this.';
    go.disabled = true;
  } else if (n > 50) {
    $('consolidate-info').textContent = `${n} candidate(s) selected, but a NotebookLM notebook holds at `
      + 'most 50 sources. Narrow to ≤50 (untick weaker ones, or re-run 📓 shortlist), then reopen this.';
    go.disabled = true;
  } else {
    $('consolidate-info').textContent = `${n} ${usingShortlist ? 'shortlisted (best)' : 'checked'} `
      + 'candidate(s) will be copied into a NEW notebook (it becomes this tab’s notebook). '
      + 'Then the best + second-best are picked automatically.';
    go.disabled = false;
  }
  $('consolidate-modal').classList.remove('hidden');
};
$('consolidate-cancel').onclick = () => $('consolidate-modal').classList.add('hidden');
$('consolidate-go').onclick = async () => {
  const title = ($('consolidate-title').value || '').trim();
  if (!title) { $('consolidate-status').textContent = 'Enter a name for the notebook.'; return; }
  const ids = consolidateIds;
  if (!ids.length) { $('consolidate-status').textContent = 'No candidates to consolidate.'; return; }
  const includeBm = $('consolidate-bm').checked;
  const go = $('consolidate-go'); go.disabled = true;
  // launch the resumable BACKGROUND job (consolidate → shortlist → debate). It runs on
  // the server, so closing the tab / a dropped connection no longer interrupts it.
  const res = await api(`/api/tabs/${activeTab}/pipeline`, {
    method: 'POST', body: JSON.stringify({ title, doc_ids: ids, include_benchmark: includeBm }) });
  go.disabled = false;
  if (res.error) { $('consolidate-status').textContent = `Error: ${res.error}`; return; }
  $('consolidate-modal').classList.add('hidden');
  pollPipeline();
};

/* ---------- consolidate→shortlist→debate background job (crash-resilient) ---------- */
let pipelinePoll = null;
async function pollPipeline() {
  clearTimeout(pipelinePoll);
  const tabAt = activeTab;
  const s = await api(`/api/tabs/${activeTab}/pipeline/status`);
  if (activeTab !== tabAt) return;
  const rs = $('rate-status'); const rb = $('pipeline-resume');
  if (s.error && !s.present) { rs.textContent = `Error: ${s.error}`; return; }
  await reloadChat();                       // surface messages the worker appended
  if (s.running) {
    rs.classList.remove('err');
    rs.textContent = `⏳ ${s.status_text || ('pipeline: ' + (s.step || ''))} (runs on the server — safe to wait or leave)`;
    if (rb) rb.classList.add('hidden');
    pipelinePoll = setTimeout(pollPipeline, 3000);
  } else if (s.phase === 'done') {
    rs.classList.remove('err');
    rs.textContent = '✅ Pipeline done — consolidated, picked best, and debated Claude ↔ NotebookLM (see chat).';
    if (rb) rb.classList.add('hidden');
    refreshDocs();
  } else if (s.resumable) {
    rs.classList.add('err');
    rs.textContent = `⚠️ Pipeline interrupted at “${s.step}”${s.error ? ' — ' + s.error : ''}.`;
    if (rb) rb.classList.remove('hidden');
  }
}
$('pipeline-resume').onclick = async () => {
  const rb = $('pipeline-resume'); rb.disabled = true;
  const res = await api(`/api/tabs/${activeTab}/pipeline`, { method: 'POST', body: JSON.stringify({ resume: true }) });
  rb.disabled = false;
  if (res.error) { $('rate-status').textContent = `Resume failed: ${res.error}`; return; }
  pollPipeline();
};
// re-attach on tab load: if a pipeline is running or was interrupted, show it / poll it
async function attachPipeline() {
  const s = await api(`/api/tabs/${activeTab}/pipeline/status`);
  const rb = $('pipeline-resume');
  if (s.running) { pollPipeline(); }
  else if (s.resumable) {
    if (rb) rb.classList.remove('hidden');
    $('rate-status').classList.add('err');
    $('rate-status').textContent = `⚠️ Pipeline interrupted at “${s.step}” — ▶️ Resume to finish.`;
  } else if (rb) { rb.classList.add('hidden'); }
}

/* ---------- NotebookLM rating (palmares: 📓 NLM vs 🤖 Claude) ---------- */
async function startNlmRate(ids, force) {
  if (!activeTab) return;
  let scope;
  if (ids && ids.length) {
    scope = `the ${ids.length} selected candidate(s) (re-rating them)`;
  } else {
    // "all" skips candidates already rated by NLM — only the rest are queried
    const fetched = lastDocs.filter(d => d.status === 'fetched');
    const already = fetched.filter(d => d.nlm_score != null).length;
    const todo = fetched.length - already;
    if (!todo) { alert(`All ${fetched.length} candidates are already NLM-rated. Use “📓 NLM-rate selected” to re-rate specific ones.`); return; }
    scope = `${todo} not-yet-rated candidate(s) (skipping ${already} already rated)`;
  }
  if (!confirm(`Ask NotebookLM to rate ${scope} against the benchmark? One query per candidate `
    + '— runs in the background; scores fill in live and you can keep working.')) return;
  const res = await api(`/api/tabs/${activeTab}/nlm-rate`, {
    method: 'POST', body: JSON.stringify({ doc_ids: ids && ids.length ? ids : null, force }) });
  if (res.error) { $('rate-status').textContent = `Error: ${res.error}`; return; }
  pollRate();
}
$('nlm-rate').onclick = () => startNlmRate(null, false);          // all (incremental)
$('nlm-rate-selected').onclick = () => {
  if (!docSelection.size) { alert('No candidates are checked. Tick the box on the candidates you want NLM to rate.'); return; }
  startNlmRate([...docSelection], true);                          // re-rate the chosen ones now
};
$('reconcile').onclick = async () => {
  if (!activeTab) return;
  const btn = $('reconcile'); btn.disabled = true;
  appendMsg({ role: 'q', text: 'Why do 📓 NotebookLM and 🤖 Claude disagree on some candidates?' });
  setBusy(true, 'Explaining the score gaps (one cheap call over the stored notes)');
  const res = await api(`/api/tabs/${activeTab}/reconcile`, {
    method: 'POST', body: JSON.stringify({ min_delta: 2 }) });
  setBusy(false); btn.disabled = false;
  if (res.error && !(res.messages || []).length) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
  for (const m of res.messages || []) appendMsg(m);
};
// ⚖️ Debate finalists: NotebookLM's shortlisted finalists (in its notebook) are read
// block-by-block by NLM (grounded) and argued by Claude (from stored digests) — a
// bidirectional reconciliation, one prompt per side. docIds = explicit set (e.g. the
// just-consolidated finalists); else the checked set; else the persisted shortlist.
async function runChallenge({ confirmFirst = true, docIds = null } = {}) {
  if (!activeTab) return;
  const explicit = docIds && docIds.length;
  const useSel = !explicit && docSelection.size > 0;
  const finalists = lastDocs.filter(d => d.shortlisted).length;
  if (!explicit && !useSel && !finalists) {
    alert('No finalists to debate yet. Run 📓 NLM shortlist (it picks & remembers the finalists) '
      + 'and 🧺 Consolidate them, or tick the documents you want debated.');
    return;
  }
  if (confirmFirst) {
    const subj = useSel ? `the ${docSelection.size} checked document(s)`
                        : "both sides' picks (NotebookLM's shortlist + Claude's high-scored)";
    if (!confirm(`Run a Claude ↔ NotebookLM debate over ${subj}? Claude's picks are added into the `
      + 'notebook so NotebookLM can judge them too; NLM reads each block-by-block, then Claude '
      + 'reconciles on opus. One prompt per side.')) return;
  }
  const btn = $('nlm-challenge'); if (btn) btn.disabled = true;
  appendMsg({ role: 'q', text: '⚖️ Debate the finalists — where do Claude and NotebookLM agree/disagree per block, and what reconciles them?' });
  setBusy(true, 'NotebookLM is re-reading the finalists; Claude will argue back block by block…');
  const tabAt = activeTab;
  const body = explicit ? { doc_ids: docIds } : (useSel ? { doc_ids: [...docSelection] } : {});
  const res = await api(`/api/tabs/${activeTab}/nlm-challenge`, { method: 'POST', body: JSON.stringify(body) });
  setBusy(false); if (btn) btn.disabled = false;
  if (activeTab !== tabAt) return res;
  if (res.error && !(res.messages || []).length) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return res; }
  for (const m of res.messages || []) appendMsg(m);
  return res;
}
$('nlm-challenge').onclick = () => runChallenge();
async function pollRate() {
  clearTimeout(ratePoll);
  const tabAt = activeTab;
  const s = await api(`/api/tabs/${activeTab}/nlm-rate/status`);
  if (activeTab !== tabAt) return;
  const el = $('rate-status');
  el.classList.remove('muted', 'err');
  if (s.running) {
    const left = Math.max(0, (s.total || 0) - s.done);
    el.textContent = `📓 rating ${s.done}/${s.total || '…'}… (NotebookLM is slow — ~${Math.max(1, Math.round(left * 0.4))} min left; scores appear below as they land)`;
    refreshDocs();                       // scores fill in live as the palmares updates
    ratePoll = setTimeout(pollRate, 5000);
  } else if (s.total) {
    const unscored = (s.total || 0) - (s.rated || 0);
    if (s.rated) {
      el.textContent = `📓 done — rated ${s.rated}/${s.total}` + (unscored ? `, ${unscored} unscored (click again to retry those)` : ' ✓');
    } else {
      el.classList.add('err');
      el.textContent = `📓 NotebookLM returned no scores (0/${s.total}) — it may have rejected the queries or be rate-limited. Try again, or check 📓 connectivity.`;
    }
    refreshDocs();
  } else {
    el.classList.add('muted');
    el.textContent = '';
  }
}
$('q').onkeydown = e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) sendChat(false);
};

/* ---------- content viewer ---------- */
function openViewer(title, doc) {
  $('view-title').textContent = title;
  $('view-meta').textContent = doc.title || '';
  const parts = [];
  if (doc.text) parts.push(doc.text);              // upload-based benchmark
  for (const [label, key] of [['ABSTRACT', 'abstract'], ['FULL-TEXT DIGEST', 'digest'], ['CLAIMS', 'claims'], ['DESCRIPTION', 'description']]) {
    if (doc[key] && typeof doc[key] === 'string') parts.push(`===== ${label} =====\n${doc[key]}`);
  }
  $('view-body').textContent = parts.join('\n\n') || '(no stored text)';
  $('view-modal').classList.remove('hidden');
}
$('view-close').onclick = () => $('view-modal').classList.add('hidden');

/* ---------- lesson modal ---------- */
function openLessonModal(answerText) {
  lessonDefaultText = answerText;
  const checked = [...document.querySelectorAll('#skills input:checked')].map(i => i.value);
  if (checked.length) $('lesson-skill').value = checked[0];
  $('lesson-text').value = '';
  $('lesson-text').placeholder =
    'One concise, generalizable lesson distilled from this answer…';
  $('lesson-modal').classList.remove('hidden');
}
$('lesson-cancel').onclick = () => $('lesson-modal').classList.add('hidden');
$('lesson-save').onclick = async () => {
  const lesson = $('lesson-text').value.trim() || lessonDefaultText.slice(0, 2000);
  const res = await api('/api/lessons', {
    method: 'POST', body: JSON.stringify({ skill: $('lesson-skill').value, lesson }) });
  $('lesson-modal').classList.add('hidden');
  appendMsg({ role: 's', text: res.error
    ? `Lesson NOT saved: ${res.error}`
    : `Lesson appended to skill /${$('lesson-skill').value}.` });
};

/* ---------- notebook connect ---------- */
function renderNbChip(cfg) {
  const chip = $('nb-chip');
  const exportBtn = $('nb-export');
  const connected = !!(cfg && cfg.notebook_id);
  if (connected) {
    const n = (cfg.selected_source_ids || []).length;
    chip.textContent = `📓 ${cfg.notebook_title || cfg.notebook_id}` + (n ? ` · ${n} src` : ' · all src')
      + (cfg.auto_add ? ' · 📤auto' : '');
    chip.classList.remove('hidden');
  } else chip.classList.add('hidden');
  if (exportBtn) exportBtn.classList.toggle('hidden', !connected);
}

// Bulk-export benchmark + every fetched candidate into the connected notebook,
// walking past the source cap by offering follow-up notebooks. setStatus(text)
// reports progress (the modal status line or a transient chat message).
async function runNotebookSync(setStatus) {
  setStatus('Exporting benchmark + candidates into the notebook…');
  let res = await api(`/api/tabs/${activeTab}/notebook/sync`, { method: 'POST' });
  while (res.full) {
    setStatus(`Added ${res.added}; notebook is FULL, ${res.remaining} candidate(s) left.`);
    const st = await api(`/api/tabs/${activeTab}/state`);
    const title = (st.notebook && st.notebook.notebook_title) || 'notebook';
    if (!confirm(`Notebook "${title}" is full. Create a follow-up notebook and continue?`)) break;
    const created = await api(`/api/tabs/${activeTab}/notebook/create`, {
      method: 'POST', body: JSON.stringify({ title: nextSeriesTitle(title) }) });
    if (created.error) { setStatus(created.error); return res; }
    renderNbChip(created.notebook);
    res = await api(`/api/tabs/${activeTab}/notebook/sync`, { method: 'POST' });
  }
  setStatus(res.error
    ? `Error: ${res.error}`
    : `Done: ${res.added} exported` + (res.remaining ? `, ${res.remaining} remaining` : '')
      + ((res.errors || []).length ? ` — errors: ${res.errors.join('; ')}` : ''));
  refreshDocs();
  return res;
}

$('nb-export').onclick = async () => {
  if (!activeTab) return;
  const btn = $('nb-export');
  btn.disabled = true;
  await runNotebookSync(t => appendMsg({ role: 's', text: `📤 ${t}` }));
  btn.disabled = false;
};

function currentTabName() {
  const t = tabs.find(t => t.id === activeTab);
  return t ? t.name : '';
}

// next notebook in a rollover series: 'X' -> 'X (2)', 'X (2)' -> 'X (3)'.
function nextSeriesTitle(title) {
  const m = title.match(/ \((\d+)\)$/);
  return m ? title.replace(/ \(\d+\)$/, ` (${+m[1] + 1})`) : `${title} (2)`;
}

// (Re)load the notebook modal: the create box, the existing-notebook list, the
// restrict-query sources, and the "documents to add" picker. force=true busts the
// notebook-list cache so a just-created notebook (and fresh source counts) show up.
// selectId pre-selects a specific notebook (e.g. the one just created / added to);
// otherwise the tab's connected notebook is selected.
async function loadNbModal(force = false, selectId = null) {
  $('nb-modal').classList.remove('hidden');
  $('nb-list').textContent = 'Loading notebooks…';
  $('nb-sources-wrap').classList.add('hidden');
  $('nb-add-wrap').classList.add('hidden');
  const [res, st] = await Promise.all([
    api('/api/notebooks' + (force ? '?force=true' : '')),
    api(`/api/tabs/${activeTab}/state`),
  ]);
  nbState = { notebooks: res.notebooks || [], chosen: null, sources: [], selected: new Set() };
  const current = st.notebook;
  // auto-export defaults ON for a fresh connection (the notebook is meant to be a
  // Claude-quota-independent mirror of the tab's candidates); preserve the user's
  // choice when re-opening an already-configured notebook.
  $('nb-auto-add').checked = current ? !!current.auto_add : true;
  $('nb-sync-status').textContent = '';
  const wantId = selectId || (current && current.notebook_id);   // which notebook to pre-select
  const wrap = $('nb-list');
  wrap.innerHTML = '';
  if (res.error) { wrap.textContent = `NotebookLM unavailable: ${res.error}`; return; }
  if (!nbState.notebooks.length) { wrap.textContent = 'No notebooks yet — create one above.'; return; }
  // account-cap awareness: NotebookLM allows ~100 notebooks; at the cap, create fails
  const cnt = nbState.notebooks.length;
  const info = document.createElement('div');
  info.className = 'muted';
  info.textContent = `${cnt} notebook(s) in the account`
    + (cnt >= 95 ? ` — near NotebookLM's ~100 limit; 🗑 delete some to create/consolidate new ones.` : '');
  wrap.appendChild(info);
  for (const nb of nbState.notebooks) {
    const row = document.createElement('div');
    row.className = 'nb-row';
    const label = document.createElement('label');
    const r = document.createElement('input');
    r.type = 'radio'; r.name = 'nb'; r.value = nb.id;
    r.onchange = () => chooseNotebook(nb, current);
    label.appendChild(r);
    label.appendChild(document.createTextNode(
      ` ${nb.title}` + (nb.sources != null
        ? ` (${nb.sources}/50${nb.sources >= 50 ? ' — FULL' : ` · ${50 - nb.sources} free`})` : '')));
    row.appendChild(label);
    const del = document.createElement('button');
    del.className = 'btn small del'; del.textContent = '🗑';
    del.title = 'Delete this notebook permanently from NotebookLM (frees a slot toward the ~100 cap)';
    del.onclick = async (ev) => {
      ev.preventDefault();
      if (!confirm(`Delete notebook «${nb.title}» permanently from NotebookLM? Its sources are lost. `
        + 'This frees a slot so you can create / consolidate.')) return;
      del.disabled = true; del.textContent = '⏳';
      const dr = await api(`/api/notebooks/${encodeURIComponent(nb.id)}`, { method: 'DELETE' });
      if (dr.error) { alert(`Delete failed: ${dr.error}`); del.disabled = false; del.textContent = '🗑'; return; }
      addPrefill = null;
      await loadNbModal(true);                 // refresh list + count
    };
    row.appendChild(del);
    wrap.appendChild(row);
    if (nb.id === wantId) { r.checked = true; chooseNotebook(nb, current); }
  }
}

$('nb-btn').onclick = () => {
  addPrefill = null;
  $('nb-new-title').value = currentTabName();     // propose the tab name as the notebook name
  $('nb-create-status').textContent = '';
  loadNbModal();
};

// Open the notebook modal to add SPECIFIC documents, letting the user pick the
// destination notebook (or create a new one) before confirming.
function openAddToNotebook(ids, label) {
  addPrefill = { ids: new Set(ids), label };
  $('nb-new-title').value = currentTabName();
  $('nb-create-status').textContent = '';
  loadNbModal();
}

$('nb-create').onclick = async () => {
  const title = ($('nb-new-title').value || '').trim() || currentTabName();
  if (!title) { $('nb-create-status').textContent = 'Enter a name for the notebook.'; return; }
  $('nb-create-status').textContent = `Creating «${title}»…`;
  const res = await api(`/api/tabs/${activeTab}/notebook/create`, {
    method: 'POST', body: JSON.stringify({ title }) });
  if (res.error) { $('nb-create-status').textContent = `Error: ${res.error}`; return; }
  renderNbChip(res.notebook);
  await loadNbModal(true, res.notebook.notebook_id);   // reload, select the new notebook → it's the add target
  $('nb-create-status').textContent =
    `Created «${res.notebook.notebook_title}» — it's selected below; now pick documents to add to it.`;
};

// "documents to add" picker: benchmark + every fetched candidate as a checkbox,
// pre-checked from the candidate-list selection. The destination is the notebook
// SELECTED in the list above (nbState.chosen); a "✓ in notebook" marks documents
// already in THAT notebook.
function renderAddPicker() {
  const wrap = $('nb-add-wrap');
  const target = nbState.chosen;
  wrap.classList.toggle('hidden', !target);
  if (!target) return;
  const free = Math.max(0, 50 - (target.sources || 0));
  $('nb-add-target-name').textContent = `📓 ${target.title} (${free} free slot${free === 1 ? '' : 's'})`;
  $('nb-add-free').textContent = free ? `${free} free` : 'FULL';
  $('nb-add-bm').checked = !addPrefill;             // benchmark off for a single-doc quick add
  $('nb-add-status').textContent = addPrefill
    ? `Pick the destination notebook above (or create one), then click “Add selected” to add ${addPrefill.label}.`
    : '';
  const list = $('nb-add-list');
  list.innerHTML = '';
  const fetched = (lastDocs || []).filter(d => d.status === 'fetched');
  if (!fetched.length) { list.textContent = 'No fetched candidates yet.'; updateAddCount(); return; }
  for (const d of fetched) {
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = String(d.id);
    const inNb = d.nlm_source_notebook === target.id;
    cb.dataset.innb = inNb ? '1' : '0';             // fill-free skips already-in docs
    // pre-check the requested docs (per-row 📓➕) or the candidate-list selection; never ones already in
    cb.checked = !inNb && (addPrefill ? addPrefill.ids.has(d.id) : docSelection.has(d.id));
    cb.onchange = updateAddCount;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(
      ` ${d.number}` + (d.title ? ` — ${d.title}` : '') + (inNb ? ' ✓ in notebook' : '')));
    list.appendChild(label);
  }
  updateAddCount();
}
function updateAddCount() {
  const n = document.querySelectorAll('#nb-add-list input:checked').length;
  $('nb-add-count').textContent = `${n} selected` + ($('nb-add-bm').checked ? ' + benchmark' : '');
}
$('nb-add-bm').onchange = updateAddCount;
$('nb-add-all').onclick = () => {
  document.querySelectorAll('#nb-add-list input').forEach(i => i.checked = true); updateAddCount();
};
$('nb-add-fillfree').onclick = () => {
  // manual split: check exactly as many not-yet-added candidates as fit in this notebook
  const free = Math.max(0, 50 - ((nbState.chosen && nbState.chosen.sources) || 0));
  let n = 0;
  document.querySelectorAll('#nb-add-list input').forEach(i => {
    const take = i.dataset.innb === '0' && n < free;
    i.checked = take;
    if (take) n++;
  });
  updateAddCount();
};
$('nb-add-clear').onclick = () => {
  document.querySelectorAll('#nb-add-list input').forEach(i => i.checked = false); updateAddCount();
};
$('nb-add-selected').onclick = async () => {
  const target = nbState.chosen;
  if (!target) { $('nb-add-status').textContent = 'Select a notebook above first.'; return; }
  const ids = [...document.querySelectorAll('#nb-add-list input:checked')].map(i => +i.value);
  const includeBm = $('nb-add-bm').checked;
  if (!ids.length && !includeBm) { $('nb-add-status').textContent = 'Nothing checked.'; return; }
  let msg = '';
  const r = await runAddToNotebook({ doc_ids: ids, include_benchmark: includeBm, notebook_id: target.id },
    t => { msg = t; });
  addPrefill = null;
  await loadNbModal(true, r.notebookId);          // reselect the target → fresh source list + ✓ marks
  $('nb-add-status').textContent = msg;            // restore the result line (loadNbModal cleared it)
};
$('nb-add-allfetched').onclick = async () => {
  const target = nbState.chosen;
  if (!target) { $('nb-add-status').textContent = 'Select a notebook above first.'; return; }
  const ids = (lastDocs || []).filter(d => d.status === 'fetched').map(d => d.id);
  let msg = '';
  const r = await runAddToNotebook({ doc_ids: ids, include_benchmark: true, notebook_id: target.id },
    t => { msg = t; });
  addPrefill = null;
  await loadNbModal(true, r.notebookId);
  $('nb-add-status').textContent = msg;
};

// Push a CHOSEN payload {doc_ids, include_benchmark, notebook_id?} into a notebook,
// walking past the source cap by offering a follow-up notebook (the payload's
// notebook_id is retargeted to the rollover so the rest land there). Returns the
// response plus notebookId = the notebook it ended on. Mirrors runNotebookSync.
async function runAddToNotebook(payload, setStatus) {
  setStatus('Adding to the notebook…');
  payload = { ...payload };
  let res = await api(`/api/tabs/${activeTab}/notebook/add-selected`,
    { method: 'POST', body: JSON.stringify(payload) });
  let notebookId = res.notebook_id || payload.notebook_id;
  while (res.full) {
    setStatus(`Added ${res.added}; notebook is FULL, ${res.remaining} left.`);
    const title = res.notebook_title || 'notebook';
    if (!confirm(`Notebook "${title}" is full. Create a follow-up notebook and continue?`)) break;
    const created = await api(`/api/tabs/${activeTab}/notebook/create`, {
      method: 'POST', body: JSON.stringify({ title: nextSeriesTitle(title) }) });
    if (created.error) {
      setStatus(`Couldn't create a follow-up notebook: ${created.error}. Your account is likely at `
        + `the ~100-notebook cap — 🗑 delete unused notebooks (or sources) to free space, then retry.`);
      await refreshDocs(); loadNbTitles();
      return { ...res, notebookId, createError: created.error };
    }
    renderNbChip(created.notebook);
    notebookId = created.notebook.notebook_id;
    payload.notebook_id = notebookId;             // retarget the rest to the new notebook
    res = await api(`/api/tabs/${activeTab}/notebook/add-selected`,
      { method: 'POST', body: JSON.stringify(payload) });
    notebookId = res.notebook_id || notebookId;
  }
  // Distinguish the FULL case from "already there" — a full notebook with added=0 used to
  // read "nothing new to add", which looked like a silent no-op.
  let status;
  if (res.error) status = `Error: ${res.error}`;
  else if (res.added) status = `Done: ${res.added} added to «${res.notebook_title || ''}»`
    + (res.remaining ? `, ${res.remaining} remaining` : '');
  else if (res.full) status = `Not added — «${res.notebook_title || ''}» is FULL (50/50 sources).`
    + (res.remaining ? ` ${res.remaining} still not in NLM.` : '');
  else status = `Nothing new to add — already in «${res.notebook_title || ''}»`;
  if (res.full) status += ` Free space first: 🗑 delete duplicate sources or unused notebooks `
    + `(account is at the ~100-notebook cap), then add them.`;
  if ((res.errors || []).length) status += ` — errors: ${res.errors.join('; ')}`;
  setStatus(status);
  await refreshDocs();
  loadNbTitles();                                   // a rollover notebook may be new → refresh badge names
  return { ...res, notebookId };
}

async function chooseNotebook(nb, current) {
  nbState.chosen = nb;
  renderAddPicker();                              // this notebook is now the add destination
  $('nb-sources-wrap').classList.remove('hidden');
  $('nb-sources').textContent = 'Loading sources…';
  // force=true so a source just added to this notebook shows up immediately
  const res = await api(`/api/sources?notebook_id=${encodeURIComponent(nb.id)}&force=true`);
  nbState.sources = res.sources || [];
  const preselected = (current && current.notebook_id === nb.id)
    ? new Set(current.selected_source_ids || []) : new Set();
  nbState.selected = preselected;
  const wrap = $('nb-sources');
  wrap.innerHTML = '';
  if (res.error) { wrap.textContent = `Error: ${res.error}`; return; }
  for (const s of nbState.sources) {
    const row = document.createElement('div');
    row.className = 'src-row';
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = s.id;
    cb.checked = preselected.size ? preselected.has(s.id) : false;
    cb.onchange = () => {
      cb.checked ? nbState.selected.add(s.id) : nbState.selected.delete(s.id);
      updateSrcCount();
    };
    label.appendChild(cb);
    label.appendChild(document.createTextNode(' ' + s.title));
    row.appendChild(label);
    const del = document.createElement('button');
    del.className = 'btn small del'; del.textContent = '🗑';
    del.title = 'Delete this source permanently from the notebook (frees a slot toward the 50-source cap)';
    del.onclick = async () => {
      if (!confirm(`Delete source «${s.title}» permanently from «${nb.title}»?`)) return;
      del.disabled = true; del.textContent = '⏳';
      const r = await api(`/api/tabs/${activeTab}/notebook/source-delete`,
        { method: 'POST', body: JSON.stringify({ notebook_id: nb.id, source_ids: [s.id] }) });
      if (r.error) { alert(`Delete failed: ${r.error}`); del.disabled = false; del.textContent = '🗑'; return; }
      await loadNbModal(true, nb.id);      // rebuild list (fresh count) + reselect → reloads sources
      await refreshDocs();                 // a deleted source may flip a candidate to 📭 not in NLM
    };
    row.appendChild(del);
    wrap.appendChild(row);
  }
  updateSrcCount();
}
function updateSrcCount() {
  $('src-count').textContent = nbState.selected.size
    ? `${nbState.selected.size} of ${nbState.sources.length} selected`
    : `all ${nbState.sources.length} (no restriction)`;
}
$('src-all').onclick = () => {
  nbState.selected = new Set(nbState.sources.map(s => s.id));
  document.querySelectorAll('#nb-sources input').forEach(i => i.checked = true);
  updateSrcCount();
};
$('src-none').onclick = () => {
  nbState.selected = new Set();
  document.querySelectorAll('#nb-sources input').forEach(i => i.checked = false);
  updateSrcCount();
};
$('nb-resync').onclick = async () => {
  const btn = $('nb-resync'); const rep = $('nb-resync-report');
  btn.disabled = true; const label = btn.textContent; btn.textContent = '♻️ Resyncing…';
  rep.textContent = 'Reading the notebooks’ real sources and reconciling…';
  const r = await api(`/api/tabs/${activeTab}/notebook/resync`, { method: 'POST', body: JSON.stringify({}) });
  btn.disabled = false; btn.textContent = label;
  if (r.error && !r.ok) { rep.textContent = `Error: ${r.error}`; return; }
  rep.innerHTML = '';
  const line = document.createElement('div');
  line.textContent = `✓ ${r.in_nlm}/${r.total} candidate(s) in NLM `
    + `(${r.retracked} re-tracked, ${r.cleared} cleared) across ${r.scanned} notebook(s).`;
  rep.appendChild(line);
  if ((r.duplicates || []).length) {
    const h = document.createElement('div');
    h.className = 'strong';
    h.textContent = `⚠ ${r.duplicates.length} duplicated candidate(s), ${r.dup_copies} extra copy/copies — delete extras to free space:`;
    rep.appendChild(h);
    for (const d of r.duplicates) {
      const row = document.createElement('div'); row.className = 'dup-row';
      row.appendChild(document.createTextNode(`${d.number}: in `));
      // keep the first copy, offer to delete each of the others
      d.locations.forEach((loc, i) => {
        const tag = document.createElement('span'); tag.className = 'chip';
        tag.textContent = nbTitleById[loc.notebook_id] || loc.notebook_title || loc.notebook_id;
        row.appendChild(tag);
        if (i > 0) {
          const del = document.createElement('button');
          del.className = 'btn small del'; del.textContent = '🗑';
          del.title = `Delete this copy of ${d.number} from «${loc.notebook_title}»`;
          del.onclick = async () => {
            if (!confirm(`Delete the copy of ${d.number} in «${loc.notebook_title}»?`)) return;
            del.disabled = true; del.textContent = '⏳';
            const dr = await api(`/api/tabs/${activeTab}/notebook/source-delete`,
              { method: 'POST', body: JSON.stringify({ notebook_id: loc.notebook_id, source_ids: [loc.source_id] }) });
            if (dr.error) { alert(`Delete failed: ${dr.error}`); del.disabled = false; del.textContent = '🗑'; return; }
            del.textContent = '✓ deleted'; tag.style.textDecoration = 'line-through';
          };
          row.appendChild(del);
        }
      });
      rep.appendChild(row);
    }
  }
  if ((r.errors || []).length) {
    const e = document.createElement('div'); e.className = 'muted';
    e.textContent = 'Scan issues: ' + r.errors.join('; ');
    rep.appendChild(e);
  }
  await loadNbModal(true);   // refresh source counts
  refreshDocs();             // re-tracked candidates now show their notebook badge
};
$('nb-import').onclick = async () => {
  if (!confirm('Import the notebook’s sources into this tab? Patent numbers become ' +
               'fetched candidates; other sources are imported as text documents. ' +
               'Already-present sources are skipped.')) return;
  $('nb-sync-status').textContent = 'Importing sources from the notebook…';
  const res = await api(`/api/tabs/${activeTab}/notebook/import`, { method: 'POST' });
  $('nb-sync-status').textContent = res.error
    ? `Error: ${res.error}`
    : `Imported: ${res.patents_added} patent(s) (fetching full text), ${res.text_added} text source(s)`
      + (res.skipped ? `, ${res.skipped} skipped` : '')
      + ((res.errors || []).length ? ` — errors: ${res.errors.join('; ')}` : '');
  refreshDocs();
};
$('nb-cancel').onclick = () => { addPrefill = null; $('nb-modal').classList.add('hidden'); };
$('nb-disconnect').onclick = async () => {
  await api(`/api/tabs/${activeTab}/notebook`, {
    method: 'PUT', body: JSON.stringify({ notebook_id: null, source_ids: [] }) });
  $('nb-modal').classList.add('hidden');
  renderNbChip(null);
};
$('nb-save').onclick = async () => {
  if (!nbState.chosen) { $('nb-modal').classList.add('hidden'); return; }
  const autoAdd = $('nb-auto-add').checked;
  const res = await api(`/api/tabs/${activeTab}/notebook`, {
    method: 'PUT', body: JSON.stringify({
      notebook_id: nbState.chosen.id,
      notebook_title: nbState.chosen.title,
      source_ids: [...nbState.selected],
      auto_add: autoAdd,
    }) });
  $('nb-modal').classList.add('hidden');
  renderNbChip(res.notebook);
  // with auto-export on, immediately push the tab's existing benchmark+candidates
  // (auto_add only mirrors FUTURE additions, so backfill the current ones now).
  if (autoAdd && res.notebook && res.notebook.notebook_id) {
    await runNotebookSync(t => appendMsg({ role: 's', text: `📤 ${t}` }));
  }
};

/* ---------- collapsible panes (give the chat more room) ---------- */
const PANE_OF = { bm: 'pane-bm', docs: 'pane-docs' };
const COLLAPSE_CLASS = { bm: 'bm-collapsed', docs: 'cand-collapsed' };
let layout = {};
try { layout = JSON.parse(localStorage.getItem('pb-layout') || '{}'); } catch {}

// chat folds: hide the model/skills toolbar or the question box to maximise the answer
const CHAT_FOLD = { bar: { cls: 'bar-hidden', btn: 'toggle-bar' },
                    q:   { cls: 'q-hidden',   btn: 'toggle-q' } };
function applyLayout() {
  const main = $('main');
  for (const key of Object.keys(PANE_OF)) {
    const collapsed = !!layout[key];
    const pane = $(PANE_OF[key]);
    if (pane) pane.classList.toggle('collapsed', collapsed);
    main.classList.toggle(COLLAPSE_CLASS[key], collapsed);
    const btn = document.querySelector(`.collapse-btn[data-pane="${key}"]`);
    if (btn) { btn.textContent = collapsed ? '▸' : '▾'; }
  }
  const chatPane = document.querySelector('.pane-chat');
  for (const [key, { cls, btn }] of Object.entries(CHAT_FOLD)) {
    const hidden = !!layout[btn];
    if (chatPane) chatPane.classList.toggle(cls, hidden);
    const b = $(btn);
    if (b) b.classList.toggle('active', hidden);
  }
}
function togglePane(key) {
  layout[key] = !layout[key];
  localStorage.setItem('pb-layout', JSON.stringify(layout));
  applyLayout();
}
for (const btn of document.querySelectorAll('.collapse-btn')) {
  btn.onclick = () => togglePane(btn.dataset.pane);
}
for (const { btn } of Object.values(CHAT_FOLD)) {
  const b = $(btn);
  if (b) b.onclick = () => togglePane(btn);
}
// a collapsed strip is itself clickable to expand again
for (const key of Object.keys(PANE_OF)) {
  const pane = $(PANE_OF[key]);
  if (pane) pane.addEventListener('click', e => {
    if (pane.classList.contains('collapsed') && !e.target.closest('.collapse-btn')) togglePane(key);
  });
}
applyLayout();

/* ---------- boot ---------- */
(async () => {
  await loadSkills();
  await Promise.all([loadHealth(), loadTabs()]);
})();
