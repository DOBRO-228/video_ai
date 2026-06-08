from __future__ import annotations


INDEX_HTML = r"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>style-kb dashboard</title>
    <link rel="icon" href="data:,">
    <link rel="stylesheet" href="/assets/styles.css">
    <script defer src="/assets/app.js"></script>
  </head>
  <body>
    <div id="app" class="app">
      <header class="topbar">
        <div class="brand">
          <span class="brand-mark">KB</span>
          <div>
            <h1>style-kb dashboard</h1>
            <p id="rootLabel"></p>
          </div>
        </div>
        <div class="top-actions">
          <span id="loadedAt" class="muted"></span>
          <button id="refreshBtn" class="button button-small" type="button">Обновить</button>
        </div>
      </header>

      <main class="layout">
        <aside class="sidebar">
          <div class="pane-head">
            <h2>Запуски</h2>
            <input id="jobSearch" class="input" type="search" placeholder="Поиск job, title, channel">
          </div>
          <div id="jobList" class="job-list"></div>
        </aside>

        <section class="workspace">
          <div id="jobHeader" class="job-header"></div>
          <nav id="tabs" class="tabs" aria-label="Dashboard sections">
            <button class="tab active" type="button" data-tab="overview">Обзор</button>
            <button class="tab" type="button" data-tab="timeline">Timeline</button>
            <button class="tab" type="button" data-tab="claims">Claims</button>
            <button class="tab" type="button" data-tab="chunks">Chunks</button>
            <button class="tab" type="button" data-tab="visuals">Visuals</button>
            <button class="tab" type="button" data-tab="logs">Logs</button>
          </nav>
          <div id="content" class="content"></div>
        </section>

        <aside id="inspector" class="inspector"></aside>
      </main>
    </div>
    <div id="toast" class="toast" role="status" aria-live="polite"></div>
  </body>
</html>
"""


STYLES_CSS = r"""
:root {
  --bg: #0f1315;
  --surface: #171c1f;
  --surface-2: #20272b;
  --line: #2c3638;
  --line-strong: #465357;
  --text: #e7ece7;
  --muted: #9aa59c;
  --soft: #252d30;
  --teal: #5fc7bd;
  --teal-weak: #173331;
  --rust: #e28e5a;
  --rust-weak: #35251d;
  --gold: #e2c76a;
  --gold-weak: #352f1a;
  --indigo: #a9b9ff;
  --indigo-weak: #242a42;
  --danger: #ff8c8c;
  --danger-weak: #3a2022;
  --ok: #72d481;
  --ok-weak: #1b3322;
  --shadow: 0 16px 34px rgba(0, 0, 0, 0.34);
  color-scheme: dark;
}

* {
  box-sizing: border-box;
}

html,
body,
.app {
  min-height: 100%;
  height: 100%;
}

body {
  margin: 0;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--text);
  letter-spacing: 0;
}

button,
input,
select,
textarea {
  font: inherit;
}

button {
  cursor: pointer;
}

.topbar {
  height: 68px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 18px;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
}

.brand {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 12px;
}

.brand-mark {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: 8px;
  background: var(--teal);
  color: #fff;
  font-size: 13px;
  font-weight: 800;
}

.brand h1 {
  margin: 0;
  font-size: 18px;
  line-height: 1.15;
}

.brand p {
  margin: 3px 0 0;
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: min(54vw, 760px);
}

.top-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.layout {
  height: calc(100% - 68px);
  display: grid;
  grid-template-columns: 310px minmax(0, 1fr) 390px;
  gap: 1px;
  background: var(--line);
  overflow: hidden;
}

.sidebar,
.workspace,
.inspector {
  background: var(--surface);
  min-height: 0;
  overflow: hidden;
}

.sidebar,
.inspector {
  display: flex;
  flex-direction: column;
}

.workspace {
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr);
}

.job-header > *,
.controls > *,
.metrics-grid > *,
.section-grid > *,
.split > *,
.card-head > * {
  min-width: 0;
}

.pane-head {
  padding: 14px;
  border-bottom: 1px solid var(--line);
}

.pane-head h2 {
  margin: 0 0 10px;
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 0;
  color: var(--muted);
}

.job-list,
.content,
.inspector-body {
  overflow: auto;
  min-height: 0;
}

.job-list {
  padding: 10px;
}

.job-item {
  width: 100%;
  display: block;
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  padding: 10px;
  margin: 0 0 8px;
  color: var(--text);
}

.job-item:hover {
  background: var(--surface-2);
}

.job-item.active {
  border-color: var(--teal);
  background: var(--teal-weak);
}

.job-title {
  display: block;
  font-size: 14px;
  font-weight: 700;
  line-height: 1.25;
  overflow-wrap: anywhere;
}

.job-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
  color: var(--muted);
  font-size: 12px;
}

.job-header {
  border-bottom: 1px solid var(--line);
  padding: 16px 18px 14px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  align-items: start;
}

.job-header h2 {
  margin: 0;
  font-size: 20px;
  line-height: 1.2;
  overflow-wrap: anywhere;
}

.job-header p {
  margin: 6px 0 0;
  color: var(--muted);
}

.header-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 8px 12px 0;
  border-bottom: 1px solid var(--line);
  overflow-x: auto;
}

.tab {
  border: 0;
  background: transparent;
  color: var(--muted);
  padding: 10px 12px;
  border-radius: 8px 8px 0 0;
  font-weight: 700;
  white-space: nowrap;
}

.tab.active {
  color: var(--text);
  background: var(--surface-2);
  box-shadow: inset 0 -2px 0 var(--teal);
}

.content {
  padding: 16px;
}

.inspector {
  border-left: 0;
}

.inspector-head {
  padding: 14px;
  border-bottom: 1px solid var(--line);
}

.inspector-head h2 {
  margin: 0;
  font-size: 15px;
  line-height: 1.25;
  overflow-wrap: anywhere;
}

.inspector-head p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}

.inspector-body {
  padding: 14px;
}

.input,
.select {
  width: 100%;
  min-height: 38px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--surface-2);
  color: var(--text);
}

.input:focus,
.select:focus,
.button:focus,
.tab:focus,
.job-item:focus {
  outline: 2px solid var(--teal);
  outline-offset: 1px;
}

.button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  min-height: 38px;
  padding: 8px 12px;
  text-decoration: none;
  font-weight: 700;
}

.button:hover {
  border-color: var(--teal);
  color: var(--teal);
}

