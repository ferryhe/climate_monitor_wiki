const STORAGE_KEY = "climate-monitor-agent-thread";

const DEFAULT_PROMPT_STARTERS = [
  {
    label: "Monthly report",
    prompt: "Give me a report for this month. Cover major themes, notable signals, and gaps.",
    answer_mode: "executive",
    description: "Theme-clustered report with date coverage, notable signals, and missing-report gaps.",
  },
  {
    label: "30-day change",
    prompt: "What changed materially over the last 30 days for insurers?",
    answer_mode: "executive",
    description: "Best for trend shifts across a recent window instead of only the latest report.",
  },
  {
    label: "14-day themes",
    prompt: "Summarize the past 14 days by theme, not by day.",
    answer_mode: "executive",
    description: "Synthesizes recurring themes first, then uses daily coverage as supporting context.",
  },
  {
    label: "Pricing explainer",
    prompt: "Why do secondary perils matter for insurance pricing? Cite the strongest evidence.",
    answer_mode: "detailed",
    description: "Evidence-heavy explanation grounded in the source reports and linked wiki notes.",
  },
  {
    label: "Latest snapshot",
    prompt: "What are the latest Climate Monitor highlights in five bullets?",
    answer_mode: "brief",
    description: "Fast snapshot for a quick scan of the most recent material developments.",
  },
];

const GRAPH_COLORS = {
  daily: "#cf9156",
  topic: "#7eb696",
  index: "#87a8c7",
  keyword: "#83a6c8",
};

const GRAPH_COPY = {
  notes: {
    title: "Vault Links",
    hint:
      "Drag note nodes to rearrange the graph. Click a node or a Dataview row to inspect the note and set it as the active chat context.",
    legendHtml: `
      <span><i class="dot dot-daily"></i>Daily</span>
      <span><i class="dot dot-topic"></i>Topic</span>
      <span><i class="dot dot-index"></i>Index</span>
    `,
  },
  keywords: {
    title: "Keyword Map",
    hint:
      "Keyword mode connects notes to source-backed concepts. Click a note to inspect it, or click a keyword to filter the Dataview table.",
    legendHtml: `
      <span><i class="dot dot-daily"></i>Daily</span>
      <span><i class="dot dot-topic"></i>Topic</span>
      <span><i class="dot dot-index"></i>Index</span>
      <span><i class="dot dot-keyword"></i>Keyword</span>
    `,
  },
};

const ANSWER_MODE_COPY = {
  brief: {
    label: "Brief",
    title: "Fast snapshot mode with a short, focused answer.",
    note: "Fastest mode. Returns a short, focused answer with only the most relevant evidence.",
    placeholder: "Ask for a quick snapshot, the latest highlights, or a short answer in a few bullets...",
  },
  detailed: {
    label: "Detailed",
    title: "Evidence-heavy mode for focused explainers and source-backed questions.",
    note: "Richer grounded answers that pull more aggressively from raw source reports.",
    placeholder: "Ask for a source-backed explainer, a focused comparison, or a deeper answer with evidence...",
  },
  executive: {
    label: "Report",
    title: "Structured report mode for period summaries, trend shifts, and big-picture questions.",
    note: "Best for period summaries. Produces a structured report with themes, coverage, and notable signals.",
    placeholder: "Ask for a report across a time window, a theme-based synthesis, or a big-picture trend brief...",
  },
};

const state = {
  messages: [],
  documents: [],
  concepts: [],
  rows: [],
  filteredRows: [],
  edges: [],
  graphMode: "notes",
  answerMode: "detailed",
  activeContextPath: null,
  isSending: false,
  activeView: "chatView",
  markdownByPath: {},
  markdownRequests: {},
  graph: null,
  graphFrame: 0,
  graphData: { notes: null, keywords: null },
  promptStarters: DEFAULT_PROMPT_STARTERS,
};

