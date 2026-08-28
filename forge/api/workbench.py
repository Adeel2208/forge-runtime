"""The coding workbench: an editor-shaped view of the agent's work.

Three panes, in the order the work actually flows: the repository on the left,
what changed in the middle, the agent on the right. A file tree is table
stakes; the pane that matters is the diff, because the whole safety model of
this agent is that its work lands on a branch and merges only when a human
says so. The interface should make reading that diff the easy path and
merging the deliberate one.

Self-contained, like the run console: one HTML string, no build step, no CDN,
no fifth dependency. Syntax highlighting is a small tokeniser over a handful of
languages rather than a 300KB library - enough that code reads as code, and
honest about doing less than an editor would.
"""

from __future__ import annotations

__all__ = ["WORKBENCH_HTML"]


WORKBENCH_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FORGE workbench</title>
<style>
:root{
  --bg:#eceef1; --panel:#f7f8f9; --raise:#fff; --ink:#12171d; --soft:#48535f;
  --muted:#6d7986; --line:#d5dae0; --accent:#2a5d91; --wash:#dde9f4;
  --ok:#2e6f4e; --warn:#8a6a12; --bad:#a03d2b; --gold:#7a5c12;
  --add:#e4f2e8; --addln:#2e6f4e; --del:#fbe7e3; --delln:#a03d2b;
  --kw:#8250a8; --str:#2e6f4e; --num:#a0522d; --com:#8792a0;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0d1116; --panel:#141a21; --raise:#1a212a; --ink:#dde3ea; --soft:#aab4c0;
  --muted:#7d8996; --line:#262e39; --accent:#6da8e0; --wash:#132639;
  --ok:#63c08d; --warn:#d4ae4e; --bad:#e28a74; --gold:#d9b451;
  --add:#132a1e; --addln:#63c08d; --del:#2c1714; --delln:#e28a74;
  --kw:#c9a0e8; --str:#7fc9a0; --num:#d9a06a; --com:#6b7684;
}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);overflow:hidden;
  font:13.5px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.mono,code,pre{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}

/* chrome */
#top{display:flex;align-items:center;gap:14px;height:44px;padding:0 14px;
  background:var(--panel);border-bottom:1px solid var(--line)}
#top b{font-size:13px;letter-spacing:-.01em}
#top .sep{color:var(--muted)}
.grow{flex:1}
.chip{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line);
  color:var(--muted);white-space:nowrap}
.chip.on{border-color:var(--accent);color:var(--accent)}
button{font:inherit;font-size:12.5px;padding:5px 11px;border-radius:5px;
  border:1px solid var(--line);background:var(--raise);color:var(--ink);cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button:disabled{opacity:.45;cursor:not-allowed}
button.go{background:var(--accent);border-color:var(--accent);color:#fff}
button.go:hover:not(:disabled){opacity:.9;color:#fff}
button.danger:hover:not(:disabled){border-color:var(--bad);color:var(--bad)}
input,textarea{font:inherit;padding:7px 10px;border-radius:5px;
  border:1px solid var(--line);background:var(--raise);color:var(--ink);width:100%}
textarea{resize:none;font-size:13px}
input:focus-visible,textarea:focus-visible,button:focus-visible{
  outline:2px solid var(--accent);outline-offset:1px}

/* layout */
#shell{display:grid;grid-template-columns:236px minmax(0,1fr) 340px;
  height:calc(100vh - 44px)}
@media (max-width:1000px){#shell{grid-template-columns:1fr;overflow-y:auto;height:auto}}
.pane{min-width:0;display:flex;flex-direction:column;overflow:hidden}
#files{background:var(--panel);border-right:1px solid var(--line)}
#agent{background:var(--panel);border-left:1px solid var(--line)}
.hd{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);
  padding:10px 12px 8px;border-bottom:1px solid var(--line);display:flex;
  align-items:center;gap:8px}
.hd .grow{flex:1}
.scroll{overflow:auto;flex:1}

/* file tree */
.f{padding:3.5px 12px 3.5px 22px;cursor:pointer;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;font-size:12.5px}
.f:hover{background:var(--wash)}
.f.on{background:var(--wash);color:var(--accent);font-weight:600}
.f.touched::before{content:"●";color:var(--gold);margin-left:-10px;
  margin-right:4px;font-size:9px;vertical-align:middle}
.dirhd{padding:7px 12px 3px;font-size:10.5px;color:var(--muted);
  letter-spacing:.05em;text-transform:uppercase}

/* tabs + code */
#tabs{display:flex;gap:2px;padding:6px 8px 0;background:var(--panel);
  border-bottom:1px solid var(--line);overflow-x:auto}
.tab{font-size:12px;padding:6px 12px;border:1px solid transparent;
  border-bottom:none;border-radius:5px 5px 0 0;cursor:pointer;color:var(--muted);
  white-space:nowrap}
.tab.on{background:var(--raise);border-color:var(--line);color:var(--ink)}
#view{flex:1;overflow:auto;background:var(--raise)}
pre{margin:0;padding:14px 16px 40px;font-size:12.5px;line-height:1.62;
  tab-size:4;white-space:pre}
.ln{color:var(--muted);user-select:none;display:inline-block;width:3.2em;
  text-align:right;padding-right:1.1em;opacity:.65}
.kw{color:var(--kw)} .str{color:var(--str)} .num{color:var(--num)}
.com{color:var(--com);font-style:italic}
.d-add{background:var(--add);color:var(--addln);display:block}
.d-del{background:var(--del);color:var(--delln);display:block}
.d-hd{color:var(--accent);display:block;font-weight:600}
.d-at{color:var(--muted);display:block;margin-top:6px}
.blank{padding:44px 20px;color:var(--muted);text-align:center;font-size:13px}

/* agent pane */
#hist{flex:1;overflow-y:auto;padding:10px 12px;display:flex;
  flex-direction:column;gap:10px}
