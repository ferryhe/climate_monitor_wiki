const STORAGE_KEY = "climate-monitor-agent-thread";

const DEFAULT_PROMPT_STARTERS = [
  {
    "label": "Generate PDF",
    "prompt": "Generate a monitoring PDF report for the last 14 days.",
    "answer_mode": "executive",
    "description": "Creates an on-demand PDF with its actual date basis and citations; it does not approve a formal report or send email."
  },
  {
    "label": "Key dates & opportunities",
    "prompt": "Summarize upcoming dates that matter to the climate committee, including consultation and submission deadlines, conferences, and expert reviews. Highlight newly added opportunities to review, respond, or attend, with dates, organizers, participation details, and sources.",
    "answer_mode": "detailed",
    "description": "Shows sourced dates and participation facts together, with missing fields and coverage gaps."
  },
  {
    "label": "New reports & articles",
    "prompt": "Summarize climate- and insurance-related reports and articles added or materially updated in the last 14 days. Explain the key findings and relevance to the climate committee, and cite sources.",
    "answer_mode": "detailed",
    "description": "Separates publication dates from approved added and material-update times."
  },
  {
    "label": "Insurance implications",
    "prompt": "Which recent developments matter most for insurance pricing, reserving, and capital? Explain why and cite sources.",
    "answer_mode": "detailed",
    "description": "Connects cited evidence to pricing, reserving and capital."
  },
  {
    "label": "Regulation & disclosure",
    "prompt": "What are the latest developments in climate regulation, supervision, and disclosure relevant to insurers?",
    "answer_mode": "detailed",
    "description": "Reviews regulation, supervision and disclosure with source evidence."
  },
  {
    "label": "Physical risks",
    "prompt": "What does the available evidence say about changing climate hazards and their implications for insurance losses?",
    "answer_mode": "detailed",
    "description": "Explains climate hazards and insurance loss evidence."
  },
  {
    "label": "Transition risks",
    "prompt": "What recent developments in the energy transition could affect insurers and actuaries?",
    "answer_mode": "detailed",
    "description": "Reviews energy transition evidence for insurers and actuaries."
  }
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
    title: "Concept Map",
    hint:
      "Edges connect a note to each detected concept it contains. Concept size shows the number of linked notes. Click a note to set it as the active chat context, or click a concept to filter the Page Index.",
    legendHtml: `
      <span><i class="dot dot-daily"></i>Daily</span>
      <span><i class="dot dot-topic"></i>Topic</span>
      <span><i class="dot dot-index"></i>Index</span>
      <span><i class="dot dot-keyword"></i>Concept</span>
    `,
  },
};

const HIDDEN_GRAPH_CONCEPTS = new Set([
  "date observations",
  "article semantic summary",
  "report observation",
  "verified article content",
  "registry article-version summary",
  "acquisition observation",
  "pdf report observation",
  "registry source observations",
  "original links",
]);

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
  graphMode: "keywords",
  answerMode: "detailed",
  activeContextPath: null,
  isSending: false,
  activeView: "registryView",
  markdownByPath: {},
  markdownRequests: {},
  graph: null,
  graphFrame: 0,
  graphData: { notes: null, keywords: null },
  promptStarters: DEFAULT_PROMPT_STARTERS,
  registry: {
    loaded: false,
    available: false,
    mode: "reports",
    reportPage: 1,
    articlePage: 1,
    meetingPage: 1,
    meetingPagination: null,
    meetingRequestSequence: 0,
    reportPagination: null,
    articlePagination: null,
    selectedReportDate: null,
    reportRequestSequence: 0,
    articleRequestSequence: 0,
    articleDetailRequestSequence: 0,
    loadPromise: null,
  },
};

const els = {
  messages: document.getElementById("messages"),
  form: document.getElementById("chatForm"),
  input: document.getElementById("messageInput"),
  send: document.getElementById("sendButton"),
  clearChat: document.getElementById("clearChatButton"),
  clearContext: document.getElementById("clearContextButton"),
  jumpToReports: document.getElementById("jumpToReportsButton"),
  useInChat: document.getElementById("useInChatButton"),
  clearSelection: document.getElementById("clearSelectionButton"),
  status: document.getElementById("connectionStatus"),
  hermesLink: document.getElementById("hermesLink"),
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
  registryView: document.getElementById("registryView"),
  meetingsView: document.getElementById("meetingsView"),
  registryStatus: document.getElementById("registryStatus"),
  registryReportsPanel: document.getElementById("registryReportsPanel"),
  registryArticlesPanel: document.getElementById("registryArticlesPanel"),
  registryMeetings: document.getElementById("registryMeetings"),
  registryMeetingSearchForm: document.getElementById("registryMeetingSearchForm"),
  registryMeetingSearch: document.getElementById("registryMeetingSearch"),
  registryMeetingVerification: document.getElementById("registryMeetingVerification"),
  registryMeetingCounts: document.getElementById("registryMeetingCounts"),
  meetingsPrevious: document.getElementById("meetingsPrevious"),
  meetingsNext: document.getElementById("meetingsNext"),
  meetingsPage: document.getElementById("meetingsPage"),
  registryReports: document.getElementById("registryReports"),
  registryReportDetail: document.getElementById("registryReportDetail"),
  registryReportTitle: document.getElementById("registryReportTitle"),
  registryReportMeta: document.getElementById("registryReportMeta"),
  registryReportPdf: document.getElementById("registryReportPdf"),
  registryExecutiveSummary: document.getElementById("registryExecutiveSummary"),
  registryBriefingExecutiveSummaryItems: document.getElementById("registryBriefingExecutiveSummaryItems"),
  registryExecutiveSummaryItems: document.getElementById("registryExecutiveSummaryItems"),
  registryMonitoringSnapshot: document.getElementById("registryMonitoringSnapshot"),
  registrySnapshotMetrics: document.getElementById("registrySnapshotMetrics"),
  registrySnapshotNotes: document.getElementById("registrySnapshotNotes"),
  registryReportArticlesTitle: document.getElementById("registryReportArticlesTitle"),
  registryReportArticles: document.getElementById("registryReportArticles"),
  registryImportedReport: document.getElementById("registryImportedReport"),
  reportsPrevious: document.getElementById("reportsPrevious"),
  reportsNext: document.getElementById("reportsNext"),
  reportsPage: document.getElementById("reportsPage"),
  registryArticles: document.getElementById("registryArticles"),
  registryArticleDetail: document.getElementById("registryArticleDetail"),
  registryAppearancesSection: document.getElementById("registryAppearancesSection"),
  registrySearchForm: document.getElementById("registrySearchForm"),
  registrySearch: document.getElementById("registrySearch"),
  registryPublisherFilter: document.getElementById("registryPublisherFilter"),
  registryPublisherCustom: document.getElementById("registryPublisherCustom"),
  articlesPrevious: document.getElementById("articlesPrevious"),
  articlesNext: document.getElementById("articlesNext"),
  articlesPage: document.getElementById("articlesPage"),
  registryArticleTitle: document.getElementById("registryArticleTitle"),
  registryOriginalLink: document.getElementById("registryOriginalLink"),
  registryArticleMeta: document.getElementById("registryArticleMeta"),
  registryEnrichment: document.getElementById("registryEnrichment"),
  registryAppearances: document.getElementById("registryAppearances"),
  registryContentSection: document.getElementById("registryContentSection"),
  registryContentTitle: document.getElementById("registryContentTitle"),
  registryMarkdown: document.getElementById("registryMarkdown"),
  registryModeButtons: Array.from(document.querySelectorAll("[data-registry-mode]")),
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
  const links = [];
  const keepLink = (html) => {
    links.push(html);
    return `\u0000${links.length - 1}\u0000`;
  };
  return text
    .replace(
      /\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]/g,
      (_, page, alias) =>
        keepLink(`<a class="obs-wikilink" data-page="${encodeURIComponent(page.trim())}">${escapeHtml((alias || page).trim())}</a>`),
    )
    .replace(
      /\[([^\]]+)\]\(((?:https?:\/\/|\/)[^)]+)\)/g,
      (_, label, url) => keepLink(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`),
    )
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/(?<!\w)_([^_\n]+)_(?!\w)/g, "<em>$1</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[(\d+)\]/g, '<span class="citation">[$1]</span>')
    .replace(/\u0000(\d+)\u0000/g, (_, index) => links[index]);
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

function sourceLinkForRow(row) {
  if (!row) {
    return { href: "", label: "-" };
  }

  if (row.type === "daily" && row.date) {
    return {
      href: `#historical-report=${encodeURIComponent(row.date)}`,
      label: `Historical report · ${row.date}`,
      reportDate: row.date,
    };
  }

  return { href: `/${row.path}`, label: row.path };
}

