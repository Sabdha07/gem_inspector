let DATA = null;
let MODEL_ID = null; // Kept at module scope so the debug step can reach the
                      // same cached model after the initial analysis finishes.
let COMP_NAMES = {}; // compartment id -> friendly display name, set after analysis
const $ = (id) => document.getElementById(id);

// Sort state for tables
let sortState = {};

const fileInput = $("modelFile");
const analyzeBtn = $("analyzeBtn");
const filename = $("filename");
const status = $("status");
const dropzone = $("dropzone");
const dietMode = $("dietMode");
const dietDropzone = $("dietDropzone");
const dietFileInput = $("dietFile");
const dietFilenameEl = $("dietFilename");

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  analyzeBtn.disabled = !file;
  filename.textContent = file ? file.name : "";
});

["dragenter","dragover"].forEach(e => dropzone.addEventListener(e, ev => {
  ev.preventDefault(); dropzone.classList.add("drag");
}));
["dragleave","drop"].forEach(e => dropzone.addEventListener(e, ev => {
  ev.preventDefault(); dropzone.classList.remove("drag");
}));
dropzone.addEventListener("drop", ev => {
  const file = ev.dataTransfer.files[0];
  if (!file) return;
  fileInput.files = ev.dataTransfer.files;
  analyzeBtn.disabled = false;
  filename.textContent = file.name;
});

// --- Diet / media controls ---
dietMode.addEventListener("change", () => {
  const isCustom = dietMode.value === "custom";
  dietDropzone.classList.toggle("hidden", !isCustom);
  if (!isCustom) {
    dietFileInput.value = "";
    dietFilenameEl.textContent = "";
  }
});

dietFileInput.addEventListener("change", () => {
  const file = dietFileInput.files[0];
  dietFilenameEl.textContent = file ? file.name : "";
});

["dragenter","dragover"].forEach(e => dietDropzone.addEventListener(e, ev => {
  ev.preventDefault(); dietDropzone.classList.add("drag");
}));
["dragleave","drop"].forEach(e => dietDropzone.addEventListener(e, ev => {
  ev.preventDefault(); dietDropzone.classList.remove("drag");
}));
dietDropzone.addEventListener("drop", ev => {
  const file = ev.dataTransfer.files[0];
  if (!file) return;
  dietFileInput.files = ev.dataTransfer.files;
  dietFilenameEl.textContent = file.name;
});

analyzeBtn.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  if (dietMode.value === "custom" && !dietFileInput.files[0]) {
    status.innerHTML = `<span class="error">Choose a diet file, or switch "Diet / media" back to something else.</span>`;
    return;
  }

  analyzeBtn.disabled = true;
  status.textContent = "Loading model and running initial analysis…";

  try {
    const form = new FormData();
    form.append("model", file);
    form.append("diet_mode", dietMode.value);
    if (dietMode.value === "custom" && dietFileInput.files[0]) {
      form.append("diet_file", dietFileInput.files[0]);
    }

    // Phase 1: Quick analysis
    const quickResponse = await fetch("/api/analyze-quick", { method:"POST", body:form });
    const quickData = await quickResponse.json();
    if (!quickResponse.ok) throw new Error(quickData.error || "Analysis failed");

    // Store quick results and model ID
    const modelId = quickData.model_id;
    delete quickData.model_id;
    MODEL_ID = modelId;
    DATA = quickData;
    DATA.exchanges = []; // Will be filled by streaming
    sortState = {};
    COMP_NAMES = Object.fromEntries((DATA.stats.compartment_details || []).map(c => [c.id, c.name]));
    $("debugTab").style.display = "none";
    $("debug").innerHTML = "";
    TRACER_RESULT = null;
    TRACER_KO_VIEW = "before";
    TRACER_HIGHLIGHT_KO = true;
    if (TRACER_CY) { TRACER_CY.destroy(); TRACER_CY = null; }

    // Show initial results
    renderAll();
    $("app").classList.remove("hidden");
    status.textContent = "Performing exchange knockouts…";

    // Phase 2: Stream KO analysis
    streamKOAnalysis(modelId);

  } catch (err) {
    status.innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    analyzeBtn.disabled = false;
  }
});

function streamKOAnalysis(modelId) {
  const eventSource = new EventSource(`/api/analyze-ko/${modelId}`);
  let receivedAny = false;
  
  eventSource.onopen = () => {
    console.log("Connection opened for model:", modelId);
  };
  
  eventSource.onmessage = (event) => {
    try {
      receivedAny = true;
      const msg = JSON.parse(event.data);
      console.log("Received message:", msg.type);
      
      if (msg.type === "info") {
        status.textContent = msg.message;
      } else if (msg.type === "progress") {
        status.textContent = `Testing exchanges: ${msg.current}/${msg.total} - ${msg.message}`;
        updateProgressBar(msg.current, msg.total);
      } else if (msg.type === "complete") {
        DATA.exchanges = msg.exchanges;
        DATA.essentiality_rule = msg.essentiality_rule;
        DATA.transport_rule = msg.transport_rule;
        
        // Re-render with completed data
        renderExchanges();
        status.textContent = "Analysis complete!";
        analyzeBtn.disabled = false;
        eventSource.close();
      }
    } catch (err) {
      console.error("Parse error:", err, "Raw data:", event.data);
    }
  };
  
  eventSource.onerror = (err) => {
    console.error("EventSource error:", err, "Received any data:", receivedAny);
    if (receivedAny) {
      // If we received data but got an error, it might just be connection closing normally
      status.textContent = "Analysis complete!";
      analyzeBtn.disabled = false;
    } else {
      status.innerHTML = `<span class="error">Connection error - try uploading again</span>`;
    }
    eventSource.close();
    analyzeBtn.disabled = false;
  };
}

function updateProgressBar(current, total) {
  const percent = Math.round((current / total) * 100);
  let progressBar = $("progressBar");
  if (!progressBar) {
    const statusDiv = document.createElement("div");
    statusDiv.id = "progressBar";
    statusDiv.style.cssText = "margin-top:8px;height:4px;background:#e5eaf1;border-radius:2px;overflow:hidden;";
    statusDiv.innerHTML = `<div style="height:100%;background:#2563eb;width:0%;transition:width 0.2s;"></div>`;
    status.parentNode.insertBefore(statusDiv, status.nextSibling);
    progressBar = $("progressBar");
  }
  const bar = progressBar.querySelector("div");
  if (bar) bar.style.width = percent + "%";
}

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
    btn.classList.add("active");
    $(btn.dataset.tab).classList.add("active");
  });
});

