/// The dashboard's one page: inline CSS and JS, served from the binary. It reads the JSON routes and
/// refetches them whenever `/events` says something changed; the only things it can send are
/// interrupt and pause/resume.
enum DashboardPage {
    static let html = #"""
<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>DollOS 後台</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.45 -apple-system, "PingFang TC", sans-serif; background: #111418; color: #dde3ea; }
  header { position: sticky; top: 0; z-index: 2; display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center;
           padding: 8px 14px; background: #1b2028; border-bottom: 1px solid #2c3440; }
  header .name { font-weight: 700; font-size: 15px; }
  .badge { padding: 1px 8px; border-radius: 10px; background: #2c3440; }
  .badge.thinking { background: #2d5a8a; } .badge.acting { background: #6a5a1e; } .badge.idle { background: #333a44; } .badge.sleeping { background: #3a2d6a; }
  .badge.paused { background: #8a2d2d; } .badge.live { background: #285a35; } .badge.dead { background: #8a2d2d; }
  header button { font: inherit; padding: 3px 12px; border-radius: 5px; border: 1px solid #4a5666; background: #26303c; color: #fff; cursor: pointer; }
  header button:disabled { opacity: .4; cursor: default; }
  main { display: grid; grid-template-columns: minmax(300px, 1.1fr) minmax(360px, 1.3fr) minmax(280px, 1fr); gap: 10px; padding: 10px; }
  section { background: #181c22; border: 1px solid #2a313b; border-radius: 6px; padding: 8px 10px; min-width: 0;
            max-height: calc(100vh - 70px); overflow: auto; }
  h2 { margin: 0 0 6px; font-size: 14px; color: #9fb4cc; }
  h3 { margin: 10px 0 4px; font-size: 13px; color: #9fb4cc; }
  img.shot { width: 100%; border: 1px solid #333; cursor: zoom-in; display: block; }
  img.thumb { max-width: 160px; border: 1px solid #333; cursor: zoom-in; margin: 3px 4px 0 0; }
  pre { white-space: pre-wrap; word-break: break-word; margin: 3px 0; font: 12px/1.4 ui-monospace, Menlo, monospace; color: #c9d2dc; }
  .muted { color: #8794a3; } .secret { color: #e39b5b; }
  .item { border-left: 3px solid #3a4452; padding: 4px 8px; margin: 0 0 8px; }
  .item.owner { border-color: #4f9d69; } .item.developer { border-color: #4fa3a3; } .item.turn { border-color: #4f7fb8; } .item.watch { border-color: #b89a4f; } .item.program { border-color: #6d5f8a; }
  .speech { font-size: 14px; color: #fff; margin: 3px 0; }
  .call { margin: 3px 0 3px 6px; }
  .call summary, .mem summary { cursor: pointer; }
  .call .fn { color: #8fc1ff; }
  .mem { margin: 2px 0; }
  .meta { color: #8794a3; font-size: 11px; }
  #zoom { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.85); z-index: 9; cursor: zoom-out; }
  #zoom img { max-width: 98vw; max-height: 98vh; margin: 1vh auto; display: block; }
</style>
</head>
<body>
<header>
  <span class="name" id="name">…</span>
  <span class="badge" id="state">…</span>
  <span class="badge" id="paused" hidden>暫停中</span>
  <span id="menu"></span>
  <span id="watch"></span>
  <span id="ctx"></span>
  <span id="effort"></span>
  <span id="queued"></span>
  <span id="interrupted" class="muted"></span>
  <span style="flex:1"></span>
  <button id="btn-interrupt" title="取消她這一回合正在跑的模型呼叫">中斷</button>
  <button id="btn-pause">暫停</button>
  <span class="badge" id="live">連線中</span>
</header>
<main>
  <section id="vision"><h2>視覺</h2><div id="vision-body" class="muted">還沒有截圖</div></section>
  <section id="actions"><h2>動作</h2><div id="timeline"></div></section>
  <section id="memory"><h2>記憶</h2>
    <label class="muted"><input type="checkbox" id="show-program"> 顯示程式讀的快捷鍵（tier program）</label>
    <div id="memory-body"></div></section>
  <section id="calendar"><h2>行事曆</h2>
    <form id="cal-form">
      <select id="cal-op"><option value="add">加</option><option value="change">改</option><option value="delete">刪</option></select>
      <input id="cal-id" placeholder="編號（改／刪）" size="6">
      <input id="cal-on" placeholder="只這一天 YYYY-MM-DD" size="12">
      <input id="cal-title" placeholder="什麼事">
      <input id="cal-date" placeholder="日期 YYYY-MM-DD" size="12">
      <input id="cal-start" placeholder="開始 HH:MM" size="6">
      <input id="cal-end" placeholder="結束 HH:MM" size="6">
      <input id="cal-repeat" placeholder="重複 daily 或 1,3,5" size="10">
      <input id="cal-until" placeholder="重複到 YYYY-MM-DD" size="12">
      <label class="muted"><input type="checkbox" id="cal-allday"> 改成全天</label>
      <button>送出</button>
    </form>
    <div class="muted">主人改的每一筆，她的時間線上都會多一則【行事曆】通知（不會叫醒她）。</div>
    <div id="calendar-body"></div></section>
  <section id="drives"><h2>她的狀態（訊號、掛念）</h2><div id="drives-body" class="muted">關閉</div></section>
</main>
<div id="zoom"><img alt=""></div>
<script>
"use strict";
const $ = id => document.getElementById(id);
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function clock(ts) {
  const d = new Date(ts * 1000), p = n => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function shot(name, cls) {
  const img = el("img", cls);
  img.src = "/shots/" + encodeURIComponent(name);
  img.alt = name;
  img.onclick = () => { $("zoom").querySelector("img").src = img.src; $("zoom").style.display = "block"; };
  return img;
}
$("zoom").onclick = () => { $("zoom").style.display = "none"; };
function details(summary, body, open) {
  const d = el("details");
  if (open) d.open = true;
  d.append(summary instanceof Node ? summary : el("summary", null, summary));
  d.append(body);
  return d;
}

let status = null;
function renderStatus(s) {
  status = s;
  $("name").textContent = s.name;
  const names = { thinking: "思考中（模型呼叫中）", acting: "動作中（跑工具）", idle: "醒著，等下一件事", sleeping: "睡覺中" };
  $("state").textContent = (names[s.state] || s.state) + (s.sleep ? "：" + s.sleep : "");
  $("state").className = "badge " + s.state;
  $("paused").hidden = !s.paused;
  $("menu").textContent = "todo：" + s.todo + (s.item ? `「${s.item}」` : "") + (s.pending.length ? `；後面 ${s.pending.length} 項` : "");
  $("watch").textContent = "watch：" + (s.watch || "沒有");
  $("ctx").textContent = `context 約 ${s.contextTokens} token`;
  $("effort").textContent = "effort：" + s.effort;
  $("queued").textContent = `排隊 ${s.queued} 件 · 第 ${s.turn} 回合`;
  $("interrupted").textContent = s.lastInterrupt ? "上次中斷 " + clock(s.lastInterrupt) : "";
  $("btn-interrupt").disabled = s.state !== "thinking";
  $("btn-pause").textContent = s.paused ? "繼續" : "暫停";
}

function renderVision(v) {
  const box = $("vision-body");
  box.replaceChildren();
  box.className = "";
  if (!v.image && !v.windows) { box.className = "muted"; box.textContent = "還沒有截圖"; return; }
  if (v.ts) box.append(el("div", "meta", `#${v.seq} · ${clock(v.ts)}`));
  if (v.image) { box.append(el("h3", null, "全螢幕")); box.append(shot(v.image, "shot")); }
  if (v.cropNote) box.append(el("pre", null, v.cropNote));
  if (v.crop) { box.append(el("h3", null, "視窗圖")); box.append(shot(v.crop, "shot")); }
  if (v.windows) { box.append(el("h3", null, "開著的視窗／焦點")); box.append(el("pre", null, v.windows)); }
  if (v.window) { box.append(el("h3", null, "視窗概要")); box.append(el("pre", null, v.window)); }
}

function renderCall(c) {
  const params = c.parameters.map(p => `${p.name}=${p.value.length > 60 ? p.value.slice(0, 60) + "…" : p.value}`).join(", ");
  const sum = el("summary");
  sum.append(el("span", "fn", c.name || "（格式壞掉）"), document.createTextNode(`(${params})`));
  if (c.secret) sum.append(el("span", "secret", " 秘密"));
  const body = el("div");
  if (c.malformed) body.append(el("pre", "secret", "格式錯：" + c.malformed));
  for (const p of c.parameters) if (p.value.length > 60 || p.value.includes("\n")) body.append(el("pre", "muted", `${p.name}:\n${p.value}`));
  body.append(el("pre", null, c.result === null || c.result === undefined ? "（還沒有結果）" : c.result));
  if (c.image) body.append(shot(c.image, "thumb"));
  if (c.crop) body.append(shot(c.crop, "thumb"));
  if (c.window) body.append(details("視窗概要", el("pre", null, c.window)));
  const d = details(sum, body);
  d.className = "call";
  return d;
}

function renderTimeline(items) {
  const box = $("timeline");
  const open = new Set([...box.querySelectorAll("details[open]")].map(d => d.dataset.key));
  box.replaceChildren();
  for (const it of items) {
    const div = el("div", "item " + it.type);
    const head = el("div", "meta", `#${it.seq} · ${clock(it.ts)}`);
    if (it.secret) head.append(el("span", "secret", " · 含秘密"));
    div.append(head);
    if (it.type === "owner") {
      div.append(el("div", "speech", `主人（${it.voice ? "語音" : "打字"}）：${it.text}`));
    } else if (it.type === "developer") {
      div.append(el("div", "speech", `開發者（dollctl）：${it.text}`));
    } else if (it.type === "watch") {
      div.append(el("pre", null, "watch：" + it.text));
      if (it.image) div.append(shot(it.image, "thumb"));
    } else if (it.type === "program") {
      const first = it.text.split("\n")[0];
      div.append(details(first, el("pre", null, it.text)));
    } else {
      const stats = [];
      if (it.seconds !== null && it.seconds !== undefined) stats.push(`${it.seconds.toFixed(1)} 秒`);
      if (it.prompt !== null && it.prompt !== undefined) stats.push(`prompt ${it.prompt}`);
      if (it.cached !== null && it.cached !== undefined) stats.push(`cached ${it.cached}`);
      if (it.effort) stats.push(`effort ${it.effort}`);
      head.append(document.createTextNode(" · 她的回合 · " + stats.join(" · ")));
      if (it.thought) div.append(el("div", "muted", "（心裡）" + it.thought));
      for (const c of it.calls) {
        const text = c.channel && c.parameters.find(p => p.name === "text");
        if (!text) { div.append(renderCall(c)); continue; }
        div.append(el("div", "speech", `說（${c.channel}）：` + text.value.trim()));
        if (c.result) div.append(el("div", "muted", c.result));
      }
      if (it.footer) div.append(details("她看到的【狀況】", el("pre", "muted", it.footer)));
    }
    div.querySelectorAll("details").forEach((d, i) => { d.dataset.key = it.seq + ":" + i; if (open.has(d.dataset.key)) d.open = true; });
    box.append(div);
  }
}

function memEntry(e, unmark) {
  const sum = el("summary");
  sum.append(document.createTextNode(e.title + " "));
  const strength = e.deep ? " · deep（不淡）" : e.settled ? " · 記牢了（不淡）"
    : e.strength == null ? "" : ` · 強度 ${e.strength.toFixed(2)}${e.faded ? "（淡了，不自動附）" : ""}`;
  sum.append(el("span", "meta", `${e.kind || "（無 kind）"} · ${e.level} ${e.confidence} · ${e.tier}${strength}`));
  const body = el("div");
  body.append(el("pre", null, e.text));
  if (e.strength != null) {
    body.append(el("div", "meta", `id ${e.id} · 證據 ${e.evidence.map(clock).join("、")}` + (e.settled ? ` · ${clock(e.settled)} 記牢` : "")));
  }
  if (unmark) {
    const b = el("button", null, "取消 deep");
    b.onclick = () => post("/api/memory/undeep/" + encodeURIComponent(e.id));
    body.append(b);
  }
  const d = details(sum, body);
  d.className = "mem";
  d.dataset.key = (unmark ? "d:" : "m:") + e.id;
  return d;
}

function deepForm() {
  const form = el("form");
  const input = el("input");
  input.placeholder = "記憶 id";
  form.append(input, el("button", null, "標成 deep"));
  form.onsubmit = ev => {
    ev.preventDefault();
    const id = input.value.trim();
    if (id) post("/api/memory/deep/" + encodeURIComponent(id));
  };
  return form;
}

function renderMemory(m) {
  const box = $("memory-body");
  const open = new Set([...box.querySelectorAll("details[open]")].map(d => d.dataset.key));
  box.replaceChildren();
  const showProgram = $("show-program").checked;
  box.append(el("h3", null, `這次附上的經驗（${m.attached.length}）`));
  for (const e of m.attached) box.append(memEntry(e));
  box.append(el("h3", null, `deep 記憶（${m.deep.length}）：永遠不淡`));
  for (const e of m.deep) box.append(memEntry(e, true));
  if (!deepHolder.firstChild) deepHolder.append(deepForm());
  box.append(deepHolder);
  box.append(el("h3", null, `這次送出的快捷鍵（${m.cheatsheets.length}）`));
  for (const s of m.cheatsheets) {
    const d = details(s.title, el("pre", null, s.text));
    d.dataset.key = "s:" + s.seq;
    box.append(d);
  }
  const titles = { own: "own（她自己的秘密）", public: "public", secret: "secret（主人的秘密）" };
  for (const f of m.folders) {
    const shown = f.entries.filter(e => showProgram || e.tier !== "program");
    const hidden = f.entries.length - shown.length;
    box.append(el("h3", f.folder === "public" ? null : "secret",
                  `${titles[f.folder] || f.folder}（${f.entries.length}${hidden ? `，藏了 ${hidden} 條快捷鍵` : ""}）`));
    for (const e of shown) box.append(memEntry(e));
  }
  box.querySelectorAll("details").forEach(d => { if (open.has(d.dataset.key)) d.open = true; });
}

function renderCalendar(c) {
  const box = $("calendar-body");
  box.replaceChildren();
  box.append(el("h3", null, `今天起 ${c.days} 天（今天 ${c.today}）`));
  let day = null;
  for (const e of c.entries) {
    if (e.day !== day) { day = e.day; box.append(el("h4", null, e.label)); }
    const time = e.start ? (e.end ? `${e.start}–${e.end}` : e.start) : "全天";
    const notes = [`#${e.id}`, e.repeat ? `重複 ${e.repeat}` : null, e.changed ? "這天改過" : null].filter(Boolean).join("，");
    const row = el("div", e.past ? "muted" : null, `${time} ${e.title}（${notes}） `);
    const del = el("button", null, e.repeat ? "取消這天" : "刪");
    del.onclick = () => calPost("delete", e.repeat ? { id: e.id, on: e.original } : { id: e.id });
    row.append(del);
    box.append(row);
  }
  box.append(el("h3", null, `所有項目（${c.items.length}）`));
  for (const it of c.items) {
    const time = it.start ? (it.end ? `${it.start}–${it.end}` : it.start) : "全天";
    box.append(el("div", null, `#${it.id} ${it.date} ${time} ${it.title}` + (it.repeat ? ` · 重複 ${it.repeat}` : "")
      + (it.until ? ` 到 ${it.until}` : "") + (it.exceptions ? ` · ${it.exceptions} 天另外改過` : "") + ` · ${it.by === "owner" ? "主人加的" : "她加的"}`));
  }
}

function renderDrives(d) {
  const box = $("drives-body");
  box.replaceChildren();
  if (!d.enabled) { box.textContent = "關閉（[drives] enabled = false）"; return; }
  const hm = ts => new Date(ts * 1000).toLocaleTimeString("zh-TW", { hour: "2-digit", minute: "2-digit", hour12: false });
  const label = { drive: "動力", stress: "壓力", closeness: "親近", sleepiness: "睡意" };
  box.append(el("div", null, "現在：" + d.values.map(v =>
    `${v.label} ${v.value.toFixed(2)}（${v.outWhenLow ? "低於" : "高於"} ${v.edge} 算超出${v.out ? "，超出" : ""}）`).join("　")));
  box.append(el("div", null, "她看到的感覺：" + (d.felt || "（沒有，四個都在範圍內）")));
  if (d.nextCrossing) box.append(el("div", "muted", "下一次有訊號跨過範圍：" + hm(d.nextCrossing)));
  // The last day's curves: one polyline per signal on a 0…1 scale.
  const colors = { drive: "#e0a030", stress: "#e05050", closeness: "#50a0e0", sleepiness: "#a070e0" };
  const W = 720, H = 120, n = d.curve.length;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", W); svg.setAttribute("height", H);
  for (const k of Object.keys(colors)) {
    const pts = d.curve.map((s, i) => `${(i / Math.max(1, n - 1) * W).toFixed(1)},${(H - s.values[k] * H).toFixed(1)}`).join(" ");
    const pl = document.createElementNS(svg.namespaceURI, "polyline");
    pl.setAttribute("points", pts); pl.setAttribute("fill", "none"); pl.setAttribute("stroke", colors[k]);
    svg.append(pl);
  }
  box.append(el("h3", null, "最近一天（" + Object.entries(colors).map(([k, c]) => label[k] + " " + c).join("、") + "）"), svg);
  box.append(el("h3", null, `每一次變化和原因（${d.changes.length}）`));
  for (const c of d.changes.slice().reverse()) box.append(el("div", null, `${hm(c.ts)} ${label[c.signal]} ${c.before.toFixed(2)} → ${c.after.toFixed(2)}：${c.cause}`));
  const about = { owner: "主人", duty: "該做的", own: "自己的事" };
  box.append(el("h3", null, `還掛著的（${d.open.length}）`));
  for (const c of d.open) box.append(el("div", null, `#${c.id}〔${about[c.about]}〕${c.text}（${c.source === "her" ? "她記的" : c.source === "calendar" ? "行事曆" : "todo 失敗"}，冒出來 ${c.surfaced.length} 次，聯想 ${c.associated.length} 次）`));
  box.append(el("h3", null, `結束的（${d.ended.length}）`));
  for (const c of d.ended.slice(-20).reverse()) box.append(el("div", "muted", `#${c.id} ${c.text}（${({ done: "做完", let_go: "放下", passed: "時間過了" })[c.end] || c.end}）`));
  box.append(el("h3", null, `最近的走神和聯想（${d.thoughts.length}）`));
  for (const t of d.thoughts.slice().reverse()) box.append(el("div", null, `${hm(t.ts)} ${t.kind}：${t.text}`));
}

async function calPost(op, params) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v !== undefined && v !== null));
  const r = await fetch(`/api/calendar/${op}?${q}`, { method: "POST" });
  if (!r.ok) alert(await r.text());
  refresh();
}
$("cal-form").onsubmit = ev => {
  ev.preventDefault();
  const v = id => $(id).value.trim();
  const op = v("cal-op");
  const p = { id: v("cal-id"), on: v("cal-on"), title: v("cal-title"), date: v("cal-date"), start: v("cal-start"), end: v("cal-end") };
  if (op === "add") { delete p.id; delete p.on; p.repeat = v("cal-repeat"); p.until = v("cal-until"); }
  if (op === "change" && $("cal-allday").checked) p.all_day = "true";
  if (op === "delete") { for (const k of ["title", "date", "start", "end"]) delete p[k]; }
  calPost(op, p);
};

async function get(path) {
  const r = await fetch(path, { cache: "no-store" });
  if (!r.ok) throw new Error(path + ": " + r.status + " " + await r.text());
  return r.json();
}

let busy = false, dirty = false, memory = null;
const deepHolder = el("div");
async function refresh() {
  if (busy) { dirty = true; return; }
  busy = true;
  try {
    const [s, t, v, m, c, dr] = await Promise.all(["/api/status", "/api/timeline", "/api/vision", "/api/memory", "/api/calendar", "/api/drives"].map(get));
    renderStatus(s); renderTimeline(t); renderVision(v); memory = m; renderMemory(m); renderCalendar(c); renderDrives(dr);
  } catch (e) {
    console.error(e);
  } finally {
    busy = false;
    if (dirty) { dirty = false; refresh(); }
  }
}
$("show-program").onchange = () => { if (memory) renderMemory(memory); };

async function post(path) {
  const r = await fetch(path, { method: "POST" });
  if (!r.ok && r.status !== 409) alert(path + ": " + r.status);
  if (r.status === 409 && path.startsWith("/api/memory/")) alert(await r.text());
  refresh();
}
$("btn-interrupt").onclick = () => post("/api/interrupt");
$("btn-pause").onclick = () => post(status && status.paused ? "/api/resume" : "/api/pause");

const events = new EventSource("/events");
events.addEventListener("change", refresh);
events.onopen = () => { $("live").textContent = "即時"; $("live").className = "badge live"; };
events.onerror = () => { $("live").textContent = "斷線，重連中"; $("live").className = "badge dead"; };
</script>
</body>
</html>
"""#
}
