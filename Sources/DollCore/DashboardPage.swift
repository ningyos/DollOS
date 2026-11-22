/// The dashboard's one page: inline CSS and JS, served from the binary. It reads the JSON routes and
/// refetches them whenever `/events` says something changed; it sends interrupt and pause/resume and
/// the owner's edits (calendar, goals, ideal self, deep marks, history undo).
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
  .badge.thinking { background: #2d5a8a; } .badge.acting { background: #6a5a1e; } .badge.idle { background: #333a44; } .badge.maintenance { background: #3a2d6a; }
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
  <section id="mind"><h2>她怎麼想（清單的計畫、估的和實際、卡住和失敗的原因）</h2><div id="mind-body" class="muted">還沒有清單</div></section>
  <section id="memory"><h2>記憶</h2>
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
  <section id="goals"><h2>她的方向（想成為的樣子、目標樹、做完的事）</h2><div id="goals-body" class="muted">關閉</div></section>
  <section id="tools"><h2>她的工具（她存下來的一串動作）</h2><div id="tools-body" class="muted">還沒有</div></section>
  <section id="boots"><h2>開機紀錄（上一次執行的 log 整理）</h2><div id="boots-body" class="muted">還沒有</div></section>
  <section id="history"><h2>改動紀錄（她的想成為的樣子、目標樹、工具、記憶、紀錄：誰、什麼時候、做了什麼、為什麼；可以看改了什麼、退回一個、或全部回到某一筆之後）</h2><div id="history-body" class="muted">還沒有</div></section>
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
  const names = { thinking: "思考中（模型呼叫中）", acting: "動作中（跑工具）", idle: "等下一件事", maintenance: "系統維護中" };
  $("state").textContent = (names[s.state] || s.state) + (s.maintenance ? "：" + s.maintenance : "") + (s.connection ? " · " + s.connection : "");
  $("state").className = "badge " + s.state;
  $("paused").hidden = !s.paused;
  // No computer ([computer] enabled = false): nothing to show in 視覺 and 她的工具.
  $("vision").hidden = !s.computer;
  $("tools").hidden = !s.computer;
  $("menu").textContent = "todo：" + s.todo + (s.item ? `「${s.item}」第 ${s.rounds} 回合` : "") + (s.deciding ? "（等她決定：繼續／問你／失敗）" : "")
    + (s.pending.length ? `；後面 ${s.pending.length} 項` : "") + s.goOn.map(g => "；她選了繼續：" + g).join("");
  renderMind(s);
  $("ctx").textContent = `context 約 ${s.contextTokens} token`;
  $("effort").textContent = "effort：" + s.effort;
  $("queued").textContent = `排隊 ${s.queued} 件 · 第 ${s.turn} 回合`;
  $("interrupted").textContent = s.lastInterrupt ? "上次中斷 " + clock(s.lastInterrupt) : "";
  $("btn-interrupt").disabled = s.state !== "thinking";
  $("btn-pause").textContent = s.paused ? "繼續" : "暫停";
}