.button-small {
  min-height: 32px;
  padding: 6px 10px;
  font-size: 13px;
}

.controls {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) repeat(3, minmax(140px, 180px));
  gap: 10px;
  margin-bottom: 14px;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(6, minmax(120px, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.metric {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  padding: 12px;
}

.metric .label {
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 6px;
}

.metric .value {
  font-size: 24px;
  font-weight: 800;
  line-height: 1.05;
}

.metric .note {
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
}

.section-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.65fr);
  gap: 14px;
}

.section {
  min-width: 0;
  margin-bottom: 14px;
}

.section-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 0 0 8px;
}

.section-title h3 {
  margin: 0;
  font-size: 15px;
}

.table-wrap {
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: auto;
  background: var(--surface);
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  padding: 9px 10px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}

th {
  color: var(--muted);
  background: var(--surface-2);
  font-weight: 800;
  white-space: nowrap;
}

tr:last-child td {
  border-bottom: 0;
}

.row-button {
  width: 100%;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  padding: 0;
}

.status,
.chip {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  min-height: 24px;
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 12px;
  font-weight: 750;
  line-height: 1.2;
  overflow-wrap: anywhere;
}

.status.completed,
.status.high,
.status.ok {
  background: var(--ok-weak);
  color: var(--ok);
}

.status.running,
.status.medium,
.status.warning {
  background: var(--gold-weak);
  color: var(--gold);
}

.status.failed,
.status.low,
.status.error {
  background: var(--danger-weak);
  color: var(--danger);
}

.status.skipped,
.status.pending,
.status.unknown {
  background: var(--soft);
  color: var(--muted);
}

.chip {
  background: var(--soft);
  color: var(--muted);
  margin: 0 6px 6px 0;
  border-radius: 8px;
  font-weight: 650;
}

.chip.teal {
  background: var(--teal-weak);
  color: var(--teal);
}

.chip.rust {
  background: var(--rust-weak);
  color: var(--rust);
}

.chip.indigo {
  background: var(--indigo-weak);
  color: var(--indigo);
}

.chip.gold {
  background: var(--gold-weak);
  color: var(--gold);
}

.card-list {
  display: grid;
  gap: 10px;
  min-width: 0;
  max-width: 100%;
}

.card-list > * {
  min-width: 0;
}

.card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  padding: 12px;
  box-shadow: none;
  min-width: 0;
  max-width: 100%;
}

.card:hover {
  border-color: var(--line-strong);
}

.card-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 8px;
  min-width: 0;
}

.card-title {
  margin: 0;
  font-size: 15px;
  line-height: 1.25;
}

.card-subtitle {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
}

.text-block {
  color: var(--text);
  font-size: 14px;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.muted {
  color: var(--muted);
}

.tiny {
  font-size: 12px;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}

.frame-strip {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 2px 0;
  margin-top: 10px;
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

.frame {
  position: relative;
  flex: 0 0 clamp(126px, 34vw, 180px);
  min-height: 82px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-2);
  overflow: hidden;
}

.frame img {
  display: block;
  width: 100%;
  aspect-ratio: 16 / 9;
  object-fit: cover;
}

.frame span {
  position: absolute;
  left: 6px;
  bottom: 6px;
  padding: 2px 5px;
  border-radius: 6px;
  background: rgba(15, 19, 21, 0.88);
  color: var(--text);
  font-size: 11px;
  font-weight: 800;
}

.progress-list {
  display: grid;
  gap: 8px;
}

.progress-row {
  display: grid;
  grid-template-columns: 130px minmax(0, 1fr) 54px;
  gap: 10px;
  align-items: center;
}

.bar {
  height: 10px;
  border-radius: 999px;
  background: var(--soft);
  overflow: hidden;
}

.bar span {
  display: block;
  height: 100%;
  min-width: 2px;
  background: var(--teal);
}

.warning-list {
  display: grid;
  gap: 8px;
}

.warning-item {
  border-left: 4px solid var(--gold);
  border-radius: 8px;
  background: var(--gold-weak);
  padding: 9px 10px;
  font-size: 13px;
  line-height: 1.35;
}

.error-item {
  border-left-color: var(--danger);
  background: var(--danger-weak);
}

.issue-item,
.location-item {
  width: 100%;
  display: block;
  text-align: left;
  color: var(--text);
  border: 1px solid var(--line);
  border-left: 4px solid var(--gold);
  border-radius: 8px;
  background: var(--gold-weak);
  padding: 10px;
}

.issue-item + .issue-item,
.location-item + .location-item {
  margin-top: 8px;
}

.issue-item:hover,
.location-item:hover {
  border-color: var(--line-strong);
}

.issue-item.error,
.location-item.error {
  border-left-color: var(--danger);
  background: var(--danger-weak);
}

.issue-title,
.location-title {
  display: block;
  font-weight: 800;
  line-height: 1.25;
  overflow-wrap: anywhere;
}

.issue-meta,
.location-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 7px;
  color: var(--muted);
  font-size: 12px;
}

.location-list {
  display: grid;
  gap: 8px;
}

.empty {
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
  padding: 18px;
  color: var(--muted);
  text-align: center;
}

.split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

details {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  margin-top: 10px;
  overflow: hidden;
}

summary {
  padding: 9px 10px;
  cursor: pointer;
  font-weight: 800;
}

pre {
  margin: 0;
  padding: 10px;
  max-height: 420px;
  overflow: auto;
  background: #0b0f11;
  color: var(--text);
  font-size: 12px;
  line-height: 1.45;
}

.link {
  color: var(--teal);
  text-decoration: none;
  font-weight: 750;
}

.link:hover {
  text-decoration: underline;
}

.toast {
  position: fixed;
  left: 50%;
  bottom: 18px;
  transform: translateX(-50%);
  background: #e7ece7;
  color: #0f1315;
  border-radius: 8px;
  padding: 9px 12px;
  box-shadow: var(--shadow);
  opacity: 0;
  pointer-events: none;
  transition: opacity 160ms ease;
  font-size: 13px;
}

.toast.show {
  opacity: 1;
}

@media (max-width: 1280px) {
  .layout {
    grid-template-columns: 280px minmax(0, 1fr);
  }

  .inspector {
    display: none;
  }

  .metrics-grid {
    grid-template-columns: repeat(3, minmax(120px, 1fr));
  }
}

