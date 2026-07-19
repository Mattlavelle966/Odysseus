import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const MODAL_ID = 'email-research-worker-modal';
const API = '/api/email-research';
let instances = [];
let accounts = [];
let endpoints = [];
let selectedId = null;
let liveSource = null;
let countdownTimer = null;
let latestStatus = null;

const defaults = {
  enabled: false,
  email_account_id: '',
  research_endpoint_id: '',
  research_model: '',
  polling_interval_seconds: 60,
  required_subject_prefix: 'Research',
  allowed_sender_addresses: [],
  allowed_sender_domains: [],
  blocked_sender_patterns: ['no-reply', 'noreply', 'mailer-daemon'],
  maximum_request_length: 50000,
  maximum_attachment_size: 1048576,
  maximum_simultaneous_jobs: 1,
  maximum_requests_per_sender_per_day: 5,
  maximum_total_jobs_per_day: 50,
  acknowledgement_template: 'Your research request has been received and is processing. Reference: {{job_reference}}',
  final_email_template: 'Research is complete. A concise summary is below and the full report is attached.',
  html_attachment_threshold: 200000,
  retry_limit: 5,
  research_depth_preset: 'standard',
  research_rounds: 3,
  research_source_limit: 30,
  research_timeout_seconds: 1800,
  early_stop_no_useful_findings: true,
  allow_plain_text_attachments: true,
};

const depthPresets = {
  quick: { rounds: 2, sources: 12, timeout: 600 },
  standard: { rounds: 3, sources: 30, timeout: 1800 },
  deep: { rounds: 5, sources: 60, timeout: 5400 },
};

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const splitList = value => String(value || '').split(/[\n,]/)
  .map(item => item.trim()).filter(Boolean);

