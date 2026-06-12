/* Patent Workbench — vanilla JS SPA. State: active tab in URL hash, prefs in localStorage. */
'use strict';

const $ = id => document.getElementById(id);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...opts,
  });
  let data;
  try { data = await res.json(); } catch { data = {}; }
  if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
  return data;
};

let tabs = [];
let activeTab = null;
let docsPoll = null;
let bmPoll = null;
let docSelection = new Set();   // candidate ids picked for a scoped deep compare
let skillsMeta = { skills: [], models: [], default_model: '' };
let nbState = { notebooks: [], chosen: null, sources: [], selected: new Set() };
let lessonDefaultText = '';

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
  await loadTabs();
  if (t.id) selectTab(t.id);
};

function showEmpty() {
  $('main').classList.add('hidden');
  $('empty').classList.remove('hidden');
}

async function selectTab(id) {
  if (activeTab !== id) docSelection = new Set();
  activeTab = id;
  location.hash = id;
  $('main').classList.remove('hidden');
  $('empty').classList.add('hidden');
  renderTabs();
  loadPrefs();
  const st = await api(`/api/tabs/${id}/state`);
  if (st.error) { alert(st.error); return; }
  renderBenchmark(st.benchmark);
  renderDocs(st.documents || []);
  renderChat(st.messages || []);
  renderNbChip(st.notebook);
  scheduleDocsPoll(st.documents || []);
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
  fd.append('reading_model', $('read-model').value);
  $('bm-status').textContent =
    `Uploading ${fileList.length} file(s)… (pictures transcribed by ` +
    `${$('read-model').value.replace('claude-', '')}, 4 pages in parallel — the card shows progress)`;
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
    readModel: $('read-model').value,
    skills: [...document.querySelectorAll('#skills input:checked')].map(i => i.value),
    useDocs: $('use-docs').checked,
    askNb: $('ask-nb').checked,
  }));
}
function loadPrefs() {
  let p = {};
  try { p = JSON.parse(localStorage.getItem(prefsKey()) || '{}'); } catch {}
  if (p.model) $('model').value = p.model;
  else $('model').value = skillsMeta.default_model;
  $('read-model').value = p.readModel || skillsMeta.default_read_model || 'claude-haiku-4-5';
  const want = new Set(p.skills || defaultSkills());
  document.querySelectorAll('#skills input').forEach(i => { i.checked = want.has(i.value); });
  $('use-docs').checked = p.useDocs !== false;
  $('ask-nb').checked = !!p.askNb;
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
  const rsel = $('read-model');
  sel.innerHTML = ''; rsel.innerHTML = '';
  for (const m of skillsMeta.models || []) {
    for (const target of [sel, rsel]) {
      const o = document.createElement('option');
      o.value = m; o.textContent = m.replace('claude-', '');
      target.appendChild(o);
    }
  }
  sel.value = skillsMeta.default_model;
  rsel.value = skillsMeta.default_read_model || 'claude-haiku-4-5';
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
  rsel.onchange = savePrefs;
  $('use-docs').onchange = savePrefs;
  $('ask-nb').onchange = savePrefs;
}
function updateSkillsSummary() {
  const n = document.querySelectorAll('#skills input:checked').length;
  $('skills-summary').textContent = `🧠 Skills${n ? ` (${n})` : ''}`;
}

/* ---------- documents ---------- */
function renderDocs(docs) {
  const wrap = $('docs');
  wrap.innerHTML = '';
  $('doc-count').textContent = docs.length || '';
  // deep-compare scores define the order: best fit first, unscored after (by insertion)
  docs = [...docs].sort((a, b) =>
    (b.score ?? -1) - (a.score ?? -1) || a.id - b.id);
  for (const d of docs) {
    const el = document.createElement('div');
    el.className = 'doc';
    const row1 = document.createElement('div');
    row1.className = 'doc-row';
    if (d.status === 'fetched') {
      const sel = document.createElement('input');
      sel.type = 'checkbox';
      sel.checked = docSelection.has(d.id);
      sel.title = 'Select for a scoped deep analysis (🏆 Best match runs only on selected; none selected = whole list)';
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
    if (d.score != null) {
      const sc = document.createElement('div');
      sc.className = 'score';
      sc.textContent = `🏆 ${d.score}/10` + (d.score_note ? ` — ${d.score_note}` : '');
      sc.title = 'Match score vs the benchmark from the last full-text deep compare'
        + (d.scored_at ? ` (${new Date(d.scored_at * 1000).toLocaleString()})` : '');
      el.appendChild(sc);
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
    for (const [label, url] of [['Google Patents', d.links.google], ['Espacenet', d.links.espacenet]]) {
      const a = document.createElement('a');
      a.href = url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = label;
      row2.appendChild(a);
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
  if (docSelection.size) {
    chip.textContent = `🏆 ${docSelection.size} selected for deep analysis · clear`;
    chip.classList.remove('hidden');
    chip.style.cursor = 'pointer';
    chip.onclick = () => { docSelection = new Set(); refreshDocs(); };
  } else chip.classList.add('hidden');
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
    method: 'POST', body: JSON.stringify({ text, reading_model: $('read-model').value }) });
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
  if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]);
};
$('in-file').onchange = e => { if (e.target.files[0]) uploadFile(e.target.files[0]); };