@media (max-width: 820px) {
  .topbar {
    height: auto;
    align-items: flex-start;
    flex-direction: column;
  }

  .layout {
    height: auto;
    min-height: calc(100vh - 108px);
    display: block;
    overflow: visible;
    width: 100%;
    max-width: 100vw;
  }

  .sidebar,
  .workspace {
    overflow: visible;
    width: 100%;
    max-width: 100vw;
  }

  .workspace {
    display: block;
  }

  .job-list {
    max-height: 260px;
  }

  .job-header {
    display: block;
    grid-template-columns: 1fr;
    width: 100%;
    max-width: 100%;
  }

  .header-stats {
    justify-content: flex-start;
  }

  .tabs,
  .content {
    width: 100%;
    max-width: 100%;
    overflow: visible;
  }

  .tabs {
    overflow-x: auto;
  }

  .controls,
  .section-grid,
  .split {
    grid-template-columns: 1fr;
    width: 100%;
    max-width: 100%;
  }

  .metrics-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    width: 100%;
    max-width: 100%;
  }

  .card-head {
    flex-wrap: wrap;
  }
}
"""


APP_JS = r"""
const state = {
  summary: null,
  job: null,
  selectedJobId: null,
  tab: 'overview',
  selection: null,
  filters: {
    timelineQuery: '',
    claimQuery: '',
    chunkQuery: '',
    visualQuery: '',
    logQuery: '',
    claimType: 'all',
    claimConfidence: 'all',
    logEvent: 'all'
  },
  index: {}
};

const els = {};

document.addEventListener('DOMContentLoaded', () => {
  els.rootLabel = document.getElementById('rootLabel');
  els.loadedAt = document.getElementById('loadedAt');
  els.refreshBtn = document.getElementById('refreshBtn');
  els.jobSearch = document.getElementById('jobSearch');
  els.jobList = document.getElementById('jobList');
  els.jobHeader = document.getElementById('jobHeader');
  els.tabs = document.getElementById('tabs');
  els.content = document.getElementById('content');
  els.inspector = document.getElementById('inspector');
  els.toast = document.getElementById('toast');

  els.refreshBtn.addEventListener('click', () => loadSummary({ keepSelection: true }));
  els.jobSearch.addEventListener('input', renderJobList);
  els.jobList.addEventListener('click', onJobListClick);
  els.tabs.addEventListener('click', onTabClick);
  els.content.addEventListener('click', onContentClick);
  els.content.addEventListener('input', onContentInput);
  els.content.addEventListener('change', onContentChange);
  els.inspector.addEventListener('click', onContentClick);

  loadSummary();
});

async function loadSummary(options = {}) {
  try {
    const response = await fetch('/api/summary');
    if (!response.ok) throw new Error(`summary failed: ${response.status}`);
    state.summary = await response.json();
    els.rootLabel.textContent = state.summary.output_root;
    els.loadedAt.textContent = `обновлено ${formatDateTime(state.summary.generated_at)}`;
    renderJobList();
    const preferred = options.keepSelection ? state.selectedJobId : null;
    const firstJob = state.summary.jobs.find((job) => job.job_id === preferred) || state.summary.jobs[0];
    if (firstJob) {
      await selectJob(firstJob.job_id);
    } else {
      state.job = null;
      renderEmptyProject();
    }
  } catch (error) {
    showToast(error.message);
    renderFatal(error);
  }
}

async function selectJob(jobId) {
  try {
    state.selectedJobId = jobId;
    state.selection = null;
    renderJobList();
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!response.ok) throw new Error(`job failed: ${response.status}`);
    state.job = await response.json();
    state.index = buildIndex(state.job);
    renderJobHeader();
    renderTabs();
    renderTab();
    renderInspector();
  } catch (error) {
    showToast(error.message);
    renderFatal(error);
  }
}

function buildIndex(job) {
  const eventsById = new Map();
  const chunksById = new Map();
  const claimsById = new Map();
  const visualsByScene = new Map();
  const framesByScene = new Map();
  const claimsByEvent = new Map();
  const claimsByChunk = new Map();
  const issuesById = new Map();

  for (const event of job.timeline_events || []) eventsById.set(event.event_id, event);
  for (const chunk of job.chunks || []) chunksById.set(chunk.chunk_id, chunk);
  for (const claim of job.style_claims || []) {
    claimsById.set(claim.claim_id, claim);
    addMapList(claimsByChunk, claim.chunk_id, claim);
    for (const eventId of claim.timeline_event_ids || []) addMapList(claimsByEvent, eventId, claim);
  }
  for (const frame of job.frame_refs || []) addMapList(framesByScene, frame.scene_id, frame);
  for (const visual of job.visual_events || []) {
    visualsByScene.set(visual.scene_id, visual);
    for (const frame of visual.frames || []) addMapList(framesByScene, frame.scene_id, frame);
  }
  for (const issue of job.quality_issues || []) issuesById.set(issue.issue_id, issue);

  return { eventsById, chunksById, claimsById, visualsByScene, framesByScene, claimsByEvent, claimsByChunk, issuesById };
}

function addMapList(map, key, value) {
  if (!key) return;
  if (!map.has(key)) map.set(key, []);
  const list = map.get(key);
  if (!list.some((item) => item.path && value.path && item.path === value.path)) list.push(value);
}

function renderJobList() {
  if (!state.summary) return;
  const query = normalize(els.jobSearch.value);
  const jobs = state.summary.jobs.filter((job) => normalize([
    job.job_id,
    job.video_id,
    job.title,
    job.channel,
    job.status
  ].join(' ')).includes(query));

  if (!jobs.length) {
    els.jobList.innerHTML = '<div class="empty">Запуски не найдены</div>';
    return;
  }

  els.jobList.innerHTML = jobs.map((job) => `
    <button class="job-item ${job.job_id === state.selectedJobId ? 'active' : ''}" type="button" data-job-id="${attr(job.job_id)}">
      <span class="job-title">${escapeHtml(job.title || job.job_id)}</span>
      <span class="job-meta">
        ${statusPill(job.status)}
        <span>${escapeHtml(job.channel || 'channel unknown')}</span>
        <span>${formatDuration(job.duration)}</span>
        <span>${escapeHtml(job.job_id)}</span>
      </span>
    </button>
  `).join('');
}

function renderEmptyProject() {
  els.jobHeader.innerHTML = '';
  els.content.innerHTML = '<div class="empty">В output root пока нет job artifacts</div>';
  els.inspector.innerHTML = '';
}

