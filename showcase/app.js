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
  activeView: "chatView",
};

const els = {
  messages: document.getElementById("messages"),
  form: document.getElementById("chatForm"),
  input: document.getElementById("messageInput"),
  send: document.getElementById("sendButton"),
  clearChat: document.getElementById("clearChatButton"),
  status: document.getElementById("connectionStatus"),
  sourceList: document.getElementById("sourceList"),
  sourceCount: document.getElementById("sourceCount"),
  wikiStats: document.getElementById("wikiStats"),
  wikiSearch: document.getElementById("wikiSearch"),
  wikiList: document.getElementById("wikiList"),
  activeContext: document.getElementById("activeContext"),
  markdownPreview: document.getElementById("markdownPreview"),
  chatView: document.getElementById("chatView"),
  obsidianView: document.getElementById("obsidianView"),
  obsContent: document.getElementById("obsContent"),
  obsEmptyState: document.getElementById("obsEmptyState"),
  obsArticle: document.getElementById("obsArticle"),
  obsArticleType: document.getElementById("obsArticleType"),
  obsArticleDate: document.getElementById("obsArticleDate"),
  obsArticleWords: document.getElementById("obsArticleWords"),
  workspaceTabs: Array.from(document.querySelectorAll(".tabbar__tab")),
};

// ── Helpers ──────────────────────────────────────────────

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

// Inline markdown → HTML (safe: only escapes &, <, > before formatting)
function inlineFmt(raw) {
  const t = raw.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
  return t
    .replace(
      /\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]/g,
      (_, page, alias) =>
        `<a class="obs-wikilink" data-page="${encodeURIComponent(page.trim())}">${(alias || page).trim()}</a>`,
    )
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
    )
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/_(.*?)_/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[(\d+)\]/g, '<span class="citation">[$1]</span>');
}

