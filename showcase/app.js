const STORAGE_KEY = "climate-monitor-agent-thread";

const SUGGESTIONS = [
  "What are the latest Climate Monitor highlights?",
  "Why do secondary perils matter for insurance pricing?",
  "How does IFRS S2 affect climate disclosure for insurers?",
  "How does parametric insurance relate to the nat-cat protection gap?",
];

const state = {
  messages: [],
  documents: [],
  filteredDocuments: [],
  activeContextPath: null,
  isSending: false,
};

const els = {
  messages: document.getElementById("messages"),
  form: document.getElementById("chatForm"),
  input: document.getElementById("messageInput"),
  send: document.getElementById("sendButton"),
  status: document.getElementById("connectionStatus"),
  sourceList: document.getElementById("sourceList"),
  sourceCount: document.getElementById("sourceCount"),
  wikiStats: document.getElementById("wikiStats"),
  wikiSearch: document.getElementById("wikiSearch"),
  wikiList: document.getElementById("wikiList"),
  activeContext: document.getElementById("activeContext"),
  markdownPreview: document.getElementById("markdownPreview"),
  clearContext: document.getElementById("clearContextButton"),
};

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderMarkdownLite(value) {
  const escaped = escapeHtml(value);
  return escaped
    .replace(/^### (.*)$/gm, "<h4>$1</h4>")
    .replace(/^## (.*)$/gm, "<h3>$1</h3>")
    .replace(/^# (.*)$/gm, "<h3>$1</h3>")
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[(\d+)\]/g, '<span class="citation">[$1]</span>')
    .replace(/^- (.*)$/gm, "<li>$1</li>")
    .replace(/(<li>.*<\/li>)/gs, "<ul>$1</ul>")
    .replace(/\n\n/g, "</p><p>")
    .replace(/\n/g, "<br>");
}

function loadThread() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      state.messages = parsed.filter((item) => item.role && item.content);
    }
  } catch {
    localStorage.removeItem(STORAGE_KEY);
  }
}

function saveThread() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.messages.slice(-16)));
}

function messageToApi(item) {
  return {
    role: item.role,
    content: item.content,
  };
}

function appendMessage(role, content, options = {}) {
  state.messages.push({
    role,
    content,
    sources: options.sources || [],
    pending: Boolean(options.pending),
  });
  saveThread();
  renderMessages();
}

function replacePendingAssistant(content, sources = []) {
  const pending = state.messages.findLast(
    (item) => item.role === "assistant" && item.pending,
  );
  if (pending) {
    pending.content = content;
    pending.sources = sources;
    pending.pending = false;
  } else {
    state.messages.push({ role: "assistant", content, sources });
  }
  saveThread();
  renderMessages();
}

function renderEmptyState() {
  const shell = document.createElement("section");
  shell.className = "empty-state";
  shell.innerHTML = `
    <p class="empty-state__lead">Ask the agent to retrieve, plan, cite, and answer from the current Obsidian wiki.</p>
    <div class="suggestions">
      ${SUGGESTIONS.map(
        (prompt) =>
          `<button class="suggestion-chip" type="button" data-prompt="${escapeHtml(prompt)}">${escapeHtml(prompt)}</button>`,
      ).join("")}
    </div>
  `;
  shell.querySelectorAll(".suggestion-chip").forEach((button) => {
    button.addEventListener("click", () => {
      els.input.value = button.getAttribute("data-prompt");
      els.form.requestSubmit();
    });
  });
  els.messages.appendChild(shell);
}

function renderMessages() {
  els.messages.innerHTML = "";
  if (state.messages.length === 0) {
    renderEmptyState();
  }

  state.messages.forEach((item) => {
    const row = document.createElement("article");
    row.className = `message-row message-row--${item.role}`;
    const bubble = document.createElement("div");
    bubble.className = `message-bubble message-bubble--${item.role}`;
    if (item.pending) {
      bubble.innerHTML = `<span class="typing-dot"></span>Searching the wiki and drafting an answer...`;
    } else if (item.role === "assistant") {
      bubble.innerHTML = `<p>${renderMarkdownLite(item.content)}</p>`;
    } else {
      bubble.textContent = item.content;
    }
    row.appendChild(bubble);
    els.messages.appendChild(row);
  });
  els.messages.scrollTop = els.messages.scrollHeight;
}

function setSending(value) {
  state.isSending = value;
  els.send.disabled = value;
  els.input.disabled = value;
  els.send.textContent = value ? "Drafting" : "Send";
}

