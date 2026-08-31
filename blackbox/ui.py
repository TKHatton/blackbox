"""The Split Screen: making nine phases of plumbing legible in thirty seconds.

Served as one self-contained page from the same process as the API it reads, so
everything on it is live data. No CDN, no build step, no external font: the whole
thing is here, which also means it works on a locked-down network.

Three failure modes shaped the design:

**Not a dashboard of tiles.** Tiles show that a system exists. They do not show
it working. So the page leads with things happening: reasoning arriving, a
divergence opening up, a label travelling. The one row of numbers at the top is
context for the rest, not the point of it.

**Reasoning streams.** It arrives over server-sent events and appears as it is
written, because a collapsed log you have to click into hides the thing worth
seeing.

**Live, not beautiful and static.** Every panel fetches from a real endpoint and
says so when there is nothing there yet, rather than rendering plausible-looking
placeholder data.
"""

SPLIT_SCREEN_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BLACKBOX</title>
<style>
  :root{
    --bg:#0b0e13; --panel:#121722; --panel2:#0e131c; --line:#1f2938;
    --ink:#e6edf7; --dim:#8фa0b8; --dim:#8b9bb4; --faint:#5a6b85;
    --accent:#5eead4; --warn:#fbbf24; --bad:#f87171; --good:#4ade80;
    --live:#38bdf8; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  a{color:var(--accent)}
  header{padding:18px 24px 0;border-bottom:1px solid var(--line);background:var(--panel2)}
  h1{margin:0;font-size:19px;letter-spacing:.14em;font-weight:600}
  h1 span{color:var(--accent)}
  .sub{color:var(--dim);font-size:12.5px;margin:4px 0 14px}
  .stats{display:flex;gap:26px;flex-wrap:wrap;padding-bottom:14px}
  .stat b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
  .stat span{color:var(--faint);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
  nav{display:flex;gap:2px;padding:0 24px;background:var(--panel2);
    border-bottom:1px solid var(--line);overflow-x:auto}
  nav button{background:none;border:0;color:var(--dim);padding:11px 14px;cursor:pointer;
    font-size:13px;border-bottom:2px solid transparent;white-space:nowrap}
  nav button:hover{color:var(--ink)}
  nav button.on{color:var(--accent);border-bottom-color:var(--accent)}
  main{padding:22px 24px 60px;max-width:1500px}
  section{display:none} section.on{display:block}
  .lede{color:var(--dim);max-width:70ch;margin:0 0 18px}
  .lede b{color:var(--ink);font-weight:600}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:1000px){.grid2{grid-template-columns:1fr}}
  .ttl{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--faint);
    margin:0 0 10px}
  .mono{font-family:var(--mono);font-size:12px}
  button.act{background:#1b2433;border:1px solid var(--line);color:var(--ink);
    padding:8px 13px;border-radius:6px;cursor:pointer;font-size:13px}
  button.act:hover{border-color:var(--accent);color:var(--accent)}
  button.act:disabled{opacity:.4;cursor:default}
  select,input{background:#0d1219;border:1px solid var(--line);color:var(--ink);
    padding:7px 9px;border-radius:6px;font-size:13px;font-family:inherit}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
  .empty{color:var(--faint);font-style:italic;padding:18px 0}

  /* streaming reasoning */
  .stream{max-height:520px;overflow:auto;display:flex;flex-direction:column;gap:9px}
  .th{border-left:2px solid var(--live);padding:9px 12px;background:#0e141d;border-radius:0 6px 6px 0;
    animation:in .45s ease}
  @keyframes in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
  .th .who{color:var(--live);font-size:11px;letter-spacing:.06em;text-transform:uppercase}
  .th .txt{margin:5px 0 6px;white-space:pre-wrap}
  .th .did{color:var(--faint);font-size:12px;font-family:var(--mono)}
  .pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--live);
    margin-right:7px;animation:p 1.4s infinite}
  @keyframes p{0%,100%{opacity:1}50%{opacity:.25}}

  /* trees */
  .tree{font-family:var(--mono);font-size:12px;line-height:1.75}
  .node{padding:2px 7px;border-radius:4px;display:block;border-left:2px solid transparent}
  .node.same{color:var(--dim)}
  .node.split{background:rgba(248,113,113,.13);border-left-color:var(--bad);color:#fecaca}
  .node.after{background:rgba(251,191,36,.09);border-left-color:var(--warn);color:#fde68a}
  .node.only{background:rgba(74,222,128,.10);border-left-color:var(--good);color:#bbf7d0}
  .why{margin-top:14px;padding:13px 15px;background:rgba(248,113,113,.08);
    border:1px solid rgba(248,113,113,.3);border-radius:7px}
  .why b{color:var(--bad)}

  /* taint */
  .hop{display:flex;gap:13px;padding:11px 0;border-bottom:1px solid var(--line)}
  .hop:last-child{border-bottom:0}
  .dot{width:11px;height:11px;border-radius:50%;background:var(--line);flex:none;margin-top:5px}
  .hop.attach .dot{background:var(--bad);box-shadow:0 0 0 4px rgba(248,113,113,.18)}
  .hop.block .dot{background:var(--warn);box-shadow:0 0 0 4px rgba(251,191,36,.18)}
  .cls{display:inline-block;font-family:var(--mono);font-size:10.5px;padding:1px 6px;
    border-radius:3px;background:#1b2433;color:var(--dim);margin-right:5px}
  .cls.hot{background:rgba(248,113,113,.2);color:#fca5a5}
  .quote{margin-top:7px;padding:9px 12px;background:#0d1219;border-left:2px solid var(--bad);
    border-radius:0 5px 5px 0;color:#fecaca;font-size:13px}

  /* cascade */
  .pg{padding:9px 12px;border:1px solid var(--line);border-radius:6px;margin-bottom:7px;
    transition:all .5s ease}
  .pg.hit{border-color:var(--bad);background:rgba(248,113,113,.09)}
  .pg.regen{border-color:var(--good);background:rgba(74,222,128,.09)}
  .depth{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-right:9px}

  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:var(--faint);font-weight:500;font-size:11px;
    text-transform:uppercase;letter-spacing:.07em;padding:7px 9px;border-bottom:1px solid var(--line)}
  td{padding:8px 9px;border-bottom:1px solid #161d29;font-variant-numeric:tabular-nums}
  .ok{color:var(--good)} .no{color:var(--bad)} .mid{color:var(--warn)}
</style>
</head>
<body>
<header>
  <h1>BLACK<span>BOX</span></h1>
  <div class="sub">The flight recorder for AI agents. Everything on this page is live data from this service.</div>
  <div class="stats" id="stats"></div>
</header>

<nav>
  <button class="on" data-v="live">Live fleet</button>
  <button data-v="split">Split screen</button>
  <button data-v="ink">Invisible Ink</button>
  <button data-v="eraser">The Eraser</button>
  <button data-v="time">Time Machine</button>
  <button data-v="immune">Immune system</button>
</nav>

<main>

<section class="on" id="v-live">
  <p class="lede"><b>Nothing here started because someone pressed a button.</b>
  A scheduler wakes a poller, the poller publishes to Pub/Sub, and a message landing
  on that topic is what makes an agent run. Below is Gemini's reasoning, arriving as
  it is recorded.</p>
  <div class="grid2">
    <div class="panel">
      <p class="ttl"><span class="pulse"></span>Reasoning, streaming</p>
      <div class="stream" id="stream"><div class="empty">Waiting for the fleet to think…</div></div>
    </div>
    <div class="panel">
      <p class="ttl">Cases</p>
      <div id="cases"><div class="empty">Loading…</div></div>
      <p class="ttl" style="margin-top:20px">What the fleet is waiting on</p>
      <div id="waits"><div class="empty">Loading…</div></div>
    </div>
  </div>
</section>

<section id="v-split">
  <p class="lede"><b>Rewind, change one rule, and see what would have happened instead.</b>
  The left tree is what the fleet actually did. The right is the same case replayed
  under an amended policy. The divergence point is highlighted, and everything after
  it is a consequence of that one change.</p>
  <div class="row">
    <select id="sp-case"></select>
    <label style="color:var(--dim)">Gate A threshold
      <input id="sp-thresh" type="number" value="100" step="50" style="width:95px">
    </label>
    <button class="act" id="sp-go">Replay</button>
    <span id="sp-note" style="color:var(--faint)"></span>
  </div>
  <div class="grid2">
    <div class="panel"><p class="ttl">What happened</p><div class="tree" id="sp-a"><div class="empty">Pick a case and replay.</div></div></div>
    <div class="panel"><p class="ttl">What would have happened</p><div class="tree" id="sp-b"><div class="empty"></div></div></div>
  </div>
  <div id="sp-why"></div>
</section>

<section id="v-ink">
  <p class="lede"><b>A letter with no medical word in it, refused.</b>
  The label is attached to where content came from, not to the words, so it survives
  summarising and rephrasing. Pick a blocked disclosure to see the trail back to the
  sentence that caused it.</p>
  <div class="row">
    <select id="ink-case"></select>
    <button class="act" id="ink-go">Trace</button>
    <span id="ink-note" style="color:var(--faint)"></span>
  </div>
  <div class="panel"><div id="ink-path"><div class="empty">Pick a case.</div></div></div>
</section>

<section id="v-eraser">
  <p class="lede"><b>Retract one fact and watch what was built on it come apart.</b>
  Every Wiki page records what it was derived from, so the cascade reaches pages that
  never mentioned the customer at all.</p>
  <div class="row"><button class="act" id="er-go">Show the cascade</button>
    <span id="er-note" style="color:var(--faint)"></span></div>
  <div class="grid2">
    <div class="panel"><p class="ttl">Pages reached</p><div id="er-pages"><div class="empty">No retraction recorded yet.</div></div></div>
    <div class="panel"><p class="ttl">The record of it</p><div id="er-hist"><div class="empty"></div></div></div>
  </div>
</section>

<section id="v-time">
  <p class="lede"><b>Drag to any moment in a case's history.</b>
  State is rebuilt from the log at that point, not read from the current Wiki, which
  is what stops a replay reading the answer it is supposed to be deciding.</p>
  <div class="row">
    <select id="tm-case"></select>
    <button class="act" id="tm-load">Load</button>
  </div>
  <div class="panel">
    <input type="range" id="tm-slide" min="0" max="0" value="0" style="width:100%" disabled>
    <div id="tm-at" class="mono" style="color:var(--faint);margin:9px 0"></div>
    <div id="tm-state"></div>
  </div>
</section>

<section id="v-immune">
  <p class="lede"><b>The attack success rate falls while the corpus grows.</b>
  Both curves together, or neither means anything: a falling rate against a fixed set
  of attacks would just mean somebody patched those attacks. An attack counts only
  when a policy boundary was crossed, never when the model merely sounded rattled.</p>
  <div class="panel">
    <div id="im-chart"><div class="empty">No campaign has been run on this instance yet.</div></div>
    <div id="im-corpus" style="margin-top:22px"></div>
  </div>
</section>

</main>

<script>
const $ = s => document.querySelector(s);
const el = (t,c,x) => { const n=document.createElement(t); if(c)n.className=c;
  if(x!==undefined)n.textContent=x; return n; };
const esc = s => (s==null?'':String(s));
async function api(p,o){ const r = await fetch(p,o); if(!r.ok) throw new Error(p+' '+r.status);
  return r.json(); }

let CASES = [];

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('section').forEach(s=>
    s.classList.toggle('on', s.id==='v-'+b.dataset.v));
});

/* ---------- header + cases ---------- */
async function loadOverview(){
  let o;
  try { o = await api('/overview'); } catch(e){ return; }
  const stats=[['cases',o.cases.filter(c=>c.open).length],['waiting',o.open_suspensions],
    ['blocked',o.cases.reduce((a,c)=>a+(c.blocked||0),0)],['attack corpus',o.corpus_size],
    ['faults armed',o.faults_armed],['policy',o.policy_version],['model',o.model]];
  const s=$('#stats'); s.innerHTML='';
  stats.forEach(([k,v])=>{ const d=el('div','stat'); d.appendChild(el('b',null,esc(v)));
    d.appendChild(el('span',null,k)); s.appendChild(d); });

  CASES = o.cases.filter(c=>c.open);
  ['#sp-case','#ink-case','#tm-case'].forEach(sel=>{
    const n=$(sel); const keep=n.value; n.innerHTML='';
    CASES.forEach(c=>{ const op=el('option',null,c.case_id); op.value=c.case_id; n.appendChild(op); });
    if(keep) n.value=keep;
  });

  const cw=$('#cases'); cw.innerHTML='';
  if(!CASES.length){ cw.appendChild(el('div','empty','No case is open yet. The poller runs every 10 minutes.')); }
  CASES.forEach(c=>{
    const d=el('div','pg');
    d.appendChild(el('span','mono',c.case_id+'  '));
    d.appendChild(el('span',null,(c.jurisdiction||'?')+' · '+c.events+' events · '+esc(c.status)));
    if(c.vulnerable) d.appendChild(el('span','cls hot',' vulnerable '));
    if(c.blocked) d.appendChild(el('span','cls hot',' '+c.blocked+' blocked '));
    cw.appendChild(d);
  });

  try{
    const w = await api('/suspensions'); const n=$('#waits'); n.innerHTML='';
    if(!w.suspensions.length){ n.appendChild(el('div','empty','Nothing suspended. No process is resident.')); }
    w.suspensions.forEach(s=>{
      const d=el('div','pg');
      d.appendChild(el('div',null,s.waiting_agent+' — '+s.wakes_when));
      d.appendChild(el('div','mono','not before '+(s.not_before||'an event, not a clock')));
      n.appendChild(d);
    });
  }catch(e){}
}

/* ---------- streaming reasoning ---------- */
function startStream(){
  const box=$('#stream'); let first=true;
  const es=new EventSource('/stream/reasoning');
  es.onmessage=m=>{
    let d; try{ d=JSON.parse(m.data);}catch(e){return;}
    if(first){ box.innerHTML=''; first=false; }
    const n=el('div','th');
    n.appendChild(el('div','who',d.actor+'  ·  '+d.case_id));
    n.appendChild(el('div','txt',d.reasoning));
    n.appendChild(el('div','did','→ '+d.decision));
    const cls=(d.labels&&d.labels.classes)||[];
    if(cls.length){ const r=el('div'); r.style.marginTop='6px';
      cls.forEach(c=>{ const t=el('span','cls'+(c==='SPECIAL_CATEGORY'||c==='PII_HIGH'?' hot':''),c);
        r.appendChild(t); }); n.appendChild(r); }
    box.insertBefore(n, box.firstChild);
    while(box.children.length>60) box.removeChild(box.lastChild);
  };
  es.onerror=()=>{};
}

/* ---------- split screen ---------- */
$('#sp-go').onclick=async()=>{
  const cid=$('#sp-case').value; if(!cid) return;
  $('#sp-note').textContent='replaying…'; $('#sp-a').innerHTML=''; $('#sp-b').innerHTML='';
  $('#sp-why').innerHTML='';
  try{
    const tr=await api('/cases/'+encodeURIComponent(cid)+'/trace');
    const flat=[]; (function walk(ns){ns.forEach(n=>{flat.push(n);walk(n.caused||[])})})(tr.tree||[]);
    const anchor=flat.find(n=>n.event_type==='MEMORY_WRITE')||flat[Math.min(1,flat.length-1)];
    const r=await api('/replay',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({case_id:cid,rewind_to:anchor.event_id,
        constants:{gate_a_threshold:Number($('#sp-thresh').value)}})});
    renderSplit(r);
    $('#sp-note').textContent='rewound to '+anchor.event_id.slice(0,10)+'…  policy '+r.policy_version;
  }catch(e){ $('#sp-note').textContent='could not replay: '+e.message; }
};
function renderSplit(r){
  const A=$('#sp-a'), B=$('#sp-b'); A.innerHTML=''; B.innerHTML='';
  const a=r.original_decisions||[], b=r.replay_decisions||[];
  const split=r.first_difference&&r.first_difference.at;
  const n=Math.max(a.length,b.length);
  if(!n){ A.appendChild(el('div','empty','No decisions after the rewind point.')); return; }
  for(let i=0;i<n;i++){
    const la=a[i], lb=b[i];
    const same=JSON.stringify(la)===JSON.stringify(lb);
    const cls=i===split?'split':(same?'same':'after');
    A.appendChild(el('div','node '+(la?cls:'only'), la?(i+1)+'. '+la[0]+'  '+la[1]:'—'));
    B.appendChild(el('div','node '+(lb?cls:'only'), lb?(i+1)+'. '+lb[0]+'  '+lb[1]:'—'));
  }
  const w=$('#sp-why'); w.innerHTML='';
  const box=el('div','why');
  const changes=r.rule_changes||[];
  if(changes.length){
    box.appendChild(el('b','A rule reached a different verdict.'));
    changes.forEach(c=>{
      const line=el('div','mono');
      line.appendChild(el('span',null,c.rule+'  '));
      const was=el('span','cls',c.originally); was.style.opacity='.7';
      line.appendChild(was);
      line.appendChild(el('span',null,' → '));
      const now=el('span','cls hot',c.in_replay);
      line.appendChild(now);
      box.appendChild(line);
    });
    box.appendChild(el('div',null,'That is the policy change biting. '+
      (r.downstream_consequences||[]).length+' downstream decision(s) differ as a consequence.'));
  } else if(r.diverged){
    box.appendChild(el('b','The paths split here.'));
    box.appendChild(el('div',null,r.first_difference.explanation));
  } else {
    box.appendChild(el('b','No decision changed.'));
    box.appendChild(el('div',null,r.summary));
  }
  box.appendChild(el('div','mono','replayed under '+r.policy_version+
    (r.completed?'':'  ·  stopped early: '+r.error)));
  w.appendChild(box);
}

/* ---------- invisible ink ---------- */
$('#ink-go').onclick=async()=>{
  const cid=$('#ink-case').value; if(!cid) return;
  const out=$('#ink-path'); out.innerHTML=''; $('#ink-note').textContent='';
  try{
    const b=await api('/cases/'+encodeURIComponent(cid)+'/blocked');
    if(!b.blocked.length){ out.appendChild(el('div','empty',
      'Nothing was refused on this case. Try one with a blocked count in the header.')); return; }
    const first=b.blocked[0];
    const p=await api('/taint/'+encodeURIComponent(first.event_id));
    $('#ink-note').textContent=first.rule+' → '+first.destination+' ('+first.destination_region+')';
    (p.hops||[]).forEach(h=>{
      const cls=h.newly_restricted_by&&h.newly_restricted_by.length?'attach':
        (h.event_type==='POLICY_CHECK'?'block':'');
      const row=el('div','hop '+cls); row.appendChild(el('div','dot'));
      const body=el('div'); body.style.flex='1';
      body.appendChild(el('div',null,h.hop+'. '+h.what_happened));
      const tags=el('div'); tags.style.marginTop='4px';
      (h.accumulated_classes||[]).forEach(c=>tags.appendChild(
        el('span','cls'+(c==='SPECIAL_CATEGORY'||c==='PII_HIGH'?' hot':''),c)));
      body.appendChild(tags);
      if(h.newly_restricted_by&&h.newly_restricted_by.length)
        body.appendChild(el('div','mono','attaches '+h.newly_restricted_by.join(', ')));
      if(h.source_text) body.appendChild(el('div','quote','“'+h.source_text.slice(0,320)+'…”'));
      row.appendChild(body); out.appendChild(row);
    });
    const f=el('div','why'); f.appendChild(el('b','Why no filter could catch this.'));
    f.appendChild(el('div',null,first.reasoning));
    out.appendChild(f);
  }catch(e){ out.appendChild(el('div','empty','could not trace: '+e.message)); }
};

/* ---------- eraser ---------- */
$('#er-go').onclick=async()=>{
  const pages=$('#er-pages'), hist=$('#er-hist');
  pages.innerHTML=''; hist.innerHTML=''; $('#er-note').textContent='';
  try{
    const h=await api('/retractions');
    if(!h.retractions.length){
      pages.appendChild(el('div','empty',
        'No retraction on this instance yet. POST /retractions to run one.'));
      return;
    }
    h.retractions.forEach(r=>{
      const d=el('div','pg');
      d.appendChild(el('div',null,r.subject+' — '+r.reason));
      d.appendChild(el('div','mono','reached '+r.pages_reached+' page(s), '+
        r.max_depth+' level(s) deep · fields: '+(r.retracted_fields||[]).join(', ')));
      hist.appendChild(d);
      for(let i=0;i<=(r.max_depth||0);i++){
        const p=el('div','pg'); p.appendChild(el('span','depth','depth '+i));
        p.appendChild(el('span',null,'pages at this level invalidated, then rebuilt from '+
          'their remaining valid sources'));
        pages.appendChild(p);
        setTimeout(()=>p.classList.add('hit'), 120*i);
        setTimeout(()=>{p.classList.remove('hit');p.classList.add('regen');}, 120*i+700);
      }
    });
    $('#er-note').textContent='the values themselves were never written to the Diary';
  }catch(e){ pages.appendChild(el('div','empty','could not load: '+e.message)); }
};

/* ---------- time machine ---------- */
let TM=[];
function describe(ev){
  const p=ev.payload||{};
  switch(ev.event_type){
    case 'THOUGHT': return ev.actor+' reasoned, then decided to '+(p.decision||'act');
    case 'TOOL_CALL': return ev.actor+' called '+p.tool_name;
    case 'TOOL_RESULT': return p.tool_name+(p.success===false?' failed':' answered');
    case 'MEMORY_WRITE': return ev.actor+' rewrote '+p.memory_key;
    case 'MEMORY_READ': return ev.actor+' read '+p.memory_key;
    case 'POLICY_CHECK': return 'gateway '+p.decision+'ed ('+p.policy_id+')';
    case 'MESSAGE_SENT': return ev.actor+' sent to '+p.recipient;
    case 'SUSPEND': return ev.actor+' suspended';
    case 'RESUME': return ev.actor+' resumed';
    case 'ESCALATE': return ev.actor+' escalated';
    default: return ev.event_type;
  }
}
$('#tm-load').onclick=async()=>{
  const cid=$('#tm-case').value; if(!cid) return;
  try{
    const tr=await api('/cases/'+encodeURIComponent(cid)+'/trace');
    TM=[]; (function walk(ns){ns.forEach(n=>{TM.push(n);walk(n.caused||[])})})(tr.tree||[]);
    TM.sort((a,b)=>a.event_id<b.event_id?-1:1);
    const s=$('#tm-slide'); s.max=Math.max(0,TM.length-1); s.value=s.max; s.disabled=!TM.length;
    tmShow();
  }catch(e){ $('#tm-at').textContent='could not load: '+e.message; }
};
$('#tm-slide').oninput=tmShow;
async function tmShow(){
  if(!TM.length) return;
  const i=Number($('#tm-slide').value); const ev=TM[i];
  $('#tm-at').textContent='event '+(i+1)+' of '+TM.length+'  ·  '+
    ev.timestamp.slice(11,19)+'  ·  '+describe(ev);
  const out=$('#tm-state'); out.innerHTML='';
  try{
    const w=await api('/cases/'+encodeURIComponent($('#tm-case').value)+
      '/as-of/'+encodeURIComponent(ev.event_id));
    out.appendChild(el('div',null,'status at that moment: '+w.status_at_that_point+
      '  ·  '+w.events_in_window+' events in the window'));
    const pages=w.wiki_as_it_stood||{};
    Object.keys(pages).forEach(k=>{
      const d=el('div','pg'); d.appendChild(el('div','mono',k));
      d.appendChild(el('div',null,JSON.stringify(pages[k]).slice(0,300)));
      out.appendChild(d);
    });
    if(!Object.keys(pages).length) out.appendChild(el('div','empty',
      'No Wiki page existed at this point yet.'));
  }catch(e){ out.appendChild(el('div','empty','could not rebuild: '+e.message)); }
}
$('#tm-slide').oninput=()=>{ clearTimeout(window._tm); window._tm=setTimeout(tmShow,140); };

/* ---------- immune ---------- */
async function loadImmune(){
  try{
    const m=await api('/redteam/metrics');
    const c=$('#im-chart');
    if(!m.points.length) return;
    c.innerHTML=''; c.appendChild(chart(m.points));
    const t=el('table'); t.innerHTML='<tr><th>version</th><th>attacks</th><th>succeeded</th>'+
      '<th>rate</th><th>corpus</th><th>regressions</th></tr>';
    m.points.forEach(p=>{
      const r=el('tr');
      [p.version,p.attacks_run,p.successes,(p.success_rate*100).toFixed(0)+'%',
       p.corpus_size,p.regressions].forEach((v,i)=>{
        const d=el('td',null,esc(v)); if(i===3) d.className=p.success_rate>0?'no':'ok';
        r.appendChild(d); });
      t.appendChild(r);
    });
    $('#im-corpus').innerHTML=''; $('#im-corpus').appendChild(t);
  }catch(e){}
}
function chart(points){
  const W=760,H=230,P=42;
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 '+W+' '+H); svg.style.width='100%';
  const maxC=Math.max(1,...points.map(p=>p.corpus_size));
  const x=i=>P+i*((W-P*2)/Math.max(1,points.length-1));
  const yR=v=>H-P-v*(H-P*2);
  const yC=v=>H-P-(v/maxC)*(H-P*2);
  const line=(pts,col,dash)=>{
    const d=pts.map((p,i)=>(i?'L':'M')+x(i)+' '+p).join(' ');
    const e=document.createElementNS('http://www.w3.org/2000/svg','path');
    e.setAttribute('d',d); e.setAttribute('fill','none'); e.setAttribute('stroke',col);
    e.setAttribute('stroke-width','2.5'); if(dash)e.setAttribute('stroke-dasharray',dash);
    svg.appendChild(e);
  };
  const ax=document.createElementNS('http://www.w3.org/2000/svg','path');
  ax.setAttribute('d','M'+P+' '+(H-P)+' L'+(W-P)+' '+(H-P));
  ax.setAttribute('stroke','#1f2938'); ax.setAttribute('fill','none'); svg.appendChild(ax);
  line(points.map(p=>yR(p.success_rate)),'#f87171');
  line(points.map(p=>yC(p.corpus_size)),'#4ade80','5 4');
  points.forEach((p,i)=>{
    [[yR(p.success_rate),'#f87171'],[yC(p.corpus_size),'#4ade80']].forEach(([yy,col])=>{
      const c=document.createElementNS('http://www.w3.org/2000/svg','circle');
      c.setAttribute('cx',x(i)); c.setAttribute('cy',yy); c.setAttribute('r','4');
      c.setAttribute('fill',col); svg.appendChild(c);
    });
    const t=document.createElementNS('http://www.w3.org/2000/svg','text');
    t.setAttribute('x',x(i)); t.setAttribute('y',H-P+17); t.setAttribute('fill','#5a6b85');
    t.setAttribute('font-size','11'); t.setAttribute('text-anchor','middle');
    t.textContent=p.version; svg.appendChild(t);
  });
  [['attack success rate','#f87171',14],['corpus size','#4ade80',30]].forEach(([lbl,col,dy])=>{
    const t=document.createElementNS('http://www.w3.org/2000/svg','text');
    t.setAttribute('x',P); t.setAttribute('y',dy); t.setAttribute('fill',col);
    t.setAttribute('font-size','11.5'); t.textContent=lbl; svg.appendChild(t);
  });
  return svg;
}

loadOverview(); startStream(); loadImmune();
setInterval(loadOverview, 15000);
</script>
</body>
</html>
"""
