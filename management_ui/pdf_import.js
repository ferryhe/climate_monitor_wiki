const form = document.querySelector('#pdf-import');
const preview = document.querySelector('#preview');
const message = document.querySelector('#message');
const confirm = document.querySelector('#confirm');
const articles = document.querySelector('#articles');
const calendar = document.querySelector('#calendar');
let previewShas = [];
let previewDigest = '';

async function intake(path, body) {
  const response = await fetch(path, {method: 'POST', body});
  if (response.status === 401) { location.href = '/manage/login?next=/manage/pdf-import'; throw Error('session expired'); }
  const value = await response.json();
  if (!response.ok) throw Error(value.detail || response.statusText);
  return value;
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
  try { show(await intake('/api/manage/pdf-intake/preview', new FormData(form))); }
  catch (error) { previewShas = []; previewDigest = ''; confirm.disabled = true; message.textContent = error.message; preview.textContent = ''; articles.replaceChildren(); calendar.replaceChildren(); }
};

confirm.onclick = async () => {
  const payload = new FormData(form);
  try {
    const query = new URLSearchParams({confirmed: 'true', preview_digest: previewDigest});
    previewShas.forEach(sha => query.append('preview_sha', sha));
    const value = await intake('/api/manage/pdf-intake/import?' + query, payload);
    preview.textContent = JSON.stringify(value, null, 2);
    message.textContent = `Imported ${value.new_documents} new PDF document(s); ${value.existing_documents} already existed; added ${value.added.article_occurrences} article occurrence(s) and ${value.added.calendar_items} calendar item(s).`;
    confirm.disabled = true;
  } catch (error) { message.textContent = error.message; }
};