function historicalReportDateFromHash() {
  const match = /^#historical-report=(\d{4}-\d{2}-\d{2})$/.exec(window.location.hash);
  return match ? match[1] : "";
}

function openHistoricalReport(reportDate, { updateHash = true } = {}) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(reportDate || "")) {
    return;
  }
  const nextHash = `#historical-report=${encodeURIComponent(reportDate)}`;
  if (updateHash && window.location.hash !== nextHash) {
    window.history.pushState(null, "", nextHash);
  }
  state.registry.selectedReportDate = reportDate;
  setRegistryMode("reports");
  setWorkspaceView("registryView");
  void loadRegistryReport(reportDate);
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

function displayDateSortKey(value) {
  const quarter = /^(\d{4})-Q([1-4])$/.exec(value);
  if (quarter) {
    const month = String((Number(quarter[2]) - 1) * 3 + 1).padStart(2, "0");
    return `${quarter[1]}-${month}-01`;
  }
  if (/^\d{4}$/.test(value)) return `${value}-01-01`;
  if (/^\d{4}-\d{2}$/.test(value)) return `${value}-01`;
  return value;
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
      displayTitle: doc.display_title || doc.title,
      displayDate: doc.display_date || "",
      displayDateBasis: doc.display_date_basis || "",
      outlinks: outCount.get(doc.path) || 0,
      inlinks: inCount.get(doc.path) || 0,
      status: doc.status || (doc.type === "daily" ? "Loading..." : "-"),
    }))
    .sort((left, right) => {
      if (!left.displayDate) return right.displayDate ? 1 : 0;
      if (!right.displayDate) return -1;
      return displayDateSortKey(right.displayDate).localeCompare(displayDateSortKey(left.displayDate));
    });

  return { rows, edges };
}