async function request(url, options = {}) {
  const response = await fetch(url, {
    credentials: 'same-origin',
    ...options,
    headers: options.body
      ? { 'Content-Type': 'application/json', ...(options.headers || {}) }
      : options.headers,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `Request failed (${response.status})`);
  return data;
}

function showMessage(text, ok = true) {
  const node = $('erw-message');
  if (!node) return;
  node.textContent = text;
  node.style.color = ok ? 'var(--green,#50fa7b)' : 'var(--red,#ff5555)';
}

function injectStyles() {
  if ($('email-research-worker-styles')) return;
  const style = document.createElement('style');
  style.id = 'email-research-worker-styles';
  style.textContent = `
    #${MODAL_ID} .modal-content{width:min(1180px,96vw);height:min(850px,94vh);background:var(--bg);display:flex;flex-direction:column}
    .erw-body{display:grid;grid-template-columns:250px minmax(0,1fr);min-height:0;flex:1;padding:0!important}
    .erw-sidebar{border-right:1px solid var(--border);display:flex;flex-direction:column;min-height:0;padding:12px}
    .erw-sidebar-head{display:flex;gap:7px;margin-bottom:10px}
    .erw-instance-list{overflow:auto;display:flex;flex-direction:column;gap:7px}
    .erw-instance{background:color-mix(in srgb,var(--fg) 3%,transparent);border:1px solid var(--border);border-radius:7px;cursor:pointer;padding:9px;text-align:left;color:var(--fg)}
    .erw-instance.active{border-color:var(--accent,var(--red));background:color-mix(in srgb,var(--accent,var(--red)) 9%,transparent)}
    .erw-instance-title{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:650}
    .erw-dot{width:7px;height:7px;border-radius:50%;background:#777;flex:none}
    .erw-dot.on{background:var(--green,#50fa7b);box-shadow:0 0 7px color-mix(in srgb,var(--green,#50fa7b) 70%,transparent)}
    .erw-instance-meta{font-size:10px;opacity:.58;margin-top:5px;line-height:1.45}
    .erw-main{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:auto}
    .erw-editor{padding:14px 16px;border-bottom:1px solid var(--border)}
    .erw-editor-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px 14px}
    .erw-field{display:flex;flex-direction:column;gap:4px;min-width:0}
    .erw-field.wide{grid-column:1/-1}
    .erw-field label{font-size:10px;opacity:.64;font-weight:650;text-transform:uppercase;letter-spacing:.035em}
    .erw-input{width:100%;padding:7px 8px;background:var(--bg);border:1px solid var(--border);border-radius:5px;color:var(--fg);font:inherit;font-size:11px}
    textarea.erw-input{resize:vertical;min-height:52px}
    .erw-inline{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}
    .erw-actions{display:flex;align-items:center;gap:7px;margin-top:12px;flex-wrap:wrap}
    .erw-activity{padding:14px 16px;min-height:260px}
    .erw-activity-head{display:flex;align-items:center;gap:8px;margin-bottom:9px}
    .erw-live{font-size:10px;color:var(--green,#50fa7b);display:flex;align-items:center;gap:5px}
    .erw-table-wrap{overflow:auto;max-height:310px;border:1px solid var(--border);border-radius:6px}
    .erw-table{border-collapse:collapse;width:100%;font-size:10px}
    .erw-table th{position:sticky;top:0;background:var(--bg);z-index:1;text-align:left;opacity:.65;padding:7px}
    .erw-table td{border-top:1px solid var(--border);padding:7px;vertical-align:top}
    .erw-counts{font-size:10px;opacity:.65;margin-left:auto}
    .erw-editor details{grid-column:1/-1;border-top:1px solid var(--border);padding-top:8px}
    .erw-editor summary{cursor:pointer;font-size:11px;font-weight:650;margin-bottom:8px}
    @media(max-width:760px){.erw-body{grid-template-columns:1fr}.erw-sidebar{border-right:0;border-bottom:1px solid var(--border);max-height:180px}.erw-editor-grid{grid-template-columns:1fr}.erw-field.wide{grid-column:auto}}
  `;
  document.head.appendChild(style);
}

function buildModal() {
  injectStyles();
  const modal = document.createElement('div');
  modal.id = MODAL_ID;
  modal.className = 'modal';
  modal.innerHTML = `
    <div class="modal-content" role="dialog" aria-label="Deep Research Email Worker">
      <div class="modal-header">
        <h3 style="margin:0;font-size:14px;display:flex;align-items:center;gap:7px">
          Deep Research Email Worker
          <span class="erw-live"><span class="erw-dot on"></span>live</span>
        </h3>
        <button class="close-btn" type="button" aria-label="Close">&times;</button>
      </div>
      <div class="modal-body erw-body">
        <aside class="erw-sidebar">
          <div class="erw-sidebar-head">
            <button class="admin-btn-add" id="erw-new" style="flex:1">+ New automation</button>
          </div>
          <div class="erw-instance-list" id="erw-instance-list"></div>
        </aside>
        <section class="erw-main">
          <div class="erw-editor">
            <div class="erw-editor-grid">
              <div class="erw-field wide"><label>Name</label><input class="erw-input" id="erw-name" placeholder="Client research inbox"></div>
              <div class="erw-field"><label>Email account</label><select class="erw-input" id="erw-account"></select></div>
              <div class="erw-field"><label>Model endpoint</label><select class="erw-input" id="erw-endpoint"></select></div>
              <div class="erw-field"><label>Research model</label><input class="erw-input" id="erw-model" placeholder="Default Deep Research model"></div>
              <div class="erw-field"><label>Research depth</label><select class="erw-input" id="erw-depth"><option value="quick">Quick · 2 rounds · 12 sources · 10 min</option><option value="standard">Standard · 3 rounds · 30 sources · 30 min</option><option value="deep">Deep · 5 rounds · 60 sources · 90 min</option><option value="custom">Custom</option></select></div>
              <div class="erw-field wide" id="erw-custom-row"><label>Custom rounds / sources / timeout seconds</label><div class="erw-inline"><input class="erw-input" id="erw-rounds" type="number" min="1" max="20"><input class="erw-input" id="erw-sources" type="number" min="1" max="500"><input class="erw-input" id="erw-timeout" type="number" min="60" max="86400"></div></div>
              <div class="erw-field"><label>Poll seconds / concurrency</label><div class="erw-inline" style="grid-template-columns:1fr 1fr"><input class="erw-input" id="erw-poll" type="number" min="15"><input class="erw-input" id="erw-concurrency" type="number" min="1" max="16"></div></div>
              <div class="erw-field"><label>Required subject prefix</label><input class="erw-input" id="erw-prefix" placeholder="Optional, e.g. Research"></div>
              <div class="erw-field"><label>Allowed sender addresses</label><textarea class="erw-input" id="erw-addresses" placeholder="client@example.com"></textarea></div>
              <div class="erw-field"><label>Allowed sender domains</label><textarea class="erw-input" id="erw-domains" placeholder="trusted.org"></textarea></div>
              <div class="erw-field wide"><label><input id="erw-early-stop" type="checkbox"> Stop early when a completed round finds no useful new information</label></div>
              <details>
                <summary>Advanced filtering, limits, delivery, and retries</summary>
                <div class="erw-editor-grid">
                  <div class="erw-field"><label>Blocked sender patterns</label><textarea class="erw-input" id="erw-blocked"></textarea></div>
                  <div class="erw-field"><label>Request / attachment bytes</label><div class="erw-inline" style="grid-template-columns:1fr 1fr"><input class="erw-input" id="erw-max-request" type="number"><input class="erw-input" id="erw-max-attachment" type="number"></div></div>
                  <div class="erw-field"><label>Per-sender / total daily jobs</label><div class="erw-inline" style="grid-template-columns:1fr 1fr"><input class="erw-input" id="erw-sender-day" type="number"><input class="erw-input" id="erw-total-day" type="number"></div></div>
                  <div class="erw-field"><label>HTML threshold / retry limit</label><div class="erw-inline" style="grid-template-columns:1fr 1fr"><input class="erw-input" id="erw-html-threshold" type="number"><input class="erw-input" id="erw-retry" type="number"></div></div>
                  <div class="erw-field wide"><label>Acknowledgement template</label><textarea class="erw-input" id="erw-ack"></textarea></div>
                  <div class="erw-field wide"><label>Final reply template</label><textarea class="erw-input" id="erw-final"></textarea></div>
                  <div class="erw-field wide"><label><input id="erw-plain-attachments" type="checkbox"> Allow safe plain-text inbound attachments</label></div>
                </div>
              </details>
            </div>
            <div class="erw-actions">
              <label style="font-size:11px;display:flex;align-items:center;gap:5px"><input id="erw-enabled" type="checkbox"> Enabled</label>
              <span id="erw-message" style="font-size:10px;flex:1"></span>
              <button class="admin-btn-sm" id="erw-test-ack">Test acknowledgement</button>
              <button class="admin-btn-sm" id="erw-test-life">Mock lifecycle</button>
              <button class="admin-btn-sm" id="erw-run">Run now</button>
              <button class="admin-btn-sm" id="erw-delete">Delete</button>
              <button class="admin-btn-add" id="erw-save">Save automation</button>
            </div>
          </div>
          <div class="erw-activity">
            <div class="erw-activity-head">
              <strong style="font-size:12px">Live worker activity</strong>
              <span id="erw-worker-state" style="font-size:10px;opacity:.65"></span>
              <span class="erw-counts" id="erw-counts"></span>
            </div>
            <div class="erw-table-wrap">
              <table class="erw-table">
                <thead><tr><th>Automation</th><th>State</th><th>Sender / subject</th><th>Progress</th><th>Updated</th><th></th></tr></thead>
                <tbody id="erw-jobs"><tr><td colspan="6">Waiting for live status…</td></tr></tbody>
              </table>
            </div>
          </div>
        </section>
      </div>
    </div>`;
  document.body.appendChild(modal);
  wireModal(modal);
  return modal;
}

function setOptions() {
  const account = $('erw-account');
  account.innerHTML = '<option value="">Select an account…</option>' + accounts.map(item =>
    `<option value="${esc(item.id)}">${esc(item.name)} — ${esc(item.from_address)}${item.has_inbox && item.has_outbox ? '' : ' (incomplete)'}</option>`
  ).join('');
  $('erw-endpoint').innerHTML = '<option value="">Default Deep Research endpoint</option>' + endpoints.map(item =>
    `<option value="${esc(item.id)}">${esc(item.name)}</option>`
  ).join('');
}

function applyDepth() {
  const preset = $('erw-depth').value;
  const fixed = depthPresets[preset];
  const custom = preset === 'custom';
  ['erw-rounds', 'erw-sources', 'erw-timeout'].forEach(id => { $(id).disabled = !custom; });
  if (fixed) {
    $('erw-rounds').value = fixed.rounds;
    $('erw-sources').value = fixed.sources;
    $('erw-timeout').value = fixed.timeout;
  }
  $('erw-custom-row').style.opacity = custom ? '1' : '.58';
}

function fillForm(instance) {
  const config = { ...defaults, ...(instance?.config || {}) };
  selectedId = instance?.id || null;
  $('erw-name').value = instance?.name || '';
  $('erw-enabled').checked = !!config.enabled;
  $('erw-account').value = config.email_account_id || '';
  $('erw-endpoint').value = config.research_endpoint_id || '';
  $('erw-model').value = config.research_model || '';
  $('erw-depth').value = config.research_depth_preset || 'standard';
  $('erw-rounds').value = config.research_rounds;
  $('erw-sources').value = config.research_source_limit;
  $('erw-timeout').value = config.research_timeout_seconds;
  $('erw-poll').value = config.polling_interval_seconds;
  $('erw-concurrency').value = config.maximum_simultaneous_jobs;
  $('erw-prefix').value = config.required_subject_prefix || '';
  $('erw-addresses').value = (config.allowed_sender_addresses || []).join('\n');
  $('erw-domains').value = (config.allowed_sender_domains || []).join('\n');
  $('erw-blocked').value = (config.blocked_sender_patterns || []).join('\n');
  $('erw-early-stop').checked = config.early_stop_no_useful_findings !== false;
  $('erw-max-request').value = config.maximum_request_length;
  $('erw-max-attachment').value = config.maximum_attachment_size;
  $('erw-sender-day').value = config.maximum_requests_per_sender_per_day;
  $('erw-total-day').value = config.maximum_total_jobs_per_day;
  $('erw-html-threshold').value = config.html_attachment_threshold;
  $('erw-retry').value = config.retry_limit;
  $('erw-ack').value = config.acknowledgement_template;
  $('erw-final').value = config.final_email_template;
  $('erw-plain-attachments').checked = config.allow_plain_text_attachments !== false;
  $('erw-delete').disabled = !selectedId;
  ['erw-run', 'erw-test-ack', 'erw-test-life'].forEach(id => { $(id).disabled = !selectedId; });
  applyDepth();
  renderInstances();
}

function collect() {
  return {
    name: $('erw-name').value.trim(),
    config: {
      ...defaults,
      enabled: $('erw-enabled').checked,
      email_account_id: $('erw-account').value,
      research_endpoint_id: $('erw-endpoint').value,
      research_model: $('erw-model').value.trim(),
      research_depth_preset: $('erw-depth').value,
      research_rounds: Number($('erw-rounds').value),
      research_source_limit: Number($('erw-sources').value),
      research_timeout_seconds: Number($('erw-timeout').value),
      polling_interval_seconds: Number($('erw-poll').value),
      maximum_simultaneous_jobs: Number($('erw-concurrency').value),
      required_subject_prefix: $('erw-prefix').value.trim(),
      allowed_sender_addresses: splitList($('erw-addresses').value),
      allowed_sender_domains: splitList($('erw-domains').value),
      blocked_sender_patterns: splitList($('erw-blocked').value),
      early_stop_no_useful_findings: $('erw-early-stop').checked,
      maximum_request_length: Number($('erw-max-request').value),
      maximum_attachment_size: Number($('erw-max-attachment').value),
      maximum_requests_per_sender_per_day: Number($('erw-sender-day').value),
      maximum_total_jobs_per_day: Number($('erw-total-day').value),
      html_attachment_threshold: Number($('erw-html-threshold').value),
      retry_limit: Number($('erw-retry').value),
      acknowledgement_template: $('erw-ack').value,
      final_email_template: $('erw-final').value,
      allow_plain_text_attachments: $('erw-plain-attachments').checked,
    },
  };
}

function renderInstances() {
  const live = new Map((latestStatus?.instances || []).map(item => [item.id, item]));
  const list = $('erw-instance-list');
  if (!instances.length) {
    list.innerHTML = '<div style="font-size:11px;opacity:.55;padding:10px">No automations yet. Create one for each mailbox policy or research-depth profile.</div>';
    return;
  }
  list.innerHTML = instances.map(instance => {
    const status = live.get(instance.id) || {};
    const next = status.status?.next_inbox_poll || '';
    return `<button class="erw-instance ${instance.id === selectedId ? 'active' : ''}" data-instance="${esc(instance.id)}">
      <div class="erw-instance-title"><span class="erw-dot ${instance.config.enabled ? 'on' : ''}"></span><span>${esc(instance.name)}</span></div>
      <div class="erw-instance-meta">${esc(instance.config.research_depth_preset || 'standard')} · ${status.counts?.active || 0} active<br><span data-next-poll="${esc(next)}">${instance.config.enabled ? 'next poll calculating…' : 'disabled'}</span></div>
    </button>`;
  }).join('');
  updateCountdowns();
}

function formatTime(value) {
  return value ? new Date(value).toLocaleString() : '—';
}

function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  const m = Math.floor(value / 60);
  return m ? `${m}m ${value % 60}s` : `${value}s`;
}

