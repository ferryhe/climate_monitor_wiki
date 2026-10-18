const STORAGE_KEY = "climate-monitor-agent-thread";

const DEFAULT_PROMPT_STARTERS = [
  {
    "label": "Generate PDF report",
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
      "Search the Page Index to focus this graph. Labels appear on hover, keyboard focus, or selection; selecting a note highlights linked pages and edges.",
    legendHtml: `
      <span><i class="dot dot-daily"></i>Daily</span>
      <span><i class="dot dot-topic"></i>Topic</span>
      <span><i class="dot dot-index"></i>Index</span>
    `,
  },
  keywords: {
    title: "Concept Map",
    hint:
      "Uses 27 manually maintained climate rules matched against Wiki page content, headings/titles, and linked source reports. Shows up to 18 concepts found on 2+ pages, or up to 12 if none recur. Size counts linked notes. Click a note to set chat context or a concept to filter the Page Index.",
    legendHtml: `
      <span><i class="dot dot-daily"></i>Daily</span>
      <span><i class="dot dot-topic"></i>Topic</span>
      <span><i class="dot dot-index"></i>Index</span>
      <span><i class="dot dot-keyword"></i>Concept</span>
    `,
  },
  articleCategories: {
    title: "Category Map",
    hint: "Counts show distinct public articles. Select a category or connection to inspect supporting articles. Shared categories do not imply causality.",
    legendHtml: `<span><i class="dot dot-keyword"></i>Category</span>`,
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
  "categories",
  "keywords",
  "key findings",
  "new research",
  "anti-bot / acquisition notes",
  "stealth escalation enabled",
  "dedup notes",
  "climate",
  "news",
]);

const ANSWER_MODE_COPY = {
  brief: {
    label: "Brief",
    title: "Fast snapshot mode with a short, focused answer.",
    placeholder: "Ask for a quick answer…",
  },
  detailed: {
    label: "Detailed",
    title: "Evidence-heavy mode for focused explainers and source-backed questions.",
    placeholder: "Ask about climate risk and insurance…",
  },
  executive: {
    label: "Report",
    title: "Structured report mode for period summaries, trend shifts, and big-picture questions.",
    placeholder: "Ask for a report or trend summary…",
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
  graphExpanded: false,
  graphReturnFocus: null,
  answerMode: "detailed",
  activeContextPath: null,
  confirmedContextPath: null,
  isSending: false,
  chatAccess: null,
  activeView: "chatView",
  pendingQuestionFocus: false,
  markdownByPath: {},
  markdownRequests: {},
  graph: null,
  graphFrame: 0,
  graphData: { notes: null, keywords: null, articleCategories: null },
  articleCategoryGraphLoading: false,
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
    publishersLoaded: false,
    publishersPromise: null,
    selectedReportDate: null,
    reportRequestSequence: 0,
    articleRequestSequence: 0,
    reportListRequestSequence: 0,
    listPages: new Map(),
    listVersion: null,
    listFetchSequence: 0,
    listVersionSequence: 0,
    articleDetailRequestSequence: 0,
    evidenceNavigationSequence: 0,
    loadPromise: null,
  },
};

const els = {
  messages: document.getElementById("messages"),
  form: document.getElementById("chatForm"),
  input: document.getElementById("messageInput"),
  send: document.getElementById("sendButton"),
  chatAccessMessage: document.getElementById("chatAccessMessage"),
  chatAccessDetails: document.getElementById("chatAccessDetails"),
  chatAccessSummary: document.getElementById("chatAccessSummary"),
  chatContextHeader: document.getElementById("chatContextHeader"),
  jumpToReports: document.getElementById("jumpToReportsButton"),
  navMenuButton: document.getElementById("navMenuButton"),
  mobilePageLabel: document.getElementById("mobilePageLabel"),
  chatTokenForm: document.getElementById("chatTokenForm"),
  chatToken: document.getElementById("chatToken"),
  retryChatQuestion: document.getElementById("retryChatQuestion"),
  clearChat: document.getElementById("clearChatButton"),
  clearContext: document.getElementById("clearContextButton"),
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
  graphViewport: document.getElementById("graphViewport"),
  graphExpandButton: document.getElementById("graphExpandButton"),
  graphExpandedSelection: document.getElementById("graphExpandedSelection"),
  graphSvg: document.getElementById("graphSvg"),
  articleCategoryEvidence: document.getElementById("articleCategoryEvidence"),
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
  registryArticleInfo: document.getElementById("registryArticleInfo"),
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
    .replace(/`([^`\n]+)`/g, (_, code) => keepLink(`<code>${code}</code>`))
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
    .replace(/https?:\/\/[^\s<]+/g, (url) => keepLink(url))
    .replace(/\[(\d+(?:\s*,\s*\d+)*)\]/g, (_, value) => value
      .split(/\s*,\s*/)
      .map((index) => `<button class="citation" type="button" data-citation-index="${index}" aria-label="Show cited evidence [${index}]">[${index}]</button>`)
      .join(""))
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
  const match = /^#historical-report=(\d{4}-\d{2}-\d{2})(?:&.*)?$/.exec(window.location.hash);
  return match ? match[1] : "";
}

function safeReportSourceUrl(value) {
  const absolute = safeSourceUrl(value);
  if (absolute) return absolute;
  return /^\/(?:sources|wiki)\/[\w./-]+$/.test(value || "") ? value : "";
}

function beginEvidenceNavigation() {
  state.registry.articleDetailRequestSequence += 1;
  return ++state.registry.evidenceNavigationSequence;
}

function openHistoricalReport(reportDate, { updateHash = true, fallbackSourceUrl = "", fallbackSourceIsChecked = false,
  originalSourceUrl = "" } = {}) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(reportDate || "")) {
    return;
  }
  beginEvidenceNavigation();
  const sourceUrl = safeReportSourceUrl(fallbackSourceUrl);
  const originalUrl = safeSourceUrl(originalSourceUrl);
  const checkedRole = fallbackSourceIsChecked && sourceUrl;
  const nextHash = `#historical-report=${encodeURIComponent(reportDate)}${sourceUrl ? `&url=${encodeURIComponent(sourceUrl)}` : ""}${checkedRole ? `&checked=${encodeURIComponent(sourceUrl)}${originalUrl ? `&original=${encodeURIComponent(originalUrl)}` : ""}` : ""}`;
  if (updateHash && window.location.hash !== nextHash) {
    window.history.pushState(null, "", nextHash);
  }
  state.registry.selectedReportDate = reportDate;
  setRegistryMode("reports");
  setWorkspaceView("registryView");
  void loadRegistryReport(reportDate, sourceUrl, Boolean(checkedRole), originalUrl);
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
  const value = String(label || "").trim();
  const isDomain = /^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$/i.test(value);
  return !HIDDEN_GRAPH_CONCEPTS.has(value.toLowerCase()) && !isDomain && !/^Article Article [a-f0-9]{24}$/i.test(value);
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
    staticLayout: true,
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
    staticLayout: mode === "notes" || Boolean(graph?.static_layout || graph?.staticLayout),
  };
}

function graphDataForCurrentMode() {
  if (state.graphMode === "article-categories") {
    return state.graphData.articleCategories || {
      mode: "article-categories",
      title: GRAPH_COPY.articleCategories.title,
      hint: "Loading public article categories…",
      emptyMessage: "Loading public article categories…",
      legendHtml: GRAPH_COPY.articleCategories.legendHtml,
      nodes: [], links: [], staticLayout: true,
    };
  }
  const precomputed = state.graphData?.[state.graphMode];
  const graph = precomputed
    ? normalizeGraphData(state.graphMode, precomputed)
    : state.graphMode === "keywords"
      ? buildKeywordGraph(state.rows)
      : buildNoteGraph(state.rows, state.edges);
  return state.graphMode === "notes" ? scopeNotesGraphToPageSearch(graph) : graph;
}

function scopeNotesGraphToPageSearch(graph) {
  const query = String(els.wikiSearch?.value || "").trim();
  if (!query) return graph;
  const paths = new Set(state.filteredRows.map((row) => row.path));
  const nodes = graph.nodes.filter((node) => paths.has(node.refPath || node.id));
  const visibleIds = new Set(nodes.map((node) => node.id));
  return {
    ...graph,
    nodes,
    links: graph.links.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target)),
    hint: `Page Index search: ${nodes.length} matching notes. Labels appear on hover, keyboard focus, or selection.`,
    emptyMessage: "No notes match the Page Index search.",
  };
}

function buildArticleCategoryGraph(articles, taxonomy) {
  const definitions = taxonomy.categories;
  const allowed = new Map(definitions.map((category) => [category.label, category.id]));
  const byIdentity = new Map();
  for (const article of articles) {
    if (!article.article_id || !Array.isArray(article.categories)) continue;
    const identity = article.canonical_url || `${article.source_kind || "registry"}:${article.article_id}`;
    if (byIdentity.has(identity)) continue;
    const categories = [...new Set(article.categories)].filter((label) => allowed.has(label));
    byIdentity.set(identity, { ...article, categories });
  }
  const categoryArticles = new Map(definitions.map((category) => [category.id, []]));
  const pairArticles = new Map();
  for (const article of byIdentity.values()) {
    const keys = article.categories.map((label) => allowed.get(label)).sort();
    for (const key of keys) categoryArticles.get(key).push(article);
    for (let left = 0; left < keys.length; left += 1) {
      for (let right = left + 1; right < keys.length; right += 1) {
        const key = JSON.stringify([keys[left], keys[right]]);
        if (!pairArticles.has(key)) pairArticles.set(key, []);
        pairArticles.get(key).push(article);
      }
    }
  }
  const nodeId = (key) => `article-category:${key}`;
  const nodes = definitions.map((category) => ({
    id: nodeId(category.id), label: category.label, description: category.description,
    kind: "category", type: "category", mode: "article-categories",
    weight: categoryArticles.get(category.id).length, articles: categoryArticles.get(category.id),
  }));
  const links = [...pairArticles].map(([key, supporting]) => {
    const [left, right] = JSON.parse(key);
    return { source: nodeId(left), target: nodeId(right), weight: supporting.length, articles: supporting };
  });
  return { mode: "article-categories", title: GRAPH_COPY.articleCategories.title,
    hint: GRAPH_COPY.articleCategories.hint, legendHtml: GRAPH_COPY.articleCategories.legendHtml,
    nodes, links, emptyMessage: "No public article categories are available.", staticLayout: true, loaded: true };
}

async function loadArticleCategoryGraph() {
  if (state.articleCategoryGraphLoading || state.graphData.articleCategories?.loaded) return;
  clearArticleCategoryEvidence();
  state.articleCategoryGraphLoading = true;
  try {
    const { items: articles, taxonomy } = await registryFetch("/api/registry/article-categories");
    state.graphData.articleCategories = buildArticleCategoryGraph(articles, taxonomy);
    clearArticleCategoryEvidence();
    renderCurrentGraph();
  } catch (error) {
    clearArticleCategoryEvidence();
    state.graphData.articleCategories = {
      mode: "article-categories", title: GRAPH_COPY.articleCategories.title,
      hint: registryErrorMessage(error), legendHtml: GRAPH_COPY.articleCategories.legendHtml,
      emptyMessage: "Article categories could not be loaded.", nodes: [], links: [], staticLayout: true,
    };
    renderCurrentGraph();
  } finally {
    state.articleCategoryGraphLoading = false;
  }
}

function clearArticleCategoryEvidence() {
  if (!els.articleCategoryEvidence) return;
  els.articleCategoryEvidence.replaceChildren();
  els.articleCategoryEvidence.hidden = true;
}

