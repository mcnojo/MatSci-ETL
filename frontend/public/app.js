const $ = (s) => document.querySelector(s);
const setStatus = (m) => { $("#status").textContent = m; };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ─── module state ─────────────────────────────────────────────────────────
// _tree: parsed tree.json for current paper
// _pageCanvases: page_index (1-based) -> canvas element, so we can position
//   overlays over the correct page after render.
// _elementIndex / _nodeIndex: id -> object for fast lookup on tree/pdf clicks.
// _annotations: {elements: {id: {position_correct, qualification_correct}},
//               nodes: {id: {structure_correct}}}. null = unreviewed.
// _saveTimer: debounce handle for annotations PUT.
let _tree = null, _paper = null, _treeBaseUrl = null;
let _pageCanvases = {}, _elementIndex = {}, _nodeIndex = {};
let _annotations = null, _saveTimer = null;

// ─── URL helpers ──────────────────────────────────────────────────────────
// Paths in tree.json (asset_path/uri, page_images) resolve relative to the
// tree.json's directory (portable artifact convention).
function assetUrl(relPath) {
  if (!relPath) return "";
  if (/^https?:\/\//.test(relPath)) return relPath;
  return new URL(relPath, _treeBaseUrl).pathname;
}

// ─── OCR rendering (mostly unchanged from previous viewer) ────────────────
const RAW_CAP = 2000;
function rawBlock(content) {
  const trimmed = content.length > RAW_CAP
    ? content.slice(0, RAW_CAP) + `\n…[truncated, ${content.length - RAW_CAP} more chars]`
    : content;
  return `<details class="raw-toggle"><summary>raw</summary><pre class="raw">${esc(trimmed)}</pre></details>`;
}

function renderAnalyze(jsonStr) {
  let parsed;
  try { parsed = JSON.parse(jsonStr); }
  catch { return `<pre class="raw">${esc(jsonStr.slice(0, RAW_CAP))}</pre>`; }
  const items = Array.isArray(parsed) ? parsed : [parsed];
  return items.map((sub) => {
    const rows = ["x_labels", "y_labels", "x_ticks", "y_ticks", "legends", "series"]
      .filter((k) => sub[k]).map((k) => `<dt>${k.replace(/_/g, " ")}</dt><dd>${esc(sub[k])}</dd>`).join("");
    return `<div class="analysis"><h5>${esc(sub.titles || "subplot")}</h5><dl>${rows}</dl></div>`;
  }).join("");
}

// chandra emits <img alt="..."> as image-region descriptions — promote to aside.
function sanitizeChandraHtml(html) {
  return html.replace(/<img\b([^>]*?)\/?>/gi, (full, attrs) => {
    if (/\bsrc\s*=/i.test(attrs)) return full;
    const m = attrs.match(/\balt\s*=\s*(?:"([^"]*)"|'([^']*)')/i);
    const alt = m ? (m[1] ?? m[2] ?? "") : "";
    return alt ? `<aside class="img-desc">${esc(alt)}</aside>` : "";
  });
}

function renderParsed(p) {
  if (!p) return "";
  if (p.format === "layout_html") {
    return (p.blocks || []).map((b) => {
      const label = esc(b.label || "Block");
      const body = b.html ? sanitizeChandraHtml(b.html) : esc(b.text || "");
      return `<div data-label="${label}">${body}</div>`;
    }).join("");
  }
  if (p.format === "figure_analysis") {
    const note = p.truncated ? `<div class="truncated-note">⚠ truncated output</div>` : "";
    const panels = (p.panels || []).map((sub) => {
      const rows = ["x_label", "y_label", "x_tick", "y_tick", "legend", "series"]
        .filter((k) => sub[k]).map((k) => `<dt>${k.replace(/_/g, " ")}</dt><dd>${esc(sub[k])}</dd>`).join("");
      return `<div class="analysis"><h5>${esc(sub.title || "subplot")}</h5><dl>${rows}</dl></div>`;
    }).join("");
    return note + panels;
  }
  return `<pre class="raw">${esc(JSON.stringify(p).slice(0, RAW_CAP))}</pre>`;
}

function renderChandra(content) {
  if (!content) return "";
  const m = content.match(/<analyze>\s*([\s\S]*?)\s*<\/analyze>/);
  if (m) return renderAnalyze(m[1]) + rawBlock(content);
  if (/data-bbox=|data-label=/.test(content)) return sanitizeChandraHtml(content);
  const opener = content.indexOf("<analyze>");
  if (opener !== -1) {
    const head = content.slice(opener + "<analyze>".length);
    const lastBrace = head.lastIndexOf("}");
    if (lastBrace !== -1) {
      const candidate = head.slice(0, lastBrace + 1) + (head.trimStart().startsWith("[") ? "]" : "");
      try {
        JSON.parse(candidate);
        return `<div class="truncated-note">⚠ truncated output, showing salvaged head</div>`
          + renderAnalyze(candidate) + rawBlock(content);
      } catch {}
    }
  }
  return rawBlock(content);
}

// ─── review-checkbox widget ───────────────────────────────────────────────
// Tri-state cycle: null -> true -> false -> null. Persists via saveAnnotations.
function reviewControl(scope, id, key, label) {
  const state = _annotations[scope][id]?.[key] ?? null;
  const cls = state === true ? "yes" : state === false ? "no" : "unset";
  const glyph = state === true ? "✓" : state === false ? "✗" : "○";
  return `<button class="review ${cls}" data-scope="${scope}" data-id="${esc(id)}" data-key="${key}"
                  title="${label}: click to cycle unset → correct → incorrect">${glyph} ${label}</button>`;
}

function cycleReview(scope, id, key) {
  _annotations[scope][id] ??= {};
  const cur = _annotations[scope][id][key] ?? null;
  const next = cur === null ? true : cur === true ? false : null;
  _annotations[scope][id][key] = next;
  scheduleSave();
}

// ─── visual-element rendering (tree pane) ─────────────────────────────────
function renderVisuals(els) {
  if (!els?.length) return "";
  return `<div class="visuals">` + els.map((e) => {
    const img = e.asset_path ? `<img src="${assetUrl(e.asset_path)}" alt="${esc(e.element_id)}">` : "";
    const cap = e.caption ? `<p class="caption">${esc(e.caption)}</p>` : "";
    const ocrBody = e.ocr_parsed ? renderParsed(e.ocr_parsed)
                  : e.ocr_text   ? renderChandra(e.ocr_text)
                  : "";
    const ocr = ocrBody ? `<div class="block"><h4>OCR</h4><div class="ocr">${ocrBody}</div></div>` : "";
    const chems = e.chem_entities?.length
      ? `<div class="block"><h4>Chem entities</h4><div class="chems">${e.chem_entities.map((c) => `<span>${esc(c)}</span>`).join("")}</div></div>` : "";
    const controls = e.bbox
      ? `<div class="review-row">
           ${reviewControl("elements", e.element_id, "position_correct", "position")}
           ${reviewControl("elements", e.element_id, "qualification_correct", "qualification")}
           <button class="locate" data-element="${esc(e.element_id)}" title="Scroll PDF to overlay">↗ locate</button>
         </div>` : "";
    return `
      <details class="visual" data-element="${esc(e.element_id)}" data-t="${esc(e.element_type)}">
        <summary><code>${esc(e.element_id)}</code><span class="type">${esc(e.element_type)}</span><span class="pgtag">p${e.page_index}</span></summary>
        <div class="visual-body">${controls}${img}${cap}${ocr}${chems}</div>
      </details>`;
  }).join("") + `</div>`;
}

function renderTree(nodes, depth = 0) {
  return nodes.map((n) => `
    <details class="node depth-${depth}" data-node="${esc(n.node_id)}" ${depth === 0 ? "open" : ""}>
      <summary>
        <span class="title">${esc(n.title)}</span>
        <span class="meta">pp ${n.start_index}–${n.end_index}</span>
        <span class="review-inline">${reviewControl("nodes", n.node_id, "structure_correct", "struct")}</span>
      </summary>
      ${n.summary ? `<div class="summary-md">${marked.parse(n.summary)}</div>` : ""}
      ${renderVisuals(n.visual_elements)}
      ${n.nodes?.length ? renderTree(n.nodes, depth + 1) : ""}
    </details>
  `).join("");
}

function renderMath(root) {
  root.querySelectorAll("math").forEach((el) => {
    const tex = el.textContent;
    const display = el.getAttribute("display") === "block";
    try { katex.render(tex, el, { throwOnError: false, displayMode: display }); } catch {}
  });
  root.querySelectorAll(".summary-md").forEach((el) => {
    el.innerHTML = el.innerHTML.replace(/\$\$([\s\S]+?)\$\$/g, (_, t) => {
      try { return katex.renderToString(t, { throwOnError: false, displayMode: true }); } catch { return _; }
    }).replace(/\$([^\$\n]+?)\$/g, (_, t) => {
      try { return katex.renderToString(t, { throwOnError: false, displayMode: false }); } catch { return _; }
    });
  });
}

// ─── indices ──────────────────────────────────────────────────────────────
// Flat lookup tables for two-way tree<->overlay linking and progress counts.
function indexTree(nodes) {
  for (const n of nodes) {
    _nodeIndex[n.node_id] = n;
    for (const e of (n.visual_elements || [])) _elementIndex[e.element_id] = e;
    if (n.nodes?.length) indexTree(n.nodes);
  }
}

// ─── PDF render + overlay ─────────────────────────────────────────────────
async function renderPdf(paper) {
  const container = $("#pdf");
  container.innerHTML = "";
  _pageCanvases = {};
  setStatus("loading pdf…");
  try {
    const pdf = await pdfjsLib.getDocument(`/pdf/${paper}`).promise;
    for (let i = 1; i <= pdf.numPages; i++) {
      const page = await pdf.getPage(i);
      const viewport = page.getViewport({ scale: 1.5 });
      // page-container wraps a canvas + an overlay div so both share the page's
      // scaled coordinate space via absolute positioning.
      const wrap = document.createElement("div");
      wrap.className = "page-wrap";
      wrap.dataset.page = i;
      wrap.style.width = viewport.width + "px";
      wrap.style.height = viewport.height + "px";
      const canvas = document.createElement("canvas");
      canvas.className = "page";
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      const overlay = document.createElement("div");
      overlay.className = "overlay";
      wrap.appendChild(canvas);
      wrap.appendChild(overlay);
      container.appendChild(wrap);
      _pageCanvases[i] = wrap;
      await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
    }
    drawOverlays();
    setStatus(`${pdf.numPages} pages`);
  } catch (e) {
    container.innerHTML = `<p class="error">PDF unavailable: ${esc(e.message || e)}</p>`;
    setStatus("pdf error");
  }
}

function drawOverlays() {
  for (const wrap of Object.values(_pageCanvases)) {
    wrap.querySelector(".overlay").innerHTML = "";
  }
  for (const [id, e] of Object.entries(_elementIndex)) {
    if (!e.bbox || !_pageCanvases[e.page_index]) continue;
    const wrap = _pageCanvases[e.page_index];
    const w = parseFloat(wrap.style.width), h = parseFloat(wrap.style.height);
    const box = document.createElement("div");
    box.className = `bbox t-${e.element_type}`;
    box.dataset.element = id;
    box.style.left = (e.bbox.x0 * w) + "px";
    box.style.top = (e.bbox.y0 * h) + "px";
    box.style.width = ((e.bbox.x1 - e.bbox.x0) * w) + "px";
    box.style.height = ((e.bbox.y1 - e.bbox.y0) * h) + "px";
    const state = _annotations.elements[id]?.position_correct ?? null;
    if (state === true) box.classList.add("pos-yes");
    else if (state === false) box.classList.add("pos-no");
    box.innerHTML = `<span class="bbox-tag">${esc(id)}</span>`;
    wrap.querySelector(".overlay").appendChild(box);
  }
}

// ─── two-way linking ──────────────────────────────────────────────────────
function scrollToElement(elementId) {
  const e = _elementIndex[elementId];
  if (!e) return;
  const wrap = _pageCanvases[e.page_index];
  if (!wrap) return;
  wrap.scrollIntoView({ behavior: "smooth", block: "start" });
  const bbox = wrap.querySelector(`.bbox[data-element="${CSS.escape(elementId)}"]`);
  if (bbox) {
    bbox.classList.add("flash");
    setTimeout(() => bbox.classList.remove("flash"), 1600);
  }
}

function scrollTreeToElement(elementId) {
  const el = $(`#tree .visual[data-element="${CSS.escape(elementId)}"]`);
  if (!el) return;
  // open every ancestor <details> so the target is actually visible
  let p = el;
  while (p && p !== document.body) { if (p.tagName === "DETAILS") p.open = true; p = p.parentElement; }
  el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1600);
}

