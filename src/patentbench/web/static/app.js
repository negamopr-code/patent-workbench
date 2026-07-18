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
let combiResult = null;         // last computed { pairs, mand, add, ... } for 🧩 Combi (null = not run)
let combiMotivations = {};      // persisted pair verdicts: 'loId-hiId' → {combinable, reason, model}
let bestPartnerById = new Map();// docId → its best complementary partner pair (inline hint)
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
  return s ? s.value : (skillsMeta.default_read_model || 'claude-sonnet-4-6');
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
  combiMotivations = st.combi_motivations || {};
  combiResult = null;                       // recompute combos per tab on demand
  renderBenchmark(st.benchmark);
  renderDocs(st.documents || []);
  renderChat(st.messages || []);
  renderNbChip(st.notebook);
  loadNbTitles();                 // fill in per-doc "in which notebook" badges (non-blocking)
  scheduleDocsPoll(st.documents || []);
  pollRate();                     // resume showing progress if an NLM rating job is in flight
  pollRead();                     // resume showing progress if a Claude deep-read is in flight
  attachPipeline();               // re-attach / offer ▶️ Resume if a pipeline job is in flight
  rehydrateCombi(id);             // restore the 🔎 investigation panel from stored coverage
}

// The 🔎 panel is otherwise pure client state (a scan's response held in memory), so a page
// reload empties it even though every verdict is safe in the DB. Re-derive the last findings
// from stored coverage so they survive a refresh / tab switch.
async function rehydrateCombi(tabAtCall) {
  const r = await api(`/api/tabs/${tabAtCall}/combi-results`);
  if (activeTab !== tabAtCall || r.error || !r.has_results) return;
  combiScan = r;
  renderCombiScanPanel();
}

/* ---------- benchmark (reference document) ---------- */
// Re-pull just the benchmark view (e.g. after figure captioning finishes).
async function refreshBenchmark(tabAtCall = activeTab, tries = 0) {
  const st = await api(`/api/tabs/${tabAtCall}/state`);
  if (activeTab !== tabAtCall || st.error) return;
  renderBenchmark(st.benchmark);
  const bm = st.benchmark;
  if (bm && bm.number && !bm.text && (bm.figures_n === 0 || bm.figures_n == null)
      && bm.figures_total && tries < 60)
    setTimeout(() => refreshBenchmark(tabAtCall, tries + 1), 6000);
}
function renderBenchmark(bm) {
  clearTimeout(bmPoll);
  currentBm = bm;
  const card = $('bm-card');
  const setup = $('bm-setup');
  if (!bm) {
    card.classList.add('hidden');
    setup.classList.remove('hidden');
    $('bm-status').textContent = '';
    const xr = $('bm-xrefs'); if (xr) xr.innerHTML = '';
    return;
  }
  setup.classList.add('hidden');
  card.classList.remove('hidden');
  card.innerHTML = '';
  loadBenchmarkXrefs(bm);   // patent numbers named here that live in other tabs

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
      openViewer(full.number || full.title || 'Benchmark', full,
                 `/api/tabs/${activeTab}/benchmark/figure`);
    };
    row.appendChild(view);
  }
  // 🔬 Decompose: a claim held as ONE feature can never be combination-analysed (🧩 combi
  // needs each document to contribute an element the other lacks). Proposes the split into
  // the editor for review — nothing is saved or scored until the user accepts.
  if (bm.status === 'ready') {
    const dec = document.createElement('button');
    dec.className = 'btn small'; dec.textContent = '🔬 Decompose into elements';
    dec.title = 'Split the claimed invention into its separable ELEMENTS, each becoming a weighted feature. '
      + 'Coverage is judged per element, so this is what makes 🧩 2-document combination analysis possible at all '
      + '(one monolithic feature can never be split between two documents). '
      + 'Proposes only — you review and edit before anything is saved or scored.';
    dec.onclick = () => decomposeBenchmark(bm);
    row.appendChild(dec);
  }
  if (bm.source === 'features' || (bm.features || []).length) {
    const edit = document.createElement('button');
    edit.className = 'btn small'; edit.textContent = '✏️ Edit features';
    edit.title = bm.source === 'features'
      ? 'Add / remove / re-weight the target features and re-save'
      : 'Add / remove / re-weight the features ranking against this document (the document stays)';
    edit.onclick = () => openFeatureEditor(bm);
    row.appendChild(edit);
  }
  // number-based benchmark (has a number, not an upload/feature spec) can have its
  // drawing sheets vision-read so the benchmark's figures are groundable too
  if (bm.status === 'ready' && bm.number && !bm.text) {
    if (bm.figures_n > 0) {
      const tag = document.createElement('span');
      tag.className = 'chip'; tag.textContent = `🖼 ${bm.figures_n} figures`;
      tag.title = 'Benchmark drawings vision-read into its text — groundable by figure number.';
      row.appendChild(tag);
    } else {
      const fb = document.createElement('button');
      fb.className = 'btn small'; fb.textContent = '🖼 Read figures';
      fb.title = 'Vision-read the benchmark’s drawing sheets into its text.';
      fb.onclick = async () => {
        fb.disabled = true; fb.textContent = '🖼 reading…';
        const r = await api(`/api/tabs/${activeTab}/benchmark/figures`, { method: 'POST' });
        if (r.error) { fb.disabled = false; fb.textContent = `error: ${r.error}`; return; }
        setTimeout(() => refreshBenchmark(), 6000);
      };
      row.appendChild(fb);
    }
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
      const isA = (f.kind || 'M') === 'A';
      chip.className = 'chip feat-chip clickable' + (isA ? ' feat-a' : '');
      chip.textContent = (isA ? `A·SL${f.sl || 5} ` : 'M ') + `${f.name} ·${'★'.repeat(f.weight)}`;
      chip.title = (isA ? `Additional feature — bonus only (stretch level ${f.sl || 5}/10)` : 'Mandatory feature — drives the base score')
        + '\n\nClick → every document with this feature + full comments';
      chip.onclick = () => openFeatureModal(f.name, f.weight, f.kind || 'M', null);
      fl.appendChild(chip);
    }
    card.appendChild(fl);
  }
  // Always-available "add a feature" window: APPENDS one weighted feature without
  // touching the existing benchmark (non-destructive) — including when the benchmark
  // is a document, where the features rank candidates against it.
  if (bm.status === 'ready' || bm.source === 'features') card.appendChild(buildAddFeatureBox());
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

// M/A kind selector + SL (stretch level) input, shared by the row editor and the add box.
// M = mandatory (drives the base score); A = additional (presence raises the score via the
// ➕ additional read, absence never lowers it); SL 1–10 = how far the argument may stretch.
function buildKindSl(kind = 'M', sl = 5) {
  const ksel = document.createElement('select');
  ksel.className = 'feat-kind';
  ksel.title = 'M = mandatory (base score)   ·   A = additional (bonus only — never penalises)';
  for (const k of ['M', 'A']) {
    const o = document.createElement('option');
    o.value = k; o.textContent = k === 'M' ? 'M (must)' : 'A (add’l)';
    if (k === kind) o.selected = true;
    ksel.appendChild(o);
  }
  const slin = document.createElement('input');
  slin.type = 'number'; slin.min = 1; slin.max = 10; slin.value = sl;
  slin.className = 'feat-sl'; slin.title = 'Stretch level 1–10 (only for A): how far the argument may be stretched';
  slin.style.width = '3.4em';
  const sync = () => { slin.style.display = ksel.value === 'A' ? '' : 'none'; };
  ksel.onchange = sync; sync();
  return { ksel, slin };
}
/* grow a textarea to fit its content (capped by the CSS max-height) */
function autoGrow(ta) {
  const fit = () => { ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px'; };
  ta.addEventListener('input', fit);
  // run once content is in the DOM so scrollHeight is measurable
  setTimeout(fit, 0);
  return ta;
}
/* one-by-one weighted feature rows */
function addFeatureRow(name = '', weight = 1, kind = 'M', sl = 5) {
  const wrap = $('bm-feat-rows');
  const row = document.createElement('div');
  row.className = 'bm-feat-row';
  const txt = document.createElement('textarea');
  txt.className = 'feat-name'; txt.rows = 2; txt.maxLength = 4000; txt.value = name;
  txt.placeholder = 'A feature a matching document must disclose… (paste freely — multi-line OK)';
  autoGrow(txt);
  const sel = document.createElement('select');
  sel.className = 'feat-weight';
  sel.title = 'Importance weight — decisive when candidates tie on points';
  for (let w = 1; w <= 5; w++) {
    const o = document.createElement('option');
    o.value = w; o.textContent = '★'.repeat(w) + ` (${w})`;
    if (w === weight) o.selected = true;
    sel.appendChild(o);
  }
  const { ksel, slin } = buildKindSl(kind, sl);
  // 🔗 link this feature into the cross-tab knowledge graph (LLM suggests, you confirm)
  const link = document.createElement('button');
  link.className = 'btn small feat-link'; link.textContent = '🔗';
  link.title = 'Classify this feature and link it to the cross-tab knowledge graph '
             + '(field › block › function › option) — reuses a matching node if one exists';
  const del = document.createElement('button');
  del.className = 'btn small del'; del.textContent = '🗑'; del.title = 'Remove this feature';
  del.onclick = () => { row.remove(); if (!wrap.children.length) addFeatureRow(); };
  // textarea on its own full-width line; weight/kind/delete stacked BELOW it so a
  // narrow pane never squeezes the text box into an unwritable sliver.
  const controls = document.createElement('div');
  controls.className = 'feat-controls';
  controls.append(sel, ksel, slin, link, del);
  const hint = document.createElement('div');
  hint.className = 'feat-link-hint';
  link.onclick = () => linkFeatureRow(txt.value.trim(), hint);
  row.append(txt, controls, hint);
  wrap.appendChild(row);
  return txt;
}
function collectFeatureRows() {
  return [...document.querySelectorAll('#bm-feat-rows .bm-feat-row')]
    .map(r => ({ name: r.querySelector('.feat-name').value.trim(),
                 weight: parseInt(r.querySelector('.feat-weight').value, 10) || 1,
                 kind: r.querySelector('.feat-kind').value,
                 sl: parseInt(r.querySelector('.feat-sl').value, 10) || 5 }))
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
  const txt = document.createElement('textarea');
  txt.rows = 2; txt.maxLength = 4000; txt.className = 'feat-name add-feat-name';
  txt.placeholder = 'A feature a matching document must disclose… (paste freely — multi-line OK, Ctrl+Enter to add)';
  autoGrow(txt);
  const sel = document.createElement('select');
  sel.className = 'feat-weight';
  sel.title = 'Importance weight — decisive when candidates tie on points';
  for (let w = 1; w <= 5; w++) {
    const o = document.createElement('option');
    o.value = w; o.textContent = '★'.repeat(w) + ` (${w})`;
    sel.appendChild(o);
  }
  const { ksel, slin } = buildKindSl('M', 5);
  const add = document.createElement('button');
  add.className = 'btn small primary'; add.textContent = 'Add';
  const submit = async () => {
    const name = txt.value.trim();
    if (!name) { txt.focus(); return; }
    const res = await api(`/api/tabs/${activeTab}/benchmark/features/add`, {
      method: 'POST', body: JSON.stringify({ name, weight: parseInt(sel.value, 10) || 1,
                                             kind: ksel.value, sl: parseInt(slin.value, 10) || 5 }) });
    if (res.error) { $('bm-status').textContent = res.error; return; }
    $('bm-status').textContent = '';
    renderBenchmark(res.benchmark);   // re-render shows the appended feature + a fresh empty input
  };
  add.onclick = submit;
  // plain Enter = newline (features can be multi-line); Ctrl/⌘+Enter submits
  txt.onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); } };
  // textarea full-width on top, controls below — stays writable in a narrow pane
  const controls = document.createElement('div');
  controls.className = 'feat-controls';
  controls.append(sel, ksel, slin, add);
  row.append(txt, controls);
  box.append(lbl, row);
  return box;
}

// 🔬 Propose a decomposition of the claimed invention into separable elements, then load
// them into the feature editor for REVIEW. Nothing is stored or scored until the user
// clicks save — a bad split must never silently poison the whole candidate list.
// Set when a 🔬 decomposition was started FROM the 🔎 investigation: approving the elements
// resumes the scan, so the approval gate costs a click rather than losing the trail.
let combiScanAfterSave = false;