function setGraphMode(mode) {
  if (!mode) {
    return;
  }
  if (state.graphMode === "article-categories" && mode !== "article-categories") {
    clearArticleCategoryEvidence();
  }
  state.graphMode = mode;
  els.graphModeButtons.forEach((button) => {
    const active = button.dataset.graphMode === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  renderCurrentGraph();
  if (mode === "article-categories") void loadArticleCategoryGraph();
}

function getNodeRadius(node) {
  if (node.mode === "article-categories") return node.radius;
  const importance = Math.max(1, Number(node.importance || node.degree || node.weight || 1));
  if (node.kind === "keyword") {
    return Math.min(22, 7 + Math.sqrt(importance) * 2.4);
  }
  return Math.min(12, (node.type === "index" ? 7 : 5) + Math.sqrt(importance));
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

function graphCanvasHeight(rectHeight, categoryHeight = 0) {
  return categoryHeight || Math.max(320, rectHeight || 700);
}

function graphNodePosition(mode, node, rank, count, width, height, categoryPosition = null) {
  if (mode === "article-categories") return categoryPosition;
  if (mode !== "notes" && mode !== "keywords" && Number.isFinite(node.x) && Number.isFinite(node.y)) {
    return { x: node.x, y: node.y };
  }
  return projectRadialPosition(rank, count, width / 2, height / 2, Math.min(width, height) * 0.42);
}

function setGraphExpanded(expanded) {
  if (Boolean(expanded) === state.graphExpanded || !els.graphViewport) return;

  state.graphExpanded = Boolean(expanded);
  els.graphViewport.classList.toggle("is-expanded", state.graphExpanded);
  document.documentElement.classList.toggle("graph-expanded", state.graphExpanded);
  if (els.graphExpandButton) {
    els.graphExpandButton.setAttribute("aria-expanded", String(state.graphExpanded));
    els.graphExpandButton.textContent = state.graphExpanded ? "Close full-screen graph" : "Expand graph";
  }

  if (state.graphExpanded) {
    state.graphReturnFocus = document.activeElement;
    els.graphExpandButton?.focus();
  }
  renderExpandedGraphSelection();
  renderCurrentGraph();
  if (!state.graphExpanded) {
    const returnFocus = state.graphReturnFocus;
    state.graphReturnFocus = null;
    if (returnFocus?.focus) returnFocus.focus();
    else els.graphExpandButton?.focus();
  }
}

function toggleGraphExpanded() {
  setGraphExpanded(!state.graphExpanded);
}

function handleGraphKeydown(event) {
  if (event.key === "Escape" && state.graphExpanded) {
    event.preventDefault();
    setGraphExpanded(false);
  }
}

function renderExpandedGraphSelection() {
  if (!els.graphExpandedSelection) return;
  els.graphExpandedSelection.hidden = !state.graphExpanded;
  if (!state.graphExpanded) return;

  const selected = state.rows.find((row) => row.path === state.activeContextPath);
  els.graphExpandedSelection.textContent = selected
    ? `Selected page: ${selected.displayTitle} · ${selected.type}. Close graph to view Note Detail.`
    : "Select a note to see its title here. Close graph to view Note Detail.";
}

function layoutArticleCategoryNodes(nodes, width, height = 0) {
  const columns = width < 304 ? 1 : width < 600 ? 2 : width < 900 ? 3 : 4;
  height = Math.max(height, 760, Math.ceil(nodes.length / columns) * 170 + 80);
  const maxWeight = Math.max(1, ...nodes.map((node) => Number(node.weight) || 0));
  const placed = [];
  const positions = new Map();
  const ranked = [...nodes].sort((a, b) => b.weight - a.weight || a.label.localeCompare(b.label));
  for (const [rank, node] of ranked.entries()) {
    const radius = 16 + (width < 520 ? 20 : 32) * Math.sqrt((Number(node.weight) || 0) / maxWeight);
    const labelLines = [];
    for (const word of node.label.split(" ")) {
      if (!labelLines.length || `${labelLines[labelLines.length - 1]} ${word}`.length > 18) labelLines.push(word);
      else labelLines[labelLines.length - 1] += ` ${word}`;
    }
    const seed = projectRadialPosition(rank, nodes.length, width / 2, height / 2, Math.min(width, height) * 0.42);
    seed.y = height / 2 + (seed.y - height / 2) * height / Math.min(width, height);
    let nearest = null;
    for (let x = 76; x <= width - 76; x += 10) {
      for (let y = radius + 16; y <= height - radius - labelLines.length * 16 - 46; y += 10) {
        const bounds = { left: x - 64, right: x + 64, top: y - radius, bottom: y + radius + labelLines.length * 16 + 30 };
        if (placed.some((other) => bounds.left < other.right + 14 && bounds.right > other.left - 14
          && bounds.top < other.bottom + 14 && bounds.bottom > other.top - 14)) continue;
        const distance = (x - seed.x) ** 2 + (y - seed.y) ** 2;
        if (!nearest || distance < nearest.distance) nearest = { x, y, radius, labelLines, bounds, distance };
      }
    }
    if (!nearest) return layoutArticleCategoryNodes(nodes, width, height + 170);
    placed.push(nearest.bounds);
    positions.set(node.id, nearest);
  }
  return { height, nodes: nodes.map((node) => positions.get(node.id)) };
}

function categoryEdgeRoutes(links, nodes, width, height) {
  const targets = [];
  for (let x = 24; x <= width - 24; x += 16) {
    for (let y = 24; y <= height - 24; y += 16) {
      if (!nodes.some(({ bounds }) => x > bounds.left - 20 && x < bounds.right + 20
        && y > bounds.top - 20 && y < bounds.bottom + 20)) targets.push({ x, y });
    }
  }
  return links.map(({ source, target }) => {
    const midpoint = { x: (source.x + target.x) / 2, y: (source.y + target.y) / 2 };
    let nearest = 0;
    targets.forEach((point, index) => {
      if ((point.x - midpoint.x) ** 2 + (point.y - midpoint.y) ** 2
        < (targets[nearest].x - midpoint.x) ** 2 + (targets[nearest].y - midpoint.y) ** 2) nearest = index;
    });
    const hit = targets.splice(nearest, 1)[0];
    const endpoint = (node) => {
      const distance = Math.hypot(hit.x - node.x, hit.y - node.y) || 1;
      return { x: node.x + (hit.x - node.x) * node.radius / distance, y: node.y + (hit.y - node.y) * node.radius / distance };
    };
    const start = endpoint(source), end = endpoint(target);
    // The quadratic passes through the exposed mouse target at its midpoint.
    return { hit, radius: 4, path: `M${start.x},${start.y} Q${2 * hit.x - (start.x + end.x) / 2},${2 * hit.y - (start.y + end.y) / 2} ${end.x},${end.y}` };
  });
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
    button.setAttribute("aria-description", buttonCopy.title);
  });
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
      const messages = parsed.filter((item) => item && item.role && item.content !== undefined
        && !item.pending && !item.transient && !isLegacyStartupFailureNotice(item));
      let changed = messages.length !== parsed.length;
      messages.forEach((item) => {
        const requestMessages = item.blockedRequest?.messages;
        if (Array.isArray(requestMessages)) {
          const cleanRequestMessages = requestMessages.filter((message) => !isLegacyStartupFailureNotice(message));
          if (cleanRequestMessages.length !== requestMessages.length) {
            item.blockedRequest.messages = cleanRequestMessages;
            changed = true;
          }
        }
      });
      state.messages = messages;
      if (changed) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state.messages));
      }
    }
  } catch {
    localStorage.removeItem(STORAGE_KEY);
  }
}

function saveThread() {
  const persisted = state.messages.filter((item) => !item.transient && !isLegacyStartupFailureNotice(item));
  localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted.slice(-16)));
}

function clearThread() {
  state.messages = [];
  localStorage.removeItem(STORAGE_KEY);
  renderMessages();
  setSending(state.isSending);
  if (els.retryChatQuestion) els.retryChatQuestion.hidden = true;
}

function stopGraphAnimation() {
  if (state.graphFrame) {
    cancelAnimationFrame(state.graphFrame);
    state.graphFrame = 0;
  }
}

function showMobileDetail(panel) {
  if (!panel || !window.matchMedia("(max-width: 760px)").matches) return;
  if (!panel.classList.contains("is-detail-open")) {
    panel.dataset.listScroll = String(window.scrollY);
    panel.listFocus = panel.querySelector(".registry-card:focus");
  }
  panel.classList.add("is-detail-open");
  window.scrollTo({ top: 0 });
  panel.querySelector(".mobile-back")?.focus({ preventScroll: true });
}

function closeMobileDetail(panel) {
  panel.classList.remove("is-detail-open");
  window.scrollTo({ top: Number(panel.dataset.listScroll) || 0 });
  (panel.listFocus || panel.querySelector(".registry-list-panel .registry-card"))?.focus({ preventScroll: true });
}

function closeNavigation() {
  if (els.navMenuButton?.getAttribute("aria-expanded") === "true") els.navMenuButton.focus();
  els.navMenuButton?.setAttribute("aria-expanded", "false");
}