// ─── annotations persistence ──────────────────────────────────────────────
// Idempotent write: same in-memory state -> same file contents. Stats block
// is recomputed each write so downstream benchmark reports read fresh totals.
function computeStats() {
  const elIds = Object.keys(_elementIndex);
  const nodeIds = Object.keys(_nodeIndex);
  const isSet = (v) => v === true || v === false;
  const count = (ids, scope, key) => ids.filter((i) => isSet(_annotations[scope][i]?.[key])).length;
  const truthy = (ids, scope, key) => ids.filter((i) => _annotations[scope][i]?.[key] === true).length;
  return {
    total_elements: elIds.length,
    reviewed_position: count(elIds, "elements", "position_correct"),
    correct_position: truthy(elIds, "elements", "position_correct"),
    reviewed_qualification: count(elIds, "elements", "qualification_correct"),
    correct_qualification: truthy(elIds, "elements", "qualification_correct"),
    total_nodes: nodeIds.length,
    reviewed_structure: count(nodeIds, "nodes", "structure_correct"),
    correct_structure: truthy(nodeIds, "nodes", "structure_correct"),
  };
}

function scheduleSave() {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(saveAnnotations, 300);
  refreshLocalUi();
}

async function saveAnnotations() {
  if (!_paper) return;
  const payload = {
    paper_id: _paper,
    schema_version: 1,
    annotated_at: new Date().toISOString(),
    elements: _annotations.elements,
    nodes: _annotations.nodes,
    stats: computeStats(),
  };
  setStatus("saving…");
  const r = await fetch(`/api/annotations/${encodeURIComponent(_paper)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) { setStatus("save failed"); return; }
  const { path } = await r.json();
  setStatus(`saved → ${path}`);
}

// Update just the changed checkboxes + overlay tint + header progress in-place
// (no full re-render — preserves expanded/collapsed state + scroll position).
function refreshLocalUi() {
  document.querySelectorAll("button.review").forEach((btn) => {
    const { scope, id, key } = btn.dataset;
    const state = _annotations[scope][id]?.[key] ?? null;
    btn.classList.remove("yes", "no", "unset");
    btn.classList.add(state === true ? "yes" : state === false ? "no" : "unset");
    const label = key === "position_correct" ? "position"
                : key === "qualification_correct" ? "qualification"
                : "struct";
    btn.textContent = `${state === true ? "✓" : state === false ? "✗" : "○"} ${label}`;
  });
  document.querySelectorAll(".bbox").forEach((box) => {
    const state = _annotations.elements[box.dataset.element]?.position_correct ?? null;
    box.classList.remove("pos-yes", "pos-no");
    if (state === true) box.classList.add("pos-yes");
    else if (state === false) box.classList.add("pos-no");
  });
  const s = computeStats();
  $("#progress").textContent =
    `elements ${s.reviewed_position}/${s.total_elements} pos, ${s.reviewed_qualification}/${s.total_elements} qual · nodes ${s.reviewed_structure}/${s.total_nodes}`;
}

// ─── paper switch ─────────────────────────────────────────────────────────
async function loadPaper(paper) {
  _paper = paper;
  history.replaceState(null, "", `?paper=${encodeURIComponent(paper)}`);
  setStatus("loading tree…");
  const treeUrl = `/trees/${paper}/tree.json`;
  _treeBaseUrl = new URL(treeUrl, location.href);
  _tree = await (await fetch(treeUrl)).json();

  _elementIndex = {}; _nodeIndex = {};
  indexTree(_tree.root_nodes);

  const saved = await (await fetch(`/api/annotations/${encodeURIComponent(paper)}`)).json();
  _annotations = saved && saved.elements && saved.nodes
    ? { elements: saved.elements, nodes: saved.nodes }
    : { elements: {}, nodes: {} };

  $("#tree").innerHTML = renderTree(_tree.root_nodes);
  renderMath($("#tree"));
  refreshLocalUi();
  await renderPdf(paper);
}

// ─── global event wiring ──────────────────────────────────────────────────
function wire() {
  // tree pane: review-button cycles + locate-in-pdf links
  $("#tree").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button.review");
    if (btn) {
      ev.preventDefault(); ev.stopPropagation();
      cycleReview(btn.dataset.scope, btn.dataset.id, btn.dataset.key);
      return;
    }
    const loc = ev.target.closest("button.locate");
    if (loc) { ev.preventDefault(); scrollToElement(loc.dataset.element); }
  });
  // pdf pane: clicking a bbox scrolls tree to matching element
  $("#pdf").addEventListener("click", (ev) => {
    const box = ev.target.closest(".bbox");
    if (box) scrollTreeToElement(box.dataset.element);
  });
}

async function init() {
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js";

  wire();

  const papers = await (await fetch("/api/papers")).json();
  const sel = $("#paper-select");
  if (!papers.length) {
    sel.innerHTML = `<option>no papers found</option>`;
    setStatus("trees/ is empty");
    return;
  }
  papers.forEach((p) => sel.add(new Option(p, p)));
  sel.addEventListener("change", () => loadPaper(sel.value));
  const initial = new URLSearchParams(location.search).get("paper") || papers[0];
  sel.value = papers.includes(initial) ? initial : papers[0];
  await loadPaper(sel.value);
}

init();