async function decomposeBenchmark(bm, { skipConfirm = false, thenScan = false,
                                        source: forceSource = null } = {}) {
  if (!activeTab) return;
  const feats = bm.features || [];
  const mand = feats.filter(f => (f.kind || 'M') !== 'A');
  const addF = feats.filter(f => (f.kind || 'M') === 'A');
  // Once the mandatory elements are already granular, re-splitting them would re-cut the
  // claim and throw away wording that was reviewed and accepted — so only the additional
  // features are split. Prefer the user's own features as the source (that IS the claim, in
  // their words); fall back to the benchmark document's claims.
  const split = forceSource === 'additional' || (mand.length > 2 && addF.length);
  const source = forceSource || (split ? 'additional' : (mand.length ? 'features' : 'benchmark'));
  const what = source === 'additional'
             ? `${addF.length} additional feature(s) (your ${mand.length} mandatory elements are already split and stay as they are)`
             : mand.length ? `${mand.length} mandatory feature(s)` : 'the benchmark\'s claims';
  if (!skipConfirm && !confirm(`🔬 Decompose ${what} into separable elements?\n\n`
      + `One cheap call. The proposed elements open in the editor for you to review, edit `
      + `and re-weight — NOTHING is saved or scored until you click save.\n\n`
      + (mand.length === 1
          ? `Note: you currently have ONE mandatory feature, so 🧩 combi can never find a `
          + `pair (a single feature cannot be split between two documents). Decomposing is `
          + `what makes 2-document coverage possible.\n\n` : '')
      + `Continue?`)) return;
  setBusy(true, 'Decomposing the claimed invention into elements');
  const res = await api(`/api/tabs/${activeTab}/benchmark/decompose`, {
    method: 'POST', body: JSON.stringify({ source, model: readModelValue() }) });
  setBusy(false);
  if (res.error) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
  const els = res.elements || [];
  if (!els.length) { appendMsg({ role: 's', text: 'Decomposition returned no elements.' }); return; }
  // Load into the editor as a PROPOSAL — the existing save button is the approval gate.
  $('bm-setup').classList.remove('hidden');
  $('bm-features').open = true;
  const rows = $('bm-feat-rows');
  rows.innerHTML = '';
  // elements carry their own kind/SL: the additional feature is split too, and its
  // elements inherit its stretch level
  for (const e of els) addFeatureRow(e.name, e.weight, e.kind || 'M', e.sl || 5);
  combiScanAfterSave = thenScan;      // resume the investigation once these are approved
  $('bm-status').textContent =
    `🔬 Proposed ${els.length} element(s) from ${res.source === 'features' ? 'your features' : 'the benchmark claims'} `
    + `(${res.model || 'model'}). REVIEW and edit them — nothing is saved or scored until you click save below.`
    + (thenScan ? ' Saving them continues the 🔎 2-document investigation automatically.' : '');
  // The editor lives in the 🎯 Benchmark pane, which the user may have COLLAPSED (persisted
  // in localStorage) — and they are looking at the chat, where they clicked. Force the pane
  // open AND show the proposal where the click happened; otherwise the result is invisible
  // and the run reads as "nothing happened".
  expandPane('bm');
  renderDecomposeProposal(els, res, thenScan);
  $('bm-features').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// Show a 🔬 proposal in the CHAT pane — where the user clicked — so approving it never
// means hunting for an editor in another pane. Editing still happens in the benchmark
// pane's rows; this is the fast path plus the visible receipt.
function renderDecomposeProposal(els, res, thenScan) {
  const panel = $('combi-panel');
  panel.classList.remove('hidden');
  panel.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'combi-head';
  head.innerHTML = `<b>🔬 Proposed ${els.length} element(s)</b> `
    + `<span class="muted">${res.mandatory || 0} mandatory`
    + (res.additional ? ` + ${res.additional} additional` : '')
    + ` from ${res.source === 'features' ? 'your feature text' : 'the benchmark claims'} `
    + `(${res.model || 'model'}) — nothing is saved or scored yet. Review below, or edit them in the `
    + `🎯 Benchmark pane.</span>`;
  panel.appendChild(head);
  let m = 0, a = 0;
  for (const e of els) {
    const isA = (e.kind || 'M') === 'A';
    const label = isA ? `A${++a} ·SL${e.sl || 5}` : `M${++m}`;
    const r = document.createElement('div');
    r.className = 'combi-row';
    r.innerHTML = `<span class="chip${isA ? ' feat-a' : ''}">${label} ·${'★'.repeat(e.weight)}</span> ${esc(e.name)}`;
    panel.appendChild(r);
  }
  const actions = document.createElement('div');
  actions.className = 'combi-actions';
  const ok = document.createElement('button');
  ok.className = 'btn small primary';
  ok.textContent = thenScan ? `✅ Accept ${els.length} & run the investigation`
                            : `✅ Accept ${els.length} element(s)`;
  ok.title = 'Save these as the benchmark\'s mandatory elements' + (thenScan ? ', then run the 2-document coverage investigation.' : '.');
  ok.onclick = () => { $('bm-feat-set').click(); };   // the one save path — no duplicate logic
  actions.appendChild(ok);
  const edit = document.createElement('button');
  edit.className = 'btn small';
  edit.textContent = '✏️ Edit them first';
  edit.title = 'Jump to the rows in the 🎯 Benchmark pane to reword, re-weight or delete elements before saving.';
  edit.onclick = () => { expandPane('bm'); $('bm-features').scrollIntoView({ behavior: 'smooth', block: 'center' }); };
  actions.appendChild(edit);
  panel.appendChild(actions);
  appendMsg({ role: 's', text: `🔬 Proposed ${els.length} element(s) from the claimed invention `
    + `(${res.model || 'model'}). They are listed above the chat and loaded into the 🎯 Benchmark `
    + `pane's editor — nothing is saved or scored yet. Review them, then click `
    + `"✅ Accept"${thenScan ? ' to save and continue the 🔎 2-document investigation' : ''}.` });
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
    for (const f of feats) addFeatureRow(f.name, f.weight, f.kind || 'M', f.sl || 5);
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
  // Approving a 🔬 decomposition that was started FROM the 🔎 investigation resumes it —
  // the split was only ever a means to that end, so don't make the user re-find the button.
  if (combiScanAfterSave) {
    combiScanAfterSave = false;
    const mand = ((res.benchmark && res.benchmark.features) || [])
      .filter(f => (f.kind || 'M') !== 'A');
    if (mand.length >= 2) await runCombiScan();
    else appendMsg({ role: 's', text: `🔎 Investigation not resumed: the saved benchmark has `
      + `${mand.length} mandatory element(s), and a 2-document combination needs at least 2.` });
  }
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
  // Identical file-set already transcribed elsewhere → offer reuse instead of re-OCR.
  if (res.reuse) {
    const r = res.reuse;
    const m = r.text_model ? ` by ${r.text_model.replace('claude-', '')}` : '';
    if (confirm(`These exact files were already transcribed${m} in tab “${r.tab_name || '?'}” ` +
                `(${r.chars} chars). Reuse that transcription instead of re-running OCR?`)) {
      const rr = await api(`/api/tabs/${activeTab}/benchmark/reuse`, { method: 'POST' });
      $('bm-status').textContent = rr.error ? `Error: ${rr.error}` : '';
      renderBenchmark(rr.benchmark || res.benchmark);
      return;
    }
    const rr = await api(`/api/tabs/${activeTab}/benchmark/transcribe`, {
      method: 'POST', body: JSON.stringify({ reading_model: readModelValue() }) });
    $('bm-status').textContent = rr.error ? `Error: ${rr.error}` : '';
    renderBenchmark(rr.benchmark || res.benchmark);
    return;
  }
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
    allTabs: $('use-all-tabs').checked,
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
  setReadModel(p.readModel || skillsMeta.default_read_model || 'claude-sonnet-4-6');
  const want = new Set(p.skills || defaultSkills());
  document.querySelectorAll('#skills input').forEach(i => { i.checked = want.has(i.value); });
  $('use-docs').checked = p.useDocs !== false;
  $('use-all-tabs').checked = !!p.allTabs;
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
  setReadModel(skillsMeta.default_read_model || 'claude-sonnet-4-6');
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
  $('use-all-tabs').onchange = savePrefs;
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
// CONSENSUS: both engines independently rate it a strong match. NLM's endorsement = it
// named the doc in the shortlist (shortlisted) OR gave it a high per-candidate nlm_score;
// Claude's = a high full-text score. When they AGREE, the doc earns a bonus that TAPERS by
// NLM's best-first rank, so the score itself separates the ties: NLM's #1 → +0.4 (8 → 8.4),
// #2 → +0.3, #3 → +0.2, #4 → +0.1, floored at +0.1. The cap (<0.5) keeps base score dominant
// — a consensus 8 never overtakes a genuine 8.5/9. Surfaced as a 🤝 sticker + the boosted score.
const CONSENSUS_MIN = 7, CONSENSUS_TOP = 0.4, CONSENSUS_STEP = 0.1, CONSENSUS_FLOOR = 0.1;
function nlmEndorsed(d) { return d.shortlisted === 1 || (d.nlm_score != null && d.nlm_score >= CONSENSUS_MIN); }
function claudeStrong(d) { return d.score != null && d.score >= CONSENSUS_MIN; }
function isConsensus(d) { return claudeStrong(d) && nlmEndorsed(d); }
// The consensus bonus is assigned by a doc's POSITION among consensus docs in the ranked
// list (1st → +0.4, 2nd → +0.3 …, floored at +0.1) — computed live in renderDocs as
// consensusBonusById. This separates them even when nlm_rank isn't populated yet, and
// becomes NLM-meaningful once a shortlist sets the order. (Not raw nlm_rank, which goes
// stale.) See renderDocs.
let consensusBonusById = new Map();
function consensusBonus(d) { return consensusBonusById.get(d.id) || 0; }
// ADDITIONAL (A-feature) bonus from the ➕ additional read: each PRESENT A-feature adds a
// weighted amount, STRETCH adds half, ABSENT adds nothing (never subtracts). Bounded so it
// refines the ranking without letting a weak base leap a tier. Shown as '(+0.6 add’l)'.
const ADD_UNIT = 0.3, ADD_CAP = 1.0;
function additionalBonus(d) {
  const a = d.additional_scores;
  if (!Array.isArray(a)) return 0;
  let b = 0;
  for (const f of a) {
    const full = ((f.weight || 1) / 5) * ADD_UNIT;
    if (f.status === 'present') b += full;
    else if (f.status === 'stretch') b += full * 0.5;
  }
  return Math.min(ADD_CAP, b);
}
// Weighted-feature ranking is driven by the feature LIST, whatever the benchmark's
// source: a document benchmark may carry features that annotate it, and those rank
// the candidates exactly the same way.
function featureMode() {
  return !!(currentBm && (currentBm.features || []).length);
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

/* ---------- 🧩 Combi: two documents that TOGETHER cover the benchmark ---------- */
const _SRANK = { yes: 2, partial: 1, no: 0 };
function _bestStatus(a, b) { return _SRANK[a] >= _SRANK[b] ? a : b; }
function _statusOf(d, name, kind) { const e = docFeatureEntry(d, name, kind); return e ? e.status : 'no'; }
function combiKey(aId, bId) { const [lo, hi] = [aId, bId].sort((x, y) => x - y); return `${lo}-${hi}`; }
// Compute, in code (free, no model call), every genuine 2-document combination from the
// stored per-feature verdicts. A pair is "complete" when the UNION fully discloses (YES)
// every MANDATORY feature, and only counts if BOTH docs uniquely contribute a mandatory YES
// the other lacks (a real combination, not one doc subsuming the other). Single-doc ratings
// are never touched — this only derives a separate combined rating.
function computeCombis() {
  const feats = (currentBm && currentBm.features) || [];
  const mand = feats.filter(f => (f.kind || 'M') !== 'A');
  const add = feats.filter(f => (f.kind || 'M') === 'A');
  bestPartnerById = new Map();
  if (!featureMode() || !mand.length) return { pairs: [], mand, add, ready: false };
  const docs = lastDocs.filter(d => d.status === 'fetched' && Array.isArray(d.feature_scores));
  const totalMandW = mand.reduce((s, f) => s + (f.weight || 1), 0);
  const totalAddW = add.reduce((s, f) => s + (f.weight || 1), 0);
  const pairs = [];
  for (let i = 0; i < docs.length; i++) {
    for (let j = i + 1; j < docs.length; j++) {
      const A = docs[i], B = docs[j];
      let mandW = 0, mandCov = 0, complete = true;
      const contribA = [], contribB = [];
      for (const f of mand) {
        const sa = _statusOf(A, f.name, 'M'), sb = _statusOf(B, f.name, 'M');
        const u = _bestStatus(sa, sb);
        if (u === 'yes') { mandW += (f.weight || 1); mandCov++; } else complete = false;
        if (u === 'partial') mandW += (f.weight || 1) * 0.5;
        if (sa === 'yes' && sb !== 'yes') contribA.push(f.name);
        else if (sb === 'yes' && sa !== 'yes') contribB.push(f.name);
      }
      if (!contribA.length || !contribB.length) continue;   // not a genuine combination
      let addW = 0, addCov = 0;
      for (const f of add) {
        const u = _bestStatus(_statusOf(A, f.name, 'A'), _statusOf(B, f.name, 'A'));
        if (u === 'yes') { addW += (f.weight || 1); addCov++; } else if (u === 'partial') addW += (f.weight || 1) * 0.5;
      }
      pairs.push({ a: A, b: B, complete, mandW, mandCov, mandTotal: mand.length, totalMandW,
        addW, addCov, addTotal: add.length, totalAddW, combinedRating: mandW + addW,
        contribA, contribB });
    }
  }
  pairs.sort((x, y) => (y.complete - x.complete) || (y.mandW - x.mandW) || (y.addW - x.addW)
    || (((combinedScore(y.a) || 0) + (combinedScore(y.b) || 0)) - ((combinedScore(x.a) || 0) + (combinedScore(x.b) || 0))));
  // best partner per document (complete pairs win), for the inline hint on each doc row
  for (const p of pairs) {
    for (const [self, other] of [[p.a, p.b], [p.b, p.a]]) {
      const cur = bestPartnerById.get(self.id);
      if (!cur || (p.complete && !cur.complete) || (p.complete === cur.complete && p.combinedRating > cur.combinedRating)) {
        bestPartnerById.set(self.id, { partner: other, complete: p.complete, combinedRating: p.combinedRating,
          mandCov: p.mandCov, mandTotal: p.mandTotal, addCov: p.addCov, addTotal: p.addTotal });
      }
    }
  }
  return { pairs, mand, add, totalMandW, totalAddW, ready: true };
}

// 🔎 COMBI INVESTIGATION — the TOOL finds the pair; the user never picks D1/D2.
// Two stages, by design: stage 1 is cheap enough to span the WHOLE corpus (the pair that
// covers everything may rank nowhere near the top), stage 2 confirms the finalists against
// full text. Its rating is independent of every other score in the app.
let combiScan = null;

// A feature longer than this is still a BLOCK, not an element: it was pasted whole rather
// than split, so it can only ever be judged all-or-nothing.
const CHUNKY_FEATURE_CHARS = 400;

async function runCombiScan() {
  if (!activeTab) return;
  const mand = ((currentBm && currentBm.features) || []).filter(f => (f.kind || 'M') !== 'A');
  if (mand.length < 2) {
    // The investigation NEEDS the split, so offer to do it here rather than dead-ending on
    // "go press the other button". The approval gate still applies: the proposed elements
    // land in the editor, and the scan only resumes once you save them.
    if (!confirm(`🔎 2-document coverage needs at least TWO elements — this benchmark has ${mand.length}.\n\n`
        + `A single monolithic feature cannot be split between two documents, so no pair could `
        + `ever "cover everything" and the analysis would always come back empty.\n\n`
        + `Decompose the claim into its elements now?\n`
        + `One cheap call → you review/edit the elements → save → the investigation continues `
        + `automatically.`)) return;
    await decomposeBenchmark(currentBm, { skipConfirm: true, thenScan: true });
    return;                       // resumes from the save button once you approve
  }
  // SECOND prerequisite. Having ≥2 mandatory elements only means the run CAN happen — an
  // additional feature still held as one block silently guts the comparison it exists for:
  // once many documents cover the whole mandatory set, the additional elements are the only
  // thing left to separate them, and a block is judged all-or-nothing. Offer the split here
  // rather than leaving the user to know to press 🔬 themselves — but let them decline, since
  // a long feature is sometimes deliberate.
  const chunkyA = ((currentBm && currentBm.features) || [])
    .filter(f => (f.kind || 'M') === 'A' && (f.name || '').length > CHUNKY_FEATURE_CHARS);
  if (chunkyA.length) {
    const chars = chunkyA.reduce((s, f) => s + f.name.length, 0);
    if (confirm(`🔎 Before investigating — ${chunkyA.length} additional feature(s) are still `
        + `ONE block (${chars} chars).\n\n`
        + `Additional elements are what separate documents once they all cover the mandatory `
        + `set. Held as a block it is judged all-or-nothing: a document disclosing most of its `
        + `sub-features scores exactly the same as one disclosing barely any.\n\n`
        + `OK  — split it into elements first (one cheap call; you review and save, then the `
        + `investigation continues automatically)\n`
        + `Cancel — investigate anyway, judging it as one block`)) {
      await decomposeBenchmark(currentBm, { skipConfirm: true, thenScan: true,
                                            source: 'additional' });
      return;                     // resumes from the save button once you approve
    }
  }
  const eligible = (lastDocs || []).filter(d => d.status === 'fetched' && d.digest_len);
  const gap = (lastDocs || []).filter(d => d.status === 'fetched' && !d.digest_len).length;
  if (eligible.length < 2) { appendMsg({ role: 's', text: 'Need ≥2 candidates with a stored digest — 🔁 backfill first.' }); return; }
  const add = ((currentBm && currentBm.features) || []).filter(f => (f.kind || 'M') === 'A');
  const KEEP = Math.min(50, eligible.length);
  if (!confirm(`🔎 Investigate 2-document coverage\n\n`
      + `🩺 STAGE 0 — fast screen: cuts ${eligible.length} candidates down to ~${KEEP} worth a `
      + `closer look. Cheapest model, short digest extracts, GENEROUS (broad/implicit readings `
      + `count) — quick, and not a verdict.\n`
      + `🔎 STAGE 1 — then maps the ${mand.length} mandatory element(s)`
      + (add.length ? ` + ${add.length} additional` : '') + ` rigorously across the shortlist.\n`
      + `🔬 STAGE 2 — you then confirm the finalists on full text.\n\n`
      + (gap ? `⚠ ${gap} candidate(s) have NO digest and will be SKIPPED — 🔁 backfill to include them.\n\n` : '')
      + `Continue?`)) return;
  await combiScanCore();
}

// The screen→scan→render core, WITHOUT the confirms/prerequisite prompts. runCombiScan is
// the interactive wrapper; 'Best match' calls this directly to assess the combination after
// each 50-batch (it already confirmed, and must not prompt mid-flow). Returns true if it ran.
async function combiScanCore({ quiet = false } = {}) {
  const mand = ((currentBm && currentBm.features) || []).filter(f => (f.kind || 'M') !== 'A');
  if (mand.length < 2) {                       // can't combine without ≥2 elements — skip quietly
    if (!quiet) appendMsg({ role: 's', text: '🔎 Needs ≥2 mandatory elements — use 🔬 Decompose first.' });
    return false;
  }
  const eligible = (lastDocs || []).filter(d => d.status === 'fetched' && d.digest_len);
  if (eligible.length < 2) return false;
  const KEEP = Math.min(50, eligible.length);
  const btn = $('combi-scan'); if (btn) btn.disabled = true;
  setBusy(true, `🩺 Stage 0: fast screen over ${eligible.length} candidates`);
  const scr = await api(`/api/tabs/${activeTab}/combi-screen`,
                        { method: 'POST', body: JSON.stringify({ top_n: KEEP }) });
  if (scr.error) { setBusy(false); if (btn) btn.disabled = false; appendMsg({ role: 's', text: `Error: ${scr.error}` }); return false; }
  const ids = (scr.shortlist || []).map(s => s.id);
  await reloadChat();
  if (ids.length < 2) { setBusy(false); if (btn) btn.disabled = false; return false; }
  const pool = (scr.screened || 0) + (scr.reused || 0);
  setBusy(true, `🔎 Stage 1: element coverage over the ${ids.length} shortlisted `
    + `(of ${pool}${scr.reused ? `; ${scr.reused} reused, ${scr.screened} newly screened` : ''})`);
  const res = await api(`/api/tabs/${activeTab}/combi-scan`,
                        { method: 'POST', body: JSON.stringify({ doc_ids: ids, model: readModelValue() }) });
  setBusy(false); if (btn) btn.disabled = false;
  if (res.error) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return false; }
  combiScan = { ...res, screened: pool, dropped: scr.dropped };
  renderCombiScanPanel();
  await reloadChat();
  return true;
}

async function runCombiVerify({ ids: explicitIds = null } = {}) {
  if (!activeTab || !combiScan) return;
  let ids, blurb;
  if (explicitIds) {
    // Per-pair "🔬 verify this pair" — deep-read exactly the two documents the user picked.
    ids = [...new Set(explicitIds)];
    blurb = `the ${ids.length} document(s) of this pair`;
  } else {
    // Two kinds of document are worth a full read: the covers-all finalists (to CONFIRM
    // them citably) AND the high-coverage NEAR-MISSES (a digest 'no' on 1–2 Must elements
    // that a real read may FLIP to covers-all — exactly the docs the chat calls a best fit
    // while the digest scan holds them down). Take the top of each so near-misses aren't
    // starved when there are many full coverers. Rows already at full depth need no re-read.
    const rows = ((combiScan.matrix && combiScan.matrix.rows) || []).filter(x => x.depth !== 'full');
    const coverers = rows.filter(x => x.covers_all).sort((a, b) => b.mand_rating - a.mand_rating);
    const nearMiss = rows.filter(x => !x.covers_all).sort((a, b) => b.mand_rating - a.mand_rating);
    ids = [...new Set([...coverers.slice(0, 16), ...nearMiss.slice(0, 8)].map(x => x.id))];
    blurb = `${ids.length} finalist document(s)`
      + (coverers.length ? ` (${Math.min(16, coverers.length)} that cover every Must element` : '')
      + (nearMiss.length ? `${coverers.length ? ' + ' : ' ('}${Math.min(8, nearMiss.length)} near-misses that a full read may flip)` : (coverers.length ? ')' : ''));
  }
  if (!ids.length) { appendMsg({ role: 's', text: 'No documents to verify.' }); return; }
  if (!confirm(`🔎 STAGE 2 — confirm against FULL text\n\n`
      + `Re-reads the full primary text of ${blurb}, replacing their digest/screen verdicts `
      + `with citable ones.\n\n`
      + `This is a full read per document — the expensive, accurate pass. A solo hit or a pair `
      + `can legitimately FALL AWAY here if the full text doesn't bear the digest out.\n\nContinue?`)) return;
  setBusy(true, `Combi stage 2: full-text re-read of ${ids.length} finalists`);
  const res = await api(`/api/tabs/${activeTab}/combi-verify`, {
    method: 'POST', body: JSON.stringify({ doc_ids: ids, model: readModelValue() }) });
  setBusy(false);
  if (res.error) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
  combiScan = res;
  renderCombiScanPanel();
  await reloadChat();
}

// 🔎 Coverage MATRIX — rows = documents, columns = MANDATORY elements, cells = ✓/~/✗.
// Replaces the old auto-computed solo+pair lists: the user reads the grid and judges
// combinations themselves. Three standalone scores ride alongside each row: mandatory
// coverage, the ➕ additional-feature bonus, and the 🏆 whole-benchmark match. All data is
// the stored per-element coverage — no model call to render this.
const CELL = { yes: { t: '✓', c: 'cell-yes', title: 'discloses (literal/full)' },
               partial: { t: '~', c: 'cell-part', title: 'partial / implicit — still meets the limitation (anticipation standard)' },
               no: { t: '✗', c: 'cell-no', title: 'not disclosed' } };
const DEPTH_CHIP = { full: '📖 full text', digest: '🧾 digest', screen: '🩺 screen only' };

function renderCombiScanPanel() {
  const panel = $('combi-panel');
  panel.classList.remove('hidden');
  panel.innerHTML = '';
  const r = combiScan;
  const matrix = r.matrix || { columns: [], rows: [] };
  const cols = matrix.columns || [], rows = matrix.rows || [];
  const gapNames = matrix.gap_names || [];
  const gapSet = new Set(gapNames);
  const anchor = rows.find(x => x.is_anchor) || rows[0];
  const head = document.createElement('div');
  head.className = 'combi-head';
  head.innerHTML = matrix.covers_all_anchor
    ? `<b>🔎 Combination finder</b> <span class="muted">— top document <b>${esc(matrix.anchor || '')}</b> `
      + `covers EVERY Must element alone, so no second document is needed. Rows below are the `
      + `next-best documents. (${matrix.total_ranked || rows.length} ranked; showing ${rows.length}.)</span>`
    : `<b>🔎 Combination finder</b> <span class="muted">— a combination is TWO documents. `
      + `Row ① is the best document <b>${esc(matrix.anchor || '')}</b>; it is missing `
      + `<b>${gapNames.length}</b> Must element(s) (highlighted). The rows below are the closest `
      + `documents that FILL those gaps — pair ① with one of them. `
      + `✓ discloses · ~ partial · ✗ absent. (${matrix.total_ranked || rows.length} ranked; showing ${rows.length}.)</span>`;
  panel.appendChild(head);
  const actions = document.createElement('div');
  actions.className = 'combi-actions';
  const v = document.createElement('button');
  v.className = 'btn small';
  v.textContent = '🔬 Stage 2: confirm finalists on FULL text';
  v.title = 'Re-read the top full/near-full coverers against their primary text, replacing each digest cell with a citable one. A cell (and a would-be combination) can legitimately flip here.';
  v.onclick = () => runCombiVerify();
  actions.appendChild(v);
  panel.appendChild(actions);
  if (!cols.length || !rows.length) {
    const m = document.createElement('div');
    m.className = 'muted';
    m.textContent = cols.length
      ? 'No candidate has been assessed against the elements yet — run the 🩺 screen and 🔎 scan first.'
      : 'This benchmark has no mandatory elements to build a matrix from — 🔬 Decompose the claim into elements first.';
    panel.appendChild(m);
    return;
  }
  // Horizontal-scroll wrapper: many elements → wide table; the document column is sticky.
  const wrap = document.createElement('div');
  wrap.className = 'combi-matrix-wrap';
  const table = document.createElement('table');
  table.className = 'combi-matrix';
  // Header: document | one column per mandatory element (short label, weight, full name on hover) | scores | depth.
  const thead = document.createElement('thead');
  const htr = document.createElement('tr');
  const shortEl = (n, i) => `E${i + 1}`;
  const hasW = rows.some(r => r.w_total);
  htr.innerHTML = `<th class="mx-doc">Document</th>`
    + cols.map((c, i) => `<th class="mx-el${gapSet.has(c.name) ? ' mx-gapcol' : ''}" title="${esc(c.name)}${c.weight > 1 ? ` — weight ${c.weight}` : ''}${gapSet.has(c.name) ? ' — GAP: the top document is missing this; a partner must fill it' : ''}">${shortEl(c.name, i)}${gapSet.has(c.name) ? ' ⚠' : ''}${c.weight > 1 ? `<span class="mx-w">·${c.weight}</span>` : ''}</th>`).join('')
    + `<th class="mx-score" title="MUST coverage — the dominant ranking criterion: how many must-elements this document discloses on its own (weighted rating out of 10). All covered = a single-reference full coverer.">Must</th>`
    + `<th class="mx-score" title="Additional (A) bonus (weight/5 · 0.3, capped): each extra feature present adds points, absence never a penalty. Differentiates within a Must tier — never lifts a weaker-on-Must doc above a stronger one.">➕ A</th>`
    + (hasW ? `<th class="mx-score" title="Whole-document (W) bonus: elements of the benchmark document itself. Same bonus-only role as Additional.">📄 W</th>` : '')
    + `<th class="mx-score" title="🏆 Whole-benchmark best-match score already stored on the row (only present once ranked).">🏆 Match</th>`
    + `<th class="mx-depth" title="Read depth of this row's cells: 🩺 screen (generous guess) · 🧾 digest (summary) · 📖 full text (citable).">Depth</th>`;
  thead.appendChild(htr);
  table.appendChild(thead);
  const tbody = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');
    tr.className = row.is_anchor ? 'mx-anchor' : (row.covers_all ? 'mx-all' : '');
    const label = row.is_anchor
      ? ' <span class="chip mx-rank" title="The best document — the anchor of the combination.">① best</span>'
      : (row.covers_all ? ' <span class="chip ok mx-solo" title="Also covers every Must element on its own.">alone</span>' : '');
    const fills = (row.fills && row.fills.length)
      ? ` <span class="chip mx-fills" title="Fills the anchor's gap(s): ${row.fills.map(esc).join('; ')}">↳ fills ${row.fills.length}</span>` : '';
    const doc = `<td class="mx-doc"><b>${esc(row.number)}</b>${label}${fills}</td>`;
    const fillSet = new Set(row.fills || []);
    const cells = row.cells.map((s, i) => {
      const meta = CELL[s] || CELL.no;
      const isFill = !row.is_anchor && fillSet.has(cols[i].name) && s !== 'no';
      const isGap = row.is_anchor && gapSet.has(cols[i].name);
      return `<td class="mx-cell ${meta.c}${isFill ? ' mx-fillcell' : ''}${isGap ? ' mx-gapcell' : ''}" title="${esc(cols[i].name)}: ${meta.title}${isFill ? ' — FILLS the anchor gap' : ''}">${meta.t}</td>`;
    }).join('');
    const must = `<td class="mx-score" title="${row.mand_full}✓${row.mand_partial ? ` +${row.mand_partial}~` : ''} of ${row.mand_total} mandatory">${row.mand_full}${row.mand_partial ? `+${row.mand_partial}~` : ''}/${row.mand_total} <span class="muted">(${row.mand_rating})</span></td>`;
    const bonus = `<td class="mx-score">${row.add_total ? `${row.add_full}${row.add_partial ? `+${row.add_partial}~` : ''}/${row.add_total}${row.add_bonus ? ` <span class="muted">(+${row.add_bonus})</span>` : ''}` : '—'}</td>`;
    const wcell = hasW ? `<td class="mx-score">${row.w_total ? `${row.w_full}${row.w_partial ? `+${row.w_partial}~` : ''}/${row.w_total}${row.w_bonus ? ` <span class="muted">(+${row.w_bonus})</span>` : ''}` : '—'}</td>` : '';
    const score = `<td class="mx-score">${row.score != null ? esc(String(row.score)) : '—'}</td>`;
    const depth = `<td class="mx-depth"><span class="chip" style="cursor:${row.depth === 'full' ? 'default' : 'pointer'}" title="${row.depth === 'full' ? 'Confirmed on full primary text.' : 'Click to re-read THIS document on full text and replace its cells with citable verdicts.'}">${DEPTH_CHIP[row.depth] || row.depth}</span></td>`;
    tr.innerHTML = doc + cells + must + bonus + wcell + score + depth;
    if (row.depth !== 'full') {
      const chip = tr.querySelector('.mx-depth .chip');
      if (chip) chip.onclick = () => runCombiVerify({ ids: [row.id] });
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  panel.appendChild(wrap);
  // Element legend: the columns are E1…En to stay narrow; spell them out below.
  const legend = document.createElement('div');
  legend.className = 'combi-legend muted';
  legend.innerHTML = '<b>Elements:</b> ' + cols.map((c, i) => `E${i + 1} = ${esc(c.name)}${c.weight > 1 ? ` <span class="mx-w">·${c.weight}</span>` : ''}`).join(' &nbsp;·&nbsp; ');
  panel.appendChild(legend);
}

// Additional-coverage chip that DISTINGUISHES full (YES) from partial: '9/9' counting both
// equally hid that a pair could be 1 solid + 8 stretchy, ranking below a 7-solid pair and
// looking broken. Now full ✓ and partial ~ are shown apart, matching the bonus-based order.
function additionalChip(p) {
  if (!p.add_total) return '';
  const full = p.add_full ?? p.add_cov, part = p.add_partial ?? 0;
  const label = part ? `${full}✓ +${part}~ /${p.add_total}` : `${full}/${p.add_total}`;
  return `<span class="chip" title="Additional (bonus) elements: ✓ = full disclosure, ~ = partial/stretched (half credit). Ranking uses the weighted bonus (+${p.add_bonus || 0}), so several full disclosures beat many partial ones — which is why a lower ✓ count can rank above a higher raw count. Absence never lowers the rating.">➕ ${label} additional${p.add_bonus ? ` (+${p.add_bonus})` : ''}</span>`;
}

$('combi-scan').onclick = () => runCombiScan();
// 🔬 Decompose is ALSO in the chat toolbar, not only on the 🎯 Benchmark card — that card
// lives in a pane the user may have collapsed, so the button was effectively hidden.
$('decompose-btn').onclick = () => {
  if (!activeTab) return;
  if (!currentBm || currentBm.status !== 'ready') {
    appendMsg({ role: 's', text: 'Set a benchmark first — 🔬 Decompose splits the claimed invention into elements.' });
    return;
  }
  decomposeBenchmark(currentBm);
};

function runCombi() {
  combiResult = computeCombis();
  if (!combiResult.ready) {
    $('combi-panel').classList.remove('hidden');
    $('combi-panel').innerHTML = '<div class="muted">🧩 Combi needs a <b>feature-combination benchmark</b> with at least one mandatory feature, and candidates that have been deep-read (so per-feature verdicts exist).</div>';
    return;
  }
  renderCombiPanel();
  renderDocs(lastDocs);   // re-render so each row shows its best-partner hint
}

function renderCombiPanel() {
  const panel = $('combi-panel');
  panel.classList.remove('hidden');
  panel.innerHTML = '';
  const r = combiResult;
  const complete = r.pairs.filter(p => p.complete);
  const top = (complete.length ? complete : r.pairs).slice(0, 12);
  const head = document.createElement('div');
  head.className = 'combi-head';
  head.innerHTML = `<b>🧩 Best combinations</b> — two documents that together cover the benchmark `
    + `<span class="muted">(${complete.length} cover ALL ${r.mand.length} mandatory; combined rating is a separate hint — single scores unchanged)</span>`;
  panel.appendChild(head);
  const actions = document.createElement('div');
  actions.className = 'combi-actions';
  const judge = document.createElement('button');
  judge.className = 'btn small'; judge.id = 'combi-judge';
  judge.textContent = '⚖️ Check combinability (LLM)';
  judge.title = 'One cheap bulk pass over the top pairs’ digests: is each pair genuinely combinable (real motivation to combine)?';
  judge.onclick = () => judgeCombinability(top);
  actions.appendChild(judge);
  const close = document.createElement('button');
  close.className = 'btn small'; close.textContent = 'hide';
  close.onclick = () => { panel.classList.add('hidden'); combiResult = null; bestPartnerById = new Map(); renderDocs(lastDocs); };
  actions.appendChild(close);
  panel.appendChild(actions);
  if (!top.length) {
    const none = document.createElement('div'); none.className = 'muted';
    none.textContent = 'No genuine 2-document combination found (no pair where each adds a mandatory feature the other lacks).';
    panel.appendChild(none);
    return;
  }
  for (const p of top) combiRow(p, panel);
}

function combiRow(p, panel) {
  const row = document.createElement('div');
  row.className = 'combi-row' + (p.complete ? ' complete' : '');
  const title = document.createElement('div');
  title.className = 'combi-pair';
  for (const d of [p.a, p.b]) {
    const a = document.createElement('a');
    a.className = 'combi-doc'; a.textContent = d.number || d.title || ('#' + d.id);
    a.title = 'Jump to this document'; a.onclick = () => scrollToDoc(d.id);
    title.appendChild(a);
    if (d === p.a) { const plus = document.createElement('span'); plus.className = 'combi-plus'; plus.textContent = ' + '; title.appendChild(plus); }
  }
  const rating = document.createElement('span');
  rating.className = 'combi-rating';
  rating.textContent = `⚖ ${p.combinedRating.toFixed(1)}/${(p.totalMandW + p.totalAddW)}`;
  rating.title = 'Combined rating of the pair: Σ weight of mandatory features the union covers + additional coverage. Separate from each doc’s own score.';
  title.appendChild(rating);
  row.appendChild(title);
  const cov = document.createElement('div');
  cov.className = 'combi-cov muted';
  cov.innerHTML = (p.complete ? '✓ covers ALL mandatory' : `~ covers ${p.mandCov}/${p.mandTotal} mandatory`)
    + (p.addTotal ? ` · ${p.addCov}/${p.addTotal} additional` : '');
  row.appendChild(cov);
  const split = document.createElement('div');
  split.className = 'combi-split muted';
  split.innerHTML = `<span>${p.a.number || ('#' + p.a.id)} adds: ${p.contribA.join('; ') || '—'}</span>`
    + `<span>${p.b.number || ('#' + p.b.id)} adds: ${p.contribB.join('; ') || '—'}</span>`;
  row.appendChild(split);
  const v = combiMotivations[combiKey(p.a.id, p.b.id)];
  if (v) {
    const mv = document.createElement('div');
    mv.className = 'combi-motiv ' + (v.combinable ? 'ok' : 'no');
    mv.textContent = (v.combinable ? '✅ combinable — ' : '🚫 not combinable — ') + (v.reason || '');
    row.appendChild(mv);
  }
  panel.appendChild(row);
}

async function judgeCombinability(pairs) {
  const btn = $('combi-judge');
  if (btn) { btn.disabled = true; btn.textContent = '⚖️ judging…'; }
  const payload = { pairs: pairs.map(p => ({ a_id: p.a.id, b_id: p.b.id,
    a_features: p.contribA, b_features: p.contribB })) };
  const res = await api(`/api/tabs/${activeTab}/combi/motivation`,
    { method: 'POST', body: JSON.stringify(payload) });
  if (res.error) { if (btn) { btn.disabled = false; btn.textContent = '⚖️ Check combinability (LLM)'; } alert(res.error); return; }
  Object.assign(combiMotivations, res.results || {});
  renderCombiPanel();
}

// The candidate's DOCUMENT-match, 0–10: Claude's deep-read score of the candidate against
// the benchmark's document text (_benchmark_fulltext = its claims/description). null = unread.
function documentScore(d) { return d.score ?? null; }
// The candidate's FEATURE-match, normalized 0–10 from the weighted coverage. null when there
// are no features or the candidate has no per-feature verdicts yet.
function featureScore10(d) {
  const fst = featureStats(d);
  if (!fst || !fst.total) return null;
  return (fst.weighted / fst.total) * 10;
}
// BLENDED match: when a benchmark has BOTH a document and features, a candidate must match
// BOTH to rank top — average the two 0–10 signals. With only one present, use that one, so a
// feature-only benchmark (no document) and a document-only benchmark both still rank sensibly.
// Returns {value, doc, feat} | null.
function blendedMatch(d) {
  const doc = documentScore(d), feat = featureScore10(d);
  if (doc == null && feat == null) return null;
  if (doc != null && feat != null) return { value: (doc + feat) / 2, doc, feat };
  return { value: doc != null ? doc : feat, doc, feat };
}
function scoreSortValue(d, key) {
  // 🎯 Must-first: the unified key (covers-all-Must ≫ weighted-Must ≫ A-bonus ≫ W-bonus),
  // computed server-side from stored coverage. This is THE ranking — Must dominates, A/W
  // only differentiate within a tier. Un-assessed docs (no rank) sort last.
  if (key === 'must') return d.rank ? d.rank.key : -1;
  if (key === 'weighted') {
    const bm = blendedMatch(d);
    if (!bm) return -1;
    // primary = blended (doc+feature) match; tiebreak = #features literally matched
    const fst = featureStats(d);
    return bm.value * 1e6 + (fst ? fst.matched : 0) * 1e3 + (documentScore(d) ?? 0) * 10;
  }
  if (key === 'nlm') return d.nlm_score ?? -1;
  if (key === 'delta') return (d.score != null && d.nlm_score != null) ? Math.abs(d.score - d.nlm_score) : -1;
  if (key === 'claude') return d.score ?? -1;   // base only; consensus handled as a secondary sort + display taper
  const cs = combinedScore(d);
  return cs == null ? -1 : cs;   // 'combined' (default), base only
}
let lastDocs = [];
let lastRankedDocIds = [];   // doc ids in the last rendered ranking order
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
  const bmAt = (currentBm && currentBm.updated_at) || 0;     // last benchmark change
  const hasRead = d => d.status === 'fetched' && (d.verdict_len || d.score != null);
  // a read counts as current only if it's NEWER than the last benchmark change (else it's
  // stale — it never saw a feature you added — and must be re-read regardless of model).
  const fresh = d => (d.scored_at || 0) >= bmAt;
  const readAtLevel = d => hasRead(d) && fresh(d) && modelRank(d.score_model) <= modelRank(rm);
  const unread = allDocs.filter(d => d.status === 'fetched' && !readAtLevel(d)).length;
  const assessed = allDocs.filter(hasRead).length;
  const cont = $('claude-continue');
  if (cont) {
    const rmShort = rm.replace('claude-', '');
    cont.classList.toggle('hidden', !(unread || assessed));
    // Always reads as "▶️ Continue" so it's clearly the RESUME button (never the restart). With
    // leftovers it reads only those; with none, it re-ranks from stored (0 tokens).
    cont.textContent = unread ? `▶️ Continue read (${unread} left)`
                              : `▶️ Continue · re-rank ${assessed} (none left)`;
    cont.title = unread
      ? `RESUME without restarting: full-reads ONLY the ${unread} candidate(s) not yet read by `
        + `${rmShort} or a stronger model — never re-reads what's done, never restarts the whole corpus.`
      : `Every candidate is already read by ${rmShort} or stronger, so there's nothing new to read — `
        + `this re-ranks from the stored reads (0 tokens). To deliberately RE-READ specific docs `
        + `(e.g. switch the top opus picks to ${rmShort}), CHECK them and click 🏆 Deep compare — `
        + `it reads only the checked ones, not all ${assessed}.`;
  }
  if (!unfetched && docsFilter === 'unfetched') docsFilter = 'all';
  // NLM coverage: fetched candidates that are NOT a source in any notebook — these are
  // invisible to the 📓 NLM shortlist, so surface + bulk-add them.
  const notInNlm = allDocs.filter(d => d.status === 'fetched' && !d.nlm_source_notebook);
  const inNlm = (counts.fetched || 0) - notInNlm.length;
  if (!notInNlm.length && docsFilter === 'no-nlm') docsFilter = 'all';
  // DIGEST coverage: every digest-based tool (➕ additional read, ♻️ re-check, 🧩 combi)
  // silently SKIPS a candidate with no stored digest — so "all documents" quietly means
  // "all WITH a digest". Surface the gap; it is otherwise invisible.
  const noDigest = allDocs.filter(d => d.status === 'fetched' && !d.digest_len);
  if (!noDigest.length && docsFilter === 'no-digest') docsFilter = 'all';
  if (allDocs.length) {
    const bar = document.createElement('div');
    bar.className = 'docs-summary';
    bar.innerHTML =
      `<span class="chip ok" title="fetched & ready">✓ ${counts.fetched || 0}</span>`
      + (counts.pending ? `<span class="chip warn" title="still fetching">⏳ ${counts.pending}</span>` : '')
      + (counts.error ? `<span class="chip err" title="failed to fetch — check the number/kind code">⚠ ${counts.error}</span>` : '')
      + (counts.fetched ? `<span class="chip" title="fetched candidates that ARE a source in some NotebookLM notebook">📓 ${inNlm} in NLM</span>` : '')
      + (counts.fetched ? `<span class="chip${noDigest.length ? ' warn' : ''}" title="Candidates with a stored DIGEST — the scope of every digest-based tool (➕ additional read, ♻️ re-check, 🧩 combi). Those without one are silently skipped by all of them.">🧾 ${(counts.fetched || 0) - noDigest.length}/${counts.fetched} digested</span>` : '');
    if (noDigest.length) {
      const t = document.createElement('button');
      t.className = 'btn small';
      t.textContent = docsFilter === 'no-digest' ? '↩ show all' : `🧾 show ${noDigest.length} without a digest`;
      t.title = 'These fetched candidates have NO stored digest, so ➕ additional read, ♻️ re-check and 🧩 combi all skip them — a run over "all documents" silently excludes them.';
      t.onclick = () => { docsFilter = docsFilter === 'no-digest' ? 'all' : 'no-digest'; renderDocs(allDocs); };
      bar.appendChild(t);
      const bf = document.createElement('button');
      bf.className = 'btn small';
      bf.textContent = `🔁 backfill ${noDigest.length} missing digest(s)`;
      bf.title = 'Generate the missing digests so "all documents" really means all of them. Costs ONE cheap call per missing candidate — that is what a digest is. Never runs on its own.';
      bf.onclick = () => backfillDigests(noDigest.length);
      bar.appendChild(bf);
    }
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
    // Bulk pick of the 🤖-unrated: fetched candidates Claude has never scored. Ticking them
    // one by one is the tedious path this replaces — one click stages them all for
    // 🏆 Deep-analyse selected. 📓 NLM scores deliberately do NOT count as rated here:
    // "rated" means Claude has deep-read it (📓 NLM-rate all already skips its own rated ones).
    const unratedByClaude = allDocs.filter(d => d.status === 'fetched' && d.score == null);
    if (unratedByClaude.length) {
      const pick = document.createElement('button');
      pick.className = 'btn small';
      const fresh = unratedByClaude.filter(d => !docSelection.has(d.id));
      pick.textContent = `☑ select ${unratedByClaude.length} not rated by 🤖`;
      pick.disabled = !fresh.length;
      pick.title = fresh.length
        ? `Tick every fetched candidate 🤖 Claude has NOT scored yet (${fresh.length} not already checked), `
          + 'ADDING them to the current selection — then hit 🏆 Deep-analyse selected to read only those. '
          + 'Candidates 🤖 already scored are left untouched; a 📓 NLM score does not count as rated here.'
        : 'All not-yet-rated candidates are already checked.';
      pick.onclick = () => {
        for (const d of unratedByClaude) docSelection.add(d.id);
        // An active filter hides some of the fresh picks, and the render prunes the
        // selection to what it drew — so drop back to the full list to keep them all.
        docsFilter = 'all';
        renderDocs(allDocs);
      };
      bar.appendChild(pick);
    }
    // sort the palmares — feature mode adds the weighted key (and defaults to it)
    const fmode = featureMode();
    const hasRank = allDocs.some(d => d.rank);
    const effSort = (!docsSortTouched && fmode) ? (hasRank ? 'must' : 'weighted') : docsSort;
    if (fmode || allDocs.some(d => d.nlm_score != null)) {
      const sortSel = document.createElement('select');
      sortSel.className = 'sort-sel';
      sortSel.title = 'Rank candidates by';
      const opts = [['combined', '🥇 by combined'], ['claude', '🤖 by Claude'], ['nlm', '📓 by NLM'], ['delta', 'Δ by disagreement']];
      if (fmode) opts.unshift(['weighted', '⚖ by weighted features']);
      if (hasRank) opts.unshift(['must', '🎯 by Must-coverage']);
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

  // ranking ("palmares"): base score first; among equal base, CONSENSUS docs lead, then by
  // NLM's best-first order (nlm_rank), then id. Base score excludes the consensus taper so
  // there's no circularity (the taper is a display/position effect computed below).
  const sortKey = (!docsSortTouched && featureMode())
    ? (allDocs.some(d => d.rank) ? 'must' : 'weighted') : docsSort;
  const nlmRankOf = d => (d.nlm_rank != null ? d.nlm_rank : 1e9);
  let docs = [...allDocs].sort((a, b) =>
    scoreSortValue(b, sortKey) - scoreSortValue(a, sortKey)
    || additionalBonus(b) - additionalBonus(a)   // A-feature coverage refines within a base tier
    || (isConsensus(b) ? 1 : 0) - (isConsensus(a) ? 1 : 0)
    || nlmRankOf(a) - nlmRankOf(b)
    || a.id - b.id);
  // tapered consensus bonus by POSITION among consensus docs (1st → +0.4 … floor +0.1) — so
  // they always separate (8.4/8.3/8.2…), ordered by NLM rank once a shortlist sets it.
  consensusBonusById = new Map();
  let _ci = 0;
  for (const d of docs) if (isConsensus(d)) {
    consensusBonusById.set(d.id, Math.max(CONSENSUS_FLOOR, CONSENSUS_TOP - _ci * CONSENSUS_STEP));
    _ci++;
  }
  if (docsFilter === 'unfetched') docs = docs.filter(d => d.status !== 'fetched');
  if (docsFilter === 'no-nlm') docs = docs.filter(d => d.status === 'fetched' && !d.nlm_source_notebook);
  if (docsFilter === 'no-digest') docs = docs.filter(d => d.status === 'fetched' && !d.digest_len);
  // 1-based position among RANKED (scored) docs, so the user sees 1st / 2nd / 3rd explicitly.
  const rankIndex = new Map(); let _rp = 0;
  for (const d of docs) if (d.score != null || d.nlm_score != null) rankIndex.set(d.id, ++_rp);
  lastRankedDocIds = docs.map(d => d.id);   // ranked order, for ➕ additional read's top-N
  for (const d of docs) {
    const el = document.createElement('div');
    el.className = 'doc';
    el.dataset.docId = d.id;
    const row1 = document.createElement('div');
    row1.className = 'doc-row';
    if (d.status === 'fetched') {
      const sel = document.createElement('input');
      sel.type = 'checkbox';
      sel.checked = docSelection.has(d.id);
      sel.title = 'Select a candidate to (a) load its FULL primary text into the chat so Claude can quote real claims/[paragraphs], (b) scope 🏆 Deep compare to it, and (c) pick D1/D2 for ⚖️ Problem-solution (tick exactly two). None selected = whole list, clipped.';
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
      // Figures-read badge: figures are OPT-IN (vision cost), so a text-only read is
      // normal — but the deficiency must be visible at a glance, with the fix one
      // click away (run a detailed check including drawings on THIS document).
      const fg = document.createElement('span');
      if (d.figures_n > 0) {
        fg.className = 'chip fig-in';
        fg.textContent = `🖼 ${d.figures_n}`;
        fg.title = `${d.figures_n} figure(s) vision-read into the text — chat & deep-compare can cite them like paragraphs.`;
      } else if (d.figures_n === 0) {
        fg.className = 'chip fig-none';
        fg.textContent = '🖼 –';
        fg.title = 'No drawing sheets found for this document.';
      } else {
        fg.className = 'chip fig-out';
        fg.textContent = '🖼✗ figures not read';
        fg.title = 'Text-only: the drawing sheets were NOT vision-read — figure content is unknown to chat/deep-compare. '
          + 'Click to read the figures now (vision pass) for a detailed check including drawings.';
        fg.style.cursor = 'pointer';
        fg.onclick = async () => {
          fg.textContent = '🖼 reading…'; fg.onclick = null; fg.style.cursor = '';
          const r = await api(`/api/tabs/${activeTab}/documents/${d.id}/figures?reading_model=${encodeURIComponent(readModelValue())}`,
                              { method: 'POST' });
          if (r.error) { fg.textContent = `🖼 error`; fg.title = r.error; return; }
          pollFiguresDone(d.id);
        };
      }
      row1.appendChild(fg);
      // ↪ cross-tab provenance: this candidate was pulled in by 🏆 Best match from
      // another tab because it covers ≥1 of THIS tab's benchmark features — the
      // tooltip names exactly which ones (digest pre-check; deep read refines).
      if (d.source === 'cross-tab') {
        const xt = document.createElement('span');
        xt.className = 'chip xtab';
        const from = (tabs.find(t => t.id === d.origin_tab_id) || {}).name || 'another tab';
        xt.textContent = `↪ ${from}`;
        const covered = (d.feature_scores || [])
          .filter(f => f.status === 'yes' || f.status === 'partial')
          .map(f => `${f.name}${f.status === 'partial' ? ' (partial)' : ''}`);
        xt.title = `Pulled in by 🏆 Best match from tab «${from}» — covers benchmark feature(s): `
          + (covered.join('; ') || (d.score_note || 'see its note'))
          + '. Digest pre-check; the deep read assesses it in full.';
        row1.appendChild(xt);
      }
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
      const consensus = isConsensus(d);
      const pos = rankIndex.get(d.id);            // ordinal position in the ranking
      if (pos) parts.push(`<span class="rankpos">#${pos}</span>`);
      // BLENDED match leads when the benchmark has BOTH a document and features: the rank is
      // (document match + feature coverage)/2, so a candidate must do well on both. Shown with
      // its two parts so the number is never opaque.
      // 🎯 MUST-FIRST badge leads: covers-all-Must decides the tier, A/W are bonus chips.
      // This is the same key the matrix and chat rank by. When it's present it REPLACES the
      // old blended 🎯 (which conflated Must with bonus and mis-ranked full coverers).
      const r = d.rank;
      if (r) {
        const mtxt = `${r.mand_full}${r.mand_partial ? `+${r.mand_partial}~` : ''}/${r.mand_total}`;
        parts.push(r.covers_all
          ? `<span class="combined must-all" title="Covers EVERY Must element (${mtxt}; weighted ${r.mand_rating}/10). A single-reference full coverer — the top tier.">🎯 covers all Must <span class="muted">(${mtxt})</span></span>`
          : `<span class="combined must-gap" title="Must coverage ${mtxt}; weighted ${r.mand_rating}/10. A Must element is still uncovered, so it can't cover the invention alone — it may still combine with another document (see the matrix).">🎯 ${mtxt} Must <span class="muted">(${r.mand_rating})</span></span>`);
        if (r.add_total) parts.push(`<span class="addl" title="Additional (bonus) features: ${r.add_full}✓ ${r.add_partial}~ of ${r.add_total}. Raises rank within a Must tier; absence never lowers it.">➕ ${r.add_bonus ? `+${r.add_bonus}` : '0'}/${r.add_total}</span>`);
        if (r.w_total) parts.push(`<span class="addl wbonus" title="Whole-document features (elements of the benchmark document itself): ${r.w_full}✓ ${r.w_partial}~ of ${r.w_total}. Bonus only.">📄 ${r.w_bonus ? `+${r.w_bonus}` : '0'}/${r.w_total}</span>`);
      }
      const bmv = (featureMode() && !r) ? blendedMatch(d) : null;
      if (bmv && bmv.doc != null && bmv.feat != null) {
        parts.push(`<span class="combined" title="Blended match = (document ${bmv.doc.toFixed(1)} + features ${bmv.feat.toFixed(1)}) / 2. Both the benchmark document and its feature list count — a candidate must match both to rank top.">🎯 ${bmv.value.toFixed(1)} <span class="muted">(📄${bmv.doc.toFixed(1)} 🧩${bmv.feat.toFixed(1)})</span></span>`);
      } else if (d.score != null && d.nlm_score != null) {
        // combined ("common") score leads when both engines rated it
        parts.push(`<span class="combined">🥇 ${combinedScore(d).toFixed(1)}</span>`);
      }
      const addl = additionalBonus(d);
      if (d.score != null) {
        // base + 🤝 consensus taper + ➕ additional bonus, capped at 10
        const shown = Math.min(10, d.score + (consensus ? consensusBonus(d) : 0) + addl);
        parts.push(`🤖 ${shown.toFixed(1)}/10`);
      }
      if (d.nlm_score != null) parts.push(`📓 ${d.nlm_score}/10`);
      if (consensus) parts.push('<span class="consensus">🤝 agree</span>');
      if (addl > 0) parts.push(`<span class="addl">➕ +${addl.toFixed(1)} add’l</span>`);
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
      // ➕ additional-read per-feature chips: 🟢 present / 🟡 stretch / ⚪ absent (evidence in tooltip)
      if (Array.isArray(d.additional_scores) && d.additional_scores.length) {
        const ar = document.createElement('div');
        ar.className = 'addl-feats';
        for (const f of d.additional_scores) {
          const c = document.createElement('span');
          const icon = f.status === 'present' ? '🟢' : f.status === 'stretch' ? '🟡' : '⚪';
          c.className = 'chip addl-chip clickable ' + f.status;
          c.textContent = `${icon} ${f.name}`;
          c.title = `${f.status.toUpperCase()} (SL${f.sl || 5}, ★${f.weight || 1})`
            + (f.evidence ? ` — ${f.evidence}` : '') + '\n\nClick → every document with this feature + full comments';
          c.onclick = () => openFeatureModal(f.name, f.weight, 'A', d.id);
          ar.appendChild(c);
        }
        el.appendChild(ar);
      }
      // when + which model did the last full read (so you know what's stale)
      if (d.scored_at || d.score_model) {
        const r = document.createElement('div');
        r.className = 'read-meta';
        const when = d.scored_at ? new Date(d.scored_at * 1000).toLocaleString() : '—';
        r.textContent = `🤖 full-read ${when}` + (d.score_model ? ` · ${d.score_model.replace('claude-', '')}` : '');
        if (bmAt && (d.scored_at || 0) < bmAt) {        // read predates the current benchmark
          const s = document.createElement('span');
          s.className = 'stale-flag';
          s.textContent = ' ⏳ stale (benchmark changed)';
          s.title = 'This read predates your last benchmark change — it never checked the feature(s) you added since. ▶️ Continue re-reads it.';
          r.appendChild(s);
        }
        el.appendChild(r);
      }
    } else if (d.status === 'fetched') {
      // fetched but never full-read — make that visible so it's clearly pending a read
      const r = document.createElement('div');
      r.className = 'read-meta muted';
      const dm = d.digest_model ? ` · digest ${d.digest_model.replace('claude-', '')}` : '';
      const tm = d.text_model ? ` · OCR ${d.text_model.replace('claude-', '')}` : '';
      r.textContent = '🤖 not yet full-read' + dm + tm;
      el.appendChild(r);
    }
    if (d.status === 'fetched') el.appendChild(buildFiguresRow(d));
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
        c.className = 'chip feat-mark clickable ' + f.status;
        c.textContent = `${mark[f.status] || '?'} ${f.name} ·${'★'.repeat(f.weight || 1)}`;
        c.title = (f.note ? f.note + '\n\n' : '') + 'Click → every document with this feature + full comments';
        c.onclick = () => openFeatureModal(f.name, f.weight, 'M', d.id);
        chips.appendChild(c);
      }
      fw.appendChild(chips);
      el.appendChild(fw);
    }
    // 🧩 inline combi hint: this doc's best complementary partner (only after 🧩 Combi was run)
    const bp = bestPartnerById.get(d.id);
    if (bp) {
      const ch = document.createElement('div');
      ch.className = 'combi-hint' + (bp.complete ? ' complete' : '');
      const partLabel = bp.partner.number || bp.partner.title || ('#' + bp.partner.id);
      ch.innerHTML = `🧩 + <a class="combi-doc">${partLabel}</a> → `
        + (bp.complete ? 'all main' : `${bp.mandCov}/${bp.mandTotal} main`)
        + (bp.addTotal ? ` + ${bp.addCov}/${bp.addTotal} add` : '')
        + ` · combi <b>${bp.combinedRating.toFixed(1)}</b>`;
      ch.title = 'Best document to COMBINE with this one to cover the benchmark. Combined rating is a separate hint — it does not change either document’s own score. Click the number to jump.';
      ch.querySelector('.combi-doc').onclick = () => scrollToDoc(bp.partner.id);
      el.appendChild(ch);
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
        openViewer(full.number, full, `/api/tabs/${activeTab}/documents/${d.id}/figure`);
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
    chip.textContent = `🏆 ${docSelection.size} selected for deep analysis`
      + (docSelection.size === 2 ? ' · ⚖️ D1+D2 ready' : '');
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
  await maybePromptReuse(res);
  refreshDocs();
};

/* ---------- cross-tab reuse (a doc OCR'd/fetched in another tab) ---------- */
// When documents_add held back numbers already processed elsewhere, ASK before
// re-doing the work: reuse the stored body + digest, or fetch fresh.
function maybePromptReuse(res) {
  const reusable = (res && res.reusable) || [];
  if (!reusable.length) return Promise.resolve();
  return new Promise(resolve => {
    const body = $('reuse-modal-body');
    body.innerHTML = '';
    $('reuse-modal-sub').textContent =
      `${reusable.length} document(s) were already processed in another tab. ` +
      `Reuse the stored full text + digest, or re-fetch from scratch?`;
    for (const r of reusable) {
      const label = document.createElement('label');
      label.className = 'reuse-row';
      const cb = document.createElement('input');
      cb.type = 'checkbox'; cb.checked = true; cb.dataset.docId = r.doc_id;
      const models = [r.text_model && `OCR ${r.text_model.replace('claude-', '')}`,
                      r.digest_model && `digest ${r.digest_model.replace('claude-', '')}`]
                     .filter(Boolean).join(', ');
      const txt = document.createElement('span');
      txt.innerHTML = `<b>${r.number}</b> — in tab “${r.tab_name || '?'}”` +
        (models ? ` <span class="muted">(${models})</span>` : '');
      label.appendChild(cb); label.appendChild(txt);
      body.appendChild(label);
    }
    const finish = () => { $('reuse-modal').classList.add('hidden'); resolve(); };
    $('reuse-do').onclick = async () => {
      const cbs = [...body.querySelectorAll('input[type=checkbox]')];
      for (const cb of cbs) {
        const id = cb.dataset.docId;
        if (cb.checked) await api(`/api/tabs/${activeTab}/documents/${id}/reuse`, { method: 'POST' });
        else await api(`/api/tabs/${activeTab}/documents/${id}/refetch`, { method: 'POST' });
      }
      finish(); refreshDocs();
    };
    $('reuse-skip').onclick = async () => {
      for (const r of reusable)
        await api(`/api/tabs/${activeTab}/documents/${r.doc_id}/refetch`, { method: 'POST' });
      finish(); refreshDocs();
    };
    $('reuse-modal').classList.remove('hidden');
  });
}
$('reuse-modal').onclick = e => { if (e.target === $('reuse-modal')) $('reuse-modal').classList.add('hidden'); };

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
  await maybePromptReuse(res);
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
      chip.textContent = ({ model: '🧬 ', skill: '🧠 ', notebook: '📓 ', documents: '📚 ', benchmark: '🎯 ', xref: '🔗 ', 'tab-docs': '🗂 ', xtalk: '💬 ', psa: '⚖️ ' }[p.kind] || '') + p.title;
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
        all_tabs: $('use-all-tabs').checked,   // reuse every OTHER tab's fetched docs
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

// ---------- ⚖️ problem-solution approach ----------
// Two GLOBAL documents (stored server-side forever, shared by all tabs):
// method = the steps, format = the answer structure.

const psaDocs = { method: null, format: null };
const PSA_UI = { method: { btn: 'psa-method-btn', file: 'psa-file', icon: '📋', label: 'method…' },
                 format: { btn: 'psa-format-btn', file: 'psa-format-file', icon: '📑', label: 'format…' } };

async function refreshPsaDoc(kind) {
  const u = PSA_UI[kind];
  const r = await api(`/api/psa/${kind}`);
  psaDocs[kind] = r.ok ? r : null;
  $(u.btn).textContent = psaDocs[kind] ? `${u.icon} ${psaDocs[kind].name}` : `${u.icon} ${u.label}`;
  if (psaDocs[kind]) {
    $(u.btn).title = `${kind}: ${psaDocs[kind].name} (${psaDocs[kind].chars} chars), `
      + 'stored permanently and shared by ALL tabs. Click to replace.';
  }
}

for (const kind of Object.keys(PSA_UI)) {
  const u = PSA_UI[kind];
  $(u.btn).onclick = () => $(u.file).click();
  $(u.file).onchange = async () => {
    const f = $(u.file).files[0];
    $(u.file).value = '';
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f);
    const r = await api(`/api/psa/${kind}`, { method: 'POST', body: fd });
    if (r.error) { alert(`${kind} upload failed: ${r.error}`); return; }
    if (r.pending) {         // scanned PDF → background vision OCR; poll progress
      $(u.btn).textContent = `${u.icon} OCR…`;
      const timer = setInterval(async () => {
        const s = await api(`/api/psa/${kind}`);
        if (s.pending) { $(u.btn).textContent = `${u.icon} OCR ${s.progress || ''}…`; return; }
        clearInterval(timer);
        if (s.error) alert(`${kind} OCR failed: ${s.error}`);
        await refreshPsaDoc(kind);
      }, 3000);
      return;
    }
    await refreshPsaDoc(kind);
  };
}

async function runPsa(stretch) {
  if (!activeTab) return;
  if (!psaDocs.method) {
    alert('Upload the problem-solution methodology document first (📋 method…).');
    $('psa-file').click();
    return;
  }
  if (docSelection.size !== 2) {
    alert(`${stretch ? '🪄' : '⚖️'} needs exactly TWO candidates as D1 and D2.\n\n`
      + 'Tick their CHECKBOXES in the 📚 Candidates list (the box at the left of '
      + 'each fetched row) — first tick = D1, second tick = D2.\n\n'
      + `Currently selected: ${docSelection.size}.`);
    return;
  }
  const tabAtSend = activeTab;
  const nums = [...docSelection]
    .map(id => (lastDocs.find(d => d.id === id) || {}).number || `#${id}`).join(' + ');
  const head = stretch ? '🪄 Argumentation stretch (problem-solution approach)'
                       : '⚖️ Problem-solution approach';
  // BASIS = what the run assesses as the claimed invention. Explicit, never inferred:
  // it is confirmed before the call and named on the run message afterwards, so a run
  // is never a mystery about what it was based on.
  const basis = $('psa-basis').value;
  const basisText = basis === 'text' ? $('q').value.trim() : '';
  if (basis === 'text' && basisText.length < 20) {
    alert('Basis is ✍️ "Chat box text", but the chat box is empty (or too short).\n\n'
        + 'Paste the feature / claim text the approach should assess into the chat box '
        + 'below, or switch the basis selector to 🎯 Benchmark document.');
    $('q').focus();
    return;
  }
  const basisLabel = { benchmark: '🎯 benchmark document',
                       features: `🧩 benchmark features (${(currentBm && currentBm.features || []).length})`,
                       text: `✍️ pasted text (${basisText.length} chars)` }[basis];
  if (!confirm(`${head}\n\n`
      + `BASIS (the claimed invention assessed): ${basisLabel}\n`
      + (basis === 'text' ? `  "${basisText.slice(0, 160)}${basisText.length > 160 ? '…' : ''}"\n`
                          + '  The benchmark document will NOT be sent.\n' : '')
      + `\nD1/D2 (prior art): ${nums}\n`
      + `Method: ${psaDocs.method.name}${psaDocs.format ? `\nFormat: ${psaDocs.format.name}` : ''}\n\nRun?`)) return;
  appendMsg({ role: 'q', text: `${head} on ${nums} (basis: ${basisLabel}, method: ${psaDocs.method.name}`
    + (psaDocs.format ? `, format: ${psaDocs.format.name}` : '') + ')' });
  setBusy(true, stretch ? 'Argumentation stretch' : 'Problem-solution approach');
  const res = await api(`/api/tabs/${tabAtSend}/psa`, {
    method: 'POST',
    body: JSON.stringify({ doc_ids: [...docSelection], model: $('model').value,
                           use_discussions: $('psa-discuss').checked,
                           basis, basis_text: basisText || null,
                           stretch }),
  });
  setBusy(false);
  if (activeTab !== tabAtSend) return;
  if (res.error && !(res.messages || []).length) {
    appendMsg({ role: 's', text: `Error: ${res.error}` });
    return;
  }
  for (const m of res.messages || []) appendMsg(m);
}

$('psa-btn').onclick = () => runPsa(false);
$('psa-stretch-btn').onclick = () => runPsa(true);

refreshPsaDoc('method');
refreshPsaDoc('format');

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
    el.textContent = bestMatch
      ? `✓ batch read — assessing the 2-document combination…`
      : `✓ assessment stopped — see chat (▶️ Continue assesses any left)`;
    refreshDocs();
    reloadChat();
    if (bestMatch) afterBestMatchBatch();      // 🏆 chain: combination assessment + next-50 offer
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

// 🏆 Best match considers ALL relevant documents, including OTHER tabs: first a
// cheap digest scan of every other tab's fetched doc vs THIS benchmark; any doc
// covering ≥1 feature is pulled in as a real candidate (covered features indicated
// on its row), THEN the deep compare runs over the enlarged list. Negatives are
// cached server-side per benchmark, so repeat clicks only scan what's new.
async function crossTabScanThen(next) {
  const el = $('read-status'); const tabAt = activeTab;
  el.classList.remove('muted');
  el.textContent = '↪ checking other tabs for documents that cover benchmark features…';
  const r = await api(`/api/tabs/${activeTab}/cross-tab-scan`,
                      { method: 'POST', body: JSON.stringify({}) });
  if (r.error || !r.started) {
    // no benchmark / nothing new to scan / scan already running — never block the run
    el.textContent = r.error ? '' :
      (r.cached_skipped ? `↪ other tabs already scanned for this benchmark (${r.cached_skipped} cached) — nothing new` : '');
    next();
    return;
  }
  const poll = async () => {
    if (activeTab !== tabAt) return;               // user switched tabs — stop narrating
    const s = await api(`/api/tabs/${activeTab}/cross-tab-scan/status`);
    if (s.running) {
      el.textContent = `↪ scanning other tabs vs this benchmark… ${s.done}/${s.total} digests checked, `
        + `${(s.imported || []).length} pulled in so far`;
      setTimeout(poll, 2500);
      return;
    }
    const got = s.imported || [];
    el.textContent = got.length
      ? `↪ pulled ${got.length} document(s) from other tabs — each covers ≥1 benchmark feature; they join this run`
      : '↪ other tabs checked — no additional document covers a benchmark feature';
    await refreshDocs();
    reloadChat();                                  // the 🏆 system line lists what came in
    next();
  };
  poll();
}
// 🏆 Best match, batched: deep-read the next 50 candidates (most-promising first, by prior
// score), then assess the 2-document COMBINATION over everything read so far, then stop with
// a "next 50" affordance. Tokens are spent on the best candidates first; intermediate results
// land each round; you re-launch for the next 50 until satisfied.
const BEST_MATCH_BATCH = 50;
let bestMatch = null;   // { remaining } while a best-match batch is in flight; null otherwise

async function runBestMatch() {
  if (!activeTab) return;
  const sel = docSelection.size ? [...docSelection] : null;
  if (sel) {   // an explicit selection → assess exactly those, then combine (no cross-tab)
    runDeepCompare(sel);
    return;
  }
  if (!confirm(`🏆 Best match (batched)\n\n`
      + `Reads the next ${BEST_MATCH_BATCH} candidates IN THIS TAB in FULL vs the benchmark — `
      + `most-promising first (by current score) — then assesses the 2-document COMBINATION over `
      + `everything read so far.\n\n`
      + `Stops after the batch with the intermediate ranking + combinations; re-launch for the `
      + `next ${BEST_MATCH_BATCH}. Both the benchmark document AND its features drive the ranking.\n\n`
      + `(Does NOT pull documents from other tabs — use 🌐 Cross-tab for that.)\n\nStart?`)) return;
  {
    const res = await api(`/api/tabs/${activeTab}/deep-compare`, {
      method: 'POST',
      body: JSON.stringify({
        model: $('model').value,
        skills: [...document.querySelectorAll('#skills input:checked')].map(i => i.value),
        reading_model: readModelValue(),
        skip_scored: true,               // read only what's not yet fresh, top-50 by score
        batch: BEST_MATCH_BATCH,
      }) });
    if (res.error) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
    if (!res.started) {
      // nothing needed reading (all fresh) → go straight to the combination + re-rank
      bestMatch = { remaining: 0 };
      await afterBestMatchBatch();
      return;
    }
    bestMatch = { remaining: res.remaining_after || 0 };
    pollRead();                          // progress; completion triggers afterBestMatchBatch()
  }
}

// After a best-match deep-read batch finishes: assess the 2-document combination over
// everything read so far, then offer the next 50 (or say the corpus is exhausted).
async function afterBestMatchBatch() {
  const remaining = bestMatch ? bestMatch.remaining : 0;
  bestMatch = null;                      // clear before the (awaited) combi so a reload is clean
  await combiScanCore({ quiet: true });  // solo + pairs over all assessed so far
  if (remaining > 0) {
    appendMsg({ role: 's', text: `🏆 Best match: batch done — ranking + combinations updated over `
      + `the assessed set. ${remaining} candidate(s) remain; click 🏆 Best match again for the next `
      + `${Math.min(BEST_MATCH_BATCH, remaining)} (most-promising first).` });
  } else {
    appendMsg({ role: 's', text: `🏆 Best match: every candidate is now assessed — ranking and `
      + `2-document combinations are complete over the full set.` });
  }
}
$('best-match').onclick = () => runBestMatch();
// 🌐 Cross-tab pull is now OPT-IN — it used to fire automatically inside Best match, which
// surprised the user by importing other tabs' documents mid-investigation.
$('cross-tab').onclick = () => {
  if (!activeTab) return;
  if (!confirm(`🌐 Cross-tab pull\n\n`
      + `Scans EVERY other tab's fetched documents and pulls in any that cover ≥1 of this `
      + `benchmark's features (cheap digest pre-check), so they join this tab as candidates.\n\n`
      + `They then rank alongside your own. Start?`)) return;
  crossTabScanThen(() => { refreshDocs(); reloadChat(); });
};
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
  // ONLY explicit checkboxes mean "consolidate exactly these". With nothing checked we ALWAYS
  // funnel Claude's top-N — we do NOT fall back to the persisted shortlist (that silently
  // shrank the set to the 2-4 remembered picks instead of the 49 the user wanted).
  consolidateIds = docSelection.size ? [...docSelection] : [];
  const n = consolidateIds.length;
  const scored = (lastDocs || []).filter(d => d.score != null).length;
  const funnel = !n;
  $('consolidate-title').value = `Best picks — ${currentTabName()}`;
  $('consolidate-bm').checked = true;
  $('consolidate-status').textContent = '';
  $('consolidate-topn-row').style.display = funnel ? '' : 'none';   // N only matters in funnel mode
  const go = $('consolidate-go');
  if (funnel && !scored) {
    $('consolidate-info').textContent = 'Nothing is scored yet. Run 🏆 deep-compare (Claude ranks them) '
      + 'first, or tick candidates by hand, then reopen this.';
    go.disabled = true;
  } else if (funnel) {
    const k = Math.min(scored, +($('consolidate-topn').value || 49));
    $('consolidate-info').textContent = `🚀 Funnel: Claude's top ${k} of ${scored} scored candidate(s) `
      + 'go into ONE new notebook (the other rollover notebooks are deleted). '
      + 'Use 📥 to just put them in NotebookLM and stop, or the primary button to also pick the best.';
    go.disabled = false;
  } else if (n > 49) {
    $('consolidate-info').textContent = `${n} candidate(s) checked, but a notebook holds 49 candidates `
      + '+ the benchmark (50-source cap). Untick down to ≤49, then reopen this.';
    go.disabled = true;
  } else {
    $('consolidate-info').textContent = `${n} checked candidate(s) will be copied into a NEW notebook; `
      + 'the other rollover notebooks are deleted. Use 📥 to stop there, or the primary button to pick the best.';
    go.disabled = false;
  }
  $('consolidate-modal').classList.remove('hidden');
};
$('consolidate-cancel').onclick = () => $('consolidate-modal').classList.add('hidden');
// Launch the resumable BACKGROUND job. consolidateOnly=true → copy the docs into ONE
// notebook and STOP (no shortlist, no debate, no NLM query); false → full funnel.
async function launchConsolidate(consolidateOnly) {
  const title = ($('consolidate-title').value || '').trim();
  if (!title) { $('consolidate-status').textContent = 'Enter a name for the notebook.'; return; }
  const ids = consolidateIds;
  const includeBm = $('consolidate-bm').checked;
  // FUNNEL mode (no explicit finalists) → let the server auto-pick Claude's top_n.
  const body = ids.length
    ? { title, doc_ids: ids, include_benchmark: includeBm, consolidate_only: consolidateOnly }
    : { title, top_n: Math.max(1, Math.min(49, +($('consolidate-topn').value || 49))),
        include_benchmark: includeBm, consolidate_only: consolidateOnly };
  const btns = [$('consolidate-go'), $('consolidate-only')];
  btns.forEach(b => b.disabled = true);
  const res = await api(`/api/tabs/${activeTab}/pipeline`, {
    method: 'POST', body: JSON.stringify(body) });
  btns.forEach(b => b.disabled = false);
  if (res.error) { $('consolidate-status').textContent = `Error: ${res.error}`; return; }
  $('consolidate-modal').classList.add('hidden');
  pollPipeline();
}
$('consolidate-go').onclick = () => launchConsolidate(false);
$('consolidate-only').onclick = () => launchConsolidate(true);

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
// ♻️ Re-check: re-score the displayed top-N against the CURRENT benchmark from stored digests
// (no full-text re-read). For after a benchmark tweak — cheap, never downgrades via a slow read.
$('digest-rescore').onclick = async () => {
  if (!activeTab) return;
  const N = 49;
  const byId = new Map((lastDocs || []).map(d => [d.id, d]));
  const ids = lastRankedDocIds
    .filter(id => { const d = byId.get(id); return d && d.status === 'fetched' && d.digest_len; })
    .slice(0, N);
  if (!ids.length) { appendMsg({ role: 's', text: 'No candidates with a stored digest yet — run a 🏆 deep-compare / full read once first.' }); return; }
  if (!confirm(`Re-check the top ${ids.length} against the current benchmark from their digests?\n\nONE bulk pass, no full-text re-read. Scores get tagged ·digest.`)) return;
  const btn = $('digest-rescore'); btn.disabled = true;
  setBusy(true, `Re-checking top ${ids.length} from digests (no re-read)`);
  const res = await api(`/api/tabs/${activeTab}/digest-rescore`, {
    method: 'POST', body: JSON.stringify({ doc_ids: ids }) });
  setBusy(false); btn.disabled = false;
  if (res.error) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
  await refreshDocs();
  await reloadChat();
};
// ➕ Additional read: check the benchmark's A-features against the displayed top-N candidates
// in ONE bulk sonnet pass over their stored digests. Sends the top-N doc ids in ranked order.
$('additional-read').onclick = async () => {
  if (!activeTab) return;
  const N = 10;
  const byId = new Map((lastDocs || []).map(d => [d.id, d]));
  const ids = lastRankedDocIds                       // displayed ranking order
    .filter(id => { const d = byId.get(id); return d && d.status === 'fetched' && d.digest_len; })
    .slice(0, N);
  if (!ids.length) { appendMsg({ role: 's', text: 'No candidates with a stored digest yet — run a 🏆 deep-compare / full read first.' }); return; }
  const btn = $('additional-read'); btn.disabled = true;
  setBusy(true, `Additional read over top ${ids.length} (one bulk sonnet pass over digests)`);
  const res = await api(`/api/tabs/${activeTab}/additional-read`, {
    method: 'POST', body: JSON.stringify({ doc_ids: ids }) });
  setBusy(false); btn.disabled = false;
  if (res.error) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
  await refreshDocs();   // re-render with the new A-feature chips + bonus
  await reloadChat();
};
// 🔁 Generate the MISSING digests. A candidate without one is invisible to every
// digest-based tool, so this is what makes "all documents" actually mean all of them.
// One cheap call per missing doc — always user-triggered, never automatic.
async function backfillDigests(n) {
  if (!activeTab) return;
  if (!confirm(`🔁 Backfill ${n} missing digest(s)\n\n`
      + `These candidates are fetched but have NO digest, so ➕ additional read, ♻️ re-check `
      + `and 🧩 combi all skip them today.\n\n`
      + `Costs ~${n} cheap call(s), one per document. Continue?`)) return;
  setBusy(true, `Backfilling ${n} missing digest(s)`);
  const res = await api(`/api/tabs/${activeTab}/digest-backfill`, {
    method: 'POST', body: JSON.stringify({ model: readModelValue() }) });
  setBusy(false);
  if (res.error) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
  await refreshDocs();
  await reloadChat();
}
// ♻️ Re-check over EVERY candidate with a digest, not just the top-N: after a benchmark
// change the WHOLE list is stale, not only the documents that happened to be on top.
$('digest-rescore-all').onclick = async () => {
  if (!activeTab) return;
  const eligible = (lastDocs || []).filter(d => d.status === 'fetched' && d.digest_len);
  if (!eligible.length) { appendMsg({ role: 's', text: 'No candidates with a stored digest yet — run a 🏆 deep-compare / full read first.' }); return; }
  const passes = Math.ceil(eligible.length / 25);
  const gap = (lastDocs || []).filter(d => d.status === 'fetched' && !d.digest_len).length;
  if (!confirm(`♻️ Re-check ALL ${eligible.length} candidate(s) with a digest`
             + `\n\n≈ ${passes} bulk pass(es) over stored digests (no full-text re-read).`
             + `\nScores are tagged ·digest. Each pass is saved as it lands.`
             + (gap ? `\n\n⚠ ${gap} fetched candidate(s) have NO digest and will be SKIPPED — `
                    + `use 🔁 backfill first to include them.` : '')
             + `\n\nContinue?`)) return;
  const btn = $('digest-rescore-all'); btn.disabled = true;
  setBusy(true, `Re-checking ALL ${eligible.length} candidates (${passes} bulk pass(es))`);
  const res = await api(`/api/tabs/${activeTab}/digest-rescore`, {
    method: 'POST', body: JSON.stringify({ all_docs: true, model: readModelValue() }) });
  setBusy(false); btn.disabled = false;
  if (res.error) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
  await refreshDocs();
  await reloadChat();
};
// ➕ Additional read over EVERY candidate with a stored digest, not just the displayed
// top-N: a low-ranked document can only earn its A-feature bonus (and climb) if it was
// actually assessed. The server batches (25/pass) and saves each batch as it lands.
$('additional-read-all').onclick = async () => {
  if (!activeTab) return;
  const eligible = (lastDocs || []).filter(d => d.status === 'fetched' && d.digest_len);
  if (!eligible.length) { appendMsg({ role: 's', text: 'No candidates with a stored digest yet — run a 🏆 deep-compare / full read first.' }); return; }
  const passes = Math.ceil(eligible.length / 25);
  const gap = (lastDocs || []).filter(d => d.status === 'fetched' && !d.digest_len).length;
  if (!confirm(`➕ Additional read over ALL ${eligible.length} candidate(s) with a digest`
             + `\n\n≈ ${passes} bulk sonnet pass(es) over stored digests (no full-text re-read).`
             + `\nEach pass is saved as it lands.`
             + (gap ? `\n\n⚠ ${gap} fetched candidate(s) have NO digest and will be SKIPPED — `
                    + `use 🔁 backfill first to include them.` : '')
             + `\n\nContinue?`)) return;
  const btn = $('additional-read-all'); btn.disabled = true;
  setBusy(true, `Additional read over ALL ${eligible.length} candidates (${passes} bulk pass(es) over digests)`);
  const res = await api(`/api/tabs/${activeTab}/additional-read`, {
    method: 'POST', body: JSON.stringify({ all_docs: true }) });
  setBusy(false); btn.disabled = false;
  if (res.error) { appendMsg({ role: 's', text: `Error: ${res.error}` }); return; }
  await refreshDocs();
  await reloadChat();
};
// 🧩 Combi — compute (free, in code) the best 2-document combinations from the stored
// per-feature read verdicts, show the panel + each doc's best-partner hint.
$('combi').onclick = () => { if (activeTab) runCombi(); };
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
// Figures row on a fetched candidate card: shows the captioned-figure count (so you
// know figures are groundable) or a 🖼 Read figures button to caption them on demand.
function buildFiguresRow(d) {
  const row = document.createElement('div');
  row.className = 'read-meta fig-meta';
  if (d.figures_n > 0) {
    row.classList.add('muted');
    row.textContent = `🖼 ${d.figures_n} figure(s) read — ask about figure numbers / reference numerals`;
    row.title = 'Drawing sheets were vision-read into the text; chat & deep-compare can cite them like paragraphs.';
  } else if (d.figures_n === 0) {
    row.classList.add('muted');
    row.textContent = '🖼 no drawings found';
  } else {
    const btn = document.createElement('button');
    btn.className = 'btn small'; btn.textContent = '🖼 Read figures';
    btn.title = 'Download this patent’s drawing sheets and vision-read them into the text, '
      + 'so you can ask about figure numbers & reference numerals.';
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = '🖼 reading figures…';
      const r = await api(`/api/tabs/${activeTab}/documents/${d.id}/figures?reading_model=${encodeURIComponent(readModelValue())}`,
                          { method: 'POST' });
      if (r.error) { btn.disabled = false; btn.textContent = `error: ${r.error}`; return; }
      pollFiguresDone(d.id);
    };
    row.appendChild(btn);
  }
  return row;
}

// Poll the doc list until this doc's figures finished captioning (figures_n set).
let figPoll = {};
function pollFiguresDone(docId, tries = 0) {
  clearTimeout(figPoll[docId]);
  figPoll[docId] = setTimeout(async () => {
    await refreshDocs();
    const d = (lastDocs || []).find(x => x.id === docId);
    if (d && (d.figures_n === null || d.figures_n === undefined) && tries < 120)
      pollFiguresDone(docId, tries + 1);
  }, 5000);
}

function openViewer(title, doc, figBase = null) {
  $('view-title').textContent = title;
  $('view-meta').textContent = doc.title || '';
  const parts = [];
  if (doc.text) parts.push(doc.text);              // upload-based benchmark
  for (const [label, key] of [['ABSTRACT', 'abstract'], ['FULL-TEXT DIGEST', 'digest'], ['CLAIMS', 'claims'], ['DESCRIPTION', 'description']]) {
    if (doc[key] && typeof doc[key] === 'string') parts.push(`===== ${label} =====\n${doc[key]}`);
  }
  $('view-body').textContent = parts.join('\n\n') || '(no stored text)';
  // drawing sheets (the real images) + their vision captions, if any
  const fwrap = $('view-figs');
  fwrap.innerHTML = '';
  let figs = doc.figures;
  if (typeof figs === 'string') { try { figs = JSON.parse(figs); } catch { figs = null; } }
  if (figBase && Array.isArray(figs) && figs.length) {
    const h = document.createElement('div');
    h.className = 'view-figs-hdr'; h.textContent = `🖼 ${figs.length} drawing sheet(s)`;
    fwrap.appendChild(h);
    const grid = document.createElement('div'); grid.className = 'view-figs-grid';
    figs.forEach((f, i) => {
      const cell = document.createElement('figure'); cell.className = 'view-fig';
      const img = document.createElement('img');
      img.loading = 'lazy'; img.src = `${figBase}/${i + 1}`;
      img.onclick = () => window.open(`${figBase}/${i + 1}`, '_blank');
      cell.appendChild(img);
      if (f.caption) {
        const cap = document.createElement('figcaption');
        cap.textContent = f.caption;
        cell.appendChild(cap);
      }
      grid.appendChild(cell);
    });
    fwrap.appendChild(grid);
    fwrap.classList.remove('hidden');
  } else fwrap.classList.add('hidden');
  $('view-modal').classList.remove('hidden');
}
$('view-close').onclick = () => $('view-modal').classList.add('hidden');

/* ---------- feature cross-reference + full comment modal ---------- */
// A candidate's verdict for one feature, normalised across mandatory (feature_scores,
// .note) and additional (additional_scores, .evidence) feature kinds.
const ADDL_STATUS = { present: 'yes', stretch: 'partial', absent: 'no' };  // A-feature → uniform
function docFeatureEntry(d, name, kind) {
  const arr = kind === 'A' ? d.additional_scores : d.feature_scores;
  if (!Array.isArray(arr)) return null;
  const e = arr.find(x => x.name === name);
  if (!e) return null;
  const status = kind === 'A' ? (ADDL_STATUS[e.status] || 'no') : e.status;
  return { status, note: e.note || e.evidence || '', weight: e.weight || 1, sl: e.sl };
}
function scrollToDoc(id) {
  const card = document.querySelector(`.doc[data-doc-id="${id}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.classList.add('doc-flash');
  setTimeout(() => card.classList.remove('doc-flash'), 1600);
}
// Click any feature chip → every document's verdict on THAT feature, with the FULL note
// (no truncated tooltip). clickedDocId highlights the row you came from.
function openFeatureModal(name, weight, kind, clickedDocId = null) {
  $('feat-modal-title').textContent = `🧩 ${name}`;
  $('feat-modal-sub').textContent =
    `${kind === 'A' ? 'Additional' : 'Mandatory'} feature · weight ★${weight || 1} — every document below, full comments`;
  const body = $('feat-modal-body');
  body.innerHTML = '';
  const rows = [];
  for (const d of lastDocs) {
    const e = docFeatureEntry(d, name, kind);
    if (e) rows.push({ d, e });
  }
  const order = { yes: 0, partial: 1, no: 2 };
  rows.sort((a, b) => (order[a.e.status] ?? 3) - (order[b.e.status] ?? 3)
    || ((combinedScore(b.d) ?? 0) - (combinedScore(a.d) ?? 0)));
  const counts = { yes: 0, partial: 0, no: 0 };
  for (const r of rows) counts[r.e.status] = (counts[r.e.status] || 0) + 1;
  const sum = document.createElement('div');
  sum.className = 'feat-modal-counts';
  sum.innerHTML = `<span class="feat-mark yes">✓ ${counts.yes} disclose</span>`
    + `<span class="feat-mark partial">~ ${counts.partial} partial</span>`
    + `<span class="feat-mark no">✗ ${counts.no} no</span>`;
  body.appendChild(sum);
  const mark = { yes: '✓', partial: '~', no: '✗' };
  let shown = 0;
  for (const { d, e } of rows) {
    if (e.status === 'no') continue;          // list docs that HAVE it (yes/partial); 'no' is in the counts
    shown++;
    const row = document.createElement('div');
    row.className = 'feat-doc-row ' + e.status + (d.id === clickedDocId ? ' current' : '');
    const head = document.createElement('div');
    head.className = 'feat-doc-head';
    const num = document.createElement('a');
    num.className = 'feat-doc-num';
    num.textContent = `${mark[e.status] || '?'} ${d.number || d.title || ('#' + d.id)}`;
    num.title = 'Jump to this document';
    num.onclick = () => { $('feat-modal').classList.add('hidden'); scrollToDoc(d.id); };
    head.appendChild(num);
    if (d.id === clickedDocId) {
      const here = document.createElement('span');
      here.className = 'chip'; here.textContent = 'this doc';
      head.appendChild(here);
    }
    row.appendChild(head);
    if (e.note) {
      const note = document.createElement('div');
      note.className = 'feat-doc-note';
      note.textContent = e.note;               // FULL comment, wrapped — the readable version
      row.appendChild(note);
    }
    body.appendChild(row);
  }
  if (!shown) {
    const none = document.createElement('div');
    none.className = 'muted'; none.textContent = 'No document discloses this feature in this tab yet.';
    body.appendChild(none);
  }
  $('feat-modal').classList.remove('hidden');
  loadFeatureXref(name, body);
}

// Cross-tab feature lookup: any document in OTHER tabs that was assessed to disclose
// the same feature. Loaded async and appended below the current tab's rows, so a
// feature checked once is visible everywhere it appears.
async function loadFeatureXref(name, body) {
  const res = await api(`/api/tabs/${activeTab}/feature-xref?name=${encodeURIComponent(name)}`);
  const docs = (res && res.documents) || [];
  if (!docs.length) return;
  const hdr = document.createElement('div');
  hdr.className = 'feat-xref-hdr';
  hdr.textContent = `🗂 In other tabs — ${docs.length} document(s) disclose this feature`;
  body.appendChild(hdr);
  const mark = { yes: '✓', partial: '~' };
  for (const d of docs) {
    const row = document.createElement('div');
    row.className = 'feat-doc-row ' + (d.status || 'yes');
    const head = document.createElement('div');
    head.className = 'feat-doc-head';
    const num = document.createElement('a');
    num.className = 'feat-doc-num';
    num.textContent = `${mark[d.status] || '✓'} ${d.number || d.title || ('#' + d.id)}`;
    num.title = 'Switch to that tab and jump to this document';
    num.onclick = async () => {
      $('feat-modal').classList.add('hidden');
      await selectTab(d.tab_id);
      setTimeout(() => scrollToDoc(d.id), 400);
    };
    head.appendChild(num);
    const tab = document.createElement('span');
    tab.className = 'chip'; tab.textContent = `tab: ${d.tab_name || '?'}`
      + (d.kind === 'A' ? ' · A' : '');
    head.appendChild(tab);
    row.appendChild(head);
    if (d.note) {
      const note = document.createElement('div');
      note.className = 'feat-doc-note'; note.textContent = d.note;
      row.appendChild(note);
    }
    body.appendChild(row);
  }
}
$('feat-modal-close').onclick = () => $('feat-modal').classList.add('hidden');
$('feat-modal').onclick = e => { if (e.target === $('feat-modal')) $('feat-modal').classList.add('hidden'); };

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
// Force a pane OPEN. Collapse state is persisted, so a pane the user collapsed days ago
// silently swallows anything rendered into it — never write a result into a 38px strip
// and call it shown.
function expandPane(key) {
  if (!layout[key]) return;
  layout[key] = false;
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

/* ---------- draggable pane widths (regulate the width of the panes) ---------- */
const COL_VAR = { bm: '--col-bm', docs: '--col-docs' };
const COL_DEFAULT = { bm: 300, docs: 440 };
const COL_MIN = { bm: 180, docs: 240 };
const COL_MAX = { bm: 1200, docs: 1200 };
let cols = {};
try { cols = JSON.parse(localStorage.getItem('pb-cols') || '{}'); } catch {}
function applyCols() {
  const main = $('main');
  for (const key of Object.keys(COL_VAR)) {
    if (cols[key]) main.style.setProperty(COL_VAR[key], cols[key] + 'px');
    else main.style.removeProperty(COL_VAR[key]);
  }
}
applyCols();
for (const handle of document.querySelectorAll('.resizer')) {
  const key = handle.dataset.resize;
  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = cols[key] || COL_DEFAULT[key];
    handle.classList.add('dragging');
    document.body.classList.add('col-resizing');
    const onMove = ev => {
      const w = Math.max(COL_MIN[key], Math.min(COL_MAX[key], startW + (ev.clientX - startX)));
      cols[key] = Math.round(w);
      $('main').style.setProperty(COL_VAR[key], cols[key] + 'px');
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      handle.classList.remove('dragging');
      document.body.classList.remove('col-resizing');
      localStorage.setItem('pb-cols', JSON.stringify(cols));
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
  // double-click a divider to reset that pane to its default width
  handle.addEventListener('dblclick', () => {
    delete cols[key];
    localStorage.setItem('pb-cols', JSON.stringify(cols));
    applyCols();
  });
}

/* ================= cross-tab knowledge graph + global search ================= */

const KIND_ICON = { field: '🗂', block: '🧱', function: '⚙️', option: '◦' };

function openKgModal() {
  $('kg-modal').classList.remove('hidden');
  $('kg-results').classList.add('hidden');
  loadKgTree();
  setTimeout(() => $('kg-q').focus(), 50);
}
$('kg-open').onclick = openKgModal;
$('kg-modal-close').onclick = () => $('kg-modal').classList.add('hidden');
$('kg-modal').onclick = e => { if (e.target === $('kg-modal')) $('kg-modal').classList.add('hidden'); };

// Top-bar box: Enter opens the graph modal already searched.
$('topsearch').onkeydown = e => {
  if (e.key !== 'Enter') return;
  const q = $('topsearch').value.trim();
  openKgModal();
  if (q) { $('kg-q').value = q; runGlobalSearch(q); }
};

let kgSearchTimer = null;
$('kg-q').oninput = () => {
  clearTimeout(kgSearchTimer);
  const q = $('kg-q').value.trim();
  if (!q) { $('kg-results').classList.add('hidden'); return; }
  kgSearchTimer = setTimeout(() => runGlobalSearch(q), 250);
};

async function loadKgTree() {
  const box = $('kg-tree');
  const res = await api('/api/kg');
  const nodes = (res && res.nodes) || [];
  box.innerHTML = '';
  if (!nodes.length) {
    box.innerHTML = '<div class="muted">The graph is empty. Click <b>🔄 Build / refresh</b> to '
      + 'classify every tab\'s features into field › block › function › option, or add a '
      + 'benchmark feature and use its 🔗 button.</div>';
    return;
  }
  for (const n of nodes) box.appendChild(renderKgNode(n, 0));
}

function renderKgNode(node, depth) {
  const wrap = document.createElement('div');
  wrap.className = 'kg-node kg-' + node.kind;
  const hasKids = (node.children && node.children.length) || (node.features && node.features.length);
  const row = document.createElement('div');
  row.className = 'kg-row';
  const tw = document.createElement('button');
  tw.className = 'kg-twist';
  tw.textContent = hasKids ? '▸' : '·';
  const label = document.createElement('span');
  label.className = 'kg-label';
  label.innerHTML = `<span class="kg-icon">${KIND_ICON[node.kind] || '•'}</span> `
    + `<b class="kg-name">${esc(node.name)}</b>`
    + (node.total_features ? ` <span class="chip kg-count">${node.total_features}</span>` : '');
  label.title = 'Rename';
  label.ondblclick = async () => {
    const nn = prompt('Rename node:', node.name);
    if (nn && nn.trim() && nn !== node.name) {
      await api(`/api/kg/node/${node.id}`, { method: 'PATCH', body: JSON.stringify({ name: nn.trim() }) });
      loadKgTree();
    }
  };
  const del = document.createElement('button');
  del.className = 'kg-del'; del.textContent = '🗑'; del.title = 'Delete this node and its children';
  del.onclick = async e => {
    e.stopPropagation();
    if (!confirm(`Delete "${node.name}" and everything under it?`)) return;
    await api(`/api/kg/node/${node.id}`, { method: 'DELETE' });
    loadKgTree();
  };
  row.append(tw, label, del);
  wrap.appendChild(row);

  const kids = document.createElement('div');
  kids.className = 'kg-kids hidden';
  // related cross-links
  if (node.related && node.related.length) {
    const rel = document.createElement('div');
    rel.className = 'kg-related';
    rel.innerHTML = '⇄ related: ';
    for (const r of node.related) {
      const chip = document.createElement('span');
      chip.className = 'chip kg-rel-chip';
      chip.textContent = `${KIND_ICON[r.kind] || '•'} ${r.name}`;
      rel.appendChild(chip);
    }
    kids.appendChild(rel);
  }
  // feature occurrences = the tabs/docs that disclose this node
  for (const f of (node.features || [])) {
    const fr = document.createElement('div');
    fr.className = 'kg-feat';
    const where = f.number ? `${f.number}` : (f.status === 'benchmark' ? '🎯 benchmark' : '#' + (f.doc_id || '?'));
    const mark = { yes: '✓', partial: '~', present: '✓', stretch: '~', benchmark: '🎯' }[f.status] || '•';
    fr.innerHTML = `<a class="kg-jump">${mark} ${esc(where)}</a> `
      + `<span class="chip">tab: ${esc(f.tab_name || '?')}</span>`
      + (f.feature_name && f.feature_name !== node.name ? ` <span class="kg-fname">“${esc(f.feature_name)}”</span>` : '');
    if (f.tab_id) {
      fr.querySelector('.kg-jump').onclick = async () => {
        $('kg-modal').classList.add('hidden');
        await selectTab(f.tab_id);
        if (f.doc_id) setTimeout(() => scrollToDoc(f.doc_id), 400);
      };
      fr.querySelector('.kg-jump').title = 'Open that tab' + (f.doc_id ? ' and jump to this document' : '');
    }
    if (f.note) {
      const note = document.createElement('div');
      note.className = 'kg-fnote'; note.textContent = f.note;
      fr.appendChild(note);
    }
    kids.appendChild(fr);
  }
  for (const ch of (node.children || [])) kids.appendChild(renderKgNode(ch, depth + 1));
  wrap.appendChild(kids);
  const toggle = () => {
    kids.classList.toggle('hidden');
    tw.textContent = kids.classList.contains('hidden') ? '▸' : '▾';
  };
  tw.onclick = toggle;
  label.onclick = toggle;
  if (depth === 0 && hasKids) toggle();   // fields open by default
  return wrap;
}

async function runGlobalSearch(q) {
  const box = $('kg-results');
  box.classList.remove('hidden');
  box.innerHTML = '<div class="muted">Searching…</div>';
  const res = await api(`/api/search?q=${encodeURIComponent(q)}`);
  if (res.error) { box.innerHTML = `<div class="err">${esc(res.error)}</div>`; return; }
  box.innerHTML = '';
  const total = (res.nodes || []).length + (res.documents || []).length + (res.messages || []).length;
  if (!total) { box.innerHTML = '<div class="muted">No cross-tab matches.</div>'; return; }

  if (res.nodes && res.nodes.length) {
    box.appendChild(sectionHead(`🗺 Graph nodes (${res.nodes.length})`));
    for (const n of res.nodes) {
      const el = document.createElement('div');
      el.className = 'kg-hit';
      const path = (n.path || []).map(p => esc(p.name)).join(' › ');
      el.innerHTML = `<span class="kg-icon">${KIND_ICON[n.kind] || '•'}</span> ${path || esc(n.name)}`;
      box.appendChild(el);
    }
  }
  if (res.documents && res.documents.length) {
    box.appendChild(sectionHead(`📄 Documents (${res.documents.length})`));
    for (const d of res.documents) {
      const el = document.createElement('div');
      el.className = 'kg-hit';
      el.innerHTML = `<a class="kg-jump">${esc(d.number || '#' + d.id)}</a> `
        + `<span class="muted">${esc(d.title || '')}</span> <span class="chip">tab: ${esc(d.tab_name)}</span>`
        + (d.score != null ? ` <span class="chip">${d.score}/10</span>` : '');
      el.querySelector('.kg-jump').onclick = async () => {
        $('kg-modal').classList.add('hidden');
        await selectTab(d.tab_id);
        setTimeout(() => scrollToDoc(d.id), 400);
      };
      box.appendChild(el);
    }
  }
  if (res.messages && res.messages.length) {
    box.appendChild(sectionHead(`💬 Chats (${res.messages.length})`));
    for (const m of res.messages) {
      const el = document.createElement('div');
      el.className = 'kg-hit';
      const who = { q: '❓', c: '🤖', a: '📓' }[m.role] || '•';
      el.innerHTML = `<a class="kg-jump">${who} open</a> <span class="chip">tab: ${esc(m.tab_name)}</span> `
        + `<span class="kg-snip">${esc(m.snippet || '')}</span>`;
      el.querySelector('.kg-jump').onclick = async () => {
        $('kg-modal').classList.add('hidden');
        await selectTab(m.tab_id);
      };
      box.appendChild(el);
    }
  }
}
function sectionHead(t) {
  const h = document.createElement('div');
  h.className = 'kg-sec'; h.textContent = t;
  return h;
}

$('kg-refresh').onclick = async () => {
  const st = $('kg-rebuild-status');
  st.textContent = 'Classifying features across all tabs (this uses a cheap LLM)…';
  $('kg-refresh').disabled = true;
  const res = await api('/api/kg/rebuild', { method: 'POST', body: JSON.stringify({}) });
  $('kg-refresh').disabled = false;
  if (res.error) { st.innerHTML = `<span class="err">${esc(res.error)}</span>`; return; }
  st.textContent = `Done — ${res.attached} feature(s) placed on ${res.nodes} node(s) `
    + `(${res.distinct_features} distinct${res.failed ? `, ${res.failed} failed` : ''}).`;
  loadKgTree();
};

// Per-feature 🔗: classify THIS feature and offer to link it to an existing node.
async function linkFeatureRow(name, hint) {
  if (!name.trim()) return;
  hint.textContent = '🔗 classifying…'; hint.className = 'feat-link-hint';
  const res = await api('/api/kg/classify', { method: 'POST',
    body: JSON.stringify({ feature_name: name, tab_id: activeTab }) });
  if (res.error && !res.classification) { hint.innerHTML = `<span class="err">${esc(res.error)}</span>`; return; }
  const cls = res.classification;
  const cands = res.candidates || [];
  hint.innerHTML = '';
  const path = [cls.field, cls.block, cls.function, cls.option].filter(Boolean).join(' › ');
  const head = document.createElement('div');
  head.innerHTML = `🔗 <b>${esc(path)}</b>`
    + (cls.related_blocks && cls.related_blocks.length ? ` <span class="muted">⇄ ${esc(cls.related_blocks.join(', '))}</span>` : '');
  hint.appendChild(head);
  const btns = document.createElement('div');
  btns.className = 'feat-link-btns';
  // link to the best existing node, if any overlap
  for (const c of cands.slice(0, 2)) {
    const b = document.createElement('button');
    b.className = 'btn small';
    b.textContent = `↪ link to “${c.name}”`;
    b.title = (c.path || []).map(p => p.name).join(' › ');
    b.onclick = () => attachFeature({ feature_name: name, node_id: c.id }, hint, c.name);
    btns.appendChild(b);
  }
  const nb = document.createElement('button');
  nb.className = 'btn small primary';
  nb.textContent = cands.length ? '＋ new node' : '＋ add to graph';
  nb.onclick = () => attachFeature({
    feature_name: name, field: cls.field, block: cls.block,
    function: cls.function, option: cls.option, related_blocks: cls.related_blocks,
  }, hint, path);
  btns.appendChild(nb);
  const dismiss = document.createElement('button');
  dismiss.className = 'btn small'; dismiss.textContent = '✕';
  dismiss.onclick = () => { hint.innerHTML = ''; };
  btns.appendChild(dismiss);
  hint.appendChild(btns);
}
async function attachFeature(payload, hint, label) {
  payload.tab_id = activeTab; payload.status = 'benchmark';
  const res = await api('/api/kg/attach', { method: 'POST', body: JSON.stringify(payload) });
  hint.innerHTML = res.error ? `<span class="err">${esc(res.error)}</span>`
    : `✅ linked to <b>${esc(label)}</b> <span class="muted">(see 🗺)</span>`;
}

// Benchmark cross-tab references: patent numbers named in the benchmark that live in
// OTHER tabs → chips whose stored arguments auto-load into chat context.
async function loadBenchmarkXrefs(bm) {
  const box = $('bm-xrefs');
  if (!box) return;
  box.innerHTML = '';
  const text = [bm && bm.text, bm && bm.title, bm && bm.number,
    ...((bm && bm.features) || []).map(f => f.name)].filter(Boolean).join('\n');
  if (!text.trim() || !activeTab) return;
  const res = await api(`/api/tabs/${activeTab}/refs?text=${encodeURIComponent(text)}`);
  const refs = (res && res.refs) || [];
  if (!refs.length) return;
  const head = document.createElement('div');
  head.className = 'bm-xref-head';
  head.textContent = '📎 Referenced patents found in other tabs — their arguments auto-load into chat:';
  box.appendChild(head);
  for (const r of refs) {
    const chip = document.createElement('span');
    chip.className = 'chip bm-xref-chip';
    chip.textContent = `📎 ${r.number} · tab ${r.tab_name || '?'}`;
    chip.title = (r.verdict || r.digest || '').slice(0, 500);
    chip.onclick = async () => { await selectTab(r.tab_id); if (r.doc_id) setTimeout(() => scrollToDoc(r.doc_id), 400); };
    box.appendChild(chip);
  }
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------- boot ---------- */
(async () => {
  await loadSkills();
  await Promise.all([loadHealth(), loadTabs()]);
})();