function updateCountdowns() {
  document.querySelectorAll('#erw-instance-list [data-next-poll]').forEach(node => {
    if (!node.dataset.nextPoll) return;
    const seconds = Math.max(0, Math.ceil((new Date(node.dataset.nextPoll).getTime() - Date.now()) / 1000));
    node.textContent = `next inbox check in ${formatDuration(seconds)}`;
  });
}

function renderLive(data) {
  latestStatus = data;
  renderInstances();
  const worker = data.worker || {};
  $('erw-worker-state').textContent = `${worker.running ? 'cycle running' : 'idle'} · next ${formatTime(worker.next_inbox_poll)}`;
  $('erw-counts').textContent = Object.entries(data.counts || {}).map(([k, v]) => `${k.replaceAll('_', ' ')} ${v}`).join(' · ');
  const names = new Map(instances.map(item => [item.id, item.name]));
  const jobs = data.jobs || [];
  $('erw-jobs').innerHTML = jobs.length ? jobs.map(job => {
    const p = job.progress || {};
    const progress = job.state === 'researching'
      ? `${esc(p.phase || 'running')} · round ${p.round || 0}/${p.max_rounds || '—'} · sources ${p.sources || 0}/${p.max_sources || '—'} · ${formatDuration(p.elapsed_seconds)}/${formatDuration(p.timeout_seconds)}`
      : (job.error_summary || `${job.attempt_count || 0} attempt(s)`);
    const retry = job.state === 'failed' ? `<button class="admin-btn-sm" data-job="${esc(job.id)}" data-action="retry">Retry</button>` : '';
    const stop = job.state === 'researching' ? `<button class="admin-btn-sm" data-job="${esc(job.id)}" data-action="stop-and-write">Stop &amp; write</button>` : '';
    const cancel = ['received','acknowledged','research_queued','retry_wait','researching'].includes(job.state)
      ? `<button class="admin-btn-sm" data-job="${esc(job.id)}" data-action="cancel">Cancel</button>` : '';
    return `<tr><td>${esc(names.get(job.automation_id) || 'Legacy')}</td><td>${esc(job.state)}</td><td><div>${esc(job.sender)}</div><div style="opacity:.58;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(job.subject)}</div></td><td>${progress}</td><td>${esc(formatTime(job.updated_at))}</td><td style="white-space:nowrap">${retry}${stop}${cancel}</td></tr>`;
  }).join('') : '<tr><td colspan="6" style="padding:14px;opacity:.5">No email research jobs yet</td></tr>';
  const active = Number(data.counts?.active || 0);
  const dot = $('email-research-worker-live-dot');
  if (dot) dot.style.display = active ? '' : 'none';
}

