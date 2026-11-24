// Domainos control room. Vanilla JS, no build step.
// Polls /api/snapshot once a second and patches only the sections that changed.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const store = {
  get(k, d) { try { const v = localStorage.getItem(`domainos.${k}`); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`domainos.${k}`, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

const ui = {
  snap: null,
  selected: store.get("selected", null),
  track: null,          // track the user clicked; null = follow the action
  view: store.get("view", "platform"),
  controls: store.get("controls", true),
  expanded: new Set(),  // step keys the user opened
  collapsed: new Set(), // step keys the user closed
  presets: [],
  offline: false,
  pollTimer: null,
  tab: store.get("tab", "request"),   // "request" | "code" | "lab"
  labMode: store.get("labMode", "plain"),          // the runtime the Agent lab shows
  labScenario: store.get("labScenario", "happy"),  // the scenario its Run button uses
  labStep: null,                                   // walkthrough step; null = follow the run
  labContent: null,                                // config/agent_lab.yaml, via /api/lab/content
  hoodFrom: null,                                  // "lab" when a Code link brought us to Under the hood
  hoodLabOpen: store.get("hoodLabOpen", false),    // Agent lab group in the Under the hood list
  snippet: null,                      // snippet the user picked; null = follow the action
  code: null,                         // /api/code payload
};

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const t = (iso) => (iso ? new Date(iso).getTime() : null);
function fmtSecs(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
const dur = (ms) => (ms == null ? "" : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`);
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }) : "");
const ago = (iso) => `<span data-ago="${esc(iso || "")}"></span>`;          // filled by tick()
const until = (iso) => `<span data-until="${esc(iso || "")}"></span>`;
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function tick() {
  const now = Date.now();
  $$("[data-ago]").forEach((el) => { const v = t(el.dataset.ago); el.textContent = v ? fmtSecs((now - v) / 1000) : ""; });
  $$("[data-until]").forEach((el) => { const v = t(el.dataset.until); el.textContent = v ? fmtSecs(Math.max(0, (v - now) / 1000)) : ""; });
}

function patch(el, html) {
  if (!el) return false;
  if (el._html === html) return false;
  // Keep anything being typed (review comments) across re-renders.
  const saved = {};
  el.querySelectorAll("textarea[id], input[type=text][id]").forEach((x) => { saved[x.id] = x.value; });
  const focusId = el.contains(document.activeElement) ? document.activeElement.id : null;
  el.innerHTML = html;
  el._html = html;
  Object.entries(saved).forEach(([id, v]) => { const x = document.getElementById(id); if (x) x.value = v; });
  if (focusId) document.getElementById(focusId)?.focus();
  return true;
}

const ICONS = {
  ext: '<path d="M7 3h6v6M13 3 6.5 9.5M11 9v4H3V5h4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
  check: '<circle cx="8" cy="8" r="7" fill="var(--done)"/><path d="m4.8 8.2 2.2 2.2 4.2-4.6" fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  run: '<circle cx="8" cy="8" r="6" fill="none" stroke="var(--oven-wash)" stroke-width="2.5"/><path class="spin" d="M8 2a6 6 0 0 1 6 6" fill="none" stroke="var(--oven)" stroke-width="2.5" stroke-linecap="round"/>',
  blocked: '<circle cx="8" cy="8" r="7" fill="var(--bad)"/><path d="M8 4.2v4.6" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/><circle cx="8" cy="11.4" r="1.1" fill="#fff"/>',
  wait: '<circle cx="8" cy="8" r="6.2" fill="none" stroke="#B6BFCB" stroke-width="1.6" stroke-dasharray="2.4 2.2"/>',
  approval: '<circle cx="8" cy="8" r="7" fill="var(--wait)"/><path d="M8 4.5V8l2.3 1.6" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>',
  undo: '<circle cx="8" cy="8" r="7" fill="#8C96A5"/><path d="M5.3 7.2h4.2a2.2 2.2 0 1 1 0 4.4H7.6M5.3 7.2l1.8-1.8M5.3 7.2 7.1 9" fill="none" stroke="#fff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
  skip: '<circle cx="8" cy="8" r="7" fill="#C2C9D3"/><path d="M5 8h6" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>',
  x: '<path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  pending: '<circle cx="8" cy="8" r="6.2" fill="none" stroke="#C9D0D9" stroke-width="1.6"/>',
};
const icon = (name, cls = "glyph") => `<svg class="${cls}" viewBox="0 0 16 16" aria-hidden="true">${ICONS[name] || ""}</svg>`;
const jump = (href, label = "Open in Temporal") => (href ? `<a class="jump" href="${esc(href)}" target="_blank" rel="noopener">${label}${icon("ext", "")}</a>` : "");

function glyph(status) {
  return icon({
    completed: "check", running: "run", starting: "run", finishing: "run", compensating: "run",
    blocked: "blocked", failed: "blocked", undo_failed: "blocked", awaiting_approval: "approval", awaiting_review: "approval", waiting: "wait",
    rolled_back: "undo", skipped: "skip", cancelled: "skip", pending: "pending",
  }[status] || "pending");
}

const STATUS_TEXT = {
  in_progress: ["In progress", "chip-oven"], blocked: ["Needs attention", "chip-bad"], completed: ["Delivered", "chip-good"],
  rejected: ["Rejected", "chip-bad"], rolled_back: ["Rolled back", "chip-bad"], rolling_back: ["Rolling back", "chip-bad"],
  cancelled: ["Cancelled", ""], failed: ["Failed", "chip-bad"], running: ["Running", "chip-oven"],
};
const statusChip = (s) => { const [txt, cls] = STATUS_TEXT[s] || [s, ""]; return `<span class="chip ${cls}">${esc(txt)}</span>`; };

async function api(method, path, body) {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error((data && (data.detail || data.message)) || `${res.status} ${res.statusText}`);
  return data;
}

function toast(msg, bad = false) {
  const el = document.createElement("div");
  el.className = `toast${bad ? " bad" : ""}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 6000 : 3200);
}

async function act(btn, fn, okMsg) {
  if (btn) btn.disabled = true;
  try {
    await fn();
    if (okMsg) toast(okMsg);
    await poll(true);
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------
async function poll() {
  clearTimeout(ui.pollTimer);
  let next = 1000;
  try {
    const qs = new URLSearchParams();
    if (ui.selected) qs.set("request", ui.selected);
    if (ui.tab === "lab" && ui.view === "platform") qs.set("lab", "1");
    const res = await fetch(`/api/snapshot${qs.toString() ? `?${qs}` : ""}`, { cache: "no-store" });
    ui.snap = await res.json();
    ui.offline = false;
    const known = ui.snap.requests || [];
    if (known.length && (!ui.selected || (!ui.snap.selected && !known.some((r) => r.workflow_id === ui.selected)))) {
      ui.selected = known[0].workflow_id;   // first visit, or the stored request is gone
      store.set("selected", ui.selected);
      next = 50;
    }
    render();
  } catch {
    ui.offline = true;
    renderTopbar();
  }
  clearTimeout(ui.pollTimer);
  ui.pollTimer = setTimeout(poll, next);
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
function render() {
  document.body.classList.toggle("view-platform", ui.view === "platform");
  document.body.classList.toggle("view-requester", ui.view === "requester");
  document.body.classList.toggle("controls-open", ui.controls);
  $$(".view-switch button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === ui.view)));
  const s = ui.snap;
  if (s?.brand) {
    $("#brand-name").textContent = s.brand.name;
    $("#brand-product").textContent = s.brand.product;
  }
  renderTopbar();
  renderRail();
  renderTabs();
  renderMain();
  renderControls();
  tick();
}

function renderTabs() {
  const live = liveMarks().size > 0;
  const tabs = [["request", "Request"], ["code", "Under the hood"], ["lab", "Agent lab"]];
  patch($("#main-tabs"), tabs.map(([id, label]) => `<button role="tab" data-action="tab" data-tab="${id}" aria-selected="${ui.tab === id}">${label}${id === "code" && live ? `<span class="live-dot" title="Code on this tab is running right now"></span>` : ""}</button>`).join(""));
}

function renderTopbar() {
  const s = ui.snap;
  if (!s) return;
  const tc = s.temporal || {};
  const temporal = ui.offline
    ? `<span class="conn"><span class="dot dot-bad"></span>Demo server unreachable</span>`
    : tc.connected
      ? `<span class="conn"><span class="dot dot-good"></span>Temporal ${tc.cloud ? "Cloud" : ""} <span class="mono">${esc(tc.namespace)}</span></span> ${jump(tc.namespace_url, "Temporal UI")}`
      : `<span class="conn"><span class="dot dot-bad"></span>Temporal not connected</span>`;
  const ai = s.ai?.enabled
    ? `<span class="conn"><span class="dot dot-good"></span>Claude <span class="mono model">${esc(s.ai.model)}</span></span>`
    : `<span class="conn"><span class="dot dot-warn"></span>Claude off (keyword rules)</span>`;
  const ctl = ui.view === "platform" ? `<button class="btn btn-sm" data-action="toggle-controls">${ui.controls ? "Hide controls" : "Demo controls"}</button>` : "";
  patch($("#topbar-status"), `${temporal}${ai}${ctl}`);
}

function stageTicks(r) {
  const stages = ui.snap.stages.map((x) => x.id);
  const i = stages.indexOf(r.stage);
  const end = ["rejected", "rolled_back", "cancelled", "failed"].includes(r.status);
  return `<div class="ticks">${stages.map((_, k) => {
    if (r.status === "completed") return `<i class="on"></i>`;
    if (k < i) return `<i class="on"></i>`;
    if (k === i) return `<i class="${end || r.status === "blocked" ? "bad" : "now"}"></i>`;
    return "<i></i>";
  }).join("")}</div>`;
}

function renderRail() {
  const reqs = ui.snap?.requests || [];
  const html = reqs.length
    ? reqs.map((r) => `
      <button class="req" data-action="select-request" data-wf="${esc(r.workflow_id)}" aria-current="${r.workflow_id === ui.selected}">
        <div class="req-top"><span class="req-id">${esc(r.id)}</span><span class="muted" style="font-size:11.5px">${ago(r.submitted_at)}</span></div>
        <div class="req-title">${esc(r.title || "Reading the request")}</div>
        <div class="req-meta">${statusChip(r.status)}${r.tracks_total ? `<span class="num">${r.tracks_done}/${r.tracks_total} teams</span>` : ""}</div>
        ${stageTicks(r)}
      </button>`).join("")
    : `<div class="empty-rail">No requests yet.</div>`;
  patch($("#request-list"), html);
}

// --- main ------------------------------------------------------------------
const SECTIONS = ["banner", "head", "tracker", "attention", "outcome", "approvals", "map", "teams", "detail", "bottom"];

function renderMain() {
  const main = $("#main-body");
  const s = ui.snap;
  const r = s?.selected;
  const showCode = ui.view === "platform" && ui.tab === "code";
  const showLab = ui.view === "platform" && ui.tab === "lab";
  const mode = !s ? "loading" : showCode ? "code" : showLab ? "lab" : !s.temporal?.connected ? "disconnected" : r ? "request" : "empty";
  if (main.dataset.mode !== mode) {
    main.dataset.mode = mode;
    main._html = null;
    if (mode === "request") {
      main.innerHTML = SECTIONS.map((k) => `<div id="sec-${k}"></div>`).join("");
    }
  }
  if (mode === "loading") return patch(main, `<div class="empty"><p class="muted">Loading…</p></div>`);
  if (mode === "code") return patch(main, codeHtml());
  if (mode === "lab") {
    if (patch(main, labHtml(s))) requestAnimationFrame(drawLabArrows);
    return;
  }
  if (mode === "disconnected") return patch(main, disconnectedHtml(s));
  if (mode === "empty") return patch(main, emptyHtml());

  patch($("#sec-banner"), bannerHtml(s, r));
  patch($("#sec-head"), headHtml(r));
  patch($("#sec-tracker"), trackerHtml(r));
  patch($("#sec-attention"), attentionHtml(r));
  patch($("#sec-outcome"), outcomeHtml(r));
  patch($("#sec-approvals"), approvalsHtml(r));
  const mapChanged = patch($("#sec-map"), mapHtml(r));
  patch($("#sec-teams"), teamsHtml(r));
  if (patch($("#sec-detail"), detailHtml(r))) $$(".log[data-follow]").forEach((el) => { el.scrollTop = el.scrollHeight; });
  patch($("#sec-bottom"), bottomHtml(r));
  if (mapChanged || !ui._edgesDrawn) drawEdges(r);
}

function disconnectedHtml(s) {
  return `<div class="empty"><h1>Waiting for Temporal</h1>
    <p>The demo server can't reach Temporal at <span class="mono">${esc(s.temporal?.address)}</span> yet. It keeps retrying.</p>
    <p class="muted">${esc(s.temporal?.error || "")}</p>
    <p>For a local run: <span class="mono">temporal server start-dev</span>. For Temporal Cloud, set <span class="mono">TEMPORAL_ADDRESS</span>, <span class="mono">TEMPORAL_NAMESPACE</span> and <span class="mono">TEMPORAL_API_KEY</span> in <span class="mono">.env</span>.</p></div>`;
}

function emptyHtml() {
  return `<div class="empty">
    <h1>One request in. Every team, approval and ticket tracked to done.</h1>
    <p>Place a request for a new AWS account. Claude reads it, works out which teams and approvals it needs, and Temporal runs the whole thing: in parallel where it can, waiting on people where it has to, and recovering on its own when something breaks.</p>
    <div class="empty-presets">${ui.presets.map((p, i) => `<button class="preset" data-action="preset" data-i="${i}"><strong>${esc(p.label)}</strong><span>${esc(p.text)}</span></button>`).join("")}</div>
  </div>`;
}

function bannerHtml(s, r) {
  const out = [];
  if (r.stale) out.push(`<div class="banner">${icon("blocked")}<div><strong>Live status paused.</strong> No core worker is answering queries, so this is the last state a worker reported${r.stale_since ? ` (${ago(new Date(r.stale_since * 1000).toISOString())} ago)` : ""}. Nothing is lost: Temporal is holding every pending step on the server. Start a worker to pick up exactly where it left off.</div></div>`);
  return out.join("");
}

function headHtml(r) {
  const ticket = r.ticket ? `<span>Change <span class="mono">${esc(r.ticket.change_number)}</span>${r.ticket.change_state ? ` (${esc(r.ticket.change_state)})` : ""}</span>` : "";
  const canCancel = !["completed", "rejected", "rolled_back", "cancelled", "rolling_back", "failed"].includes(r.status);
  return `<div class="req-head">
    <div>
      <div class="req-sub"><span class="mono">${esc(r.id)}</span><span>${esc(r.requester || "")}</span><span>Submitted ${clock(r.submitted_at)}</span>${ticket}</div>
      <h1>${esc(r.title || "Reading the request…")}</h1>
    </div>
    <div class="req-links platform-only">
      ${jump(r.links?.workflow, "Request workflow")}
      ${jump(r.links?.family, "All workflows for this request")}
      ${r.history_length ? `<span class="muted" style="font-size:12px">${r.history_length} history events</span>` : ""}
      ${canCancel ? `<button class="btn btn-sm btn-bad" data-action="cancel">Cancel request</button>` : ""}
    </div>
  </div>`;
}

function trackerHtml(r) {
  const stages = ui.snap.stages;
  const idx = stages.findIndex((x) => x.id === r.stage);
  const failed = ["rejected", "rolled_back", "cancelled", "failed", "rolling_back"].includes(r.status);
  const tone = r.headline?.tone;
  const cells = stages.map((st, i) => {
    let cls = "pending";
    if (r.status === "completed") cls = i === stages.length - 1 ? "done final-good" : "done";
    else if (i < idx) cls = "done";
    else if (i === idx) cls = failed || (tone === "bad" && ui.view === "platform") ? "bad" : tone === "wait" ? "wait" : "now";
    const time = r.stage_times?.[st.id];
    return `<div class="stage ${cls}">
      <div class="stage-bar"></div>
      <div><div class="stage-label">${esc(st.label)}</div><div class="stage-sub">${esc(st.pizza)}</div>
      <div class="stage-time">${time && cls !== "pending" ? clock(time) : "&nbsp;"}</div></div>
    </div>`;
  }).join("");
  const hh = r.headline || {};
  const h = ui.view === "requester" ? { text: hh.plain || hh.text, tone: hh.plain_tone || hh.tone, since: hh.since } : hh;
  const now = (r.parent_pending || []).map((p) => {
    const retry = p.next_attempt_at_future ? ` · retry in ${until(p.next_attempt_at)}` : "";
    const fail = p.last_failure ? ` · last error: ${esc(p.last_failure.type)}` : "";
    return `<span class="chip ${p.attempt > 1 ? "chip-oven" : ""}">${esc(ACTIVITY_LABEL[p.activity_type] || p.activity_type)} · attempt ${p.attempt}${p.last_worker ? ` · <span class="mono">${esc(p.last_worker)}</span>` : ""}${retry}${fail}</span>`;
  }).join("");
  return `<div class="tracker">${cells}</div>
    <div class="headline tone-${esc(h.tone || "info")}"><span>${esc(h.text || "")}</span>${h.since ? `<span class="since">${ago(h.since)}</span>` : ""}</div>
    ${now ? `<div class="now-strip platform-only">${now} ${jump(r.links?.pending, "Pending activities")}</div>` : ""}`;
}

const ACTIVITY_LABEL = {
  ai_parse_request: "Claude: reading the request", plan_request: "Building the plan", itsm_create_change: "Opening the change request",
  ai_risk_review: "Claude: briefing approvers", notify: "Notifying", itsm_update_change: "Updating the change request",
  verify_request: "Verifying the account", ai_completion_summary: "Claude: writing the hand-off note", compensate_step: "Undoing a step",
};

function sourceLine(meta) {
  if (!meta) return "";
  if (meta.source === "claude") return `<span class="src">Claude <span class="mono">${esc(meta.model || "")}</span>${meta.latency_ms ? ` · ${dur(meta.latency_ms)}` : ""}${meta.attempt > 1 ? ` · attempt ${meta.attempt}` : ""}</span>`;
  return `<span class="src">Keyword rules${meta.reason ? ` (${esc(meta.reason)})` : ""}</span>`;
}

const REVIEW_SUGGESTIONS = ["Drop the NTP rule; we use Amazon Time Sync", "Limit Oracle access to the app subnets only", "Add a short justification per rule"];

function rulesTable(rules) {
  if (!rules?.length) return `<p class="muted">No rules: nothing needs opening.</p>`;
  return `<table class="rules"><thead><tr><th>Rule</th><th>To</th><th>Port</th><th>Purpose</th></tr></thead><tbody>${rules.map((x) => `
    <tr><td class="mono">${esc(x.name)}</td><td>${esc(x.destination_name || "")} <span class="mono muted">${esc(x.destination)}</span></td>
    <td class="mono">${esc(x.protocol)}/${esc(x.port)}</td><td>${esc(x.purpose || "")}</td></tr>`).join("")}</tbody></table>`;
}

function reviewHtml(r) {
  return (r.reviews || []).map((rv) => {
    const ag = rv.agent || {};
    const d = ag.draft || {};
    const pol = d.policy;
    const st = ag.stats || {};
    const id = `review-${esc(ag.workflow_id)}`.replace(/[^\w-]/g, "_");
    const policy = pol ? (pol.compliant ? `<span class="chip chip-good">Passed policy check</span>` : `<span class="chip chip-bad">${pol.violations.length} policy issue(s)</span>`) : "";
    const warn = (pol?.warnings || []).map((w) => `<div class="muted" style="font-size:12px">${esc(w.rule)}: ${esc(w.problem)}</div>`).join("");
    return `<div class="review platform-only">
      <div class="review-head">
        <div><h3>Review the firewall rules Claude drafted</h3>
        <div class="muted" style="font-size:12.5px">${esc(rv.team)} · ${esc(ag.reviewer || "")} · round ${ag.round || 1} · ${plural(st.llm_calls || 0, "model turn")}, ${plural(st.tool_calls || 0, "tool call")}</div></div>
        <div>${policy}</div>
      </div>
      <div class="platform-only">
        ${rulesTable(d.rules)}
        ${d.rationale ? `<p class="rationale"><strong>Claude's rationale.</strong> ${esc(d.rationale)}</p>` : ""}${warn}
        <div class="review-actions">
          <textarea id="${id}" rows="2" placeholder="What should change? (only needed for Request changes)"></textarea>
          <div class="suggest">${REVIEW_SUGGESTIONS.map((x) => `<button type="button" data-action="suggest" data-target="${id}" data-text="${esc(x)}">${esc(x)}</button>`).join("")}</div>
          <div class="actions">
            <button class="btn btn-good" data-action="review" data-do="approve" data-wf="${esc(ag.workflow_id)}" data-input="${id}">Approve rules</button>
            <button class="btn" data-action="review" data-do="changes" data-wf="${esc(ag.workflow_id)}" data-input="${id}">Request changes</button>
            <button class="btn btn-bad" data-action="review" data-do="reject" data-wf="${esc(ag.workflow_id)}" data-input="${id}">Reject</button>
            <span style="flex:1"></span>${jump(ag.link, "Agent workflow")}
          </div>
        </div>
      </div>
    </div>`;
  }).join("");
}

function attentionHtml(r) {
  const blocked = r.tracks.filter((x) => x.status === "blocked");
  return reviewHtml(r) + blocked.map((tr) => {
    const step = tr.steps.find((s) => s.status === "blocked") || {};
    const err = step.error || tr.error || {};
    const d = step.diagnosis || tr.diagnosis;
    const where = `${esc(step.system_label || "")} (${step.location === "onprem" ? "on-prem" : "cloud"})`;
    const diag = d
      ? `<div class="diag">
          <div><h4>Likely cause</h4><p>${esc(d.root_cause)}</p></div>
          <div><h4>What it holds up</h4><p>${esc(d.impact)}</p></div>
          <div><h4>Suggested fix</h4><ol>${(d.operator_steps || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ol></div>
          <div><h4>What the requester sees</h4><p>${esc(d.requester_note)}</p><p class="diag-src" style="margin-top:8px">${sourceLine(d.meta)}</p></div>
        </div>`
      : `<p class="muted" style="margin-top:10px">${icon("run")} Claude is reading the failure…</p>`;
    const rec = d ? `<span class="rec">Claude recommends <strong>${esc(d.recommended_action)}</strong> (${esc(d.confidence)} confidence)</span>` : "";
    return `<div class="attention">
      <h3>Decision needed: ${esc(step.name || tr.blocked_step)}</h3>
      <div class="muted" style="font-size:12.5px">${esc(tr.name)} · ${where}${step.worker ? ` · last ran on <span class="mono">${esc(step.worker)}</span>` : ""}</div>
      <div class="err">${esc(err.type || "Error")}: ${esc(err.message || "")}</div>
      ${diag}
      <div class="actions platform-only">
        <button class="btn btn-primary" data-action="resolve" data-wf="${esc(tr.workflow_id)}" data-do="retry">Retry the step</button>
        <button class="btn" data-action="resolve" data-wf="${esc(tr.workflow_id)}" data-do="skip">Skip it</button>
        <button class="btn btn-bad" data-action="resolve" data-wf="${esc(tr.workflow_id)}" data-do="rollback">Roll back the request</button>
        ${rec}
        <span style="flex:1"></span>${jump(tr.link, "Track workflow")}
      </div>
    </div>`;
  }).join("");
}

function outcomeHtml(r) {
  if (r.status === "completed" && r.summary) {
    const o = r.outputs || {};
    const keys = [["account_id", "Account"], ["account_name", "Name"], ["vpc_cidr", "CIDR"], ["vpc_id", "VPC"], ["okta_group", "Okta group"], ["change_number", "Change"], ["ci_number", "CMDB"]];
    return `<div class="section"><div class="delivered">
      <h3>${esc(r.summary.headline)}</h3><p>${esc(r.summary.message)}</p>
      ${(r.summary.next_steps || []).length ? `<ol style="margin:0 0 6px;padding-left:18px">${r.summary.next_steps.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}
      <div class="outputs">${keys.filter(([k]) => o[k]).map(([k, l]) => `<div><span>${l}</span><span class="mono">${esc(o[k])}</span></div>`).join("")}</div>
      <div style="margin-top:10px">${sourceLine(r.summary.meta)}</div>
    </div></div>`;
  }
  if (["rejected", "rolled_back", "cancelled"].includes(r.status)) {
    const undone = r.tracks.filter((x) => x.status === "rolled_back").length;
    return `<div class="section"><div class="attention"><h3>${r.status === "rejected" ? "Rejected" : r.status === "cancelled" ? "Cancelled" : "Rolled back"}</h3>
      <p style="margin:4px 0 0">${esc(r.status_reason || "")}</p>
      <p class="muted" style="margin:6px 0 0">${undone ? `${plural(undone, "team")}' completed work was undone in reverse order.` : "Nothing had been provisioned yet."} The change request was closed.</p></div></div>`;
  }
  return "";
}

function approvalsHtml(r) {
  if (!r.approvals?.length) return "";
  const risk = r.risk;
  const briefing = risk ? `<div class="briefing platform-only">
      <div style="display:flex;justify-content:space-between;gap:10px;align-items:baseline">
        <strong>Briefing for approvers</strong><span><span class="chip ${risk.risk_level === "high" ? "chip-bad" : risk.risk_level === "medium" ? "chip-oven" : "chip-good"}">${esc(risk.risk_level)} risk</span></span>
      </div>
      <div>${esc(risk.summary)}</div>
      ${(risk.concerns || []).length ? `<ul>${risk.concerns.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      <div class="muted" style="font-size:12.5px">${esc(risk.recommendation || "")} · ${sourceLine(risk.meta)}</div>
    </div>` : "";
  const cards = r.approvals.map((a) => {
    let state;
    if (a.status === "approved") state = `<span class="chip chip-good">Approved</span>`;
    else if (a.status === "rejected") state = `<span class="chip chip-bad">Rejected</span>`;
    else state = `<span class="chip chip-wait">Waiting ${ago(a.requested_at)}</span>`;
    const decided = a.decided_by ? `<div class="appr-why">${esc(a.decided_by)} at ${clock(a.decided_at)}${a.comment ? `: ${esc(a.comment)}` : ""}</div>` : "";
    const gate = a.blocks !== "request" ? `<div class="appr-why">Only holds up: ${esc((r.tracks.find((x) => x.id === a.blocks) || {}).name || a.blocks)}</div>` : "";
    const esc8 = a.escalated ? `<div><span class="chip chip-oven">Escalated to ${esc(a.escalation)}</span></div>` : "";
    const btns = a.status === "pending" ? `<div class="actions platform-only" style="margin-top:6px">
        <button class="btn btn-good btn-sm" data-action="approve" data-id="${esc(a.id)}" data-who="${esc(a.approver)}" title="Acting as ${esc(a.approver)}">Approve</button>
        <button class="btn btn-bad btn-sm" data-action="reject" data-id="${esc(a.id)}" data-who="${esc(a.approver)}">Reject</button></div>` : "";
    return `<div class="appr">
      <div class="appr-top"><span class="appr-name">${esc(a.name)}</span>${state}</div>
      <div class="appr-who">${esc(a.approver)} · ${esc(a.role)}</div>
      ${a.reason ? `<div class="appr-why">Why: ${esc(a.reason)}</div>` : a.auto ? `<div class="appr-why">${esc(a.auto_reason)}</div>` : ""}
      ${gate}${esc8}${decided}${btns}
    </div>`;
  }).join("");
  const pending = r.approvals.filter((a) => a.status === "pending").length;
  if (!pending) {
    // Everything decided: collapse to one line so the provisioning map gets the space.
    const line = r.approvals.map((a) => `<span>${icon(a.status === "approved" ? "check" : "blocked")}<strong>${esc(a.name)}</strong><span class="muted">${esc(a.decided_by || "")}${a.decided_at ? ` at ${clock(a.decided_at)}` : ""}</span></span>`).join("");
    return `<div class="section"><div class="section-head"><h2>Approvals</h2><span class="hint">All decided</span></div>
      <div class="card"><div class="appr-compact">${line}</div>${briefing ? `<details class="briefing-more platform-only"><summary>Briefing Claude gave the approvers</summary>${briefing}</details>` : ""}</div></div>`;
  }
  return `<div class="section"><div class="section-head"><h2>Approvals</h2><span class="hint">${plural(pending, "decision")} outstanding</span></div>
    <div class="card">${briefing}<div class="approvals">${cards}</div></div></div>`;
}

// --- provisioning map --------------------------------------------------------
function nodeNow(tr) {
  switch (tr.status) {
    case "running": case "starting": case "finishing": return tr.current_step || "Starting";
    case "waiting": return `After ${tr.waiting_on.join(", ")}`;
    case "awaiting_approval": return `Needs ${tr.waiting_on[0] || "approval"}`;
    case "awaiting_review": return `Waiting on ${tr.team} review`;
    case "blocked": return `Blocked: ${tr.blocked_step || tr.current_step || ""}`;
    case "completed": return tr.started_at && tr.completed_at ? `Done in ${fmtSecs((t(tr.completed_at) - t(tr.started_at)) / 1000)}` : "Done";
    case "compensating": return "Undoing its work";
    case "rolled_back": return "Rolled back";
    case "cancelled": return "Not started";
    default: return "Queued";
  }
}

function nodeHtml(tr, selectedId) {
  const chips = [];
  if (tr.queued) chips.push(`<span class="chip chip-bad">no worker yet</span>`);
  if (tr.worker && ["running", "starting", "blocked"].includes(tr.status)) chips.push(`<span class="chip chip-ink"><span class="mono">${esc(tr.worker)}</span></span>`);
  if (tr.attempt > 1 && tr.status === "running") chips.push(`<span class="chip chip-oven">attempt ${tr.attempt}</span>`);
  if (tr.backoff_until) chips.push(`<span class="chip chip-oven">retry in ${until(tr.backoff_until)}</span>`);
  const loc = new Set(tr.steps.map((s) => s.location));
  if (loc.has("onprem")) chips.push(`<span class="chip">${loc.has("cloud") ? "hybrid" : "on-prem"}</span>`);
  return `<div class="node s-${esc(tr.status)}" role="button" tabindex="0" data-action="select-track" data-track="${esc(tr.id)}" data-node="${esc(tr.id)}" aria-pressed="${tr.id === selectedId}">
    <div class="node-top">${glyph(tr.status)}<span class="node-name">${esc(tr.name)}</span></div>
    <div class="node-team">${esc(tr.team)}</div>
    <div class="steps-bar">${tr.steps.map((s) => `<i class="${esc(s.status)}" title="${esc(s.name)}"></i>`).join("")}</div>
    <div class="node-now">${esc(nodeNow(tr))}</div>
    <div class="node-chips">${chips.join("")}</div>
    ${tr.link ? `<a class="node-link jump" href="${esc(tr.link)}" target="_blank" rel="noopener" title="Open this track in Temporal" aria-label="Open ${esc(tr.name)} in Temporal">${icon("ext", "")}</a>` : ""}
  </div>`;
}

function currentTrackId(r) {
  if (ui.track && r.tracks.some((x) => x.id === ui.track)) return ui.track;
  const pick = r.tracks.find((x) => x.status === "blocked") || r.tracks.find((x) => x.status === "awaiting_review")
    || r.tracks.find((x) => x.status === "running" && (x.retrying || x.backoff_until || x.queued))
    || r.tracks.find((x) => ["running", "starting"].includes(x.status)) || r.tracks.find((x) => x.status === "compensating");
  return (pick || r.tracks[0] || {}).id || null;
}

function mapHtml(r) {
  if (!r.tracks?.length) return "";
  const maxD = Math.max(0, ...r.tracks.map((x) => x.depth || 0));
  const sel = currentTrackId(r);
  const stageIdx = ui.snap.stages.findIndex((x) => x.id === r.stage);
  const reqAppr = (r.approvals || []).filter((a) => a.blocks === "request");
  const startOn = stageIdx >= 2 || r.status === "completed";
  const endOn = stageIdx >= 3 || r.status === "completed";
  const cols = [];
  cols.push(`<div class="map-col"><div class="node pseudo ${startOn ? "on" : ""}" data-node="__start"><div class="node-name">${startOn ? "Approved" : "Approvals"}</div><div class="node-team" style="color:inherit;opacity:.75">${plural(reqAppr.length, "gate")}</div></div></div>`);
  for (let d = 0; d <= maxD; d++) {
    cols.push(`<div class="map-col">${r.tracks.filter((x) => (x.depth || 0) === d).map((x) => nodeHtml(x, sel)).join("")}</div>`);
  }
  cols.push(`<div class="map-col"><div class="node pseudo ${endOn ? "on" : ""}" data-node="__end"><div class="node-name">Verify and hand off</div><div class="node-team" style="color:inherit;opacity:.75">fan-in</div></div></div>`);
  const running = r.tracks.filter((x) => ["running", "starting", "blocked", "awaiting_review", "compensating", "finishing"].includes(x.status)).length;
  const done = r.tracks.filter((x) => ["completed", "skipped"].includes(x.status)).length;
  const skipped = (r.skipped || []).length ? `<div class="skipped-note">Not needed for this request: ${r.skipped.map((x) => esc(x.name)).join(", ")}.</div>` : "";
  return `<div class="section platform-only">
    <div class="section-head"><h2>Provisioning map</h2><span class="hint">${r.tracks.length} teams · ${running} working · ${done} done · each box is a child workflow</span></div>
    <div class="card"><div class="map-wrap"><div class="map-canvas" id="map-canvas" style="position:relative">
      <svg class="map-edges" id="map-edges"></svg>
      <div class="map" id="map" style="grid-template-columns:minmax(78px,.55fr) repeat(${maxD + 1}, minmax(150px,1fr)) minmax(78px,.55fr)">${cols.join("")}</div>
    </div></div>
    ${skipped}
    <div class="map-legend"><span>${icon("check")} Done</span><span>${icon("run")} Working</span><span>${icon("approval")} Waiting on a person</span><span>${icon("wait")} Waiting on another team</span><span>${icon("blocked")} Needs a decision</span><span>${icon("undo")} Rolled back</span></div>
    </div></div>`;
}

function drawEdges(r) {
  const canvas = $("#map-canvas");
  const svg = $("#map-edges");
  if (!canvas || !svg || !r?.tracks) return;
  ui._edgesDrawn = true;
  const base = canvas.getBoundingClientRect();
  svg.setAttribute("width", canvas.scrollWidth);
  svg.setAttribute("height", canvas.scrollHeight);
  const box = (id) => {
    const el = canvas.querySelector(`[data-node="${CSS.escape(id)}"]`);
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return { l: b.left - base.left, r: b.right - base.left, y: b.top - base.top + b.height / 2 };
  };
  const byId = Object.fromEntries(r.tracks.map((x) => [x.id, x]));
  const hasChild = new Set(r.tracks.flatMap((x) => x.depends_on || []));
  const startOn = ui.snap.stages.findIndex((x) => x.id === r.stage) >= 2 || r.status === "completed";
  const edges = [];
  r.tracks.forEach((tr) => {
    const deps = tr.depends_on?.length ? tr.depends_on : ["__start"];
    deps.forEach((d) => edges.push([d, tr.id]));
    if (!hasChild.has(tr.id)) edges.push([tr.id, "__end"]);
  });
  const active = new Set(["running", "starting", "finishing", "compensating"]);
  svg.innerHTML = edges.map(([a, b]) => {
    const pa = box(a); const pb = box(b);
    if (!pa || !pb) return "";
    const src = byId[a]; const dst = byId[b];
    const srcDone = a === "__start" ? startOn : ["completed", "skipped"].includes(src?.status);
    let cls = srcDone ? "on" : "";
    if (srcDone && dst && active.has(dst.status)) cls = "hot";
    if (dst?.status === "blocked" && srcDone) cls = "bad";
    const x1 = pa.r; const x2 = pb.l; const dx = Math.max(16, (x2 - x1) / 2);
    return `<path class="${cls}" d="M${x1},${pa.y} C${x1 + dx},${pa.y} ${x2 - dx},${pb.y} ${x2},${pb.y}"/>`;
  }).join("");
}

// --- track detail -------------------------------------------------------------
function logLine(l) {
  const e = esc(l);
  if (/FAILED|UNREACHABLE|failed:/i.test(l)) return `<span class="l-fail">${e}</span>`;
  if (/^PLAY /.test(l)) return `<span class="l-play">${e}</span>`;
  if (/\] changed:/.test(l) || /SUCCEEDED|available|Created/.test(l)) return `<span class="l-changed">${e}</span>`;
  if (/\] ok:/.test(l)) return `<span class="l-ok">${e}</span>`;
  return e;
}

function stepOpen(key, s) {
  if (ui.collapsed.has(key)) return false;
  if (ui.expanded.has(key)) return true;
  return ["running", "blocked", "compensating"].includes(s.status);
}

function transcriptHtml(ag) {
  const st = ag.stats || {};
  const live = (ag.pending || []).map((p) => {
    const what = ["agent_llm_turn", "lc_llm_turn"].includes(p.activity_type) ? "Claude is working on the next turn"
      : ["agent_tool", "lc_tool"].includes(p.activity_type) ? "Running a tool call" : "Running a graph node";
    const retry = p.next_attempt_at_future ? ` · retry in ${until(p.next_attempt_at)}` : "";
    const fail = p.last_failure ? ` · last attempt failed: ${esc(p.last_failure.type)}` : "";
    return `<div class="tr-live">${icon("run")} ${what} · attempt ${p.attempt}${p.last_worker ? ` · <span class="mono">${esc(p.last_worker)}</span>` : ""}${retry}${fail}</div>`;
  }).join("");
  const rows = (ag.transcript || []).map((e) => {
    if (e.kind === "turn") {
      const src = e.source === "claude" ? `Claude${e.latency_ms ? ` · ${dur(e.latency_ms)}` : ""}` : "Offline agent";
      return `<li class="tr-turn"><div class="tr-meta"><strong>Turn ${e.n}</strong> · ${src}${e.worker ? ` · <span class="mono">${esc(e.worker)}</span>` : ""}${e.attempt > 1 ? ` · <span class="chip chip-oven">attempt ${e.attempt}</span>` : ""}</div>
        ${e.text ? `<div>${esc(e.text)}</div>` : ""}${e.tools?.length ? `<div class="tr-tools">${e.tools.map((n) => `<span class="chip">${esc(n)}</span>`).join("")}</div>` : ""}</li>`;
    }
    if (e.kind === "tool") {
      return `<li class="tr-tool"><span class="mono">${esc(e.name)}</span> ${esc(e.summary)}${e.attempt > 1 ? ` <span class="chip chip-oven">attempt ${e.attempt}</span>` : ""}</li>`;
    }
    if (e.kind === "review") {
      const verb = { approve: "approved", reject: "rejected", changes: "asked for changes" }[e.decision] || e.decision;
      return `<li class="tr-review"><strong>${esc(e.reviewer)}</strong> ${verb}${e.comment ? `: “${esc(e.comment)}”` : ""}</li>`;
    }
    return "";
  }).join("");
  return `<div class="transcript">
    <div class="tr-head"><strong>Agent run${ag.mode && ag.mode !== "plain" ? ` · ${esc(ui.snap?.frameworks?.[ag.mode]?.label || ag.mode)}` : ""}</strong><span class="muted">${plural(st.llm_calls || 0, "model turn")} · ${plural(st.tool_calls || 0, "tool call")}${st.input_tokens ? ` · ${(st.input_tokens + st.output_tokens).toLocaleString()} tokens` : ""} · round ${ag.round || 1}</span><span style="flex:1"></span>${jump(ag.link, "Agent workflow")}</div>
    <ol class="tr-list">${rows}</ol>${live}
  </div>`;
}

function detailHtml(r) {
  const id = currentTrackId(r);
  const tr = r.tracks?.find((x) => x.id === id);
  if (!tr) return "";
  const rows = tr.steps.map((s) => {
    const key = `${tr.workflow_id}|${s.id}`;
    const open = stepOpen(key, s);
    const lv = s.live || {};
    const worker = lv.worker_now || s.worker || "";
    const attempt = s.attempt || lv.attempt;
    let timeCell = "";
    if (s.duration_ms != null && ["completed", "rolled_back"].includes(s.status)) timeCell = dur(s.duration_ms);
    else if (s.started_at && ["running", "blocked"].includes(s.status)) timeCell = ago(lv.last_started_at || s.started_at);
    const head = `<button class="step-row" data-action="toggle-step" data-key="${esc(key)}" aria-expanded="${open}">
      ${glyph(s.status)}
      <span class="step-name">${esc(s.name)}</span>
      <span class="step-sys hide-sm"><span>${esc(s.system_label)}</span><span class="loc ${s.location === "onprem" ? "loc-onprem" : ""}">${s.location === "onprem" ? "on-prem" : "cloud"}</span></span>
      <span class="step-cell mono">${esc(worker)}</span>
      <span class="step-cell hide-sm">${attempt ? `attempt ${attempt}${lv.max_attempts ? ` of ${lv.max_attempts}` : ""}` : ""}</span>
      <span class="step-cell num">${timeCell}</span>
      <span class="step-cell hide-sm">${esc(s.status.replace("_", " "))}</span>
      <span class="step-cell">${open ? "−" : "+"}</span>
    </button>`;
    if (!open) return `<div class="step">${head}</div>`;
    const notes = [];
    if (lv.state === "scheduled" && !lv.next_attempt_at_future && (lv.waiting_s || 0) >= 3) notes.push(`Queued on <span class="mono">${esc(s.queue)}</span>: no worker has picked it up yet.`);
    if (lv.next_attempt_at_future) notes.push(`Retrying in ${until(lv.next_attempt_at)} (Temporal backoff).`);
    if (lv.last_failure) notes.push(`Previous attempt failed: ${esc(lv.last_failure.type)}: ${esc(lv.last_failure.message)}`);
    if (lv.last_heartbeat_at && s.status === "running") notes.push(`Last heartbeat ${ago(lv.last_heartbeat_at)} ago${lv.last_worker ? ` from <span class="mono">${esc(lv.last_worker)}</span>` : ""}.`);
    const log = (lv.log?.length ? lv.log : null) || (lv.heartbeat?.log?.length ? lv.heartbeat.log : null) || s.log || [];
    const out = Object.entries(s.output || {}).filter(([, v]) => v !== null && v !== "" && (typeof v !== "object" || (Array.isArray(v) && v.every((x) => typeof x !== "object"))));
    const hist = (s.history || []).map((h) => `<li>${clock(h.at)} · ${esc(h.event)}${h.by ? ` by ${esc(h.by)}` : ""}${h.error ? `: ${esc(h.error)}` : ""}</li>`).join("");
    return `<div class="step">${head}<div class="step-body">
      ${notes.length ? `<div class="live-note">${notes.join("<br>")}</div>` : ""}
      ${s.agent ? transcriptHtml(s.agent) : ""}
      ${s.error ? `<div class="err">${esc(s.error.type)}: ${esc(s.error.message)}</div>` : ""}
      ${log.length ? `<div class="log" ${s.status === "running" ? "data-follow" : ""}>${log.map(logLine).join("\n")}</div>` : ""}
      ${out.length ? `<dl class="kv">${out.map(([k, v]) => `<dt>${esc(k)}</dt><dd class="mono">${esc(Array.isArray(v) ? v.join(", ") : v)}</dd>`).join("")}</dl>` : ""}
      ${hist ? `<ul style="margin:0;padding-left:18px;font-size:12.5px;color:var(--ink-2)">${hist}</ul>` : ""}
    </div></div>`;
  }).join("");
  return `<div class="section platform-only"><div class="card">
    <div class="detail-head">
      <div><h3>${glyph(tr.status)} ${esc(tr.name)}</h3><div class="muted" style="font-size:12.5px;margin-top:2px">${esc(tr.team)} · ${plural(tr.steps.length, "step")}${tr.workflow_id ? ` · <span class="mono">${esc(tr.workflow_id)}</span>` : ""}</div></div>
      <div class="detail-links">${jump(tr.link, "Track workflow")}${jump(tr.pending_link, "Pending activities")}</div>
    </div>
    ${rows}
  </div></div>`;
}

// --- requester view -------------------------------------------------------------
function teamsHtml(r) {
  if (!r.tracks?.length) return "";
  const rows = r.tracks.map((tr) => {
    let state = "";
    switch (tr.status) {
      case "completed": state = `Done ${ago(tr.completed_at)} ago`; break;
      case "running": case "starting": case "finishing": state = `Working: ${esc(tr.current_step || "starting")}`; break;
      case "blocked": state = `Sorting out a problem`; break;
      case "awaiting_approval": state = `Waiting on ${esc(tr.waiting_on[0] || "approval")}`; break;
      case "awaiting_review": state = "Reviewing the rules Claude drafted"; break;
      case "waiting": state = `Starts after ${esc(tr.waiting_on.join(", "))}`; break;
      case "rolled_back": state = "Undone"; break;
      default: state = "Not started";
    }
    return `<li>${glyph(tr.status)}<div><div class="team-name">${esc(tr.team)}</div><div class="team-what">${esc(tr.name)}</div></div><div class="team-state">${state}</div></li>`;
  }).join("");
  return `<div class="section requester-only"><div class="section-head"><h2>Teams working on your request</h2><span class="hint">One request instead of ${r.tracks.length * 2}+ tickets</span></div>
    <div class="card"><ul class="teams">${rows}</ul></div></div>`;
}

// --- spec + events ------------------------------------------------------------
function bottomHtml(r) {
  const sp = r.spec;
  const spec = sp ? `<div class="card">
      <div class="section-head pad" style="margin:0;padding-bottom:0"><h2>What Claude understood</h2>${sourceLine(r.intake)}</div>
      <dl class="spec">
        <dt>Account</dt><dd class="mono">${esc(sp.account_name)}</dd>
        <dt>Environment</dt><dd>${esc(sp.environment)}</dd>
        <dt>Data</dt><dd>${esc(sp.data_classification)}</dd>
        <dt>On-prem access</dt><dd>${sp.onprem_connectivity ? `Yes${sp.onprem_targets?.length ? `: ${esc(sp.onprem_targets.join(", "))}` : ""}` : "No"}</dd>
        <dt>Network</dt><dd>${esc(sp.cidr_size)} in ${esc(sp.region)}</dd>
        <dt>Cost center</dt><dd>${esc(sp.cost_center)}, $${Number(sp.monthly_budget_usd || 0).toLocaleString()}/month</dd>
        <dt>Owners</dt><dd>${esc((sp.owners || []).join(", "))}</dd>
      </dl>
      ${(r.intake?.assumptions || []).length || (r.intake?.missing_info || []).length ? `<div class="assume">
        ${(r.intake.assumptions || []).length ? `Assumed:<ul>${r.intake.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : ""}
        ${(r.intake.missing_info || []).length ? `<div style="margin-top:6px">To confirm: ${esc(r.intake.missing_info.join(", "))}</div>` : ""}
      </div>` : ""}
    </div>` : `<div class="card pad muted">${icon("run")} Claude is reading the request…</div>`;
  const ev = (r.events || []).map((e) => `<li class="t-${esc(e.tone || "info")}"><time>${clock(e.t)}</time><span class="dot"></span><span>${esc(e.message)}</span></li>`).join("");
  return `<div class="section two-col">
    <div>${spec}</div>
    <div class="card"><div class="section-head pad" style="margin:0;padding-bottom:4px"><h2>What happened</h2><span class="hint platform-only">from workflow state</span></div><ul class="events">${ev}</ul></div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Under the hood: live code
// ---------------------------------------------------------------------------
function liveMarks() {
  const r = ui.snap?.selected;
  const m = new Set();
  if (!r || ["completed", "rejected", "rolled_back", "cancelled"].includes(r.status)) return m;
  if ((r.approvals || []).some((a) => a.status === "pending")) m.add("approval-wait");
  if (r.stage === "provisioning") m.add("fanout-wait");
  for (const tr of r.tracks || []) {
    if (tr.status === "blocked") m.add("blocked-wait");
    for (const st of tr.steps || []) {
      if (st.status === "running" && st.kind !== "agent") m.add("step-run");
      if (st.status === "running" && st.live?.last_failure?.type === "IndexError") m.add("demo-bug");
      const ag = st.agent;
      if (!ag) continue;
      if (ag.status === "awaiting_review") m.add("review-wait");
      for (const p of ag.pending || []) {
        if (p.activity_type === "agent_llm_turn") m.add("llm-turn");
        if (p.activity_type === "agent_tool") m.add("tool-call");
      }
    }
  }
  return m;
}

const PY_KW = new Set("and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield".split(" "));
const PY_CONST = new Set(["True", "False", "None", "self"]);
function hl(line) {
  const re = /(#.*$)|([rbfuRBFU]{0,2}"(?:[^"\\]|\\.)*"|[rbfuRBFU]{0,2}'(?:[^'\\]|\\.)*')|(@[\w.]+)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)|(\s+)|(.)/g;
  let out = "";
  let prev = "";
  let m;
  while ((m = re.exec(line))) {
    const [tok, com, str, dec, num, word, ws] = m;
    if (com) out += `<span class="c">${esc(tok)}</span>`;
    else if (str) out += `<span class="s">${esc(tok)}</span>`;
    else if (dec) out += `<span class="d">${esc(tok)}</span>`;
    else if (num) out += `<span class="n">${esc(tok)}</span>`;
    else if (word) {
      if (PY_KW.has(word)) out += `<span class="k">${word}</span>`;
      else if (PY_CONST.has(word)) out += `<span class="b">${word}</span>`;
      else if (prev === "def" || prev === "class") out += `<span class="f">${word}</span>`;
      else out += esc(word);
    } else out += esc(tok);
    if (word) prev = word; else if (!ws) prev = "";
  }
  return out;
}

const MARK_PRIORITY = ["demo-bug", "review-wait", "blocked-wait", "llm-turn", "tool-call", "approval-wait", "step-run", "fanout-wait"];
const MARK_LABEL = {
  "llm-turn": "Claude is on this turn", "tool-call": "a tool call is running", "review-wait": "waiting on the reviewer",
  "approval-wait": "waiting on an approver", "fanout-wait": "waiting for teams", "step-run": "steps running",
  "blocked-wait": "waiting for an operator", "demo-bug": "this line is failing right now",
  "lc-turn": "a LangChain turn is running", "lc-tool": "a tool call is running", "lg-run": "the graph is running",
  "lg-interrupt": "paused here for review", "lg-review-wait": "waiting on the reviewer",
};

function codeLines(sn, marks) {
  return sn.lines.map((l) => {
    const hot = l.marks.filter((x) => marks.has(x));
    const isBug = l.marks.includes("demo-bug");
    const commented = isBug && l.text.trim().startsWith("#");
    return `<div class="ln ${hot.length ? "hot" : ""} ${isBug ? (commented ? "bug-off" : "bug-on") : ""}"><span class="no">${l.n}</span><code>${hl(l.text) || " "}</code>${hot.length ? `<span class="here">${esc(MARK_LABEL[hot[0]] || "running now")}</span>` : isBug ? `<span class="here ${commented ? "ok" : "bad"}">${commented ? "commented out" : "the bug"}</span>` : ""}</div>`;
  }).join("");
}

const HOOD_MODE_LABEL = { shared: "Every runtime", plain: "No framework", langchain: "LangChain", langgraph: "LangGraph" };

function codeHtml() {
  const all = ui.code?.snippets;
  if (!all) return `<div class="empty"><p class="muted">Loading code…</p></div>`;
  const reqSnips = all.filter((sn) => sn.section !== "lab");
  const labSnips = all.filter((sn) => sn.section === "lab");
  const marks = liveMarks();
  const isLive = (sn) => sn.lines.some((l) => l.marks.some((x) => marks.has(x)));
  // When following the action, show the most interesting thing happening right now.
  const order = MARK_PRIORITY.filter((x) => marks.has(x));
  const follow = order.map((mk) => reqSnips.find((sn) => sn.lines.some((l) => l.marks.includes(mk)))).find(Boolean);
  const pick = all.find((x) => x.id === ui.snippet) || follow || reqSnips[0];
  if (!pick) return `<div class="empty"><p class="muted">No snippets configured.</p></div>`;
  const item = (sn) => `<button class="hood-item" data-action="pick-snippet" data-id="${esc(sn.id)}" aria-current="${sn.id === pick.id}">
      <span class="hood-title">${esc(sn.title)}${isLive(sn) ? `<span class="live-dot"></span>` : ""}</span>
      <span class="hood-sub">${esc(sn.summary)}</span></button>`;
  const open = ui.hoodLabOpen || pick.section === "lab";
  const labGroup = labSnips.length ? `<div class="hood-group">
      <button class="hood-group-head" data-action="hood-lab-toggle" aria-expanded="${open}">Agent lab code<span class="muted">${labSnips.length}</span></button>
      ${open ? Object.keys(HOOD_MODE_LABEL).map((m) => {
        const xs = labSnips.filter((sn) => (sn.mode || "shared") === m);
        return xs.length ? `<div class="hood-mode">${esc(HOOD_MODE_LABEL[m])}</div>${xs.map(item).join("")}` : "";
      }).join("") : ""}
    </div>` : "";
  const bug = pick.id === "demo-bug" ? `<div class="card pad" style="margin-bottom:12px">${bugControls(ui.snap?.code, true)}<div style="margin-top:10px">${deployControls(ui.snap?.code)}</div></div>` : "";
  const lines = codeLines(pick, marks);
  // Where to see this code running.
  let see = "";
  if (pick.section === "lab") {
    const href = pick.mode && pick.mode !== "shared" ? ui.snap?.lab_links?.[pick.mode] : null;
    see = href ? jump(href, `Latest ${HOOD_MODE_LABEL[pick.mode]} lab run in Temporal UI`)
      : pick.mode && pick.mode !== "shared" ? `<span class="muted" style="font-size:12.5px">Run it in the Agent lab to get a Temporal UI link</span>` : "";
  } else if (ui.snap?.selected?.links?.workflow) {
    see = jump(ui.snap.selected.links.workflow, `${ui.snap.selected.id || "This request"} in Temporal UI`);
  }
  const where = pick.illustration ? `<span class="chip">Illustration: simplified Python, not run</span>`
    : `<span class="mono muted">${esc(pick.file)}${pick.start ? `:${pick.start}` : ""}</span>`;
  return `<div class="hood">
    <nav class="hood-list" aria-label="Code snippets">${reqSnips.map(item).join("")}
      <p class="hood-follow">${ui.snippet ? `<button class="btn btn-sm" data-action="pick-snippet" data-id="">Follow the action</button>` : "Following the action: the tab jumps to whatever is running."}</p>
      ${labGroup}</nav>
    <section class="hood-main">
      ${ui.hoodFrom === "lab" ? `<button class="btn btn-sm hood-back" data-action="hood-back">Back to Agent lab</button>` : ""}
      <div class="hood-head"><h2>${esc(pick.title)}</h2>${pick.section === "lab" && pick.mode ? `<span class="own own-${pick.mode === "langchain" ? "langchain" : pick.mode === "langgraph" ? "langgraph" : "temporal"}">${esc(HOOD_MODE_LABEL[pick.mode] || pick.mode)}</span>` : ""}${where}</div>
      <p class="hood-summary">${esc(pick.summary)}</p>
      ${see ? `<div class="hood-see">${see}</div>` : ""}
      ${bug}
      ${pick.missing ? `<div class="banner bad">Snippet markers not found in ${esc(pick.file)}.</div>` : `<div class="code">${lines}</div>`}
      ${pick.points?.length ? `<div class="notes"><h3>What to notice</h3><ul>${pick.points.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    </section>
  </div>`;
}

async function loadCode() {
  try { ui.code = await api("GET", "/api/code"); } catch { /* keep the last copy */ }
  if (ui.tab === "lab") await loadLabContent();
  if (ui.tab === "code" || ui.tab === "lab") renderMain();
}
setInterval(() => { if ((ui.tab === "code" || ui.tab === "lab") && ui.view === "platform") loadCode(); }, 3000);

// ---------------------------------------------------------------------------
// Agent lab: where Temporal fits with LangChain and LangGraph
// ---------------------------------------------------------------------------
const MODE_ORDER = ["plain", "langchain", "langgraph"];
const TOOL_SHORT = { lookup_cmdb: "cmdb", check_existing_rules: "rules", check_policy: "policy", submit_rules: "submit" };
const AGENT_STATUS = {
  drafting: ["Drafting", "chip-oven"], revising: ["Revising", "chip-oven"], awaiting_review: ["Waiting for review", "chip-wait"],
  approved: ["Approved", "chip-good"], rejected: ["Rejected", "chip-bad"], failed: ["Failed", "chip-bad"],
};
const MODEL_ACTS = ["agent_llm_turn", "lc_llm_turn"];
const TOOL_ACTS = ["agent_tool", "lc_tool"];
// Which box contains which, so a lit box's containers stay readable.
const BOX_PARENT = { "llm.inner": "llm", "tool.inner": "tool", "review.inner": "review", "workflow.review": "workflow", graph: "workflow",
  workflow: "worker", llm: "worker", tool: "worker", review: "worker" };
const DEFAULT_EXAMPLE = 'lookup_cmdb(query="Oracle loan origination database")';

const labModeId = () => (MODE_ORDER.includes(ui.labMode) ? ui.labMode : "plain");

function labRun(mode) { return ui.snap?.lab?.runs?.[mode] || null; }

// What the selected run is doing right now, in the walkthrough's terms.
function runPhase(run) {
  if (!run) return "none";
  const ag = run.agent || {};
  if (["approved", "rejected", "failed"].includes(ag.status)) return "done";
  if (ag.status === "awaiting_review") return "review";
  const pend = ag.pending || [];
  const lastCrash = (run.crashes || []).slice(-1)[0];
  if ((lastCrash && Date.now() - lastCrash.at * 1000 < 10000) || pend.some((p) => (p.attempt || 1) > 1 && p.last_failure?.type === "Timeout")) return "recover";
  for (const p of pend) {
    const node = p.heartbeat?.node;
    if (MODEL_ACTS.includes(p.activity_type) || node === "agent") return "llm";
    if (TOOL_ACTS.includes(p.activity_type) || node === "tools") return "tool";
  }
  if (pend.length && run.mode === "langgraph") return ag.node === "agent" ? "tool" : "llm";
  return "idle";
}

function labStepIndex(mdef, run) {
  const steps = mdef?.steps || [];
  if (ui.labStep !== null && ui.labStep !== undefined && ui.labStep < steps.length) return { i: ui.labStep, live: false };
  const phase = runPhase(run);
  const i = steps.findIndex((st) => st.live === phase);
  return { i: i >= 0 ? i : 0, live: i >= 0 && !["none", "done"].includes(phase) };
}

function fillExample(text, run) {
  const ex = run?.example || DEFAULT_EXAMPLE;
  return String(text || "").replaceAll("{example}", ex).replaceAll("{tool}", `${ex.split("(")[0]}(…)`);
}

const ownChip = (owner, text) => `<span class="own own-${esc(owner)}">${esc(text)}</span>`;

function codeBtn(id, label) {
  const sn = ui.code?.snippets.find((x) => x.id === id);
  return `<button class="ab-code" data-action="code-jump" data-id="${esc(id)}" title="${esc(sn ? `${sn.title}${sn.file ? ` (${sn.file})` : ""}` : id)}">${esc(label || "Code")}</button>`;
}

function temporalHref(kind, run, links) {
  if (kind === "run") return run?.agent?.link || null;
  if (kind === "queue") return links?.queue || null;
  if (kind === "runs") return links?.runs || null;
  return null;
}

function boxLinks(def, run, links) {
  const code = (def.code || []).slice(0, 1).map((id) => codeBtn(id)).join("");
  const href = temporalHref(def.temporal, run, links);
  const t = href ? `<a class="ab-t" href="${esc(href)}" target="_blank" rel="noopener" title="Open in Temporal UI">Temporal${icon("ext", "")}</a>`
    : def.temporal === "run" ? `<span class="ab-t off" title="Run this runtime to get a Temporal UI link">Temporal</span>` : "";
  return code || t ? `<span class="ab-links">${code}${t}</span>` : "";
}

function boxClass(id, spot) {
  if (!spot) return "";
  if (spot.lit.has(id)) return "lit";
  if (spot.ctx.has(id)) return "ctx";
  return "dim";
}

function innerBox(id, def, spot, run, links, extra = "") {
  if (!def) return "";
  return `<div class="ab ab-inner own-${esc(def.owner || "code")} ${boxClass(id, spot)}" data-node="${id}">
    <div class="ab-titlerow">${ownChip(def.owner || "code", def.tag)}<span class="ab-title">${esc(def.title)}</span>${boxLinks(def, run, links)}</div>
    ${def.body ? `<div class="ab-body">${esc(def.body)}</div>` : ""}${extra}
  </div>`;
}

function liveBadge(text, cls = "") { return `<span class="ab-live ${cls}">${esc(text)}</span>`; }

function anatomyHtml(content, mode, run, spot, links) {
  const m = content.modes[mode];
  const sh = content.shared || {};
  const b = m.boxes || {};
  const phase = runPhase(run);
  const sc = run?.score || {};
  const pend = run?.agent?.pending || [];
  const attempt = Math.max(1, ...pend.map((p) => p.attempt || 1));
  const rows = ["workflow", "llm", "tool", ...(b.review ? ["review"] : [])];
  const R = rows.length;
  const box = (id, def, area, inner = "", badge = "") => `<div class="ab own-${esc(def.owner || "temporal")} ${boxClass(id, spot)} ${def.owner === "external" ? "ext" : ""}" data-node="${id}" style="grid-area:${area}">
      <div class="ab-head">${ownChip(def.owner || "temporal", def.tag)}${def.name ? `<span class="ab-name mono">${esc(def.name)}</span>` : ""}${badge}</div>
      ${def.title ? `<div class="ab-titlerow"><span class="ab-title">${esc(def.title)}</span>${boxLinks(def, run, links)}</div>` : ""}
      ${def.body ? `<div class="ab-body">${esc(def.body)}</div>` : ""}
      ${def.items ? `<ul class="ab-items">${def.items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      ${inner}
    </div>`;

  // Workflow, with the graph (LangGraph) and the review wait inside it.
  const st = run?.agent?.status;
  const wfBadge = run ? liveBadge((AGENT_STATUS[st] || ["Starting"])[0], phase === "review" ? "wait" : phase === "done" ? "" : "on") : "";
  let graph = "";
  if (b.graph) {
    const nodeOn = { llm: "agent", tool: "tools", review: "review" }[phase];
    const pills = ["START", ...(b.graph.nodes || []), "END"].map((n) => `<span class="gpill ${n === nodeOn ? "on" : ""} ${n === "START" || n === "END" ? "term" : ""}">${esc(n)}</span>`).join(`<span class="garrow">→</span>`);
    graph = innerBox("graph", { ...b.graph, owner: "langgraph" }, spot, run, links, `<div class="gpills">${pills}</div>`);
  }
  const reviewWait = b.workflow.review ? innerBox("workflow.review", { owner: "temporal", ...b.workflow.review }, spot, run, links,
    phase === "review" ? `<div>${liveBadge("Waiting now", "wait")}</div>` : "") : "";
  const wf = box("workflow", b.workflow, `1 / 2 / 2 / 3`, graph + reviewWait, wfBadge);

  const llmBadge = run ? (phase === "llm" ? liveBadge(`running${attempt > 1 ? `, attempt ${attempt}` : ""}`, "on") : sc.turns ? liveBadge(`${sc.turns} done`) : "") : "";
  const toolRuns = mode === "langgraph" ? (run?.units || []).filter((u) => u.kind === "node" && u.node === "tools").length : sc.tool_calls;
  const toolBadge = run ? (phase === "tool" ? liveBadge(`running${attempt > 1 ? `, attempt ${attempt}` : ""}`, "on") : toolRuns ? liveBadge(`${toolRuns} done`) : "") : "";
  const llm = box("llm", b.llm, `2 / 2 / 3 / 3`, innerBox("llm.inner", b.llm.inner, spot, run, links), llmBadge);
  const tool = box("tool", b.tool, `3 / 2 / 4 / 3`, innerBox("tool.inner", b.tool.inner, spot, run, links), toolBadge);
  const review = b.review ? box("review", b.review, `4 / 2 / 5 / 3`, innerBox("review.inner", b.review.inner, spot, run, links),
    phase === "review" ? liveBadge("paused here", "wait") : "") : "";

  const worker = sh.worker || {};
  const frame = `<div class="worker-frame ${boxClass("worker", spot)}" data-node="worker" style="grid-area:1 / 2 / ${R + 1} / 3">
      <div class="wf-label">${ownChip("code", worker.tag || "Your worker process")}<span class="wf-desc">${esc(worker.title || "")}</span>${boxLinks(worker, run, links)}</div>
    </div>`;
  const svc = box("service", sh.service, `1 / 1 / ${R + 1} / 2`);  // tall: every hop goes through it
  const person = box("person", sh.person, `1 / 3 / 2 / 4`);
  const claude = box("claude", sh.claude, `2 / 3 / 3 / 4`);
  const systems = box("systems", sh.systems, `3 / 3 / 4 / 4`);
  return `<div class="anat ${spot ? "spot" : ""}" data-mode="${esc(mode)}" style="grid-template-rows:repeat(${R}, auto)">
    ${frame}${svc}${wf}${llm}${tool}${review}${claude}${systems}${person}
    <svg class="anat-arrows" aria-hidden="true"></svg><div class="anat-labels" aria-hidden="true"></div>
  </div>`;
}

function spotFor(step) {
  if (!step) return null;
  const lit = new Set(step.lit || []);
  const ctx = new Set();
  for (const id of lit) { let p = BOX_PARENT[id]; while (p) { if (!lit.has(p)) ctx.add(p); p = BOX_PARENT[p]; } }
  return { lit, ctx };
}

// Arrows for the current step, drawn over the diagram after it renders.
function drawLabArrows() {
  const wrap = document.querySelector(".anat");
  if (!wrap) return;
  const svg = wrap.querySelector(".anat-arrows");
  const labels = wrap.querySelector(".anat-labels");
  const arrows = ui._labArrows || [];
  const base = wrap.getBoundingClientRect();
  svg.setAttribute("width", base.width);
  svg.setAttribute("height", base.height);
  svg.setAttribute("viewBox", `0 0 ${base.width} ${base.height}`);
  const rect = (id) => {
    const el = wrap.querySelector(`[data-node="${id}"]`);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const o = { l: r.left - base.left, r: r.right - base.left, t: r.top - base.top, b: r.bottom - base.top };
    return { ...o, cx: (o.l + o.r) / 2, cy: (o.t + o.b) / 2, w: o.r - o.l, h: o.b - o.t };
  };
  let paths = `<defs><marker id="ah" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="var(--arrow)"/></marker></defs>`;
  let tags = "";
  arrows.forEach(([from, to, label, route], k) => {
    const a = rect(from); const b = rect(to);
    if (!a || !b) return;
    let d; let lx; let ly; let side = "above";
    if (route === "gutter") {
      const gx = Math.min(a.l, b.l) - 16;
      d = `M${a.l} ${a.cy} H${gx} V${b.cy} H${b.l - 2}`;
      lx = gx; ly = (a.cy + b.cy) / 2; side = "left";
    } else if (a.r <= b.l || b.r <= a.l) {
      const right = a.r <= b.l;
      const x0 = right ? a.r : a.l; const x1 = right ? b.l : b.r;
      const top = Math.max(a.t, b.t); const bot = Math.min(a.b, b.b);
      const small = a.h < b.h ? a : b;
      let y0; let y1;
      if (bot - top > 18) { y0 = y1 = Math.min(Math.max(small.cy, top + 12), bot - 12); } else { y0 = a.cy; y1 = b.cy; }
      const end = x1 + (right ? -2 : 2);
      d = y0 === y1 ? `M${x0} ${y0} H${end}` : `M${x0} ${y0} C${(x0 + x1) / 2} ${y0}, ${(x0 + x1) / 2} ${y1}, ${end} ${y1}`;
      lx = (x0 + x1) / 2; ly = (y0 + y1) / 2;
    } else {
      const down = a.b <= b.t;
      const left = Math.max(a.l, b.l); const right2 = Math.min(a.r, b.r);
      const x = (left + right2) / 2;
      const y0 = down ? a.b : a.t; const y1 = down ? b.t - 2 : b.b + 2;
      d = `M${x} ${y0} V${y1}`;
      lx = x; ly = (y0 + y1) / 2; side = "right";
    }
    paths += `<path d="${d}" class="arrow" style="animation-delay:${k * 0.25}s" marker-end="url(#ah)"/>`;
    if (label) tags += `<span class="atag ${side}" style="left:${lx}px;top:${ly}px">${esc(fillExample(label, labRun(labModeId())))}</span>`;
  });
  svg.innerHTML = paths;
  labels.innerHTML = tags;
}

function walkthroughHtml(mdef, run, idx) {
  const steps = mdef.steps || [];
  const { i, live } = idx;
  const followable = run && !["approved", "rejected", "failed"].includes(run.agent?.status);
  const list = steps.map((st, k) => {
    const on = k === i;
    return `<li class="${on ? "on" : ""}">
      <button class="st-n" data-action="lab-step" data-i="${k}" aria-current="${on}" title="${esc(st.title)}">${k + 1}</button>
      <div class="st-main">
        <button class="st-title" data-action="lab-step" data-i="${k}">${esc(st.title)}</button>
        ${on ? `<p class="st-text">${esc(fillExample(st.text, run))}</p>
          <div class="st-links">${(st.code || []).map((id) => { const sn = ui.code?.snippets.find((x) => x.id === id); return codeBtn(id, sn ? sn.title : id); }).join("")}
          ${run?.agent?.link ? `<a class="ab-t" href="${esc(run.agent.link)}" target="_blank" rel="noopener">See it in Temporal UI${icon("ext", "")}</a>` : ""}</div>` : ""}
      </div></li>`;
  }).join("");
  return `<div class="walk">
    <div class="walk-head"><h3>Follow one tool call</h3>
      <div class="walk-nav">
        <button class="btn btn-sm" data-action="lab-step" data-i="${Math.max(0, i - 1)}" ${i === 0 ? "disabled" : ""} aria-label="Previous step">Back</button>
        <button class="btn btn-sm" data-action="lab-step" data-i="${Math.min(steps.length - 1, i + 1)}" ${i >= steps.length - 1 ? "disabled" : ""} aria-label="Next step">Next</button>
        ${followable ? `<button class="btn btn-sm ${ui.labStep === null ? "btn-on" : ""}" data-action="lab-step" data-i="">${ui.labStep === null ? (live ? "Following the run" : "Following the run (idle)") : "Follow the run"}</button>` : ""}
      </div></div>
    <ol class="steps">${list}</ol>
  </div>`;
}

function cellHtml(u, mode) {
  const retry = (u.attempt || 1) > 1;
  const who = u.worker ? ` · ${u.worker}` : "";
  if (u.kind === "pending") {
    const label = mode === "langgraph" ? "node" : u.label === "Model turn" ? "turn" : "tool";
    const fail = u.failure ? ` · last attempt failed: ${u.failure.type}` : "";
    return `<span class="cell k-pending ${retry ? "retried" : ""}" title="${esc(`${u.label} in flight · attempt ${u.attempt || 1}${who}${fail}`)}">${esc(label)}${retry ? `<i>×${u.attempt}</i>` : ""}</span>`;
  }
  if (u.kind === "wait") return `<span class="cell k-wait" title="Waiting for Network Security">review</span>`;
  let label = u.label;
  let cls = `k-${u.kind}`;
  if (u.kind === "turn") label = `T${String(u.label).replace(/\D/g, "")}`;
  else if (u.kind === "tool") label = TOOL_SHORT[u.label] || u.label;
  else if (u.kind === "node") { cls += ` n-${u.node}`; label = u.node === "tools" ? `tools · ${u.calls}` : u.node; }
  const title = `${u.kind === "node" ? `Node: ${u.label}` : u.kind === "turn" ? u.label : `Tool: ${u.label}`}${retry ? ` · attempt ${u.attempt}` : ""}${who}`;
  return `<span class="cell ${cls} ${retry ? "retried" : ""}" title="${esc(title)}">${esc(label)}${retry ? `<i>×${u.attempt}</i>` : ""}</span>`;
}

function tapeHtml(run) {
  const ag = run.agent || {};
  const finished = ["approved", "rejected", "failed"].includes(ag.status);
  const crashAt = (run.crashes || []).map((c) => c.at * 1000).sort((a, b) => a - b);
  const marker = (ms) => `<span class="crash" title="Every core worker killed at ${esc(clock(new Date(ms).toISOString()))}">crash</span>`;
  let ci = 0;
  const groups = [];
  const add = (html, fresh) => { if (fresh || !groups.length) groups.push([]); groups[groups.length - 1].push(html); };
  const flush = (before) => { while (ci < crashAt.length && (before === null || crashAt[ci] < before)) add(marker(crashAt[ci++]), false); };
  for (const u of run.units) {
    if (u.at) flush(t(u.at));
    else if (!finished) flush(null);
    add(cellHtml(u, run.mode), u.kind === "turn" || (u.kind === "node" && u.node === "agent"));
  }
  if (!finished && run.units.length) flush(null);
  return groups.map((g) => `<span class="grp">${g.join("")}</span>`).join("");
}

function scoreHtml(run) {
  const sc = run.score || {};
  if (!sc.turns && !sc.tool_calls) return `<div class="lab-score muted">No calls yet</div>`;
  const rep = (sc.repeated_model_calls || 0) + (sc.repeated_tool_runs || 0);
  const repText = rep
    ? `Repeated: ${[sc.repeated_model_calls ? plural(sc.repeated_model_calls, "model call") : "", sc.repeated_tool_runs ? plural(sc.repeated_tool_runs, "tool run") : ""].filter(Boolean).join(", ")}`
    : "Nothing repeated";
  const coarse = sc.coarse ? `<div class="what-if" title="If the whole agent ran inside one activity, a crash would restart it from the top.">As one big activity, the crash would have redone ${plural(sc.coarse.model_calls, "model call")} and ${plural(sc.coarse.tool_calls, "tool call")}</div>` : "";
  return `<div class="lab-score">
    <div class="nums"><span><b class="num">${sc.model_calls}</b> model calls</span><span><b class="num">${sc.tool_runs}</b> tool runs</span></div>
    <div class="${rep ? "rep-bad" : "rep-ok"}">${repText}</div>
    ${sc.tokens ? `<div class="muted">${sc.tokens.toLocaleString()} tokens</div>` : ""}
    ${coarse}
  </div>`;
}

function runStatusHtml(run) {
  const ag = run.agent || {};
  const [txt, cls] = AGENT_STATUS[ag.status] || ["Starting", "chip-oven"];
  return `<span class="chip ${cls}">${esc(txt)}</span>${(ag.round || 1) > 1 ? `<span class="muted">round ${ag.round}</span>` : ""}`;
}

function runReviewHtml(run, change) {
  const ag = run.agent || {};
  if (ag.status !== "awaiting_review") return "";
  const d = ag.draft || {};
  const pol = d.policy;
  return `<div class="rp-review">
    <div class="rp-review-head"><strong>Network Security review</strong>
      ${pol ? (pol.compliant ? `<span class="chip chip-good">Policy OK</span>` : `<span class="chip chip-bad">${pol.violations.length} issue(s)</span>`) : ""}
      <span class="muted">round ${ag.round || 1} · ${plural((d.rules || []).length, "rule")}</span></div>
    <ul class="rp-rules">${(d.rules || []).map((x) => `<li><span class="mono">${esc(x.protocol)}/${esc(x.port)}</span> ${esc(x.destination_name || x.destination)}</li>`).join("")}</ul>
    <textarea id="lab-comment-${esc(run.mode)}" rows="1" aria-label="What should change">${esc(change || "")}</textarea>
    <div class="actions" style="margin-top:8px">
      <button class="btn btn-good" data-action="lab-review-one" data-wf="${esc(ag.workflow_id)}" data-mode="${esc(run.mode)}" data-do="approve">Approve</button>
      <button class="btn" data-action="lab-review-one" data-wf="${esc(ag.workflow_id)}" data-mode="${esc(run.mode)}" data-do="changes">Request changes</button>
      <button class="btn btn-bad" data-action="lab-review-one" data-wf="${esc(ag.workflow_id)}" data-mode="${esc(run.mode)}" data-do="reject">Reject</button>
    </div>
  </div>`;
}

function runPanelHtml(s, content, mode) {
  const fw = s.frameworks || {};
  const meta = fw[mode] || {};
  const lab = s.lab || {};
  const run = labRun(mode);
  const scenarios = content.scenarios || [];
  const scen = scenarios.find((x) => x.id === ui.labScenario) || scenarios[0] || {};
  const coreAlive = (s.pools || []).find((p) => p.id === "core")?.workers.filter((w) => ["running", "starting", "unresponsive"].includes(w.status)).length || 0;
  const flaky = lab.cmdb_error_rate || 0;
  const active = run && !["approved", "rejected", "failed"].includes(run.agent?.status);
  const allAvail = MODE_ORDER.filter((m) => fw[m]?.available);
  const scenCards = scenarios.map((x) => `<button class="scen ${x.id === scen.id ? "on" : ""}" data-action="lab-scenario" data-id="${esc(x.id)}" aria-pressed="${x.id === scen.id}">
      <strong>${esc(x.label)}</strong><span>${esc(x.summary)}</span></button>`).join("");
  const modeSeg = MODE_ORDER.map((m) => {
    const f = fw[m] || {};
    return `<button data-action="agent-mode" data-mode="${m}" data-label="${esc(f.label || m)}" aria-pressed="${(s.agent_mode || "plain") === m}" ${f.available ? "" : `disabled title="${esc(f.reason || "Not installed")}"`}>${esc(f.label || m)}</button>`;
  }).join("");
  const runCard = run ? `<div class="rp-run">
      <div class="rp-run-head">
        <strong>Latest ${esc(meta.label || mode)} run</strong>${runStatusHtml(run)}
        <span class="muted">${esc(run.scenario?.label || "")} · started ${esc(clock(new Date(run.started_at * 1000).toISOString()))}</span>
        <span class="grow"></span>${jump(run.agent?.link, "Open in Temporal UI")}
      </div>
      <div class="rp-run-body">
        <div><div class="tape" aria-label="One box per durable step">${tapeHtml(run) || `<span class="muted" style="font-size:12px">Waiting for a core worker…</span>`}</div>
          <div class="tape-note">Each box is one activity: the step Temporal retries, and never repeats once it's done.</div></div>
        ${scoreHtml(run)}
      </div>
      ${runReviewHtml(run, lab.change)}
    </div>` : `<div class="rp-empty">No ${esc(meta.label || mode)} run yet. Pick a scenario and run it.</div>`;
  return `<div class="card rp">
    <div class="rp-head"><h3>Run it</h3><span class="muted">The same request each time: ${esc(lab.input?.account || "mortgage-analytics-prod")}, VPC ${esc(lab.input?.vpc_cidr || "10.48.36.0/22")}</span></div>
    <div class="scens" role="group" aria-label="Scenario">${scenCards}</div>
    <div class="rp-actions">
      ${meta.available ? `<button class="btn btn-primary" data-action="lab-run" data-modes="${mode}" data-active="${active ? 1 : 0}">Run on ${esc(meta.label || mode)}</button>`
        : `<button class="btn btn-primary" disabled>Run on ${esc(meta.label || mode)}</button><span class="mono muted" style="font-size:12px">${esc(meta.install || "")}</span>`}
      ${allAvail.length > 1 ? `<button class="btn" data-action="lab-run" data-modes="${allAvail.join(",")}" data-active="0">Run all ${allAvail.length === 3 ? "three" : allAvail.length} side by side</button>` : ""}
      <span class="grow"></span>
      <button class="btn btn-bad btn-sm" data-action="lab-crash" ${coreAlive ? "" : "disabled"} title="Kill every core worker now; fresh ones start 3s later">Crash core workers now</button>
      <div class="seg seg-sm" role="group" aria-label="CMDB lookups"><button data-action="lab-flaky" data-rate="0" aria-pressed="${!flaky}">CMDB healthy</button><button data-action="lab-flaky" data-rate="0.3" aria-pressed="${flaky > 0}">Fail 30%</button></div>
    </div>
    ${runCard}
    <div class="rp-foot"><span class="lab-ctl-label">New requests use</span><div class="seg seg-sm" role="group">${modeSeg}</div><span class="muted">The firewall track in a real request starts this runtime's agent.</span></div>
  </div>`;
}

function compareHtml(s) {
  const runs = MODE_ORDER.map((m) => labRun(m)).filter(Boolean);
  if (runs.length < 2) return "";
  const rows = runs.map((run) => `<div class="cmp-run">
      <div class="lab-who"><button class="lab-name linkish" data-action="lab-mode" data-mode="${esc(run.mode)}">${esc(run.label)}</button>
        <div class="lab-status">${runStatusHtml(run)}</div><div class="muted" style="font-size:11.5px">${esc(run.scenario?.label || "")}</div></div>
      <div class="tape">${tapeHtml(run) || `<span class="muted" style="font-size:12px">Waiting…</span>`}</div>
      ${scoreHtml(run)}
    </div>`).join("");
  return `<div class="section"><div class="section-head"><h2>Latest runs side by side</h2><span class="hint">One box per activity</span></div>
    <div class="card cmp-runs">${rows}
      <div class="lab-legend"><span><i class="cell k-turn"></i>model turn</span><span><i class="cell k-tool"></i>tool call</span><span><i class="cell k-node"></i>graph node</span><span><i class="cell retried k-tool"></i>retried</span><span><i class="cell k-pending"></i>in flight</span><span><i class="crash"></i>workers killed</span></div>
    </div></div>`;
}

function matrixHtml(content, fw) {
  const owners = content.owners || {};
  const rows = (content.matrix || []).map((row) => `<tr><th scope="row">${esc(row.job)}</th>${MODE_ORDER.map((m) => `<td>${(row[m] || []).map(([o, txt]) => `<span class="mx"><i class="dot own-${esc(o)}" title="${esc(owners[o]?.label || o)}"></i>${esc(txt)}</span>`).join("")}</td>`).join("")}</tr>`).join("");
  const legend = Object.entries(owners).map(([o, v]) => `<span>${ownChip(o, v.label)}<span class="muted">${esc(v.note || "")}</span></span>`).join("");
  return `<div class="section"><div class="section-head"><h2>Who does what</h2><span class="hint">Colored by who owns that part</span></div>
    <div class="card cmp-wrap"><table class="cmp"><thead><tr><th></th>${MODE_ORDER.map((m) => `<th scope="col">${esc(fw[m]?.label || content.modes?.[m]?.label || m)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table>
    <div class="own-legend">${legend}</div></div></div>`;
}

function labHtml(s) {
  const content = ui.labContent;
  if (!content) return `<div class="empty"><p class="muted">Loading the Agent lab…</p></div>`;
  if (content.error) return `<div class="banner bad">${esc(content.error)}</div>`;
  const fw = s.frameworks || {};
  const mode = labModeId();
  const mdef = content.modes?.[mode];
  const run = labRun(mode);
  const idx = labStepIndex(mdef, run);
  const step = mdef.steps?.[idx.i];
  const spot = spotFor(step);
  ui._labArrows = step?.arrows || [];
  const links = s.lab?.links || {};
  const tabs = MODE_ORDER.map((m) => {
    const f = fw[m] || {};
    const r = labRun(m);
    const busy = r && !["approved", "rejected", "failed"].includes(r.agent?.status);
    return `<button role="tab" data-action="lab-mode" data-mode="${m}" aria-selected="${m === mode}">${esc(f.label || content.modes?.[m]?.label || m)}${content.modes?.[m]?.preview ? `<span class="chip chip-wait">Preview</span>` : ""}${busy ? `<span class="live-dot" title="A run is in progress"></span>` : ""}${f.available === false ? `<span class="tab-off">not installed</span>` : ""}</button>`;
  }).join("");
  const note = mdef.note ? `<div class="anat-note"><strong>${esc(mdef.note.title)}</strong> ${esc(mdef.note.body)} ${(mdef.note.code || []).map((id) => codeBtn(id, "Show the code")).join("")}</div>` : "";
  const legend = ["temporal", "langchain", "langgraph", "code", "external"].map((o) => ownChip(o, content.owners?.[o]?.label || o)).join("");
  return `<div class="lab">
    <div class="lab-head">
      <div>
        <h1>Agent lab</h1>
        <p>The firewall-rule agent from the request, three ways. Pick a runtime to see where Temporal sits, follow one tool call from Claude back to Temporal, and run it with a scenario.</p>
      </div>
    </div>
    <nav class="lab-tabs" role="tablist" aria-label="Runtime">${tabs}</nav>
    <p class="lab-headline">${esc(mdef.headline || "")}</p>
    <div class="lab-stage">
      ${walkthroughHtml(mdef, run, idx)}
      <div class="anat-wrap">
        <div class="anat-legend"><span class="muted">Who owns each box:</span>${legend}</div>
        ${anatomyHtml(content, mode, run, spot, links)}
        ${note}
      </div>
    </div>
    ${runPanelHtml(s, content, mode)}
    ${compareHtml(s)}
    ${matrixHtml(content, fw)}
  </div>`;
}

async function loadLabContent() {
  try { ui.labContent = await api("GET", "/api/lab/content"); } catch { /* keep the last copy */ }
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------
function seg(name, options, current) {
  return `<div class="seg" role="group">${options.map(([v, l]) => `<button data-action="setting" data-name="${name}" data-value="${v}" aria-pressed="${String(current) === String(v)}">${l}</button>`).join("")}</div>`;
}

function versionChip(w, code) {
  if (!w.version) return "";
  const dead = ["killed", "stopped", "crashed"].includes(w.status);
  const old = code && !dead && w.version !== code.disk_version;
  const title = w.replacing ? `New code, replacing ${w.replacing}` : old ? "Running older code than what's on disk" : "Code version";
  return `<span class="ver ${old ? "ver-old" : ""}" title="${esc(title)}">v${esc(w.version)}${old ? " old" : ""}${w.replacing ? " new" : ""}</span>`;
}

function deployLine(code) {
  const d = code?.last_deploy;
  if (!d) return `<span class="muted">Workers run v${esc(code?.deployed_version || "")}.</span>`;
  if (d.state === "rolling") return `<span class="live-note">${icon("run")} Rolling ${plural(d.replaced, "worker")} onto v${esc(d.version)}…</span>`;
  if (d.state === "failed") return `<span class="err-inline">Deploy failed: ${esc(d.error || "")}</span>`;
  return `<span class="muted">${d.replaced ? `Deployed v${esc(d.version)} to ${plural(d.replaced, "worker")}` : "All workers already run the code on disk"}${d.at ? ` at ${clock(new Date(d.at * 1000).toISOString())}` : ""}.</span>`;
}

function bugControls(code, compact) {
  return (code?.bugs || []).map((b) => {
    const line = b.line ? `<span class="mono">${esc(b.file)}:${b.line}</span>` : `<span class="mono">${esc(b.file)}</span>`;
    const state = b.error ? `<span class="err-inline">${esc(b.error)}</span>`
      : b.active ? `<span class="chip chip-bad">bug line is live</span>` : `<span class="chip chip-good">bug line commented out</span>`;
    return `<div class="bug">
      <div class="ctl-row" style="margin:0"><strong style="font-size:13px">${esc(b.label)}</strong>
        <div class="seg" role="group"><button data-action="bug" data-id="${esc(b.id)}" data-on="0" aria-pressed="${!b.enabled}">Off</button><button data-action="bug" data-id="${esc(b.id)}" data-on="1" aria-pressed="${b.enabled}">Bug mode</button></div></div>
      ${compact ? "" : `<div class="fault-desc">${esc(b.description)}</div>`}
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:12px">${line} ${state}</div>
      ${b.enabled && b.active ? `<div class="fault-desc" style="margin-top:4px">New requests retry this step until the fix is deployed.</div>` : ""}
    </div>`;
  }).join("");
}

function deployControls(code) {
  return `<div class="ctl-row"><label>Redeploy workers on save</label>
      <div class="seg" role="group"><button data-action="auto-deploy" data-on="0" aria-pressed="${!code?.auto_deploy}">Off</button><button data-action="auto-deploy" data-on="1" aria-pressed="${!!code?.auto_deploy}">On</button></div></div>
    <div class="ctl-row"><span style="font-size:12px">Code on disk <span class="ver">v${esc(code?.disk_version || "")}</span></span><button class="btn btn-sm" data-action="deploy">Deploy now</button></div>
    <div style="font-size:12px">${deployLine(code)}</div>`;
}

function fmtWait(sec) {
  const n = Math.max(0, Math.round(sec));
  return n >= 60 ? `${Math.floor(n / 60)}m ${String(n % 60).padStart(2, "0")}s` : `${n}s`;
}

function renderControls() {
  const s = ui.snap;
  if (!s) return;
  const pools = (s.pools || []).map((p) => {
    const alive = p.workers.filter((w) => ["running", "starting", "unresponsive", "stopping"].includes(w.status));
    const workers = p.workers.map((w) => {
      const dead = ["killed", "stopped", "crashed"].includes(w.status);
      const busy = (w.current || []).map((c) => c.label).join(", ");
      const dot = dead ? "dot-bad" : w.status === "running" ? "dot-good" : "dot-warn";
      // A dead worker stays on Temporal's list until 5 minutes after its last poll.
      const listed = dead && w.last_poll_s != null && w.listed_for_s > 0
        ? `<div class="worker-listed" title="Temporal lists a worker until 5 minutes after its last poll. This process is gone; Temporal just hasn't dropped it yet.">Temporal still lists it: last poll ${Math.round(w.last_poll_s)}s ago, off the list in ${fmtWait(w.listed_for_s)}</div>` : "";
      const state = dead ? `${w.status}${w.exit_code != null ? ` (exit ${w.exit_code})` : ""}` : w.status === "starting" ? "starting…" : w.status === "stopping" ? "finishing work, then stopping" : busy || "idle";
      return `<div class="worker ${dead ? "dead" : ""}">
        <span class="dot ${dot}"></span>
        <div style="min-width:0"><div class="worker-id">${esc(w.id)} ${versionChip(w, s.code)} <span class="muted" style="font-family:var(--sans);font-size:11px">${w.completed ? `· ${w.completed} done` : ""}</span></div>
        <div class="worker-busy ${busy ? "" : "idle"}" title="${esc(state)}">${esc(state)}</div>${listed}</div>
        <div class="worker-btns">${dead ? "" : `<button class="btn btn-sm" data-action="stop-worker" data-id="${esc(w.id)}" title="Graceful stop (SIGTERM)">Stop</button><button class="btn btn-sm btn-bad" data-action="kill-worker" data-id="${esc(w.id)}" title="Kill now (SIGKILL), mid-task">Kill</button>`}</div>
      </div>`;
    }).join("");
    const backlog = p.backlog == null ? "" : `<span class="${p.backlog > 0 ? "backlog-hot" : ""}">Backlog <b>${p.backlog}</b></span>`;
    return `<div class="pool">
      <div class="pool-head">
        <div class="pool-name"><span>${esc(p.label)}</span>${jump(p.queue_url, `<span class="mono">${esc(p.queue)}</span>`)}</div>
        <div class="pool-desc">${esc(p.description)} · ${esc(p.where)}</div>
        <div class="pool-stats"><span title="Demo workers that are running and polling this task queue">Polling now <b>${p.polling ?? p.pollers.length}</b></span>${(p.listed_gone || []).length ? `<span class="muted" title="Temporal lists a worker until 5 minutes after its last poll. A killed worker never signs off, so it stays on the list (and in the Temporal UI's Workers tab) until then.">Temporal still lists ${(p.listed_gone || []).length} stopped</span>` : ""}${backlog}</div>
        ${(p.listed_other || []).length ? `<div class="pool-other" title="Pollers on this task queue that the demo didn't start">Also on Temporal's list: <span class="mono">${(p.listed_other || []).map(esc).join(", ")}</span></div>` : ""}
      </div>
      ${workers || `<div class="worker"><span class="dot dot-bad"></span><div class="worker-busy">No workers. Work for this queue waits on the server.</div><span></span></div>`}
      ${p.managed ? `<div class="pool-foot">
        <div class="stepper"><button data-action="scale" data-pool="${esc(p.id)}" data-n="${alive.length - 1}" aria-label="Remove a worker">−</button><span>${alive.length}</span><button data-action="scale" data-pool="${esc(p.id)}" data-n="${alive.length + 1}" aria-label="Add a worker">+</button></div>
        <span class="muted" style="font-size:11.5px;flex:1">workers</span>
        <button class="btn btn-sm btn-bad" data-action="kill-all" data-pool="${esc(p.id)}" ${alive.length ? "" : "disabled"}>Kill all</button>
      </div>` : `<div class="pollers">Workers are managed outside the demo (DEMO_MANAGE_WORKERS=false).</div>`}
    </div>`;
  }).join("");

  const faults = (s.fault_presets || []).map((f) => {
    const armed = s.armed?.[f.id];
    return `<div class="fault ${armed ? "armed" : ""}">
      <span class="fault-label">${esc(f.label)} <span class="chip ${f.kind === "hard" ? "chip-bad" : "chip-oven"}" style="height:18px;font-size:10.5px">${f.kind === "hard" ? "needs a decision" : "auto-retries"}</span></span>
      <span class="fault-desc">${esc(f.description)}</span>
      <button class="btn btn-sm" data-action="${armed ? "disarm" : "arm"}" data-id="${esc(f.id)}">${armed ? `Armed${f.kind === "transient" ? ` (${armed})` : ""}` : "Arm"}</button>
    </div>`;
  }).join("");

  const flaky = (s.systems || []).map((sys) => {
    const rate = Math.round(((s.system_chaos?.[sys.id] || {}).error_rate || 0) * 100);
    return `<span>${esc(sys.label)}</span><input type="range" min="0" max="90" step="10" value="${rate}" data-action="flaky" data-id="${esc(sys.id)}" aria-label="${esc(sys.label)} error rate"><span class="num">${rate}%</span>`;
  }).join("");

  const st = s.settings || {};
  const html = `
    <h2 class="ctl-title">Demo controls</h2>
    <p class="ctl-sub">Break things on purpose and watch the request keep its place.</p>
    <div class="ctl"><h3>Workers <small>each is a real process</small></h3>${pools}</div>
    <div class="ctl"><h3>Bug and deploy <small>edit the code, watch it recover</small></h3>${bugControls(s.code)}${deployControls(s.code)}</div>
    <div class="ctl"><h3>Inject a failure <small>fires on the next matching step</small></h3>${faults}
      <details style="margin-top:10px"><summary>Flaky systems (random errors per call)</summary><div class="flaky">${flaky}</div></details>
    </div>
    <div class="ctl"><h3>Approvals <small>new requests</small></h3>
      <div class="ctl-row"><label>Auto-approve after</label>${seg("auto_approve_seconds", [[0, "Off"], [5, "5s"], [15, "15s"], [30, "30s"]], st.approvals?.auto_approve_seconds)}</div>
      <div class="ctl-row"><label>Escalate if waiting</label>${seg("sla_seconds", [[0, "Never"], [30, "30s"], [60, "1m"], [120, "2m"]], st.approvals?.sla_seconds)}</div>
    </div>
    <div class="ctl"><h3>Retries <small>new requests</small></h3>
      <div class="ctl-row"><label>Attempts per step</label>${seg("max_attempts", [[3, "3"], [6, "6"], [10, "10"]], st.retries?.max_attempts)}</div>
      <div class="ctl-row"><label>Dead worker noticed in</label>${seg("heartbeat_seconds", [[5, "5s"], [8, "8s"], [15, "15s"]], st.retries?.heartbeat_seconds)}</div>
    </div>
    <div class="ctl"><h3>Pace</h3>
      <div class="ctl-row"><label>Step duration</label>${seg("speed", [[0.4, "Fast"], [1, "Normal"], [2, "Slow"]], st.speed)}</div>
    </div>
    <div class="ctl"><button class="btn btn-block" data-action="reset-chaos">Clear all injected failures</button></div>`;
  if (document.activeElement?.dataset?.action === "flaky") return; // don't yank a slider mid-drag
  patch($("#controls"), html);
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------
function selectedWf() { return ui.snap?.selected?.workflow_id || ui.selected; }

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el || el.tagName === "INPUT") return;
  if (el.tagName === "A") return;
  const a = el.dataset.action;
  switch (a) {
    case "view": ui.view = el.dataset.view; store.set("view", ui.view); ui._edgesDrawn = false; render(); break;
    case "toggle-controls": ui.controls = !ui.controls; store.set("controls", ui.controls); render(); requestAnimationFrame(() => drawEdges(ui.snap?.selected)); break;
    case "open-intake": openIntake(); break;
    case "close-intake": $("#intake").close(); break;
    case "preset": openIntake(ui.presets[Number(el.dataset.i)]); break;
    case "fill-preset": fillPreset(ui.presets[Number(el.dataset.i)]); break;
    case "select-request":
      ui.selected = el.dataset.wf; ui.track = null; store.set("selected", ui.selected);
      $("#main-body").dataset.mode = ""; poll(true); break;
    case "select-track": ui.track = el.dataset.track; render(); break;
    case "toggle-step": {
      const key = el.dataset.key; const open = el.getAttribute("aria-expanded") === "true";
      if (open) { ui.collapsed.add(key); ui.expanded.delete(key); } else { ui.expanded.add(key); ui.collapsed.delete(key); }
      render(); break;
    }
    case "approve": case "reject": {
      const decision = a === "approve" ? "approved" : "rejected";
      const comment = a === "reject" ? (prompt(`Why is ${el.dataset.who} rejecting?`, "Not enough detail on data handling") ?? null) : "";
      if (comment === null) return;
      act(el, () => api("POST", `/api/requests/${encodeURIComponent(selectedWf())}/approvals/${el.dataset.id}`, { decision, approver: el.dataset.who, comment }), decision === "approved" ? "Approved" : "Rejected");
      break;
    }
    case "resolve": {
      const what = el.dataset.do;
      if (what === "rollback" && !confirm("Roll back the whole request? Every team's completed work is undone in reverse order.")) return;
      act(el, () => api("POST", `/api/tracks/${encodeURIComponent(el.dataset.wf)}/resolve`, { action: what }), { retry: "Retrying the step", skip: "Skipped", rollback: "Rolling back" }[what]);
      break;
    }
    case "cancel":
      if (!confirm("Cancel this request? Anything already provisioned is rolled back.")) return;
      act(el, () => api("POST", `/api/requests/${encodeURIComponent(selectedWf())}/cancel`, { reason: "Cancelled by the requester" }), "Cancelling");
      break;
    case "kill-worker": act(el, () => api("POST", `/api/workers/${el.dataset.id}/kill`, { graceful: false }), `Killed ${el.dataset.id}`); break;
    case "stop-worker": act(el, () => api("POST", `/api/workers/${el.dataset.id}/kill`, { graceful: true }), `Stopping ${el.dataset.id}`); break;
    case "scale": act(el, () => api("POST", `/api/pools/${el.dataset.pool}/scale`, { count: Math.max(0, Number(el.dataset.n)) })); break;
    case "kill-all": act(el, () => api("POST", `/api/pools/${el.dataset.pool}/kill-all`), "Killed every worker in the pool"); break;
    case "arm": act(el, () => api("POST", `/api/faults/${el.dataset.id}`), "Armed. It fires on the next matching step."); break;
    case "disarm": act(el, () => api("DELETE", `/api/faults/${el.dataset.id}`)); break;
    case "setting": act(el, () => api("PUT", "/api/settings", { [el.dataset.name]: Number(el.dataset.value) })); break;
    case "reset-chaos": act(el, () => api("POST", "/api/reset-chaos"), "Cleared"); break;
    case "tab": ui.tab = el.dataset.tab; store.set("tab", ui.tab); ui.hoodFrom = null; $("#main-body").dataset.mode = ""; ui._edgesDrawn = false; if (ui.tab !== "request") loadCode(); render(); poll(); break;
    case "lab-mode": ui.labMode = el.dataset.mode; store.set("labMode", ui.labMode); ui.labStep = null; if (ui.tab !== "lab") { ui.tab = "lab"; store.set("tab", "lab"); } render(); break;
    case "lab-step": ui.labStep = el.dataset.i === "" ? null : Number(el.dataset.i); renderMain(); break;
    case "lab-scenario": ui.labScenario = el.dataset.id; store.set("labScenario", ui.labScenario); renderMain(); break;
    case "lab-run": {
      const modes = el.dataset.modes.split(",");
      if (el.dataset.active === "1" && !confirm("Start a new run? The current one keeps going in Temporal; the lab switches to the new one.")) return;
      ui.labStep = null;
      act(el, () => api("POST", "/api/lab/runs", { modes, scenario: ui.labScenario }), modes.length > 1 ? "Started the agent on every runtime" : `Started the agent on ${ui.snap?.frameworks?.[modes[0]]?.label || modes[0]}`);
      break;
    }
    case "lab-crash": act(el, () => api("POST", "/api/lab/crash", { respawn_after: 3 }), "Killed every core worker. Fresh ones start in 3s."); break;
    case "lab-flaky": act(el, () => api("PUT", "/api/systems/servicenow", { error_rate: Number(el.dataset.rate) }), Number(el.dataset.rate) ? "CMDB lookups now fail 30% of the time" : "CMDB is healthy again"); break;
    case "lab-review-one": {
      const what = el.dataset.do;
      const box = document.getElementById(`lab-comment-${el.dataset.mode}`);
      const comment = (box?.value || "").trim();
      if (what === "changes" && !comment) { toast("Say what should change first", true); box?.focus(); return; }
      act(el, () => api("POST", `/api/agents/${encodeURIComponent(el.dataset.wf)}/review`, { decision: what, reviewer: "Network Security on-call", comment: what === "approve" ? "" : comment || "Too broad for a production account" }),
        { approve: "Approved", changes: "Sent your notes back to the agent", reject: "Rejected" }[what]);
      break;
    }
    case "code-jump":
      ui.snippet = el.dataset.id; ui.hoodFrom = ui.tab === "lab" ? "lab" : ui.hoodFrom; ui.hoodLabOpen = true; store.set("hoodLabOpen", true);
      ui.tab = "code"; store.set("tab", "code"); $("#main-body").dataset.mode = ""; render(); window.scrollTo({ top: 0 });
      break;
    case "hood-back": ui.tab = "lab"; store.set("tab", "lab"); ui.hoodFrom = null; $("#main-body").dataset.mode = ""; loadCode(); render(); poll(); break;
    case "hood-lab-toggle": ui.hoodLabOpen = !ui.hoodLabOpen; store.set("hoodLabOpen", ui.hoodLabOpen); renderMain(); break;
    case "agent-mode": act(el, () => api("PUT", "/api/agent-mode", { mode: el.dataset.mode }), `New requests use ${el.dataset.label}`); break;
    case "pick-snippet": ui.snippet = el.dataset.id || null; renderMain(); break;
    case "suggest": { const t = document.getElementById(el.dataset.target); if (t) { t.value = el.dataset.text; t.focus(); } break; }
    case "review": {
      const what = el.dataset.do;
      const box = document.getElementById(el.dataset.input);
      let comment = (box?.value || "").trim();
      if (what === "changes" && !comment) { toast("Say what should change first", true); box?.focus(); return; }
      if (what === "reject" && !comment) { comment = prompt("Why are you rejecting these rules?", "Too broad for a production account") ?? ""; if (!comment) return; }
      act(el, async () => { await api("POST", `/api/agents/${encodeURIComponent(el.dataset.wf)}/review`, { decision: what, comment }); if (box) box.value = ""; },
        { approve: "Approved. Staging the rules on Panorama.", changes: "Sent back to Claude with your notes", reject: "Rejected" }[what]);
      break;
    }
    case "bug": act(el, () => api("PUT", `/api/bugs/${el.dataset.id}`, { enabled: el.dataset.on === "1" }), el.dataset.on === "1" ? "Bug mode on. Place a request to hit it." : "Bug mode off"); break;
    case "auto-deploy": act(el, () => api("PUT", "/api/deploy/auto", { enabled: el.dataset.on === "1" })); break;
    case "deploy": act(el, () => api("POST", "/api/deploy"), "Deploying"); break;
    default: break;
  }
});

document.addEventListener("keydown", (e) => {
  const el = e.target.closest?.('[role="button"][data-action]');
  if (el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); el.click(); }
});

document.addEventListener("change", (e) => {
  const el = e.target;
  if (el.dataset?.action === "flaky") {
    act(null, () => api("PUT", `/api/systems/${el.dataset.id}`, { error_rate: Number(el.value) / 100 }));
    el.blur();
  }
});

window.addEventListener("resize", () => { drawEdges(ui.snap?.selected); drawLabArrows(); });
// Lab arrows follow the boxes: redraw when the main area changes size or the web fonts arrive.
if ("ResizeObserver" in window) new ResizeObserver(() => drawLabArrows()).observe($("#main-body"));
document.fonts?.ready.then(() => drawLabArrows());
setInterval(tick, 1000);

// --- intake ----------------------------------------------------------------
function fillPreset(p) {
  if (!p) return;
  $("#intake-text").value = p.text;
  $("#intake-requester").value = p.requester;
}

function openIntake(preset) {
  $("#intake-presets").innerHTML = ui.presets.map((p, i) => `<button type="button" data-action="fill-preset" data-i="${i}">${esc(p.label)}</button>`).join("");
  if (preset) fillPreset(preset);
  else if (!$("#intake-text").value) fillPreset(ui.presets[0]);
  $("#intake").showModal();
  $("#intake-text").focus();
}

$("#intake-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#intake-submit");
  btn.disabled = true;
  try {
    const res = await api("POST", "/api/requests", { text: $("#intake-text").value, requester: $("#intake-requester").value });
    ui.selected = res.workflow_id; ui.track = null; store.set("selected", ui.selected);
    $("#intake").close();
    $("#intake-text").value = "";
    toast(`Placed ${res.id}`);
    $("#main-body").dataset.mode = "";
    await poll(true);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

// --- boot ------------------------------------------------------------------
(async () => {
  try { ui.presets = await api("GET", "/api/presets"); } catch { ui.presets = []; }
  if (ui.tab !== "request") loadCode();
  render();
  poll();
})();