// Hook 1–4 (2026-11-17): what she wrote, verbatim.
function renderMind(s) {
  const box = $("mind-body");
  box.replaceChildren();
  box.className = "";
  // Each fact with where the program found its quote (2026-11-17); a plan from before has none.
  const fact = f => `事實：${f.fact}` + (f.source ? `（出處：${f.source}「${f.quote}」）` : "（沒有出處：舊的清單）");
  const plan = p => [`項目：${p.items.map(i => "「" + i + "」").join("")}`, ...p.facts.map(fact), `假設：${p.assumptions}`,
    `邊界：${p.limits}`, `估：${p.estimate} 回合`, `怎麼確認：${p.check}`];
  // The estimate check (2026-11-18): what the like past lists took, a refusal, and her reason.
  const check = (c, est) => !c ? ["程式比對：沒有像這次的舊清單，估的照收"] : [
    `程式比對：像這次的舊清單 ${c.past.length} 份，實際中位數 ${c.reference} 回合（${c.low} 到 ${c.high} 算接近）`,
    ...c.past.map(p => `　${p.items.map(i => "「" + i + "」").join("")}：估 ${p.estimate} → 實際 ${p.rounds} 回合`),
    ...(c.refused != null ? [`程式退回了她先估的 ${c.refused} 回合`] : []),
    ...(c.reason ? [`她說這次不一樣：${c.reason}`] : c.refused != null && (est < c.low || est > c.high) ? ["她沒給理由，第三次程式照收"] : [])];
  if (s.plan) {
    box.append(el("div", null, `現在的清單（到現在 ${s.listRounds} 回合）`));
    for (const l of plan(s.plan)) box.append(el("div", "muted", l));
    for (const l of check(s.estimateCheck, s.plan.estimate)) box.append(el("div", "muted", l));
    for (const g of s.goOn) box.append(el("div", null, "她選了繼續：" + g));
  }
  if (s.estimates.length) {
    box.append(el("div", null, "做完或失敗的清單（新到舊）："));
    for (const r of [...s.estimates].reverse()) {
      const body = el("div");
      for (const l of plan(r.plan)) body.append(el("div", "muted", l));
      if (r.text) for (const l of check(r.check, r.plan.estimate)) body.append(el("div", "muted", l));
      if (r.cause) body.append(el("div", null, `根本原因：${r.cause}`), el("div", "muted", `她照抄的根據：${r.evidence}`));
      const was = r.check && r.check.refused != null ? `（先估 ${r.check.refused} 被退回）` : "";
      const why = r.check && r.check.reason ? `；她的理由：${r.check.reason}` : "";
      box.append(details(`${clock(r.ts)} ${r.outcome}：估 ${r.plan.estimate} 回合${was} → 實際 ${r.rounds} 回合、${r.seconds} 秒${why}`, body));
    }
  }
  if (!s.plan && !s.estimates.length) { box.className = "muted"; box.textContent = "還沒有清單"; }
}

function renderVision(v) {
  const box = $("vision-body");
  box.replaceChildren();
  box.className = "";
  if (!v.image && !v.window) { box.className = "muted"; box.textContent = "還沒有讀過視窗"; return; }
  if (v.ts) box.append(el("div", "meta", `#${v.seq} · ${clock(v.ts)}`));
  if (v.image) { box.append(el("h3", null, "視窗截圖")); box.append(shot(v.image, "shot")); }
  if (v.window) { box.append(el("h3", null, "元素清單")); box.append(el("pre", null, v.window)); }
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
  if (c.window) body.append(details("元素清單", el("pre", null, c.window)));
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
        // The screenshot that went with her words to the owner (Doll.shotSent, 2026-11-16).
        const sent = c.result && c.result.match(/截圖給主人：([^）]+)）/);
        if (sent) div.append(shot(sent[1], "thumb"));
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

function fixEntry(f) {
  const who = { her: "她改的", audit: "系統維護改的", flag: "系統維護標的（沒改）" }[f.by] || f.by;
  const what = f.by === "flag" ? (f.after == null ? "可能整條都錯" : `可能該是「${f.after}」`)
    : f.after == null ? "收起來了" : `改成「${f.after}」`;
  const sum = el("summary");
  sum.append(document.createTextNode(`#${f.id} ${who}：「${f.before}」${what} `));
  sum.append(el("span", "meta", clock(f.ts)));
  const body = el("div");
  body.append(el("div", "meta", `依據（${f.basisFrom}）：「${f.basis}」`));
  body.append(el("div", "meta", `為什麼：${f.why} · 記憶 ${f.memory}` + (f.replacement ? ` → ${f.replacement}` : "")));
  if (f.by !== "flag") {
    const b = el("button", null, "還原");
    b.onclick = () => post("/api/memory/revert/" + f.id);
    body.append(b);
  }
  const d = details(sum, body);
  d.className = "mem";
  d.dataset.key = "f:" + f.id;
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
  box.append(el("h3", null, `這次附上的經驗（${m.attached.length}）`));
  for (const e of m.attached) box.append(memEntry(e));
  box.append(el("h3", null, `deep 記憶（${m.deep.length}）：永遠不淡`));
  for (const e of m.deep) box.append(memEntry(e, true));
  if (!deepHolder.firstChild) deepHolder.append(deepForm());
  box.append(deepHolder);
  box.append(el("h3", null, `改過的記憶（${m.fixes.length}）：她改的、系統維護改的、系統維護標出來沒改的`));
  for (const f of m.fixes) box.append(fixEntry(f));
  const titles = { own: "own（她自己的秘密）", public: "public", secret: "secret（主人的秘密）" };
  for (const f of m.folders) {
    box.append(el("h3", f.folder === "public" ? null : "secret", `${titles[f.folder] || f.folder}（${f.entries.length}）`));
    for (const e of f.entries) box.append(memEntry(e));
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
      + (it.until ? ` 到 ${it.until}` : "") + (it.exceptions ? ` · ${it.exceptions} 天另外改過` : "") + ` · ${it.by === "owner" ? "主人加的" : it.by === "program" ? "程式建的" : "她加的"}`));
  }
}

