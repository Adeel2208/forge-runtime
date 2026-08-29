"""FORGE Studio: the coding agent as an application.

An editor shell, not a web page. Tabs, a command palette, resizable panes,
search, keyboard-first navigation, and layout that survives a reload - the
conventions people already know from their editor, so nothing here has to be
learned.

**Why this is not Electron.** A desktop build needs Node or Rust, a bundler and
a packaging step, and this project's whole distribution story is `pip install`
and four dependencies (ADR-0002). So the app is served by the process that
already exists and `forge app` opens it chromeless - a real window, no browser
furniture, no toolchain. The trade is deliberate: no auto-update and no native
menus, in exchange for `pip install` staying the only build step there is.

The layout follows what the work actually is. The agent proposes; a human
decides. So the diff is the centre of the screen and Merge is one deliberate
click away from reading it, never the default action.
"""

from __future__ import annotations

__all__ = ["WORKBENCH_HTML"]


WORKBENCH_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FORGE Studio</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<meta name="theme-color" content="#16202b">
<meta name="apple-mobile-web-app-capable" content="yes">
<style>
:root{
  --bg:#eceef1;--panel:#f6f7f9;--raise:#fff;--ink:#12171d;--soft:#48535f;
  --muted:#6d7986;--line:#d6dbe1;--accent:#2a5d91;--wash:#dce8f4;--sel:#cfe0f0;
  --ok:#2e6f4e;--warn:#8a6a12;--bad:#a03d2b;--gold:#7a5c12;
  --add:#e3f2e8;--addf:#1e5c3c;--del:#fbe6e2;--delf:#8f3323;
  --kw:#8250a8;--str:#2e6f4e;--num:#a0522d;--com:#8792a0;--fn:#2a5d91;
  --bar:#2a5d91;--barf:#fff;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0c1015;--panel:#12181f;--raise:#171e26;--ink:#dde3ea;--soft:#aab4c0;
  --muted:#7d8996;--line:#242c37;--accent:#6da8e0;--wash:#122437;--sel:#1b3247;
  --ok:#63c08d;--warn:#d4ae4e;--bad:#e28a74;--gold:#d9b451;
  --add:#112a1d;--addf:#6ecf9a;--del:#2c1613;--delf:#ef9c86;
  --kw:#c9a0e8;--str:#7fc9a0;--num:#d9a06a;--com:#6b7684;--fn:#6da8e0;
  --bar:#16202b;--barf:#aab4c0;
}}
*{box-sizing:border-box}
html,body{height:100%;margin:0;overflow:hidden}
body{background:var(--bg);color:var(--ink);
  font:13px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.mono,pre,code,input.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
button{font:inherit;font-size:12px;padding:4px 10px;border-radius:5px;
  border:1px solid var(--line);background:var(--raise);color:var(--ink);cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
button.go{background:var(--accent);border-color:var(--accent);color:#fff}
button.go:hover:not(:disabled){opacity:.88;color:#fff}
button.bad:hover:not(:disabled){border-color:var(--bad);color:var(--bad)}
select{font:inherit;font-size:11.5px;padding:3px 6px;border-radius:5px;
  border:1px solid var(--line);background:var(--raise);color:var(--ink);max-width:190px}
select:hover{border-color:var(--accent)}
select:disabled{opacity:.5}
input,textarea{font:inherit;padding:6px 9px;border-radius:5px;border:1px solid var(--line);
  background:var(--raise);color:var(--ink);width:100%}
textarea{resize:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line);border-radius:5px}

/* frame */
#app{display:grid;grid-template-rows:36px 1fr 22px;height:100vh}
#title{display:flex;align-items:center;gap:10px;padding:0 10px;background:var(--panel);
  border-bottom:1px solid var(--line);-webkit-app-region:drag;user-select:none}
#title b{font-size:12px;letter-spacing:.02em}
#title .x{-webkit-app-region:no-drag}
.grow{flex:1}
.chip{font-size:10.5px;padding:1.5px 7px;border-radius:9px;border:1px solid var(--line);
  color:var(--muted);white-space:nowrap}
.chip.on{border-color:var(--accent);color:var(--accent)}

#body{display:grid;grid-template-columns:var(--lw,250px) 5px minmax(0,1fr) 5px var(--rw,330px);
  min-height:0}
.gut{background:transparent;cursor:col-resize}
.gut:hover{background:var(--accent)}
.col{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}
#left{background:var(--panel);border-right:1px solid var(--line)}
#right{background:var(--panel);border-left:1px solid var(--line)}
.hd{font-size:10px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);
  padding:9px 11px 7px;display:flex;gap:7px;align-items:center;flex-shrink:0}
.scroll{overflow:auto;flex:1;min-height:0}

/* explorer */
#filter{margin:0 9px 7px;font-size:12px;padding:5px 8px}
.node{padding:2.5px 10px;cursor:pointer;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;font-size:12.5px;border-radius:4px;margin:0 5px}
.node:hover{background:var(--wash)}
.node.on{background:var(--sel);color:var(--accent);font-weight:600}
.node .tw{display:inline-block;width:11px;color:var(--muted);font-size:9px}
.node.dir{color:var(--soft);font-weight:600}
.node.touch::after{content:"●";color:var(--gold);margin-left:6px;font-size:8px;
  vertical-align:middle}

/* tabs + editor */
#tabs{display:flex;gap:1px;background:var(--panel);border-bottom:1px solid var(--line);
  overflow-x:auto;flex-shrink:0;min-height:31px}
.tab{display:flex;align-items:center;gap:7px;font-size:12px;padding:7px 9px 7px 12px;
  cursor:pointer;color:var(--muted);border-right:1px solid var(--line);white-space:nowrap}