function keywordNodeId(label) {
  return `keyword:${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
}

function isDisplayableGraphConcept(label) {
  return !HIDDEN_GRAPH_CONCEPTS.has(label.toLowerCase()) && !/^Article Article [a-f0-9]{24}$/i.test(label);
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
      label: row.displayTitle,
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
  const concepts = (state.concepts || []).filter((concept) => isDisplayableGraphConcept(concept.label));
  const keywordEntries = concepts
    .filter((concept) => concept.document_count >= 2)
    .slice(0, 18);
  const fallbackEntries = concepts.slice(0, 12);
  const selectedEntries = keywordEntries.length ? keywordEntries : fallbackEntries;
  const selectedKeywords = new Set(selectedEntries.map((concept) => concept.label));
  const connectedRows = rows.filter((row) => (docKeywords.get(row.path) || []).some((label) => selectedKeywords.has(label)));

  const nodes = connectedRows.map((row) => ({
    id: row.path,
    refPath: row.path,
    label: row.displayTitle,
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
    staticLayout: false,
  };
}

function normalizeGraphData(mode, graph) {
  const copy = GRAPH_COPY[mode] || GRAPH_COPY.notes;
  const rowByPath = new Map(state.rows.map((row) => [row.path, row]));
  const sourceNodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const hiddenConceptIds = new Set(
    mode === "keywords"
      ? sourceNodes
          .filter((node) => node.kind === "keyword" && !isDisplayableGraphConcept(node.label))
          .map((node) => node.id)
      : [],
  );
  const links = (Array.isArray(graph?.links) ? graph.links : []).filter(
    (edge) => !hiddenConceptIds.has(edge.source) && !hiddenConceptIds.has(edge.target),
  );
  const visibleConceptIds = new Set(
    sourceNodes
      .filter((node) => node.kind === "keyword" && !hiddenConceptIds.has(node.id))
      .map((node) => node.id),
  );
  const linkedNoteIds = new Set(
    mode === "keywords"
      ? links.flatMap((edge) =>
          visibleConceptIds.has(edge.source)
            ? [edge.target]
            : visibleConceptIds.has(edge.target)
              ? [edge.source]
              : [],
        )
      : [],
  );
  const nodes = sourceNodes
    .filter(
      (node) =>
        !hiddenConceptIds.has(node.id) &&
        (mode !== "keywords" || node.kind === "keyword" || linkedNoteIds.has(node.id)),
    )
    .map((node) => {
      const row = node.kind === "keyword" ? null : rowByPath.get(node.refPath || node.id);
      return row ? { ...node, label: row.displayTitle } : node;
    });
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
  const importance = Math.max(1, Number(node.importance || node.degree || node.weight || 1));
  if (node.kind === "keyword") {
    return Math.min(22, 7 + Math.sqrt(importance) * 2.4);
  }
  return Math.min(12, (node.type === "index" ? 7 : 5) + Math.sqrt(importance));
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

function projectRadialPosition(rank, count, centerX, centerY, maxRadius) {
  if (rank === 0 || count <= 1) {
    return { x: centerX, y: centerY };
  }
  const angle = rank * 2.399963229728653;
  const radius = 48 + Math.sqrt(rank / Math.max(1, count - 1)) * (maxRadius - 48);
  return {
    x: centerX + Math.cos(angle) * radius,
    y: centerY + Math.sin(angle) * radius * 0.78,
  };
}

function setConnectionStatus(agentMode, model) {
  if (!els.status) {
    return;
  }
  els.status.textContent = ["openai", "anthropic"].includes(agentMode) ? `AI synthesis · ${model}` : "Source-only mode";
  els.status.classList.toggle("status-pill--offline", !["openai", "anthropic"].includes(agentMode));
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

function stopGraphAnimation() {
  if (state.graphFrame) {
    cancelAnimationFrame(state.graphFrame);
    state.graphFrame = 0;
  }
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
  if (els.registryView) {
    els.registryView.hidden = viewId !== "registryView";
  }
  if (els.meetingsView) {
    els.meetingsView.hidden = viewId !== "meetingsView";
  }
  els.workspaceTabs.forEach((button) => {
    const active = button.dataset.view === viewId;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  });
  if (viewId === "registryView" && !state.registry.loaded) {
    void loadRegistry();
  }
  if (viewId === "meetingsView") {
    state.registry.meetingPage = 1;
    void loadRegistryMeetings();
  }
  if (viewId === "obsidianView") {
    renderCurrentGraph();
  } else {
    stopGraphAnimation();
  }
}

function messageToApi(item) {
  // The API bounds history messages to 8000 characters; keep the full report in the UI.
  return { role: item.role, content: item.role === "assistant" ? item.content.slice(0, 8000) : item.content, context: item.context || null };
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

function replacePendingAssistant(content, sources = [], rangeReport = null, context = null) {
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    const message = state.messages[index];
    if (message.role === "assistant" && message.pending) {
      message.content = content;
      message.sources = sources;
      message.rangeReport = rangeReport;
      message.context = context;
      message.pending = false;
      saveThread();
      renderMessages();
      return;
    }
  }

  state.messages.push({ role: "assistant", content, sources, rangeReport, context, pending: false });
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
              <button class="source-card" type="button" data-path="${escapeHtml(source.path || "")}" data-url="${escapeHtml(safeSourceUrl(source.url))}">
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
    if (item.rangeReport) {
      bubble.classList.add("message-bubble--report");
    }

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
      if (item.rangeReport) {
        bubble.querySelectorAll("table").forEach((table) => {
          const scroll = document.createElement("div");
          scroll.className = "report-table-scroll";
          table.before(scroll);
          scroll.appendChild(table);
        });
      }
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
    replacePendingAssistant(payload.text, payload.sources || [], payload.range_report || null, payload.context || null);
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
    els.activeContextBadge.hidden = true;
    els.activeContext.hidden = true;
    if (els.clearContext) {
      els.clearContext.disabled = true;
      els.clearContext.hidden = true;
    }
    if (els.useInChat) {
      els.useInChat.disabled = true;
    }
    if (els.clearSelection) {
      els.clearSelection.disabled = true;
    }
    return;
  }

  const contextKind = doc.type === "daily" ? "report" : "note";
  els.activeContextBadge.textContent = `Focused ${contextKind}: ${doc.displayTitle}`;
  els.activeContextBadge.hidden = false;
  els.activeContext.textContent = `${doc.displayTitle} is the focused ${contextKind} prioritized during chat retrieval.`;
  els.activeContext.hidden = false;
  if (els.clearContext) {
    els.clearContext.disabled = false;
    els.clearContext.hidden = false;
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
          <td>${escapeHtml(row.displayTitle)}</td>
          <td>${escapeHtml(row.type)}</td>
          <td>${escapeHtml(displayDateLabel(row))}</td>
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
      `${row.displayTitle} ${row.type} ${displayDateLabel(row)} ${row.status} ${row.path} ${concepts}`,
    );
    return !needle || haystack.includes(needle);
  });
  if (!needle && !state.filteredRows.length && state.rows.length) {
    state.filteredRows = [...state.rows];
  }
  renderRows();
}

function displayDateLabel(row) {
  if (!row.displayDate) return "Unknown";
  const basis = {
    publication_date: "publication date",
    report_date: "report date",
    topic_update_date: "topic update date",
    page_update_date: "last updated date",
  }[row.displayDateBasis] || "date basis unknown";
  return `${row.displayDate} (${basis})`;
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

  els.detailTitle.textContent = row.displayTitle;
  els.detailType.textContent = row.type;
  els.detailDate.textContent = displayDateLabel(row);
  els.detailWords.textContent = String(row.words);
  els.detailOutlinks.textContent = String(row.outlinks);
  els.detailInlinks.textContent = String(row.inlinks);
  els.detailStatus.textContent = row.status;
  const sourceLink = sourceLinkForRow(row);
  const reportDateAttribute = sourceLink.reportDate
    ? ` data-historical-report-date="${escapeHtml(sourceLink.reportDate)}"`
    : ` target="_blank" rel="noopener noreferrer"`;
  els.detailFile.innerHTML = `<a href="${escapeHtml(sourceLink.href)}"${reportDateAttribute}>${escapeHtml(sourceLink.label)}</a>`;

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

  stopGraphAnimation();

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

  const degreeById = new Map(graphData.nodes.map((node) => [node.id, 0]));
  graphData.links.forEach((edge) => {
    degreeById.set(edge.source, (degreeById.get(edge.source) || 0) + 1);
    degreeById.set(edge.target, (degreeById.get(edge.target) || 0) + 1);
  });
  const importanceFor = (node) =>
    Math.max(Number(node.weight || 0), degreeById.get(node.id) || 0, 1);
  const centralRank = new Map(
    [...graphData.nodes]
      .sort((left, right) => importanceFor(right) - importanceFor(left) || left.label.localeCompare(right.label))
      .map((node, index) => [node.id, index]),
  );
  const noteCount = graphData.nodes.filter((node) => node.kind !== "keyword").length;
  let noteIndex = 0;

  const nodes = graphData.nodes.map((node) => {
    const importance = importanceFor(node);
    const radialPosition = projectRadialPosition(
      centralRank.get(node.id) || 0,
      graphData.nodes.length,
      width / 2,
      height / 2,
      Math.min(width, height) * 0.42,
    );
    const position =
      graphData.mode === "keywords"
        ? radialPosition
        : Number.isFinite(node.x) && Number.isFinite(node.y)
        ? { x: node.x, y: node.y }
        : projectGridPosition(noteIndex++, noteCount, 70, width - 70, 70, height - 70);

    return {
      ...node,
      ...position,
      degree: degreeById.get(node.id) || 0,
      importance,
      targetX: radialPosition.x,
      targetY: radialPosition.y,
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
  let ticksRemaining = 0;

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
    group.setAttribute("class", `graph-node graph-node--${node.kind || "note"} graph-node--${graphData.mode}`);
    group.setAttribute("role", "button");
    group.setAttribute("tabindex", "0");
    group.setAttribute(
      "aria-label",
      node.kind === "keyword" ? `Filter Page Index by keyword ${node.label}` : `Open note ${node.label}`,
    );

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

    const activateNode = () => {
      if (node.kind === "keyword") {
        if (els.wikiSearch) {
          els.wikiSearch.value = node.label;
          applyTableFilter(node.label);
        }
        return;
      }
      setActiveContext(node.refPath || node.id);
    };

    group.addEventListener("click", activateNode);
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activateNode();
      }
    });

    let dragging = false;
    group.addEventListener("pointerdown", (event) => {
      dragging = true;
      node.pinned = true;
      startAnimation(90);
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
    state.graphFrame = 0;
    if (document.hidden || state.activeView !== "obsidianView" || ticksRemaining <= 0) {
      return;
    }
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
      const pull = graphData.mode === "keywords" ? 0.0024 : 0.0009;
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
      if (graphData.mode === "keywords") {
        node.vx += (node.targetX - node.x) * 0.006;
        node.vy += (node.targetY - node.y) * 0.006;
      } else {
        node.vx += (width / 2 - node.x) * 0.00035;
        node.vy += (height / 2 - node.y) * 0.00035;
      }
      node.x += node.vx;
      node.y += node.vy;
      const margin = getNodeRadius(node) + 28;
      node.x = Math.max(margin, Math.min(width - margin, node.x));
      node.y = Math.max(margin, Math.min(height - margin, node.y));
    }

    draw();
    ticksRemaining -= 1;
    if (ticksRemaining > 0) {
      state.graphFrame = requestAnimationFrame(tick);
    }
  }

  function startAnimation(frameLimit = 180) {
    if (staticLayout || document.hidden || state.activeView !== "obsidianView") {
      return;
    }
    ticksRemaining = Math.max(ticksRemaining, frameLimit);
    if (!state.graphFrame) {
      state.graphFrame = requestAnimationFrame(tick);
    }
  }

  state.graph = { nodes, nodeEls };
  updateGraphSelection();
  draw();
  if (staticLayout) {
    return;
  }
  startAnimation();
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

function registryElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== "") {
    element.textContent = String(text);
  }
  return element;
}

function registryErrorMessage(error) {
  if (error && error.status === 503) {
    return "The archive is not connected yet. Chat and the wiki remain available.";
  }
  return "The archive could not be loaded. Please try again later.";
}

async function registryFetch(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const error = new Error(`Registry request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function renderRegistryNotice(container, message) {
  if (!container) {
    return;
  }
  container.replaceChildren(registryElement("p", "registry-notice", message));
}

function updateRegistryPagination(kind, pagination) {
  const previous = kind === "reports" ? els.reportsPrevious : kind === "meetings" ? els.meetingsPrevious : els.articlesPrevious;
  const next = kind === "reports" ? els.reportsNext : kind === "meetings" ? els.meetingsNext : els.articlesNext;
  const label = kind === "reports" ? els.reportsPage : kind === "meetings" ? els.meetingsPage : els.articlesPage;
  if (!pagination) {
    previous.disabled = true;
    next.disabled = true;
    label.textContent = "Page —";
    return;
  }
  previous.disabled = pagination.page <= 1;
  next.disabled = pagination.page >= pagination.pages;
  label.textContent = pagination.pages
    ? `Page ${pagination.page} of ${pagination.pages}`
    : "No results";
}

function registryMetric(label, value) {
  const wrapper = registryElement("div");
  wrapper.append(registryElement("dt", "", label), registryElement("dd", "", value ?? "—"));
  return wrapper;
}

const REGISTRY_SUMMARY_PRESENTATION = {
  content_enrichment: {
    sourceLabel: "Captured content",
    provenanceCopy: "Summary generated from captured article content",
  },
  original_content_annotation: {
    sourceLabel: "Original source",
    provenanceCopy: "Summary based on the linked original content",
  },
  official_replacement_annotation: {
    sourceLabel: "Official replacement",
    provenanceCopy: "Summary based on an official replacement page",
  },
  publisher_excerpt_annotation: {
    sourceLabel: "Publisher excerpt",
    provenanceCopy: "Summary based on the publisher's available excerpt",
  },
  report_fallback_annotation: {
    sourceLabel: "Report fallback",
    provenanceCopy: "Summary based on historical report text because the original source was unavailable",
  },
  source_report: {
    sourceLabel: "Historical report",
    provenanceCopy: "Summary from the historical report",
  },
};

function registrySummaryPresentation(article) {
  let text = "";
  let provenance = null;
  if (article.summary) {
    text = article.summary;
    provenance =
      article.summary_provenance ||
      (article.source_annotation?.source_basis
        ? `${article.source_annotation.source_basis}_annotation`
        : null);
  } else if (article.enrichment?.summary) {
    text = article.enrichment.summary;
    provenance = "content_enrichment";
  } else if (article.report_summary) {
    text = article.report_summary;
    provenance = "source_report";
  }
  const presentation = REGISTRY_SUMMARY_PRESENTATION[provenance] || {};
  return {
    text,
    provenance,
    sourceLabel: presentation.sourceLabel || "",
    provenanceCopy: presentation.provenanceCopy || "",
  };
}

function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

function setRegistryMode(mode) {
  state.registry.mode = mode === "articles" ? "articles" : "reports";
  els.registryReportsPanel.hidden = state.registry.mode !== "reports";
  els.registryArticlesPanel.hidden = state.registry.mode !== "articles";
  els.registryModeButtons.forEach((button) => {
    const active = button.dataset.registryMode === state.registry.mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  if (state.registry.available && state.registry.mode === "articles" && !state.registry.articlePagination) {
    void loadRegistryArticles();
  }
}

function appendInformationCheck(container, item) {
  const labels = { unchecked: "Pending check", accessible: "URL accessible", unavailable: "URL unavailable",
    failed: "URL check failed", partial: "Partially checked", conflict: "Conflicting information", verified: "Verified / collected" };
  container.append(registryElement("p", "registry-card__meta", [
    item.origin === "web_collection" ? (item.pdf_observations?.length ? "Web collection · PDF import" : "Web collection") : "PDF import",
    labels[item.access_status] || "URL unchecked", labels[item.verification_status] || "Pending check",
    item.checked_at ? `Checked ${item.checked_at}` : "",
  ].filter(Boolean).join(" · ")));
  if (!item.checks?.length) return;
  const details = registryElement("details", "registry-detail-section");
  details.append(registryElement("summary", "", "Field checks and evidence"));
  item.checks.forEach((check) => {
    const block = registryElement("dl", "detail-meta");
    block.append(registryElement("dt", "", "Source"), registryElement("dd", "", check.source_url));
    Object.entries(check.comparisons || {}).forEach(([field, value]) => {
      const text = `${value.status} · PDF: ${String(value.expected ?? "Not provided")}\n` +
        (value.observed != null ? `Website: ${String(value.observed)}\n` : "") + (value.evidence || "");
      block.append(registryElement("dt", "", field.replaceAll("_", " ")), registryElement("dd", "", text));
    });
    if (check.error) block.append(registryElement("dt", "", "Check result"), registryElement("dd", "", check.error));
    if (check.enrichment_error) block.append(registryElement("dt", "", "Enrichment result"), registryElement("dd", "", check.enrichment_error));
    details.append(block);
  });
  container.append(details);
}

function meetingDateGroup(item) {
  const date = item.start_date || item.deadline_date;
  const day = typeof date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(date)
    ? new Date(`${date}T00:00:00Z`)
    : null;
  if (day && !Number.isNaN(day.getTime())) {
    return {
      key: `day:${date}`,
      label: new Intl.DateTimeFormat("en", {
        timeZone: "UTC", weekday: "short", year: "numeric", month: "short", day: "numeric",
      }).format(day),
    };
  }
  const label = item.raw_date || date || (item.end_date ? `Through ${item.end_date}` : "Date not specified");
  return { key: `period:${label}`, label };
}

function meetingInstitution(item) {
  const values = [item.organizer, item.institution, item.publisher, item.source_name,
    typeof item.source === "string" ? item.source : item.source?.name];
  return values.find((value) => typeof value === "string" && value.trim())?.trim() || "Institution not specified";
}

function meetingLocationSummary(item) {
  const location = item.collected_candidate?.location || item.location || item.pdf_observations?.find((row) => row.location)?.location;
  return typeof location === "string" && location.trim()
    ? (location.length > 56 ? `${location.slice(0, 55).trimEnd()}…` : location)
    : "Location not provided";
}

function appendMeetingLocations(container, item) {
  const pdfRows = item.pdf_observations || ((item.source_kind === "pdf" || item.origin === "pdf_import") ? [item] : []);
  const locations = pdfRows.map((row) => ({
    source: `PDF${row.source_filename ? ` · ${row.source_filename}` : ""}${row.page ? ` · page ${row.page}` : ""}`,
    value: row.location || "Location not provided",
  }));
  const webLocation = item.collected_candidate?.location || (item.origin === "web_collection" ? item.location : null);
  if (webLocation) {
    const candidate = item.collected_candidate;
    const sourceCheck = candidate
      ? item.checks?.find((check) => check.verification_status === "verified"
        && JSON.stringify(check.website_candidate) === JSON.stringify(candidate))
      : item.checks?.find((check) => check.verification_status === "verified" && check.source_url);
    const sourceUrl = item.collected_candidate_source_url || sourceCheck?.source_url || (!candidate ? item.source_urls?.[0] : "");
    locations.push({ source: `Website${sourceUrl ? ` · ${sourceUrl}` : ""}`, value: webLocation });
  }
  if (!locations.length && item.location) locations.push({ source: "Source", value: item.location });
  if (!locations.length) locations.push({ source: item.origin === "web_collection" ? "Website" : "PDF", value: "Location not provided" });
  locations.forEach(({ source, value }) => {
    container.append(registryElement("dt", "", "Location"), registryElement("dd", "meeting-entry__location", `${value}\n${source}`));
  });
}

async function loadRegistryMeetings() {
  const sequence = ++state.registry.meetingRequestSequence;
  renderRegistryNotice(els.registryMeetings, "Loading meetings…");
  const params = new URLSearchParams({ page: String(state.registry.meetingPage), page_size: "20" });
  if (els.registryMeetingSearch.value.trim()) params.set("query", els.registryMeetingSearch.value.trim());
  if (els.registryMeetingVerification.value) params.set("verification_status", els.registryMeetingVerification.value);
  try {
    const payload = await registryFetch(`/api/registry/meetings?${params}`);
    if (sequence !== state.registry.meetingRequestSequence) return;
    state.registry.meetingPagination = payload.pagination;
    els.registryMeetings.replaceChildren();
    const counts = payload.verification_counts || {};
    els.registryMeetingCounts.textContent = `As of ${payload.base_date} (UTC) · ${payload.pagination.total} current / upcoming · ${counts.verified || 0} verified · ` +
      `${counts.unchecked || 0} pending · ${counts.partial || 0} partial · ${counts.conflict || 0} conflicting`;
    const groups = new Map();
    payload.items.forEach((item) => {
      const date = meetingDateGroup(item);
      if (!groups.has(date.key)) groups.set(date.key, { ...date, items: [] });
      groups.get(date.key).items.push(item);
    });
    const agenda = registryElement("div", "meetings-agenda");
    groups.forEach((group) => {
      const section = registryElement("section", "meeting-date-group");
      section.append(registryElement("h4", "meeting-date-group__date", group.label));
      const entries = registryElement("div", "meeting-date-group__entries");
      group.items.forEach((item) => {
        const card = registryElement("details", "meeting-entry");
        const summary = registryElement("summary", "meeting-entry__summary");
        summary.append(registryElement("span", "meeting-entry__name", item.name),
          registryElement("span", "meeting-entry__institution", meetingInstitution(item)),
          registryElement("span", "meeting-entry__location-short", meetingLocationSummary(item)));
        card.append(summary);
        const body = registryElement("div", "meeting-entry__body");
        const fields = registryElement("dl", "detail-meta");
        const values = { "Date(s)": item.raw_date || [item.start_date, item.end_date].filter(Boolean).join(" through ") || item.deadline_date,
          "Time": item.raw_time_text, "Timezone": item.event_timezone || item.timezone, "Host": item.organizer,
          "Status": item.status, "Relevance": item.relevance || item.relevance_reason,
          "Deadline": item.deadline_date ? `${item.deadline_type || "Deadline"}: ${item.deadline_date}` : "" };
        Object.entries(values).forEach(([label, value]) => {
          if (value) fields.append(registryElement("dt", "", label), registryElement("dd", "", value));
        });
        appendMeetingLocations(fields, item);
        body.append(fields);
        const urls = [...new Set([...(item.source_urls || []), ...(item.sources || []).map((source) => source.source_url), item.online_url])];
        urls.forEach((url) => {
          const safe = safeSourceUrl(url);
          if (!safe) return;
          const link = registryElement("a", "registry-source-link", url);
          link.href = safe; link.target = "_blank"; link.rel = "noopener noreferrer";
          body.append(link);
        });
        appendInformationCheck(body, item);
        card.append(body);
        entries.append(card);
      });
      section.append(entries);
      agenda.append(section);
    });
    els.registryMeetings.append(agenda);
    if (!payload.items.length) renderRegistryNotice(els.registryMeetings, "No current meetings match these filters.");
    updateRegistryPagination("meetings", payload.pagination);
  } catch (error) {
    if (sequence === state.registry.meetingRequestSequence) renderRegistryNotice(els.registryMeetings, registryErrorMessage(error));
  }
}

async function loadRegistryPublishers() {
  if (!els.registryPublisherFilter) {
    return;
  }
  const currentValue = els.registryPublisherFilter.value;
  const allPublishers = registryElement("option", "", "All publishers");
  allPublishers.value = "";
  try {
    const payload = await registryFetch("/api/registry/publishers?include_pdf=true");
    const options = (payload.items || []).map((publisher) => {
      const option = registryElement("option", "", publisher.label || publisher.hostname);
      option.value = publisher.hostname;
      return option;
    });
    els.registryPublisherFilter.replaceChildren(allPublishers, ...options);
    if (els.registryPublisherCustom) {
      els.registryPublisherCustom.hidden = !payload.truncated;
      if (!payload.truncated) {
        els.registryPublisherCustom.value = "";
      }
    }
    if (options.some((option) => option.value === currentValue)) {
      els.registryPublisherFilter.value = currentValue;
    }
  } catch {
    els.registryPublisherFilter.replaceChildren(allPublishers);
    if (els.registryPublisherCustom) {
      els.registryPublisherCustom.hidden = true;
      els.registryPublisherCustom.value = "";
    }
  }
}

async function loadRegistry() {
  if (state.registry.loadPromise) {
    return state.registry.loadPromise;
  }
  state.registry.loadPromise = loadRegistryOnce();
  try {
    return await state.registry.loadPromise;
  } finally {
    state.registry.loadPromise = null;
  }
}

async function loadRegistryOnce() {
  renderRegistryNotice(els.registryReports, "Checking the historical archive…");
  try {
    const status = await registryFetch("/api/registry/status?include_pdf=true");
    state.registry.loaded = true;
    state.registry.available = Boolean(status.available);
    if (!status.available) {
      els.registryStatus.textContent = "Registry: unavailable";
      renderRegistryNotice(
        els.registryReports,
        "The archive is not connected yet. Chat and the Obsidian explorer remain available.",
      );
      renderRegistryNotice(els.registryArticles, "Article history will appear when the registry is connected.");
      updateRegistryPagination("reports", null);
      updateRegistryPagination("articles", null);
      return;
    }
    els.registryStatus.textContent = `${status.reports} reports · ${status.articles} articles`;
    await loadRegistryPublishers();
    await loadRegistryReports();
  } catch (error) {
    state.registry.loaded = false;
    state.registry.available = false;
    els.registryStatus.textContent = "Registry: unavailable";
    renderRegistryNotice(els.registryReports, registryErrorMessage(error));
  }
}

async function loadRegistryReports() {
  renderRegistryNotice(els.registryReports, "Loading reports…");
  try {
    const payload = await registryFetch(
      `/api/registry/reports?include_pdf=true&page=${state.registry.reportPage}&page_size=12`,
    );
    state.registry.reportPagination = payload.pagination;
    els.registryReports.replaceChildren();
    if (!payload.items.length) {
      renderRegistryNotice(els.registryReports, "No historical reports are available.");
    }
    payload.items.forEach((report) => {
      const button = registryElement("button", "registry-card");
      button.type = "button";
      if (report.source_kind === "pdf") button.dataset.pdfReportId = report.report_id;
      else button.dataset.reportDate = report.report_date;
      const heading = registryElement("strong", "registry-card__title", report.report_title);
      const metadata = registryElement(
        "span",
        "registry-card__meta",
        [report.report_date || "Report date unavailable", report.source_label || "Monitoring report",
          report.source_kind === "pdf" ? report.filename : "", `${report.article_count} articles`].filter(Boolean).join(" · "),
      );
      button.append(heading, metadata);
      els.registryReports.append(button);
    });
    updateRegistryPagination("reports", payload.pagination);
  } catch (error) {
    renderRegistryNotice(els.registryReports, registryErrorMessage(error));
    updateRegistryPagination("reports", null);
  }
}

function clearHistoricalReportContent() {
  els.registryImportedReport.hidden = true;
  els.registryImportedReport.replaceChildren();
  els.registryReportPdf.hidden = true;
  els.registryReportPdf.removeAttribute("href");
  els.registryReportPdf.removeAttribute("download");
  els.registryExecutiveSummary.hidden = true;
  els.registryBriefingExecutiveSummaryItems.hidden = true;
  els.registryBriefingExecutiveSummaryItems.replaceChildren();
  els.registryExecutiveSummaryItems.hidden = true;
  els.registryExecutiveSummaryItems.replaceChildren();
  els.registryMonitoringSnapshot.hidden = true;
  els.registrySnapshotMetrics.replaceChildren();
  els.registrySnapshotNotes.replaceChildren();
  els.registryReportArticlesTitle.hidden = true;
  els.registryReportArticles.replaceChildren();
}

function resetHistoricalReportDetail() {
  state.registry.reportRequestSequence += 1;
  state.registry.selectedReportDate = null;
  els.registryReportDetail.setAttribute("aria-busy", "false");
  els.registryReportTitle.textContent = "Select a report";
  els.registryReportMeta.textContent = "Choose a week to see monitoring coverage and its ordered source articles.";
  clearHistoricalReportContent();
}

async function loadRegistryReport(reportDate) {
  const requestToken = ++state.registry.reportRequestSequence;
  const isCurrentRequest = () =>
    state.registry.selectedReportDate === reportDate &&
    state.registry.reportRequestSequence === requestToken;
  state.registry.selectedReportDate = reportDate;
  els.registryReportDetail.setAttribute("aria-busy", "true");
  els.registryReportTitle.textContent = "Loading report…";
  els.registryReportMeta.textContent = "Loading report details…";
  clearHistoricalReportContent();
  try {
    const report = await registryFetch(`/api/registry/reports/${encodeURIComponent(reportDate)}`);
    if (!isCurrentRequest()) {
      return;
    }
    els.registryReportTitle.textContent = report.report_title;
    const monitoring = report.monitoring || {};
    els.registryReportMeta.textContent = [
      report.report_date,
      `${monitoring.sites_succeeded ?? "—"}/${monitoring.sites_checked ?? "—"} sites succeeded`,
      `${monitoring.sites_failed ?? "—"} failed`,
    ].join(" · ");
    const briefing = report.report_briefing;
    const snapshot = briefing?.monitoring_snapshot;
    const snapshotKeys = [
      "sites_checked",
      "sites_succeeded",
      "sites_failed",
      "pillar_a_updates",
      "pillar_b_updates",
    ];
    const briefingSummary = Array.isArray(briefing?.executive_summary)
      ? briefing.executive_summary.filter((item) => typeof item === "string" && item.trim())
      : [];
    const snapshotNotes = Array.isArray(snapshot?.notes)
      ? snapshot.notes.filter((item) => typeof item === "string" && item.trim())
      : [];
    const hasBriefing =
      Array.isArray(briefing?.executive_summary) &&
      briefingSummary.length > 0 &&
      briefingSummary.length === briefing.executive_summary.length &&
      snapshotKeys.every((key) => Number.isInteger(snapshot?.[key]) && snapshot[key] >= 0) &&
      Array.isArray(snapshot?.notes) &&
      snapshotNotes.length === snapshot.notes.length;
    const executiveSummary = hasBriefing
      ? briefingSummary
      : Array.isArray(report.executive_summary)
        ? report.executive_summary.filter((item) => typeof item === "string" && item.trim())
        : [];
    const summaryContainer = hasBriefing
      ? els.registryBriefingExecutiveSummaryItems
      : els.registryExecutiveSummaryItems;
    const summaryElement = hasBriefing ? "p" : "li";
    executiveSummary.forEach((summary) => {
      summaryContainer.append(registryElement(summaryElement, "", summary));
    });
    summaryContainer.hidden = executiveSummary.length === 0;
    els.registryExecutiveSummary.hidden = executiveSummary.length === 0;
    if (hasBriefing) {
      const snapshotLabels = [
        ["Sites checked", "sites_checked"],
        ["Succeeded", "sites_succeeded"],
        ["Failed", "sites_failed"],
        ["Pillar A updates", "pillar_a_updates"],
        ["Pillar B updates", "pillar_b_updates"],
      ];
      els.registrySnapshotMetrics.append(
        ...snapshotLabels.map(([label, key]) => registryMetric(label, snapshot[key])),
      );
      els.registrySnapshotNotes.append(
        ...snapshotNotes.map((note) => registryElement("li", "", note)),
      );
      els.registrySnapshotNotes.hidden = snapshotNotes.length === 0;
      els.registryMonitoringSnapshot.hidden = false;
    }
    const reportPdf = report.report_pdf;
    const pdfFilename = typeof reportPdf?.filename === "string" ? reportPdf.filename.trim() : "";
    const pdfDownloadUrl =
      typeof reportPdf?.download_url === "string" ? reportPdf.download_url.trim() : "";
    const expectedPdfFilename = `climate-monitor-${reportDate}.pdf`;
    const expectedPdfPath = `/api/registry/reports/${encodeURIComponent(reportDate)}/pdf`;
    let hasValidPdf = false;
    if (
      hasBriefing &&
      pdfFilename === reportPdf?.filename &&
      pdfFilename === expectedPdfFilename &&
      pdfDownloadUrl === reportPdf?.download_url
    ) {
      try {
        const parsedPdfUrl = new URL(pdfDownloadUrl, window.location.origin);
        const absoluteExpectedPdfUrl = new URL(expectedPdfPath, window.location.origin).href;
        hasValidPdf =
          (pdfDownloadUrl === expectedPdfPath || pdfDownloadUrl === absoluteExpectedPdfUrl) &&
          parsedPdfUrl.origin === window.location.origin &&
          parsedPdfUrl.pathname === expectedPdfPath &&
          parsedPdfUrl.search === "" &&
          parsedPdfUrl.hash === "" &&
          parsedPdfUrl.username === "" &&
          parsedPdfUrl.password === "";
      } catch {
        hasValidPdf = false;
      }
    }
    if (hasValidPdf) {
      els.registryReportPdf.href = reportPdf.download_url;
      els.registryReportPdf.download = reportPdf.filename;
      els.registryReportPdf.hidden = false;
    }
    els.registryReportArticlesTitle.hidden = false;
    report.articles.forEach((article) => {
      const item = registryElement("li", "registry-appearance");
      const button = registryElement("button", "registry-article-link", article.title);
      button.type = "button";
      button.dataset.articleId = article.article_id;
      const summaryPresentation = registrySummaryPresentation(article);
      const meta = registryElement(
        "span",
        "registry-card__meta",
        [
          article.pillar ? `Pillar ${article.pillar}` : article.section,
          article.publisher,
          summaryPresentation.sourceLabel,
        ]
          .filter(Boolean)
          .join(" · "),
      );
      item.append(button, meta);
      if (summaryPresentation.text) {
        item.append(registryElement("p", "registry-card__summary", summaryPresentation.text));
      }
      els.registryReportArticles.append(item);
    });
    if (!report.articles.length) {
      els.registryReportArticles.append(
        registryElement("li", "registry-notice", "No source articles are recorded for this report."),
      );
    }
  } catch (error) {
    if (isCurrentRequest()) {
      clearHistoricalReportContent();
      els.registryReportTitle.textContent = "Report unavailable";
      els.registryReportMeta.textContent = registryErrorMessage(error);
    }
  } finally {
    if (isCurrentRequest()) {
      els.registryReportDetail.setAttribute("aria-busy", "false");
    }
  }
}

async function loadRegistryArticles() {
  renderRegistryNotice(els.registryArticles, "Loading articles…");
  const requestSequence = ++state.registry.articleRequestSequence;
  const params = new URLSearchParams({
    page: String(state.registry.articlePage),
    page_size: "20",
    include_pdf: "true",
  });
  if (els.registrySearch.value.trim()) params.set("query", els.registrySearch.value.trim());
  const publisher = els.registryPublisherCustom?.value.trim() || els.registryPublisherFilter.value;
  if (publisher) params.set("source", publisher);
  try {
    const payload = await registryFetch(`/api/registry/articles?${params.toString()}`);
    if (requestSequence !== state.registry.articleRequestSequence) {
      return;
    }
    state.registry.articlePagination = payload.pagination;
    els.registryArticles.replaceChildren();
    if (!payload.items.length) {
      renderRegistryNotice(
        els.registryArticles,
        "No articles match these filters.",
      );
    }
    payload.items.forEach((article) => {
      const button = registryElement("button", "registry-card");
      button.type = "button";
      button.dataset.articleId = article.article_id;
      const pdfSource = article.source_kind === "pdf";
      button.dataset.articleSource = pdfSource ? "pdf" : "registry";
      button.append(
        registryElement("strong", "registry-card__title", article.title || article.canonical_url),
        registryElement(
          "span",
          "registry-card__meta",
          pdfSource
            ? `${article.publisher} · PDF import · ${article.occurrence_count} PDF ${article.occurrence_count === 1 ? "mention" : "mentions"} · last seen ${article.last_seen || "—"}`
            : `${article.publisher} · ${article.source_label || "Registry"} · last seen ${article.last_seen}${article.pdf_occurrence_count ? ` · ${article.pdf_occurrence_count} PDF ${article.pdf_occurrence_count === 1 ? "mention" : "mentions"}` : ""}`,
        ),
      );
      els.registryArticles.append(button);
    });
    updateRegistryPagination("articles", payload.pagination);
  } catch (error) {
    if (requestSequence !== state.registry.articleRequestSequence) {
      return;
    }
    renderRegistryNotice(els.registryArticles, registryErrorMessage(error));
    updateRegistryPagination("articles", null);
  }
}

function appendVerifiedInformation(container, information) {
  if (!information) return;
  const block = registryElement("section", "registry-detail-section");
  block.append(registryElement("h4", "", "Verified information"));
  if (information.summary) block.append(registryElement("p", "", information.summary));
  appendRegistryTags(block, "Categories", information.categories || []);
  appendRegistryTags(block, "Keywords", information.keywords || []);
  block.append(registryElement("p", "muted", `From checked website content · ${information.generated_at || ""}`));
  container.append(block);
}

async function loadImportedRegistryReport(documentId) {
  const requestToken = ++state.registry.reportRequestSequence;
  state.registry.selectedReportDate = documentId;
  const isCurrent = () => requestToken === state.registry.reportRequestSequence;
  clearHistoricalReportContent();
  els.registryReportDetail.setAttribute("aria-busy", "true");
  els.registryReportTitle.textContent = "Loading imported report…";
  els.registryReportMeta.textContent = "Loading PDF details…";
  try {
    const report = await registryFetch(`/api/registry/pdf-intake/reports/${encodeURIComponent(documentId)}`);
    if (!isCurrent()) return;
    els.registryReportTitle.textContent = report.report_title;
    els.registryReportMeta.textContent = [report.report_date || "Report date unavailable", "PDF import", report.filename].join(" · ");
    const details = els.registryImportedReport;
    const metadata = registryElement("dl", "detail-meta");
    const fields = {"Report date": report.report_date, "Sources": report.source_filenames?.join(", "),
      "Edition": report.edition, "Reporting period": report.reporting_period,
      "Period start": report.period_start, "Period end": report.period_end, "Pages": report.page_count,
      "PDF created": report.pdf_created_at, "PDF modified": report.pdf_modified_at, "Imported": report.imported_at};
    Object.entries(fields).forEach(([label, value]) => {
      if (value != null && value !== "") metadata.append(registryMetric(label, value));
    });
    Object.entries(report.pdf_metadata || {}).forEach(([label, value]) => metadata.append(registryMetric(label.replace(/^\//, ""), value)));
    details.append(metadata);
    (report.executive_summary || []).forEach((summary) => {
      details.append(registryElement("h4", "", "Executive Summary"), registryElement("p", "", summary));
    });
    if (report.articles?.length) details.append(registryElement("h4", "", "Articles"));
    (report.articles || []).forEach((article) => {
      const button = registryElement("button", "registry-article-link", article.title || article.canonical_url);
      button.type = "button";
      button.dataset.articleId = article.article_id;
      button.dataset.articleSource = "pdf";
      details.append(button);
      appendRegistryPdfOccurrences(details, article.occurrences);
    });
    if (report.calendar_items?.length) details.append(registryElement("h4", "", "Meetings & Key Dates"));
    (report.calendar_items || []).forEach((item) => {
      const block = registryElement("section", "registry-detail-section");
      block.append(registryElement("h4", "", item.name || "Calendar item"));
      const values = registryElement("dl", "detail-meta");
      ["kind", "event_type", "raw_date", "date_precision", "start_date", "end_date", "date_evidence",
        "organizer", "publisher", "location", "raw_time_text", "event_timezone", "status", "deadline_type",
        "deadline_date", "deadline_evidence", "relevance", "page"].forEach((key) => {
        if (item[key]) values.append(registryMetric(key.replaceAll("_", " "), item[key]));
      });
      block.append(values);
      [...new Set([...(item.source_urls || []), item.online_url])].forEach((url) => {
        const safe = safeSourceUrl(url);
        if (!safe) return;
        const link = registryElement("a", "registry-source-link", url);
        link.href = safe; link.target = "_blank"; link.rel = "noopener noreferrer";
        block.append(link);
      });
      appendInformationCheck(block, item);
      details.append(block);
    });
    (report.pages || []).forEach((page) => {
      if (!page.text) return;
      const section = registryElement("details", "registry-detail-section");
      section.append(registryElement("summary", "", `Extracted text · page ${page.page}`), registryElement("pre", "md-preview", page.text));
      details.append(section);
    });
    details.hidden = false;
    const download = report.report_pdf;
    if (download?.download_url === `/api/registry/pdf-intake/reports/${documentId}/pdf` && download.filename) {
      els.registryReportPdf.href = download.download_url;
      els.registryReportPdf.download = download.filename;
      els.registryReportPdf.hidden = false;
    }
  } catch (error) {
    if (isCurrent()) {
      clearHistoricalReportContent();
      els.registryReportTitle.textContent = "Report unavailable";
      els.registryReportMeta.textContent = registryErrorMessage(error);
    }
  } finally {
    if (isCurrent()) els.registryReportDetail.setAttribute("aria-busy", "false");
  }
}

function appendRegistryPdfOccurrences(container, occurrences) {
  if (!Array.isArray(occurrences) || !occurrences.length) {
    return;
  }
  const block = registryElement("div", "registry-pdf-history");
  block.append(registryElement("h4", "", "PDF report history"));
  const list = registryElement("ol", "registry-pdf-history__list");
  occurrences.forEach((occurrence) => {
    const item = registryElement("li", "registry-pdf-history__item");
    const sourceName = occurrence.source_document || occurrence.source_observations?.[0]?.filename || "Imported PDF";
    const date = occurrence.report_date
      ? `Report date ${occurrence.report_date}`
      : occurrence.publication_date
        ? `Publication date ${occurrence.publication_date}`
        : "Date unavailable";
    const page = occurrence.page ? ` · page ${occurrence.page}` : "";
    item.append(
      registryElement("strong", "", `${sourceName} · ${date}${page}`),
      registryElement("p", "registry-pdf-history__summary", occurrence.summary || "No article context was captured."),
    );
    appendInformationCheck(item, occurrence);
    appendVerifiedInformation(item, occurrence.verified_information);
    const sourceUrl = safeSourceUrl(occurrence.raw_url);
    if (sourceUrl) {
      const link = registryElement("a", "registry-source-link", "Open source link");
      link.href = sourceUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
    }
    list.append(item);
  });
  block.append(list);
  container.append(block);
}

function appendRegistryTags(container, label, values) {
  if (!values || !values.length) {
    return;
  }
  const block = registryElement("div", "registry-tag-block");
  block.append(registryElement("h4", "", label));
  const tags = registryElement("div", "registry-tags");
  values.forEach((value) => tags.append(registryElement("span", "registry-tag", value)));
  block.append(tags);
  container.append(block);
}

async function loadRegistryArticle(articleId, articleSource = "registry") {
  const requestSequence = ++state.registry.articleDetailRequestSequence;
  const isCurrentRequest = () => requestSequence === state.registry.articleDetailRequestSequence;
  els.registryArticleDetail.setAttribute("aria-busy", "true");
  els.registryArticleTitle.textContent = "Loading article…";
  els.registryArticleMeta.replaceChildren();
  els.registryEnrichment.replaceChildren();
  els.registryEnrichment.hidden = true;
  els.registryAppearances.replaceChildren();
  els.registryAppearancesSection.hidden = articleSource === "pdf";
  els.registryContentSection.hidden = true;
  els.registryOriginalLink.hidden = true;
  try {
    const pdfSource = articleSource === "pdf";
    const endpoint = pdfSource
      ? "/api/registry/pdf-intake/articles"
      : "/api/registry/articles";
    const article = await registryFetch(`${endpoint}/${encodeURIComponent(articleId)}`);
    if (!isCurrentRequest()) {
      return;
    }
    els.registryArticleTitle.textContent = article.title || article.canonical_url;
    const annotationBasis = article.source_annotation?.source_basis;
    const usesAlternatePublisherPage =
      annotationBasis === "official_replacement" || annotationBasis === "publisher_excerpt";
    const sourceUrl = safeSourceUrl(
      (usesAlternatePublisherPage && article.source_annotation?.source_url) ||
        article.original_url ||
        article.canonical_url,
    );
    if (sourceUrl) {
      els.registryOriginalLink.href = sourceUrl;
      els.registryOriginalLink.textContent = pdfSource
        ? "Open linked source"
        :
        annotationBasis === "official_replacement"
          ? "Open official replacement"
          : annotationBasis === "publisher_excerpt"
            ? "Open publisher page"
            : "Open original source";
      els.registryOriginalLink.hidden = false;
    }
    const metrics = [
      registryMetric("Publisher", article.publisher),
      registryMetric("First seen", article.first_seen),
      registryMetric("Last seen", article.last_seen),
    ];
    if (pdfSource) {
      metrics.push(registryMetric("PDF mentions", article.occurrences.length));
    } else if (article.pdf_occurrences?.length) {
      metrics.push(registryMetric("PDF mentions", article.pdf_occurrences.length));
    }
    if (article.latest_fetch?.fetch_status) {
      metrics.push(registryMetric("Latest fetch", article.latest_fetch.fetch_status));
    }
    const collectedAt = article.collected_at || article.content?.collected_at ||
      (!article.date_basis ? article.content?.fetched_at : null);
    if (collectedAt) {
      metrics.push(registryMetric("Collected at", collectedAt));
    } else if (article.information_date) {
      metrics.push(registryMetric("Information date", article.information_date));
    } else if (article.publication_date) {
      metrics.push(registryMetric("Publication date", article.publication_date));
    } else if (article.report_date) {
      metrics.push(registryMetric("Report date (publication unconfirmed)", article.report_date));
    } else {
      metrics.push(registryMetric("Date basis", "Unconfirmed"));
    }
    els.registryArticleMeta.append(...metrics);
    const summaryPresentation = registrySummaryPresentation(article);
    if (pdfSource && article.type_safe_classification?.label) {
      appendRegistryTags(els.registryEnrichment, "TypeSafe classification", [article.type_safe_classification.label]);
    }
    if (!pdfSource && summaryPresentation.text) {
      const summaryBlock = registryElement("div", "registry-summary");
      summaryBlock.append(
        registryElement("h4", "", "Summary"),
        registryElement("p", "", summaryPresentation.text),
      );
      els.registryEnrichment.append(summaryBlock);
    }
    if (!pdfSource) {
      appendRegistryTags(
        els.registryEnrichment,
        "Categories",
        article.categories?.length ? article.categories : article.enrichment?.categories || [],
      );
      appendRegistryTags(
        els.registryEnrichment,
        "Keywords",
        article.keywords?.length ? article.keywords : article.enrichment?.keywords || [],
      );
    }
    if (!pdfSource && summaryPresentation.provenanceCopy) {
      const reviewed =
        summaryPresentation.provenance?.endsWith("_annotation") &&
        article.source_annotation?.generated_on
          ? ` · reviewed ${article.source_annotation.generated_on}`
          : "";
      els.registryEnrichment.append(
        registryElement(
          "p",
          "muted registry-provenance",
          `${summaryPresentation.provenanceCopy}${reviewed}`,
        ),
      );
    }
    appendRegistryPdfOccurrences(
      els.registryEnrichment,
      pdfSource ? article.occurrences : article.pdf_occurrences,
    );
    els.registryEnrichment.hidden = els.registryEnrichment.childElementCount === 0;
    const appearances = article.appearances || [];
    appearances.forEach((appearance) => {
      const item = registryElement("li", "registry-appearance");
      item.append(
        registryElement("strong", "", appearance.report_title),
        registryElement(
          "span",
          "registry-card__meta",
          `${appearance.report_date} · ${appearance.pillar ? `Pillar ${appearance.pillar}` : appearance.section}`,
        ),
      );
      els.registryAppearances.append(item);
    });
    if (!appearances.length) {
      els.registryAppearances.append(
        registryElement("li", "registry-notice", "No report appearances are recorded for this article."),
      );
    }
    const displayText = article.content?.markdown || article.content?.supporting_excerpt || "";
    if (displayText) {
      els.registryContentSection.hidden = false;
      els.registryContentTitle.textContent = article.content.markdown ? "Original Markdown" : "Supporting excerpt";
      els.registryMarkdown.textContent = displayText;
    }
  } catch (error) {
    if (!isCurrentRequest()) {
      return;
    }
    els.registryArticleTitle.textContent = "Article unavailable";
    els.registryEnrichment.append(registryElement("p", "registry-notice", registryErrorMessage(error)));
    els.registryEnrichment.hidden = false;
  } finally {
    if (isCurrentRequest()) {
      els.registryArticleDetail.setAttribute("aria-busy", "false");
    }
  }
}

async function loadFinalPipelineReports() {
  const panel = document.querySelector('#registryView');
  if (!panel) return;
  const section = registryElement('section', 'registry-notice');
  section.append(registryElement('h3', '', 'Approved reports and pipeline status'));
  panel.append(section);
  const results = await Promise.allSettled([
    fetch('/api/registry/final-reports').then(response => { if (!response.ok) throw new Error('unavailable'); return response.json(); }),
    fetch('/api/job-status').then(response => { if (!response.ok) throw new Error('unavailable'); return response.json(); }),
  ]);
  if (results[0].status === 'fulfilled') {
    const items = results[0].value.items || [];
    for (const item of items) {
      const link = registryElement('a', 'registry-source-link', `${item.occurrence} · approved PDF revision ${item.revision}`);
      link.href = `/api/registry/final-reports/${encodeURIComponent(item.occurrence)}/pdf`;
      section.append(link, document.createElement('br'));
    }
    if (!items.length) section.append(registryElement('p', '', 'No final approved report is archived yet.'));
  } else section.append(registryElement('p', '', 'Final report history is unavailable.'));
  if (results[1].status === 'fulfilled') {
    const value = results[1].value;
    const labels = {T1: 'Daily source checks', T2: 'Website rotation', T3: 'Weekly search', T4: 'Biweekly report', T5: 'Final PDF review', T6: 'Approved report delivery', T7: 'Status observer', T8: 'LLM cost report', T9: 'Docker cleanup', T10: 'Acquisition review and recovery'};
    section.append(registryElement('p', '', value.observer?.is_stale ? 'Scheduler evidence is stale.' : `Scheduler observed at ${value.generated_at || 'unavailable'}.`));
    for (const [role, job] of Object.entries(value.jobs || {})) {
      const business = value.business?.[role];
      section.append(registryElement('p', '', `${labels[role] || role}: scheduler ${job.state}; business ${business?.status || 'not observed'}`));
      if (business) {
        const detail = document.createElement('details');
        detail.append(registryElement('summary', '', 'Saved business results'), registryElement('pre', '', JSON.stringify(business, null, 2)));
        section.append(detail);
      }
    }
  } else section.append(registryElement('p', '', 'Scheduler evidence is unavailable.'));
}

function attachEvents() {
  els.registryMeetingSearchForm?.addEventListener("submit", (event) => {
    event.preventDefault(); state.registry.meetingPage = 1; void loadRegistryMeetings();
  });
  [[els.meetingsPrevious, -1], [els.meetingsNext, 1]].forEach(([button, direction]) => {
    button?.addEventListener("click", () => {
      state.registry.meetingPage += direction; void loadRegistryMeetings();
    });
  });
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

  els.registryModeButtons.forEach((button) => {
    button.addEventListener("click", () => setRegistryMode(button.dataset.registryMode));
  });

  if (els.registrySearchForm) {
    els.registrySearchForm.addEventListener("submit", (event) => {
      event.preventDefault();
      state.registry.articlePage = 1;
      void loadRegistryArticles();
    });
  }

  if (els.registryPublisherFilter) {
    els.registryPublisherFilter.addEventListener("change", () => {
      if (els.registryPublisherCustom && els.registryPublisherFilter.value) {
        els.registryPublisherCustom.value = "";
      }
    });
  }

  if (els.registryPublisherCustom) {
    els.registryPublisherCustom.addEventListener("input", () => {
      if (els.registryPublisherCustom.value.trim()) {
        els.registryPublisherFilter.value = "";
      }
    });
  }

  if (els.reportsPrevious) {
    els.reportsPrevious.addEventListener("click", () => {
      state.registry.reportPage = Math.max(1, state.registry.reportPage - 1);
      void loadRegistryReports();
    });
    els.reportsNext.addEventListener("click", () => {
      state.registry.reportPage += 1;
      void loadRegistryReports();
    });
    els.articlesPrevious.addEventListener("click", () => {
      state.registry.articlePage = Math.max(1, state.registry.articlePage - 1);
      void loadRegistryArticles();
    });
    els.articlesNext.addEventListener("click", () => {
      state.registry.articlePage += 1;
      void loadRegistryArticles();
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

  if (els.jumpToReports) {
    els.jumpToReports.addEventListener("click", () => {
      setRegistryMode("reports");
      setWorkspaceView("registryView");
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
      setGraphMode(button.dataset.graphMode || "keywords");
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

    const historicalReportLink = target.closest("[data-historical-report-date]");
    if (historicalReportLink) {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
        return;
      }
      event.preventDefault();
      openHistoricalReport(historicalReportLink.dataset.historicalReportDate);
      return;
    }

    const pdfReportCard = target.closest("[data-pdf-report-id]");
    if (pdfReportCard) {
      void loadImportedRegistryReport(pdfReportCard.dataset.pdfReportId);
      return;
    }
    const reportCard = target.closest("[data-report-date]");
    if (reportCard) {
      openHistoricalReport(reportCard.dataset.reportDate);
      return;
    }

    const articleCard = target.closest("[data-article-id]");
    if (articleCard) {
      setRegistryMode("articles");
      const articleSource = articleCard.dataset.articleSource || "registry";
      void loadRegistryArticle(articleCard.dataset.articleId, articleSource);
      return;
    }

    const sourceCard = target.closest(".source-card");
    if (sourceCard) {
      if (sourceCard.dataset.path?.startsWith("wiki/")) {
        setActiveContext(sourceCard.dataset.path, { switchView: "obsidianView" });
      } else if (sourceCard.dataset.path) {
        window.open(`/${sourceCard.dataset.path}`, "_blank", "noopener");
      } else {
        const url = safeSourceUrl(sourceCard.dataset.url);
        if (url) window.open(url, "_blank", "noopener");
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

  window.addEventListener("hashchange", () => {
    if (window.location.hash === "#meetings") {
      setWorkspaceView("meetingsView");
      return;
    }
    const reportDate = historicalReportDateFromHash();
    if (reportDate) {
      openHistoricalReport(reportDate, { updateHash: false });
    } else {
      resetHistoricalReportDetail();
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopGraphAnimation();
    } else if (state.activeView === "obsidianView") {
      renderCurrentGraph();
    } else if (state.activeView === "meetingsView") {
      state.registry.meetingPage = 1;
      void loadRegistryMeetings();
    }
  });
}

async function main() {
  loadThread();
  fetch("/api/manage/session", { credentials: "same-origin" })
    .then((response) => (response.ok ? response.json() : { authenticated: false }))
    .then(({ authenticated }) => {
      if (authenticated && els.hermesLink) els.hermesLink.hidden = false;
    })
    .catch(() => {});
  setAnswerMode(state.answerMode);
  setGraphMode(state.graphMode);
  setWorkspaceView(window.location.hash === "#meetings" ? "meetingsView" : state.activeView);
  renderMessages();
  attachEvents();
  const linkedReportDate = historicalReportDateFromHash();
  if (linkedReportDate) {
    openHistoricalReport(linkedReportDate, { updateHash: false });
  }

  try {
    await loadConfig();
    void hydrateDocumentsInBackground();
  } catch (error) {
    els.status.textContent = "Service unavailable";
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

loadFinalPipelineReports();