function renderDrives(d) {
  const box = $("drives-body");
  box.replaceChildren();
  if (!d.enabled) { box.textContent = "關閉（[drives] enabled = false）"; return; }
  const hm = ts => new Date(ts * 1000).toLocaleTimeString("zh-TW", { hour: "2-digit", minute: "2-digit", hour12: false });
  const label = { drive: "動力", stress: "壓力", closeness: "親近" };
  box.append(el("div", null, "現在：" + d.values.map(v =>
    `${v.label} ${v.value.toFixed(2)}（${v.outWhenLow ? "低於" : "高於"} ${v.edge} 算超出${v.out ? "，超出" : ""}）`).join("　")));
  box.append(el("div", null, "她看到的感覺：" + (d.felt || "（沒有，三個都在範圍內）")));
  if (d.nextCrossing) box.append(el("div", "muted", "下一次有訊號跨過範圍：" + hm(d.nextCrossing)));
  // The last day's curves: one polyline per signal on a 0…1 scale.
  const colors = { drive: "#e0a030", stress: "#e05050", closeness: "#50a0e0" };
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
  for (const c of d.open) box.append(el("div", null, `#${c.id}〔${about[c.about]}〕${c.text}（${({ her: "她記的", calendar: "行事曆", goal: "目標樹", maintenance: "系統維護" })[c.source] || "todo 失敗"}，冒出來 ${c.surfaced.length} 次，聯想 ${c.associated.length} 次）`));
  box.append(el("h3", null, `結束的（${d.ended.length}）`));
  for (const c of d.ended.slice(-20).reverse()) box.append(el("div", "muted", `#${c.id} ${c.text}（${({ done: "做完", let_go: "放下", passed: "時間過了" })[c.end] || c.end}）`));
  box.append(el("h3", null, `最近的走神和聯想（${d.thoughts.length}）`));
  for (const t of d.thoughts.slice().reverse()) box.append(el("div", null, `${hm(t.ts)} ${t.kind}：${t.text}`));
}

