const NOTE_FILES = [
  "actuaries-climate-index.md",
  "cas-soa-climate-research.md",
  "climate-monitor-2026-04-01.md",
  "climate-monitor-2026-04-02.md",
  "climate-monitor-2026-04-03.md",
  "climate-monitor-2026-04-04.md",
  "climate-monitor-2026-04-05.md",
  "climate-monitor-2026-04-06.md",
  "climate-monitor-2026-04-07.md",
  "climate-monitor-2026-04-08.md",
  "climate-monitor-2026-04-09.md",
  "climate-monitor-2026-04-10.md",
  "climate-monitor-2026-04-11.md",
  "climate-monitor-2026-04-12.md",
  "climate-monitor-2026-04-13.md",
  "climate-monitor-2026-04-14.md",
  "climate-monitor-2026-04-15.md",
  "climate-monitor-2026-04-16.md",
  "climate-monitor-2026-04-17.md",
  "climate-monitor-2026-04-18.md",
  "climate-monitor-2026-04-19.md",
  "climate-monitor-2026-04-20.md",
  "fsb-climate-risk.md",
  "iais-climate-risk.md",
  "index.md",
  "isbb-ifrs-s2.md",
  "log.md",
  "nat-cat-protection-gap.md",
  "parametric-insurance.md",
  "secondary-perils.md",
  "swiss-re-sigma.md",
  "talents-gap.md",
  "wri-colombia.md"
];

const COLORS = {
  daily: "#e67e22",
  topic: "#158f77",
  index: "#ca2c55"
};

const titleFromFile = (name) => name.replace(/\.md$/i, "");

function detectType(title) {
  if (title === "index") return "index";
  if (/^climate-monitor-\d{4}-\d{2}-\d{2}$/.test(title)) return "daily";
  return "topic";
}

function extractDate(title) {
  const m = title.match(/(\d{4}-\d{2}-\d{2})/);
  return m ? m[1] : "-";
}

