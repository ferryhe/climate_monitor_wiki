const form = document.querySelector('#pdf-import');
const preview = document.querySelector('#preview');
const message = document.querySelector('#message');
const confirm = document.querySelector('#confirm');
const articles = document.querySelector('#articles');
const calendar = document.querySelector('#calendar');
const file = document.querySelector('#pdf-file');
const batch = document.querySelector('#batch');
const batchStatus = document.querySelector('#batch-status');
const retry = document.querySelector('#retry');
const batchOverview = document.querySelector('#batch-overview');
const batchSummary = document.querySelector('#batch-summary');
const batchList = document.querySelector('#batch-list');
let previewShas = [];
let previewDigest = '';
let batchId = '';

async function intake(path, body, method = 'POST') {
  const response = await fetch(path, {method, body});
  if (response.status === 401) { location.href = '/manage/login?next=/manage/pdf-import'; throw Error('session expired'); }
  const value = await response.json();
  if (!response.ok) throw Error(value.detail || response.statusText);
  return value;
}

function requireOneFile() {
  if (file.files.length !== 1) throw Error('Choose exactly one PDF file.');
}

function showBatch(value) {
  batchId = value.batch_id;
  batch.hidden = false;
  batchStatus.textContent = `Imported: ${value.imported ? 'yes' : 'no'} · Indexed: ${value.indexed ? 'yes' : 'no'} · Chat ready: ${value.chat_ready ? 'yes' : 'no'}${value.error ? ` · ${value.error}` : ''}`;
  retry.hidden = value.stage !== 'failed';
  return value.chat_ready || value.stage === 'failed';
}

function failureDetails(value) {
  const history = value.failure_history || [];
  const details = history.map(failure => `${failure.at} · attempt ${failure.attempt} · ${failure.reason}`);
  const latest = history[history.length - 1];
  if (value.error && (!latest || latest.reason !== value.error)) details.push(value.error);
  return details.join('\n');
}

function showOverview(value) {
  batchOverview.hidden = false;
  const stages = Object.entries(value.stage_counts).map(([stage, count]) => `${stage}: ${count}`).join(' · ');
  const milestones = Object.entries(value.milestone_counts).map(([name, count]) => `${name}: ${count}`).join(' · ');
  batchSummary.textContent = `Total: ${value.total_batches} · Stages: ${stages} · Milestones: ${milestones}`;
  batchList.replaceChildren(...value.batches.map(value => {
    const row = document.createElement('tr');
    for (const text of [value.batch_id, value.filename || '', value.stage, value.created_at, value.updated_at, value.attempts,
      `imported: ${value.imported ? 'yes' : 'no'}; indexed: ${value.indexed ? 'yes' : 'no'}; chat ready: ${value.chat_ready ? 'yes' : 'no'}`, failureDetails(value)]) {
      const cell = document.createElement('td');
      cell.textContent = text;
      row.append(cell);
    }
    const action = document.createElement('td');
    if (value.stage === 'failed') {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = 'Retry processing';
      button.onclick = () => retryBatch(value.batch_id);
      action.append(button);
    }
    row.append(action);
    return row;
  }));
}

async function loadOverview() {
  try { showOverview(await intake('/api/manage/pdf-intake/batches', undefined, 'GET')); }
  catch (error) { message.textContent = error.message; }
}

async function pollBatch() {
  if (!batchId) return;
  try {
    const value = await intake(`/api/manage/pdf-intake/batches/${batchId}`, undefined, 'GET');
    await loadOverview();
    if (!showBatch(value)) setTimeout(pollBatch, 1000);
  } catch (error) { message.textContent = error.message; setTimeout(pollBatch, 1000); }
}

function show(value) {
  preview.textContent = JSON.stringify(value, null, 2);
  articles.replaceChildren(...value.articles.map(entry => {
    const item = document.createElement('li');
    item.textContent = `${entry.title || 'Untitled'} · ${entry.url} · page ${entry.page}\n${entry.report_summary}`;
    return item;
  }));
  calendar.replaceChildren(...value.calendar.map(entry => {
    const item = document.createElement('li');
    item.textContent = `${entry.name || 'Unnamed'} · ${entry.date || 'Date unavailable'} · page ${entry.page}\n${entry.summary}`;
    return item;
  }));
  previewShas = value.documents.map(document => document.sha256);
  previewDigest = value.preview_digest;
  confirm.disabled = !value.writable;
  message.textContent = value.error || (value.writable ? 'Preview only; nothing has been written.' : 'Import is unavailable.');
}

form.onsubmit = async event => {
  event.preventDefault();
  try { requireOneFile(); show(await intake('/api/manage/pdf-intake/preview', new FormData(form))); }
  catch (error) { previewShas = []; previewDigest = ''; confirm.disabled = true; message.textContent = error.message; preview.textContent = ''; articles.replaceChildren(); calendar.replaceChildren(); }
};

confirm.onclick = async () => {
  try {
    requireOneFile();
    const payload = new FormData(form);
    const query = new URLSearchParams({confirmed: 'true', preview_digest: previewDigest});
    previewShas.forEach(sha => query.append('preview_sha', sha));
    const value = await intake('/api/manage/pdf-intake/import?' + query, payload);
    preview.textContent = JSON.stringify(value, null, 2);
    message.textContent = `Batch ${value.batch_id} was queued.`;
    showBatch(value);
    loadOverview();
    setTimeout(pollBatch, 250);
    confirm.disabled = true;
  } catch (error) { message.textContent = error.message; }
};

async function retryBatch(id) {
  try {
    showBatch(await intake(`/api/manage/pdf-intake/batches/${id}/retry`));
    loadOverview();
    setTimeout(pollBatch, 250);
  } catch (error) { message.textContent = error.message; }
}

retry.onclick = () => retryBatch(batchId);
loadOverview();