// Her direction (phase 2): the owner sees and edits; she is told of every edit.
function btn(text, f) { const b = el("button", null, text); b.onclick = f; return b; }
async function goalPost(path, params) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v !== undefined && v !== null));
  const r = await fetch(`${path}?${q}`, { method: "POST" });
  if (!r.ok) alert(await r.text());
  refresh();
}
function renderGoals(g) {
  const box = $("goals-body");
  box.replaceChildren();
  if (!g.enabled) { box.textContent = "關閉（[drives] enabled = false）"; return; }
  box.append(el("h3", null, "她想成為的樣子"), el("pre", null, g.ideal));
  const ta = el("textarea"); ta.rows = 4; ta.cols = 80; ta.value = g.ideal;
  box.append(ta, btn("改成這樣（她會被告知）", () => goalPost("/api/ideal/edit", { text: ta.value })));
  box.append(el("h3", null, `修改紀錄（${g.revisions.length}）`));
  const who = { pack: "角色包", her: "她", maintenance: "系統維護", owner: "主人", revert: "主人退回" };
  for (const r of g.revisions) {
    const row = el("div", null, `第 ${r.id} 版 ${clock(r.ts)} ${who[r.by] || r.by}：${r.why}${r.basedOn ? "（根據：" + r.basedOn + "）" : ""} → ${r.after}`);
    if (r.by !== "pack") row.append(" ", btn("退回這次", () => goalPost("/api/ideal/revert", { id: r.id })));
    box.append(row);
  }
  box.append(el("h3", null, "目標樹（還開著的）"));
  const level = { 1: "目標", 2: "子目標", 3: "步驟" };
  const open = g.nodes.filter(n => !n.ended);
  const show = (n, depth) => {
    const row = el("div", null, "　".repeat(depth) + `#${n.id}〔${level[n.level]}〕${n.text}${n.by === "owner" ? "（主人加的）" : ""}`);
    row.append(" ", btn("改", () => { const t = prompt("改成", n.text); if (t) goalPost("/api/goals/change", { id: n.id, text: t }); }));
    if (n.level < 3) row.append(" ", btn("加下一層", () => { const t = prompt("加什麼"); if (t) goalPost("/api/goals/add", { parent: n.id, text: t }); }));
    row.append(" ", btn("做完", () => goalPost("/api/goals/done", { id: n.id })), " ", btn("放下", () => goalPost("/api/goals/drop", { id: n.id })));
    box.append(row);
    for (const c of open.filter(c => c.parent === n.id)) show(c, depth + 1);
  };
  for (const n of open.filter(n => n.level === 1)) show(n, 0);
  box.append(btn("加一個目標", () => { const t = prompt("目標"); if (t) goalPost("/api/goals/add", { text: t }); }));
  if (g.link) box.append(el("div", "muted", `手上的 todo 清單：${g.link.subgoal ? "子目標 #" + g.link.subgoal + " 的步驟" : "不是目標樹的"}；做完了 ${g.link.done.length} 項`));
  box.append(el("h3", null, `做完的事（${g.record.length}；動力：現在算數的／全額）`));
  const kind = { step: "步驟", subgoal: "子目標", goal: "目標", list: "todo 清單" };
  const ev = e => e ? (e.good ? "好" : "不對") + (e.quote ? `「${e.quote}」` : "") : "—";
  for (const e of g.record) {
    const row = el("div", null, `#${e.id} ${clock(e.ts)} ${kind[e.kind]}：${e.text}　她記的主人評價：${ev(e.her)}　主人：${ev(e.owner)}　${e.given.toFixed(2)}／${e.amount.toFixed(2)}`);
    row.append(" ", btn("好", () => goalPost("/api/record/eval", { id: e.id, kind: "good" })), " ", btn("不好", () => goalPost("/api/record/eval", { id: e.id, kind: "bad" })));
    if (e.owner) row.append(" ", btn("取消我的評價", () => goalPost("/api/record/eval", { id: e.id, kind: "none" })));
    box.append(row);
  }
  box.append(el("div", "muted", `上次碰到新東西：${g.lastNew ? clock(g.lastNew) : "—"}${g.curiosityDue ? `；沒有新東西的話，${clock(g.curiosityDue)} 會冒一次好奇` : "；好奇現在是關的（沒有電腦就沒有新東西的來源）"}`));
}

function renderTools(ts) {
  const box = $("tools-body");
  box.replaceChildren();
  if (!ts.length) { box.textContent = "還沒有"; return; }
  for (const t of ts) {
    const head = el("h3", t.retired ? "muted" : null, `${t.name}${t.retired ? `（停用：連續失敗 ${t.failuresInRow} 次）` : ""}`);
    head.append(" ", btn("刪掉", async () => {
      if (!confirm(`刪掉工具「${t.name}」？`)) return;
      const r = await fetch(`/api/tools/delete?${new URLSearchParams({ name: t.name })}`, { method: "POST" });
      if (!r.ok) alert(await r.text());
      refresh();
    }));
    box.append(head, el("div", null, `什麼時候用：${t.when}`));
    box.append(el("div", null, "參數：" + (t.params.length ? t.params.join("；") : "沒有")));
    box.append(el("div", null, "步驟：" + t.steps.join(" → ")));
    box.append(el("div", null, `結果怎麼對：${t.expect}`));
    box.append(el("div", "muted", `用了 ${t.uses} 次，成功 ${t.successes} 次${t.lastRun ? "，上次 " + t.lastRun : ""}；從 ${t.from} 存的，${t.saved}；檔案 ${t.file}`));
  }
}