function setWorkspaceView(viewId) {
  if (!viewId) {
    return;
  }
  if (viewId !== "obsidianView" && state.graphExpanded) setGraphExpanded(false);
  state.activeView = viewId;
  closeNavigation();
  if (els.mobilePageLabel) els.mobilePageLabel.textContent = {chatView: "Chat", registryView: "Reports", meetingsView: "Dates", obsidianView: "Graph"}[viewId] || "Menu";
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
  if (viewId === "obsidianView") renderCurrentGraph();
  if (viewId === "chatView" && state.pendingQuestionFocus) {
    state.pendingQuestionFocus = false;
    renderMessages({ focusCurrentQuestion: true });
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

function isLegacyStartupFailureNotice(item) {
  return item?.role === "assistant" && typeof item.content === "string"
    && item.content.startsWith("Could not reach the backend service: ")
    && item.content.endsWith("\n\nStart it with `uvicorn api_server:app --host 0.0.0.0 --port 8501`.")
    && !item.context && !item.rangeReport && !(item.sources?.length);
}

function appendMessage(role, content, options = {}) {
  state.messages.push({
    role,
    content,
    sources: options.sources || [],
    pending: Boolean(options.pending),
    ...(options.transient ? { transient: true } : {}),
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
      renderMessages({ focusCurrentQuestion: true });
      return;
    }
  }

  state.messages.push({ role: "assistant", content, sources, rangeReport, context, pending: false });
  saveThread();
  renderMessages({ focusCurrentQuestion: true });
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
              <a class="source-card" data-index="${escapeHtml(source.index)}" ${sourceNavigationAttributes(source)} href="${escapeHtml(sourceNavigationHref(source) || sourceCitationHref(source))}" target="_blank" rel="noopener noreferrer">
                <div class="source-card__title">
                  <span class="source-card__index">[${source.index}]</span>
                  <span class="source-card__heading">${escapeHtml(source.document_title || source.title || source.path || "Source")}</span>
                </div>
                <p class="source-card__meta">${escapeHtml((source.corpus || "wiki").toUpperCase())} · ${escapeHtml(source.matched_section || source.heading || source.path || "")}${Number.isFinite(source.score) ? ` · Retrieval score ${escapeHtml(source.score)}` : ""}</p>
                <p class="source-card__snippet">${escapeHtml(source.snippet || "")}</p>
              </a>
            `,
          )
          .join("")}
      </div>
    </details>
  `;
}

function sourceNavigationTarget(source) {
  const path = source.path || "";
  if (source.meeting_id || path === "wiki/registry-meetings.md") {
    const name = source.matched_section || (source.heading !== "meeting" ? source.heading : "") || source.title || "";
    return { kind: "meeting", id: source.meeting_id || "", name };
  }
  const articleMatch = /^(?:wiki\/)?(pdf-)?article-(.+)\.md$/.exec(path);
  const articleId = source.article_id || (articleMatch
    ? articleMatch[1] && !articleMatch[2].startsWith("article-") ? `article-${articleMatch[2]}` : articleMatch[2]
    : "");
  const articleTarget = (id, articleSource) => {
    const roles = reviewedSourceRoles(source);
    const originalUrl = safeSourceUrl(source.original_source_url || source.canonical_url || source.source_urls?.[0] || source.url);
    return { kind: "article", id, source: articleSource, url: roles?.checkedSourceUrl || originalUrl,
      ...(roles ? { checkedSourceUrl: roles.checkedSourceUrl, originalUrl: roles.originalSourceUrl } : {}) };
  };
  const legacyKind = [source.heading, source.type].find((value) => ["meeting", "article", "pdf_article"].includes(value));
  // Old EvidenceTurn records kept the Registry identity in evidence_id and the
  // entity kind in heading/type. Wiki chunks and web reads also have evidence
  // IDs, so only interpret this legacy shape when it is not a Wiki source.
  const legacyId = !path && source.corpus !== "wiki" ? source.evidence_id || "" : "";
  if (!source.meeting_id && !path && legacyKind === "meeting" && legacyId) {
    return { kind: "meeting", id: legacyId, name: source.title || source.document_title || "" };
  }
  const legacyArticleId = !path && ["article", "pdf_article"].includes(legacyKind) ? legacyId : "";
  if (!articleId && legacyArticleId) {
    return articleTarget(legacyArticleId, legacyKind === "pdf_article" ? "pdf" : "registry");
  }
  if (articleId) {
    const articleSource = source.article_source || (path.includes("pdf-article-") ? "pdf" : "registry");
    return articleTarget(articleId, articleSource);
  }
  const pdfObservationMatch = /^(?:wiki\/)?registry-source-observation-pdf-article-occurrence-([A-Za-z0-9_-]+)\.md$/.exec(path);
  if (pdfObservationMatch) {
    const roles = reviewedSourceRoles(source);
    return { kind: "pdf-observation", id: `pdf-article-occurrence-${pdfObservationMatch[1]}`,
      search: source.matched_section || source.document_title || source.title || "",
      url: roles?.checkedSourceUrl || safeSourceUrl(source.canonical_url || source.source_urls?.[0] || source.url),
      ...(roles ? { checkedSourceUrl: roles.checkedSourceUrl, originalUrl: roles.originalSourceUrl } : {}),
      snippet: source.snippet || "" };
  }
  const articleRoles = reviewedSourceRoles(source);
  const articleUrl = source.type === "web" ? safeSourceUrl(source.original_source_url || source.canonical_url || source.url) : "";
  if (articleUrl) return { kind: "article-url", url: articleUrl,
    ...(articleRoles ? { checkedSourceUrl: articleRoles.checkedSourceUrl, originalUrl: articleRoles.originalSourceUrl } : {}) };
  const reportMatch = /^(?:wiki|sources)\/climate-monitor-(\d{4}-\d{2}-\d{2})\.md$/.exec(path);
  if (reportMatch) return { kind: "report", date: reportMatch[1], url: safeReportSourceUrl(sourceCitationHref(source)),
    ...(articleRoles ? { checkedSourceUrl: articleRoles.checkedSourceUrl, originalUrl: articleRoles.originalSourceUrl } : {}) };
  return null;
}

function sourceNavigationAttributes(source) {
  const target = sourceNavigationTarget(source);
  if (!target) return "";
  if (target.kind === "meeting") return `data-source-navigation="meeting"${target.id ? ` data-meeting-id="${escapeHtml(target.id)}"` : ""}${target.name ? ` data-meeting-name="${escapeHtml(target.name)}"` : ""}`;
  if (target.kind === "article") return `data-source-navigation="article" data-article-id="${escapeHtml(target.id)}" data-article-source="${escapeHtml(target.source)}"${target.url ? ` data-source-url="${escapeHtml(target.url)}" data-original-url="${escapeHtml(target.originalUrl || target.url)}"` : ""}${target.checkedSourceUrl ? ` data-source-checked="true"` : ""}`;
  if (target.kind === "article-url") return `data-source-navigation="article-url" data-source-url="${escapeHtml(target.url)}"${target.checkedSourceUrl ? ` data-source-checked="true" data-checked-url="${escapeHtml(target.checkedSourceUrl)}" data-original-url="${escapeHtml(target.originalUrl || target.url)}"` : ""}`;
  if (target.kind === "pdf-observation") return `data-source-navigation="pdf-observation" data-occurrence-id="${escapeHtml(target.id)}" data-observation-search="${escapeHtml(target.search)}" data-observation-url="${escapeHtml(target.url)}" data-observation-snippet="${escapeHtml(target.snippet)}"${target.checkedSourceUrl ? ` data-source-checked="true" data-original-url="${escapeHtml(target.originalUrl || target.url)}"` : ""}`;
  return `data-source-navigation="report" data-report-date="${target.date}"${target.url ? ` data-report-source-url="${escapeHtml(target.url)}"` : ""}${target.checkedSourceUrl ? ` data-source-checked="true" data-original-url="${escapeHtml(target.originalUrl || target.url)}"` : ""}`;
}

function sourceNavigationHref(source) {
  const target = sourceNavigationTarget(source);
  if (!target) return "";
  if (target.kind === "meeting") return target.id
    ? `#meeting=${encodeURIComponent(`id:${target.id}${target.name ? `|name:${target.name}` : ""}`)}`
    : target.name ? `#meeting=${encodeURIComponent(`name:${target.name}`)}` : "#meetings";
  if (target.kind === "article") return `#registry-article=${encodeURIComponent(target.source)}:${encodeURIComponent(target.id)}${target.url ? `&url=${encodeURIComponent(target.url)}` : ""}${target.checkedSourceUrl ? `&checked=1${target.originalUrl ? `&original=${encodeURIComponent(target.originalUrl)}` : ""}` : ""}`;
  if (target.kind === "article-url") return `#registry-article-url=${encodeURIComponent(target.url)}${target.checkedSourceUrl ? `&checked=${encodeURIComponent(target.checkedSourceUrl)}&original=${encodeURIComponent(target.originalUrl || target.url)}` : ""}`;
  if (target.kind === "pdf-observation") {
    const params = new URLSearchParams({ search: target.search, url: target.url, snippet: (target.snippet || "").slice(0, 100) });
    if (target.checkedSourceUrl) {
      params.set("checked", target.checkedSourceUrl);
      params.set("original", target.originalUrl || target.url);
    }
    return `#registry-pdf-occurrence=${encodeURIComponent(target.id)}&${params}`;
  }
  return `#historical-report=${encodeURIComponent(target.date)}${target.url ? `&url=${encodeURIComponent(target.url)}` : ""}${target.checkedSourceUrl ? `&checked=${encodeURIComponent(target.checkedSourceUrl)}&original=${encodeURIComponent(target.originalUrl || target.url)}` : ""}`;
}

function sourceNavigationFromHash() {
  try {
    const rawHash = window.location.hash;
    const report = /^#historical-report=(\d{4}-\d{2}-\d{2})(?:&.*)?$/.exec(rawHash);
    if (report) {
      const params = new URLSearchParams(rawHash.slice(rawHash.indexOf("&") + 1));
      const url = safeReportSourceUrl(params.get("url") || "");
      const checked = params.get("checked") === url ? safeReportSourceUrl(url) : "";
      return { kind: "report", date: report[1], url: checked || url,
        ...(checked ? { checkedSourceUrl: checked, originalUrl: safeSourceUrl(params.get("original") || "") } : {}) };
    }
    const pdfObservation = /^#registry-pdf-occurrence=([^&]+)(?:&.*)?$/.exec(rawHash);
    if (pdfObservation) {
      const params = new URLSearchParams(rawHash.slice(rawHash.indexOf("&") + 1));
      return { kind: "pdf-observation", id: decodeURIComponent(pdfObservation[1]),
        search: params.get("search") || "", url: params.get("url") || "", snippet: params.get("snippet") || "",
        ...(params.get("checked") === params.get("url") ? { checkedSourceUrl: safeSourceUrl(params.get("checked") || ""),
          originalUrl: safeSourceUrl(params.get("original") || "") } : {}) };
    }
    const hash = decodeURIComponent(rawHash);
    const meeting = /^#meeting=(?:id:([^|]+)(?:\|name:(.+))?|name:(.+))$/.exec(hash);
    if (meeting) return { kind: "meeting", id: meeting[1] || "", name: meeting[2] || meeting[3] || "" };
    const article = /^#registry-article=(registry|pdf):([^&]+)(?:&(.*))?$/.exec(rawHash);
    if (article) {
      const params = new URLSearchParams(article[3] || "");
      const target = { kind: "article", source: article[1], id: decodeURIComponent(article[2]),
        url: safeSourceUrl(params.get("url") || "") };
      if (params.get("checked") === "1") {
        target.checkedSourceUrl = target.url;
        target.originalUrl = safeSourceUrl(params.get("original") || "");
      }
      return target;
    }
    const articleUrl = /^#registry-article-url=([^&]+)(?:&(.*))?$/.exec(rawHash);
    if (articleUrl) {
      const params = new URLSearchParams(articleUrl[2] || "");
      const url = safeSourceUrl(decodeURIComponent(articleUrl[1]));
      const checked = safeSourceUrl(params.get("checked") || "");
      return { kind: "article-url", url,
        ...(checked ? { checkedSourceUrl: checked, originalUrl: safeSourceUrl(params.get("original") || "") } : {}) };
    }
  } catch {
    return null;
  }
  return null;
}

function reviewedCheckedSourceUrl(source) {
  const values = [source, source?.latest_occurrence, ...(source?.occurrences || []), ...(source?.pdf_occurrences || [])];
  for (const value of values) {
    const url = value?.checked_source_url;
    const owner = value?.owner_agent_verification;
    if (url && owner?.verification_status === "verified" && owner.review_id && owner.reviewer
      && owner.final_url === url) return safeSourceUrl(url);
    if (url && value?.checked_source_provenance === "owner_agent_verified_source"
      && value?.original_source_url && value.canonical_url === url) {
      return safeSourceUrl(url);
    }
  }
  return "";
}

function reviewedSourceRoles(source) {
  const checkedSourceUrl = reviewedCheckedSourceUrl(source);
  if (!checkedSourceUrl) return null;
  const values = [source, source?.latest_occurrence, ...(source?.occurrences || []), ...(source?.pdf_occurrences || [])];
  const originalSourceUrl = values.map((value) => safeSourceUrl(value?.original_url || value?.original_source_url
    || value?.raw_url || value?.owner_agent_verification?.source_url || value?.canonical_url)).find(Boolean);
  return { checkedSourceUrl, originalSourceUrl: originalSourceUrl || checkedSourceUrl };
}

function sourceCitationHref(source) {
  const checked = reviewedCheckedSourceUrl(source);
  if (checked) return checked;
  const canonical = safeSourceUrl(source.canonical_url);
  if (canonical) return canonical;
  const path = source.path || "";
  if (/^(?:wiki|sources)\/[\w./-]+$/.test(path)) return `/${path}`;
  return safeSourceUrl(source.url);
}

function renderEmptyState() {
  const starters = Array.isArray(state.promptStarters) && state.promptStarters.length
    ? state.promptStarters
    : DEFAULT_PROMPT_STARTERS;
  const shell = document.createElement("section");
  shell.className = "empty-state";
  shell.innerHTML = `
    <div class="suggestions">
      ${starters.map(
        (starter) =>
          `<button class="suggestion-chip" type="button" data-prompt="${escapeHtml(starter.prompt || "")}" data-answer-mode="${escapeHtml(starter.answer_mode || "detailed")}" title="${escapeHtml(starter.prompt || "")}" aria-description="${escapeHtml(starter.prompt || "")}">
            <span>${escapeHtml(starter.label || "")}</span><span aria-hidden="true">↗</span>
          </button>`,
      ).join("")}
    </div>
  `;

  shell.querySelectorAll(".suggestion-chip").forEach((button) => {
    button.addEventListener("click", () => {
      setAnswerMode(button.getAttribute("data-answer-mode") || state.answerMode);
      els.input.value = button.getAttribute("data-prompt");
      els.input.focus();
    });
  });

  els.messages.appendChild(shell);
}

function renderMessages({ focusCurrentQuestion = false } = {}) {
  if (!els.messages) {
    return;
  }

  els.messages.innerHTML = "";
  els.messages.style.paddingBottom = "";
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

  if (focusCurrentQuestion) {
    if (els.chatView?.hidden || !els.messages.clientHeight) {
      state.pendingQuestionFocus = true;
      return;
    }
    state.pendingQuestionFocus = false;
    els.messages.style.paddingBottom = `${els.messages.clientHeight}px`;
    const question = [...els.messages.querySelectorAll(".message-row--user")].at(-1);
    if (question) {
      els.messages.scrollTop += question.getBoundingClientRect().top - els.messages.getBoundingClientRect().top;
      return;
    }
  } else {
    els.messages.style.paddingBottom = "";
  }
  els.messages.scrollTop = els.messages.scrollHeight;
}

function setSending(value) {
  state.isSending = value;
  if (els.send) {
    els.send.disabled = value || Boolean(blockedChatMessage());
  }
  if (els.form) {
    els.form.setAttribute("aria-busy", String(value));
  }
  if (els.retryChatQuestion) els.retryChatQuestion.disabled = value;
}

function blockedChatMessage() {
  return state.messages.findLast((item) => item.role === "user" && item.blockedRequest);
}

function renderChatAccess(status, notice = "") {
  state.chatAccess = status;
  const reset = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", dateStyle: "medium", timeStyle: "long" }).format(new Date(status.reset_at));
  const allowance = `Free questions today: ${status.remaining} of ${status.limit ?? 5} remaining.`;
  if (els.chatToken) {
    els.chatToken.placeholder = allowance;
    els.chatToken.setAttribute("aria-description", allowance);
  }
  if (els.chatAccessSummary) {
    els.chatAccessSummary.textContent = status.access_enabled && !status.invalid_token
      ? "Access enabled" : `Access token · ${status.remaining} free questions left`;
    els.chatAccessSummary.title = `Free questions reset after ${reset}.`;
    els.chatAccessSummary.setAttribute("aria-description", els.chatAccessSummary.title);
  }
  if (els.chatAccessMessage) els.chatAccessMessage.textContent = notice || (status.invalid_token
    ? "This token is invalid or has been revoked." : "");
  if (els.chatTokenForm) els.chatTokenForm.hidden = status.access_enabled && !status.invalid_token;
  if (els.retryChatQuestion) els.retryChatQuestion.hidden = !blockedChatMessage() || status.invalid_token || (!status.access_enabled && status.remaining === 0);
  if (els.chatAccessDetails && (status.invalid_token || notice || (els.retryChatQuestion && !els.retryChatQuestion.hidden))) els.chatAccessDetails.open = true;
}

async function refreshChatAccess() {
  try {
    const response = await fetch("/api/chat/access", { cache: "no-store" });
    if (response.ok) renderChatAccess(await response.json());
  } catch {
    if (els.chatToken) {
      els.chatToken.placeholder = "Free question allowance is unavailable. Please retry.";
      els.chatToken.setAttribute("aria-description", els.chatToken.placeholder);
    }
  }
}

async function continueWithToken(event) {
  event.preventDefault();
  if (state.isSending) return;
  const button = els.chatTokenForm.querySelector("button");
  button.disabled = true;
  try {
    const token = els.chatToken.value.trim();
    if (!token) throw new Error("This token is invalid or has been revoked.");
    const response = await fetch("/api/chat/access", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (!response.ok) {
      const payload = await response.json();
      const invalidToken = response.status === 401 || (response.status === 422 && Array.isArray(payload.detail) && payload.detail.some((error) => error.loc?.includes("token")));
      throw new Error(invalidToken ? "This token is invalid or has been revoked." : (typeof payload.detail === "string" ? payload.detail : "Access could not be enabled. Please retry."));
    }
    renderChatAccess(await response.json(), "Access enabled. You can continue asking questions.");
    els.chatToken.value = "";
    const blocked = blockedChatMessage();
    if (blocked) await sendMessage(blocked.content, blocked.blockedRequest);
  } catch (error) {
    els.chatAccessMessage.textContent = error.message;
    if (els.chatAccessDetails) els.chatAccessDetails.open = true;
  } finally {
    button.disabled = false;
  }
}

async function sendMessage(message, retryRequest = null) {
  setSending(true);
  if (!retryRequest) appendMessage("user", message);
  const userMessage = state.messages.findLast((item) => item.role === "user");
  const requestBody = retryRequest || {
    messages: state.messages.filter((item) => !item.pending && !item.transient
      && !isLegacyStartupFailureNotice(item)).map(messageToApi),
    contextPath: state.activeContextPath,
    language: "en",
    answerMode: state.answerMode,
  };
  appendMessage("assistant", "", { pending: true });

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });

    if (!response.ok) {
      if (response.status === 429 || response.status === 401) {
        const payload = await response.json();
        if (response.status === 401 || payload.detail?.code === "chat_quota_exhausted") {
          userMessage.blockedRequest = requestBody;
          state.messages = state.messages.filter((item) => !item.pending);
          saveThread();
          renderMessages();
          if (response.status === 429) renderChatAccess(payload.detail);
          else {
            els.chatAccessMessage.textContent = "This token is invalid or has been revoked.";
            els.chatTokenForm.hidden = false;
            if (els.chatAccessDetails) els.chatAccessDetails.open = true;
          }
          return;
        }
      }
      const detail = await response.text();
      throw new Error(detail || `HTTP ${response.status}`);
    }

    const payload = await response.json();
    delete userMessage.blockedRequest;
    replacePendingAssistant(payload.text, payload.sources || [], payload.range_report || null, payload.context || null);
    setConnectionStatus(payload.agent_mode, payload.model);
    setAnswerMode(payload.answer_mode || state.answerMode);
  } catch (error) {
    delete userMessage.blockedRequest;
    replacePendingAssistant(`Request failed: ${error.message}`);
  } finally {
    setSending(false);
    void refreshChatAccess();
    if (els.input) {
      els.input.focus();
    }
  }
}