function renderFatal(error) {
  els.content.innerHTML = `<div class="empty">Ошибка загрузки: ${escapeHtml(error.message)}</div>`;
}

function onJobListClick(event) {
  const item = event.target.closest('[data-job-id]');
  if (!item) return;
  selectJob(item.dataset.jobId);
}

function onTabClick(event) {
  const button = event.target.closest('[data-tab]');
  if (!button) return;
  state.tab = button.dataset.tab;
  state.selection = null;
  renderTabs();
  renderTab();
  renderInspector();
}

function renderTabs() {
  for (const button of els.tabs.querySelectorAll('[data-tab]')) {
    button.classList.toggle('active', button.dataset.tab === state.tab);
  }
}

function renderJobHeader() {
  const job = state.job.job || {};
  const video = state.job.video_info || {};
  const counts = state.job.derived?.counts || {};
  const title = video.title || job.title || job.job_id;
  const url = video.url || job.url || '';
  els.jobHeader.innerHTML = `
    <div>
      <h2>${escapeHtml(title)}</h2>
      <p>${escapeHtml(video.channel || job.channel || 'channel unknown')} · ${escapeHtml(job.job_id || '')} · ${formatDuration(video.duration || job.duration)}</p>
      ${url ? `<p><a class="link" href="${attr(url)}" target="_blank" rel="noreferrer">YouTube</a></p>` : ''}
    </div>
    <div class="header-stats">
      ${statusPill(job.status)}
      <span class="chip teal">${num(counts.timeline_events)} events</span>
      <span class="chip rust">${num(counts.chunks)} chunks</span>
      <span class="chip indigo">${num(counts.style_claims)} claims</span>
      <span class="chip gold">${num(counts.frame_refs)} frames</span>
    </div>
  `;
}

function renderTab() {
  if (!state.job) return;
  if (state.tab === 'overview') renderOverview();
  if (state.tab === 'timeline') renderTimeline();
  if (state.tab === 'claims') renderClaims();
  if (state.tab === 'chunks') renderChunks();
  if (state.tab === 'visuals') renderVisuals();
  if (state.tab === 'logs') renderLogs();
}

function renderOverview() {
  const job = state.job;
  const counts = job.derived?.counts || {};
  const quality = job.quality_report || {};
  const coverage = quality.coverage || {};
  const mismatches = quality.mismatches || {};
  els.content.innerHTML = `
    <div class="metrics-grid">
      ${metric('Timeline', counts.timeline_events, 'events')}
      ${metric('Chunks', counts.chunks, 'knowledge blocks')}
      ${metric('Claims', counts.style_claims, 'style facts')}
      ${metric('Visuals', counts.visual_events, 'scene analyses')}
      ${metric('Frames', counts.frame_refs, 'keyframes')}
      ${metric('Speakers', counts.speakers, 'diarized')}
    </div>
    <div class="section-grid">
      <div>
        <section class="section">
          <div class="section-title"><h3>Стадии</h3><span class="muted tiny">${num(job.stages.length)} rows</span></div>
          ${stageTable(job.stages)}
        </section>
        <section class="section">
          <div class="section-title"><h3>Artifacts</h3></div>
          ${artifactTable(job.artifacts || [])}
        </section>
      </div>
      <div>
        <section class="section">
          <div class="section-title"><h3>Coverage</h3></div>
          ${coverageBlock(coverage)}
        </section>
        <section class="section">
          <div class="section-title"><h3>Warnings / errors</h3></div>
          ${issuesBlock(job.quality_issues || [], quality.warnings || [], quality.errors || [])}
        </section>
        <section class="section">
          <div class="section-title"><h3>Presenter profile</h3></div>
          ${presenterBlock(job.presenter_profile)}
        </section>
        <section class="section">
          <div class="section-title"><h3>Top topics</h3></div>
          ${topicCloud(job.derived?.top_topics || [])}
        </section>
        <section class="section">
          <div class="section-title"><h3>Mismatches</h3></div>
          ${keyValueList(mismatches)}
        </section>
      </div>
    </div>
  `;
}

function renderTimeline() {
  const query = state.filters.timelineQuery;
  const events = (state.job.timeline_events || []).filter((item) => matchesQuery(item, query, [
    'event_id', 'scene_id', 'speech_text', 'visual_summary', 'on_screen_text', 'items', 'topics'
  ]));
  els.content.innerHTML = `
    ${searchControls('timelineQuery', query, 'Поиск по речи, OCR, scene id, topics')}
    <div class="card-list">
      ${events.length ? events.map((event) => timelineCard(event)).join('') : empty('Нет timeline events')}
    </div>
  `;
}

function renderClaims() {
  const query = state.filters.claimQuery;
  const types = optionValues(state.job.style_claims, 'claim_type');
  const confidences = optionValues(state.job.style_claims, 'confidence');
  const claims = (state.job.style_claims || []).filter((claim) => {
    if (state.filters.claimType !== 'all' && claim.claim_type !== state.filters.claimType) return false;
    if (state.filters.claimConfidence !== 'all' && claim.confidence !== state.filters.claimConfidence) return false;
    return matchesQuery(claim, query, ['claim_id', 'subject', 'claim', 'rationale', 'topics', 'prefer', 'avoid', 'evidence']);
  });
  els.content.innerHTML = `
    <div class="controls">
      ${searchInput('claimQuery', query, 'Поиск по claims, evidence, topics')}
      ${selectInput('claimType', 'Тип', state.filters.claimType, ['all', ...types])}
      ${selectInput('claimConfidence', 'Confidence', state.filters.claimConfidence, ['all', ...confidences])}
      <div class="metric"><div class="label">Показано</div><div class="value">${num(claims.length)}</div></div>
    </div>
    <div class="card-list">
      ${claims.length ? claims.map((claim) => claimCard(claim)).join('') : empty('Нет claims по фильтру')}
    </div>
  `;
}

function renderChunks() {
  const query = state.filters.chunkQuery;
  const chunks = (state.job.chunks || []).filter((chunk) => matchesQuery(chunk, query, [
    'chunk_id', 'chunk_title', 'boundary_reason', 'speech_text', 'visual_text', 'combined_text', 'topics', 'entities'
  ]));
  els.content.innerHTML = `
    ${searchControls('chunkQuery', query, 'Поиск по chunks, topics, entities')}
    <div class="card-list">
      ${chunks.length ? chunks.map((chunk) => chunkCard(chunk)).join('') : empty('Нет chunks')}
    </div>
  `;
}

