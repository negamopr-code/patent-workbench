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
let skillsMeta = { skills: [], models: [], default_model: '' };
let nbState = { notebooks: [], chosen: null, sources: [], selected: new Set() };
let lessonDefaultText = '';

/* ---------- reading / OCR model (shared by the benchmark + candidates panes) ---------- */
const READ_SELECT_IDS = ['bm-read-model', 'cand-read-model'];
const readSelects = () => READ_SELECT_IDS.map($).filter(Boolean);
function readModelValue() {
  const s = readSelects()[0];
  return s ? s.value : (skillsMeta.default_read_model || 'claude-haiku-4-5');
}
function setReadModel(v) { for (const s of readSelects()) s.value = v; }

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
    const name = document.createElement('span');
    name.textContent = t.name;
    el.appendChild(name);
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
    el.ondblclick = () => startRename(el, name, t);
    wrap.appendChild(el);
  }
}

function startRename(tabEl, nameEl, t) {
  const input = document.createElement('input');
  input.value = t.name;
  tabEl.replaceChild(input, nameEl);
  input.focus(); input.select();
  const commit = async () => {
    const name = input.value.trim();
    if (name && name !== t.name) await api(`/api/tabs/${t.id}`, { method: 'PATCH', body: JSON.stringify({ name }) });
    loadTabs();
  };
  input.onblur = commit;
  input.onkeydown = e => { if (e.key === 'Enter') input.blur(); if (e.key === 'Escape') loadTabs(); };
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
  scheduleDocsPoll(st.documents || []);
  pollRate();                     // resume showing progress if an NLM rating job is in flight
  pollRead();                     // resume showing progress if a Claude deep-read is in flight
}

