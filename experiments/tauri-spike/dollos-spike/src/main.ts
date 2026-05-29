// DollOS Tauri spike: canvas character renderer + idle animation + WS echo loop.
//
// Notes:
// - Vanilla TS only, no UI framework (per spike scope).
// - Canvas 2D rather than WebGL/Cubism — same surface Cubism Web SDK will use
//   later. Render layer is a single drawFrame(t) called from rAF.
// - Character is abstracted as a CharacterRenderer interface so swapping
//   static-image -> Live2D later only touches one module.
//
// Eye-region coords (in placeholder doll.svg viewBox 400x600):
//   left eye  ~ (172, 278), right eye ~ (228, 278). When the character image
//   is scaled+translated onto the canvas, the same affine maps the blink
//   overlay rectangle, so eyes line up regardless of window size.

import dollUrl from "./assets/doll.svg";

// ───────────────────────── character renderer ─────────────────────────

interface CharacterRenderer {
  ready: Promise<void>;
  /** Draws at given canvas size and time (ms since start). */
  draw(ctx: CanvasRenderingContext2D, w: number, h: number, tMs: number): void;
}

class StaticImageCharacter implements CharacterRenderer {
  private img = new Image();
  ready: Promise<void>;
  // intrinsic coords from the SVG viewBox; blink overlay uses these.
  private readonly VB_W = 400;
  private readonly VB_H = 600;
  private readonly EYE_L = { x: 172, y: 278, rx: 14, ry: 18 };
  private readonly EYE_R = { x: 228, y: 278, rx: 14, ry: 18 };

  // blink state — simple finite-state randomiser
  private nextBlinkAt = 1500 + Math.random() * 2000;
  private blinkStart = -1;
  private readonly BLINK_DUR = 170; // ms

  constructor(src: string) {
    this.ready = new Promise((resolve, reject) => {
      this.img.onload = () => resolve();
      this.img.onerror = () => reject(new Error("failed to load character asset"));
      this.img.src = src;
    });
  }

  draw(ctx: CanvasRenderingContext2D, w: number, h: number, tMs: number) {
    // breathing: sin Y-offset, amplitude 6px, period ~3.5s
    const breath = Math.sin((tMs / 3500) * Math.PI * 2) * 6;

    const targetH = h * 0.6;
    const scale = targetH / this.VB_H;
    const targetW = this.VB_W * scale;
    const x = (w - targetW) / 2;
    const y = (h - targetH) / 2 + breath;

    ctx.clearRect(0, 0, w, h);

    // soft floor shadow so breathing reads
    ctx.save();
    ctx.globalAlpha = 0.25;
    ctx.fillStyle = "#000";
    ctx.beginPath();
    ctx.ellipse(w / 2, y + targetH - 4, targetW * 0.32, 10, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();

    ctx.drawImage(this.img, x, y, targetW, targetH);

    // blink: cover eye region with a skin-tone patch + lash line
    if (this.blinkStart < 0 && tMs >= this.nextBlinkAt) {
      this.blinkStart = tMs;
    }
    if (this.blinkStart >= 0) {
      const dt = tMs - this.blinkStart;
      if (dt > this.BLINK_DUR) {
        this.blinkStart = -1;
        // schedule next blink 3-5s out per spec
        this.nextBlinkAt = tMs + 3000 + Math.random() * 2000;
      } else {
        for (const e of [this.EYE_L, this.EYE_R]) {
          const ex = x + e.x * scale;
          const ey = y + e.y * scale;
          const erx = e.rx * scale;
          const ery = e.ry * scale;
          ctx.fillStyle = "#ffe4d2";
          ctx.beginPath();
          ctx.ellipse(ex, ey, erx + 1, ery + 1, 0, 0, Math.PI * 2);
          ctx.fill();
          ctx.strokeStyle = "#1c1c2a";
          ctx.lineWidth = Math.max(1.5, 2 * scale);
          ctx.lineCap = "round";
          ctx.beginPath();
          ctx.moveTo(ex - erx, ey);
          ctx.quadraticCurveTo(ex, ey + ery * 0.35, ex + erx, ey);
          ctx.stroke();
        }
      }
    }
  }
}

// ───────────────────────── canvas setup ─────────────────────────

const canvas = document.getElementById("stage") as HTMLCanvasElement;
const ctx = canvas.getContext("2d")!;
let dpr = window.devicePixelRatio || 1;

function resize() {
  dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(canvas.clientWidth * dpr);
  canvas.height = Math.floor(canvas.clientHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener("resize", resize);
resize();

const character: CharacterRenderer = new StaticImageCharacter(dollUrl);

const startMs = performance.now();
function frame() {
  const t = performance.now() - startMs;
  character.draw(ctx, canvas.clientWidth, canvas.clientHeight, t);
  requestAnimationFrame(frame);
}
character.ready
  .then(() => requestAnimationFrame(frame))
  .catch((e) => log("err", `character load failed: ${e.message}`));

// ───────────────────────── WS log + connection ─────────────────────────

const logEl = document.getElementById("log")!;
function log(kind: "in" | "out" | "sys" | "err", text: string) {
  const div = document.createElement("div");
  div.className = `entry ${kind}`;
  const ts = new Date().toLocaleTimeString();
  div.textContent = `[${ts}] ${kind.toUpperCase()} ${text}`;
  logEl.appendChild(div);
  while (logEl.childElementCount > 50) logEl.firstChild?.remove();
  logEl.scrollTop = logEl.scrollHeight;
}

const WS_URL = "ws://localhost:9876";
let ws: WebSocket | null = null;

function connect() {
  log("sys", `connecting ${WS_URL}`);
  try {
    ws = new WebSocket(WS_URL);
  } catch (e: any) {
    log("err", `ctor failed: ${e?.message ?? e}`);
    setTimeout(connect, 2500);
    return;
  }
  ws.addEventListener("open", () => log("sys", "open"));
  ws.addEventListener("message", (ev) => log("in", String(ev.data)));
  ws.addEventListener("error", () => log("err", "ws error"));
  ws.addEventListener("close", (ev) => {
    log("sys", `close code=${ev.code}`);
    ws = null;
    setTimeout(connect, 2500);
  });
}
connect();

const btn = document.getElementById("ping-btn") as HTMLButtonElement;
btn.addEventListener("click", () => {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    log("err", "ws not open");
    return;
  }
  const msg = JSON.stringify({ type: "ping", ts: Date.now() });
  ws.send(msg);
  log("out", msg);
});