async function uploadFile(file) {
  $('upload-status').textContent = `Extracting numbers from ${file.name}… (images go through Claude OCR, ~30 s)`;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('reading_model', $('read-model').value);
  const res = await api(`/api/tabs/${activeTab}/upload`, { method: 'POST', body: fd });
  $('in-file').value = '';
  if (res.error) { $('upload-status').textContent = `Error: ${res.error}`; return; }
  if (!res.numbers.length) { $('upload-status').textContent = 'No patent numbers found in the file.'; return; }
  const unc = (res.uncertain || []).length;
  $('upload-status').textContent = `Found ${res.numbers.length} number(s) in ${file.name}` +
    (unc ? ` — ⚠ ${unc} read inconsistently between two OCR passes, verify them against the photo:` : ':');
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
                                           reading_model: $('read-model').value }) });
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
  body.textContent = m.text;
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
$('best-match').onclick = async () => {
  if (!activeTab) return;
  const ids = [...docSelection];
  const scope = ids.length ? `the ${ids.length} SELECTED candidate(s)` : 'EVERY candidate';
  if (!confirm(`Deep compare: a cheap model reads ${scope} in FULL against ` +
               'the benchmark, then the selected model compiles the answer. ' +
               (ids.length ? '' : 'Takes a few minutes. ') + 'Start?')) return;
  const q = $('q').value.trim();          // optional custom task; default ranking otherwise
  $('q').value = '';
  const tabAtSend = activeTab;
  appendMsg({ role: 'q', text: `[Deep compare — full text, ${ids.length || 'all'} candidate(s)]${q ? '\n' + q : ''}` });
  setBusy(true, ids.length
    ? `Deep comparing ${ids.length} selected candidate(s) at full text`
    : 'Deep comparing — reading every candidate in full');
  const res = await api(`/api/tabs/${tabAtSend}/deep-compare`, {
    method: 'POST', body: JSON.stringify({
      model: $('model').value,
      skills: [...document.querySelectorAll('#skills input:checked')].map(i => i.value),
      question: q || null,
      doc_ids: ids.length ? ids : null,
      reading_model: $('read-model').value,
    }) });
  setBusy(false);
  if (activeTab !== tabAtSend) return;
  refreshDocs();                       // new scores re-order the candidates column
  if (res.error && !(res.messages || []).length) {
    appendMsg({ role: 's', text: `Error: ${res.error}` });
    return;
  }
  for (const m of res.messages || []) appendMsg(m);
};
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
  if (cfg && cfg.notebook_id) {
    const n = (cfg.selected_source_ids || []).length;
    chip.textContent = `📓 ${cfg.notebook_title || cfg.notebook_id}` + (n ? ` · ${n} src` : ' · all src')
      + (cfg.auto_add ? ' · 📤auto' : '');
    chip.classList.remove('hidden');
  } else chip.classList.add('hidden');
}

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
  $('nb-auto-add').checked = !!(current && current.auto_add);
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
$('nb-sync').onclick = async () => {
  $('nb-sync-status').textContent = 'Syncing candidates into the notebook…';
  let res = await api(`/api/tabs/${activeTab}/notebook/sync`, { method: 'POST' });
  while (res.full) {
    $('nb-sync-status').textContent =
      `Added ${res.added}; notebook is FULL, ${res.remaining} candidate(s) left.`;
    const st = await api(`/api/tabs/${activeTab}/state`);
    const title = (st.notebook && st.notebook.notebook_title) || 'notebook';
    if (!confirm(`Notebook "${title}" is full. Create a follow-up notebook and continue?`)) break;
    const next = title.replace(/ \((\d+)\)$/, (m, n) => ` (${+n + 1})`);
    const created = await api(`/api/tabs/${activeTab}/notebook/create`, {
      method: 'POST', body: JSON.stringify({
        title: next === title ? `${title} (2)` : next }) });
    if (created.error) { $('nb-sync-status').textContent = created.error; return; }
    renderNbChip(created.notebook);
    res = await api(`/api/tabs/${activeTab}/notebook/sync`, { method: 'POST' });
  }
  $('nb-sync-status').textContent = res.error
    ? `Error: ${res.error}`
    : `Done: ${res.added} added` + (res.remaining ? `, ${res.remaining} remaining` : '')
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
  const res = await api(`/api/tabs/${activeTab}/notebook`, {
    method: 'PUT', body: JSON.stringify({
      notebook_id: nbState.chosen.id,
      notebook_title: nbState.chosen.title,
      source_ids: [...nbState.selected],
      auto_add: $('nb-auto-add').checked,
    }) });
  $('nb-modal').classList.add('hidden');
  renderNbChip(res.notebook);
};

/* ---------- boot ---------- */
(async () => {
  await loadSkills();
  await Promise.all([loadHealth(), loadTabs()]);
})();