const endWords = { clean: "正常關掉", fatal: "程式出錯停下", boot_failed: "開機失敗", crashed: "當掉", no_end: "沒有結束的紀錄（強制結束、當掉或斷電）" };
function renderBoots(bs) {
  const box = $("boots-body");
  box.replaceChildren();
  if (!bs.length) { box.textContent = "還沒有"; return; }
  for (const b of bs) {
    const p = b.previous;
    const head = `${b.bootTime} 開機（pid ${b.pid}）` + (b.why.length ? `：給她一個回合（${b.why.join("、")}）` : "：沒給回合");
    box.append(el("h3", b.why.length ? null : "muted", head));
    if (!p) { box.append(el("div", "muted", "沒有上一次執行的 log")); continue; }
    box.append(el("div", null, `上一次：${p.started ? clock(p.started) + " 開機，" : "開頭已輪替掉，"}${endWords[p.ending.how] || p.ending.how}${p.ending.detail ? "（" + p.ending.detail + "）" : ""}，最後一行 ${p.lastLine ? clock(p.lastLine) : "—"}；${p.lines} 行${p.unreadable ? `，${p.unreadable} 行讀不懂` : ""}；build ${p.build || "—"}`));
    for (const k of p.kinds) {
      box.append(el("div", null, `${k.level} ×${k.count}（${k.first ? clock(k.first) : "—"} 到 ${k.last ? clock(k.last) : "—"}）：${k.example}`));
    }
    for (const u of b.unfinished) box.append(el("div", null, "沒做完：" + u));
    if (b.notice) box.append(el("pre", "muted", b.notice));
  }
}

const whoWords = { her: "她", owner: "主人", maintenance: "系統維護", program: "程式" };
function renderHistory(hs) {
  const box = $("history-body");
  const open = new Set([...box.querySelectorAll("details[open]")].map(d => d.dataset.key));
  box.replaceChildren();
  if (!hs.length) { box.textContent = "還沒有"; return; }
  for (const h of hs) {
    const e = h.entry;
    const sum = el("summary");
    sum.append(document.createTextNode(`${e.id.slice(0, 7)} ${whoWords[e.who] || e.who}${e.task ? "（" + e.task + "）" : ""}：${e.what}${!e.why && e.args && !h.secret ? "（" + e.args.slice(0, 60) + "）" : ""} `));
    sum.append(el("span", "meta", clock(e.ts) + (h.secret ? " · 有秘密記憶" : "")));
    const body = el("div");
    if (e.why) body.append(el("div", null, "為什麼：" + e.why));
    if (e.args) body.append(el("div", "meta", "參數：" + e.args));
    body.append(el("div", "meta", "動到：" + h.things));
    for (const u of e.unrecordedBefore) body.append(el("div", "meta", "之前沒記上的改動也在這筆裡：" + u));
    const diff = el("pre", "muted");
    body.append(btn("看改了什麼", async () => {
      const r = await fetch("/api/history/diff/" + e.id, { cache: "no-store" });
      diff.textContent = await r.text();
    }), " ", btn("退回這個改動", () => {
      if (confirm(`退回「${e.what}」？（會記成一筆新的改動，她下一回合會知道）`)) post("/api/history/undo/" + e.id);
    }), " ", btn("全部回到這筆之後的樣子", () => {
      if (confirm(`把她的東西全部回到 ${clock(e.ts)} 這筆之後的樣子？之後的改動都退回（會記成一筆新的改動）`)) post("/api/history/restore/" + e.id);
    }), diff);
    const d = details(sum, body, open.has(e.id));
    d.className = "mem";
    d.dataset.key = e.id;
    box.append(d);
  }
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
    const [s, t, v, m, c, dr, go, tl, bo, hi] = await Promise.all(["/api/status", "/api/timeline", "/api/vision", "/api/memory", "/api/calendar", "/api/drives", "/api/goals", "/api/tools", "/api/boots", "/api/history"].map(get));
    renderStatus(s); renderTimeline(t); renderVision(v); memory = m; renderMemory(m); renderCalendar(c); renderDrives(dr); renderGoals(go); renderTools(tl); renderBoots(bo); renderHistory(hi);
  } catch (e) {
    console.error(e);
  } finally {
    busy = false;
    if (dirty) { dirty = false; refresh(); }
  }
}

async function post(path) {
  const r = await fetch(path, { method: "POST" });
  if (!r.ok && r.status !== 409) alert(path + ": " + r.status);
  if (r.status === 409 && (path.startsWith("/api/memory/") || path.startsWith("/api/history/"))) alert(await r.text());
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