async function sendMessage(message) {
  setSending(true);
  appendMessage("user", message);
  appendMessage("assistant", "", { pending: true });

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: state.messages
          .filter((item) => !item.pending)
          .map(messageToApi),
        contextPath: state.activeContextPath,
        language: "en",
      }),
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }
    const payload = await response.json();
    replacePendingAssistant(payload.text, payload.sources || []);
    renderSources(payload.sources || []);
    els.status.textContent =
      payload.agent_mode === "openai" ? `OpenAI: ${payload.model}` : "Offline demo";
    els.status.classList.toggle("status-pill--offline", payload.agent_mode !== "openai");
  } catch (error) {
    replacePendingAssistant(`Request failed: ${error.message}`);
  } finally {
    setSending(false);
    els.input.focus();
  }
}

function renderSources(sources) {
  els.sourceCount.textContent = String(sources.length);
  if (!sources.length) {
    els.sourceList.innerHTML = `<p class="muted">No sources returned yet.</p>`;
    return;
  }

  els.sourceList.innerHTML = sources
    .map(
      (source) => `
      <button class="source-item" type="button" data-path="${escapeHtml(source.path)}">
        <span class="source-item__index">[${source.index}]</span>
        <span class="source-item__title">${escapeHtml(source.title)}</span>
        <span class="source-item__meta">${escapeHtml(source.heading || source.path)}</span>
        <span class="source-item__snippet">${escapeHtml(source.snippet || "")}</span>
      </button>
    `,
    )
    .join("");

  els.sourceList.querySelectorAll(".source-item").forEach((button) => {
    button.addEventListener("click", () => setActiveContext(button.dataset.path));
  });
}

function renderWikiList() {
  if (!state.filteredDocuments.length) {
    els.wikiList.innerHTML = `<p class="muted">No matching pages.</p>`;
    return;
  }

  els.wikiList.innerHTML = state.filteredDocuments
    .map(
      (doc) => `
      <button class="wiki-row ${doc.path === state.activeContextPath ? "is-active" : ""}" type="button" data-path="${escapeHtml(doc.path)}">
        <span>
          <strong>${escapeHtml(doc.title)}</strong>
          <small>${escapeHtml(doc.type)} | ${escapeHtml(doc.date || "-")} | ${doc.words} words</small>
        </span>
      </button>
    `,
    )
    .join("");

  els.wikiList.querySelectorAll(".wiki-row").forEach((button) => {
    button.addEventListener("click", () => setActiveContext(button.dataset.path));
  });
}

async function setActiveContext(path) {
  if (!path) return;
  state.activeContextPath = path;
  const doc = state.documents.find((item) => item.path === path);
  els.activeContext.textContent = doc ? `${doc.title} (${doc.path})` : path;
  renderWikiList();
  try {
    const response = await fetch(`/${path}`);
    const markdown = await response.text();
    els.markdownPreview.textContent = markdown;
  } catch (error) {
    els.markdownPreview.textContent = `Load failed: ${error.message}`;
  }
}

function clearContext() {
  state.activeContextPath = null;
  els.activeContext.textContent = "No page selected";
  els.markdownPreview.textContent = "Select a wiki page to prioritize it during retrieval.";
  renderWikiList();
}

async function loadConfig() {
  const response = await fetch("/api/config");
  if (!response.ok) {
    throw new Error(`API ${response.status}`);
  }
  const config = await response.json();
  state.documents = config.documents || [];
  state.filteredDocuments = state.documents;
  els.wikiStats.textContent = `${config.wiki.documents} pages / ${config.wiki.chunks} chunks`;
  els.status.textContent =
    config.agent_mode === "openai" ? `OpenAI: ${config.model}` : "Offline demo";
  els.status.classList.toggle("status-pill--offline", config.agent_mode !== "openai");
  renderWikiList();
}

function attachEvents() {
  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    const message = els.input.value.trim();
    if (!message || state.isSending) return;
    els.input.value = "";
    sendMessage(message);
  });

  els.input.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      els.form.requestSubmit();
    }
  });

  els.wikiSearch.addEventListener("input", () => {
    const query = els.wikiSearch.value.trim().toLowerCase();
    state.filteredDocuments = state.documents.filter((doc) =>
      `${doc.title} ${doc.type} ${doc.date} ${doc.path}`.toLowerCase().includes(query),
    );
    renderWikiList();
  });

  els.clearContext.addEventListener("click", clearContext);
}

async function main() {
  loadThread();
  renderMessages();
  renderSources([]);
  attachEvents();
  try {
    await loadConfig();
  } catch (error) {
    els.status.textContent = "API offline";
    els.status.classList.add("status-pill--offline");
    els.wikiStats.textContent = "Failed";
    appendMessage(
      "assistant",
      `Could not reach the backend service: ${error.message}\n\nStart it with \`uvicorn api_server:app --host 0.0.0.0 --port 8501\`.`,
    );
  }
}

main();