function renderVisuals() {
  const query = state.filters.visualQuery;
  const visuals = (state.job.visual_events || []).filter((visual) => matchesQuery(visual, query, [
    'visual_event_id', 'scene_id', 'visual_summary', 'observations', 'interpretations', 'on_screen_text', 'items', 'style_topics', 'notes'
  ]));
  els.content.innerHTML = `
    ${searchControls('visualQuery', query, 'Поиск по visuals, OCR, items')}
    <div class="card-list">
      ${visuals.length ? visuals.map((visual) => visualCard(visual)).join('') : empty('Нет visual events')}
    </div>
  `;
}

function renderLogs() {
  const eventTypes = optionValues(state.job.pipeline_events, 'event');
  const query = state.filters.logQuery;
  const events = (state.job.pipeline_events || []).filter((event) => {
    if (state.filters.logEvent !== 'all' && event.event !== state.filters.logEvent) return false;
    return matchesQuery(event, query, ['event', 'stage', 'status', 'message', 'data', 'error_code']);
  });
  els.content.innerHTML = `
    <div class="section">
      <div class="section-title"><h3>Stages</h3></div>
      ${stageTable(state.job.stages || [])}
    </div>
    <div class="controls">
      ${searchInput('logQuery', query, 'Поиск по pipeline events')}
      ${selectInput('logEvent', 'Event', state.filters.logEvent, ['all', ...eventTypes])}
      <div></div>
      <div class="metric"><div class="label">Показано</div><div class="value">${num(events.length)}</div></div>
    </div>
    <div class="card-list">
      ${events.length ? events.map((event, index) => logCard(event, index)).join('') : empty('Нет events')}
    </div>
  `;
}