function connectLive() {
  liveSource?.close();
  liveSource = new EventSource(`${API}/events`);
  liveSource.addEventListener('status', event => {
    try { renderLive(JSON.parse(event.data)); } catch (_) {}
  });
  liveSource.onerror = () => {
    const state = $('erw-worker-state');
    if (state) state.textContent = 'live connection reconnecting…';
  };
}

async function loadInstances(preferredId = selectedId) {
  const data = await request(`${API}/instances`);
  instances = data.instances || [];
  accounts = data.accounts || [];
  endpoints = data.endpoints || [];
  setOptions();
  const selected = instances.find(item => item.id === preferredId) || instances[0] || null;
  fillForm(selected);
}

function closeWorker() {
  liveSource?.close();
  liveSource = null;
  clearInterval(countdownTimer);
  countdownTimer = null;
  $(MODAL_ID)?.remove();
}

function wireModal(modal) {
  const content = modal.querySelector('.modal-content');
  makeWindowDraggable(modal, {
    content,
    header: modal.querySelector('.modal-header'),
  });
  Modals.register(MODAL_ID, {
    sidebarBtnId: 'tool-email-research-worker-btn',
    label: 'Research Email Worker',
    icon: 'M2 5h20v14H2zM2 7l10 6 10-6M18 2v5M15.5 4.5h5',
    restoreFn: () => connectLive(),
    closeFn: closeWorker,
  });
  modal.querySelector('.close-btn').addEventListener('click', () => Modals.close(MODAL_ID));
  $('erw-depth').addEventListener('change', applyDepth);
  $('erw-new').addEventListener('click', () => fillForm(null));
  $('erw-instance-list').addEventListener('click', event => {
    const button = event.target.closest('[data-instance]');
    if (!button) return;
    fillForm(instances.find(item => item.id === button.dataset.instance));
  });
  $('erw-save').addEventListener('click', async () => {
    try {
      const body = collect();
      const url = selectedId ? `${API}/instances/${encodeURIComponent(selectedId)}` : `${API}/instances`;
      const saved = await request(url, { method: selectedId ? 'PUT' : 'POST', body: JSON.stringify(body) });
      selectedId = saved.id;
      await loadInstances(saved.id);
      showMessage('Automation saved');
    } catch (error) { showMessage(error.message, false); }
  });
  $('erw-delete').addEventListener('click', async () => {
    if (!selectedId || !confirm('Delete this automation? Automations with job history should be disabled instead.')) return;
    try {
      await request(`${API}/instances/${encodeURIComponent(selectedId)}`, { method: 'DELETE' });
      selectedId = null;
      await loadInstances();
      showMessage('Automation deleted');
    } catch (error) { showMessage(error.message, false); }
  });
  $('erw-run').addEventListener('click', async () => {
    if (!selectedId) return;
    try {
      showMessage('Running inbox cycle…');
      const result = await request(`${API}/instances/${encodeURIComponent(selectedId)}/run-now`, { method: 'POST' });
      showMessage(result.result === 'no-op' ? 'Worker already owns the cycle lock' : 'Inbox cycle complete');
    } catch (error) { showMessage(error.message, false); }
  });
  $('erw-test-ack').addEventListener('click', async () => {
    try {
      await request(`${API}/test-acknowledgement?automation_id=${encodeURIComponent(selectedId)}`, { method: 'POST' });
      showMessage('Test acknowledgement delivered');
    } catch (error) { showMessage(error.message, false); }
  });
  $('erw-test-life').addEventListener('click', async () => {
    try {
      const result = await request(`${API}/test-lifecycle?automation_id=${encodeURIComponent(selectedId)}`, { method: 'POST' });
      showMessage(`Mock lifecycle ${result.result}; no external services called`);
    } catch (error) { showMessage(error.message, false); }
  });
  $('erw-jobs').addEventListener('click', async event => {
    const button = event.target.closest('[data-job][data-action]');
    if (!button) return;
    try {
      await request(`${API}/jobs/${encodeURIComponent(button.dataset.job)}/${button.dataset.action}`, { method: 'POST' });
    } catch (error) { showMessage(error.message, false); }
  });
}

export async function open() {
  let modal = $(MODAL_ID);
  if (!modal) modal = buildModal();
  modal.classList.remove('hidden');
  await loadInstances().catch(error => showMessage(error.message, false));
  connectLive();
  clearInterval(countdownTimer);
  countdownTimer = setInterval(updateCountdowns, 1000);
}

export function isOpen() {
  const modal = $(MODAL_ID);
  return !!modal && !modal.classList.contains('hidden');
}

export default { open, isOpen };