.tab.on{background:var(--raise);color:var(--ink)}
.tab .cl{opacity:.45;font-size:14px;line-height:1}
.tab .cl:hover{opacity:1;color:var(--bad)}
.tab.dirty .cl::before{content:"●";font-size:9px}
#stage{flex:1;min-height:0;overflow:auto;background:var(--raise);position:relative}
pre{margin:0;padding:12px 14px 60px;font-size:12.5px;line-height:1.6;tab-size:4;
  white-space:pre;min-height:100%}
#pane{position:absolute;inset:0;display:none;grid-template-columns:auto 1fr}
#gutter{padding:12px 8px 60px 12px;text-align:right;color:var(--muted);opacity:.55;
  font-size:12.5px;line-height:1.6;user-select:none;overflow:hidden;
  background:var(--raise);border-right:1px solid var(--line);white-space:pre}
#edit{padding:12px 14px 60px;border:0;border-radius:0;font-size:12.5px;
  line-height:1.6;tab-size:4;white-space:pre;overflow:auto;background:var(--raise);
  resize:none;width:100%;height:100%}
#find{position:absolute;top:8px;right:18px;display:none;gap:6px;align-items:center;
  background:var(--panel);border:1px solid var(--line);border-radius:7px;
  padding:6px 8px;box-shadow:0 8px 22px -12px rgba(0,0,0,.5);z-index:5}
#find input{width:190px;font-size:12px;padding:4px 8px}
#find .n{font-size:11px;color:var(--muted);white-space:nowrap}
mark{background:var(--gold);color:#000}
.ln{color:var(--muted);user-select:none;display:inline-block;width:3.4em;
  text-align:right;padding-right:1.2em;opacity:.5}
.kw{color:var(--kw)}.str{color:var(--str)}.num{color:var(--num)}
.com{color:var(--com);font-style:italic}.fn{color:var(--fn)}
.dl{display:block}.dl.a{background:var(--add);color:var(--addf)}
.dl.d{background:var(--del);color:var(--delf)}
.dl.h{color:var(--accent);font-weight:600}.dl.at{color:var(--muted);margin-top:8px}
.empty{padding:50px 24px;color:var(--muted);text-align:center;font-size:12.5px;
  line-height:1.7}
.empty kbd{font-size:11px}

/* agent */
#tasks{flex:1;overflow-y:auto;padding:8px 9px;display:flex;flex-direction:column;gap:8px}
.tk{border:1px solid var(--line);border-radius:6px;background:var(--raise);padding:9px 10px;
  cursor:pointer}
.tk.on{border-color:var(--accent)}
.tk .g{font-size:12.5px;line-height:1.4}
.tk .m{display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap;
  font-size:10.5px;color:var(--muted)}
.st{font-size:9.5px;padding:1px 6px;border-radius:8px;border:1px solid currentColor}
.st.running{color:var(--accent)}.st.completed{color:var(--ok)}.st.failed{color:var(--bad)}
.st.merged{color:var(--ok)}.st.discarded{color:var(--muted)}.st.cancelled{color:var(--warn)}
.acts{display:flex;gap:5px;margin-top:8px;flex-wrap:wrap}
.files{display:flex;flex-direction:column;gap:2px;margin-top:6px}
.stk{font-size:10.5px;color:var(--warn);margin-top:6px;line-height:1.4}
.chg{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:var(--gold);
  cursor:pointer;padding:1px 4px;border-radius:3px}
.chg:hover{background:var(--wash);color:var(--accent)}
.prog{margin-top:7px;border-top:1px solid var(--line);padding-top:6px;
  font-size:11px;max-height:150px;overflow-y:auto}
.pl{padding:1px 0;color:var(--muted)}
.pl.step{color:var(--soft);font-weight:600;margin-top:3px}
.pl.ok{color:var(--ok)}.pl.bad{color:var(--bad)}.pl.warn{color:var(--warn)}
.pl.plan{color:var(--accent)}
#ask{border-top:1px solid var(--line);padding:9px;background:var(--panel);flex-shrink:0}
#goal{font-size:12.5px}
.row{display:flex;gap:7px;align-items:center;margin-top:7px}
.hint{font-size:10.5px;color:var(--muted);margin-top:6px;line-height:1.45}
.warnbox{font-size:11px;color:var(--warn);margin-top:7px;line-height:1.45;
  border-left:2px solid var(--warn);padding-left:8px}
.err{color:var(--bad);font-size:11px}

/* status bar */
#bar{display:flex;align-items:center;gap:14px;padding:0 11px;background:var(--bar);
  color:var(--barf);font-size:11px;user-select:none}
#bar .s{cursor:pointer}
#bar .s:hover{opacity:.75}

/* palette + toasts */
#veil{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;z-index:50}
#pal{position:fixed;top:14vh;left:50%;transform:translateX(-50%);width:min(620px,92vw);
  background:var(--raise);border:1px solid var(--line);border-radius:9px;
  box-shadow:0 20px 60px -18px rgba(0,0,0,.5);display:none;z-index:51;overflow:hidden}
#palin{border:0;border-bottom:1px solid var(--line);border-radius:0;padding:12px 14px;
  font-size:14px}
#palls{max-height:52vh;overflow-y:auto}
.pi{padding:8px 14px;cursor:pointer;display:flex;gap:10px;align-items:baseline;font-size:12.5px}
.pi.on{background:var(--wash)}
.pi .k{color:var(--muted);font-size:11px;margin-left:auto}
.pi .sub{color:var(--muted);font-size:11px}
#toasts{position:fixed;right:14px;bottom:34px;display:flex;flex-direction:column;
  gap:7px;z-index:60}
.toast{background:var(--raise);border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:6px;padding:9px 13px;font-size:12px;box-shadow:0 8px 24px -12px rgba(0,0,0,.5);
  max-width:330px}