/* ---------- benchmark (reference document) ---------- */
function renderBenchmark(bm) {
  clearTimeout(bmPoll);
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
  name.textContent = bm.number || `📷 ${(bm.files || []).length} uploaded file(s)`;
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
      openViewer(full.number || 'Benchmark (uploaded files)', full);
    };
    row.appendChild(view);
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
  for (const s of readSelects()) s.onchange = () => { setReadModel(s.value); savePrefs(); };
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
let docsSort = 'combined';   // 'combined' | 'claude' | 'nlm' | 'delta' — palmares ranking key
// Combined ("common") score: average of the two engines when both rated, else the
// single available score. Ranking by this puts documents BOTH engines like on top.
function combinedScore(d) {
  if (d.score != null && d.nlm_score != null) return (d.score + d.nlm_score) / 2;
  return d.score ?? d.nlm_score ?? null;
}
function scoreSortValue(d, key) {
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
  // surface "Continue deep-read" only when some fetched candidates are NOT yet full-read
  const unread = allDocs.filter(d => d.status === 'fetched' && d.score == null).length;
  const cont = $('claude-continue');
  if (cont) { cont.classList.toggle('hidden', !unread); cont.textContent = `▶️ Continue deep-read (${unread} left)`; }
  if (!unfetched && docsFilter === 'unfetched') docsFilter = 'all';
  if (allDocs.length) {
    const bar = document.createElement('div');
    bar.className = 'docs-summary';
    bar.innerHTML =
      `<span class="chip ok" title="fetched & ready">✓ ${counts.fetched || 0}</span>`
      + (counts.pending ? `<span class="chip warn" title="still fetching">⏳ ${counts.pending}</span>` : '')
      + (counts.error ? `<span class="chip err" title="failed to fetch — check the number/kind code">⚠ ${counts.error}</span>` : '');
    if (unfetched) {
      const t = document.createElement('button');
      t.className = 'btn small';
      t.textContent = docsFilter === 'unfetched' ? '↩ show all' : `🔎 show ${unfetched} not-fetched`;
      t.onclick = () => { docsFilter = docsFilter === 'unfetched' ? 'all' : 'unfetched'; renderDocs(allDocs); };
      bar.appendChild(t);
    }
    // sort the palmares by Claude, NotebookLM, or biggest disagreement
    if (allDocs.some(d => d.nlm_score != null)) {
      const sortSel = document.createElement('select');
      sortSel.className = 'sort-sel';
      sortSel.title = 'Rank candidates by';
      for (const [v, label] of [['combined', '🥇 by combined'], ['claude', '🤖 by Claude'], ['nlm', '📓 by NLM'], ['delta', 'Δ by disagreement']]) {
        const o = document.createElement('option'); o.value = v; o.textContent = label;
        if (v === docsSort) o.selected = true;
        sortSel.appendChild(o);
      }
      sortSel.onchange = () => { docsSort = sortSel.value; renderDocs(allDocs); };
      bar.appendChild(sortSel);
    }
    wrap.appendChild(bar);
  }

  // ranking ("palmares"): chosen score first, ties/unscored after (by insertion)
  let docs = [...allDocs].sort((a, b) =>
    scoreSortValue(b, docsSort) - scoreSortValue(a, docsSort) || a.id - b.id);
  if (docsFilter === 'unfetched') docs = docs.filter(d => d.status !== 'fetched');
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
    if (d.status === 'fetched') {
      const sz = document.createElement('div');
      sz.className = 'sizes';
      const fmt = n => !n ? '—' : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
      sz.textContent = `abstract ${fmt(d.abstract_len)} · claims ${fmt(d.claims_len)} · description ${fmt(d.description_len)} chars · ` +
        (d.digest_len ? 'full-text digest ✓' : 'digesting full text…') +
        (d.nlm_source_notebook ? ' · 📓 in notebook' : '');
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

async function runDeepCompare(idsArg, skipScored) {
  // idsArg: array of doc ids → those candidates; null/[] → EVERY candidate
  // skipScored: CONTINUE mode — read only candidates not yet full-read this batch
  if (!activeTab) return;
  const ids = idsArg || [];
  let scope = ids.length ? `the ${ids.length} SELECTED candidate(s)` : 'EVERY candidate';
  if (skipScored) {
    const todo = lastDocs.filter(d => d.status === 'fetched' && d.score == null).length;
    if (!todo) { alert('All candidates have already been full-read by Claude. Use 🤖 Claude deep-read all to re-read.'); return; }
    scope = `the ${todo} candidate(s) NOT yet full-read (most promising first)`;
  }
  if (!confirm(`Deep read: the 📖 reading model reads ${scope} in FULL against ` +
               `the benchmark (most-promising first)` +
               (skipScored ? ', skipping the ones already read' : '') + '. ' +
               (ids.length ? '' : 'Takes a few minutes. ') + 'Start?')) return;
  const q = $('q').value.trim();          // optional custom task; default ranking otherwise
  $('q').value = '';
  const tabAtSend = activeTab;
  const res = await api(`/api/tabs/${tabAtSend}/deep-compare`, {
    method: 'POST', body: JSON.stringify({
      model: $('model').value,
      skills: [...document.querySelectorAll('#skills input:checked')].map(i => i.value),
      question: q || null,
      doc_ids: ids.length ? ids : null,
      reading_model: readModelValue(),
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
  if (s.running) {
    readWasRunning = true;
    el.textContent = `🤖 deep-reading ${s.done}/${s.total}… (scores land below; safe to reload)`;
    refreshDocs();
    readPoll = setTimeout(pollRead, 5000);
  } else if (readWasRunning) {
    readWasRunning = false;
    el.textContent = `✓ deep-read finished — ranking posted to chat`;
    refreshDocs();
    reloadChat();
  } else {
    el.classList.add('muted'); el.textContent = '';
  }
}
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
    const next = title.replace(/ \((\d+)\)$/, (m, n) => ` (${+n + 1})`);
    const created = await api(`/api/tabs/${activeTab}/notebook/create`, {
      method: 'POST', body: JSON.stringify({ title: next === title ? `${title} (2)` : next }) });
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

$('nb-btn').onclick = async () => {
  $('nb-modal').classList.remove('hidden');
  $('nb-list').textContent = 'Loading notebooks…';
  $('nb-sources-wrap').classList.add('hidden');
  const [res, st] = await Promise.all([
    api('/api/notebooks'),
    api(`/api/tabs/${activeTab}/state`),
  ]);
  nbState = { notebooks: res.notebooks || [], chosen: null, sources: [], selected: new Set() };
  const current = st.notebook;
  // auto-export defaults ON for a fresh connection (the notebook is meant to be a
  // Claude-quota-independent mirror of the tab's candidates); preserve the user's
  // choice when re-opening an already-configured notebook.
  $('nb-auto-add').checked = current ? !!current.auto_add : true;
  $('nb-sync-status').textContent = '';
  const wrap = $('nb-list');
  wrap.innerHTML = '';
  if (res.error) { wrap.textContent = `NotebookLM unavailable: ${res.error}`; return; }
  if (!nbState.notebooks.length) { wrap.textContent = 'No notebooks in the account.'; return; }
  for (const nb of nbState.notebooks) {
    const label = document.createElement('label');
    const r = document.createElement('input');
    r.type = 'radio'; r.name = 'nb'; r.value = nb.id;
    r.onchange = () => chooseNotebook(nb, current);
    label.appendChild(r);
    label.appendChild(document.createTextNode(
      ` ${nb.title}` + (nb.sources != null ? ` (${nb.sources} sources)` : '')));
    wrap.appendChild(label);
    if (current && current.notebook_id === nb.id) { r.checked = true; chooseNotebook(nb, current); }
  }
};

async function chooseNotebook(nb, current) {
  nbState.chosen = nb;
  $('nb-sources-wrap').classList.remove('hidden');
  $('nb-sources').textContent = 'Loading sources…';
  const res = await api(`/api/sources?notebook_id=${encodeURIComponent(nb.id)}`);
  nbState.sources = res.sources || [];
  const preselected = (current && current.notebook_id === nb.id)
    ? new Set(current.selected_source_ids || []) : new Set();
  nbState.selected = preselected;
  const wrap = $('nb-sources');
  wrap.innerHTML = '';
  if (res.error) { wrap.textContent = `Error: ${res.error}`; return; }
  for (const s of nbState.sources) {
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
    wrap.appendChild(label);
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
$('nb-sync').onclick = () => runNotebookSync(t => { $('nb-sync-status').textContent = t; });
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
$('nb-cancel').onclick = () => $('nb-modal').classList.add('hidden');
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