function escapeHtml(x) {
  return String(x ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;" }[c]));
}
function fmt(x, digits=4) {
  if (x === null || x === undefined || Number.isNaN(Number(x))) return "—";
  const n = Number(x);
  if (!Number.isFinite(n)) return n > 0 ? "∞" : "-∞";
  return n.toFixed(digits).replace(/\.?0+$/, "");
}
function join(x) { return (x || []).join(", "); }

// Make table sortable by adding click handlers to headers
function makeSortable(tableId, rows, renderFn) {
  const table = $(tableId);
  if (!table) return;
  
  const headers = table.querySelectorAll("th");
  headers.forEach((th, colIndex) => {
    th.classList.add("sortable");
    th.style.cursor = "pointer";
    
    th.addEventListener("click", () => {
      const key = sortState[tableId] || {};
      const isAsc = key.col === colIndex ? !key.asc : true;
      sortState[tableId] = { col: colIndex, asc: isAsc };
      
      // Update header indicators
      headers.forEach(h => h.textContent = h.textContent.replace(/\s*[↑↓]\s*$/, ""));
      th.textContent += (isAsc ? " ↑" : " ↓");
      
      // Get column key from th data attribute or determine from content
      const colKey = th.dataset.key;
      
      const sorted = [...rows].sort((a, b) => {
        let aVal = a[colKey];
        let bVal = b[colKey];
        
        // Handle null/undefined
        if (aVal == null && bVal == null) return 0;
        if (aVal == null) return isAsc ? 1 : -1;
        if (bVal == null) return isAsc ? -1 : 1;
        
        // Numeric comparison
        if (typeof aVal === "number" && typeof bVal === "number") {
          return isAsc ? aVal - bVal : bVal - aVal;
        }
        
        // String comparison
        aVal = String(aVal).toLowerCase();
        bVal = String(bVal).toLowerCase();
        return isAsc ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
      });
      
      renderFn(sorted);
    });
  });
}

function renderAll() {
  renderSummary();
  renderOverview();
  renderReactions();
  renderMetabolites();
  renderExchanges();
  renderObjective();
  renderTracer();
}

function renderSummary() {
  const s = DATA.stats;
  const cards = [
    ["Genes", s.genes], ["Metabolites", s.metabolites], ["Reactions", s.reactions],
    ["Compartments", s.compartments], ["WT growth", fmt(s.wt_growth)], ["Solver", s.solver]
  ];
  $("summary").innerHTML = cards.map(([l,v]) =>
    `<div class="summary-card"><div class="label">${escapeHtml(l)}</div><div class="value">${escapeHtml(v)}</div></div>`
  ).join("");
}

function renderOverview() {
  const s = DATA.stats;
  const d = DATA.diet;
  $("overview").innerHTML = `
    <h2>Model overview</h2>
    <dl class="kv">
      <dt>Model ID</dt><dd>${escapeHtml(s.model_id)}</dd>
      <dt>Model name</dt><dd>${escapeHtml(s.model_name || "—")}</dd>
      <dt>Objective direction</dt><dd>${escapeHtml(s.objective_direction)}</dd>
      <dt>WT optimization</dt><dd>${escapeHtml(s.wt_status)}</dd>
      <dt>WT objective value</dt><dd>${fmt(s.wt_growth)}</dd>
    </dl>
    <h3>Compartments</h3>
    <div>${s.compartment_details.map(c =>
      `<span class="badge" title="${escapeHtml(c.raw_name && c.raw_name !== c.name ? `Model-provided name: ${c.raw_name}` : "")}">${escapeHtml(c.id)}: ${escapeHtml(c.name)}</span> `
    ).join("")}</div>
    ${d ? renderDietSummary(d) : ""}
    <h3>Analysis rule</h3>
    <div class="note">${escapeHtml(DATA.essentiality_rule)}</div>
    <div class="note">${escapeHtml(DATA.transport_rule)}</div>
  `;
}

function renderDietSummary(d) {
  const modeLabels = { none: "Model's original bounds", complete: "Complete media", minimal: "Minimal media (computed)", custom: "Custom diet" };
  const label = modeLabels[d.mode] || d.mode;
  const cls = d.error ? "error" : "note";
  let html = `<h3>Diet / media</h3><div class="${cls}"><strong>${escapeHtml(label)}.</strong> ${escapeHtml(d.note || "")}</div>`;
  if (d.unmatched && d.unmatched.length) {
    html += `<div class="note">Unmatched diet entries (no matching exchange reaction found in this model): ${
      d.unmatched.map(u => `<span class="badge bad">${escapeHtml(u.id)} (${fmt(u.flux, 4)})</span>`).join(" ")
    }</div>`;
  }
  return html;
}

function renderReactions() {
  const types = [...new Set(DATA.reactions.map(r => r.type))].sort();
  $("reactions").innerHTML = `
    <div class="toolbar">
      <input id="rxnSearch" type="text" placeholder="Search ID, name, subsystem…">
      <select id="rxnType"><option value="">All types</option>${types.map(t => `<option>${escapeHtml(t)}</option>`).join("")}</select>
      <select id="rxnSubsystem"><option value="">All subsystems</option>${subsystems(DATA.reactions).map(t => `<option>${escapeHtml(t)}</option>`).join("")}</select>
      <span id="rxnCount"></span>
    </div>
    <div class="table-wrap"><table id="rxnTable">
      <thead><tr>
        <th data-key="id">ID</th>
        <th data-key="name">Name</th>
        <th data-key="type">Type</th>
        <th data-key="subsystem">Subsystem</th>
        <th data-key="compartments">Compartments</th>
        <th data-key="lower_bound">LB</th>
        <th data-key="upper_bound">UB</th>
        <th data-key="wt_flux">WT flux</th>
        <th data-key="gene_reaction_rule">GPR</th>
      </tr></thead>
      <tbody id="rxnBody"></tbody>
    </table></div>
  `;
  const redraw = (filtered) => {
    const q = $("rxnSearch").value.toLowerCase();
    const type = $("rxnType").value;
    const sub = $("rxnSubsystem").value;
    const rows = filtered ? filtered : DATA.reactions.filter(r =>
      (!q || `${r.id} ${r.name} ${r.subsystem} ${r.gene_reaction_rule}`.toLowerCase().includes(q)) &&
      (!type || r.type === type) && (!sub || r.subsystem === sub)
    );
    $("rxnCount").textContent = `${rows.length} / ${DATA.reactions.length}`;
    $("rxnBody").innerHTML = rows.map(r => `<tr>
      <td><strong>${escapeHtml(r.id)}</strong></td><td class="wrap">${escapeHtml(r.name)}</td>
      <td><span class="badge">${escapeHtml(r.type)}</span></td><td>${escapeHtml(r.subsystem || "—")}</td>
      <td>${compartmentBadges(r.compartments)}</td><td class="num">${fmt(r.lower_bound, 2)}</td><td class="num">${fmt(r.upper_bound, 2)}</td>
      <td class="num">${fmt(r.wt_flux)}</td><td class="wrap">${escapeHtml(r.gene_reaction_rule || "—")}</td>
    </tr>`).join("");
  };
  ["rxnSearch","rxnType","rxnSubsystem"].forEach(id => $(id).addEventListener("input", () => redraw()));
  redraw();
  setTimeout(() => makeSortable("rxnTable", DATA.reactions, redraw), 0);
}

function renderMetabolites() {
  const comps = [...new Set(DATA.metabolites.map(m => m.compartment).filter(Boolean))].sort();
  $("metabolites").innerHTML = `
    <div class="toolbar">
      <input id="metSearch" type="text" placeholder="Search metabolite ID, name, formula…">
      <select id="metComp"><option value="">All compartments</option>${comps.map(c => `<option>${escapeHtml(c)}</option>`).join("")}</select>
      <span id="metCount"></span>
    </div>
    <div class="table-wrap"><table id="metTable">
      <thead><tr>
        <th data-key="id">ID</th>
        <th data-key="name">Name</th>
        <th data-key="compartment">Compartment</th>
        <th data-key="formula">Formula</th>
        <th data-key="charge">Charge</th>
      </tr></thead>
      <tbody id="metBody"></tbody>
    </table></div>
  `;
  const redraw = (filtered) => {
    const q = $("metSearch").value.toLowerCase();
    const comp = $("metComp").value;
    const rows = filtered ? filtered : DATA.metabolites.filter(m =>
      (!q || `${m.id} ${m.name} ${m.formula}`.toLowerCase().includes(q)) &&
      (!comp || m.compartment === comp)
    );
    $("metCount").textContent = `${rows.length} / ${DATA.metabolites.length}`;
    $("metBody").innerHTML = rows.map(m => `<tr>
      <td><strong id="met-${escapeHtml(m.id)}">${escapeHtml(m.id)}</strong></td><td>${escapeHtml(m.name)}</td>
      <td>${compartmentBadges([m.compartment])}</td>
      <td>${escapeHtml(m.formula || "—")}</td><td>${escapeHtml(m.charge ?? "—")}</td>
    </tr>`).join("");
  };
  ["metSearch","metComp"].forEach(id => $(id).addEventListener("input", () => redraw()));
  redraw();
  setTimeout(() => makeSortable("metTable", DATA.metabolites, redraw), 0);
}

function renderExchanges() {
  const exchanges = DATA.exchanges || [];
  const essentialExchanges = exchanges.filter(e => e.essential);
  const tested = exchanges.filter(e => e.ko_growth !== null && e.ko_growth !== undefined);
  const avgFraction = tested.length
    ? tested.reduce((sum, e) => sum + (e.ko_growth_fraction || 0), 0) / tested.length
    : null;

  const statCards = exchanges.length ? [
    ["Exchanges tested", exchanges.length],
    ["Essential", essentialExchanges.length],
    ["Non-essential", exchanges.length - essentialExchanges.length],
    ["Avg. KO growth retained", avgFraction == null ? "—" : `${fmt(avgFraction * 100, 1)}%`],
  ] : [];

  $("exchanges").innerHTML = `
    <div class="note">${escapeHtml(DATA.essentiality_rule)} Essentiality testing can be the slowest part for large models because each exchange is optimized after KO. Hover a row for the full reaction.</div>
    ${statCards.length ? `<div class="mini-stats">${statCards.map(([l, v]) =>
      `<div class="mini-stat"><div class="mini-stat-value">${escapeHtml(v)}</div><div class="mini-stat-label">${escapeHtml(l)}</div></div>`
    ).join("")}</div>` : ""}
    <div class="toolbar">
      <input id="exSearch" type="text" placeholder="Search exchange ID or metabolite…">
      <select id="exEssential"><option value="">All</option><option value="essential">Essential only</option><option value="nonessential">Non-essential</option></select>
      <select id="exUptake"><option value="">Uptake: all</option><option value="yes">Uptake allowed</option><option value="no">Uptake blocked</option></select>
      <span id="exCount"></span>
      ${essentialExchanges.length > 0 ? `<button id="debugExBtn" style="margin-left:auto;">🔍 Debug Essential Exchanges (${essentialExchanges.length})</button>` : ''}
    </div>
    <div class="table-wrap"><table id="exTable">
      <thead><tr>
        <th data-key="id">Exchange</th>
        <th data-key="name">Metabolite</th>
        <th data-key="lower_bound">LB</th>
        <th data-key="upper_bound">UB</th>
        <th data-key="wt_flux">WT flux</th>
        <th data-key="uptake_allowed">Uptake?</th>
        <th data-key="ko_growth_fraction">Growth retained after KO</th>
        <th data-key="essential">Essential?</th>
      </tr></thead>
      <tbody id="exBody"></tbody>
    </table></div>
  `;

  // Add debug button listener
  if (essentialExchanges.length > 0) {
    const debugBtn = $("debugExBtn");
    if (debugBtn) {
      debugBtn.addEventListener("click", () => showDebugModal(essentialExchanges));
    }
  }

  const byId = new Map(exchanges.map(r => [r.id, r]));

  const redraw = (filtered) => {
    const q = $("exSearch").value.toLowerCase();
    const ess = $("exEssential").value;
    const uptake = $("exUptake").value;
    const rows = filtered ? filtered : exchanges.filter(r => {
      const matchQ = !q || `${r.id} ${r.name} ${r.metabolites.join(" ")} ${r.metabolite_names.join(" ")}`.toLowerCase().includes(q);
      const matchEss = !ess || (ess === "essential" ? r.essential : !r.essential);
      const matchUp = !uptake || (uptake === "yes" ? r.uptake_allowed : !r.uptake_allowed);
      return matchQ && matchEss && matchUp;
    });
    $("exCount").textContent = `${rows.length} / ${exchanges.length}`;
    $("exBody").innerHTML = rows.map(r => `<tr data-row-id="${escapeHtml(r.id)}" class="${r.essential ? "row-essential" : ""}">
      <td><strong>${escapeHtml(r.id)}</strong><br><span class="stoich">${escapeHtml(r.name)}</span></td>
      <td>${escapeHtml(join(r.metabolite_names) || join(r.metabolites))}</td>
      <td class="num">${fmt(r.lower_bound, 2)}</td><td class="num">${fmt(r.upper_bound, 2)}</td><td class="num">${fmt(r.wt_flux)}</td>
      <td>${r.uptake_allowed ? "Yes" : "No"}</td>
      <td>${growthBar(r.ko_growth_fraction)}</td>
      <td><span class="badge ${r.essential ? "bad" : "good"}">${r.essential ? "ESSENTIAL" : "No"}</span></td>
    </tr>`).join("");
  };
  ["exSearch","exEssential","exUptake"].forEach(id => $(id).addEventListener("input", () => redraw()));
  redraw();
  setTimeout(() => makeSortable("exTable", exchanges, redraw), 0);
  attachRowTooltip("exBody", byId, buildExchangeTooltipHtml);
}

// A small inline bar visualizing what fraction of WT growth survives the
// knockout — quicker to scan at a glance than the raw percentage alone.
function growthBar(fraction) {
  if (fraction == null) return `<span class="stoich">not tested</span>`;
  const pct = Math.max(0, Math.min(100, fraction * 100));
  const color = pct < 5 ? "var(--bad)" : pct < 50 ? "#d97706" : "var(--good)";
  return `<div class="growth-bar-wrap">
    <div class="growth-bar"><div class="growth-bar-fill" style="width:${pct}%;background:${color};"></div></div>
    <span class="growth-bar-pct">${fmt(pct, 1)}%</span>
  </div>`;
}

function buildExchangeTooltipHtml(r) {
  return `
    <div class="tooltip-title">${escapeHtml(r.id)}${r.name ? ` — ${escapeHtml(r.name)}` : ""}</div>
    <div class="tooltip-row"><span class="tooltip-label">Reaction</span><code>${escapeHtml(r.reaction_string || "—")}</code></div>
    <div class="tooltip-row"><span class="tooltip-label">Subsystem</span>${escapeHtml(r.subsystem || "—")}</div>
    <div class="tooltip-row"><span class="tooltip-label">GPR</span>${escapeHtml(r.gene_reaction_rule || "—")}</div>
    <div class="tooltip-row"><span class="tooltip-label">Bounds</span>[${fmt(r.lower_bound, 2)}, ${fmt(r.upper_bound, 2)}]</div>
    <div class="tooltip-row"><span class="tooltip-label">WT flux</span>${fmt(r.wt_flux)}</div>
    <div class="tooltip-row"><span class="tooltip-label">KO growth</span>${fmt(r.ko_growth)} (${escapeHtml(r.ko_status)})</div>
  `;
}

// ---------------------------------------------------------------------------
// Generic row-hover tooltip: a single fixed-position element reused across
// tables (so it's never clipped by a scrolling .table-wrap), populated and
// repositioned as the mouse moves over rows carrying a data-row-id.
// ---------------------------------------------------------------------------

let TOOLTIP_EL = null;
function ensureTooltip() {
  if (!TOOLTIP_EL) {
    TOOLTIP_EL = document.createElement("div");
    TOOLTIP_EL.className = "hover-tooltip";
    document.body.appendChild(TOOLTIP_EL);
  }
  return TOOLTIP_EL;
}
function positionTooltip(x, y) {
  const el = ensureTooltip();
  const pad = 16;
  const rect = el.getBoundingClientRect();
  let left = x + pad;
  let top = y + pad;
  if (left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
  if (top + rect.height > window.innerHeight - 8) top = y - rect.height - pad;
  el.style.left = Math.max(8, left) + "px";
  el.style.top = Math.max(8, top) + "px";
}
function showTooltip(html, x, y) {
  const el = ensureTooltip();
  el.innerHTML = html;
  el.style.display = "block";
  positionTooltip(x, y);
}
function hideTooltip() {
  if (TOOLTIP_EL) TOOLTIP_EL.style.display = "none";
}

function attachRowTooltip(tbodyId, rowsById, buildHtml) {
  const tbody = $(tbodyId);
  if (!tbody) return;
  let lastRowId = null;
  tbody.addEventListener("mousemove", ev => {
    const tr = ev.target.closest("tr[data-row-id]");
    if (!tr) { hideTooltip(); lastRowId = null; return; }
    const rowId = tr.dataset.rowId;
    if (rowId !== lastRowId) {
      const row = rowsById.get(rowId);
      if (!row) { hideTooltip(); lastRowId = null; return; }
      showTooltip(buildHtml(row), ev.clientX, ev.clientY);
      lastRowId = rowId;
    } else {
      positionTooltip(ev.clientX, ev.clientY);
    }
  });
  tbody.addEventListener("mouseleave", () => { hideTooltip(); lastRowId = null; });
}

function renderObjective() {
  $("objective").innerHTML = `
    <div class="toolbar">
      <input id="objSearch" type="text" placeholder="Filter metabolite or exchange…">
    </div>
    <div class="table-wrap"><table id="objTable">
      <thead><tr>
        <th data-key="metabolite_id">Metabolite ID</th>
        <th data-key="metabolite_name">Metabolite Name</th>
        <th data-key="compartment">Compartment</th>
        <th data-key="coefficient">Coefficient</th>
        <th data-key="direct_exchange">Direct Exchange?</th>
        <th data-key="appears_in_reactions">In Reactions</th>
      </tr></thead>
      <tbody id="objBody"></tbody>
    </table></div>
  `;
  
  const redraw = (filtered) => {
    const q = $("objSearch").value.toLowerCase();
    const rows = filtered ? filtered : (DATA.objective_metabolites || []).filter(o =>
      !q || `${o.metabolite_id} ${o.metabolite_name} ${o.direct_exchange || ""}`.toLowerCase().includes(q)
    );
    
    $("objBody").innerHTML = rows.length > 0 ? rows.map(o => `<tr>
      <td><strong><a href="#met-${escapeHtml(o.metabolite_id)}" style="cursor:pointer;color:inherit;text-decoration:underline;">${escapeHtml(o.metabolite_id)}</a></strong></td>
      <td>${escapeHtml(o.metabolite_name || "—")}</td>
      <td><span class="badge">${escapeHtml(o.compartment || "—")}</span></td>
      <td>${fmt(o.coefficient, 8)}</td>
      <td>${o.direct_exchange ? `<strong>${escapeHtml(o.direct_exchange)}</strong>` : "—"}</td>
      <td>${o.appears_in_reactions}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="note">No objective metabolites found (or all filtered as common).</td></tr>`;
  };
  
  $("objSearch").addEventListener("input", () => redraw());
  redraw();
  setTimeout(() => makeSortable("objTable", DATA.objective_metabolites || [], redraw), 0);
}

function formatStoich(rows) {
  if (!rows || !rows.length) return "—";
  return rows.map(x => `${fmt(Math.abs(x.coefficient), 8)} × ${escapeHtml(x.name || x.id)} <span class="badge">${escapeHtml(x.id)} [${escapeHtml(x.compartment || "?")}]</span>`).join("<br>");
}

function subsystems(rows) {
  return [...new Set(rows.map(r => r.subsystem).filter(Boolean))].sort();
}

// Compact "code" badge that shows the friendly compartment name on hover,
// so tables stay narrow while the mapping from earlier ("map the compartment
// code to a friendly name") is still one hover away wherever a compartment
// shows up.
function compartmentBadges(compartmentIds) {
  const ids = (compartmentIds || []).filter(Boolean);
  if (!ids.length) return "—";
  return ids.map(id =>
    `<span class="badge" title="${escapeHtml(COMP_NAMES[id] || id)}">${escapeHtml(id)}</span>`
  ).join(" ");
}

// ---------------------------------------------------------------------------
// Exchange debugging (on-request, after the initial analysis).
//
// Trace, for each essential exchange the user picks, the active-flux path
// from the exchanged metabolite through to biomass — once with the exchange
// active ("before KO") and once with it reversibly knocked out ("after KO"),
// so the two can be compared side by side.
// ---------------------------------------------------------------------------

function showDebugModal(essentialExchanges) {
  $("debugTab").style.display = "";
  renderDebugSelection(essentialExchanges);
  $("debugTab").click();
}

function renderDebugSelection(essentialExchanges) {
  const rows = essentialExchanges.map(e => `
    <label class="debug-check-row">
      <input type="checkbox" class="debugExCheck" value="${escapeHtml(e.id)}" checked>
      <span class="debug-check-id">${escapeHtml(e.id)}</span>
      <span class="debug-check-name">${escapeHtml(join(e.metabolite_names) || join(e.metabolites))}</span>
    </label>`).join("");

  $("debug").innerHTML = `
    <div class="note">
      Trace active flux from each selected essential exchange's metabolite through to biomass — once with the
      exchange active (before knockout) and once with it blocked (after knockout). Only reactions carrying
      non-zero flux in the current FBA solution are followed, so a missing path can mean the metabolite is
      rerouted differently after knockout, or that no active route to biomass remains.
    </div>
    <div class="toolbar">
      <button id="debugSelectAll" type="button">Select all</button>
      <button id="debugSelectNone" type="button">Select none</button>
      <span id="debugSelCount"></span>
      <label class="debug-toggle">
        <input type="checkbox" id="debugHideTrivial" checked>
        Hide common cofactors (H, H2O, ATP/ADP, NAD(H), Pi…)
      </label>
      <button id="debugRunBtn" type="button" style="margin-left:auto;">Run debugging analysis</button>
    </div>
    <div class="debug-select-list">${rows || '<div class="note">No essential exchanges to debug.</div>'}</div>
    <div id="debugStatus" class="debug-status"></div>
    <div id="debugResults"></div>
  `;

  const checks = () => Array.from(document.querySelectorAll(".debugExCheck"));
  const updateCount = () => {
    const n = checks().filter(c => c.checked).length;
    $("debugSelCount").textContent = `${n} selected`;
  };
  checks().forEach(c => c.addEventListener("change", updateCount));
  updateCount();

  const selectAllBtn = $("debugSelectAll");
  const selectNoneBtn = $("debugSelectNone");
  if (selectAllBtn) selectAllBtn.addEventListener("click", () => { checks().forEach(c => c.checked = true); updateCount(); });
  if (selectNoneBtn) selectNoneBtn.addEventListener("click", () => { checks().forEach(c => c.checked = false); updateCount(); });

  const runBtn = $("debugRunBtn");
  if (runBtn) {
    runBtn.addEventListener("click", () => {
      const selected = checks().filter(c => c.checked).map(c => c.value);
      if (!selected.length) {
        $("debugStatus").innerHTML = `<span class="error">Select at least one exchange to debug.</span>`;
        return;
      }
      const hideTrivial = $("debugHideTrivial") ? $("debugHideTrivial").checked : true;
      runDebugAnalysis(selected, hideTrivial);
    });
  }
}

async function runDebugAnalysis(exchangeIds, hideTrivial) {
  const btn = $("debugRunBtn");
  if (btn) btn.disabled = true;
  $("debugStatus").textContent = `Tracing ${exchangeIds.length} exchange(s) to biomass (before and after knockout)…`;
  $("debugResults").innerHTML = "";

  try {
    if (!MODEL_ID) throw new Error("Model session expired — please re-run the initial analysis.");

    const res = await fetch("/api/debug-exchanges", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model_id: MODEL_ID,
        exchange_ids: exchangeIds,
        exclude_trivial_metabolites: hideTrivial,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "Debug analysis failed");

    $("debugStatus").textContent = `Done. Biomass reaction: ${data.biomass_reaction}. ${data.trivial_metabolite_rule || ""}`;
    renderDebugResults(data.results);
  } catch (err) {
    $("debugStatus").innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderDebugResults(results) {
  const cards = Object.entries(results).map(([exchId, r]) => {
    if (r.error) {
      return `<div class="debug-card">
        <h3>${escapeHtml(exchId)}</h3>
        <div class="error">${escapeHtml(r.error)}</div>
      </div>`;
    }
    const gi = r.growth_impact || {};
    return `<div class="debug-card">
      <h3>${escapeHtml(r.exchange_id)} <span class="stoich">${escapeHtml(r.exchange_name || "")}</span></h3>
      <dl class="kv">
        <dt>Exchanged metabolite</dt>
        <dd>${escapeHtml(r.internal_metabolite.name || r.internal_metabolite.id)} <span class="badge">${escapeHtml(r.internal_metabolite.id)}</span></dd>
        <dt>Biomass reaction</dt><dd>${escapeHtml(r.biomass_reaction)}</dd>
        <dt>WT growth</dt><dd>${fmt(gi.wt_growth)}</dd>
        <dt>Growth after KO</dt>
        <dd>${fmt(gi.ko_growth)}${gi.ko_growth_fraction == null ? "" : ` (${fmt(gi.ko_growth_fraction * 100, 2)}% of WT)`}</dd>
      </dl>
      <div class="debug-compare">
        <div class="debug-col">
          <h4>Before KO</h4>
          ${renderDebugTrace(r.before_ko)}
        </div>
        <div class="debug-col">
          <h4>After KO</h4>
          ${renderDebugTrace(r.after_ko)}
        </div>
      </div>
    </div>`;
  }).join("");

  $("debugResults").innerHTML = cards || `<div class="note">No results.</div>`;
}

function renderDebugTrace(trace) {
  if (!trace) return `<div class="note">No data.</div>`;
  if (trace.status !== "success") {
    return `<div class="note">FBA status: ${escapeHtml(trace.status)}${trace.error ? " — " + escapeHtml(trace.error) : ""}</div>`;
  }
  const header = `<div class="stoich">Exchange flux: ${fmt(trace.exchange_flux)} · Biomass flux: ${fmt(trace.biomass_flux)} · Active reactions: ${trace.num_active_reactions}</div>`;
  if (!trace.paths || !trace.paths.length) {
    return header + `<div class="note">No active-flux pathway to biomass found.</div>`;
  }
  const paths = trace.paths.map((p, i) => {
    const steps = p.reactions.map((rxn, idx) => {
      const met = p.metabolites[idx + 1];
      return `<div class="debug-step">
        <span class="debug-arrow">↓</span> <span class="badge">${escapeHtml(rxn.id)}</span> flux=${fmt(rxn.flux)}
        <div class="debug-met">→ ${escapeHtml(met.name || met.id)} <span class="stoich">${escapeHtml(met.id)}</span></div>
      </div>`;
    }).join("");
    return `<div class="debug-path">
      <div class="debug-path-title">Path ${i + 1}</div>
      <div class="debug-met">${escapeHtml(p.metabolites[0].name || p.metabolites[0].id)} <span class="stoich">${escapeHtml(p.metabolites[0].id)}</span></div>
      ${steps}
    </div>`;
  }).join("");
  return header + paths;
}

// ---------------------------------------------------------------------------
// Pathway Tracer — a general-purpose module, separate from exchange
// essentiality/debugging. Traces active-flux paths between ANY starting
// point (a metabolite, or a reaction resolved to its current-flux products)
// and ANY target metabolite, for however many BFS "levels" the user asks
// for. Never runs on its own — the tab renders an empty selection form as
// soon as the model loads, and nothing is computed until "Run trace" is
// clicked. Two view modes: a text/words view (same style as the exchange
// debugger) and an interactive, filterable Cytoscape.js network view.
// ---------------------------------------------------------------------------

let TRACER_RESULT = null;
let TRACER_CY = null;
let TRACER_KO_VIEW = "before";     // "before" | "after" — which snapshot the words/network view renders
let TRACER_HIGHLIGHT_KO = true;    // whether changed reactions are called out (badge / edge color)

function renderTracer() {
  const metOptions = (DATA.metabolites || []).map(m => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name || "")}</option>`).join("");
  const rxnOptions = (DATA.reactions || []).map(r => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.name || "")}</option>`).join("");

  $("tracer").innerHTML = `
    <div class="note">
      Trace active-flux pathways between any starting point and any target metabolite — not tied to biomass or
      exchanges. Only reactions carrying non-zero flux in the current FBA solution are followed, and common
      cofactors can be hidden from the trace, same as exchange debugging. Nothing runs automatically — pick a
      start and target below and click "Run trace".
    </div>
    <datalist id="tracerMetList">${metOptions}</datalist>
    <datalist id="tracerRxnList">${rxnOptions}</datalist>
    <datalist id="tracerKoList">${metOptions}${rxnOptions}</datalist>
    <div class="tracer-form">
      <div class="tracer-field">
        <label>Start from</label>
        <select id="tracerStartType">
          <option value="metabolite">Metabolite</option>
          <option value="reaction">Reaction</option>
        </select>
      </div>
      <div class="tracer-field tracer-field-wide">
        <label id="tracerStartLabel">Starting metabolite</label>
        <input id="tracerStartId" type="text" list="tracerMetList" placeholder="Search metabolite ID or name…">
      </div>
      <div class="tracer-field tracer-field-wide">
        <label>Target metabolite</label>
        <input id="tracerTargetId" type="text" list="tracerMetList" placeholder="Search metabolite ID or name…">
      </div>
      <div class="tracer-field">
        <label>Max levels</label>
        <input id="tracerMaxLevels" type="number" min="1" max="100" value="15">
      </div>
      <div class="tracer-field">
        <label>Max paths</label>
        <input id="tracerMaxPaths" type="number" min="1" max="100" value="10">
      </div>
      <div class="tracer-field">
        <label>&nbsp;</label>
        <label class="debug-toggle tracer-inline-toggle">
          <input type="checkbox" id="tracerHideTrivial" checked>
          Hide common cofactors
        </label>
      </div>
      <div class="tracer-field">
        <label>View as</label>
        <div class="tracer-view-toggle">
          <label><input type="radio" name="tracerView" value="text" checked> Words</label>
          <label><input type="radio" name="tracerView" value="network"> Network</label>
        </div>
      </div>
      <div class="tracer-field tracer-field-wide">
        <label>Compare knockout (optional)</label>
        <input id="tracerKnockouts" type="text" list="tracerKoList" placeholder="e.g. EX_glc_D_e, atp_c — comma separated">
      </div>
      <div class="tracer-field">
        <label>&nbsp;</label>
        <button id="tracerRunBtn" type="button">Run trace</button>
      </div>
    </div>
    <div class="tracer-ko-hint stoich">
      Leave blank for a single trace. List one or more reaction or metabolite ids to also trace the same
      start → target with them knocked out, so you can toggle before/after and see which reactions in the
      traced paths changed flux. A reaction is blocked directly; a metabolite is "knocked out" by blocking
      every reaction it participates in (can be a large effect for a highly-connected metabolite).
    </div>
    <div id="tracerStatus" class="debug-status"></div>
    <div id="tracerResults"></div>
  `;

  const startTypeSel = $("tracerStartType");
  startTypeSel.addEventListener("change", () => {
    const isRxn = startTypeSel.value === "reaction";
    $("tracerStartLabel").textContent = isRxn ? "Starting reaction" : "Starting metabolite";
    $("tracerStartId").setAttribute("list", isRxn ? "tracerRxnList" : "tracerMetList");
    $("tracerStartId").placeholder = isRxn ? "Search reaction ID or name…" : "Search metabolite ID or name…";
    $("tracerStartId").value = "";
  });

  $("tracerRunBtn").addEventListener("click", runPathwayTrace);
  document.querySelectorAll('input[name="tracerView"]').forEach(r => r.addEventListener("change", () => {
    if (TRACER_RESULT) renderTracerActiveView();
  }));
}

async function runPathwayTrace() {
  const startType = $("tracerStartType").value;
  const startId = $("tracerStartId").value.trim();
  const targetId = $("tracerTargetId").value.trim();
  const maxLevels = parseInt($("tracerMaxLevels").value, 10) || 15;
  const maxPaths = parseInt($("tracerMaxPaths").value, 10) || 10;
  const hideTrivial = $("tracerHideTrivial").checked;
  const knockouts = $("tracerKnockouts").value.split(",").map(s => s.trim()).filter(Boolean);

  if (!startId) { $("tracerStatus").innerHTML = `<span class="error">Choose a starting ${escapeHtml(startType)}.</span>`; return; }
  if (!targetId) { $("tracerStatus").innerHTML = `<span class="error">Choose a target metabolite.</span>`; return; }
  if (!MODEL_ID) { $("tracerStatus").innerHTML = `<span class="error">Model session expired — please re-run the initial analysis.</span>`; return; }

  const btn = $("tracerRunBtn");
  btn.disabled = true;
  $("tracerStatus").textContent = knockouts.length ? "Tracing pathway and comparing knockout…" : "Tracing pathway…";
  $("tracerResults").innerHTML = "";
  hideTooltip();
  if (TRACER_CY) { TRACER_CY.destroy(); TRACER_CY = null; }
  TRACER_KO_VIEW = "before";
  TRACER_HIGHLIGHT_KO = true;

  try {
    const res = await fetch("/api/trace-pathway", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model_id: MODEL_ID,
        start_type: startType,
        start_id: startId,
        target_metabolite_id: targetId,
        max_levels: maxLevels,
        max_paths: maxPaths,
        exclude_trivial_metabolites: hideTrivial,
        knockouts,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "Pathway trace failed");

    TRACER_RESULT = data;
    const starts = data.start_metabolites || [];
    const startLabel = starts.length > 1 ? `${starts.length} starting metabolites` : (starts[0] || {}).id || startId;
    let statusMsg = `Found ${data.paths.length} path(s) from ${startLabel} to ${data.target_metabolite.id} ` +
      `(max ${data.max_levels} levels). Active reactions in solution: ${data.num_active_reactions}.`;
    if (data.knockout) {
      if (data.knockout.error) {
        statusMsg += ` Knockout comparison could not run: ${data.knockout.error}`;
      } else {
        const gi = data.knockout.growth_impact || {};
        statusMsg += ` Knockout comparison ready — WT growth ${fmt(gi.wt_growth)} → KO growth ${fmt(gi.ko_growth)}` +
          `${gi.ko_growth_fraction == null ? "" : ` (${fmt(gi.ko_growth_fraction * 100, 1)}% retained)`}.`;
      }
    }
    $("tracerStatus").textContent = statusMsg;
    renderTracerResults();
  } catch (err) {
    TRACER_RESULT = null;
    $("tracerStatus").innerHTML = `<span class="error">${escapeHtml(err.message)}</span>`;
    $("tracerResults").innerHTML = "";
  } finally {
    btn.disabled = false;
  }
}

function renderTracerResults() {
  if (!TRACER_RESULT) return;
  $("tracerResults").innerHTML = `<div id="tracerKoToolbar"></div><div id="tracerViewContent"></div>`;
  renderTracerKoToolbar();
  renderTracerActiveView();
}

// The optional before/after-knockout comparison toolbar: growth-impact
// summary, a Before KO / After KO toggle, and a "highlight changed
// reactions" checkbox. Rendered once per trace run — the view toggles
// below only re-render #tracerViewContent, not this toolbar, so the user's
// radio/checkbox choices aren't lost while switching Words/Network.
function renderTracerKoToolbar() {
  const el = $("tracerKoToolbar");
  const ko = TRACER_RESULT && TRACER_RESULT.knockout;
  if (!ko) { el.innerHTML = ""; return; }

  if (ko.error) {
    el.innerHTML = `<div class="note error">Knockout comparison: ${escapeHtml(ko.error)}</div>`;
    return;
  }

  const targetsLabel = (ko.targets || []).map(t => `${escapeHtml(t.id)} (${escapeHtml(t.type)})`).join(", ");
  const blockedLabel = (ko.blocked_reactions || []).join(", ");
  const gi = ko.growth_impact || {};
  const hasAfter = !!(ko.after_ko && !ko.after_ko.error);

  el.innerHTML = `
    <div class="tracer-ko-toolbar">
      <div class="tracer-ko-summary">
        <strong>Knockout comparison:</strong> ${targetsLabel} → blocked reaction(s): <code>${escapeHtml(blockedLabel || "—")}</code>.
        WT growth ${fmt(gi.wt_growth)} → KO growth ${fmt(gi.ko_growth)}${gi.ko_growth_fraction == null ? "" : ` (${fmt(gi.ko_growth_fraction * 100, 1)}% retained)`}.
        ${ko.not_found && ko.not_found.length ? `<br><span class="stoich">Not found in model, skipped: ${escapeHtml(ko.not_found.join(", "))}</span>` : ""}
        ${!hasAfter && ko.after_ko && ko.after_ko.error ? `<br><span class="stoich">After-KO trace: ${escapeHtml(ko.after_ko.error)}</span>` : ""}
      </div>
      <div class="tracer-ko-controls">
        <div class="tracer-view-toggle">
          <label><input type="radio" name="tracerKoView" value="before" ${TRACER_KO_VIEW === "before" ? "checked" : ""}> Before KO</label>
          <label><input type="radio" name="tracerKoView" value="after" ${TRACER_KO_VIEW === "after" ? "checked" : ""} ${hasAfter ? "" : "disabled"}> After KO</label>
        </div>
        <label class="debug-toggle tracer-inline-toggle">
          <input type="checkbox" id="tracerHighlightKo" ${TRACER_HIGHLIGHT_KO ? "checked" : ""}>
          Highlight KO-affected reactions
        </label>
      </div>
    </div>
  `;

  document.querySelectorAll('input[name="tracerKoView"]').forEach(r => r.addEventListener("change", ev => {
    TRACER_KO_VIEW = ev.target.value;
    renderTracerActiveView();
  }));
  const highlightCb = $("tracerHighlightKo");
  if (highlightCb) highlightCb.addEventListener("change", () => {
    TRACER_HIGHLIGHT_KO = highlightCb.checked;
    renderTracerActiveView();
  });
}

// The paths currently on display: the baseline ("before KO") trace, or —
// when the toggle is set to "after" and an after-KO trace actually
// succeeded — the after-KO trace's paths instead. Everything else
// (target metabolite, start type/id) is shared between the two, so only
// "paths" and "start_metabolites" need swapping.
function getActiveTracerData() {
  const base = TRACER_RESULT;
  const ko = base && base.knockout;
  if (TRACER_KO_VIEW === "after" && ko && !ko.error && ko.after_ko && !ko.after_ko.error) {
    return Object.assign({}, base, {
      paths: ko.after_ko.paths,
      start_metabolites: ko.after_ko.start_metabolites || base.start_metabolites,
    });
  }
  return base;
}

function renderTracerActiveView() {
  const view = (document.querySelector('input[name="tracerView"]:checked') || {}).value || "text";
  hideTooltip();
  if (TRACER_CY) { TRACER_CY.destroy(); TRACER_CY = null; }
  const data = getActiveTracerData();
  if (view === "network") {
    renderTracerNetworkView(data);
  } else {
    renderTracerTextView(data);
  }
}

// Per-reaction before/after-KO summary, used by both the words view (as an
// inline badge) and the network view (as an edge color class). Returns null
// when there's no knockout comparison, highlighting is off, or this
// reaction isn't one of the ones the comparison tracked.
function tracerFluxDelta(rxnId) {
  if (!TRACER_HIGHLIGHT_KO) return null;
  const ko = TRACER_RESULT && TRACER_RESULT.knockout;
  if (!ko || ko.error || !ko.flux_comparison) return null;
  return ko.flux_comparison[rxnId] || null;
}

function fluxDeltaBadge(rxnId) {
  const fc = tracerFluxDelta(rxnId);
  if (!fc) return "";
  if (fc.active_before && !fc.active_after) {
    return `<span class="badge bad">KO: blocked (${fmt(fc.before_flux)} → 0)</span>`;
  }
  if (!fc.active_before && fc.active_after) {
    return `<span class="badge good">KO: new route (0 → ${fmt(fc.after_flux)})</span>`;
  }
  if (Math.abs(fc.after_flux - fc.before_flux) > 1e-6) {
    return `<span class="badge">KO: ${fmt(fc.before_flux)} → ${fmt(fc.after_flux)}</span>`;
  }
  return `<span class="badge good">KO: unaffected</span>`;
}

function fluxDeltaClass(rxnId) {
  const fc = tracerFluxDelta(rxnId);
  if (!fc) return "";
  if (fc.active_before && !fc.active_after) return "ko-removed";
  if (!fc.active_before && fc.active_after) return "ko-added";
  if (Math.abs(fc.after_flux - fc.before_flux) > 1e-6) return "ko-changed";
  return "";
}

function renderTracerTextView(data) {
  if (!data.paths || !data.paths.length) {
    $("tracerViewContent").innerHTML = `<div class="note">No active-flux pathway found from ${escapeHtml(data.start_id)} to ${escapeHtml(data.target_metabolite.id)} ${TRACER_KO_VIEW === "after" ? "(after knockout)" : ""}.</div>`;
    return;
  }
  const paths = data.paths.map((p, i) => {
    const steps = p.reactions.map((rxn, idx) => {
      const met = p.metabolites[idx + 1];
      return `<div class="debug-step">
        <span class="debug-arrow">↓</span> <span class="badge">${escapeHtml(rxn.id)}</span> flux=${fmt(rxn.flux)}
        ${rxn.subsystem ? `<span class="stoich"> · ${escapeHtml(rxn.subsystem)}</span>` : ""}
        ${fluxDeltaBadge(rxn.id)}
        <div class="debug-met">→ ${escapeHtml(met.name || met.id)} <span class="stoich">${escapeHtml(met.id)}</span></div>
      </div>`;
    }).join("");
    return `<div class="debug-path">
      <div class="debug-path-title">Path ${i + 1}</div>
      <div class="debug-met">${escapeHtml(p.metabolites[0].name || p.metabolites[0].id)} <span class="stoich">${escapeHtml(p.metabolites[0].id)}</span></div>
      ${steps}
    </div>`;
  }).join("");
  $("tracerViewContent").innerHTML = `<div class="debug-col tracer-text-col">${paths}</div>`;
}

function renderTracerNetworkView(data) {
  const showKoLegend = TRACER_HIGHLIGHT_KO && TRACER_RESULT && TRACER_RESULT.knockout && !TRACER_RESULT.knockout.error;
  $("tracerViewContent").innerHTML = `
    <div class="tracer-network-toolbar">
      <select id="tracerFilterSubsystem"><option value="">All subsystems</option></select>
      <label class="tracer-flux-filter">Min |flux| <input id="tracerFilterFlux" type="number" min="0" step="0.01" value="0"></label>
      <input id="tracerFilterSearch" type="text" placeholder="Highlight metabolite or reaction…">
      <span id="tracerNetCount" class="stoich"></span>
    </div>
    <div id="tracerCyContainer" class="tracer-cy-container"></div>
    <div class="tracer-legend">
      <span><i class="tracer-dot tracer-dot-start"></i> Start</span>
      <span><i class="tracer-dot tracer-dot-target"></i> Target</span>
      <span><i class="tracer-dot tracer-dot-mid"></i> Intermediate</span>
      ${showKoLegend ? `
        <span><i class="tracer-dot tracer-dot-ko-removed"></i> Removed by KO</span>
        <span><i class="tracer-dot tracer-dot-ko-added"></i> New route after KO</span>
        <span><i class="tracer-dot tracer-dot-ko-changed"></i> Flux changed</span>
      ` : ""}
      <span class="stoich">Hover a metabolite or reaction for details.</span>
    </div>
  `;

  if (!data.paths || !data.paths.length) {
    $("tracerCyContainer").innerHTML = `<div class="note">No active-flux pathway found from ${escapeHtml(data.start_id)} to ${escapeHtml(data.target_metabolite.id)} ${TRACER_KO_VIEW === "after" ? "(after knockout)" : ""}.</div>`;
    document.querySelectorAll(".tracer-network-toolbar select, .tracer-network-toolbar input").forEach(el => el.disabled = true);
    return;
  }

  const startIds = new Set((data.start_metabolites || []).map(m => m.id));
  const targetId = data.target_metabolite.id;

  const nodesById = new Map();
  const edges = [];
  const subsystems = new Set();

  data.paths.forEach((p, pathIdx) => {
    // Keep the full metabolite/reaction record from the API response (id,
    // name, compartment, formula, charge, reaction string, bounds, GPR,
    // type…) so the hover tooltip below has everything it needs without a
    // second request — the network view is just a different rendering of
    // the same data the text view already gets.
    p.metabolites.forEach(m => {
      if (!nodesById.has(m.id)) nodesById.set(m.id, m);
    });
    p.reactions.forEach((rxn, idx) => {
      const source = p.metabolites[idx].id;
      const target = p.metabolites[idx + 1].id;
      if (rxn.subsystem) subsystems.add(rxn.subsystem);
      edges.push(Object.assign({}, rxn, {
        id: `e_${pathIdx}_${idx}`,
        source, target,
        rxnId: rxn.id, rxnName: rxn.name || "",
        koClass: fluxDeltaClass(rxn.id),
      }));
    });
  });

  const elements = [];
  nodesById.forEach(n => {
    let role = "intermediate";
    if (startIds.has(n.id)) role = "start";
    if (n.id === targetId) role = "target";
    elements.push({ data: Object.assign({}, n, { label: n.name || n.id }), classes: role });
  });
  edges.forEach(e => {
    elements.push({
      data: Object.assign({}, e, {
        label: e.rxnId,
        flux: Math.abs(e.flux),
        rawFlux: e.flux,
      }),
      classes: e.koClass || "",
    });
  });

  const subsystemSelect = $("tracerFilterSubsystem");
  [...subsystems].sort().forEach(s => {
    const opt = document.createElement("option");
    opt.value = s; opt.textContent = s;
    subsystemSelect.appendChild(opt);
  });

  TRACER_CY = cytoscape({
    container: $("tracerCyContainer"),
    elements,
    style: [
      { selector: "node", style: {
        "label": "data(label)", "font-size": 9, "text-valign": "bottom", "text-margin-y": 4,
        "text-wrap": "ellipsis", "text-max-width": "70px",
        "width": 26, "height": 26, "background-color": "#94a3b8", "border-width": 2, "border-color": "#64748b",
      }},
      { selector: "node.start", style: { "background-color": "#2563eb", "border-color": "#1e40af", "width": 32, "height": 32 } },
      { selector: "node.target", style: { "background-color": "#047857", "border-color": "#065f46", "width": 32, "height": 32 } },
      { selector: "node.tracer-dimmed", style: { "opacity": 0.15 } },
      { selector: "node.tracer-highlighted", style: { "border-color": "#f59e0b", "border-width": 4 } },
      { selector: "edge", style: {
        "width": "mapData(flux, 0, 20, 1.5, 7)", "line-color": "#94a3b8", "target-arrow-color": "#94a3b8",
        "target-arrow-shape": "triangle", "curve-style": "bezier", "label": "data(label)",
        "font-size": 8, "color": "#475569", "text-background-color": "#ffffff", "text-background-opacity": 0.85,
        "text-rotation": "autorotate",
      }},
      { selector: "edge.tracer-dimmed", style: { "opacity": 0.08 } },
      { selector: "edge.tracer-highlighted", style: { "line-color": "#f59e0b", "target-arrow-color": "#f59e0b", "width": 4 } },
      // Before/after-knockout comparison overlay (see fluxDeltaClass) —
      // only applied to edges when a knockout was traced and "Highlight
      // KO-affected reactions" is checked.
      { selector: "edge.ko-removed", style: { "line-color": "#dc2626", "target-arrow-color": "#dc2626", "line-style": "dashed", "width": 3 } },
      { selector: "edge.ko-added", style: { "line-color": "#16a34a", "target-arrow-color": "#16a34a", "width": 4 } },
      { selector: "edge.ko-changed", style: { "line-color": "#d97706", "target-arrow-color": "#d97706", "width": 4 } },
      { selector: ".tracer-hidden", style: { "display": "none" } },
    ],
    layout: { name: "breadthfirst", roots: [...startIds], directed: true, spacingFactor: 1.25, padding: 24 },
    wheelSensitivity: 0.25,
  });

  attachTracerNetworkTooltips(TRACER_CY);

  $("tracerNetCount").textContent = `${nodesById.size} metabolites · ${edges.length} reaction hops`;

  const applyFilters = () => {
    const subVal = subsystemSelect.value;
    const fluxMin = parseFloat($("tracerFilterFlux").value) || 0;
    const search = $("tracerFilterSearch").value.trim().toLowerCase();

    TRACER_CY.edges().forEach(e => {
      const passSub = !subVal || e.data("subsystem") === subVal;
      const passFlux = e.data("flux") >= fluxMin;
      e.toggleClass("tracer-hidden", !(passSub && passFlux));
    });
    TRACER_CY.nodes().forEach(n => {
      const visibleEdges = n.connectedEdges().filter(e => !e.hasClass("tracer-hidden"));
      const isEndpoint = startIds.has(n.id()) || n.id() === targetId;
      n.toggleClass("tracer-hidden", !(visibleEdges.length > 0 || isEndpoint));
    });

    TRACER_CY.elements().removeClass("tracer-highlighted tracer-dimmed");
    if (search) {
      const visible = TRACER_CY.elements().not(".tracer-hidden");
      const matches = visible.filter(el =>
        (el.data("label") || "").toLowerCase().includes(search) ||
        (el.data("rxnName") || "").toLowerCase().includes(search) ||
        (el.id() || "").toLowerCase().includes(search)
      );
      if (matches.length) {
        visible.addClass("tracer-dimmed");
        matches.removeClass("tracer-dimmed").addClass("tracer-highlighted");
      }
    }
  };

  subsystemSelect.addEventListener("change", applyFilters);
  $("tracerFilterFlux").addEventListener("input", applyFilters);
  $("tracerFilterSearch").addEventListener("input", applyFilters);
  if (!subsystems.size) subsystemSelect.disabled = true;
}

// Hover tooltip content for a network node (metabolite) / edge (reaction) —
// reuses the same fixed-position tooltip element as the exchange table
// (ensureTooltip/showTooltip/positionTooltip/hideTooltip above).
function buildTracerNodeTooltipHtml(d) {
  return `
    <div class="tooltip-title">${escapeHtml(d.name || d.id)}</div>
    <div class="tooltip-row"><span class="tooltip-label">ID</span><code>${escapeHtml(d.id)}</code></div>
    <div class="tooltip-row"><span class="tooltip-label">Compartment</span>${escapeHtml(d.compartment_name || d.compartment || "—")}${d.compartment ? ` <span class="stoich">(${escapeHtml(d.compartment)})</span>` : ""}</div>
    <div class="tooltip-row"><span class="tooltip-label">Formula</span>${escapeHtml(d.formula || "—")}</div>
    <div class="tooltip-row"><span class="tooltip-label">Charge</span>${d.charge === null || d.charge === undefined ? "—" : d.charge}</div>
  `;
}

function buildTracerEdgeTooltipHtml(d) {
  return `
    <div class="tooltip-title">${escapeHtml(d.rxnId)}${d.rxnName ? ` — ${escapeHtml(d.rxnName)}` : ""}</div>
    <div class="tooltip-row"><span class="tooltip-label">Reaction</span><code>${escapeHtml(d.reaction_string || "—")}</code></div>
    <div class="tooltip-row"><span class="tooltip-label">Subsystem</span>${escapeHtml(d.subsystem || "—")}</div>
    <div class="tooltip-row"><span class="tooltip-label">Type</span>${escapeHtml(d.type || "—")}</div>
    <div class="tooltip-row"><span class="tooltip-label">GPR</span>${escapeHtml(d.gene_reaction_rule || "—")}</div>
    <div class="tooltip-row"><span class="tooltip-label">Bounds</span>[${fmt(d.lower_bound, 2)}, ${fmt(d.upper_bound, 2)}]</div>
    <div class="tooltip-row"><span class="tooltip-label">Flux (this trace)</span>${fmt(d.rawFlux)}</div>
  `;
}

function attachTracerNetworkTooltips(cy) {
  const showFor = (ev, buildHtml) => {
    const orig = ev.originalEvent;
    const x = orig ? orig.clientX : 0;
    const y = orig ? orig.clientY : 0;
    showTooltip(buildHtml(ev.target.data()), x, y);
  };
  cy.on("mouseover", "node", ev => showFor(ev, buildTracerNodeTooltipHtml));
  cy.on("mouseover", "edge", ev => showFor(ev, buildTracerEdgeTooltipHtml));
  cy.on("mousemove", "node, edge", ev => {
    const orig = ev.originalEvent;
    if (orig) positionTooltip(orig.clientX, orig.clientY);
  });
  cy.on("mouseout", "node, edge", () => hideTooltip());
  cy.on("pan zoom drag", () => hideTooltip());
  // cy's own "mouseout" only fires when the pointer moves off an element
  // while still inside the container; a mouse move that jumps straight from
  // a node/edge to somewhere else on the page never reaches cytoscape at
  // all, so the tooltip needs its own listener on the container itself.
  const container = cy.container();
  if (container) container.addEventListener("mouseleave", () => hideTooltip());
}