function renderChatContext() {
  if (els.chatContextHeader) els.chatContextHeader.hidden = !state.activeContextPath;
  const doc = state.rows.find((item) => item.path === state.activeContextPath);

  if (!doc) {
    els.activeContextBadge.hidden = true;
    els.activeContextBadge.classList.remove("is-confirmed");
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
  const confirmed = state.confirmedContextPath === doc.path;
  els.activeContextBadge.classList.toggle("is-confirmed", confirmed);
  els.activeContextBadge.textContent = confirmed
    ? `Selected reference for chat: ${doc.displayTitle}`
    : `Focused ${contextKind}: ${doc.displayTitle}`;
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
  if (state.graphMode === "notes") renderCurrentGraph();
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
  const neighborIds = new Set();
  for (const edge of state.graph.links || []) {
    if (edge.source.id === state.activeContextPath) neighborIds.add(edge.target.id);
    if (edge.target.id === state.activeContextPath) neighborIds.add(edge.source.id);
  }
  state.graph.nodes.forEach((node, index) => {
    const active = node.kind === "note" && (node.refPath || node.id) === state.activeContextPath;
    const group = state.graph.nodeEls[index];
    const circle = group.querySelector("circle");
    group.classList.toggle("is-active", active);
    group.classList.toggle("is-neighbor", node.kind === "note" && neighborIds.has(node.id));
    if (circle) {
      const baseRadius = getNodeRadius(node);
      circle.setAttribute("r", String(active ? baseRadius + 2 : baseRadius));
      circle.setAttribute("stroke-width", active ? "2" : "1");
    }
  });
  (state.graph.links || []).forEach((edge, index) => {
    const line = state.graph.linkEls?.[index];
    if (line) {
      line.classList.toggle(
        "is-context-link",
        edge.source.id === state.activeContextPath || edge.target.id === state.activeContextPath,
      );
    }
  });
  renderExpandedGraphSelection();
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
  const categoryMode = graphData.mode === "article-categories";
  if (!categoryMode) svg.style.height = "";
  const width = categoryMode
    ? Math.max(280, svg.getBoundingClientRect().width || 1200)
    : Math.max(320, svg.getBoundingClientRect().width || 1200);
  const categoryLayout = categoryMode ? layoutArticleCategoryNodes(graphData.nodes, width) : null;
  const height = graphCanvasHeight(svg.getBoundingClientRect().height, categoryLayout?.height);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.style.height = categoryMode ? `${height}px` : "";
  svg.classList.toggle("category-map", categoryMode);
  svg.innerHTML = "";

  if (!graphData.nodes.length) {
    const empty = document.createElementNS(NS, "text");
    empty.textContent = graphData.emptyMessage || "Graph data is loading…";
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
    graphData.mode === "article-categories"
      ? Math.max(Number(node.weight || 0), 1)
      : Math.max(Number(node.weight || 0), degreeById.get(node.id) || 0, 1);
  const centralRank = new Map(
    [...graphData.nodes]
      .sort((left, right) => importanceFor(right) - importanceFor(left) || left.label.localeCompare(right.label))
      .map((node, index) => [node.id, index]),
  );
  const nodes = graphData.nodes.map((node, index) => {
    const importance = importanceFor(node);
    const rank = centralRank.get(node.id) || 0;
    const radialPosition = projectRadialPosition(
      rank,
      graphData.nodes.length,
      width / 2,
      height / 2,
      Math.min(width, height) * 0.42,
    );
    const position = graphNodePosition(
      graphData.mode,
      node,
      rank,
      graphData.nodes.length,
      width,
      height,
      categoryLayout?.nodes[index],
    );

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
    .map((edge) => ({ ...edge, source: byId.get(edge.source), target: byId.get(edge.target) }))
    .filter((edge) => edge.source && edge.target);
  const staticLayout = Boolean(graphData.staticLayout);
  const categoryRoutes = categoryMode ? categoryEdgeRoutes(links, nodes, width, height) : [];
  let ticksRemaining = 0;

  const linkGroup = document.createElementNS(NS, "g");
  const nodeGroup = document.createElementNS(NS, "g");
  const edgeTargetGroup = document.createElementNS(NS, "g");
  const edgeLabelGroup = document.createElementNS(NS, "g");
  svg.appendChild(linkGroup);
  svg.appendChild(nodeGroup);
  svg.appendChild(edgeTargetGroup);
  svg.appendChild(edgeLabelGroup);

  const edgeTargetEls = [];
  const lineEls = links.map((edge, index) => {
    const line = document.createElementNS(NS, categoryMode ? "path" : "line");
    line.setAttribute("fill", "none");
    line.setAttribute(
      "stroke",
      graphData.mode === "keywords" ? "rgba(135, 168, 199, 0.28)" : "rgba(151, 163, 170, 0.42)",
    );
    line.setAttribute("stroke-width", "1.1");
    if (graphData.mode === "article-categories") {
      line.setAttribute("class", "graph-edge graph-edge--article-categories");
      const target = document.createElementNS(NS, "circle");
      target.setAttribute("class", "category-edge-target");
      target.setAttribute("role", "button");
      target.setAttribute("tabindex", "0");
      target.setAttribute("r", String(categoryRoutes[index].radius));
      target.setAttribute("aria-label", `${edge.source.label} and ${edge.target.label}: ${edge.weight} supporting articles`);
      const title = document.createElementNS(NS, "title");
      title.textContent = `${edge.weight} articles contain both ${edge.source.label} and ${edge.target.label}`;
      line.appendChild(title);
      target.appendChild(title.cloneNode(true));
      const activate = () => activateArticleCategoryEvidence(`${edge.source.label} + ${edge.target.label}`, edge.articles);
      line.addEventListener("click", activate);
      target.addEventListener("click", activate);
      target.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); }
      });
      edgeTargetGroup.appendChild(target);
      edgeTargetEls.push(target);
    } else if (graphData.mode === "notes") {
      line.setAttribute("class", "graph-edge graph-edge--notes");
    }
    linkGroup.appendChild(line);
    return line;
  });
  const edgeLabelEls = graphData.mode === "article-categories" ? links.map((edge) => {
    const label = document.createElementNS(NS, "text");
    label.textContent = String(edge.weight);
    label.setAttribute("class", "graph-edge-count");
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("aria-hidden", "true");
    label.hidden = true;
    label.style.display = "none";
    const index = links.indexOf(edge);
    const line = lineEls[index];
    const target = edgeTargetEls[index];
    const reveal = () => { label.style.display = ""; line.classList.add("is-highlighted"); };
    const hide = () => { if (line.getAttribute("aria-pressed") !== "true") { label.style.display = "none"; line.classList.remove("is-highlighted"); } };
    line.setAttribute("aria-pressed", "false");
    target.setAttribute("aria-pressed", "false");
    line.addEventListener("pointerenter", reveal); line.addEventListener("pointerleave", hide);
    target.addEventListener("pointerenter", reveal); target.addEventListener("pointerleave", hide);
    target.addEventListener("focus", reveal); target.addEventListener("blur", hide);
    const select = () => {
      lineEls.forEach((other, index) => {
        other.setAttribute("aria-pressed", String(other === line));
        edgeTargetEls[index].setAttribute("aria-pressed", String(other === line));
        other.classList.toggle("is-highlighted", other === line);
        if (edgeLabelEls[index]) edgeLabelEls[index].style.display = other === line ? "" : "none";
      });
    };
    line.addEventListener("click", select);
    target.addEventListener("click", select);
    target.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") select(); });
    edgeLabelGroup.appendChild(label);
    return label;
  }) : [];

  const nodeEls = nodes.map((node) => {
    const group = document.createElementNS(NS, "g");
    group.setAttribute("class", `graph-node graph-node--${node.kind || "note"} graph-node--${graphData.mode}`);
    group.setAttribute("role", "button");
    group.setAttribute("tabindex", "0");
    group.setAttribute("aria-label", categoryMode ? `${node.label}: ${node.weight} supporting articles`
      : node.kind === "keyword" ? `Filter Page Index by keyword ${node.label}` : `Open note ${node.label}`);
    if (categoryMode) {
      const circle = document.createElementNS(NS, "circle");
      circle.setAttribute("r", String(getNodeRadius(node)));
      group.appendChild(circle);
      const title = document.createElementNS(NS, "title");
      title.textContent = `${node.label}: ${node.description || ""}`;
      group.appendChild(title);
      const label = document.createElementNS(NS, "text");
      label.setAttribute("class", "graph-node-label--article-categories");
      label.setAttribute("text-anchor", "middle");
      node.labelLines.forEach((text, index) => {
        const span = document.createElementNS(NS, "tspan");
        span.textContent = text;
        span.setAttribute("x", "0");
        span.setAttribute("y", String(node.radius + 18 + index * 16));
        label.appendChild(span);
      });
      const count = document.createElementNS(NS, "tspan");
      count.textContent = `${node.weight} articles`;
      count.setAttribute("x", "0"); count.setAttribute("y", String(node.radius + node.labelLines.length * 16 + 22));
      count.setAttribute("class", "category-map-count");
      label.appendChild(count);
      group.appendChild(label);
    } else {
      const circle = document.createElementNS(NS, "circle");
      circle.setAttribute("r", String(getNodeRadius(node)));
      circle.setAttribute("fill", GRAPH_COLORS[node.type] || GRAPH_COLORS.topic);
      circle.setAttribute("stroke", "#0d1411"); circle.setAttribute("stroke-width", "1");
      const label = document.createElementNS(NS, "text");
      const rightAligned = graphData.mode === "notes" && node.x > width / 2;
      const availableWidth = rightAligned ? node.x - 24 : width - node.x - 24;
      const maxLabelLength = Math.max(8, Math.floor(availableWidth / 6));
      label.textContent = graphData.mode === "notes" && node.label.length > maxLabelLength
        ? `${node.label.slice(0, Math.max(5, maxLabelLength - 1))}…`
        : node.label;
      label.setAttribute("x", rightAligned ? "-10" : "10");
      label.setAttribute("y", "4");
      if (rightAligned) label.setAttribute("text-anchor", "end");
      group.append(circle, label);
      if (graphData.mode === "notes") {
        const title = document.createElementNS(NS, "title");
        title.textContent = node.label;
        group.appendChild(title);
      }
    }
    nodeGroup.appendChild(group);

    const activateNode = () => {
      if (graphData.mode === "article-categories") {
        lineEls.forEach((line, index) => {
          line.setAttribute("aria-pressed", "false");
          line.classList.remove("is-highlighted");
          edgeTargetEls[index].setAttribute("aria-pressed", "false");
          edgeLabelEls[index].style.display = "none";
        });
        activateArticleCategoryEvidence(node.label, node.articles);
        return;
      }
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
      if (categoryMode) return;
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
      if (categoryMode) {
        lineEls[index].setAttribute("d", categoryRoutes[index].path);
        edgeTargetEls[index].setAttribute("cx", categoryRoutes[index].hit.x);
        edgeTargetEls[index].setAttribute("cy", categoryRoutes[index].hit.y);
      } else {
        lineEls[index].setAttribute("x1", edge.source.x);
        lineEls[index].setAttribute("y1", edge.source.y);
        lineEls[index].setAttribute("x2", edge.target.x);
        lineEls[index].setAttribute("y2", edge.target.y);
      }
    });

    nodes.forEach((node, index) => {
      nodeEls[index].setAttribute("transform", `translate(${node.x},${node.y})`);

    });
    links.forEach((edge, index) => {
      if (edgeLabelEls[index]) {
        edgeLabelEls[index].setAttribute("x", String(Math.max(16, Math.min(width - 16, categoryRoutes[index].hit.x))));
        edgeLabelEls[index].setAttribute("y", String(Math.max(18, categoryRoutes[index].hit.y - 5)));
      }
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

  state.graph = { nodes, nodeEls, links, linkEls: lineEls };
  updateGraphSelection();
  draw();
  if (staticLayout) {
    return;
  }
  startAnimation();
}

function activateArticleCategoryEvidence(label, articles) {
  if (state.graphMode !== "article-categories" || state.articleCategoryGraphLoading) return;
  showArticleCategoryEvidence(label, articles);
}

function showArticleCategoryEvidence(label, articles) {
  if (!els.articleCategoryEvidence) return;
  const section = els.articleCategoryEvidence;
  section.replaceChildren();
  section.hidden = false;
  section.append(registryElement("h3", "", `Supporting articles: ${label}`));
  const list = registryElement("ul", "article-category-evidence__list");
  [...articles].sort((a, b) => (a.title || "").localeCompare(b.title || "")).forEach((article) => {
    const item = registryElement("li", "article-category-evidence__item");
    const button = registryElement("button", "registry-article-link", article.title || article.canonical_url);
    button.type = "button";
    button.dataset.articleId = article.article_id;
    button.dataset.articleSource = "registry";
    const provenance = article.metadata_provenance?.categories;
    const version = article.content_version_id;
    const articleVersion = article.current_version_id || article.record_reference?.version_id;
    const metadataRecord = article.published_candidate_sha256;
    const sourceKind = article.source_kind === "pdf" ? "pdf" : "registry";
    button.dataset.articleSource = sourceKind;
    item.append(button, registryElement("p", "muted", `${article.publisher || article.source || "Publisher unavailable"}${provenance ? ` · ${provenance}` : ""}${version ? ` · content version ${version}` : articleVersion ? ` · article version ${articleVersion}` : metadataRecord ? ` · approved candidate ${metadataRecord.slice(0, 12)}` : ""}`));
    list.append(item);
  });
  section.append(list);
  section.focus?.();
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

function clearContext() {
  state.activeContextPath = null;
  state.confirmedContextPath = null;
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

  if (state.activeContextPath !== path) {
    state.confirmedContextPath = null;
  }
  state.activeContextPath = path;
  renderChatContext();
  renderRows();
  renderDetail(path);
  updateGraphSelection();

  if (options.switchView) {
    beginEvidenceNavigation();
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
  state.documents = (config.documents || []).map((doc) => ({ ...doc, status: doc.status || "-" }));

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
  owner_agent_verified_source: {
    sourceLabel: "Owner-agent verified source",
    provenanceCopy: "Source-supported public summary, independently reviewed",
  },
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
  if (state.registry.available && state.registry.mode === "articles") {
    void loadRegistryPublishers();
    if (!state.registry.articlePagination) {
      void loadRegistryArticles();
    }
  }
}

function appendInformationCheck(container, item, concise = false) {
  const labels = { unchecked: "Pending check", accessible: "URL accessible", unavailable: "URL unavailable",
    failed: "URL check failed", partial: "Partially checked", conflict: "Conflicting information", verified: "Verified / collected" };
  container.append(registryElement("p", "registry-card__meta", [
    item.origin === "web_collection" ? (item.pdf_observations?.length ? "Web collection · PDF import" : "Web collection") : "PDF import",
    labels[item.access_status] || "URL unchecked", labels[item.verification_status] || "Pending check",
    item.owner_agent_verification ? "Owner-agent source check · Independently reviewed public scope" : "",
    item.checked_at ? `Checked ${item.checked_at}` : "",
  ].filter(Boolean).join(" · ")));
  if (concise || !item.checks?.length) return;
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
    const sourceUrl = item.owner_agent_verification?.final_url || item.collected_candidate_source_url || sourceCheck?.source_url || (!candidate ? item.source_urls?.[0] : "");
    locations.push({ source: `Website${sourceUrl ? ` · ${sourceUrl}` : ""}`, value: webLocation });
  }
  if (!locations.length && item.location) locations.push({ source: "Source", value: item.location });
  if (!locations.length) locations.push({ source: item.origin === "web_collection" ? "Website" : "PDF", value: "Location not provided" });
  locations.forEach(({ source, value }) => {
    container.append(registryElement("dt", "", "Location"), registryElement("dd", "meeting-entry__location", `${value}\n${source}`));
  });
}

async function loadRegistryMeetings({ focusId = "", focusName = "" } = {}) {
  const sequence = ++state.registry.meetingRequestSequence;
  renderRegistryNotice(els.registryMeetings, "Loading meetings…");
  const params = new URLSearchParams({ page: String(state.registry.meetingPage), page_size: "20" });
  const query = focusName || els.registryMeetingSearch.value.trim();
  if (query) params.set("query", query);
  if (focusId || focusName) {
    params.set("base_date", "1900-01-01");
    params.set("include_unknown", "true");
  }
  if (els.registryMeetingVerification.value) params.set("verification_status", els.registryMeetingVerification.value);
  try {
    let payload = await registryFetch(`/api/registry/meetings?${params}`);
    while (focusId && !payload.items.some((item) =>
      (item.canonical_event_id || item.event_id || item.occurrence_id || "") === focusId) &&
      Number(params.get("page")) < payload.pagination.pages) {
      params.set("page", String(Number(params.get("page")) + 1));
      payload = await registryFetch(`/api/registry/meetings?${params}`);
    }
    if (sequence !== state.registry.meetingRequestSequence) return;
    state.registry.meetingPage = Number(params.get("page"));
    state.registry.meetingPagination = payload.pagination;
    els.registryMeetings.replaceChildren();
    const focusedMeeting = focusId
      ? payload.items.find((item) => (item.canonical_event_id || item.event_id || item.occurrence_id || "") === focusId)
      : focusName
        ? payload.items.filter((item) => item.name === focusName).length === 1
          ? payload.items.find((item) => item.name === focusName)
          : payload.pagination.total === 1 ? payload.items[0] : null
        : null;
    const counts = payload.verification_counts || {};
    els.registryMeetingCounts.textContent = focusId || focusName
      ? "Selected evidence record"
      : `As of ${payload.base_date} (UTC) · ${payload.pagination.total} current / upcoming · ${counts.verified || 0} verified · ` +
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
        card.dataset.meetingId = item.canonical_event_id || item.event_id || item.occurrence_id || "";
        const summary = registryElement("summary", "meeting-entry__summary");
        summary.append(registryElement("span", "meeting-entry__name", item.name),
          registryElement("span", "meeting-entry__institution", meetingInstitution(item)),
          registryElement("span", "meeting-entry__location-short", meetingLocationSummary(item)));
        card.append(summary);
        const body = registryElement("div", "meeting-entry__body");
        const fields = registryElement("dl", "detail-meta");
        const partialDateText = item.date_precision && item.date_precision !== "day" ? item.raw_time_text : "";
        const values = { "Date(s)": item.raw_date || partialDateText || [item.start_date, item.end_date].filter(Boolean).join(" through ") || item.deadline_date,
          "Time": item.raw_time_text === partialDateText ? "" : item.raw_time_text,
          "Timezone": item.event_timezone || item.timezone, "Host": item.organizer,
          "Status": item.status, "Relevance": item.relevance || item.relevance_reason,
          "Deadline": item.deadline_date ? `${item.deadline_type || "Deadline"}: ${item.deadline_date}` : "" };
        Object.entries(values).forEach(([label, value]) => {
          if (value) fields.append(registryElement("dt", "", label), registryElement("dd", "", value));
        });
        appendMeetingLocations(fields, item);
        body.append(fields);
        const checkedSourceUrl = reviewedCheckedSourceUrl(item);
        const originalSourceUrl = safeSourceUrl(item.original_source_url || item.owner_agent_verification?.source_url);
        const urls = [...new Set([...(item.source_urls || []), ...(item.sources || []).map((source) => source.source_url), item.online_url])];
        urls.forEach((url) => {
          const safe = safeSourceUrl(url);
          if (!safe) return;
          const label = checkedSourceUrl && safe === checkedSourceUrl ? "Open checked source"
            : checkedSourceUrl && originalSourceUrl && safe === originalSourceUrl ? "Original source link (provenance)" : url;
          const link = registryElement("a", "registry-source-link", label);
          link.href = safe; link.target = "_blank"; link.rel = "noopener noreferrer";
          body.append(link);
        });
        appendInformationCheck(body, item, true);
        card.append(body);
        if (item === focusedMeeting) {
          card.open = true;
          requestAnimationFrame(() => card.scrollIntoView({ block: "center", behavior: "smooth" }));
        }
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

function openMeetingEvidence(meetingId, meetingName, { updateHash = true } = {}) {
  beginEvidenceNavigation();
  if (updateHash) {
    const value = meetingId ? `id:${meetingId}${meetingName ? `|name:${meetingName}` : ""}` : `name:${meetingName}`;
    const hash = `#meeting=${encodeURIComponent(value)}`;
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
  }
  els.registryMeetingSearch.value = meetingName || "";
  els.registryMeetingVerification.value = "";
  setWorkspaceView("meetingsView");
  state.registry.meetingPage = 1;
  void loadRegistryMeetings({ focusId: meetingId, focusName: meetingName });
}

function openRegistryArticleEvidence(articleId, articleSource = "registry", { updateHash = true, fallbackSourceUrl = "", fallbackSourceIsChecked = false, originalSourceUrl = "" } = {}) {
  if (!articleId) return;
  beginEvidenceNavigation();
  if (updateHash) {
    const safeFallbackUrl = safeSourceUrl(fallbackSourceUrl);
    const safeOriginalUrl = safeSourceUrl(originalSourceUrl);
    const checkedSource = fallbackSourceIsChecked && safeFallbackUrl;
    const hash = `#registry-article=${encodeURIComponent(articleSource)}:${encodeURIComponent(articleId)}${safeFallbackUrl ? `&url=${encodeURIComponent(safeFallbackUrl)}` : ""}${checkedSource ? `&checked=1${safeOriginalUrl && safeOriginalUrl !== safeFallbackUrl ? `&original=${encodeURIComponent(safeOriginalUrl)}` : ""}` : ""}`;
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
  }
  if (state.activeView !== "registryView") setWorkspaceView("registryView");
  setRegistryMode("articles");
  void loadRegistryArticle(articleId, articleSource, fallbackSourceUrl, fallbackSourceIsChecked, originalSourceUrl);
}

async function openPdfObservationEvidence(occurrenceId, { search = "", url = "", snippet = "", updateHash = true,
  fallbackSourceIsChecked = false, originalSourceUrl = "" } = {}) {
  if (!/^[A-Za-z0-9_-]+$/.test(occurrenceId || "")) return;
  const navigationSequence = beginEvidenceNavigation();
  if (updateHash) {
    const params = new URLSearchParams({ search, url: safeSourceUrl(url), snippet: (snippet || "").slice(0, 100) });
    if (fallbackSourceIsChecked && safeSourceUrl(url)) {
      params.set("checked", safeSourceUrl(url));
      if (safeSourceUrl(originalSourceUrl)) params.set("original", safeSourceUrl(originalSourceUrl));
    }
    const hash = `#registry-pdf-occurrence=${encodeURIComponent(occurrenceId)}&${params}`;
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
  }
  if (state.activeView !== "registryView") setWorkspaceView("registryView");
  setRegistryMode("articles");
  showMobileDetail(els.registryArticlesPanel);
  resetRegistryArticleDetail("Loading PDF observation…", url, fallbackSourceIsChecked);
  try {
    const queries = [...new Set([search, url, (snippet || "").slice(0, 100)].map((value) => String(value || "").trim()).filter((value) => value.length >= 4))].slice(0, 3);
    const candidateIds = new Set();
    const checkedIds = new Set();
    for (const query of [...queries, ""]) {
      for (let page = 1; page <= 10; page += 1) {
        const params = new URLSearchParams({ page: String(page), page_size: "100", include_linked: "true" });
        if (query) params.set("query", query.slice(0, 200));
        const payload = await registryFetch(`/api/registry/pdf-intake/articles?${params}`);
        if (navigationSequence !== state.registry.evidenceNavigationSequence) return;
        for (const item of payload.items || []) {
          if (item.latest_occurrence?.occurrence_id === occurrenceId) {
            const roles = reviewedSourceRoles(item.latest_occurrence) || reviewedSourceRoles(item);
            const checkedUrl = roles?.checkedSourceUrl || (fallbackSourceIsChecked ? safeSourceUrl(url) : "");
            const originalUrl = roles?.originalSourceUrl || (fallbackSourceIsChecked ? safeSourceUrl(originalSourceUrl) : "");
            if (checkedUrl) await loadRegistryArticle(item.article_id, "pdf", checkedUrl, true, originalUrl);
            else await loadRegistryArticle(item.article_id, "pdf", url);
            return;
          }
          if (item.article_id) candidateIds.add(item.article_id);
        }
        if (page >= payload.pagination.pages) break;
      }
      for (const articleId of candidateIds) {
        if (checkedIds.has(articleId)) continue;
        checkedIds.add(articleId);
        let article;
        try {
          article = await registryFetch(`/api/registry/pdf-intake/articles/${encodeURIComponent(articleId)}`);
        } catch (error) {
          if (error.status === 404) continue;
          throw error;
        }
        if (navigationSequence !== state.registry.evidenceNavigationSequence) return;
        if ((article.occurrences || []).some((occurrence) => occurrence.occurrence_id === occurrenceId)) {
          const occurrence = article.occurrences.find((item) => item.occurrence_id === occurrenceId);
          const roles = reviewedSourceRoles(occurrence) || reviewedSourceRoles(article);
          const checkedUrl = roles?.checkedSourceUrl || (fallbackSourceIsChecked ? safeSourceUrl(url) : "");
          const originalUrl = roles?.originalSourceUrl || (fallbackSourceIsChecked ? safeSourceUrl(originalSourceUrl) : "");
          if (checkedUrl) await loadRegistryArticle(articleId, "pdf", checkedUrl, true, originalUrl);
          else await loadRegistryArticle(articleId, "pdf", url);
          return;
        }
      }
    }
    if (navigationSequence === state.registry.evidenceNavigationSequence) {
      els.registryArticleTitle.textContent = "PDF observation unavailable";
      els.registryEnrichment.textContent = "This PDF observation is not available in the public Registry.";
      if (fallbackSourceIsChecked && safeSourceUrl(originalSourceUrl) !== safeSourceUrl(url)) {
        const provenance = registryElement("p", "muted registry-provenance", "Original PDF/source link (provenance): ");
        const link = registryElement("a", "registry-source-link", "Open original source");
        link.href = safeSourceUrl(originalSourceUrl); link.target = "_blank"; link.rel = "noopener noreferrer";
        provenance.append(link); els.registryEnrichment.append(provenance);
      }
      els.registryEnrichment.hidden = false;
      els.registryArticleDetail.setAttribute("aria-busy", "false");
    }
  } catch (error) {
    if (navigationSequence === state.registry.evidenceNavigationSequence) {
      els.registryArticleTitle.textContent = "Article unavailable";
      els.registryEnrichment.textContent = registryErrorMessage(error);
      if (fallbackSourceIsChecked && safeSourceUrl(originalSourceUrl) !== safeSourceUrl(url)) {
        const provenance = registryElement("p", "muted registry-provenance", "Original PDF/source link (provenance): ");
        const link = registryElement("a", "registry-source-link", "Open original source");
        link.href = safeSourceUrl(originalSourceUrl); link.target = "_blank"; link.rel = "noopener noreferrer";
        provenance.append(link); els.registryEnrichment.append(provenance);
      }
      els.registryEnrichment.hidden = false;
      els.registryArticleDetail.setAttribute("aria-busy", "false");
    }
  }
}

function comparableSourceUrl(value) {
  const safe = safeSourceUrl(value);
  if (!safe) return "";
  const url = new URL(safe);
  url.hash = "";
  if (url.pathname.length > 1) url.pathname = url.pathname.replace(/\/$/, "");
  return url.href;
}

async function openWebEvidence(url, { updateHash = true, fallbackSourceIsChecked = false,
  checkedSourceUrl = "", originalSourceUrl = "" } = {}) {
  const safe = safeSourceUrl(url);
  if (!safe) return;
  const lookup = comparableSourceUrl(safe);
  const navigationSequence = beginEvidenceNavigation();
  if (updateHash) {
    const checked = fallbackSourceIsChecked ? safeSourceUrl(checkedSourceUrl || safe) : "";
    const original = safeSourceUrl(originalSourceUrl);
    const hash = `#registry-article-url=${encodeURIComponent(safe)}${checked ? `&checked=${encodeURIComponent(checked)}&original=${encodeURIComponent(original || safe)}` : ""}`;
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
  }
  if (state.activeView !== "registryView") setWorkspaceView("registryView");
  setRegistryMode("articles");
  try {
    for (let page = 1; page <= 10; page += 1) {
      const params = new URLSearchParams({ query: lookup, include_pdf: "true", page: String(page), page_size: "100" });
      const payload = await registryFetch(`/api/registry/articles?${params}`);
      if (navigationSequence !== state.registry.evidenceNavigationSequence) return;
      const match = payload.items.find((article) => comparableSourceUrl(article.canonical_url) === comparableSourceUrl(safe));
      if (match) {
        const roles = reviewedSourceRoles(match);
        const checkedUrl = roles?.checkedSourceUrl || (fallbackSourceIsChecked ? safeSourceUrl(checkedSourceUrl || safe) : "");
        const original = roles?.originalSourceUrl || (fallbackSourceIsChecked ? safeSourceUrl(originalSourceUrl) : "");
        if (updateHash && roles) {
          const hash = `#registry-article-url=${encodeURIComponent(safe)}&checked=${encodeURIComponent(checkedUrl)}&original=${encodeURIComponent(original)}`;
          if (window.location.hash !== hash) window.history.pushState(null, "", hash);
        }
        if (checkedUrl) await loadRegistryArticle(match.article_id, match.source_kind === "pdf" ? "pdf" : "registry",
          checkedUrl, true, original);
        else await loadRegistryArticle(match.article_id, match.source_kind === "pdf" ? "pdf" : "registry", safe);
        return;
      }
      if (page >= payload.pagination.pages) break;
    }
  } catch {
    // An unreadable Registry does not change the original web evidence.
  }
  if (navigationSequence === state.registry.evidenceNavigationSequence) {
    window.location.assign(fallbackSourceIsChecked ? safeSourceUrl(checkedSourceUrl) || safe : safe);
  }
}

async function loadRegistryPublishers() {
  if (!els.registryPublisherFilter) {
    return;
  }
  if (state.registry.publishersLoaded) {
    return;
  }
  if (state.registry.publishersPromise) {
    return state.registry.publishersPromise;
  }
  const currentValue = els.registryPublisherFilter.value;
  const allPublishers = registryElement("option", "", "All publishers");
  allPublishers.value = "";
  state.registry.publishersPromise = (async () => {
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
      state.registry.publishersLoaded = true;
    } catch {
      els.registryPublisherFilter.replaceChildren(allPublishers);
      if (els.registryPublisherCustom) {
        els.registryPublisherCustom.hidden = true;
        els.registryPublisherCustom.value = "";
      }
    } finally {
      state.registry.publishersPromise = null;
    }
  })();
  return state.registry.publishersPromise;
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
  state.registry.available = false;
  let reportsLoaded = false;
  try {
    reportsLoaded = await loadRegistryReports();
    if (reportsLoaded) {
      const reportCount = state.registry.reportPagination?.total ?? 0;
      els.registryStatus.textContent = `${reportCount} reports`;
    }
    const status = await registryFetch("/api/registry/status?include_pdf=true");
    state.registry.loaded = reportsLoaded;
    state.registry.available = Boolean(status.available);
    if (!status.available) {
      els.registryStatus.textContent = reportsLoaded
        ? `${state.registry.reportPagination?.total ?? 0} reports · Registry unavailable`
        : "Registry: unavailable";
      renderRegistryNotice(els.registryArticles, "Article history will appear when the registry is connected.");
      updateRegistryPagination("articles", null);
      return;
    }
    els.registryStatus.textContent = `${status.reports} reports · ${status.articles} articles`;
    if (state.registry.mode === "articles") {
      void loadRegistryPublishers();
      if (!state.registry.articlePagination) {
        void loadRegistryArticles();
      }
    }
  } catch (error) {
    state.registry.loaded = false;
    state.registry.available = false;
    els.registryStatus.textContent = reportsLoaded
      ? `${state.registry.reportPagination?.total ?? 0} reports`
      : "Registry: unavailable";
    if (state.registry.mode === "articles") {
      renderRegistryNotice(els.registryArticles, registryErrorMessage(error));
    }
  }
}

function clearRegistryPageCache() {
  state.registry.listPages.clear();
  state.registry.listVersion = null;
}

async function registryPageFetch(path, prefetch = false) {
  const pages = state.registry.listPages;
  const cached = pages.get(path);
  if (cached) {
    const payload = cached.payload || await cached.promise;
    if (prefetch) return payload;
    try {
      const status = await registryFetch("/api/registry/status?include_pdf=true");
      if (status.available && status.cache_version && status.cache_version === cached.version
          && cached.version === state.registry.listVersion && pages.get(path) === cached) return payload;
      if (pages.get(path) === cached && cached.version === state.registry.listVersion) clearRegistryPageCache();
    } catch (error) {
      if (pages.get(path) === cached && cached.version === state.registry.listVersion) clearRegistryPageCache();
      throw error;
    }
  }
  const entry = { sequence: ++state.registry.listFetchSequence };
  entry.promise = registryFetch(path).then((payload) => {
    if (payload.cache_version && state.registry.listVersion !== payload.cache_version
        && entry.sequence >= state.registry.listVersionSequence) {
      clearRegistryPageCache();
      state.registry.listVersion = payload.cache_version;
      state.registry.listVersionSequence = entry.sequence;
    }
    entry.payload = payload;
    entry.version = payload.cache_version;
    if (payload.cache_version && payload.cache_version === state.registry.listVersion) pages.set(path, entry);
    else if (pages.get(path) === entry) pages.delete(path);
    return payload;
  }).catch((error) => {
    if (pages.get(path) === entry) pages.delete(path);
    if (error.status === 503 && entry.sequence >= state.registry.listVersionSequence) clearRegistryPageCache();
    throw error;
  });
  pages.set(path, entry);
  return entry.promise;
}

function prefetchRegistryPage(path, pagination) {
  if (!pagination || pagination.page >= pagination.pages) return;
  const next = path.replace(/([?&]page=)\d+/, `$1${pagination.page + 1}`);
  registryPageFetch(next, true).catch(() => {});
}

async function loadRegistryReports() {
  renderRegistryNotice(els.registryReports, "Loading reports…");
  const sequence = ++state.registry.reportListRequestSequence;
  const path = `/api/registry/reports?include_pdf=true&page=${state.registry.reportPage}&page_size=12`;
  try {
    const payload = await registryPageFetch(path);
    if (sequence !== state.registry.reportListRequestSequence) return false;
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
    prefetchRegistryPage(path, payload.pagination);
    return true;
  } catch (error) {
    if (sequence !== state.registry.reportListRequestSequence) return false;
    renderRegistryNotice(els.registryReports, registryErrorMessage(error));
    updateRegistryPagination("reports", null);
    return false;
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

async function loadRegistryReport(reportDate, fallbackSourceUrl = "", fallbackSourceIsChecked = false, originalSourceUrl = "") {
  showMobileDetail(els.registryReportsPanel);
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
      const roles = reviewedSourceRoles(article);
      if (roles) {
        button.dataset.sourceUrl = roles.checkedSourceUrl;
        button.dataset.sourceChecked = "true";
        button.dataset.originalUrl = roles.originalSourceUrl;
      }
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
      const information = registryElement("details", "registry-report-info");
      information.append(registryElement("summary", "", "Report information"), meta);
      item.append(button);
      if (summaryPresentation.text) {
        item.append(registryElement("p", "registry-card__summary", summaryPresentation.text));
      }
      item.append(information);
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
      const fallbackSource = safeReportSourceUrl(fallbackSourceUrl);
      if (fallbackSource) {
        const link = registryElement("a", "registry-source-link", fallbackSourceIsChecked ? "Open checked source" : "Open original source");
        link.href = fallbackSource;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        const nodes = [registryElement("span", "", registryErrorMessage(error)), link];
        const original = safeSourceUrl(originalSourceUrl);
        if (fallbackSourceIsChecked && original && original !== fallbackSource) {
          const provenance = registryElement("span", "muted registry-provenance", "Original source link (provenance): ");
          const originalLink = registryElement("a", "registry-source-link", "Open original source");
          originalLink.href = original; originalLink.target = "_blank"; originalLink.rel = "noopener noreferrer";
          provenance.append(originalLink); nodes.push(provenance);
        }
        els.registryReportMeta.replaceChildren(...nodes);
      } else {
        els.registryReportMeta.textContent = registryErrorMessage(error);
      }
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
    const path = `/api/registry/articles?${params.toString()}`;
    const payload = await registryPageFetch(path);
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
      const roles = reviewedSourceRoles(article);
      if (roles) {
        button.dataset.sourceUrl = roles.checkedSourceUrl;
        button.dataset.sourceChecked = "true";
        button.dataset.originalUrl = roles.originalSourceUrl;
      }
      button.append(
        registryElement("strong", "registry-card__title", article.title || article.canonical_url),
        registryElement(
          "span",
          "registry-card__meta",
          pdfSource
            ? `${article.publisher} · PDF import · ${article.occurrence_count} PDF ${article.occurrence_count === 1 ? "mention" : "mentions"} · ${article.publication_date ? `Published ${article.publication_date}` : article.collected_at ? `Collected ${article.collected_at.slice(0, 10)}` : "Date unknown"}`
            : `${article.publisher} · ${article.source_label || "Registry"} · ${article.publication_date ? `Published ${article.publication_date}` : article.collected_at ? `Collected ${article.collected_at.slice(0, 10)}` : "Date unknown"}${article.pdf_occurrence_count ? ` · ${article.pdf_occurrence_count} PDF ${article.pdf_occurrence_count === 1 ? "mention" : "mentions"}` : ""}`,
        ),
      );
      els.registryArticles.append(button);
    });
    updateRegistryPagination("articles", payload.pagination);
    prefetchRegistryPage(path, payload.pagination);
  } catch (error) {
    if (requestSequence !== state.registry.articleRequestSequence) {
      return;
    }
    renderRegistryNotice(els.registryArticles, registryErrorMessage(error));
    updateRegistryPagination("articles", null);
  }
}

function appendPdfAppearances(container, occurrences) {
  if (!Array.isArray(occurrences)) return;
  occurrences.forEach((occurrence) => {
    const filename = occurrence.source_filename || occurrence.source_document ||
      occurrence.source_observations?.[0]?.filename || "";
    const item = registryElement("li", "registry-appearance");
    const documentRef = [filename, occurrence.page ? `page ${occurrence.page}` : ""].filter(Boolean).join(" · ");
    if (documentRef) item.append(registryElement("strong", "", documentRef));
    const appearanceMeta = [
      occurrence.report_date || occurrence.publication_date,
      occurrence.verification_status ? `Verification: ${occurrence.verification_status}` : "",
    ].filter(Boolean).join(" · ");
    if (appearanceMeta) item.append(registryElement("span", "registry-card__meta", appearanceMeta));
    const checkedSourceUrl = reviewedCheckedSourceUrl(occurrence);
    const originalUrl = safeSourceUrl(occurrence.raw_url);
    const sourceUrl = safeSourceUrl(checkedSourceUrl || originalUrl);
    if (sourceUrl) {
      const link = registryElement("a", "registry-source-link", checkedSourceUrl ? "Open checked source" : "Open original source");
      link.href = sourceUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
      if (checkedSourceUrl && originalUrl && checkedSourceUrl !== originalUrl) {
        const original = registryElement("a", "registry-source-link", "Open original source");
        original.href = originalUrl;
        original.target = "_blank";
        original.rel = "noopener noreferrer";
        item.append(original);
      }
    }
    container.append(item);
  });
}

async function loadImportedRegistryReport(documentId, page = 1) {
  showMobileDetail(els.registryReportsPanel);
  const requestToken = ++state.registry.reportRequestSequence;
  state.registry.selectedReportDate = documentId;
  const isCurrent = () => requestToken === state.registry.reportRequestSequence;
  clearHistoricalReportContent();
  els.registryReportDetail.setAttribute("aria-busy", "true");
  els.registryReportTitle.textContent = "Loading imported report…";
  els.registryReportMeta.textContent = "Loading PDF details…";
  try {
    const report = await registryFetch(`/api/registry/pdf-intake/reports/${encodeURIComponent(documentId)}?page=${page}&page_size=5`);
    if (!isCurrent()) return;
    els.registryReportTitle.textContent = report.report_title;
    els.registryReportMeta.textContent = [report.report_date || "Report date unavailable", "PDF import", report.filename].join(" · ");
    const details = els.registryImportedReport;
    const content = registryElement("div", "registry-imported-content");
    details.append(content);
    const metadata = registryElement("dl", "detail-meta");
    const fields = {"Report date": report.report_date, "Reporting period": report.reporting_period,
      "Pages": report.page_count, "Imported": report.imported_at};
    Object.entries(fields).forEach(([label, value]) => {
      if (value != null && value !== "") metadata.append(registryMetric(label, value));
    });
    Object.entries(report.pdf_metadata || {}).forEach(([label, value]) => {
      if (!["/CreationDate", "/Creator", "/ModDate", "/Producer"].includes(label)) {
        metadata.append(registryMetric(label.replace(/^\//, ""), value));
      }
    });
    const information = registryElement("details", "registry-report-info");
    information.append(registryElement("summary", "", "Report information"), metadata);
    content.append(information);
    if ((report.pagination?.page ?? page) === 1 && report.executive_summary?.length) {
      const executive = registryElement("details", "registry-imported-summary meeting-entry");
      executive.append(registryElement("summary", "meeting-entry__summary", "Executive Summary"));
      const body = registryElement("div", "meeting-entry__body");
      report.executive_summary.forEach((summary) => body.append(registryElement("p", "", summary)));
      if (report.executive_summary_label) {
        body.append(registryElement("p", "muted registry-provenance", report.executive_summary_label));
      }
      executive.append(body);
      content.append(executive);
    }
    const modules = registryElement("div", "registry-imported-modules");
    content.append(modules);
    if (report.articles?.length) modules.append(registryElement("h4", "", "Articles"));
    (report.articles || []).forEach((article, index) => {
      const block = registryElement("section", "registry-detail-section");
      const button = registryElement("button", "registry-article-link", `${(report.article_offset || 0) + index + 1}. ${article.title || article.canonical_url}`);
      button.type = "button";
      const reference = article.metadata_only ? article.verified_primary_reference : null;
      button.dataset.articleId = reference?.entity_id || article.article_id;
      button.dataset.articleSource = reference?.entity_kind === "article" ? "registry" : "pdf";
      const roles = reviewedSourceRoles(article);
      if (roles) {
        button.dataset.sourceUrl = roles.checkedSourceUrl;
        button.dataset.sourceChecked = "true";
        button.dataset.originalUrl = roles.originalSourceUrl;
      }
      block.append(button);
      appendRegistryPdfOccurrences(block, article);
      modules.append(block);
    });
    if (report.calendar_items?.length) modules.append(registryElement("h4", "", "Meetings & Key Dates"));
    (report.calendar_items || []).forEach((item) => {
      if (item.metadata_only) {
        const block = registryElement("section", "registry-detail-section");
        const reference = item.verified_primary_display || {};
        const link = registryElement("a", "registry-source-link", reference.name || "Verified meeting reference");
        const safe = safeSourceUrl(reference.source_url);
        if (safe) { link.href = safe; link.target = "_blank"; link.rel = "noopener noreferrer"; }
        block.append(link, registryElement("p", "", "Reference to a verified meeting; this report observation is unverified."),
          registryElement("p", "", [item.source_filename || report.filename, item.page ? `page ${item.page}` : ""].filter(Boolean).join(" · ")));
        modules.append(block);
        return;
      }
      const block = registryElement("details", "meeting-entry");
      const summary = registryElement("summary", "meeting-entry__summary");
      summary.append(registryElement("span", "meeting-entry__name", item.name || "Calendar item"),
        registryElement("span", "meeting-entry__institution", item.organizer || item.publisher || ""),
        registryElement("span", "meeting-entry__location-short", item.location || ""));
      block.append(summary);
      const body = registryElement("div", "meeting-entry__body");
      const values = registryElement("dl", "detail-meta");
      Object.entries({ "Kind": item.kind,
        "Date(s)": item.raw_date || [item.start_date, item.end_date].filter(Boolean).join(" through ") || item.deadline_date,
        "Host": item.organizer || item.publisher, "Relevance": item.relevance || item.relevance_reason,
        "Location": item.location }).forEach(([label, value]) => {
        if (value) values.append(registryElement("dt", "", label), registryElement("dd", "", value));
      });
      body.append(values);
      [...new Set([...(item.source_urls || []), item.online_url])].forEach((url) => {
        const safe = safeSourceUrl(url);
        if (!safe) return;
        const link = registryElement("a", "registry-source-link", url);
        link.href = safe; link.target = "_blank"; link.rel = "noopener noreferrer";
        body.append(link);
      });
      block.append(body);
      modules.append(block);
    });
    if (report.pagination) {
      const pagination = registryElement("nav", "registry-pagination registry-pagination--pdf");
      pagination.setAttribute("aria-label", "PDF report pages");
      const { page: current, pages, total } = report.pagination;
      [["Previous", current - 1, current <= 1], ["Next", current + 1, current >= pages]].forEach(([label, next, disabled]) => {
        const button = registryElement("button", "ghost-button", label);
        button.type = "button";
        button.disabled = disabled;
        button.addEventListener("click", () => void loadImportedRegistryReport(documentId, next));
        pagination.append(button);
      });
      pagination.append(registryElement("span", "", `Page ${current} of ${pages || 1} · ${total} modules`));
      const jump = registryElement("form", "registry-page-jump");
      const pageInput = registryElement("input");
      pageInput.type = "number";
      pageInput.min = "1";
      pageInput.max = String(pages || 1);
      pageInput.step = "1";
      pageInput.required = true;
      pageInput.value = String(current);
      pageInput.setAttribute("aria-label", "Page number");
      const go = registryElement("button", "ghost-button", "Go to page");
      go.type = "submit";
      jump.append(pageInput, go);
      jump.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!pageInput.reportValidity()) return;
        const next = Number(pageInput.value);
        if (next !== current) void loadImportedRegistryReport(documentId, next);
      });
      pagination.append(jump);
      details.append(pagination);
    }
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

function appendRegistryPdfOccurrences(container, article) {
  if (typeof article?.summary === "string" && article.summary.trim()) {
    container.append(registryElement("p", "registry-pdf-article-summary", article.summary));
  }
  const occurrences = article?.occurrences;
  if (!Array.isArray(occurrences) || !occurrences.length) return;
  const block = registryElement("div", "registry-pdf-history");
  const list = registryElement("ul", "registry-pdf-history__list");
  occurrences.forEach((occurrence) => {
    const item = registryElement("li", "registry-pdf-history__item");
    const sourceName = occurrence.source_document || occurrence.source_observations?.[0]?.filename || "Imported PDF";
    const date = occurrence.report_date
      ? `Report date ${occurrence.report_date}`
      : occurrence.publication_date
        ? `Publication date ${occurrence.publication_date}`
        : "Date unavailable";
    const page = occurrence.page ? ` · page ${occurrence.page}` : "";
    const information = registryElement("details", "registry-report-info");
    information.append(
      registryElement("summary", "", "Report information"),
      registryElement("strong", "", `${sourceName} · ${date}${page}`),
    );
    appendInformationCheck(information, occurrence);
    const checkedSourceUrl = reviewedCheckedSourceUrl(occurrence);
    const originalSourceUrl = safeSourceUrl(occurrence.raw_url || occurrence.original_source_url);
    const sourceUrl = safeSourceUrl(checkedSourceUrl || originalSourceUrl);
    if (sourceUrl) {
      const link = registryElement("a", "registry-source-link", checkedSourceUrl ? "Open checked source" : "Open source link");
      link.href = sourceUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
      if (checkedSourceUrl && originalSourceUrl && checkedSourceUrl !== originalSourceUrl) {
        const original = registryElement("a", "registry-source-link", "Original PDF/source link (provenance)");
        original.href = originalSourceUrl; original.target = "_blank"; original.rel = "noopener noreferrer";
        item.append(original);
      }
    }
    item.append(information);
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

function resetRegistryArticleDetail(title, fallbackSourceUrl = "", fallbackSourceIsChecked = false) {
  els.registryArticleDetail.setAttribute("aria-busy", "true");
  els.registryArticleTitle.textContent = title;
  els.registryArticleMeta.replaceChildren();
  if (els.registryArticleInfo) {
    els.registryArticleInfo.hidden = true;
    els.registryArticleInfo.removeAttribute("open");
  }
  els.registryEnrichment.replaceChildren();
  els.registryEnrichment.hidden = true;
  els.registryAppearances.replaceChildren();
  els.registryAppearancesSection.hidden = false;
  els.registryContentSection.hidden = true;
  els.registryMarkdown.textContent = "";
  els.registryOriginalLink.hidden = true;
  els.registryOriginalLink.removeAttribute("href");
  const safeFallbackUrl = safeSourceUrl(fallbackSourceUrl);
  if (safeFallbackUrl) {
    els.registryOriginalLink.href = safeFallbackUrl;
    els.registryOriginalLink.textContent = fallbackSourceIsChecked ? "Open checked source" : "Open original source";
    els.registryOriginalLink.hidden = false;
  }
}

async function loadRegistryArticle(articleId, articleSource = "registry", fallbackSourceUrl = "",
  fallbackSourceIsChecked = false, originalSourceUrl = "") {
  showMobileDetail(els.registryArticlesPanel);
  state.registry.evidenceNavigationSequence += 1;
  const requestSequence = ++state.registry.articleDetailRequestSequence;
  const isCurrentRequest = () => requestSequence === state.registry.articleDetailRequestSequence;
  resetRegistryArticleDetail("Loading article…");
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
    const checkedSourceUrl = reviewedCheckedSourceUrl(article);
    const sourceUrl = safeSourceUrl(checkedSourceUrl ||
      (usesAlternatePublisherPage && article.source_annotation?.source_url) ||
        article.original_url ||
        article.canonical_url);
    if (sourceUrl) {
      els.registryOriginalLink.href = sourceUrl;
      els.registryOriginalLink.textContent = checkedSourceUrl
        ? "Open checked source"
        : pdfSource
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
    }
    if (article.information_date) {
      metrics.push(registryMetric("Information date", article.information_date));
    }
    if (article.publication_date) {
      metrics.push(registryMetric("Publication date", article.publication_date));
    }
    if (!article.publication_date && article.report_date) {
      metrics.push(registryMetric("Report date (publication unconfirmed)", article.report_date));
    }
    if (!collectedAt && !article.information_date && !article.publication_date && !article.report_date) {
      metrics.push(registryMetric("Date basis", "Unconfirmed"));
    }
    els.registryArticleMeta.append(...metrics);
    if (els.registryArticleInfo) els.registryArticleInfo.hidden = false;
    const summaryPresentation = registrySummaryPresentation(article);
    if (pdfSource && article.type_safe_classification?.label) {
      appendRegistryTags(els.registryEnrichment, "TypeSafe classification", [article.type_safe_classification.label]);
    }
    if (summaryPresentation.text) {
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
        article.categories === undefined ? article.enrichment?.categories || [] : article.categories,
      );
      appendRegistryTags(
        els.registryEnrichment,
        "Keywords",
        article.keywords?.length ? article.keywords : article.enrichment?.keywords || [],
      );
    } else {
      for (const [field, label] of [["categories", "Categories"], ["keywords", "Keywords"]]) {
        if (article.metadata_provenance?.[field] && article[field]?.length) {
          appendRegistryTags(els.registryEnrichment, label, article[field]);
        }
      }
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
    if (!pdfSource && !(article.pdf_occurrences || []).length) {
      const sourceRoles = reviewedSourceRoles(article);
      const originalSourceUrl = safeSourceUrl(sourceRoles?.originalSourceUrl);
      if (sourceRoles?.checkedSourceUrl && originalSourceUrl
        && originalSourceUrl !== sourceRoles.checkedSourceUrl) {
        const provenance = registryElement("p", "muted registry-provenance", "Original canonical/source link (provenance): ");
        const link = registryElement("a", "registry-source-link", "Open original source");
        link.href = originalSourceUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        provenance.append(link);
        els.registryEnrichment.append(provenance);
      }
    }
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
    appendPdfAppearances(els.registryAppearances, pdfSource ? article.occurrences : article.pdf_occurrences);
    if (!appearances.length) {
      if (!pdfSource && !article.pdf_occurrences?.length) {
        els.registryAppearances.append(
          registryElement("li", "registry-notice", "No report appearances are recorded for this article."),
        );
      } else if (pdfSource && !article.occurrences?.length) {
        els.registryAppearances.append(
          registryElement("li", "registry-notice", "No report appearances are recorded for this article."),
        );
      }
    }
    const displayText = article.content?.markdown || "";
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
    const fallbackSource = safeSourceUrl(fallbackSourceUrl);
    if (fallbackSource) {
      els.registryOriginalLink.href = fallbackSource;
      els.registryOriginalLink.textContent = fallbackSourceIsChecked ? "Open checked source" : "Open original source";
      els.registryOriginalLink.hidden = false;
    }
    els.registryEnrichment.append(registryElement("p", "registry-notice",
      `${registryErrorMessage(error)}${fallbackSource ? fallbackSourceIsChecked ? " The checked source is still available below." : " The original cited source is still available below." : ""}`));
    const safeOriginalUrl = safeSourceUrl(originalSourceUrl);
    if (fallbackSourceIsChecked && safeOriginalUrl && safeOriginalUrl !== fallbackSource) {
      const provenance = registryElement("p", "muted registry-provenance", "Original canonical/source link (provenance): ");
      const link = registryElement("a", "registry-source-link", "Open original source");
      link.href = safeOriginalUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      provenance.append(link);
      els.registryEnrichment.append(provenance);
    }
    els.registryEnrichment.hidden = false;
  } finally {
    if (isCurrentRequest()) {
      els.registryArticleDetail.setAttribute("aria-busy", "false");
    }
  }
}

function attachSourceCardClickHandler() {
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const sourceCard = target.closest("[data-source-navigation]");
    if (!sourceCard || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (sourceCard.dataset.sourceNavigation === "meeting") {
      openMeetingEvidence(sourceCard.dataset.meetingId || "", sourceCard.dataset.meetingName || "");
    } else if (sourceCard.dataset.sourceNavigation === "article") {
      openRegistryArticleEvidence(sourceCard.dataset.articleId, sourceCard.dataset.articleSource || "registry",
        { fallbackSourceUrl: sourceCard.dataset.sourceUrl || sourceCard.dataset.originalUrl || "",
          fallbackSourceIsChecked: sourceCard.dataset.sourceChecked === "true",
          originalSourceUrl: sourceCard.dataset.sourceChecked === "true" && sourceCard.dataset.sourceUrl !== sourceCard.dataset.originalUrl
            ? sourceCard.dataset.originalUrl || "" : "" });
    } else if (sourceCard.dataset.sourceNavigation === "article-url") {
      void openWebEvidence(sourceCard.dataset.sourceUrl, { fallbackSourceIsChecked: sourceCard.dataset.sourceChecked === "true",
        checkedSourceUrl: sourceCard.dataset.checkedUrl || "",
        originalSourceUrl: sourceCard.dataset.originalUrl || "" });
    } else if (sourceCard.dataset.sourceNavigation === "pdf-observation") {
      void openPdfObservationEvidence(sourceCard.dataset.occurrenceId, {
        search: sourceCard.dataset.observationSearch,
        url: sourceCard.dataset.observationUrl,
        snippet: sourceCard.dataset.observationSnippet,
        fallbackSourceIsChecked: sourceCard.dataset.sourceChecked === "true",
        originalSourceUrl: sourceCard.dataset.originalUrl || "",
      });
    } else if (sourceCard.dataset.sourceNavigation === "report") {
      openHistoricalReport(sourceCard.dataset.reportDate, { fallbackSourceUrl: sourceCard.dataset.reportSourceUrl || "",
        fallbackSourceIsChecked: sourceCard.dataset.sourceChecked === "true", originalSourceUrl: sourceCard.dataset.originalUrl || "" });
    }
  });
}

function attachEvents() {
  els.navMenuButton?.addEventListener("click", () => {
    const expanded = els.navMenuButton.getAttribute("aria-expanded") === "true";
    els.navMenuButton.setAttribute("aria-expanded", String(!expanded));
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".tabbar")) closeNavigation();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && els.navMenuButton?.getAttribute("aria-expanded") === "true") {
      closeNavigation();
      els.navMenuButton.focus();
    }
  });
  document.querySelectorAll(".mobile-back").forEach((button) => {
    button.addEventListener("click", () => closeMobileDetail(button.closest(".registry-grid")));
  });
  attachSourceCardClickHandler();
  els.jumpToReports?.addEventListener("click", () => {
    beginEvidenceNavigation();
    setWorkspaceView("registryView");
  });
  els.graphExpandButton?.addEventListener("click", toggleGraphExpanded);
  document.addEventListener("keydown", handleGraphKeydown);
  els.registryMeetingSearchForm?.addEventListener("submit", (event) => {
    event.preventDefault(); state.registry.meetingPage = 1; void loadRegistryMeetings();
  });
  els.registryMeetingVerification?.addEventListener("change", () => {
    state.registry.meetingPage = 1; void loadRegistryMeetings();
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
      if (blockedChatMessage()) return;
      els.input.value = "";
      sendMessage(message);
    });
  }

  els.chatTokenForm?.addEventListener("submit", continueWithToken);
  els.retryChatQuestion?.addEventListener("click", () => {
    const blocked = blockedChatMessage();
    if (blocked && !state.isSending) sendMessage(blocked.content, blocked.blockedRequest);
  });

  els.registryModeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      beginEvidenceNavigation();
      setRegistryMode(button.dataset.registryMode);
    });
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
      if (els.registryPublisherCustom) els.registryPublisherCustom.value = "";
      state.registry.articlePage = 1;
      void loadRegistryArticles();
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

  if (els.useInChat) {
    els.useInChat.addEventListener("click", () => {
      if (!state.activeContextPath) {
        return;
      }
      state.confirmedContextPath = state.activeContextPath;
      renderChatContext();
      beginEvidenceNavigation();
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

    const citation = target.closest("[data-citation-index]");
    if (citation) {
      const row = citation.closest(".message-row");
      const sources = row?.querySelector(".message-sources");
      const source = [...(sources?.querySelectorAll(".source-card") || [])]
        .find((card) => card.dataset.index === citation.dataset.citationIndex);
      if (sources && source) {
        sources.open = true;
        source.focus({ preventScroll: true });
        source.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      return;
    }

    const sourceCard = target.closest("[data-source-navigation]");
    if (sourceCard) {
      return;
    }

    const tab = target.closest(".tabbar__tab");
    if (tab) {
      beginEvidenceNavigation();
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
      beginEvidenceNavigation();
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
      if (state.activeView !== "registryView") setWorkspaceView("registryView");
      setRegistryMode("articles");
      const articleSource = articleCard.dataset.articleSource || "registry";
      if (articleCard.dataset.sourceChecked === "true") {
        void loadRegistryArticle(articleCard.dataset.articleId, articleSource, articleCard.dataset.sourceUrl || "",
          true, articleCard.dataset.originalUrl || "");
      } else {
        void loadRegistryArticle(articleCard.dataset.articleId, articleSource);
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

  window.addEventListener("resize", () => {
    if (state.graphExpanded || state.activeView === "obsidianView") renderCurrentGraph();
  });
  window.addEventListener("hashchange", () => {
    const sourceTarget = sourceNavigationFromHash();
    if (sourceTarget?.kind === "meeting") {
      openMeetingEvidence(sourceTarget.id, sourceTarget.name, { updateHash: false });
      return;
    }
    if (sourceTarget?.kind === "article") {
      openRegistryArticleEvidence(sourceTarget.id, sourceTarget.source, { updateHash: false, fallbackSourceUrl: sourceTarget.url,
        fallbackSourceIsChecked: Boolean(sourceTarget.checkedSourceUrl), originalSourceUrl: sourceTarget.originalUrl || "" });
      return;
    }
    if (sourceTarget?.kind === "article-url") {
      void openWebEvidence(sourceTarget.url, { updateHash: false,
        fallbackSourceIsChecked: Boolean(sourceTarget.checkedSourceUrl), checkedSourceUrl: sourceTarget.checkedSourceUrl || "",
        originalSourceUrl: sourceTarget.originalUrl || "" });
      return;
    }
    if (sourceTarget?.kind === "pdf-observation") {
      void openPdfObservationEvidence(sourceTarget.id, { search: sourceTarget.search, url: sourceTarget.url,
        snippet: sourceTarget.snippet, updateHash: false, fallbackSourceIsChecked: Boolean(sourceTarget.checkedSourceUrl),
        originalSourceUrl: sourceTarget.originalUrl || "" });
      return;
    }
    if (window.location.hash === "#meetings") {
      beginEvidenceNavigation();
      setWorkspaceView("meetingsView");
      return;
    }
    const reportDate = historicalReportDateFromHash();
    if (reportDate) {
      openHistoricalReport(reportDate, { updateHash: false,
        fallbackSourceUrl: sourceTarget?.kind === "report" ? sourceTarget.url : "",
        fallbackSourceIsChecked: sourceTarget?.kind === "report" && Boolean(sourceTarget.checkedSourceUrl),
        originalSourceUrl: sourceTarget?.kind === "report" ? sourceTarget.originalUrl || "" : "" });
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
      const target = sourceNavigationFromHash();
      if (target?.kind === "meeting" && els.registryMeetingSearch.value.trim() === target.name && !els.registryMeetingVerification.value) {
        void loadRegistryMeetings({ focusId: target.id, focusName: target.name });
      } else {
        void loadRegistryMeetings();
      }
    }
  });
}

async function main() {
  loadThread();
  void refreshChatAccess();
  setSending(false);
  fetch("/api/manage/session", { credentials: "same-origin" })
    .then((response) => (response.ok ? response.json() : { authenticated: false }))
    .then(({ authenticated }) => {
      if (authenticated && els.hermesLink) els.hermesLink.hidden = false;
    })
    .catch(() => {});
  setAnswerMode(state.answerMode);
  setGraphMode(state.graphMode);
  const sourceTarget = sourceNavigationFromHash();
  const reportDate = historicalReportDateFromHash();
  setWorkspaceView(sourceTarget?.kind === "meeting" || window.location.hash === "#meetings"
    ? "meetingsView"
    : sourceTarget?.kind === "article" || sourceTarget?.kind === "article-url" || sourceTarget?.kind === "pdf-observation" || reportDate ? "registryView" : state.activeView);
  renderMessages();
  attachEvents();
  if (sourceTarget?.kind === "meeting") {
    openMeetingEvidence(sourceTarget.id, sourceTarget.name, { updateHash: false });
  } else if (sourceTarget?.kind === "article") {
    openRegistryArticleEvidence(sourceTarget.id, sourceTarget.source, { updateHash: false, fallbackSourceUrl: sourceTarget.url,
      fallbackSourceIsChecked: Boolean(sourceTarget.checkedSourceUrl), originalSourceUrl: sourceTarget.originalUrl || "" });
  } else if (sourceTarget?.kind === "article-url") {
    void openWebEvidence(sourceTarget.url, { updateHash: false,
      fallbackSourceIsChecked: Boolean(sourceTarget.checkedSourceUrl), checkedSourceUrl: sourceTarget.checkedSourceUrl || "",
      originalSourceUrl: sourceTarget.originalUrl || "" });
  } else if (sourceTarget?.kind === "pdf-observation") {
    void openPdfObservationEvidence(sourceTarget.id, { search: sourceTarget.search, url: sourceTarget.url,
      snippet: sourceTarget.snippet, updateHash: false, fallbackSourceIsChecked: Boolean(sourceTarget.checkedSourceUrl),
      originalSourceUrl: sourceTarget.originalUrl || "" });
  } else if (reportDate) {
    openHistoricalReport(reportDate, { updateHash: false,
      fallbackSourceUrl: sourceTarget?.kind === "report" ? sourceTarget.url : "",
      fallbackSourceIsChecked: sourceTarget?.kind === "report" && Boolean(sourceTarget.checkedSourceUrl),
      originalSourceUrl: sourceTarget?.kind === "report" ? sourceTarget.originalUrl || "" : "" });
  }

  try {
    await loadConfig();
  } catch {
    els.status.textContent = "Service unavailable";
    els.status.classList.add("status-pill--offline");
    if (els.graphHint) {
      els.graphHint.textContent = "The service could not be reached.";
    }
    appendMessage(
      "assistant",
      "The service could not be reached. Please refresh and try again.",
      { transient: true },
    );
  }
}

main();