const els = {
  messages: document.getElementById("messages"),
  form: document.getElementById("chatForm"),
  input: document.getElementById("messageInput"),
  send: document.getElementById("sendButton"),
  clearChat: document.getElementById("clearChatButton"),
  clearContext: document.getElementById("clearContextButton"),
  jumpToWiki: document.getElementById("jumpToWikiButton"),
  useInChat: document.getElementById("useInChatButton"),
  clearSelection: document.getElementById("clearSelectionButton"),
  status: document.getElementById("connectionStatus"),
  activeContextBadge: document.getElementById("activeContextBadge"),
  activeContext: document.getElementById("activeContext"),
  detailTitle: document.getElementById("detailTitle"),
  detailType: document.getElementById("detailType"),
  detailDate: document.getElementById("detailDate"),
  detailWords: document.getElementById("detailWords"),
  detailOutlinks: document.getElementById("detailOutlinks"),
  detailInlinks: document.getElementById("detailInlinks"),
  detailStatus: document.getElementById("detailStatus"),
  detailFile: document.getElementById("detailFile"),
  detailMarkdown: document.getElementById("detailMarkdown"),
  metaNotes: document.getElementById("metaNotes"),
  metaEdges: document.getElementById("metaEdges"),
  wikiStats: document.getElementById("wikiStats"),
  wikiSearch: document.getElementById("wikiSearch"),
  rows: document.getElementById("rows"),
  graphSvg: document.getElementById("graphSvg"),
  graphTitle: document.getElementById("graphTitle"),
  graphLegend: document.getElementById("graphLegend"),
  graphHint: document.getElementById("graphHint"),
  chatView: document.getElementById("chatView"),
  obsidianView: document.getElementById("obsidianView"),
  answerModeButtons: Array.from(document.querySelectorAll("[data-answer-mode]")),
  graphModeButtons: Array.from(document.querySelectorAll("[data-graph-mode]")),
  workspaceTabs: Array.from(document.querySelectorAll(".tabbar__tab")),
  answerModeHint: document.getElementById("answerModeHint"),
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function normalizeMojibake(text) {
  return text
    .replaceAll("â†’", "→")
    .replaceAll("â€”", "—")
    .replaceAll("â€“", "–")
    .replaceAll("â€œ", '"')
    .replaceAll("â€\x9d", '"')
    .replaceAll("â€˜", "'")
    .replaceAll("â€™", "'")
    .replaceAll("�", "");
}

function inlineFmt(raw) {
  const text = escapeHtml(raw);
  return text
    .replace(
      /\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]/g,
      (_, page, alias) =>
        `<a class="obs-wikilink" data-page="${encodeURIComponent(page.trim())}">${escapeHtml((alias || page).trim())}</a>`,
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

function renderMarkdownFull(markdown) {
  const lines = markdown.split("\n");
  const out = [];
  let inCode = false;
  let codeLines = [];
  let inTable = false;

  const flushCode = () => {
    out.push(`<pre><code>${escapeHtml(codeLines.join("\n").trimEnd())}</code></pre>`);
    codeLines = [];
  };

  const parseTableCells = (line) => {
    const trimmed = line.trim();
    if (!trimmed.startsWith("|") || !trimmed.endsWith("|")) {
      return null;
    }
    const cells = trimmed
      .split("|")
      .slice(1, -1)
      .map((cell) => cell.trim());
    return cells.length >= 2 ? cells : null;
  };

  for (const line of lines) {
    if (line.startsWith("```")) {
      if (!inCode) {
        if (inTable) {
          out.push("</tbody></table>");
          inTable = false;
        }
        inCode = true;
        codeLines = [];
      } else {
        inCode = false;
        flushCode();
      }
      continue;
    }

    if (inCode) {
      codeLines.push(line);
      continue;
    }

    const cells = parseTableCells(line);
    if (cells) {
      if (cells.length && cells.every((cell) => /^[-: ]+$/.test(cell))) {
        continue;
      }
      if (!inTable) {
        inTable = true;
        out.push(
          `<table><thead><tr>${cells.map((cell) => `<th>${inlineFmt(cell)}</th>`).join("")}</tr></thead><tbody>`,
        );
      } else {
        out.push(`<tr>${cells.map((cell) => `<td>${inlineFmt(cell)}</td>`).join("")}</tr>`);
      }
      continue;
    }

    if (inTable) {
      out.push("</tbody></table>");
      inTable = false;
    }

    if (!line.trim()) {
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = Math.min(heading[1].length + 1, 5);
      out.push(`<h${level}>${inlineFmt(heading[2])}</h${level}>`);
      continue;
    }

    if (/^[-*_]{3,}$/.test(line.trim())) {
      out.push("<hr>");
      continue;
    }

    const blockquote = line.match(/^>\s*(.*)$/);
    if (blockquote) {
      out.push(`<blockquote>${inlineFmt(blockquote[1])}</blockquote>`);
      continue;
    }

    const ordered = line.match(/^\d+\.\s+(.+)$/);
    if (ordered) {
      out.push(`<ol><li>${inlineFmt(ordered[1])}</li></ol>`);
      continue;
    }

    const unordered = line.match(/^[-*+]\s+(.+)$/);
    if (unordered) {
      out.push(`<ul><li>${inlineFmt(unordered[1])}</li></ul>`);
      continue;
    }

    out.push(`<p>${inlineFmt(line)}</p>`);
  }

  if (inCode) {
    flushCode();
  }

  if (inTable) {
    out.push("</tbody></table>");
  }

  return out.join("\n").replace(/<\/ul>\n<ul>/g, "").replace(/<\/ol>\n<ol>/g, "");
}

function titleFromPath(path) {
  return path.split("/").pop().replace(/\.md$/i, "");
}

function sourceLinkForRow(row) {
  if (!row) {
    return { href: "", label: "-" };
  }

  if (row.type === "daily" && row.source_url && row.source_path) {
    return { href: row.source_url, label: row.source_path };
  }

  return { href: `/${row.path}`, label: row.path };
}

function normalizeSearchText(value) {
  return String(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function deriveStatus(doc, markdown) {
  if (doc.type !== "daily") {
    return "-";
  }
  return /no climate monitor report|no report/i.test(markdown) ? "No report" : "Reported";
}

function buildWorkspaceData(documents) {
  const byTitle = new Map(documents.map((doc) => [doc.title, doc]));
  const inCount = new Map(documents.map((doc) => [doc.path, 0]));
  const outCount = new Map(documents.map((doc) => [doc.path, 0]));
  const edges = [];

  for (const doc of documents) {
    const uniqueLinks = [...new Set(doc.links || [])];
    for (const rawLink of uniqueLinks) {
      const normalized = rawLink.replace(/^wiki\//, "").replace(/\.md$/i, "");
      const target = byTitle.get(normalized);
      if (!target) {
        continue;
      }
      edges.push({ source: doc.path, target: target.path });
      outCount.set(doc.path, (outCount.get(doc.path) || 0) + 1);
      inCount.set(target.path, (inCount.get(target.path) || 0) + 1);
    }
  }

  const rows = documents
    .map((doc) => ({
      ...doc,
      outlinks: outCount.get(doc.path) || 0,
      inlinks: inCount.get(doc.path) || 0,
      status: doc.status || (doc.type === "daily" ? "Loading..." : "-"),
    }))
    .sort((left, right) => left.title.localeCompare(right.title));

  return { rows, edges };
}

function keywordNodeId(label) {
  return `keyword:${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
}

function buildNoteGraph(rows, edges) {
  return {
    mode: "notes",
    title: GRAPH_COPY.notes.title,
    hint: GRAPH_COPY.notes.hint,
    legendHtml: GRAPH_COPY.notes.legendHtml,
    nodes: rows.map((row) => ({
      id: row.path,
      refPath: row.path,
      label: row.title,
      kind: "note",
      type: row.type,
    })),
    links: edges.map((edge) => ({ source: edge.source, target: edge.target })),
  };
}

function buildKeywordGraph(rows) {
  const docKeywords = new Map(
    rows.map((row) => [row.path, (row.concepts || []).map((concept) => concept.label)]),
  );
  const keywordEntries = (state.concepts || [])
    .filter((concept) => concept.document_count >= 2)
    .slice(0, 18);
  const fallbackEntries = (state.concepts || []).slice(0, 12);
  const selectedEntries = keywordEntries.length ? keywordEntries : fallbackEntries;
  const selectedKeywords = new Set(selectedEntries.map((concept) => concept.label));
  const connectedRows = rows.filter((row) => (docKeywords.get(row.path) || []).some((label) => selectedKeywords.has(label)));

  const nodes = connectedRows.map((row) => ({
    id: row.path,
    refPath: row.path,
    label: row.title,
    kind: "note",
    type: row.type,
  }));

  for (const concept of selectedEntries) {
    nodes.push({
      id: keywordNodeId(concept.label),
      label: concept.label,
      kind: "keyword",
      type: "keyword",
      weight: concept.document_count,
    });
  }

  const links = [];
  for (const row of connectedRows) {
    for (const label of docKeywords.get(row.path) || []) {
      if (selectedKeywords.has(label)) {
        links.push({ source: row.path, target: keywordNodeId(label) });
      }
    }
  }

  return {
    mode: "keywords",
    title: GRAPH_COPY.keywords.title,
    hint: selectedEntries.length
      ? GRAPH_COPY.keywords.hint
      : "Keyword mode is still warming up. Once concepts are indexed from wiki and raw source files, they will appear here.",
    legendHtml: GRAPH_COPY.keywords.legendHtml,
    nodes,
    links,
    staticLayout: true,
  };
}

function normalizeGraphData(mode, graph) {
  const copy = GRAPH_COPY[mode] || GRAPH_COPY.notes;
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const links = Array.isArray(graph?.links) ? graph.links : [];
  const hasKeywords = nodes.some((node) => node.kind === "keyword");
  return {
    mode,
    title: copy.title,
    hint:
      mode === "keywords" && !hasKeywords
        ? "Keyword mode is still warming up. Once concepts are indexed from wiki and raw source files, they will appear here."
        : copy.hint,
    legendHtml: copy.legendHtml,
    nodes,
    links,
    staticLayout: Boolean(graph?.static_layout || graph?.staticLayout),
  };
}

function graphDataForCurrentMode() {
  const precomputed = state.graphData?.[state.graphMode];
  if (precomputed) {
    return normalizeGraphData(state.graphMode, precomputed);
  }
  return state.graphMode === "keywords"
    ? buildKeywordGraph(state.rows)
    : buildNoteGraph(state.rows, state.edges);
}

function setGraphMode(mode) {
  if (!mode) {
    return;
  }
  state.graphMode = mode;
  els.graphModeButtons.forEach((button) => {
    const active = button.dataset.graphMode === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  renderCurrentGraph();
}

function getNodeRadius(node) {
  if (node.kind === "keyword") {
    return Math.min(11, 6 + Math.max(0, (node.weight || 1) - 1));
  }
  return node.type === "index" ? 8 : 6;
}

function projectGridPosition(index, count, minX, maxX, minY, maxY) {
  const cols = Math.max(1, Math.ceil(Math.sqrt(count)));
  const rows = Math.max(1, Math.ceil(count / cols));
  const col = index % cols;
  const row = Math.floor(index / cols);
  return {
    x: minX + ((col + 0.5) / cols) * (maxX - minX),
    y: minY + ((row + 0.5) / rows) * (maxY - minY),
  };
}

function setConnectionStatus(agentMode, model) {
  if (!els.status) {
    return;
  }
  els.status.textContent = agentMode === "openai" ? `OpenAI: ${model}` : "Offline demo";
  els.status.classList.toggle("status-pill--offline", agentMode !== "openai");
}

function setAnswerMode(mode) {
  if (!mode) {
    return;
  }
  state.answerMode = mode;
  const copy = ANSWER_MODE_COPY[mode] || ANSWER_MODE_COPY.detailed;
  els.answerModeButtons.forEach((button) => {
    const buttonMode = button.dataset.answerMode || "detailed";
    const buttonCopy = ANSWER_MODE_COPY[buttonMode] || ANSWER_MODE_COPY.detailed;
    const active = buttonMode === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
    button.title = buttonCopy.title;
  });
  if (els.answerModeHint) {
    els.answerModeHint.textContent = copy.note;
  }
  if (els.input && copy.placeholder) {
    els.input.placeholder = copy.placeholder;
  }
}

function loadThread() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return;
    }
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      state.messages = parsed.filter((item) => item && item.role && item.content !== undefined);
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
}

function setWorkspaceView(viewId) {
  if (!viewId) {
    return;
  }
  state.activeView = viewId;
  if (els.chatView) {
    els.chatView.hidden = viewId !== "chatView";
  }
  if (els.obsidianView) {
    els.obsidianView.hidden = viewId !== "obsidianView";
  }
  els.workspaceTabs.forEach((button) => {
    const active = button.dataset.view === viewId;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
}

function messageToApi(item) {
  return { role: item.role, content: item.content };
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
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    const message = state.messages[index];
    if (message.role === "assistant" && message.pending) {
      message.content = content;
      message.sources = sources;
      message.pending = false;
      saveThread();
      renderMessages();
      return;
    }
  }

  state.messages.push({ role: "assistant", content, sources, pending: false });
  saveThread();
  renderMessages();
}

function renderSourceCards(sources) {
  if (!sources.length) {
    return "";
  }
  return `
    <details class="message-sources">
      <summary>Evidence ${sources.length}</summary>
      <div class="source-list">
        ${sources
          .map(
            (source) => `
              <button class="source-card" type="button" data-path="${escapeHtml(source.path || "")}">
                <div class="source-card__title">
                  <span class="source-card__index">[${source.index}]</span>
                  <span class="source-card__heading">${escapeHtml(source.title || source.path || "Source")}</span>
                </div>
                <p class="source-card__meta">${escapeHtml((source.corpus || "wiki").toUpperCase())} · ${escapeHtml(source.heading || source.path || "")}</p>
                <p class="source-card__snippet">${escapeHtml(source.snippet || "")}</p>
              </button>
            `,
          )
          .join("")}
      </div>
    </details>
  `;
}

function renderEmptyState() {
  const starters = Array.isArray(state.promptStarters) && state.promptStarters.length
    ? state.promptStarters
    : DEFAULT_PROMPT_STARTERS;
  const shell = document.createElement("section");
  shell.className = "empty-state";
  shell.innerHTML = `
    <p class="empty-state__lead">
      Start with a task, not just a topic. These prompt starters switch to the best answer mode
      automatically, so period questions open in Report while explainers stay in Detailed.
      Switch to the Obsidian tab whenever you want to inspect the graph, Dataview table, or choose
      the active note for retrieval.
    </p>
    <div class="suggestions">
      ${starters.map(
        (starter) =>
          `<button class="suggestion-chip" type="button" data-prompt="${escapeHtml(starter.prompt || "")}" data-answer-mode="${escapeHtml(starter.answer_mode || "detailed")}" title="${escapeHtml(starter.description || "")}">
            <span class="suggestion-chip__meta">
              <span class="suggestion-chip__mode">${escapeHtml((ANSWER_MODE_COPY[starter.answer_mode] || ANSWER_MODE_COPY.detailed).label)}</span>
              <span class="suggestion-chip__label">${escapeHtml(starter.label || "")}</span>
            </span>
            <span class="suggestion-chip__prompt">${escapeHtml(starter.prompt || "")}</span>
            <span class="suggestion-chip__description">${escapeHtml(starter.description || "")}</span>
          </button>`,
      ).join("")}
    </div>
  `;

  shell.querySelectorAll(".suggestion-chip").forEach((button) => {
    button.addEventListener("click", () => {
      setAnswerMode(button.getAttribute("data-answer-mode") || state.answerMode);
      els.input.value = button.getAttribute("data-prompt");
      els.form.requestSubmit();
    });
  });

  els.messages.appendChild(shell);
}

function renderMessages() {
  if (!els.messages) {
    return;
  }

  els.messages.innerHTML = "";
  if (state.messages.length === 0) {
    renderEmptyState();
    return;
  }

  state.messages.forEach((item) => {
    const row = document.createElement("article");
    row.className = `message-row message-row--${item.role}`;

    const bubble = document.createElement("div");
    bubble.className = `message-bubble message-bubble--${item.role}`;

    if (item.pending) {
      bubble.innerHTML = `
        <div class="message-bubble__typing">
          <span class="typing-dot" aria-hidden="true"></span>
          Searching the wiki and drafting an answer…
        </div>
      `;
    } else if (item.role === "assistant") {
      bubble.innerHTML = `
        <div class="message-markdown">${renderMarkdownFull(item.content)}</div>
        ${renderSourceCards(item.sources || [])}
      `;
    } else {
      bubble.innerHTML = `<p class="message-bubble__plain">${escapeHtml(item.content)}</p>`;
    }

    row.appendChild(bubble);
    els.messages.appendChild(row);
  });

  els.messages.scrollTop = els.messages.scrollHeight;
}

function setSending(value) {
  state.isSending = value;
  if (els.send) {
    els.send.disabled = value;
  }
  if (els.form) {
    els.form.setAttribute("aria-busy", String(value));
  }
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
        answerMode: state.answerMode,
      }),
    });

    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }

    const payload = await response.json();
    replacePendingAssistant(payload.text, payload.sources || []);
    setConnectionStatus(payload.agent_mode, payload.model);
    setAnswerMode(payload.answer_mode || state.answerMode);
  } catch (error) {
    replacePendingAssistant(`Request failed: ${error.message}`);
  } finally {
    setSending(false);
    if (els.input) {
      els.input.focus();
    }
  }
}

function renderChatContext() {
  const doc = state.rows.find((item) => item.path === state.activeContextPath);

  if (!doc) {
    els.activeContextBadge.textContent = "No active note";
    els.activeContextBadge.classList.add("is-empty");
    els.activeContext.textContent = "No active note selected for chat.";
    if (els.clearContext) {
      els.clearContext.disabled = true;
    }
    if (els.useInChat) {
      els.useInChat.disabled = true;
    }
    if (els.clearSelection) {
      els.clearSelection.disabled = true;
    }
    return;
  }

  els.activeContextBadge.textContent = `Active note: ${doc.title}`;
  els.activeContextBadge.classList.remove("is-empty");
  els.activeContext.textContent = `${doc.title} is the active note prioritized during chat retrieval.`;
  if (els.clearContext) {
    els.clearContext.disabled = false;
  }
  if (els.useInChat) {
    els.useInChat.disabled = false;
  }
  if (els.clearSelection) {
    els.clearSelection.disabled = false;
  }
}

function renderWorkspaceMetrics(graphData = null) {
  if (els.metaNotes) {
    els.metaNotes.textContent = `Notes: ${state.rows.length}`;
  }
  if (els.metaEdges) {
    const edgeCount = graphData ? graphData.links.length : state.edges.length;
    els.metaEdges.textContent = state.graphMode === "keywords" ? `Links: ${edgeCount}` : `Edges: ${edgeCount}`;
  }
  if (els.wikiStats) {
    els.wikiStats.textContent = `Pages: ${state.documents.length}`;
  }
}

function renderRows() {
  if (!els.rows) {
    return;
  }

  if (!state.filteredRows.length) {
    els.rows.innerHTML = `
      <tr>
        <td colspan="7" class="muted">No matching pages.</td>
      </tr>
    `;
    return;
  }

  els.rows.innerHTML = state.filteredRows
    .map((row) => {
      const selectedClass = row.path === state.activeContextPath ? "is-selected" : "";
      const statusClass =
        row.status === "Reported"
          ? "status-ok"
          : row.status === "No report"
            ? "status-empty"
            : row.status === "Loading..."
              ? "status-loading"
              : "";
      return `
        <tr class="${selectedClass}" data-path="${escapeHtml(row.path)}">
          <td>${escapeHtml(row.title)}</td>
          <td>${escapeHtml(row.type)}</td>
          <td>${escapeHtml(row.date || "-")}</td>
          <td>${row.words}</td>
          <td>${row.outlinks}</td>
          <td>${row.inlinks}</td>
          <td class="${statusClass}">${escapeHtml(row.status)}</td>
        </tr>
      `;
    })
    .join("");

  els.rows.querySelectorAll("tr[data-path]").forEach((rowEl) => {
    rowEl.addEventListener("click", () => {
      setActiveContext(rowEl.dataset.path);
    });
  });
}

function applyTableFilter(query = "") {
  const needle = normalizeSearchText(query);
  state.filteredRows = state.rows.filter((row) => {
    const concepts = (row.concepts || []).map((concept) => concept.label).join(" ");
    const haystack = normalizeSearchText(
      `${row.title} ${row.type} ${row.date} ${row.status} ${row.path} ${concepts}`,
    );
    return !needle || haystack.includes(needle);
  });
  if (!needle && !state.filteredRows.length && state.rows.length) {
    state.filteredRows = [...state.rows];
  }
  renderRows();
}

function renderDetail(path) {
  const row = state.rows.find((item) => item.path === path);

  if (!row) {
    els.detailTitle.textContent = "Select a note";
    els.detailType.textContent = "None";
    els.detailDate.textContent = "-";
    els.detailWords.textContent = "-";
    els.detailOutlinks.textContent = "-";
    els.detailInlinks.textContent = "-";
    els.detailStatus.textContent = "-";
    els.detailFile.textContent = "-";
    els.detailMarkdown.textContent = "Select a note to preview its markdown source.";
    renderChatContext();
    return;
  }

  els.detailTitle.textContent = row.title;
  els.detailType.textContent = row.type;
  els.detailDate.textContent = row.date || "-";
  els.detailWords.textContent = String(row.words);
  els.detailOutlinks.textContent = String(row.outlinks);
  els.detailInlinks.textContent = String(row.inlinks);
  els.detailStatus.textContent = row.status;
  const sourceLink = sourceLinkForRow(row);
  els.detailFile.innerHTML = `<a href="${escapeHtml(sourceLink.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(sourceLink.label)}</a>`;

  const markdown = state.markdownByPath[path];
  els.detailMarkdown.textContent = markdown || "Loading markdown preview…";
  renderChatContext();
}

function updateGraphSelection() {
  if (!state.graph) {
    return;
  }
  state.graph.nodes.forEach((node, index) => {
    const active = node.kind === "note" && (node.refPath || node.id) === state.activeContextPath;
    const group = state.graph.nodeEls[index];
    const circle = group.querySelector("circle");
    group.classList.toggle("is-active", active);
    if (circle) {
      const baseRadius = getNodeRadius(node);
      circle.setAttribute("r", String(active ? baseRadius + 2 : baseRadius));
      circle.setAttribute("stroke-width", active ? "2" : "1");
    }
  });
}

function renderCurrentGraph() {
  const graphData = graphDataForCurrentMode();
  if (els.graphTitle) {
    els.graphTitle.textContent = graphData.title;
  }
  if (els.graphLegend) {
    els.graphLegend.innerHTML = graphData.legendHtml;
  }
  if (els.graphHint) {
    els.graphHint.textContent = graphData.hint;
  }
  renderWorkspaceMetrics(graphData);
  renderGraph(graphData);
}

function renderGraph(graphData) {
  if (!els.graphSvg) {
    return;
  }

  if (state.graphFrame) {
    cancelAnimationFrame(state.graphFrame);
    state.graphFrame = 0;
  }

  const svg = els.graphSvg;
  const NS = "http://www.w3.org/2000/svg";
  const width = 1200;
  const height = 700;
  svg.innerHTML = "";

  if (!graphData.nodes.length) {
    const empty = document.createElementNS(NS, "text");
    empty.textContent = "Graph data is loading…";
    empty.setAttribute("x", String(width / 2));
    empty.setAttribute("y", String(height / 2));
    empty.setAttribute("fill", "#97a3aa");
    empty.setAttribute("font-size", "16");
    empty.setAttribute("text-anchor", "middle");
    svg.appendChild(empty);
    state.graph = { nodes: [], nodeEls: [] };
    return;
  }

  const noteCount = graphData.nodes.filter((node) => node.kind !== "keyword").length;
  const keywordCount = graphData.nodes.filter((node) => node.kind === "keyword").length;
  let noteIndex = 0;
  let keywordIndex = 0;

  const nodes = graphData.nodes.map((node) => {
    const position =
      Number.isFinite(node.x) && Number.isFinite(node.y)
        ? { x: node.x, y: node.y }
        : graphData.mode === "keywords" && node.kind === "keyword"
          ? projectGridPosition(keywordIndex++, keywordCount, width * 0.68, width - 80, 90, height - 90)
          : graphData.mode === "keywords"
            ? projectGridPosition(noteIndex++, noteCount, 70, width * 0.58, 70, height - 70)
            : projectGridPosition(noteIndex++, noteCount, 70, width - 70, 70, height - 70);

    return {
      ...node,
      ...position,
      vx: 0,
      vy: 0,
      pinned: false,
    };
  });

  const byId = new Map(nodes.map((node) => [node.id, node]));
  const links = graphData.links
    .map((edge) => ({ source: byId.get(edge.source), target: byId.get(edge.target) }))
    .filter((edge) => edge.source && edge.target);
  const staticLayout = Boolean(graphData.staticLayout);

  const linkGroup = document.createElementNS(NS, "g");
  const nodeGroup = document.createElementNS(NS, "g");
  svg.appendChild(linkGroup);
  svg.appendChild(nodeGroup);

  const lineEls = links.map(() => {
    const line = document.createElementNS(NS, "line");
    line.setAttribute(
      "stroke",
      graphData.mode === "keywords" ? "rgba(135, 168, 199, 0.28)" : "rgba(151, 163, 170, 0.42)",
    );
    line.setAttribute("stroke-width", "1.1");
    linkGroup.appendChild(line);
    return line;
  });

  const nodeEls = nodes.map((node) => {
    const group = document.createElementNS(NS, "g");
    group.setAttribute("class", `graph-node graph-node--${node.kind || "note"}`);

    const circle = document.createElementNS(NS, "circle");
    circle.setAttribute("r", String(getNodeRadius(node)));
    circle.setAttribute("fill", GRAPH_COLORS[node.type] || GRAPH_COLORS.topic);
    circle.setAttribute("stroke", "#0d1411");
    circle.setAttribute("stroke-width", "1");

    const label = document.createElementNS(NS, "text");
    label.textContent = node.label;
    label.setAttribute("x", "10");
    label.setAttribute("y", "4");

    group.appendChild(circle);
    group.appendChild(label);
    nodeGroup.appendChild(group);

    group.addEventListener("click", () => {
      if (node.kind === "keyword") {
        if (els.wikiSearch) {
          els.wikiSearch.value = node.label;
          applyTableFilter(node.label);
        }
        return;
      }
      setActiveContext(node.refPath || node.id);
    });

    let dragging = false;
    group.addEventListener("pointerdown", (event) => {
      dragging = true;
      node.pinned = true;
      group.setPointerCapture(event.pointerId);
    });

    group.addEventListener("pointermove", (event) => {
      if (!dragging) {
        return;
      }
      const rect = svg.getBoundingClientRect();
      const sx = width / rect.width;
      const sy = height / rect.height;
      node.x = (event.clientX - rect.left) * sx;
      node.y = (event.clientY - rect.top) * sy;
      if (staticLayout) {
        draw();
      }
    });

    group.addEventListener("pointerup", () => {
      dragging = false;
    });

    return group;
  });

  function draw() {
    links.forEach((edge, index) => {
      lineEls[index].setAttribute("x1", edge.source.x);
      lineEls[index].setAttribute("y1", edge.source.y);
      lineEls[index].setAttribute("x2", edge.target.x);
      lineEls[index].setAttribute("y2", edge.target.y);
    });

    nodes.forEach((node, index) => {
      nodeEls[index].setAttribute("transform", `translate(${node.x},${node.y})`);
    });
  }

  function tick() {
    for (const node of nodes) {
      node.vx *= 0.86;
      node.vy *= 0.86;
    }

    for (let left = 0; left < nodes.length; left += 1) {
      for (let right = left + 1; right < nodes.length; right += 1) {
        const a = nodes[left];
        const b = nodes[right];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        const dist2 = dx * dx + dy * dy + 0.01;
        const repulse = 1600 / dist2;
        dx *= repulse;
        dy *= repulse;
        if (!a.pinned) {
          a.vx += dx;
          a.vy += dy;
        }
        if (!b.pinned) {
          b.vx -= dx;
          b.vy -= dy;
        }
      }
    }

    for (const edge of links) {
      const dx = edge.target.x - edge.source.x;
      const dy = edge.target.y - edge.source.y;
      const pull = graphData.mode === "keywords" ? 0.0013 : 0.0009;
      if (!edge.source.pinned) {
        edge.source.vx += dx * pull;
        edge.source.vy += dy * pull;
      }
      if (!edge.target.pinned) {
        edge.target.vx -= dx * pull;
        edge.target.vy -= dy * pull;
      }
    }

    for (const node of nodes) {
      if (node.pinned) {
        continue;
      }
      node.x += node.vx;
      node.y += node.vy;
      node.x = Math.max(18, Math.min(width - 18, node.x));
      node.y = Math.max(18, Math.min(height - 18, node.y));
    }

    draw();
    state.graphFrame = requestAnimationFrame(tick);
  }

  state.graph = { nodes, nodeEls };
  updateGraphSelection();
  draw();
  if (staticLayout) {
    return;
  }
  state.graphFrame = requestAnimationFrame(tick);
}

async function fetchMarkdown(path) {
  const response = await fetch(`/${path}`);
  if (!response.ok) {
    throw new Error(`Failed to load ${path}: ${response.status}`);
  }
  return normalizeMojibake(await response.text());
}

function updateDocumentStatus(path, markdown) {
  let changed = false;
  state.documents = state.documents.map((doc) => {
    if (doc.path !== path) {
      return doc;
    }
    const nextStatus = deriveStatus(doc, markdown);
    if (doc.status === nextStatus) {
      return doc;
    }
    changed = true;
    return { ...doc, status: nextStatus };
  });

  if (!changed) {
    if (state.graphMode === "keywords") {
      renderCurrentGraph();
    }
    return;
  }

  const data = buildWorkspaceData(state.documents);
  state.rows = data.rows;
  state.edges = data.edges;
  applyTableFilter(els.wikiSearch ? els.wikiSearch.value : "");
  renderDetail(state.activeContextPath);
  if (state.graphMode === "keywords") {
    renderCurrentGraph();
  } else {
    renderWorkspaceMetrics();
  }
}

async function ensureMarkdownLoaded(path) {
  if (!path) {
    return null;
  }

  if (state.markdownByPath[path]) {
    return state.markdownByPath[path];
  }

  if (state.markdownRequests[path]) {
    return state.markdownRequests[path];
  }

  state.markdownRequests[path] = fetchMarkdown(path)
    .then((markdown) => {
      state.markdownByPath[path] = markdown;
      updateDocumentStatus(path, markdown);
      if (state.activeContextPath === path) {
        renderDetail(path);
      }
      return markdown;
    })
    .catch((error) => {
      if (state.activeContextPath === path) {
        els.detailMarkdown.textContent = `Load failed: ${error.message}`;
      }
      return null;
    })
    .finally(() => {
      delete state.markdownRequests[path];
    });

  return state.markdownRequests[path];
}

async function hydrateDocumentsInBackground() {
  const tasks = state.documents.map((doc) =>
    ensureMarkdownLoaded(doc.path).catch(() => null),
  );
  await Promise.all(tasks);
}

function clearContext() {
  state.activeContextPath = null;
  renderChatContext();
  renderRows();
  renderDetail(null);
  updateGraphSelection();
}

function scrollSelectedRowIntoView() {
  if (state.activeView !== "obsidianView") {
    return;
  }
  const rowEl = document.querySelector(`tr[data-path="${CSS.escape(state.activeContextPath || "")}"]`);
  if (rowEl) {
    rowEl.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function setActiveContext(path, options = {}) {
  if (!path) {
    clearContext();
    return;
  }

  state.activeContextPath = path;
  renderChatContext();
  renderRows();
  renderDetail(path);
  updateGraphSelection();

  if (options.switchView) {
    setWorkspaceView(options.switchView);
  }

  scrollSelectedRowIntoView();
  void ensureMarkdownLoaded(path);
}

async function loadConfig() {
  const response = await fetch("/api/config");
  if (!response.ok) {
    throw new Error(`API ${response.status}`);
  }

  const config = await response.json();
  state.concepts = config.concepts || [];
  state.graphData = config.graphs || { notes: null, keywords: null };
  state.promptStarters = Array.isArray(config.prompt_starters) && config.prompt_starters.length
    ? config.prompt_starters
    : DEFAULT_PROMPT_STARTERS;
  state.documents = (config.documents || []).map((doc) => ({
    ...doc,
    status: doc.type === "daily" ? "Loading..." : "-",
  }));

  const data = buildWorkspaceData(state.documents);
  state.rows = data.rows;
  state.edges = data.edges;

  if (els.wikiSearch) {
    els.wikiSearch.value = "";
  }
  applyTableFilter("");
  renderDetail(null);
  renderChatContext();
  setConnectionStatus(config.agent_mode, config.model);
  setAnswerMode(config.default_answer_mode || "detailed");
  setGraphMode(state.graphMode);
  if (state.messages.length === 0) {
    renderMessages();
  }
}

function attachEvents() {
  if (els.form) {
    els.form.addEventListener("submit", (event) => {
      event.preventDefault();
      const message = els.input.value.trim();
      if (!message || state.isSending) {
        return;
      }
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
      applyTableFilter(els.wikiSearch.value);
    });
  }

  if (els.jumpToWiki) {
    els.jumpToWiki.addEventListener("click", () => {
      setWorkspaceView("obsidianView");
      scrollSelectedRowIntoView();
    });
  }

  if (els.useInChat) {
    els.useInChat.addEventListener("click", () => {
      if (!state.activeContextPath) {
        return;
      }
      setWorkspaceView("chatView");
      els.input.focus();
    });
  }

  if (els.clearChat) {
    els.clearChat.addEventListener("click", () => {
      clearThread();
    });
  }

  if (els.clearContext) {
    els.clearContext.addEventListener("click", () => {
      clearContext();
    });
  }

  if (els.clearSelection) {
    els.clearSelection.addEventListener("click", () => {
      clearContext();
    });
  }

  els.answerModeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      setAnswerMode(button.dataset.answerMode || "detailed");
    });
  });

  els.graphModeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      setGraphMode(button.dataset.graphMode || "notes");
    });
  });

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) {
      return;
    }

    const tab = target.closest(".tabbar__tab");
    if (tab) {
      setWorkspaceView(tab.dataset.view);
      return;
    }

    const sourceCard = target.closest(".source-card");
    if (sourceCard && sourceCard.dataset.path) {
      if (sourceCard.dataset.path.startsWith("wiki/")) {
        setActiveContext(sourceCard.dataset.path, { switchView: "obsidianView" });
      } else {
        window.open(`/${sourceCard.dataset.path}`, "_blank", "noopener");
      }
      return;
    }

    const wikiLink = target.closest(".obs-wikilink");
    if (wikiLink) {
      const pageName = decodeURIComponent(wikiLink.dataset.page || "");
      const doc = state.rows.find(
        (item) => item.title === pageName || item.path === pageName || item.path === `wiki/${pageName}.md`,
      );
      if (doc) {
        setActiveContext(doc.path, { switchView: "obsidianView" });
      }
    }
  });
}

async function main() {
  loadThread();
  setAnswerMode(state.answerMode);
  setGraphMode(state.graphMode);
  setWorkspaceView(state.activeView);
  renderMessages();
  attachEvents();

  try {
    await loadConfig();
    void hydrateDocumentsInBackground();
  } catch (error) {
    els.status.textContent = "API offline";
    els.status.classList.add("status-pill--offline");
    if (els.graphHint) {
      els.graphHint.textContent = `Load failed: ${error.message}`;
    }
    appendMessage(
      "assistant",
      `Could not reach the backend service: ${error.message}\n\nStart it with \`uvicorn api_server:app --host 0.0.0.0 --port 8501\`.`,
    );
  }
}

main();