.toast.ok{border-left-color:var(--ok)}.toast.bad{border-left-color:var(--bad)}
@media (max-width:900px){#body{grid-template-columns:1fr;grid-template-rows:auto auto 1fr}
  .gut{display:none}#left,#right{max-height:34vh}}
</style>
</head>
<body>
<div id="app">

  <div id="title">
    <b>FORGE Studio</b>
    <span id="repo" class="chip"></span>
    <span id="branch" class="chip"></span>
    <span id="dirty" class="chip" title="Your uncommitted changes"></span>
    <span class="grow"></span>
    <select class="x" id="model" title="Local model used by the agent"></select>
    <button class="x" id="install" style="display:none">Install app</button>
    <button class="x" id="cmd">⌘K Commands</button>
    <input class="x mono" id="key" type="password" placeholder="API key"
           style="width:110px;font-size:11px" autocomplete="off">
    <button class="x" id="save">Use</button>
  </div>

  <div id="body">
    <div class="col" id="left">
      <div class="hd"><span class="grow">Explorer</span><span id="fc"></span></div>
      <input id="filter" placeholder="Filter files…">
      <div class="scroll" id="tree"></div>
    </div>
    <div class="gut" id="gl"></div>

    <div class="col">
      <div id="tabs"></div>
      <div id="stage">
        <div class="empty">
          Open a file, or describe a change on the right.<br><br>
          <kbd>Ctrl</kbd>+<kbd>K</kbd> commands &nbsp;·&nbsp;
          <kbd>Ctrl</kbd>+<kbd>P</kbd> files &nbsp;·&nbsp;
          <kbd>Ctrl</kbd>+<kbd>S</kbd> save
        </div>
        <div id="find">
          <input id="findq" placeholder="Find in file…" autocomplete="off">
          <span class="n" id="findn"></span>
          <button id="findprev">↑</button><button id="findnext">↓</button>
          <button id="findx">Close</button>
        </div>
        <div id="pane"><pre id="gutter" class="mono"></pre><textarea id="edit" spellcheck="false" class="mono"></textarea></div>
      </div>
    </div>
    <div class="gut" id="gr"></div>

    <div class="col" id="right">
      <div class="hd"><span class="grow">Agent</span><span id="busy"></span></div>
      <div id="tasks"></div>
      <div id="ask">
        <textarea id="goal" rows="3"
          placeholder="Describe a change. The agent works on its own branch."></textarea>
        <div class="row">
          <button id="send" class="go">Give task</button>
          <span class="grow"></span><span id="cerr" class="err"></span>
        </div>
        <div id="warn" class="warnbox" style="display:none"></div>
        <div class="hint">Nothing reaches your branch until you press Merge.</div>
      </div>
    </div>
  </div>

  <div id="bar">
    <span class="s" id="b-branch">—</span>
    <span class="s" id="b-model">—</span>
    <span class="s" id="b-policy">—</span>
    <span class="grow"></span>
    <span class="s" id="b-pos"></span>
    <span class="s" id="b-runs">Runs ↗</span>
  </div>
</div>

<div id="veil"></div>
<div id="pal"><input id="palin" placeholder="Type a command…" autocomplete="off"><div id="palls"></div></div>
<div id="toasts"></div>

<script>
"use strict";
const $=i=>document.getElementById(i);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const store={get(k,d){try{return JSON.parse(localStorage.getItem("fs_"+k))??d}catch(e){return d}},
             set(k,v){try{localStorage.setItem("fs_"+k,JSON.stringify(v))}catch(e){}}};

let key=store.get("key","");
/* The desktop shell mints the key and passes it in the fragment, which is
   never sent to the server. Consume it once, persist it, and strip it from the
   address so a copied URL does not carry a credential. */
(function(){
  const m=/(?:^|&)key=([^&]+)/.exec(location.hash.replace(/^#/,""));
  if(m){key=decodeURIComponent(m[1]);store.set("key",key);
        history.replaceState(null,"",location.pathname+location.search);}
})();
let tree=[],tabs=[],active=null,tasks=[],current=null,status={},timer=null,openDirs=new Set();

/* ---------- transport ---------- */
async function api(path,opts){
  const o=Object.assign({headers:{}},opts||{});
  if(key)o.headers["Authorization"]="Bearer "+key;
  if(o.body)o.headers["Content-Type"]="application/json";
  const r=await fetch(path,o);
  if(r.status===401)throw new Error("Unauthorized — set the API key (top right)");
  if(!r.ok){let d;try{d=(await r.json()).detail}catch(e){d=await r.text()}
            throw new Error(String(d).slice(0,160))}
  return r.json();
}
function toast(msg,kind){
  const n=document.createElement("div");n.className="toast "+(kind||"");n.textContent=msg;
  $("toasts").appendChild(n);setTimeout(()=>n.remove(),4200);
}

/* ---------- highlighting: a tokeniser, not a library ---------- */
const KW=/\\b(async|await|def|class|return|if|elif|else|for|while|try|except|finally|with|import|from|as|raise|yield|lambda|pass|break|continue|not|and|or|in|is|None|True|False|self|const|let|var|function|new|typeof|interface|type|export|default|public|private|struct|enum|fn|impl|pub|match|use|mod|nil|func|package|throw|catch|switch|case)\\b/g;
function hl(line){
  let s=esc(line);const bin=[];
  const keep=(c,t)=>{bin.push('<span class="'+c+'">'+t+"</span>");return "\\u0001"+(bin.length-1)+"\\u0001"};
  s=s.replace(/(#|\\/\\/).*$/g,m=>keep("com",m));
  s=s.replace(/("([^"\\\\]|\\\\.)*"|'([^'\\\\]|\\\\.)*'|`([^`\\\\]|\\\\.)*`)/g,m=>keep("str",m));
  s=s.replace(/\\b([A-Za-z_]\\w*)(?=\\()/g,m=>keep("fn",m));
  s=s.replace(KW,m=>keep("kw",m));
  s=s.replace(/\\b\\d+(\\.\\d+)?\\b/g,m=>keep("num",m));
  return s.replace(/\\u0001(\\d+)\\u0001/g,(_,i)=>bin[+i]);
}

/* ---------- tabs ---------- */
function tabOf(id){return tabs.find(t=>t.id===id)}
function openTab(tab){
  if(!tabOf(tab.id))tabs.push(tab);
  active=tab.id;render();
}
function closeTab(id,ev){
  if(ev)ev.stopPropagation();
  const t=tabOf(id);
  if(t&&t.dirty&&!confirm("Discard unsaved changes to "+t.title+"?"))return;
  tabs=tabs.filter(x=>x.id!==id);
  if(active===id)active=tabs.length?tabs[tabs.length-1].id:null;
  render();
}
function drawTabs(){
  $("tabs").innerHTML=tabs.map(t=>
    `<div class="tab ${t.id===active?"on":""} ${t.dirty?"dirty":""}" data-id="${esc(t.id)}">
       <span>${esc(t.title)}</span><span class="cl" data-cl="${esc(t.id)}">${t.dirty?"":"&times;"}</span></div>`).join("");
  $("tabs").querySelectorAll(".tab").forEach(n=>n.onclick=e=>{
    if(e.target.dataset.cl)return closeTab(e.target.dataset.cl,e);
    active=n.dataset.id;render();});
}

/* ---------- stage ---------- */
function render(){
  drawTabs();
  const t=tabOf(active),stage=$("stage"),ed=$("edit"),pane=$("pane");
  const old=stage.querySelector("pre.view, .empty");if(old)old.remove();
  pane.style.display="none";closeFind();
  if(!t){
    stage.insertAdjacentHTML("afterbegin",
      '<div class="empty">Open a file, or describe a change on the right.<br><br>'+
      '<kbd>Ctrl</kbd>+<kbd>K</kbd> commands &nbsp;&middot;&nbsp; <kbd>Ctrl</kbd>+<kbd>P</kbd> files'+
      ' &nbsp;&middot;&nbsp; <kbd>Ctrl</kbd>+<kbd>F</kbd> find &nbsp;&middot;&nbsp; '+
      '<kbd>Ctrl</kbd>+<kbd>S</kbd> save</div>');
    $("b-pos").textContent="";return;
  }
  if(t.kind==="file"){
    pane.style.display="grid";ed.value=t.content;gutter();
    ed.oninput=()=>{const x=tabOf(t.id);if(!x)return;x.content=ed.value;gutter();
      if(!x.dirty){x.dirty=true;drawTabs()}};
    ed.onscroll=()=>{$("gutter").scrollTop=ed.scrollTop};
    ed.onkeyup=ed.onclick=caret;
    caret();
  }else{
    stage.insertAdjacentHTML("afterbegin",'<pre class="view">'+t.html+"</pre>");
    $("b-pos").textContent="";
  }
  document.querySelectorAll(".node").forEach(n=>
    n.classList.toggle("on",t.kind==="file"&&n.dataset.p===t.path));
}

async function openFile(path){
  const id="f:"+path;
  if(tabOf(id)){active=id;return render()}
  try{
    const f=await api("/code/file?path="+encodeURIComponent(path));
    if(f.binary)return toast("Binary file — not shown","bad");
    openTab({id,kind:"file",path,title:path.split("/").pop(),content:f.content,dirty:false});
  }catch(e){toast(e.message,"bad")}
}
function renderDiffText(d){
  if(!d.trim())return '<span class="dl">No change in this file.</span>';
  return d.split(/\\r?\\n/).map(l=>{
    const c=l.startsWith("+++")||l.startsWith("---")||l.startsWith("diff ")||l.startsWith("index ")?"h":
            l.startsWith("@@")?"at":l.startsWith("+")?"a":l.startsWith("-")?"d":"";
    return '<span class="dl '+c+'">'+esc(l)+"</span>";}).join("");
}

/* One file at a time. A whole-task diff stops being readable past a few
   files, and reading the change is the step this product exists to make easy. */
async function openFileDiff(taskId,file){
  current=tasks.find(t=>t.id===taskId)||current;
  try{
    const d=(await api("/code/tasks/"+taskId+"/diff/file?path="+encodeURIComponent(file))).diff;
    const id="df:"+taskId+":"+file;
    tabs=tabs.filter(x=>x.id!==id);
    openTab({id,kind:"diff",title:"\u0394 "+file.split("/").pop(),html:renderDiffText(d)});
    drawTasks();
  }catch(e){toast(e.message,"bad")}
}

/* Your own uncommitted work. An editor built around reading diffs that will
   not show yours is incoherent - and this is also what the agent branches
   from, so it is worth seeing before starting a task. */
async function openChanges(){
  try{
    const c=await api("/code/changes");
    const body=c.clean
      ? '<span class="dl">Working tree is clean.</span>'
      : (c.diff.trim()?renderDiffText(c.diff)
         :'<span class="dl">Untracked files:</span>'+
          c.files.map(f=>'<span class="dl a">  '+esc(f)+"</span>").join(""));
    tabs=tabs.filter(x=>x.id!=="wt");
    openTab({id:"wt",kind:"diff",title:"your changes",html:body});
  }catch(e){toast(e.message,"bad")}
}

/* The durable event log for one task. Every claim this project makes -
   authorized, recorded, recoverable - is a claim about this log, and it was
   not visible from the place where the work is judged. */
const AUDIT_TONE={
  POLICY_DECIDED:"a",PERMIT_ISSUED:"",ACTION_DISPATCHED:"h",EFFECT_OBSERVED:"a",
  EFFECT_REUSED:"at",LOOP_DETECTED:"d",RUN_FAILED:"d",PROPOSAL_REJECTED:"d",
  RUN_COMPLETED:"a",COMPENSATION_APPLIED:"d",
};
async function openAudit(taskId){
  try{
    const rows=await api("/code/tasks/"+taskId+"/audit");
    const body=rows.length?rows.map(r=>{
      const bits=Object.entries(r.payload).map(([k,v])=>k+"="+v).join(" ");
      const cls=AUDIT_TONE[r.type]!==undefined?AUDIT_TONE[r.type]:"";
      return '<span class="dl '+cls+'">'+
        String(r.seq).padStart(4)+"  s"+(r.step??0)+"  "+
        esc(r.type.padEnd(22))+" "+esc(bits)+"</span>";
    }).join(""):'<span class="dl">No events recorded for this task.</span>';
    tabs=tabs.filter(x=>x.id!=="au:"+taskId);
    openTab({id:"au:"+taskId,kind:"diff",title:"audit \u00b7 "+taskId.slice(-6),html:body});
  }catch(e){toast(e.message,"bad")}
}

async function loadModels(){
  const sel=$("model");
  try{
    const m=await api("/code/models");
    if(!m.reachable){
      sel.innerHTML='<option>ollama not running</option>';sel.disabled=true;
      return;
    }
    sel.disabled=!!status.busy;
    sel.innerHTML=m.installed.map(x=>
      `<option value="${esc(x.name)}" ${x.name===m.active?"selected":""}>`+
      `${esc(x.name)} \u00b7 ${x.size_gb}GB</option>`).join("")
      ||'<option>no models pulled</option>';
    if(m.active&&!m.installed.some(x=>x.name===m.active)){
      sel.insertAdjacentHTML("afterbegin",
        `<option value="${esc(m.active)}" selected>${esc(m.active)} - not pulled</option>`);
    }
  }catch(e){sel.innerHTML='<option>'+esc(e.message.slice(0,40))+'</option>';sel.disabled=true}
}

$("model").onchange=async()=>{
  const name=$("model").value;
  $("model").disabled=true;
  try{await api("/code/models",{method:"POST",body:JSON.stringify({name})});
      toast("Now using "+name,"ok");await refresh();}
  catch(e){toast(e.message,"bad")}
  finally{$("model").disabled=false}
};

async function openDiff(taskId){
  current=tasks.find(t=>t.id===taskId)||current;
  try{
    const d=(await api("/code/tasks/"+taskId+"/diff")).diff;
    const html=!d.trim()?'<span class="dl">This task changed nothing.</span>':renderDiffText(d);
    const id="d:"+taskId;
    tabs=tabs.filter(t=>t.id!==id);
    openTab({id,kind:"diff",title:"diff · "+(current&&current.branch||taskId).split("/").pop(),html});
    drawTasks();
  }catch(e){toast(e.message,"bad")}
}
async function saveActive(){
  const t=tabOf(active);
  if(!t||t.kind!=="file"||!t.dirty)return;
  try{
    await api("/code/file",{method:"PUT",body:JSON.stringify({path:t.path,content:t.content})});
    t.dirty=false;drawTabs();toast("Saved "+t.title,"ok");refresh();
  }catch(e){toast(e.message,"bad")}
}

/* ---------- explorer ---------- */
function drawTree(){
  const q=($("filter").value||"").toLowerCase();
  const files=q?tree.filter(f=>f.toLowerCase().includes(q)):tree;
  $("fc").textContent=files.length;

  if(!files.length){
    $("tree").innerHTML='<div class="empty">'+
      (tree.length?"No file matches that filter."
                 :"This repository has no files git can see.<br><br>"+
                  "Everything may be ignored by .gitignore, or the folder is empty.")+
      "</div>";
    return;
  }

  /* A real tree, not a list of full paths grouped by parent. Deep projects
     are unreadable as the latter - "src/api/routes/v2/users.py" repeated
     forty times, once per file - and a folder you cannot collapse is a folder
     you have to scroll past. */
  const root={dirs:new Map(),files:[]};
  for(const f of files){
    const parts=f.split("/");
    let node=root;
    for(let i=0;i<parts.length-1;i++){
      if(!node.dirs.has(parts[i]))node.dirs.set(parts[i],{dirs:new Map(),files:[]});
      node=node.dirs.get(parts[i]);
    }
    node.files.push({name:parts[parts.length-1],path:f});
  }

  const touched=new Set((current&&current.files)||[]);
  const out=[];
  /* A folder whose only content is another folder is collapsed into one row -
     "a/b/c" rather than three rows each needing a click. Without this, a
     repository whose top level is one folder containing one folder opens to
     show a folder and no files, which reads as an empty folder and was
     reported as one. */
  const compact=(node,name)=>{
    const parts=[name];
    while(node.files.length===0&&node.dirs.size===1){
      const only=[...node.dirs.keys()][0];
      parts.push(only);
      node=node.dirs.get(only);
    }
    return {node,label:parts.join("/"),segments:parts};
  };

  const walk=(node,prefix,depth)=>{
    for(const name of [...node.dirs.keys()].sort()){
      const {node:target,label,segments}=compact(node.dirs.get(name),name);
      const full=prefix?prefix+"/"+segments.join("/"):segments.join("/");
      /* A filter is a search: collapsed folders would hide the hits. */
      const open=q||openDirs.has(full);
      out.push('<div class="node dir" data-d="'+esc(full)+'" style="padding-left:'+
        (6+depth*12)+'px" title="'+esc(full)+'"><span class="tw">'+
        (open?"\u25be":"\u25b8")+"</span>"+esc(label)+"</div>");
      if(open)walk(target,full,depth+1);
    }
    for(const f of [...node.files].sort((a,b)=>a.name.localeCompare(b.name))){
      out.push('<div class="node '+(touched.has(f.path)?"touch":"")+
        '" data-p="'+esc(f.path)+'" style="padding-left:'+(20+depth*12)+
        'px" title="'+esc(f.path)+'">'+esc(f.name)+"</div>");
    }
  };
  walk(root,"",0);

  $("tree").innerHTML=out.join("");
  $("tree").querySelectorAll("[data-p]").forEach(n=>n.onclick=()=>openFile(n.dataset.p));
  $("tree").querySelectorAll("[data-d]").forEach(n=>n.onclick=()=>{
    const d=n.dataset.d;
    openDirs.has(d)?openDirs.delete(d):openDirs.add(d);
    store.set("dirs2",[...openDirs]);
    drawTree();
  });
}


/* ---------- agent ---------- */
function drawTasks(){
  if(!tasks.length){$("tasks").innerHTML='<div class="empty">No tasks yet.<br>Describe a change below.</div>';return}
  $("tasks").innerHTML=tasks.map(t=>{
    const state=t.merged?"merged":t.discarded?"discarded":t.status;
    const stacked=(t.stacked_above||[]).length>0;
    const act=t.status==="completed"&&t.commits>0&&!t.merged&&!t.discarded&&!stacked;
    const prog=(t.progress||[]).slice(-14);
    return `<div class="tk ${current&&current.id===t.id?"on":""}" data-id="${t.id}">
      <div class="g">${esc(t.goal)}</div>
      <div class="m"><span class="st ${state}">${state}</span>
        ${t.commits?`<span>${t.commits} commit${t.commits>1?"s":""}</span>`:""}
        ${t.files.length?`<span>${t.files.length} file${t.files.length>1?"s":""}</span>`:""}</div>
      ${t.files.length ? `<div class="files">${t.files.map(f =>
        `<span class="chg" data-t="${t.id}" data-f="${esc(f)}" title="Show what changed in this file">${esc(f)}</span>`
      ).join("")}</div>` : ""}
      ${t.error?`<div class="m err">${esc(t.error)}</div>`:""}
      ${stacked?`<div class="stk">A later task builds on this one. Merge the newest
        task instead - it already contains this work.</div>`:""}
      ${t.stacked_on?`<div class="stk">Built on the task below.</div>`:""}
      ${prog.length?`<div class="prog">${prog.map(p=>`<div class="pl ${esc(p.kind)}">${esc(p.text)}</div>`).join("")}</div>`:""}
      ${t.status==="running"?`<div class="acts">
        <button data-a="stop" data-id="${t.id}" class="bad">Stop</button></div>`:""}
      ${t.status!=="running"&&t.run_id?`<div class="acts">
        ${t.commits?`<button data-a="diff" data-id="${t.id}">Review diff</button>`:""}
        <button data-a="audit" data-id="${t.id}">Audit trail</button>
        ${act?`<button data-a="merge" data-id="${t.id}" class="go">Merge</button>
        <button data-a="undo" data-id="${t.id}" class="bad">Discard</button>`:""}
      </div>`:""}
    </div>`}).join("");
  $("tasks").querySelectorAll(".tk").forEach(n=>n.onclick=e=>{
    if(e.target.dataset.a||e.target.dataset.f)return;openDiff(n.dataset.id)});
  $("tasks").querySelectorAll(".chg").forEach(n=>n.onclick=e=>{
    e.stopPropagation();openFileDiff(n.dataset.t,n.dataset.f)});
  $("tasks").querySelectorAll("button[data-a]").forEach(b=>b.onclick=async e=>{
    e.stopPropagation();const id=b.dataset.id,a=b.dataset.a;
    if(a==="diff")return openDiff(id);
    if(a==="audit")return openAudit(id);
    if(a==="stop"){
      b.disabled=true;
      try{await api("/code/tasks/"+id+"/cancel",{method:"POST"});toast("Stopping...");await refresh();}
      catch(err){toast(err.message,"bad");b.disabled=false}
      return;
    }
    if(a==="undo"&&!confirm("Discard this task's branch? The work is lost."))return;
    b.disabled=true;
    try{const r=await api("/code/tasks/"+id+"/"+(a==="merge"?"accept":"undo"),{method:"POST"});
        toast(a==="merge"?("Merged into "+r.into):"Discarded","ok");await refresh();}
    catch(err){toast(err.message,"bad");b.disabled=false}});
}

async function refresh(){
  try{
    status=await api("/code/status");
    $("repo").textContent=status.name;$("repo").className="chip on";
    $("branch").textContent=status.branch;
    $("dirty").textContent=status.clean?"clean":status.dirty_files.length+" changed";
    $("dirty").className="chip"+(status.clean?"":" on");
    $("dirty").style.cursor=status.clean?"default":"pointer";
    $("b-branch").textContent="⎇ "+status.branch;
    $("b-model").textContent=status.model;$("b-policy").textContent=status.policy;
    // A repository with no commits cannot be worked on: the agent branches
    // from HEAD and there is no HEAD. Saying so here, next to a disabled
    // button, beats letting someone write a task and then be refused.
    const noCommits=status.has_commits===false;
    $("send").disabled=!!status.busy||noCommits;
    $("goal").disabled=noCommits;
    $("goal").placeholder=noCommits
      ? "Make the first commit in this repository, then reload."
      : "Describe a change. The agent works on its own branch.";
    $("warn").textContent=noCommits
      ? "No commits yet - the agent branches from HEAD, so make one first: " +
        "git add -A && git commit -m 'initial'"
      : "";
    $("warn").style.display=noCommits?"block":"none";
    $("busy").textContent=status.busy?"working…":"";
  }catch(e){$("repo").textContent=e.message;$("repo").className="chip"}
  await loadModels();
  try{
    tree=(await api("/code/tree")).files;
    // Open the top level the first time a repository is seen: an explorer
    // that starts fully collapsed looks identical to one that found nothing.
    if(!openDirs.size&&tree.length){
      for(const f of tree){const i=f.indexOf("/");if(i>0)openDirs.add(f.slice(0,i));}
    }
    drawTree();
  }catch(e){}
  try{tasks=await api("/code/tasks");
      if(current)current=tasks.find(t=>t.id===current.id)||current;drawTasks()}catch(e){}
  if(timer)clearTimeout(timer);
  if(status.busy)timer=setTimeout(refresh,1200);
}

/* ---------- command palette ---------- */
const COMMANDS=[
  {n:"Open file…",k:"Ctrl+P",run:()=>palette("file")},
  {n:"Search in repository…",k:"Ctrl+Shift+F",run:()=>palette("grep")},
  {n:"Save file",k:"Ctrl+S",run:saveActive},
  {n:"Find in file…",k:"Ctrl+F",run:()=>openFind()},
  {n:"Close tab",k:"Ctrl+W",run:()=>active&&closeTab(active)},
  {n:"Give the agent a task",k:"Ctrl+Enter",run:()=>$("goal").focus()},
  {n:"Review latest diff",run:()=>tasks[0]&&openDiff(tasks[0].id)},
  {n:"Show my uncommitted changes",run:openChanges},
  {n:"Show the latest task's audit trail",run:()=>tasks[0]&&openAudit(tasks[0].id)},
  {n:"Stop the running task",run:()=>{const r=tasks.find(x=>x.status==="running");
     if(r)api("/code/tasks/"+r.id+"/cancel",{method:"POST"}).then(refresh);
     else toast("Nothing is running")}},
  {n:"Refresh",k:"Ctrl+R",run:refresh},
  {n:"Open run console",run:()=>location.href="/"},
];
let palMode="cmd",palItems=[],palIdx=0;
function palette(mode){
  palMode=mode||"cmd";palIdx=0;
  $("veil").style.display=$("pal").style.display="block";
  $("palin").value="";
  $("palin").placeholder=palMode==="file"?"Go to file…":
                         palMode==="grep"?"Search text in repository…":"Type a command…";
  $("palin").focus();palFill();
}
function palClose(){$("veil").style.display=$("pal").style.display="none"}
async function palFill(){
  const q=$("palin").value.trim(),ql=q.toLowerCase();
  if(palMode==="cmd"){
    palItems=COMMANDS.filter(c=>c.n.toLowerCase().includes(ql))
      .map(c=>({label:c.n,note:c.k||"",go:c.run}));
  }else if(palMode==="file"){
    palItems=tree.filter(f=>f.toLowerCase().includes(ql)).slice(0,60)
      .map(f=>({label:f.split("/").pop(),note:f,go:()=>openFile(f)}));
  }else{
    if(q.length<2){palItems=[{label:"Type at least two characters",note:"",go:null}]}
    else{try{
      const hits=(await api("/code/search?q="+encodeURIComponent(q))).hits;
      palItems=hits.length?hits.map(h=>({label:h.path+":"+h.line,note:h.text.trim(),
                                         go:()=>openFile(h.path)}))
                          :[{label:"No matches",note:"",go:null}];
    }catch(e){palItems=[{label:e.message,note:"",go:null}]}}
  }
  palIdx=0;palDraw();
}
function palDraw(){
  $("palls").innerHTML=palItems.map((it,i)=>
    `<div class="pi ${i===palIdx?"on":""}" data-i="${i}">
       <span>${esc(it.label)}</span><span class="sub">${esc(it.note||"")}</span></div>`).join("");
  $("palls").querySelectorAll(".pi").forEach(n=>n.onclick=()=>palPick(+n.dataset.i));
  const on=$("palls").querySelector(".pi.on");if(on)on.scrollIntoView({block:"nearest"});
}
function palPick(i){
  const it=palItems[i];if(!it||!it.go)return;
  palClose();it.go();
}

/* ---------- wiring ---------- */
$("palin").oninput=palFill;
$("veil").onclick=palClose;
$("palin").onkeydown=e=>{
  if(e.key==="Escape")return palClose();
  if(e.key==="ArrowDown"){palIdx=Math.min(palIdx+1,palItems.length-1);palDraw();e.preventDefault()}
  if(e.key==="ArrowUp"){palIdx=Math.max(palIdx-1,0);palDraw();e.preventDefault()}
  if(e.key==="Enter")palPick(palIdx);
};
document.addEventListener("keydown",e=>{
  const mod=e.ctrlKey||e.metaKey;
  if(mod&&e.key.toLowerCase()==="k"){e.preventDefault();palette("cmd")}
  else if(mod&&e.key.toLowerCase()==="p"){e.preventDefault();palette("file")}
  else if(mod&&e.shiftKey&&e.key.toLowerCase()==="f"){e.preventDefault();palette("grep")}
  else if(mod&&e.key.toLowerCase()==="s"){e.preventDefault();saveActive()}
  else if(mod&&!e.shiftKey&&e.key.toLowerCase()==="f"){e.preventDefault();openFind()}
  else if(mod&&e.key.toLowerCase()==="w"){e.preventDefault();active&&closeTab(active)}
  else if(mod&&e.key==="Enter"){e.preventDefault();$("send").click()}
});
$("cmd").onclick=()=>palette("cmd");
$("filter").oninput=drawTree;
$("b-runs").onclick=()=>location.href="/";
$("send").onclick=async()=>{
  const goal=$("goal").value.trim();if(!goal)return;
  $("send").disabled=true;$("cerr").textContent="";
  try{const t=await api("/code/tasks",{method:"POST",body:JSON.stringify({goal})});
      $("goal").value="";current=t;toast("Task started on its own branch");await refresh();}
  catch(e){$("cerr").textContent=e.message;$("send").disabled=false}
};
$("goal").onkeydown=e=>{if(e.key==="Enter"&&(e.ctrlKey||e.metaKey))$("send").click()};
$("save").onclick=()=>{key=$("key").value.trim();store.set("key",key);refresh();toast("Key saved","ok")};
$("key").value=key;
$("key").onkeydown=e=>{if(e.key==="Enter")$("save").click()};

/* ---------- editor mechanics ---------- */
function gutter(){
  const n=$("edit").value.split("\\n").length;
  let s="";for(let i=1;i<=n;i++)s+=i+"\\n";
  $("gutter").textContent=s;
}
function caret(){
  const ed=$("edit"),upto=ed.value.slice(0,ed.selectionStart).split("\\n");
  $("b-pos").textContent="Ln "+upto.length+", Col "+(upto[upto.length-1].length+1);
}
/* Tab indents instead of leaving the editor, and Enter keeps the indent.
   Losing your place because Tab moved focus is the single most irritating
   thing an unimproved textarea does. */
$("edit").addEventListener("keydown",e=>{
  const ed=$("edit");
  if(e.key==="Tab"){
    e.preventDefault();
    const s=ed.selectionStart,en=ed.selectionEnd;
    ed.value=ed.value.slice(0,s)+"    "+ed.value.slice(en);
    ed.selectionStart=ed.selectionEnd=s+4;ed.dispatchEvent(new Event("input"));
  }else if(e.key==="Enter"){
    const s=ed.selectionStart,line=ed.value.slice(0,s).split("\\n").pop();
    const pad=(line.match(/^[ \\t]*/)||[""])[0]+(/[:{[(]\\s*$/.test(line)?"    ":"");
    if(pad){
      e.preventDefault();
      ed.value=ed.value.slice(0,s)+"\\n"+pad+ed.value.slice(ed.selectionEnd);
      ed.selectionStart=ed.selectionEnd=s+1+pad.length;
      ed.dispatchEvent(new Event("input"));
    }
  }
});

/* ---------- find in file ---------- */
let findHits=[],findAt=0;
function openFind(){
  const t=tabOf(active);if(!t||t.kind!=="file")return;
  $("find").style.display="flex";$("findq").focus();$("findq").select();runFind();
}
function closeFind(){$("find").style.display="none";findHits=[]}
function runFind(){
  const q=$("findq").value,ed=$("edit");
  findHits=[];
  if(q){let i=ed.value.indexOf(q);while(i>=0){findHits.push(i);i=ed.value.indexOf(q,i+q.length)}}
  findAt=0;
  $("findn").textContent=q?(findHits.length?"1 / "+findHits.length:"no matches"):"";
  if(findHits.length)jumpFind(0);
}
function jumpFind(d){
  if(!findHits.length)return;
  findAt=(findAt+d+findHits.length)%findHits.length;
  const ed=$("edit"),at=findHits[findAt],q=$("findq").value;
  ed.focus();ed.setSelectionRange(at,at+q.length);
  const upto=ed.value.slice(0,at).split("\\n").length;
  const total=ed.value.split("\\n").length;
  ed.scrollTop=Math.max(0,(upto-8)*(ed.scrollHeight/total));
  $("findn").textContent=(findAt+1)+" / "+findHits.length;caret();
}
$("findq").oninput=runFind;
$("findq").onkeydown=e=>{
  if(e.key==="Escape")closeFind();
  else if(e.key==="Enter"){e.preventDefault();jumpFind(e.shiftKey?-1:1)}
};
$("findnext").onclick=()=>jumpFind(1);
$("findprev").onclick=()=>jumpFind(-1);
$("findx").onclick=closeFind;

/* ---------- installability ---------- */
let installPrompt=null;
window.addEventListener("beforeinstallprompt",e=>{
  e.preventDefault();installPrompt=e;$("install").style.display="";
});
$("install").onclick=async()=>{
  if(!installPrompt)return;
  $("install").style.display="none";
  installPrompt.prompt();
  const choice=await installPrompt.userChoice;
  installPrompt=null;
  toast(choice.outcome==="accepted"?"Installed - look for FORGE in your apps":"Not installed");
};
window.addEventListener("appinstalled",()=>{$("install").style.display="none"});
if("serviceWorker" in navigator){
  navigator.serviceWorker.register("/sw.js").catch(()=>{/* blocked; the app still runs */});
}

/* resizable panes, remembered */

/* resizable panes, remembered */
function drag(handle,varName,storeKey,invert){
  handle.onmousedown=e=>{
    e.preventDefault();
    const move=ev=>{
      const w=invert?window.innerWidth-ev.clientX:ev.clientX;
      const px=Math.max(170,Math.min(560,w));
      document.documentElement.style.setProperty(varName,px+"px");store.set(storeKey,px);};
    const up=()=>{document.removeEventListener("mousemove",move);
                  document.removeEventListener("mouseup",up)};
    document.addEventListener("mousemove",move);document.addEventListener("mouseup",up);};
}
drag($("gl"),"--lw","lw",false);drag($("gr"),"--rw","rw",true);
document.documentElement.style.setProperty("--lw",store.get("lw",250)+"px");
document.documentElement.style.setProperty("--rw",store.get("rw",330)+"px");
(store.get("dirs2",[])||[]).forEach(d=>openDirs.add(d));

refresh();
</script>
</body>
</html>
"""
