"""A single-page console for watching runs.

Served from the API process at `/`. One HTML string, no build step, no CDN, no
fifth dependency (ADR-0002) - it talks to the same JSON endpoints any other
client would use, which keeps it honest: nothing is visible here that is not
also available over the API.

What it is for. This runtime's whole argument is that every effect is
authorized, recorded and recoverable, and until now the only way to see that
was `forge trace` or raw JSON. The event stream *is* the product, so the
console renders it as its primary object: phases, policy decisions, dispatches,
reused effects and denials, colour-coded by what they mean rather than by
severity.

The page itself needs no credential - it contains no data. Every request it
makes carries the operator's API key, which is held in localStorage and never
sent anywhere but this origin.
"""

from __future__ import annotations

__all__ = ["DASHBOARD_HTML"]


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FORGE console</title>
<style>
:root{
  --bg:#eef0f3; --surface:#f9fafb; --raise:#fff; --ink:#12171d; --soft:#48535f;
  --muted:#6d7986; --line:#d5dae0; --accent:#2a5d91; --wash:#dee9f4;
  --ok:#2e6f4e; --warn:#8a6a12; --bad:#a03d2b; --gold:#7a5c12;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0e1217; --surface:#151a21; --raise:#1b212a; --ink:#dde3ea; --soft:#aab4c0;
  --muted:#7d8996; --line:#28303b; --accent:#6da8e0; --wash:#132639;
  --ok:#63c08d; --warn:#d4ae4e; --bad:#e28a74; --gold:#d9b451;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
code,.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
header{display:flex;align-items:center;gap:14px;padding:12px 20px;
  background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
h1{font-size:15px;margin:0;letter-spacing:-.01em;font-weight:700}
h1 span{color:var(--muted);font-weight:400}
.grow{flex:1}
button{font:inherit;padding:6px 12px;border-radius:5px;border:1px solid var(--line);
  background:var(--raise);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{opacity:.9;color:#fff}
button:disabled{opacity:.5;cursor:not-allowed}
input{font:inherit;padding:6px 10px;border-radius:5px;border:1px solid var(--line);
  background:var(--raise);color:var(--ink)}
input:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
main{display:grid;grid-template-columns:300px minmax(0,1fr);gap:0;height:calc(100vh - 53px)}
@media (max-width:820px){main{grid-template-columns:1fr;height:auto}}
#list{border-right:1px solid var(--line);overflow-y:auto;background:var(--surface)}
#detail{overflow-y:auto;padding:20px}
.run{padding:11px 16px;border-bottom:1px solid var(--line);cursor:pointer}
.run:hover{background:var(--wash)}
.run.on{background:var(--wash);box-shadow:inset 3px 0 0 var(--accent)}
.run .id{font-size:12.5px;font-weight:600}
.run .meta{font-size:11px;color:var(--muted);margin-top:3px;
  display:flex;gap:8px;flex-wrap:wrap}
.pill{display:inline-block;font-size:10px;letter-spacing:.04em;padding:1.5px 7px;
  border-radius:9px;border:1px solid currentColor;white-space:nowrap}
.s-COMPLETED{color:var(--ok)} .s-RUNNING{color:var(--accent)} .s-PENDING{color:var(--muted)}
.s-FAILED{color:var(--bad)} .s-ABORTED{color:var(--bad)} .s-INTERRUPTED{color:var(--warn)}
.s-SUSPENDED{color:var(--warn)}
.empty{padding:28px 18px;color:var(--muted);font-size:13px}
.card{background:var(--raise);border:1px solid var(--line);border-radius:6px;
  padding:16px;margin-bottom:16px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}
.kv div{min-width:0}
.kv .k{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}
.kv .v{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums;
  overflow-wrap:anywhere}
.answer{border-left:2px solid var(--ok);padding:10px 14px;background:var(--surface);
  border-radius:0 4px 4px 0;margin-top:12px}
h2{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
  margin:0 0 10px;font-weight:600}
.phases{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:4px}
.ph{font-size:10px;padding:2.5px 8px;border-radius:4px;border:1px solid var(--line);
  color:var(--muted)}
.ph.hit{border-color:var(--accent);color:var(--accent);background:var(--wash)}
.ph.term{border-color:var(--ok);color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);font-weight:400;padding:0 10px 7px 0;border-bottom:1px solid var(--line)}
td{padding:6px 10px 6px 0;border-bottom:1px solid var(--line);vertical-align:top}
td.seq{font-variant-numeric:tabular-nums;color:var(--muted);width:52px}
td.step{color:var(--muted);width:44px}
td.type{white-space:nowrap;font-weight:600;width:210px}
td.detail{color:var(--soft);overflow-wrap:anywhere}
.t-ok{color:var(--ok)} .t-bad{color:var(--bad)} .t-warn{color:var(--warn)}
.t-act{color:var(--accent)} .t-gold{color:var(--gold)} .t-dim{color:var(--muted)}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.err{color:var(--bad);font-size:12.5px;margin-left:4px}
.live{font-size:10px;color:var(--accent)}
</style>
</head>
<body>
<header>
  <h1>FORGE <span>console</span></h1>
  <span id="live" class="live"></span>
  <span class="grow"></span>
  <input id="key" type="password" placeholder="API key" size="16" autocomplete="off">
  <button id="save">Use</button>
  <button id="refresh">Refresh</button>
</header>

<main>
  <div id="list"><div class="empty">Loading&hellip;</div></div>
  <div id="detail">
    <div class="bar">
      <input id="goal" placeholder="Ask the agent to do something&hellip;" style="flex:1;min-width:220px">
      <button id="start" class="primary">Start run</button>
      <span id="err" class="err"></span>
    </div>
    <div class="empty">Select a run on the left, or start one above.</div>
  </div>
</main>

<script>
const PHASES = ["BOOT","VIEW","PROPOSE","VALIDATE","AUTHORIZE","DISPATCH","OBSERVE",
                "RECONCILE","COMMIT","EVALUATE","CONTINUE","COMPLETE","HANDOFF",
                "FAILED","ABORTED"];
const TERMINAL = new Set(["COMPLETE","HANDOFF","FAILED","ABORTED"]);

// How each event type reads. Colour carries meaning, never decoration.
const TONE = {
  RUN_CREATED:"t-dim", RUN_RESUMED:"t-warn", RUN_COMPLETED:"t-ok",
  RUN_FAILED:"t-bad", RUN_ABORTED:"t-bad",
  PROPOSAL_RECEIVED:"t-act", PROPOSAL_REJECTED:"t-warn",
  POLICY_DECIDED:"t-act", PERMIT_ISSUED:"t-dim",
  ACTION_DISPATCHED:"t-act", EFFECT_OBSERVED:"t-ok", EFFECT_REUSED:"t-gold",
  EFFECT_RECONCILED:"t-dim", COMPENSATION_APPLIED:"t-warn",
  STEP_COMMITTED:"t-ok", LOOP_DETECTED:"t-bad", ERROR_RAISED:"t-bad",
  RETRY_SCHEDULED:"t-warn", APPROVAL_REQUESTED:"t-warn",
  NOTE_PROPOSED:"t-act", ATTESTATION_RECORDED:"t-act",
};

let key = "";
try { key = localStorage.getItem("forge_key") || ""; } catch (e) { key = ""; }
let current = null, timer = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function api(path, opts) {
  const o = Object.assign({headers:{}}, opts || {});
  if (key) o.headers["Authorization"] = "Bearer " + key;
  if (o.body) o.headers["Content-Type"] = "application/json";
  const r = await fetch(path, o);
  if (r.status === 401) throw new Error("unauthorized - check the API key");
  if (!r.ok) throw new Error("HTTP " + r.status + " " + (await r.text()).slice(0,120));
  return r.json();
}

function fmt(ts){ try { return new Date(ts).toLocaleTimeString(); } catch(e){ return ""; } }

// One readable line per event. The payload keys worth surfacing differ by
// type, so this is a small explicit map rather than a JSON dump.
function summarise(e) {
  const p = e.payload || {};
  const bits = [];
  if (p.phase) bits.push(p.phase);
  if (p.tool) bits.push(p.tool);
  if (p.decision) bits.push(p.decision);
  if (p.capability) bits.push(p.capability);
  if (p.reason) bits.push(p.reason);
  if (p.detail) bits.push(p.detail);
  if (p.action) bits.push("(" + p.action + ")");
  if (p.error) bits.push(String(p.error).slice(0,160));
  if (p.answer) bits.push(String(p.answer).slice(0,160));
  if (p.model) bits.push(p.model);
  if (p.ok === false) bits.push("failed");
  if (p.output !== undefined && !p.tool) bits.push(String(p.output).slice(0,120));
  return bits.join(" · ");
}

async function loadRuns() {
  try {
    const runs = await api("/runs");
    const el = $("list");
    if (!runs.length) { el.innerHTML = '<div class="empty">No runs yet.</div>'; return; }
    el.innerHTML = runs.map(r => `
      <div class="run ${r.run_id === current ? "on" : ""}" data-id="${esc(r.run_id)}">
        <div class="id mono">${esc(r.run_id)}</div>
        <div class="meta">
          <span class="pill s-${esc(r.status || "PENDING")}">${esc(r.status || "PENDING")}</span>
          <span>${r.events ?? 0} events</span>
        </div>
      </div>`).join("");
    el.querySelectorAll(".run").forEach(n =>
      n.onclick = () => openRun(n.dataset.id));
  } catch (e) { $("list").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
}

async function openRun(id) {
  current = id;
  document.querySelectorAll(".run").forEach(n =>
    n.classList.toggle("on", n.dataset.id === id));
  await render();
}

async function render() {
  if (!current) return;
  let view, events;
  try {
    [view, events] = await Promise.all([
      api("/runs/" + current), api("/runs/" + current + "/events")
    ]);
  } catch (e) { $("detail").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; return; }

  const seen = new Set(events.filter(e => e.type === "PHASE_ENTERED")
                             .map(e => (e.payload || {}).phase));
  const status = view.status || "PENDING";
  const dup = view.duplicate_effects ?? 0;
  const denials = events.filter(e =>
    e.type === "POLICY_DECIDED" && (e.payload || {}).decision === "DENY").length;
  const reused = events.filter(e => e.type === "EFFECT_REUSED").length;

  $("detail").innerHTML = `
    <div class="bar">
      <input id="goal" placeholder="Ask the agent to do something&hellip;" style="flex:1;min-width:220px">
      <button id="start" class="primary">Start run</button>
      <button id="resume">Resume this run</button>
      <span id="err" class="err"></span>
    </div>

    <div class="card">
      <div class="kv">
        <div><div class="k">Run</div><div class="v mono" style="font-size:13px">${esc(current)}</div></div>
        <div><div class="k">Status</div><div class="v"><span class="pill s-${esc(status)}">${esc(status)}</span></div></div>
        <div><div class="k">Steps</div><div class="v">${view.steps ?? 0}</div></div>
        <div><div class="k">Tokens</div><div class="v">${(view.usage && view.usage.total_tokens) ?? 0}</div></div>
        <div><div class="k">Duplicate effects</div>
             <div class="v" style="color:${dup ? "var(--bad)" : "var(--ok)"}">${dup}</div></div>
        <div><div class="k">Effects reused</div><div class="v" style="color:var(--gold)">${reused}</div></div>
        <div><div class="k">Policy denials</div>
             <div class="v" style="color:${denials ? "var(--warn)" : "var(--muted)"}">${denials}</div></div>
      </div>
      ${view.answer ? '<div class="answer">' + esc(view.answer) + "</div>" : ""}
    </div>

    <div class="card">
      <h2>Lifecycle reached</h2>
      <div class="phases">${PHASES.map(p =>
        `<span class="ph ${seen.has(p) ? (TERMINAL.has(p) ? "hit term" : "hit") : ""}">${p}</span>`
      ).join("")}</div>
    </div>

    <div class="card">
      <h2>Audit trail &mdash; ${events.length} events</h2>
      <div style="overflow-x:auto">
      <table><thead><tr><th>seq</th><th>step</th><th>event</th><th>detail</th><th>time</th></tr></thead>
      <tbody>${events.map(e => `
        <tr>
          <td class="seq mono">${e.seq}</td>
          <td class="step mono">${e.step_index ?? ""}</td>
          <td class="type mono ${TONE[e.type] || "t-dim"}">${esc(e.type)}</td>
          <td class="detail mono">${esc(summarise(e))}</td>
          <td class="seq mono">${fmt(e.ts)}</td>
        </tr>`).join("")}</tbody></table>
      </div>
    </div>`;

  wireBar();
  $("resume").onclick = async () => {
    try { await api("/runs/" + current + "/resume", {method:"POST"}); schedule(true); }
    catch (e) { $("err").textContent = e.message; }
  };
  schedule(status === "RUNNING" || status === "PENDING");
}

function wireBar() {
  const start = $("start"), goal = $("goal");
  if (!start) return;
  start.onclick = async () => {
    const text = (goal.value || "").trim();
    if (!text) return;
    start.disabled = true; $("err").textContent = "";
    try {
      const r = await api("/runs", {method:"POST", body: JSON.stringify({goal: text})});
      goal.value = "";
      await loadRuns();
      await openRun(r.run_id);
    } catch (e) { $("err").textContent = e.message; }
    finally { start.disabled = false; }
  };
  goal.onkeydown = (ev) => { if (ev.key === "Enter") start.click(); };
}

// Poll only while something is actually moving, and say so.
function schedule(active) {
  if (timer) { clearTimeout(timer); timer = null; }
  $("live").textContent = active ? "live" : "";
  if (!active) return;
  timer = setTimeout(async () => { await loadRuns(); await render(); }, 1500);
}

$("save").onclick = () => {
  key = $("key").value.trim();
  try { localStorage.setItem("forge_key", key); } catch (e) { /* private mode */ }
  loadRuns(); if (current) render();
};
$("refresh").onclick = () => { loadRuns(); if (current) render(); };
$("key").value = key;
$("key").onkeydown = (ev) => { if (ev.key === "Enter") $("save").click(); };

wireBar();
loadRuns();
</script>
</body>
</html>
"""