function stageTable(stages) {
  if (!stages?.length) return empty('Нет stage state в SQLite');
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Stage</th><th>Status</th><th>Attempt</th><th>Duration</th><th>Metrics</th></tr></thead>
        <tbody>
          ${stages.map((stage) => `
            <tr>
              <td>${num(stage.ordinal)}</td>
              <td><button class="row-button" type="button" data-inspect-kind="stage" data-inspect-id="${attr(stage.stage_name)}">${escapeHtml(stage.stage_name)}</button></td>
              <td>${statusPill(stage.status)}</td>
              <td>${num(stage.attempt)}</td>
              <td>${formatDuration(stage.duration_seconds)}</td>
              <td class="tiny">${escapeHtml(compactObject(stage.metrics))}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function artifactTable(artifacts) {
  if (!artifacts.length) return empty('Нет artifact metadata');
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>Artifact</th><th>State</th><th>Size</th><th>Modified</th></tr></thead>
        <tbody>
          ${artifacts.map((artifact) => `
            <tr>
              <td><span class="mono">${escapeHtml(artifact.key)}</span><div class="tiny muted">${escapeHtml(artifact.relative_path || artifact.path)}</div></td>
              <td>${artifact.exists ? '<span class="status ok">exists</span>' : '<span class="status unknown">missing</span>'}</td>
              <td>${formatBytes(artifact.size_bytes)}</td>
              <td>${formatDateTime(artifact.mtime)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function timelineCard(event) {
  const frames = state.index.framesByScene.get(event.scene_id) || [];
  const claims = state.index.claimsByEvent.get(event.event_id) || [];
  const visual = state.index.visualsByScene.get(event.scene_id);
  return `
    <article class="card" data-inspect-kind="event" data-inspect-id="${attr(event.event_id)}">
      <div class="card-head">
        <div>
          <h3 class="card-title">${formatRange(event.start, event.end)} · ${escapeHtml(event.scene_id || '')}</h3>
          <p class="card-subtitle">${num(claims.length)} claims · ${escapeHtml(event.event_id || '')}</p>
        </div>
        <a class="button button-small" href="${attr(event.timestamp_url || '#')}" target="_blank" rel="noreferrer">YouTube</a>
      </div>
      ${chips(event.topics, 'teal')}
      ${event.on_screen_text?.length ? `<div>${chips(event.on_screen_text, 'gold')}</div>` : ''}
      <div class="split">
        <div class="text-block">${escapeHtml(truncate(event.speech_text || '', 700))}</div>
        <div class="text-block muted">${escapeHtml(truncate(event.visual_summary || visual?.visual_summary || '', 700))}</div>
      </div>
      ${frameStrip(frames)}
    </article>
  `;
}

function claimCard(claim) {
  const frames = framesForEventIds(claim.timeline_event_ids || []);
  return `
    <article class="card" data-inspect-kind="claim" data-inspect-id="${attr(claim.claim_id)}">
      <div class="card-head">
        <div>
          <h3 class="card-title">${escapeHtml(claim.subject || claim.claim_type || 'claim')}</h3>
          <p class="card-subtitle">${formatRange(claim.start, claim.end)} · ${escapeHtml(claim.claim_id || '')}</p>
        </div>
        <div>${statusPill(claim.claim_type, 'status unknown')} ${statusPill(claim.confidence)}</div>
      </div>
      <div class="text-block">${escapeHtml(claim.claim || '')}</div>
      ${chips(claim.topics, 'teal')}
      ${claim.prefer?.length ? `<p class="tiny"><strong>Prefer:</strong> ${escapeHtml(claim.prefer.join(', '))}</p>` : ''}
      ${claim.avoid?.length ? `<p class="tiny"><strong>Avoid:</strong> ${escapeHtml(claim.avoid.join(', '))}</p>` : ''}
      ${claim.evidence?.length ? `<p class="tiny muted">${escapeHtml(truncate(claim.evidence.join(' · '), 320))}</p>` : ''}
      ${frameStrip(frames.slice(0, 6))}
    </article>
  `;
}

function chunkCard(chunk) {
  const claims = state.index.claimsByChunk.get(chunk.chunk_id) || [];
  const frames = framesForEventIds(chunk.timeline_event_ids || []);
  return `
    <article class="card" data-inspect-kind="chunk" data-inspect-id="${attr(chunk.chunk_id)}">
      <div class="card-head">
        <div>
          <h3 class="card-title">${escapeHtml(chunk.chunk_title || chunk.chunk_id)}</h3>
          <p class="card-subtitle">${formatRange(chunk.start, chunk.end)} · ${num(claims.length)} claims · ${num((chunk.timeline_event_ids || []).length)} events</p>
        </div>
        <a class="button button-small" href="${attr(chunk.timestamp_url || '#')}" target="_blank" rel="noreferrer">YouTube</a>
      </div>
      ${chips(chunk.topics, 'teal')}
      ${chunk.entities?.length ? chips(chunk.entities, 'rust') : ''}
      <p class="tiny muted">${escapeHtml(chunk.boundary_reason || '')}</p>
      <div class="text-block">${escapeHtml(truncate(chunk.combined_text || chunk.speech_text || '', 900))}</div>
      ${frameStrip(frames.slice(0, 8))}
    </article>
  `;
}

function visualCard(visual) {
  const frames = state.index.framesByScene.get(visual.scene_id) || visual.frames || [];
  return `
    <article class="card" data-inspect-kind="visual" data-inspect-id="${attr(visual.visual_event_id)}">
      <div class="card-head">
        <div>
          <h3 class="card-title">${formatRange(visual.start, visual.end)} · ${escapeHtml(visual.scene_id || '')}</h3>
          <p class="card-subtitle">${escapeHtml(visual.visual_event_id || '')}</p>
        </div>
        ${statusPill(visual.confidence)}
      </div>
      ${visual.style_topics?.length ? chips(visual.style_topics, 'teal') : ''}
      ${visual.on_screen_text?.length ? chips(visual.on_screen_text, 'gold') : ''}
      <div class="text-block">${escapeHtml(truncate([
        visual.visual_summary,
        ...(visual.observations || []),
        ...(visual.interpretations || [])
      ].filter(Boolean).join('\n'), 700))}</div>
      ${frameStrip(frames)}
    </article>
  `;
}

function logCard(event, index) {
  const id = event.event_id || String(index);
  return `
    <article class="card" data-inspect-kind="log" data-inspect-id="${attr(id)}">
      <div class="card-head">
        <div>
          <h3 class="card-title">${escapeHtml(event.event || 'event')} ${event.stage ? `· ${escapeHtml(event.stage)}` : ''}</h3>
          <p class="card-subtitle">${formatDateTime(event.timestamp)} · seq ${num(event.seq)}</p>
        </div>
        ${statusPill(event.status || 'unknown')}
      </div>
      <div class="text-block">${escapeHtml(event.message || '')}</div>
      ${event.data?.warnings ? `<p class="tiny muted">${escapeHtml(event.data.warnings.join(' · '))}</p>` : ''}
    </article>
  `;
}

function coverageBlock(coverage) {
  const entries = Object.entries(coverage || {});
  if (!entries.length) return empty('Coverage не найден');
  return `<div class="progress-list">${entries.map(([key, value]) => `
    <div class="progress-row">
      <div class="tiny">${escapeHtml(key)}</div>
      <div class="bar"><span style="width:${Math.max(0, Math.min(100, Number(value || 0) * 100))}%"></span></div>
      <div class="tiny mono">${Math.round(Number(value || 0) * 100)}%</div>
    </div>
  `).join('')}</div>`;
}

function issuesBlock(issues, warnings, errors) {
  if (issues.length) {
    return `<div class="warning-list">${issues.map((issue) => issueCard(issue)).join('')}</div>`;
  }
  if (!warnings.length && !errors.length) return empty('Warnings и errors отсутствуют');
  return `<div class="warning-list">
    ${errors.map((item) => `<div class="warning-item error-item">${escapeHtml(item)}</div>`).join('')}
    ${warnings.map((item) => `<div class="warning-item">${escapeHtml(item)}</div>`).join('')}
  </div>`;
}

function issueCard(issue) {
  const isError = issue.severity === 'error';
  const first = issue.locations?.[0];
  return `
    <button class="issue-item ${isError ? 'error' : ''}" type="button" data-inspect-kind="issue" data-inspect-id="${attr(issue.issue_id)}">
      <span class="issue-title">${escapeHtml(issue.title || issue.issue_id)}</span>
      <span class="issue-meta">
        ${statusPill(issue.severity || 'warning')}
        <span>${num(issue.locations_count || 0)} locations</span>
        <span>${escapeHtml(issue.summary || '')}</span>
      </span>
      ${first ? `<span class="tiny muted">${escapeHtml(first.label || first.object_id || '')} · ${escapeHtml(first.field || '')}</span>` : ''}
    </button>
  `;
}

function presenterBlock(profile) {
  if (!profile) return empty('Presenter profile отсутствует');
  return `
    <div class="card">
      ${statusPill(profile.confidence)}
      <p class="text-block">${escapeHtml(profile.baseline_summary || '')}</p>
      ${chips(profile.recurring_visual_markers, 'indigo')}
      <p class="tiny muted">${escapeHtml(profile.notes || '')}</p>
    </div>
  `;
}

function topicCloud(topics) {
  if (!topics.length) return empty('Topics не найдены');
  return `<div>${topics.slice(0, 24).map((item) => `<span class="chip teal">${escapeHtml(item.topic)} ${num(item.count)}</span>`).join('')}</div>`;
}

function keyValueList(object) {
  const entries = Object.entries(object || {});
  if (!entries.length) return empty('Нет данных');
  return `<div class="card-list">${entries.map(([key, value]) => `
    <div class="card"><strong>${escapeHtml(key)}</strong><div class="muted tiny">${escapeHtml(String(value))}</div></div>
  `).join('')}</div>`;
}

function metric(label, value, note) {
  return `<div class="metric"><div class="label">${escapeHtml(label)}</div><div class="value">${num(value)}</div><div class="note">${escapeHtml(note)}</div></div>`;
}

function searchControls(key, value, placeholder) {
  return `<div class="controls"><div>${searchInput(key, value, placeholder)}</div><div></div><div></div><div></div></div>`;
}

function searchInput(key, value, placeholder) {
  const id = `filter-${key}`;
  return `<input id="${attr(id)}" class="input" type="search" data-filter-key="${attr(key)}" placeholder="${attr(placeholder)}" value="${attr(value)}">`;
}

function selectInput(key, label, value, options) {
  const id = `filter-${key}`;
  return `
    <label class="tiny muted" for="${attr(id)}">${escapeHtml(label)}
      <select id="${attr(id)}" class="select" data-filter-key="${attr(key)}">
        ${options.map((option) => `<option value="${attr(option)}" ${option === value ? 'selected' : ''}>${escapeHtml(option)}</option>`).join('')}
      </select>
    </label>
  `;
}

function onContentInput(event) {
  const input = event.target.closest('[data-filter-key]');
  if (!input || input.tagName !== 'INPUT') return;
  state.filters[input.dataset.filterKey] = input.value;
  rerenderAndFocus(input.dataset.filterKey);
}

function onContentChange(event) {
  const control = event.target.closest('[data-filter-key]');
  if (!control) return;
  state.filters[control.dataset.filterKey] = control.value;
  renderTab();
}

function rerenderAndFocus(key) {
  renderTab();
  const control = document.getElementById(`filter-${key}`);
  if (control) {
    control.focus();
    const end = control.value.length;
    control.setSelectionRange(end, end);
  }
}

function onContentClick(event) {
  const inspect = event.target.closest('[data-inspect-kind]');
  if (!inspect) return;
  if (event.target.closest('a')) return;
  state.selection = { kind: inspect.dataset.inspectKind, id: inspect.dataset.inspectId };
  renderInspector();
}

function renderInspector() {
  if (!state.job) {
    els.inspector.innerHTML = '';
    return;
  }
  const selection = state.selection;
  if (!selection) {
    const job = state.job.job || {};
    els.inspector.innerHTML = `
      <div class="inspector-head">
        <h2>${escapeHtml(job.job_id || 'Job')}</h2>
        <p>Run summary</p>
      </div>
      <div class="inspector-body">
        ${keyValueList({
          status: job.status,
          current_stage: job.current_stage || '-',
          created_at: job.created_at || '-',
          finished_at: job.finished_at || '-',
          error_code: job.error_code || '-'
        })}
        ${rawDetails('Job JSON', job)}
      </div>
    `;
    return;
  }

  const item = resolveSelection(selection);
  if (!item) {
    els.inspector.innerHTML = '<div class="inspector-head"><h2>Не найдено</h2></div>';
    return;
  }
  els.inspector.innerHTML = inspectorHtml(selection.kind, item);
}

function resolveSelection(selection) {
  if (selection.kind === 'issue') return state.index.issuesById.get(selection.id);
  if (selection.kind === 'event') return state.index.eventsById.get(selection.id);
  if (selection.kind === 'claim') return state.index.claimsById.get(selection.id);
  if (selection.kind === 'chunk') return state.index.chunksById.get(selection.id);
  if (selection.kind === 'visual') return (state.job.visual_events || []).find((item) => item.visual_event_id === selection.id);
  if (selection.kind === 'stage') return (state.job.stages || []).find((item) => item.stage_name === selection.id);
  if (selection.kind === 'log') return (state.job.pipeline_events || []).find((item) => (item.event_id || String(item.seq)) === selection.id);
  return null;
}

function inspectorHtml(kind, item) {
  const title = item.title || item.claim_id || item.chunk_id || item.event_id || item.visual_event_id || item.stage_name || item.event || kind;
  let frames = [];
  if (kind === 'event') frames = state.index.framesByScene.get(item.scene_id) || [];
  if (kind === 'claim') frames = framesForEventIds(item.timeline_event_ids || []);
  if (kind === 'chunk') frames = framesForEventIds(item.timeline_event_ids || []);
  if (kind === 'visual') frames = state.index.framesByScene.get(item.scene_id) || item.frames || [];
  if (kind === 'issue') frames = framesForIssue(item);
  return `
    <div class="inspector-head">
      <h2>${escapeHtml(title)}</h2>
      <p>${escapeHtml(kind)} ${item.start != null ? `· ${formatRange(item.start, item.end)}` : ''}</p>
    </div>
    <div class="inspector-body">
      ${item.timestamp_url ? `<p><a class="button" href="${attr(item.timestamp_url)}" target="_blank" rel="noreferrer">Открыть timestamp</a></p>` : ''}
      ${frames.length ? frameStrip(frames.slice(0, 12)) : ''}
      ${summaryForInspector(kind, item)}
      ${rawDetails('Raw JSON', item)}
    </div>
  `;
}

function summaryForInspector(kind, item) {
  if (kind === 'issue') {
    const locations = item.locations || [];
    return `
      ${statusPill(item.severity || 'warning')}
      <p class="text-block">${escapeHtml(item.summary || '')}</p>
      <p class="tiny muted">Source: ${escapeHtml(item.source || '-')}</p>
      <h3>Locations</h3>
      ${locations.length ? `<div class="location-list">${locations.map((location) => locationCard(location, item.severity)).join('')}</div>` : empty('Конкретные locations не найдены в текущих artifacts')}
    `;
  }
  if (kind === 'event') {
    const claims = state.index.claimsByEvent.get(item.event_id) || [];
    return `
      ${chips(item.topics, 'teal')}
      ${chips(item.on_screen_text, 'gold')}
      <h3>Speech</h3><p class="text-block">${escapeHtml(item.speech_text || '')}</p>
      <h3>Visual</h3><p class="text-block muted">${escapeHtml(item.visual_summary || '')}</p>
      <h3>Claims</h3>${claims.length ? claims.map((claim) => `<p class="tiny"><strong>${escapeHtml(claim.claim_type)}</strong> ${escapeHtml(claim.claim)}</p>`).join('') : empty('Нет claims')}
    `;
  }
  if (kind === 'claim') {
    return `
      ${statusPill(item.claim_type, 'status unknown')} ${statusPill(item.confidence)}
      ${chips(item.topics, 'teal')}
      <p class="text-block">${escapeHtml(item.claim || '')}</p>
      <p><strong>Rationale:</strong> ${escapeHtml(item.rationale || '')}</p>
      ${item.evidence?.length ? `<h3>Evidence</h3><ul>${item.evidence.map((text) => `<li>${escapeHtml(text)}</li>`).join('')}</ul>` : ''}
    `;
  }
  if (kind === 'chunk') {
    return `
      ${chips(item.topics, 'teal')}
      ${chips(item.entities, 'rust')}
      <p class="tiny muted">${escapeHtml(item.boundary_reason || '')}</p>
      <h3>Combined</h3><p class="text-block">${escapeHtml(item.combined_text || '')}</p>
    `;
  }
  if (kind === 'visual') {
    return `
      ${statusPill(item.confidence)}
      ${chips(item.style_topics, 'teal')}
      ${chips(item.on_screen_text, 'gold')}
      <p class="text-block">${escapeHtml([item.visual_summary, ...(item.observations || []), ...(item.interpretations || [])].filter(Boolean).join('\n'))}</p>
    `;
  }
  if (kind === 'stage') {
    return `
      ${statusPill(item.status)}
      ${keyValueList({
        attempt: item.attempt,
        duration: formatDuration(item.duration_seconds),
        error_code: item.error_code || '-',
        error_message: item.error_message || '-',
        started_at: item.started_at || '-',
        finished_at: item.finished_at || '-'
      })}
    `;
  }
  if (kind === 'log') {
    return `<p class="text-block">${escapeHtml(item.message || '')}</p>`;
  }
  return '';
}

function locationCard(location, severity) {
  const relatedKind = location.related_kind || '';
  const relatedId = location.related_id || '';
  const canInspect = relatedKind && relatedId;
  const attrs = canInspect ? `data-inspect-kind="${attr(relatedKind)}" data-inspect-id="${attr(relatedId)}"` : '';
  const tag = canInspect ? 'button' : 'div';
  return `
    <${tag} class="location-item ${severity === 'error' ? 'error' : ''}" ${canInspect ? 'type="button"' : ''} ${attrs}>
      <span class="location-title">${escapeHtml(location.label || location.object_id || location.kind || 'location')}</span>
      <span class="location-meta">
        <span>${escapeHtml(location.kind || '-')}</span>
        <span>${escapeHtml(location.field || '-')}</span>
        ${location.timestamp_url ? `<span>${formatRange(location.start, location.end)}</span>` : ''}
      </span>
      <span class="tiny">${escapeHtml(truncate(location.marker || location.preview || '', 240))}</span>
    </${tag}>
  `;
}

function framesForIssue(issue) {
  const frames = [];
  const seen = new Set();
  for (const location of issue.locations || []) {
    if (location.kind === 'visual_event') {
      const sceneFrames = state.index.framesByScene.get(location.scene_id) || [];
      for (const frame of sceneFrames) pushFrame(frames, seen, frame);
    }
    if (location.kind === 'chunk') {
      for (const frame of framesForEventIds(location.timeline_event_ids || [])) pushFrame(frames, seen, frame);
    }
  }
  return frames;
}

function pushFrame(frames, seen, frame) {
  const key = frame.path || `${frame.scene_id}-${frame.timestamp}`;
  if (seen.has(key)) return;
  seen.add(key);
  frames.push(frame);
}

function framesForEventIds(eventIds) {
  const frames = [];
  const seen = new Set();
  for (const eventId of eventIds) {
    const event = state.index.eventsById.get(eventId);
    if (!event) continue;
    for (const frame of state.index.framesByScene.get(event.scene_id) || []) {
      const key = frame.path || `${frame.scene_id}-${frame.timestamp}`;
      if (!seen.has(key)) {
        seen.add(key);
        frames.push(frame);
      }
    }
  }
  return frames;
}

function frameStrip(frames) {
  if (!frames?.length) return '';
  return `
    <div class="frame-strip">
      ${frames.map((frame) => `
        <a class="frame" href="${attr(frame.timestamp_url || '#')}" target="_blank" rel="noreferrer" title="${attr(formatRange(frame.start, frame.end))}">
          <img loading="lazy" src="${attr(mediaUrl(frame.path))}" alt="${attr(frame.scene_id || 'frame')}">
          <span>${escapeHtml(formatTime(frame.timestamp ?? frame.start))}</span>
        </a>
      `).join('')}
    </div>
  `;
}

function mediaUrl(path) {
  const jobId = state.selectedJobId || state.job?.job?.job_id || '';
  const relative = relativeFramePath(path || '');
  return `/media/${encodeURIComponent(jobId)}/${relative.split('/').map(encodeURIComponent).join('/')}`;
}

function relativeFramePath(path) {
  const jobId = state.selectedJobId || '';
  const marker = `/jobs/${jobId}/`;
  const index = path.indexOf(marker);
  if (index >= 0) return path.slice(index + marker.length);
  return path.replace(/^\.?\//, '');
}

function chips(items, tone = '') {
  if (!items?.length) return '';
  return `<div>${items.slice(0, 18).map((item) => `<span class="chip ${tone}">${escapeHtml(item)}</span>`).join('')}</div>`;
}

function rawDetails(title, object) {
  return `<details><summary>${escapeHtml(title)}</summary><pre>${escapeHtml(JSON.stringify(object, null, 2))}</pre></details>`;
}

function matchesQuery(item, query, keys) {
  const needle = normalize(query);
  if (!needle) return true;
  const text = keys.map((key) => JSON.stringify(item?.[key] ?? '')).join(' ');
  return normalize(text).includes(needle);
}

function optionValues(items, key) {
  return [...new Set((items || []).map((item) => item[key]).filter(Boolean))].sort();
}

function statusPill(value, fallbackClass = '') {
  const text = value || 'unknown';
  const klass = fallbackClass || `status ${cssToken(text)}`;
  return `<span class="${attr(klass)}">${escapeHtml(text)}</span>`;
}

function empty(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

function compactObject(object) {
  const entries = Object.entries(object || {});
  if (!entries.length) return '';
  return entries.slice(0, 4).map(([key, value]) => `${key}=${JSON.stringify(value)}`).join(', ') + (entries.length > 4 ? `, +${entries.length - 4}` : '');
}

function formatTime(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return '--:--';
  const total = Math.max(0, Math.floor(value));
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  return `${minutes}:${String(secs).padStart(2, '0')}`;
}

function formatRange(start, end) {
  return `${formatTime(start)}-${formatTime(end)}`;
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return '-';
  if (value >= 3600) {
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    return `${hours}h ${minutes}m`;
  }
  if (value >= 60) return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
  return `${Math.round(value)}s`;
}

function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value)) return '-';
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function formatDateTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('ru-RU', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  });
}

function truncate(text, maxLength) {
  const value = String(text || '');
  if (value.length <= maxLength) return value;
  return `${value.slice(0, Math.max(0, maxLength - 1))}…`;
}

function normalize(value) {
  return String(value || '').toLocaleLowerCase('ru-RU');
}

function num(value) {
  if (value === null || value === undefined || value === '') return '0';
  const number = Number(value);
  if (!Number.isFinite(number)) return escapeHtml(String(value));
  return new Intl.NumberFormat('ru-RU').format(number);
}

function cssToken(value) {
  return String(value || 'unknown').toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function attr(value) {
  return escapeHtml(value);
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.add('show');
  window.setTimeout(() => els.toast.classList.remove('show'), 2400);
}
"""