// Full markdown → rendered HTML for Obsidian preview
function renderMarkdownFull(markdown) {
  const lines = markdown.split("\n");
  const out = [];
  let inCode = false;
  let codeLines = [];
  let inTable = false;

  const flushCode = () => {
    out.push(`<pre class="obs-code"><code>${escapeHtml(codeLines.join("\n").trimEnd())}</code></pre>`);
    codeLines = [];
  };

  for (const line of lines) {
    // Code fence
    if (line.startsWith("```")) {
      if (!inCode) {
        if (inTable) { out.push("</tbody></table>"); inTable = false; }
        inCode = true;
        codeLines = [];
      } else {
        inCode = false;
        flushCode();
      }
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }

    // Table rows (contains |)
    if (line.includes("|")) {
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      if (cells.length && cells.every((c) => /^[-: ]+$/.test(c))) continue; // separator
      if (!inTable) {
        inTable = true;
        out.push(`<table><thead><tr>${cells.map((c) => `<th>${inlineFmt(c)}</th>`).join("")}</tr></thead><tbody>`);
      } else {
        out.push(`<tr>${cells.map((c) => `<td>${inlineFmt(c)}</td>`).join("")}</tr>`);
      }
      continue;
    }
    if (inTable) { out.push("</tbody></table>"); inTable = false; }

    if (!line.trim()) continue;

    // Heading
    const hm = line.match(/^(#{1,4})\s+(.+)$/);
    if (hm) {
      const lvl = Math.min(hm[1].length + 1, 5);
      out.push(`<h${lvl}>${inlineFmt(hm[2])}</h${lvl}>`);
      continue;
    }

    // Horizontal rule
    if (/^[-*_]{3,}$/.test(line.trim())) { out.push("<hr>"); continue; }

    // Blockquote
    const bq = line.match(/^>\s*(.*)$/);
    if (bq) { out.push(`<blockquote>${inlineFmt(bq[1])}</blockquote>`); continue; }

    // Ordered list
    const oli = line.match(/^\d+\.\s+(.+)$/);
    if (oli) { out.push(`<ol><li>${inlineFmt(oli[1])}</li></ol>`); continue; }

    // Unordered list
    const uli = line.match(/^[-*+]\s+(.+)$/);
    if (uli) { out.push(`<ul><li>${inlineFmt(uli[1])}</li></ul>`); continue; }

    out.push(`<p>${inlineFmt(line)}</p>`);
  }

  if (inCode) flushCode();
  if (inTable) out.push("</tbody></table>");

  return out
    .join("\n")
    .replace(/<\/ul>\n<ul>/g, "")
    .replace(/<\/ol>\n<ol>/g, "");
}

// Lite markdown renderer for chat bubbles
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

// ── Thread persistence ───────────────────────────────────

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

function clearThread() {
  state.messages = [];
  localStorage.removeItem(STORAGE_KEY);
  renderMessages();
  renderSources([]);
}

// ── Tab switching ────────────────────────────────────────

function setWorkspaceView(viewId) {
  if (!viewId) return;
  state.activeView = viewId;
  if (els.chatView) els.chatView.hidden = viewId !== "chatView";
  if (els.obsidianView) els.obsidianView.hidden = viewId !== "obsidianView";
  els.workspaceTabs.forEach((btn) => {
    const active = btn.dataset.view === viewId;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", String(active));
  });
}

// ── Chat messages ────────────────────────────────────────

function messageToApi(item) {
  return { role: item.role, content: item.content };
}

function appendMessage(role, content, options = {}) {
  state.messages.push({ role, content, sources: options.sources || [], pending: Boolean(options.pending) });
  saveThread();
  renderMessages();
}

function replacePendingAssistant(content, sources = []) {
  const pending = state.messages.findLast((item) => item.role === "assistant" && item.pending);
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
    <p class="empty-state__lead">Ask the agent to retrieve, plan, cite, and answer from the wiki.</p>
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
  if (state.messages.length === 0) { renderEmptyState(); return; }
  state.messages.forEach((item) => {
    const row = document.createElement("article");
    row.className = `message-row message-row--${item.role}`;
    const bubble = document.createElement("div");
    bubble.className = `message-bubble message-bubble--${item.role}`;
    if (item.pending) {
      bubble.innerHTML = `<span class="typing-dot"></span>Searching the wiki and drafting an answer…`;
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
        messages: state.messages.filter((item) => !item.pending).map(messageToApi),
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

// ── Evidence panel ───────────────────────────────────────

function openEvidencePanel() {
  const panel = document.getElementById("evidencePanel");
  const toggle = document.querySelector(".ev-toggle");
  if (!panel || !toggle || !panel.hidden) return;
  panel.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
  toggle.classList.add("is-open");
}

function renderSources(sources) {
  els.sourceCount.textContent = String(sources.length);
  if (!sources.length) {
    els.sourceList.innerHTML = `<p class="muted">No sources returned yet.</p>`;
    return;
  }
  openEvidencePanel();
  els.sourceList.innerHTML = sources
    .map(
      (source) => `
      <button class="source-item" type="button" data-path="${escapeHtml(source.path)}">
        <span class="source-item__index">[${source.index}]</span>
        <span class="source-item__title">${escapeHtml(source.title)}</span>
        <span class="source-item__meta">${escapeHtml(source.heading || source.path)}</span>
        <span class="source-item__snippet">${escapeHtml(source.snippet || "")}</span>
      </button>`,
    )
    .join("");
  els.sourceList.querySelectorAll(".source-item").forEach((button) => {
    button.addEventListener("click", () => setActiveContext(button.dataset.path));
  });
}

// ── Wiki file list ───────────────────────────────────────

const DOC_ICON = `<svg class="wiki-row__icon" width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="2" y="1" width="12" height="14" rx="2" stroke="currentColor" stroke-width="1.4"/><path d="M5 5h6M5 8h6M5 11h4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`;

function renderWikiList() {
  if (!state.filteredDocuments.length) {
    els.wikiList.innerHTML = `<p style="color:var(--obs-muted);font-size:12px;padding:8px;">No matching pages.</p>`;
    return;
  }
  els.wikiList.innerHTML = state.filteredDocuments
    .map(
      (doc) => `
      <button class="wiki-row ${doc.path === state.activeContextPath ? "is-active" : ""}"
          type="button" data-path="${escapeHtml(doc.path)}">
        ${DOC_ICON}
        <span class="wiki-row__body">
          <strong>${escapeHtml(doc.title)}</strong>
          <small>${escapeHtml(doc.type)}${doc.date && doc.date !== "-" ? " · " + escapeHtml(doc.date) : ""}</small>
        </span>
      </button>`,
    )
    .join("");
  els.wikiList.querySelectorAll(".wiki-row").forEach((button) => {
    button.addEventListener("click", () => setActiveContext(button.dataset.path));
  });
}

// ── Active context (Obsidian view) ───────────────────────

async function setActiveContext(path) {
  if (!path) return;
  state.activeContextPath = path;
  const doc = state.documents.find((item) => item.path === path);

  if (els.obsArticle) els.obsArticle.hidden = false;
  if (els.obsEmptyState) els.obsEmptyState.hidden = true;

  if (els.activeContext) els.activeContext.textContent = doc ? doc.title : path;
  if (els.obsArticleType && doc) els.obsArticleType.textContent = doc.type;
  if (els.obsArticleDate && doc) els.obsArticleDate.textContent = doc.date && doc.date !== "-" ? doc.date : "";
  if (els.obsArticleWords && doc) els.obsArticleWords.textContent = doc.words ? `${doc.words} words` : "";

  renderWikiList();

  try {
    const response = await fetch(`/${path}`);
    const markdown = await response.text();
    if (els.markdownPreview) {
      els.markdownPreview.innerHTML = renderMarkdownFull(markdown);
      if (els.obsContent) els.obsContent.scrollTo({ top: 0, behavior: "instant" });
    }
  } catch (error) {
    if (els.markdownPreview) {
      els.markdownPreview.innerHTML = `<p class="muted">Load failed: ${escapeHtml(error.message)}</p>`;
    }
  }
}

function clearContext() {
  state.activeContextPath = null;
  if (els.obsArticle) els.obsArticle.hidden = true;
  if (els.obsEmptyState) els.obsEmptyState.hidden = false;
  if (els.activeContext) els.activeContext.textContent = "—";
  renderWikiList();
}

// ── Config loading ───────────────────────────────────────

async function loadConfig() {
  const response = await fetch("/api/config");
  if (!response.ok) throw new Error(`API ${response.status}`);
  const config = await response.json();
  state.documents = config.documents || [];
  state.filteredDocuments = state.documents;
  els.wikiStats.textContent = `${config.wiki.documents} pages`;
  els.status.textContent =
    config.agent_mode === "openai" ? `OpenAI: ${config.model}` : "Offline demo";
  els.status.classList.toggle("status-pill--offline", config.agent_mode !== "openai");
  renderWikiList();
}

// ── Events ───────────────────────────────────────────────

function attachEvents() {
  if (els.form) {
    els.form.addEventListener("submit", (event) => {
      event.preventDefault();
      const message = els.input.value.trim();
      if (!message || state.isSending) return;
      els.input.value = "";
      sendMessage(message);
    });
  }

  if (els.input) {
    els.input.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        els.form.requestSubmit();
      }
    });
  }

  if (els.wikiSearch) {
    els.wikiSearch.addEventListener("input", () => {
      const query = els.wikiSearch.value.trim().toLowerCase();
      state.filteredDocuments = state.documents.filter((doc) =>
        `${doc.title} ${doc.type} ${doc.date} ${doc.path}`.toLowerCase().includes(query),
      );
      renderWikiList();
    });
  }

  // Global delegation — handles tabs, evidence toggle, clear, wiki links
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;

    // Tab buttons
    const tab = target.closest(".tabbar__tab");
    if (tab) { setWorkspaceView(tab.dataset.view); return; }

    // Evidence accordion
    const evToggle = target.closest(".ev-toggle");
    if (evToggle) {
      const panel = document.getElementById(evToggle.dataset.target);
      if (panel) {
        const opening = panel.hidden;
        panel.hidden = !opening;
        evToggle.setAttribute("aria-expanded", String(opening));
        evToggle.classList.toggle("is-open", opening);
      }
      return;
    }

    // Deactivate context
    if (target.closest("#clearContextButton")) { clearContext(); return; }

    // Clear chat
    if (target.closest("#clearChatButton")) { clearThread(); return; }

    // Obsidian internal [[wiki link]]
    const wikiLink = target.closest(".obs-wikilink");
    if (wikiLink) {
      const pageName = decodeURIComponent(wikiLink.dataset.page || "");
      const doc = state.documents.find(
        (d) => d.title === pageName || d.path === pageName || d.path === `wiki/${pageName}.md`,
      );
      if (doc) setActiveContext(doc.path);
      return;
    }
  });
}

// ── Bootstrap ────────────────────────────────────────────

async function main() {
  loadThread();
  setWorkspaceView(state.activeView);
  renderMessages();
  renderSources([]);
  attachEvents();
  try {
    await loadConfig();
  } catch (error) {
    els.status.textContent = "API offline";
    els.status.classList.add("status-pill--offline");
    if (els.wikiStats) els.wikiStats.textContent = "Failed";
    appendMessage(
      "assistant",
      `Could not reach the backend service: ${error.message}\n\nStart it with \`uvicorn api_server:app --host 0.0.0.0 --port 8501\`.`,
    );
  }
}

main();