function parseLinks(markdown) {
  const links = [];
  const rgx = /\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]/g;
  let m;
  while ((m = rgx.exec(markdown)) !== null) {
    links.push(m[1].replace(/^\.\//, "").replace(/\.md$/i, ""));
  }
  return links;
}

function countWords(markdown) {
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/[#>*\[\]()|`_-]/g, " ")
    .split(/\s+/)
    .filter(Boolean).length;
}

function rowStatus(type, markdown) {
  if (type !== "daily") return "-";
  return /no report/i.test(markdown) ? "No report" : "Reported";
}

function buildData(entries) {
  const notes = entries.map((entry) => {
    const title = titleFromFile(entry.file);
    return {
      file: entry.file,
      title,
      type: detectType(title),
      date: extractDate(title),
      markdown: entry.markdown,
      links: parseLinks(entry.markdown),
      words: countWords(entry.markdown)
    };
  });

  const byTitle = new Map(notes.map((n) => [n.title, n]));
  const edges = [];
  for (const note of notes) {
    for (const rawTarget of note.links) {
      const target = rawTarget.replace(/^wiki\//, "");
      if (!byTitle.has(target)) continue;
      edges.push({ source: note.title, target });
    }
  }

  const inCount = new Map(notes.map((n) => [n.title, 0]));
  for (const edge of edges) {
    inCount.set(edge.target, (inCount.get(edge.target) || 0) + 1);
  }

  const rows = notes
    .map((n) => ({
      ...n,
      outlinks: edges.filter((e) => e.source === n.title).length,
      inlinks: inCount.get(n.title) || 0,
      status: rowStatus(n.type, n.markdown)
    }))
    .sort((a, b) => a.title.localeCompare(b.title));

  return { rows, edges };
}

function renderRows(rows) {
  const tbody = document.getElementById("rows");
  tbody.innerHTML = rows
    .map((row) => {
      const statusClass = row.status === "Reported" ? "status-ok" : row.status === "No report" ? "status-empty" : "";
      return `
      <tr data-page="${row.title}">
        <td>${row.title}</td>
        <td>${row.type}</td>
        <td>${row.date}</td>
        <td>${row.words}</td>
        <td>${row.outlinks}</td>
        <td>${row.inlinks}</td>
        <td class="${statusClass}">${row.status}</td>
      </tr>`;
    })
    .join("");
}

function attachSearch(rows) {
  const input = document.getElementById("searchInput");
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    const filtered = rows.filter((r) =>
      `${r.title} ${r.type} ${r.date} ${r.status}`.toLowerCase().includes(q)
    );
    renderRows(filtered);
  });
}

function renderGraph(rows, edges) {
  const svg = document.getElementById("graphSvg");
  const NS = "http://www.w3.org/2000/svg";
  const width = 1200;
  const height = 700;

  svg.innerHTML = "";

  const nodes = rows.map((row, i) => ({
    id: row.title,
    type: row.type,
    x: (i % 8) * 130 + 90,
    y: Math.floor(i / 8) * 120 + 90,
    vx: 0,
    vy: 0,
    pinned: false
  }));

  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links = edges
    .map((e) => ({ source: byId.get(e.source), target: byId.get(e.target) }))
    .filter((e) => e.source && e.target);

  const linkGroup = document.createElementNS(NS, "g");
  const nodeGroup = document.createElementNS(NS, "g");
  svg.appendChild(linkGroup);
  svg.appendChild(nodeGroup);

  const lineEls = links.map(() => {
    const line = document.createElementNS(NS, "line");
    line.setAttribute("stroke", "#99b4c6");
    line.setAttribute("stroke-opacity", "0.45");
    line.setAttribute("stroke-width", "1.1");
    linkGroup.appendChild(line);
    return line;
  });

  const nodeEls = nodes.map((node) => {
    const g = document.createElementNS(NS, "g");
    const circle = document.createElementNS(NS, "circle");
    const label = document.createElementNS(NS, "text");
    circle.setAttribute("r", node.type === "index" ? "9" : "6");
    circle.setAttribute("fill", COLORS[node.type] || COLORS.topic);
    circle.setAttribute("stroke", "#113245");
    circle.setAttribute("stroke-width", "0.8");

    label.textContent = node.id;
    label.setAttribute("class", "node-label");
    label.setAttribute("x", "10");
    label.setAttribute("y", "4");

    g.appendChild(circle);
    g.appendChild(label);
    nodeGroup.appendChild(g);

    g.addEventListener("click", () => {
      const row = document.querySelector(`tr[data-page="${node.id}"]`);
      if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
    });

    let dragging = false;
    g.addEventListener("pointerdown", (ev) => {
      dragging = true;
      node.pinned = true;
      g.setPointerCapture(ev.pointerId);
    });
    g.addEventListener("pointermove", (ev) => {
      if (!dragging) return;
      const rect = svg.getBoundingClientRect();
      const sx = width / rect.width;
      const sy = height / rect.height;
      node.x = (ev.clientX - rect.left) * sx;
      node.y = (ev.clientY - rect.top) * sy;
    });
    g.addEventListener("pointerup", () => {
      dragging = false;
    });

    return g;
  });

  function tick() {
    for (const n of nodes) {
      n.vx *= 0.86;
      n.vy *= 0.86;
    }

    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = nodes[i];
        const b = nodes[j];
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

    for (const e of links) {
      const dx = e.target.x - e.source.x;
      const dy = e.target.y - e.source.y;
      const pull = 0.0009;
      if (!e.source.pinned) {
        e.source.vx += dx * pull;
        e.source.vy += dy * pull;
      }
      if (!e.target.pinned) {
        e.target.vx -= dx * pull;
        e.target.vy -= dy * pull;
      }
    }

    for (const n of nodes) {
      if (n.pinned) continue;
      n.x += n.vx;
      n.y += n.vy;
      n.x = Math.max(18, Math.min(width - 18, n.x));
      n.y = Math.max(18, Math.min(height - 18, n.y));
    }

    links.forEach((e, i) => {
      lineEls[i].setAttribute("x1", e.source.x);
      lineEls[i].setAttribute("y1", e.source.y);
      lineEls[i].setAttribute("x2", e.target.x);
      lineEls[i].setAttribute("y2", e.target.y);
    });

    nodes.forEach((n, i) => {
      nodeEls[i].setAttribute("transform", `translate(${n.x},${n.y})`);
    });

    requestAnimationFrame(tick);
  }

  requestAnimationFrame(tick);
}

async function main() {
  const entries = await Promise.all(
    NOTE_FILES.map(async (file) => {
      const markdown = await fetch(`../wiki/${file}`).then((r) => r.text());
      return { file, markdown };
    })
  );

  const { rows, edges } = buildData(entries);
  document.getElementById("metaNotes").textContent = `Notes: ${rows.length}`;
  document.getElementById("metaEdges").textContent = `Edges: ${edges.length}`;

  renderRows(rows);
  attachSearch(rows);
  renderGraph(rows, edges);
}

main().catch((err) => {
  const hint = document.getElementById("graphHint");
  hint.textContent = `Load failed: ${err.message}`;
});