.t{border:1px solid var(--line);border-radius:6px;background:var(--raise);
  padding:10px 12px;cursor:pointer}
.t.on{border-color:var(--accent)}
.t .goal{font-size:12.5px;line-height:1.45}
.t .row{display:flex;gap:7px;align-items:center;margin-top:7px;flex-wrap:wrap;
  font-size:10.5px;color:var(--muted)}
.st{font-size:10px;padding:1.5px 7px;border-radius:9px;border:1px solid currentColor}
.st.running{color:var(--accent)} .st.completed{color:var(--ok)}
.st.failed{color:var(--bad)} .st.merged{color:var(--ok)}
.st.discarded{color:var(--muted)}
.acts{display:flex;gap:6px;margin-top:9px}
#compose{border-top:1px solid var(--line);padding:10px 12px;
  display:flex;flex-direction:column;gap:8px;background:var(--panel)}
.err{color:var(--bad);font-size:11.5px}
.note{font-size:11px;color:var(--muted);line-height:1.45}
.spin{display:inline-block;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
</style>
</head>
<body>

<div id="top">
  <b>FORGE</b><span class="sep">/</span>
  <span id="repo" class="mono">&hellip;</span>
  <span id="branch" class="chip"></span>
  <span id="dirty" class="chip"></span>
  <span class="grow"></span>
  <span id="model" class="chip"></span>
  <span id="policy" class="chip"></span>
  <input id="key" type="password" placeholder="API key" style="width:120px" autocomplete="off">
  <button id="save">Use</button>
  <button id="runs">Runs</button>
</div>

<div id="shell">
  <div class="pane" id="files">
    <div class="hd"><span class="grow">Repository</span><span id="fcount"></span></div>
    <div class="scroll" id="tree"><div class="blank">Loading&hellip;</div></div>
  </div>

  <div class="pane">
    <div id="tabs"></div>
    <div id="view"><div class="blank">Select a file, or give the agent a task.</div></div>
  </div>

  <div class="pane" id="agent">
    <div class="hd"><span class="grow">Agent</span><span id="busy"></span></div>
    <div id="hist"><div class="blank">No tasks yet.</div></div>
    <div id="compose">
      <textarea id="goal" rows="3"
        placeholder="Describe a change. The agent works on its own branch."></textarea>
      <div style="display:flex;gap:8px;align-items:center">
        <button id="send" class="go">Give task</button>
        <span id="cerr" class="err"></span>
      </div>
      <div class="note">Nothing reaches your branch until you press Merge.</div>
    </div>
  </div>
</div>

<script>
let key = "";
try { key = localStorage.getItem("forge_key") || ""; } catch (e) {}
let tree = [], openFile = null, tasks = [], current = null, mode = "file", timer = null;

const $ = (i) => document.getElementById(i);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function api(path, opts) {
  const o = Object.assign({headers:{}}, opts || {});
  if (key) o.headers["Authorization"] = "Bearer " + key;
  if (o.body) o.headers["Content-Type"] = "application/json";
  const r = await fetch(path, o);
  if (r.status === 401) throw new Error("unauthorized - check the API key");
  if (!r.ok) {
    let d; try { d = (await r.json()).detail; } catch (e) { d = await r.text(); }
    throw new Error(String(d).slice(0, 140));
  }
  return r.json();
}

/* A small tokeniser, not a library. Enough that code reads as code. */
const KW = /\\b(async|await|def|class|return|if|elif|else|for|while|try|except|finally|with|import|from|as|raise|yield|lambda|pass|break|continue|not|and|or|in|is|None|True|False|self|const|let|var|function|new|typeof|interface|type|export|public|private|struct|enum|fn|impl|pub|match|use|mod|nil|func|package|end|do|then)\\b/g;
function hl(line) {
  let s = esc(line);
  const holes = [];
  const stash = (cls, txt) => { holes.push('<span class="' + cls + '">' + txt + '</span>');
                                return "\\u0000" + (holes.length - 1) + "\\u0000"; };
  s = s.replace(/(#|\\/\\/).*$/g, m => stash("com", m));
  s = s.replace(/("([^"\\\\]|\\\\.)*"|'([^'\\\\]|\\\\.)*')/g, m => stash("str", m));
  s = s.replace(KW, m => stash("kw", m));
  s = s.replace(/\\b\\d+(\\.\\d+)?\\b/g, m => stash("num", m));
  return s.replace(/\\u0000(\\d+)\\u0000/g, (_, i) => holes[+i]);
}

function renderFile(path, content) {
  mode = "file"; openFile = path;
  const lines = content.split("\\n");
  $("view").innerHTML = "<pre>" + lines.map((l, i) =>
    '<span class="ln">' + (i + 1) + "</span>" + hl(l)).join("\\n") + "</pre>";
  drawTabs();
  document.querySelectorAll(".f").forEach(n => n.classList.toggle("on", n.dataset.p === path));
}

function renderDiff(text) {
  mode = "diff";
  if (!text.trim()) {
    $("view").innerHTML = '<div class="blank">This task changed nothing.</div>';
    drawTabs(); return;
  }
  const html = text.split("\\n").map(l => {
    if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff ") ||
        l.startsWith("index ")) return '<span class="d-hd">' + esc(l) + "</span>";
    if (l.startsWith("@@")) return '<span class="d-at">' + esc(l) + "</span>";
    if (l.startsWith("+")) return '<span class="d-add">' + esc(l) + "</span>";
    if (l.startsWith("-")) return '<span class="d-del">' + esc(l) + "</span>";
    return "<span>" + esc(l) + "</span>";
  }).join("");
  $("view").innerHTML = "<pre>" + html + "</pre>";
  drawTabs();
}

function drawTabs() {
  const t = [];
  if (openFile) t.push(`<div class="tab ${mode === "file" ? "on" : ""}" data-k="file">${esc(openFile)}</div>`);
  if (current) t.push(`<div class="tab ${mode === "diff" ? "on" : ""}" data-k="diff">diff &middot; ${esc(current.branch || current.id)}</div>`);
  $("tabs").innerHTML = t.join("");
  $("tabs").querySelectorAll(".tab").forEach(n => n.onclick = () => {
    if (n.dataset.k === "file" && openFile) openPath(openFile);
    else if (current) showDiff(current.id);
  });
}

async function loadStatus() {
  try {
    const s = await api("/code/status");
    $("repo").textContent = s.name;
    $("branch").textContent = s.branch;
    $("branch").className = "chip on";
    $("dirty").textContent = s.clean ? "clean" : s.dirty_files.length + " uncommitted";
    $("model").textContent = s.model;
    $("policy").textContent = s.policy;
    $("send").disabled = s.busy;
    $("busy").innerHTML = s.busy ? '<span class="spin">◐</span>' : "";
    return s;
  } catch (e) { $("repo").textContent = e.message; return null; }
}

async function loadTree() {
  try {
    tree = (await api("/code/tree")).files;
    $("fcount").textContent = tree.length;
    const groups = {};
    for (const f of tree) {
      const i = f.lastIndexOf("/");
      (groups[i < 0 ? "" : f.slice(0, i)] ||= []).push(f);
    }
    const touched = new Set((current && current.files) || []);
    $("tree").innerHTML = Object.keys(groups).sort().map(d =>
      (d ? `<div class="dirhd">${esc(d)}</div>` : "") +
      groups[d].map(f => `<div class="f ${touched.has(f) ? "touched" : ""}" data-p="${esc(f)}">${esc(f.split("/").pop())}</div>`).join("")
    ).join("");
    $("tree").querySelectorAll(".f").forEach(n => n.onclick = () => openPath(n.dataset.p));
  } catch (e) { $("tree").innerHTML = '<div class="blank">' + esc(e.message) + "</div>"; }
}

async function openPath(p) {
  try {
    const f = await api("/code/file?path=" + encodeURIComponent(p));
    if (f.binary) { $("view").innerHTML = '<div class="blank">Binary file.</div>'; openFile = p; drawTabs(); return; }
    renderFile(p, f.content);
  } catch (e) { $("view").innerHTML = '<div class="blank">' + esc(e.message) + "</div>"; }
}

async function showDiff(id) {
  current = tasks.find(t => t.id === id) || current;
  try { renderDiff((await api("/code/tasks/" + id + "/diff")).diff); }
  catch (e) { $("view").innerHTML = '<div class="blank">' + esc(e.message) + "</div>"; }
  drawHistory();
}

function drawHistory() {
  if (!tasks.length) { $("hist").innerHTML = '<div class="blank">No tasks yet.</div>'; return; }
  $("hist").innerHTML = tasks.map(t => {
    const state = t.merged ? "merged" : t.discarded ? "discarded" : t.status;
    const canAct = t.status === "completed" && t.commits > 0 && !t.merged && !t.discarded;
    return `<div class="t ${current && current.id === t.id ? "on" : ""}" data-id="${t.id}">
      <div class="goal">${esc(t.goal)}</div>
      <div class="row">
        <span class="st ${state}">${state}</span>
        ${t.commits ? `<span>${t.commits} commit${t.commits > 1 ? "s" : ""}</span>` : ""}
        ${t.files.length ? `<span>${t.files.length} file${t.files.length > 1 ? "s" : ""}</span>` : ""}
        ${t.branch ? `<span class="mono">${esc(t.branch)}</span>` : ""}
      </div>
      ${t.error ? `<div class="row err">${esc(t.error)}</div>` : ""}
      ${canAct ? `<div class="acts">
        <button data-a="diff" data-id="${t.id}">Review diff</button>
        <button data-a="merge" data-id="${t.id}" class="go">Merge</button>
        <button data-a="undo" data-id="${t.id}" class="danger">Discard</button>
      </div>` : ""}
    </div>`;
  }).join("");

  $("hist").querySelectorAll(".t").forEach(n => n.onclick = (ev) => {
    if (ev.target.dataset.a) return;
    showDiff(n.dataset.id);
  });
  $("hist").querySelectorAll("button[data-a]").forEach(b => b.onclick = async (ev) => {
    ev.stopPropagation();
    const id = b.dataset.id;
    if (b.dataset.a === "diff") return showDiff(id);
    if (b.dataset.a === "undo" && !confirm("Discard this task's branch? The work is lost.")) return;
    b.disabled = true;
    try { await api("/code/tasks/" + id + "/" + (b.dataset.a === "merge" ? "accept" : "undo"),
                    {method:"POST"}); await refresh(); }
    catch (e) { $("cerr").textContent = e.message; b.disabled = false; }
  });
}

async function refresh() {
  const s = await loadStatus();
  try {
    tasks = await api("/code/tasks");
    if (current) current = tasks.find(t => t.id === current.id) || current;
    drawHistory();
  } catch (e) {}
  await loadTree();
  if (timer) clearTimeout(timer);
  if (s && s.busy) timer = setTimeout(refresh, 1500);
}

$("send").onclick = async () => {
  const goal = $("goal").value.trim();
  if (!goal) return;
  $("send").disabled = true; $("cerr").textContent = "";
  try {
    const t = await api("/code/tasks", {method:"POST", body: JSON.stringify({goal})});
    $("goal").value = ""; current = t;
    await refresh();
  } catch (e) { $("cerr").textContent = e.message; $("send").disabled = false; }
};
$("goal").onkeydown = (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) $("send").click();
};
$("save").onclick = () => {
  key = $("key").value.trim();
  try { localStorage.setItem("forge_key", key); } catch (e) {}
  refresh();
};
$("key").value = key;
$("key").onkeydown = (e) => { if (e.key === "Enter") $("save").click(); };
$("runs").onclick = () => { location.href = "/"; };

refresh();
</script>
</body>
</html>
"""
