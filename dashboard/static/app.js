/* TradeCommand SPA — vanilla JS, no build step. */
"use strict";

/* ============================== utils ============================== */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => [...(r || document).querySelectorAll(s)];

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const k of kids.flat()) {
    if (k === null || k === undefined) continue;
    n.append(k.nodeType ? k : document.createTextNode(k));
  }
  return n;
}

const fmt = {
  usd(v, dp = 2) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US",
      { minimumFractionDigits: dp, maximumFractionDigits: dp });
  },
  num(v, dp = 2) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Number(v).toLocaleString("en-US", { maximumFractionDigits: dp });
  },
  pct(v, dp = 2, signed = true) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    const s = signed && v > 0 ? "+" : "";
    return s + Number(v).toFixed(dp) + "%";
  },
  sign(v) { return v > 0 ? "up" : v < 0 ? "dn" : "dim"; },
  ago(sec) {
    if (sec === null || sec === undefined) return "never";
    if (sec < 90) return Math.round(sec) + "s ago";
    if (sec < 5400) return Math.round(sec / 60) + "m ago";
    if (sec < 172800) return (sec / 3600).toFixed(1) + "h ago";
    return (sec / 86400).toFixed(1) + "d ago";
  },
  when(ts) {
    if (!ts) return "—";
    try {
      const d = new Date(ts);
      if (isNaN(+d)) return String(ts).slice(0, 16);
      return d.toLocaleString("en-US", { month: "short", day: "numeric",
        hour: "numeric", minute: "2-digit" });
    } catch { return String(ts).slice(0, 16); }
  },
};

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (Arm.token) headers["X-Arm-Token"] = Arm.token;
  const res = await fetch(path, { ...opts, headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  let j = null;
  try { j = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && j && j.arm_required) { Arm.clear(); throw new ApiErr("Not armed — enter PIN", 401, j); }
  if (!res.ok) throw new ApiErr((j && (j.error || j.detail)) || `HTTP ${res.status}`, res.status, j);
  return j;
}
class ApiErr extends Error { constructor(m, code, body) { super(m); this.code = code; this.body = body; } }

function toast(msg, kind = "") {
  const t = el("div", { class: `toast ${kind}` }, msg);
  $("#toastRoot").append(t);
  setTimeout(() => t.remove(), 4200);
}

/* ============================== arm manager ============================== */
const Arm = {
  token: null, expiresAt: 0, timer: null,
  get armed() { return this.token && Date.now() < this.expiresAt; },
  clear() { this.token = null; this.expiresAt = 0; this.renderChip(); },
  set(token, ttl) {
    this.token = token; this.expiresAt = Date.now() + ttl * 1000;
    clearInterval(this.timer);
    this.timer = setInterval(() => {
      if (!this.armed) { this.clear(); clearInterval(this.timer); }
      this.renderChip();
    }, 1000);
    this.renderChip();
  },
  renderChip() {
    const c = $("#armChip");
    if (this.armed) {
      const left = Math.max(0, Math.round((this.expiresAt - Date.now()) / 1000));
      c.textContent = `🔓 armed ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
      c.classList.add("armed");
    } else { c.textContent = "🔒 locked"; c.classList.remove("armed"); }
  },
  async ensure() {
    if (this.armed) return true;
    return await new Promise((resolve) => {
      const input = el("input", { type: "password", inputmode: "numeric",
        autocomplete: "current-password", placeholder: "PIN", style: "font-size:18px;letter-spacing:4px" });
      const err = el("div", { class: "errbox hidden" });
      const go = async () => {
        try {
          const r = await api("/api/arm", { method: "POST", body: { pin: input.value } });
          if (r.ok) { this.set(r.token, r.ttl); closeModal(); resolve(true); }
          else { err.textContent = r.error || "wrong PIN"; err.classList.remove("hidden"); }
        } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
      };
      input.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
      openModal(el("div", {},
        el("h2", {}, "🔐 Arm control actions"),
        el("div", { class: "dim", style: "font-size:12.5px;margin-bottom:8px" },
          "Money-moving and bot-control actions need your PIN. Arms this device for 5 minutes."),
        el("div", { class: "formrow" }, input),
        err,
        el("div", { class: "btnrow" },
          el("button", { class: "btn primary", onclick: go }, "Arm"),
          el("button", { class: "btn", onclick: () => { closeModal(); resolve(false); } }, "Cancel"))),
        () => resolve(false));
      setTimeout(() => input.focus(), 60);
    });
  },
};
$("#armChip").addEventListener("click", async () => {
  if (Arm.armed) { try { await api("/api/disarm", { method: "POST", body: {} }); } catch {} Arm.clear(); toast("Disarmed"); }
  else await Arm.ensure();
});

/* ============================== modal ============================== */
let _modalOnClose = null;
function openModal(content, onClose) {
  _modalOnClose = onClose || null;
  const ov = el("div", { class: "overlay", onclick: (e) => { if (e.target === ov) closeModal(); } },
    el("div", { class: "sheet" }, content));
  $("#modalRoot").replaceChildren(ov);
}
function closeModal() {
  $("#modalRoot").replaceChildren();
  if (_modalOnClose) { const f = _modalOnClose; _modalOnClose = null; f(); }
}

function holdButton(label, onFire, { cls = "", ms = 1200 } = {}) {
  const fill = el("i", { class: "fill" });
  const btn = el("button", { class: `holdbtn ${cls}` }, fill, el("span", {}, label));
  let t0 = null, raf = null, fired = false;
  const step = () => {
    if (t0 === null) return;
    const p = Math.min(1, (performance.now() - t0) / ms);
    fill.style.width = (p * 100) + "%";
    if (p >= 1 && !fired) { fired = true; cancel(); onFire(); return; }
    raf = requestAnimationFrame(step);
  };
  const start = (e) => { e.preventDefault(); if (btn.disabled) return; t0 = performance.now(); fired = false; raf = requestAnimationFrame(step); };
  const cancel = () => { t0 = null; cancelAnimationFrame(raf); fill.style.width = "0%"; };
  btn.addEventListener("pointerdown", start);
  btn.addEventListener("pointerup", cancel);
  btn.addEventListener("pointerleave", cancel);
  btn.addEventListener("pointercancel", cancel);
  return btn;
}

/* ============================== charts (SVG) ============================== */
function sparkline(points, w = 90, h = 26, cls = "") {
  if (!points || points.length < 2) return el("span", { class: "dim" }, "");
  const min = Math.min(...points), max = Math.max(...points);
  const span = (max - min) || 1;
  const step = w / (points.length - 1);
  const d = points.map((p, i) =>
    `${i ? "L" : "M"}${(i * step).toFixed(1)},${(h - 2 - ((p - min) / span) * (h - 4)).toFixed(1)}`).join("");
  const up = points[points.length - 1] >= points[0];
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`); svg.setAttribute("width", w); svg.setAttribute("height", h);
  svg.classList.add("sparkline"); if (cls) svg.classList.add(cls);
  svg.innerHTML = `<path d="${d}" fill="none" stroke="${up ? "var(--up)" : "var(--dn)"}" stroke-width="1.5"/>`;
  return svg;
}

/* series: [{name,color,points:[{x(ms),y}]}]; opts {h, money, pct} */
function lineChart(series, opts = {}) {
  const H = opts.h || 220, W = 760, padL = 54, padR = 8, padT = 10, padB = 22;
  const all = series.flatMap(s => s.points);
  if (!all.length) return el("div", { class: "dim", style: "padding:20px" }, opts.empty || "no data yet");
  const xs = all.map(p => p.x), ys = all.map(p => p.y);
  let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (x1 === x0) x1 = x0 + 1;
  const yPad = (y1 - y0) * 0.08 || Math.abs(y1) * 0.05 || 1;
  y0 -= yPad; y1 += yPad;
  const X = x => padL + (x - x0) / (x1 - x0) * (W - padL - padR);
  const Y = y => padT + (1 - (y - y0) / (y1 - y0)) * (H - padT - padB);
  let g = "";
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = y0 + (y1 - y0) * i / ticks, y = Y(v);
    const lbl = opts.pct ? v.toFixed(1) + "%" : (opts.money ? "$" + v.toFixed(v < 100 ? 1 : 0) : v.toFixed(1));
    g += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="var(--line)" stroke-width="0.6"/>`
       + `<text x="${padL - 5}" y="${y + 3}" text-anchor="end">${lbl}</text>`;
  }
  for (let i = 0; i <= 4; i++) {
    const t = x0 + (x1 - x0) * i / 4;
    const d = new Date(t);
    g += `<text x="${X(t)}" y="${H - 6}" text-anchor="middle">${(d.getMonth() + 1)}/${d.getDate()}</text>`;
  }
  for (const s of series) {
    if (!s.points.length) continue;
    const pts = [...s.points].sort((a, b) => a.x - b.x);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
    if (s.area) {
      const dd = d + `L${X(pts[pts.length - 1].x)},${Y(y0)}L${X(pts[0].x)},${Y(y0)}Z`;
      g += `<path d="${dd}" fill="${s.color}" opacity="0.15" stroke="none"/>`;
    }
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.w || 1.8}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""}/>`;
  }
  const svg = el("div", {},
    el("div", { class: "legend" }, series.map(s =>
      el("span", {}, el("i", { style: `background:${s.color}` }), s.name))));
  const c = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  c.setAttribute("viewBox", `0 0 ${W} ${H}`); c.classList.add("chart");
  c.innerHTML = g;
  svg.append(c);
  return svg;
}

function barChart(items, opts = {}) { // items: [{label, v}]
  const H = opts.h || 170, W = 760, padL = 46, padB = 30, padT = 8;
  if (!items.length) return el("div", { class: "dim", style: "padding:20px" }, "no data yet");
  const vals = items.map(i => i.v);
  let y1 = Math.max(0, ...vals), y0 = Math.min(0, ...vals);
  if (y1 === y0) y1 = y0 + 1;
  const Y = v => padT + (1 - (v - y0) / (y1 - y0)) * (H - padT - padB);
  const bw = Math.min(34, (W - padL - 10) / items.length - 4);
  let g = `<line x1="${padL}" y1="${Y(0)}" x2="${W - 6}" y2="${Y(0)}" stroke="var(--line)"/>`;
  items.forEach((it, i) => {
    const x = padL + 4 + i * ((W - padL - 10) / items.length);
    const y = Y(Math.max(0, it.v)), hh = Math.abs(Y(it.v) - Y(0));
    g += `<rect x="${x}" y="${y}" width="${bw}" height="${Math.max(1, hh)}" rx="2"
      fill="${it.v >= 0 ? "var(--up)" : "var(--dn)"}" opacity="0.85"><title>${it.label}: ${it.v}</title></rect>`;
    if (items.length <= 20)
      g += `<text x="${x + bw / 2}" y="${H - 14}" text-anchor="middle">${it.label}</text>`;
  });
  const c = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  c.setAttribute("viewBox", `0 0 ${W} ${H}`); c.classList.add("chart");
  c.innerHTML = g;
  return c;
}

/* ============================== markdown-lite ============================== */
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function mdLite(text) {
  const out = [];
  let inCode = false, code = [], inList = false;
  const flushList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  for (const raw of String(text || "").split("\n")) {
    if (raw.startsWith("```")) {
      if (inCode) { out.push(`<pre>${esc(code.join("\n"))}</pre>`); code = []; inCode = false; }
      else { flushList(); inCode = true; }
      continue;
    }
    if (inCode) { code.push(raw); continue; }
    let line = esc(raw);
    line = line.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
               .replace(/`([^`]+)`/g, "<code>$1</code>");
    if (/^### /.test(line)) { flushList(); out.push(`<h3>${line.slice(4)}</h3>`); }
    else if (/^## /.test(line)) { flushList(); out.push(`<h2>${line.slice(3)}</h2>`); }
    else if (/^# /.test(line)) { flushList(); out.push(`<h1>${line.slice(2)}</h1>`); }
    else if (/^\s*[-*] /.test(line)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${line.replace(/^\s*[-*] /, "")}</li>`);
    } else if (line.trim() === "") flushList();
    else { flushList(); out.push(`<p>${line}</p>`); }
  }
  if (inCode && code.length) out.push(`<pre>${esc(code.join("\n"))}</pre>`);
  flushList();
  return el("div", { class: "md", html: out.join("\n") });
}

/* ============================== shared bits ============================== */
function emaBadge(state, transition) {
  const cls = state === "BUY" ? "buy" : state === "SELL" ? "sell" : "neutral";
  return el("span", { class: `badge ${cls}`, title: transition || "" }, state || "—");
}
function confMeter(c) {
  const color = c >= 75 ? "var(--up)" : c >= 60 ? "var(--warn)" : "var(--dn)";
  return el("div", { class: "conf-meter" },
    el("i", { style: `width:${Math.min(100, c || 0)}%;background:${color}` }));
}
function pnlCell(v, pct) {
  return el("td", { class: `num ${fmt.sign(v)}` },
    `${fmt.usd(v)}${pct !== undefined && pct !== null ? ` (${fmt.pct(pct)})` : ""}`);
}

/* ============================== actions ============================== */
async function armedApi(path, body) {
  if (!await Arm.ensure()) throw new ApiErr("cancelled", 0);
  return api(path, { method: "POST", body });
}

function orderTicket(prefill = {}) {
  let side = prefill.side || "buy";
  let useDollars = prefill.dollar_amount != null || !prefill.quantity;
  const sym = el("input", { value: prefill.symbol || "", placeholder: "SPY",
    style: "text-transform:uppercase;font-family:var(--mono);font-weight:700", maxlength: 6 });
  const qty = el("input", { type: "number", step: "any", inputmode: "decimal",
    value: prefill.quantity ?? "", placeholder: "shares" });
  const dollars = el("input", { type: "number", step: "any", inputmode: "decimal",
    value: prefill.dollar_amount ?? "", placeholder: "USD notional" });
  const typeSel = el("select", {},
    ["market", "limit", "stop_market", "stop_limit"].map(t => el("option", { value: t }, t)));
  typeSel.value = prefill.type || "market";
  const limit = el("input", { type: "number", step: "any", placeholder: "limit price" });
  const stopP = el("input", { type: "number", step: "any", placeholder: "stop price" });
  const tif = el("select", {}, el("option", { value: "gfd" }, "gfd (day)"), el("option", { value: "gtc" }, "gtc"));
  const msg = el("div", {});
  const segBuy = el("button", { class: side === "buy" ? "on buy" : "" }, "BUY");
  const segSell = el("button", { class: side === "sell" ? "on sell" : "" }, "SELL");
  segBuy.onclick = () => { side = "buy"; segBuy.className = "on buy"; segSell.className = ""; };
  segSell.onclick = () => { side = "sell"; segSell.className = "on sell"; segBuy.className = ""; };
  const modeSeg = el("div", { class: "seg" },
    el("button", { class: useDollars ? "on" : "", onclick: (e) => { useDollars = true; syncMode(e); } }, "$ amount"),
    el("button", { class: !useDollars ? "on" : "", onclick: (e) => { useDollars = false; syncMode(e); } }, "# shares"));
  function syncMode(e) {
    $$("button", modeSeg).forEach(b => b.className = "");
    e.target.className = "on";
    dollars.parentElement.classList.toggle("hidden", !useDollars);
    qty.parentElement.classList.toggle("hidden", useDollars);
  }
  function syncType() {
    limit.parentElement.classList.toggle("hidden", !typeSel.value.includes("limit"));
    stopP.parentElement.classList.toggle("hidden", !typeSel.value.startsWith("stop"));
  }
  typeSel.onchange = syncType;
  const previewBtn = el("button", { class: "btn primary", style: "width:100%", onclick: preview }, "Preview order");
  async function preview() {
    msg.replaceChildren();
    const body = { symbol: sym.value.trim().toUpperCase(), side, type: typeSel.value,
      time_in_force: tif.value };
    if (useDollars) body.dollar_amount = parseFloat(dollars.value);
    else body.quantity = parseFloat(qty.value);
    if (typeSel.value.includes("limit")) body.limit_price = parseFloat(limit.value);
    if (typeSel.value.startsWith("stop")) body.stop_price = parseFloat(stopP.value);
    previewBtn.disabled = true; previewBtn.textContent = "Previewing…";
    try {
      const r = await armedApi("/api/order/preview", body);
      renderPreview(r);
    } catch (e) { msg.replaceChildren(el("div", { class: "errbox" }, e.message)); }
    previewBtn.disabled = false; previewBtn.textContent = "Preview order";
  }
  function renderPreview(r) {
    const p = r.params;
    const lines = [
      `${p.side.toUpperCase()} ${p.symbol}  ·  ${p.type}${p.limit_price ? " @ $" + p.limit_price : ""}`,
      p.quantity ? `${p.quantity} shares` : `$${p.dollar_amount} notional`,
      r.quote && r.quote.price ? `last price   ${fmt.usd(r.quote.price)}` : null,
      r.est_cost ? `est. ${p.side === "buy" ? "cost" : "proceeds"}   ${fmt.usd(r.est_cost)}` : null,
      `tif ${p.time_in_force || "gfd"}`,
    ].filter(Boolean).join("\n");
    msg.replaceChildren(
      el("div", { class: "preview-kv" }, lines),
      ...(r.warnings || []).map(w => el("div", { class: "warnbox" }, "⚠ " + w)),
      r.broker_review && r.broker_review.ok === false
        ? el("div", { class: "warnbox" }, "broker review unavailable: " + (r.broker_review.error || ""))
        : el("details", { class: "raw" }, el("summary", {}, "broker pre-trade review"),
            el("pre", { style: "font-size:11px;white-space:pre-wrap" },
              JSON.stringify(r.broker_review, null, 1).slice(0, 1600))),
      holdButton(`HOLD to ${p.side.toUpperCase()} ${p.symbol}`, async () => {
        msg.replaceChildren(el("div", { class: "dim" }, "placing…"));
        try {
          const res = await armedApi("/api/order/place", { preview_id: r.preview_id });
          msg.replaceChildren(el("div", { class: "okbox" },
            `✓ order placed (${(res.result && res.result.ref_id) || "ok"}) — check Orders on Overview`));
          toast("Order placed", "ok");
          setTimeout(() => { closeModal(); refreshTab(true); }, 1200);
        } catch (e) { msg.replaceChildren(el("div", { class: "errbox" }, "✗ " + e.message)); }
      }, { cls: p.side === "buy" ? "buyside" : "" }));
  }
  openModal(el("div", {},
    el("h2", {}, "⚡ Order ticket ", el("span", { class: "pill" }, "real money")),
    el("div", { class: "formrow" }, el("label", {}, "symbol"), sym,
      el("div", { class: "seg" }, segBuy, segSell)),
    el("div", { class: "formrow" }, el("label", {}, "size"), modeSeg),
    el("div", { class: "formrow" + (useDollars ? "" : " hidden") }, el("label", {}, "$ amount"), dollars),
    el("div", { class: "formrow" + (useDollars ? " hidden" : "") }, el("label", {}, "shares"), qty),
    el("div", { class: "formrow" }, el("label", {}, "type"), typeSel, tif),
    el("div", { class: "formrow hidden" }, el("label", {}, "limit"), limit),
    el("div", { class: "formrow hidden" }, el("label", {}, "stop"), stopP),
    previewBtn, msg));
  syncType();
}

function closePositionTicket(pos) {
  const full = pos.shares;
  orderTicket({ symbol: pos.symbol, side: "sell", quantity: full, type: "market" });
}

async function botPause() {
  try { await armedApi("/api/bot/pause", { reason: "dashboard" }); toast("Bot paused — protective exits stay on", "ok"); refreshTab(true); }
  catch (e) { toast(e.message, "err"); }
}
async function botResume() {
  try { await armedApi("/api/bot/resume", {}); toast("Bot resumed", "ok"); refreshTab(true); }
  catch (e) { toast(e.message, "err"); }
}
function botHalt() {
  const reason = el("input", { placeholder: "why? (logged)" });
  const msg = el("div", {});
  openModal(el("div", { class: "dangerzone" },
    el("h2", {}, "🛑 FORCE KILL-SWITCH HALT"),
    el("div", { class: "warnbox" },
      "Writes the HALT file. The bot will NOT trade — including its own stop-loss enforcement — until you clear it. The independent watchdog keeps alerting. Positions are NOT auto-sold."),
    el("div", { class: "formrow" }, el("label", {}, "reason"), reason), msg,
    holdButton("HOLD to HALT the bot", async () => {
      try { await armedApi("/api/bot/halt", { reason: reason.value }); toast("HALT written", "ok"); closeModal(); refreshTab(true); }
      catch (e) { msg.replaceChildren(el("div", { class: "errbox" }, e.message)); }
    })));
}
function botClearHalt() {
  const inp = el("input", { placeholder: 'type CLEAR', style: "font-family:var(--mono)" });
  const msg = el("div", {});
  openModal(el("div", { class: "dangerzone" },
    el("h2", {}, "🟢 Clear HALT"),
    el("div", { class: "warnbox" }, "Re-arms the bot to trade on its next cycle. Make sure you understand why it halted first."),
    el("div", { class: "formrow" }, inp), msg,
    holdButton("HOLD to CLEAR halt", async () => {
      try {
        await armedApi("/api/bot/clear_halt", { confirm: inp.value.trim() });
        toast("HALT cleared", "ok"); closeModal(); refreshTab(true);
      } catch (e) { msg.replaceChildren(el("div", { class: "errbox" }, e.message)); }
    })));
}
async function toggleDnt(sym, blocked) {
  try { await armedApi("/api/dnt", { symbol: sym, blocked }); toast(`${sym} ${blocked ? "added to" : "removed from"} do-not-trade`, "ok"); refreshTab(true); }
  catch (e) { toast(e.message, "err"); }
}
function stopOverrideModal(row) {
  const stopP = el("input", { type: "number", step: "any", value: row.override?.stop_price ?? "", placeholder: row.stop_price ?? "absolute $" });
  const stopPct = el("input", { type: "number", step: "any", value: row.override?.stop_pct ?? "", placeholder: "0.10 = 10%" });
  const trailPct = el("input", { type: "number", step: "any", value: row.override?.trail_pct ?? "", placeholder: "0.25 = 25%" });
  const msg = el("div", {});
  const save = async (clear) => {
    const body = { symbol: row.symbol, clear };
    if (!clear) {
      if (stopP.value) body.stop_price = parseFloat(stopP.value);
      if (stopPct.value) body.stop_pct = parseFloat(stopPct.value);
      if (trailPct.value) body.trail_pct = parseFloat(trailPct.value);
    }
    try { await armedApi("/api/stop_override", body); toast("Override saved", "ok"); closeModal(); refreshTab(true); }
    catch (e) { msg.replaceChildren(el("div", { class: "errbox" }, e.message)); }
  };
  openModal(el("div", { class: "dangerzone" },
    el("h2", {}, `🎯 Stop override — ${row.symbol}`),
    el("div", { class: "dim", style: "font-size:12px" },
      `Current: stop ${fmt.usd(row.stop_price)} · trail ${fmt.usd(row.trail_price)} · price ${fmt.usd(row.price)}. The bot reads these next cycle; the watchdog snapshot follows.`),
    el("div", { class: "formrow" }, el("label", {}, "stop $"), stopP),
    el("div", { class: "formrow" }, el("label", {}, "stop %"), stopPct),
    el("div", { class: "formrow" }, el("label", {}, "trail %"), trailPct), msg,
    el("div", { class: "btnrow" },
      el("button", { class: "btn primary", onclick: () => save(false) }, "Save"),
      el("button", { class: "btn", onclick: () => save(true) }, "Clear override"),
      el("button", { class: "btn", onclick: closeModal }, "Cancel"))));
}

/* ============================== tabs ============================== */
const Tabs = {};
let currentTab = "overview";
const cache = {};

async function loadTab(name, force = false) {
  const paths = { overview: "/api/overview", thinking: "/api/thinking",
    market: "/api/market", performance: "/api/performance", trades: "/api/trades",
    risk: "/api/risk", news: "/api/news", strategy: "/api/strategy",
    shadow: "/api/shadow", console: "/api/actions" };
  if (!force && cache[name] && Date.now() - cache[name].at < 8000) return cache[name].data;
  const data = await api(paths[name]);
  cache[name] = { at: Date.now(), data };
  return data;
}

async function refreshTab(force = false) {
  const view = $("#view");
  try {
    const data = await loadTab(currentTab, force);
    view.replaceChildren(Tabs[currentTab](data));
    updateChrome();
  } catch (e) {
    view.replaceChildren(el("div", { class: "errbox", style: "margin:20px" },
      `Failed to load ${currentTab}: ${e.message}`));
  }
}

async function updateChrome() {
  try {
    const [meta, ov] = [await api("/api/meta"), cache.overview?.data];
    const mkt = $("#mktChip");
    mkt.textContent = meta.market_open ? "MKT OPEN" : "mkt closed";
    mkt.className = "chip " + (meta.market_open ? "ok" : "");
    const bc = $("#brokerChip");
    bc.textContent = "broker: " + meta.broker.mode;
    bc.className = "chip " + (meta.broker.mode === "direct-mcp" ? "ok" : "warn");
    bc.title = meta.broker.reason || "";
    if (ov) {
      const b = $("#botChip"); const st = ov.bot.state;
      b.textContent = "bot: " + st.toUpperCase() +
        (ov.bot.heartbeat.age_seconds != null ? ` · ${fmt.ago(ov.bot.heartbeat.age_seconds)}` : "");
      b.className = "chip " + (st === "running" ? "ok" : st === "halted" || st === "stale" ? "bad" : "warn");
    }
    const alerts = await api("/api/alerts");
    const urgent = alerts.alerts.filter(a => a.level !== "info");
    const strip = $("#alertStrip");
    if (urgent.length) {
      strip.classList.remove("hidden");
      strip.innerHTML = `<b>▲ ${urgent.length} alert${urgent.length > 1 ? "s" : ""}</b> — ${esc(urgent[0].title)}`;
      strip.onclick = () => switchTab("console");
    } else strip.classList.add("hidden");
  } catch { /* chrome update is best-effort */ }
}

function switchTab(name) {
  currentTab = name;
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $("#view").replaceChildren(el("div", { class: "loading" }, "loading…"));
  refreshTab(true);
}
$$("#tabs button").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));

/* ---------------- overview ---------------- */
Tabs.overview = (d) => {
  const a = d.account, ks = d.killswitch;
  const total = a.live_total ?? a.total;
  const dayTotal = (d.day_pnl_positions || 0) + (d.realized_today || 0);
  const botStateBadge = { running: ["ok", "● RUNNING"], paused: ["warn", "⏸ PAUSED"],
    halted: ["crit", "🛑 HALTED"], stale: ["crit", "⚠ STALE / DOWN"],
    unknown: ["warn", "? UNKNOWN"] }[d.bot.state] || ["warn", d.bot.state];
  const wrap = el("div", { class: "grid" });

  wrap.append(el("div", { class: "panel" },
    el("div", { class: "hero" },
      el("div", {},
        el("div", { class: "big" }, fmt.usd(total)),
        el("div", { class: "sub" },
          `broker ${fmt.usd(a.total)} · cash ${fmt.usd(a.cash)} (${a.cash_pct ?? "—"}%) · BP ${fmt.usd(a.buying_power)}`)),
      el("div", {},
        el("div", { class: `big ${fmt.sign(dayTotal)}`, style: "font-size:24px" },
          (dayTotal >= 0 ? "+" : "") + fmt.usd(dayTotal).replace("$-", "-$")),
        el("div", { class: "sub" }, `today · positions ${fmt.usd(d.day_pnl_positions)} · realized ${fmt.usd(d.realized_today)}`)),
      el("div", { style: "margin-left:auto;text-align:right" },
        el("span", { class: `badge ${botStateBadge[0]}`, style: "font-size:13px;padding:6px 12px" }, botStateBadge[1]),
        el("div", { class: "sub", style: "margin-top:4px" },
          `heartbeat ${fmt.ago(d.bot.heartbeat.age_seconds)}${d.bot.cycle.running_fresh ? " · CYCLE RUNNING" : ""}`))),
    el("div", { class: "sub dim", style: "margin-top:6px;font-family:var(--mono);font-size:11px" },
      a.updated
        ? `broker read ${fmt.when(a.updated)} via ${a.source || "—"}` + (a.error ? ` · ⚠ ${a.error}` : "")
        : "no broker read yet — tap ↻ Broker refresh for equity/cash/orders" + (a.error ? ` · ⚠ ${a.error}` : "")),
    el("div", { class: "btnrow" },
      el("button", { class: "btn primary", onclick: () => orderTicket() }, "⚡ New order"),
      el("button", { class: "btn", onclick: refreshBroker }, "↻ Broker refresh"),
      d.bot.pause.paused
        ? el("button", { class: "btn buy", onclick: botResume }, "▶ Resume bot")
        : el("button", { class: "btn", onclick: botPause }, "⏸ Pause bot"),
      d.bot.halt.halted
        ? el("button", { class: "btn buy", onclick: botClearHalt }, "🟢 Clear HALT")
        : el("button", { class: "btn dangerous", onclick: botHalt }, "🛑 Halt"))));

  // positions
  const tbl = el("table", {},
    el("thead", {}, el("tr", {},
      ...["sym", "px", "day", "P&L", "ribbon", "stop dist", ""].map(h =>
        el("th", { class: ["px", "day", "P&L"].includes(h) ? "num" : "" }, h)))),
    el("tbody", {}, d.positions.map(p => {
      const stopDist = p.price && p.stop.stop_price
        ? (p.price - p.stop.stop_price) / p.price * 100 : null;
      return el("tr", {},
        el("td", {},
          el("div", { class: "sym" }, p.symbol, p.dnt ? el("span", { class: "pill", title: "do-not-trade" }, " DNT") : "",
            p.locked ? " 🔒" : ""),
          el("div", { class: "subrow" },
            `${fmt.num(p.shares, 4)} sh @ ${fmt.usd(p.entry_price)} · conf ${p.confidence ?? "—"}`)),
        el("td", { class: "num" }, fmt.usd(p.price), el("div", { class: `subrow ${fmt.sign((p.price ?? 0) - (p.prev_close ?? 0))}` },
          p.prev_close ? fmt.pct((p.price / p.prev_close - 1) * 100) : "")),
        el("td", { class: `num ${fmt.sign(p.day_pnl)}` }, fmt.usd(p.day_pnl)),
        pnlCell(p.unrealized_pnl, p.unrealized_pct),
        el("td", {}, emaBadge(p.ema_state, p.ema_transition), " ", sparkline(p.spark, 74, 22)),
        el("td", { class: `num ${stopDist !== null && stopDist < 3 ? "dn" : "dim"}` },
          stopDist !== null ? fmt.pct(stopDist, 1, false) : "—",
          el("div", { class: "subrow" }, p.stop.stop_price ? `$${fmt.num(p.stop.stop_price)}` : "")),
        el("td", {},
          el("div", { class: "btnrow", style: "margin:0" },
            el("button", { class: "btn small sell", onclick: () => closePositionTicket(p) }, "Close"),
            el("button", { class: "btn small", onclick: () => orderTicket({ symbol: p.symbol, side: "buy" }) }, "Add"))));
    })));
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, `Open positions (${d.positions.length})`,
      el("span", { class: "right dim" }, `win rate ${fmt.pct((d.summary.win_rate || 0) * 100, 1, false)} · total P&L ${fmt.usd(d.summary.total_pnl)}`)),
    d.positions.length ? el("div", { class: "tablewrap" }, tbl)
      : el("div", { class: "dim" }, "no open positions")));

  // orders today + kill-switch + indices
  const cols = el("div", { class: "grid cols3" });
  cols.append(el("div", { class: "panel" },
    el("h3", {}, "Today's orders"),
    (d.orders_today || []).length
      ? el("div", { class: "tablewrap" }, el("table", {}, el("tbody", {},
          d.orders_today.map(o => el("tr", {},
            el("td", {}, el("span", { class: "sym" }, o.symbol || "—"),
              el("div", { class: "subrow" }, `${o.side || ""} ${o.type || ""} · ${o.state || ""}`)),
            el("td", { class: "num" }, o.quantity ? fmt.num(o.quantity, 4) + " sh" : fmt.usd(o.notional)),
            el("td", { class: "num" }, fmt.usd(o.price)),
            el("td", {}, ["new", "queued", "confirmed", "unconfirmed", "partially_filled"].includes(o.state)
              ? el("button", { class: "btn small dangerous", onclick: async () => {
                  try { await armedApi("/api/order/cancel", { order_id: o.id }); toast("Cancel sent", "ok"); refreshTab(true); }
                  catch (e) { toast(e.message, "err"); } } }, "Cancel")
              : ""))))))
      : el("div", { class: "dim" }, "none seen (refresh broker to update)")));
  const ddPct = ks.drawdown != null ? ks.drawdown * 100 : null;
  cols.append(el("div", { class: "panel" },
    el("h3", {}, "Kill-switch budget"),
    el("div", { class: "kv" }, el("span", { class: "k" }, "month peak"), el("span", { class: "v" }, fmt.usd(ks.peak))),
    el("div", { class: "kv" }, el("span", { class: "k" }, "current"), el("span", { class: "v" }, fmt.usd(ks.current))),
    el("div", { class: "kv" }, el("span", { class: "k" }, "drawdown"),
      el("span", { class: `v ${ddPct > 15 ? "dn" : ddPct > 8 ? "warn" : "up"}` }, fmt.pct(ddPct, 1, false))),
    el("div", { class: "kv" }, el("span", { class: "k" }, "halts at"), el("span", { class: "v" }, `${fmt.usd(ks.halt_at_value)} (−25%)`)),
    el("div", { class: "meter", style: "margin-top:6px" },
      el("i", { style: `width:${Math.min(100, (ddPct || 0) / 25 * 100)}%;background:${ddPct > 15 ? "var(--dn)" : ddPct > 8 ? "var(--warn)" : "var(--up)"}` }))));
  cols.append(el("div", { class: "panel" },
    el("h3", {}, "Indices"),
    ...(d.indices || []).map(ix => el("div", { class: "kv" },
      el("span", { class: "k" }, ix.label || ix.symbol),
      el("span", { class: `v ${fmt.sign(ix.change_pct)}` },
        `${fmt.num(ix.price)}  ${fmt.pct(ix.change_pct)}`)))));
  wrap.append(cols);
  return wrap;
};

async function refreshBroker() {
  toast("Refreshing broker …");
  try {
    await api("/api/broker/refresh", { method: "POST", body: {} });
    toast("Broker refreshed", "ok"); refreshTab(true);
  } catch (e) { toast(e.message, "err"); }
}

/* ---------------- thinking ---------------- */
Tabs.thinking = (d) => {
  const wrap = el("div", {});
  const meta = d.picks_file;
  wrap.append(el("div", { class: "section-title" },
    `Latest research — ${meta ? meta.file : "none found"}`));
  if (d.picks && d.picks.header && d.picks.header.length)
    wrap.append(el("div", { class: "panel", style: "margin-bottom:10px" },
      el("h3", {}, "Run header"),
      ...d.picks.header.slice(0, 8).map(h => el("div", { class: "kv" }, el("span", { class: "dim", html: esc(h).replace(/\*\*/g, "") })))));
  const grid = el("div", { class: "grid cols2" });
  for (const p of (d.picks.picks || [])) {
    const f = p.fields || {};
    grid.append(el("div", { class: "pickcard" },
      el("div", { class: "head" },
        el("span", { class: "sym", style: "font-size:16px" }, `#${p.rank} ${p.symbol}`),
        p.name ? el("span", { class: "dim", style: "font-size:12px" }, p.name) : null,
        emaBadge(p.ema_state), p.held ? el("span", { class: "badge buy" }, "HELD") : null,
        el("span", { class: "conf", style: "margin-left:auto" }, `${p.confidence ?? "—"}/100`)),
      confMeter(p.confidence),
      f.ema ? el("div", { class: "field" }, el("b", {}, "EMA: "), f.ema) : null,
      f.momentum ? el("div", { class: "field" }, el("b", {}, "Momentum: "), f.momentum) : null,
      f.targets ? el("div", { class: "field" }, el("b", {}, "Targets: "), f.targets) : null,
      f.allocation ? el("div", { class: "field" }, el("b", {}, "Allocation: "), f.allocation) : null,
      f.sources ? el("div", { class: "field" }, el("b", {}, "Sources: "), f.sources) : null,
      f.invalidates ? el("div", { class: "invalid" }, "⚠ Invalidates if: " + f.invalidates) : null,
      el("div", { class: "btnrow" },
        el("button", { class: "btn small buy", onclick: () => orderTicket({ symbol: p.symbol, side: "buy" }) }, "Buy"),
        p.price ? el("span", { class: "pill" }, `now ${fmt.usd(p.price)}`) : null)));
  }
  if (!(d.picks.picks || []).length)
    grid.append(el("div", { class: "panel dim" }, "no parsed picks in the latest research file"));
  wrap.append(grid);

  if ((d.momentum_watch && Object.keys(d.momentum_watch).length))
    wrap.append(el("div", { class: "section-title" }, "Momentum options watch (paper sleeve feed)"),
      el("div", { class: "grid cols2" },
        Object.entries(d.momentum_watch).map(([s, m]) => el("div", { class: "pickcard" },
          el("div", { class: "head" }, el("span", { class: "sym" }, s),
            el("span", { class: "conf", style: "margin-left:auto" }, `${m.confidence}/100`)),
          el("div", { class: "field" }, el("b", {}, "Catalyst: "), m.catalyst)))));

  const pending = (d.rewrite_queue || []).filter(q => !q.done);
  wrap.append(el("div", { class: "section-title" }, `Self-rewrite queue (${pending.length} pending)`));
  wrap.append(el("div", { class: "panel" },
    (d.rewrite_queue || []).length ? el("div", {}, (d.rewrite_queue || []).slice().reverse().slice(0, 12).map(q =>
      el("div", { class: "kv" },
        el("span", { class: q.done ? "dim" : "warn" }, q.done ? "✓ done" : "◌ pending"),
        el("span", { class: "v", style: "text-align:left;flex:1;margin-left:10px;white-space:normal" },
          q.trade_id ? `${q.trade_id} ${q.symbol} ${q.outcome} ${q.pnl}` : q.raw.slice(0, 90)))))
      : el("div", { class: "dim" }, "queue empty")));

  if (d.midweek_file)
    wrap.append(el("div", { class: "section-title" }, `Midweek review — ${d.midweek_file.file}`),
      el("div", { class: "panel" },
        (d.midweek.sections || []).slice(0, 6).map(s =>
          el("details", { open: s === d.midweek.sections[0] ? "" : null },
            el("summary", { style: "cursor:pointer;font-weight:600;margin:4px 0" }, s.title),
            mdLite(s.body)))));

  if (meta)
    wrap.append(el("div", { class: "panel", style: "margin-top:10px" },
      el("details", { class: "raw" }, el("summary", {}, "full raw research output"),
        mdLite(d.picks.raw))));
  return wrap;
};

/* ---------------- market ---------------- */
Tabs.market = (d) => {
  const wrap = el("div", { class: "grid" });
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "Indices"),
    el("div", { class: "grid cols3" },
      d.indices.map(ix => el("div", {},
        el("div", { class: "kv" },
          el("span", { class: "k" }, ix.label || ix.symbol),
          el("span", { class: `v ${fmt.sign(ix.change_pct)}` }, `${fmt.num(ix.price)} · ${fmt.pct(ix.change_pct)}`)),
        sparkline(ix.spark, 200, 30))))));
  const wlInput = el("input", { value: (d.watchlist_symbols || []).join(", "),
    style: "flex:1;font-family:var(--mono)" });
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "My watchlist"),
    el("div", { class: "formrow" }, wlInput,
      el("button", { class: "btn small", onclick: async () => {
        const symbols = wlInput.value.split(",").map(s => s.trim().toUpperCase()).filter(Boolean);
        try { await api("/api/watchlist", { method: "POST", body: { symbols } }); toast("Watchlist saved", "ok"); refreshTab(true); }
        catch (e) { toast(e.message, "err"); }
      } }, "Save")),
    el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["sym", "px", "chg", "ribbon", "", ""].map(h => el("th", {}, h)))),
      el("tbody", {}, (d.watchlist || []).map(r =>
        el("tr", {},
          el("td", {}, el("span", { class: "sym" }, r.symbol), r.held ? el("span", { class: "badge buy", style: "margin-left:6px" }, "held") : ""),
          el("td", { class: "num" }, fmt.usd(r.price)),
          el("td", { class: `num ${fmt.sign(r.change_pct)}` }, fmt.pct(r.change_pct)),
          el("td", {}, emaBadge(r.ema_state, r.ema_transition)),
          el("td", {}, sparkline(r.spark, 90, 24)),
          el("td", {}, el("button", { class: "btn small", onclick: () => orderTicket({ symbol: r.symbol, side: "buy" }) }, "Trade"))
        )
      ))
    ))));
  // earnings
  const e = d.earnings || {};
  let earnBody;
  if (e.unavailable) earnBody = el("div", { class: "dim" }, e.unavailable);
  else if (e.error) earnBody = el("div", { class: "warnbox" }, e.error);
  else {
    const items = e.earnings || e.results || e.calendar || (Array.isArray(e) ? e : []);
    const held = new Set(d.held || []);
    const rows = (items || []).slice(0, 40).map(x => {
      const sym = (x.symbol || x.ticker || "").toUpperCase();
      return el("tr", {},
        el("td", {}, el("span", { class: "sym" }, sym),
          held.has(sym) ? el("span", { class: "badge warn", style: "margin-left:6px" }, "HELD — blackout?") : ""),
        el("td", {}, x.report_date || x.date || "—"),
        el("td", {}, x.timing || x.time || ""),
        el("td", { class: "num" }, x.eps_estimate ?? x.estimated_eps ?? "—"));
    });
    earnBody = rows.length ? el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["sym", "date", "when", "est EPS"].map(h => el("th", {}, h)))),
      el("tbody", {}, rows))) : el("div", { class: "dim" }, "nothing in the window");
  }
  wrap.append(el("div", { class: "panel" }, el("h3", {}, "Earnings — next 14 days"), earnBody));
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "Headlines"),
    ...(d.general_news || []).slice(0, 14).map(n => el("div", { class: "kv" },
      el("a", { href: n.link || "#", target: "_blank", style: "white-space:normal" }, n.title),
      el("span", { class: "dim", style: "font-size:11px;white-space:nowrap" }, n.source)))));
  return wrap;
};

/* ---------------- performance ---------------- */
Tabs.performance = (d) => {
  const wrap = el("div", { class: "grid" });
  const curve = (d.equity_curve || []).map(p => ({ x: +new Date(p.ts), y: p.total }));
  const series = [];
  if (curve.length > 1) {
    series.push({ name: "account equity", color: "var(--acc)", points: curve, area: true });
    const spy = (d.benchmark_spy || []).map(p => ({ x: +new Date(p.ts), y: p.close }));
    if (spy.length && curve.length) {
      const t0 = curve[0].x;
      const spyWin = spy.filter(p => p.x >= t0 - 86400000);
      if (spyWin.length) {
        const k = curve[0].y / spyWin[0].y;
        series.push({ name: "SPY (scaled)", color: "var(--dim)", dash: "4 3",
          points: spyWin.map(p => ({ x: p.x, y: p.y * k })) });
      }
    }
    const rx3 = (d.rx3_curve || []).map(p => ({ x: +new Date(p.ts), y: p.value }));
    if (rx3.length > 1 && curve.length) {
      const k = curve[0].y / rx3[0].y;
      series.push({ name: "RX-3 paper (scaled)", color: "var(--pur)",
        points: rx3.map(p => ({ x: p.x, y: p.y * k })) });
    }
  }
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "Equity curve",
      el("span", { class: "right dim" }, curve.length ? `${curve.length} snapshots` : "recording starts now — every broker read appends a point")),
    lineChart(series, { money: true, empty: "No equity snapshots yet. They accrue automatically from broker reads (logs/equity_curve.jsonl)." })));

  const realized = (d.realized || []).map(p => ({ x: +new Date(p.ts), y: p.cum_pnl }));
  wrap.append(el("div", { class: "grid cols2" },
    el("div", { class: "panel" },
      el("h3", {}, "Cumulative realized P&L (all closed trades)"),
      lineChart([{ name: "realized P&L", color: "var(--up)", points: realized, area: true }], { money: true })),
    el("div", { class: "panel" },
      el("h3", {}, "Drawdown (recorded equity)"),
      lineChart([{ name: "drawdown %", color: "var(--dn)", area: true,
        points: (d.drawdown || []).map(p => ({ x: +new Date(p.ts), y: p.dd })) }], { pct: true }))));

  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "P&L by trade"),
    barChart((d.per_trade || []).slice(-24).map(t => ({ label: t.symbol, v: t.pnl || 0 })))));

  const s = d.summary || {}, pr = d.progress || {};
  wrap.append(el("div", { class: "grid cols3" },
    el("div", { class: "panel" }, el("h3", {}, "Scoreboard"),
      kv("trades", s.total_trades), kv("wins / losses", `${s.wins ?? "—"} / ${s.losses ?? "—"}`),
      kv("win rate", fmt.pct((s.win_rate || 0) * 100, 1, false)),
      kv("total P&L", fmt.usd(s.total_pnl), fmt.sign(s.total_pnl))),
    el("div", { class: "panel" }, el("h3", {}, "Month vs goal"),
      kv("month start", fmt.usd(pr.month_start_value ?? s.month_start_value)),
      kv("current", fmt.usd(pr.current_value ?? s.current_value)),
      kv("return", (pr.current_return ?? s.monthly_return_pct ?? "—") + (pr.current_return ? "" : "%")),
      kv("goal", pr.monthly_goal || s.monthly_goal || "—"),
      kv("on track", String(pr.on_track ?? s.on_track ?? "—"))),
    el("div", { class: "panel" }, el("h3", {}, "Exit reasons"),
      ...Object.entries(d.exit_reasons || {}).map(([k, v]) => kv(k, v)))));
  function kv(k, v, cls = "") {
    return el("div", { class: "kv" }, el("span", { class: "k" }, k), el("span", { class: `v ${cls}` }, String(v ?? "—")));
  }
  if (d.rx3_meta && Object.keys(d.rx3_meta).length)
    wrap.append(el("div", { class: "panel" }, el("h3", {}, "RX-3 paper meta"),
      el("pre", { style: "font:11.5px var(--mono);white-space:pre-wrap" }, JSON.stringify(d.rx3_meta, null, 1))));
  return wrap;
};

/* ---------------- trades ---------------- */
Tabs.trades = (d) => {
  const wrap = el("div", {});
  let sortKey = "exit_date", sortDir = -1, filter = "";
  const fInput = el("input", { placeholder: "filter symbol / reason / outcome…", style: "flex:1" });
  const body = el("div", {});
  const render = () => {
    let rows = d.trades.filter(t => {
      const hay = `${t.symbol} ${t.outcome} ${t.exit_reason || ""} ${t.id}`.toLowerCase();
      return hay.includes(filter.toLowerCase());
    });
    rows.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      return (av > bv ? 1 : av < bv ? -1 : 0) * sortDir;
    });
    const mkTh = (label, key, num) => el("th", { class: num ? "num" : "", onclick: () => {
      if (sortKey === key) sortDir *= -1; else { sortKey = key; sortDir = -1; }
      render();
    } }, label + (sortKey === key ? (sortDir > 0 ? " ↑" : " ↓") : ""));
    body.replaceChildren(el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {},
        mkTh("id", "id"), mkTh("sym", "symbol"), mkTh("entry", "entry_price", 1),
        mkTh("exit", "exit_price", 1), mkTh("P&L", "pnl_dollar", 1),
        mkTh("%", "pnl_pct", 1), mkTh("exit reason", "exit_reason"),
        mkTh("closed", "exit_date"), el("th", {}, "analysis"))),
      el("tbody", {}, rows.map(t => el("tr", {},
        el("td", { class: "dim mono" }, t.id),
        el("td", {}, el("span", { class: "sym" }, t.symbol),
          t.stop_loss ? el("span", { class: "badge crit", style: "margin-left:5px" }, "STOP") : ""),
        el("td", { class: "num" }, fmt.usd(t.entry_price)),
        el("td", { class: "num" }, fmt.usd(t.exit_price)),
        pnlCell(t.pnl_dollar), el("td", { class: `num ${fmt.sign(t.pnl_pct)}` }, fmt.pct(t.pnl_pct)),
        el("td", { class: "dim" }, t.exit_reason || (t.stop_loss ? "stop_loss" : "—")),
        el("td", { class: "dim mono", style: "font-size:11px" }, String(t.exit_date || "").slice(0, 16)),
        el("td", {}, t.analysis_file
          ? el("button", { class: "btn small", onclick: () => showPostmortem(t.analysis_file) },
              t.outcome === "WIN" ? "🏆 victory" : "🔬 postmortem")
          : el("span", { class: "dim" }, "—"))))))));
  };
  fInput.addEventListener("input", () => { filter = fInput.value; render(); });
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, `Closed trades (${d.trades.length})`),
    el("div", { class: "formrow" }, fInput), body));
  render();
  wrap.append(el("div", { class: "panel", style: "margin-top:10px" },
    el("h3", {}, "All analyses"),
    el("div", { class: "grid cols3" }, d.postmortems.map(p =>
      el("button", { class: "btn", style: "text-align:left", onclick: () => showPostmortem(p.file) },
        `${p.kind === "victory" ? "🏆" : "🔬"} ${p.file}`,
        el("div", { class: "subrow" }, p.title.slice(0, 70)))))));
  return wrap;
};

async function showPostmortem(file) {
  try {
    const r = await api("/api/postmortem?file=" + encodeURIComponent(file));
    openModal(el("div", {}, el("h2", {}, file), mdLite(r.text || "(empty)")));
  } catch (e) { toast(e.message, "err"); }
}

/* ---------------- risk ---------------- */
Tabs.risk = (d) => {
  const wrap = el("div", { class: "grid" });
  const ks = d.killswitch, ddPct = ks.drawdown != null ? ks.drawdown * 100 : null;
  wrap.append(el("div", { class: d.bot.halt.halted ? "dangerzone" : "panel" },
    el("h3", {}, "Kill-switch (monthly −25% drawdown)"),
    el("div", { class: "grid cols3" },
      el("div", {}, el("div", { class: "kv" }, el("span", { class: "k" }, "month peak"), el("span", { class: "v" }, fmt.usd(ks.peak))),
        el("div", { class: "kv" }, el("span", { class: "k" }, "current"), el("span", { class: "v" }, fmt.usd(ks.current))),
        el("div", { class: "kv" }, el("span", { class: "k" }, "halts at"), el("span", { class: "v" }, fmt.usd(ks.halt_at_value)))),
      el("div", {}, el("div", { class: "kv" }, el("span", { class: "k" }, "drawdown"),
          el("span", { class: `v ${ddPct > 15 ? "dn" : "warn"}` }, fmt.pct(ddPct, 1, false))),
        el("div", { class: "kv" }, el("span", { class: "k" }, "headroom"),
          el("span", { class: "v" }, ddPct != null ? fmt.pct(25 - ddPct, 1, false) : "—")),
        el("div", { class: "meter", style: "margin-top:8px" },
          el("i", { style: `width:${Math.min(100, (ddPct || 0) / 25 * 100)}%;background:${ddPct > 15 ? "var(--dn)" : "var(--warn)"}` }))),
      el("div", {},
        d.bot.halt.halted
          ? el("div", {}, el("div", { class: "errbox" }, "HALTED — bot will not trade (its stop enforcement is OFF; watchdog alerts only)"),
              el("button", { class: "btn buy", onclick: botClearHalt }, "🟢 Clear HALT"))
          : el("button", { class: "btn dangerous", onclick: botHalt }, "🛑 Force HALT now")))));

  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "Per-position stops",
      el("span", { class: "right dim" },
        `hard ${((d.risk_management.stop_loss_pct || 0.1) * 100).toFixed(0)}% · trail ${((d.risk_management.trailing_stop_pct || 0) * 100).toFixed(0)}% · ribbon-exit ${d.risk_management.exit_on_ribbon_sell ? "ON" : "off (advisory)"}`)),
    el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["sym", "px", "stop", "dist", "trail", "dist", "override", ""].map(h => el("th", {}, h)))),
      el("tbody", {}, (d.positions || []).map(r => el("tr", {},
        el("td", {}, el("span", { class: "sym" }, r.symbol),
          el("div", { class: "subrow" }, `entry ${fmt.usd(r.entry)} · peak ${fmt.usd(r.peak)}`)),
        el("td", { class: "num" }, fmt.usd(r.price)),
        el("td", { class: "num" }, fmt.usd(r.stop_price)),
        el("td", { class: `num ${r.dist_stop_pct != null && r.dist_stop_pct < 3 ? "dn" : "dim"}` }, fmt.pct(r.dist_stop_pct, 1, false)),
        el("td", { class: "num" }, fmt.usd(r.trail_price)),
        el("td", { class: `num ${r.dist_trail_pct != null && r.dist_trail_pct < 3 ? "warn" : "dim"}` }, fmt.pct(r.dist_trail_pct, 1, false)),
        el("td", {}, r.override ? el("span", { class: "badge warn" }, "OVERRIDE") : el("span", { class: "dim" }, "—")),
        el("td", {}, el("button", { class: "btn small", onclick: () => stopOverrideModal(r) }, "Edit"))))))),
    el("div", { class: "dim", style: "font-size:11.5px;margin-top:6px" },
      "Overrides are honored by the bot next cycle and mirrored into the watchdog's stops.json. Leveraged ETFs already get a tighter effective stop.")));

  // DNT
  const dntInput = el("input", { placeholder: "SYMBOL", style: "text-transform:uppercase;max-width:110px" });
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "Do-not-trade list (blocks bot BUYs; sells still allowed)"),
    el("div", { class: "formrow" }, dntInput,
      el("button", { class: "btn small", onclick: () => dntInput.value.trim() && toggleDnt(dntInput.value.trim().toUpperCase(), true) }, "Block")),
    el("div", { class: "btnrow" }, (d.dnt || []).length
      ? d.dnt.map(s => el("button", { class: "btn small", onclick: () => toggleDnt(s, false), title: "click to unblock" }, `${s} ✕`))
      : el("span", { class: "dim" }, "empty"))));

  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "Watchdog (independent, launchd)"),
    el("div", { class: "dim", style: "font-size:12px;margin-bottom:6px" },
      `stops.json updated ${d.stops_file.updated || "—"}`),
    el("pre", { style: "font:11px var(--mono);white-space:pre-wrap;max-height:220px;overflow:auto" },
      (d.watchdog_log || []).join("\n") || "(no watchdog log lines yet)")));
  return wrap;
};

/* ---------------- news ---------------- */
Tabs.news = (d) => {
  const wrap = el("div", { class: "grid" });
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "Source accuracy leaderboard (bot-tracked, drives its weights)"),
    el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["source", "weight", "wins", "losses", "accuracy", ""].map(h => el("th", {}, h)))),
      el("tbody", {}, (d.leaderboard || []).map(r => el("tr", {},
        el("td", { class: "sym" }, r.source),
        el("td", { class: "num" }, r.weight != null ? (r.weight * 100).toFixed(1) + "%" : "—"),
        el("td", { class: "num up" }, r.wins), el("td", { class: "num dn" }, r.losses),
        el("td", { class: "num" }, r.n ? fmt.pct((r.accuracy || 0) * 100, 0, false) : "—"),
        el("td", { style: "width:40%" }, el("div", { class: "meter" },
          el("i", { style: `width:${(r.accuracy || 0) * 100}%;background:${(r.accuracy || 0) >= .5 ? "var(--up)" : "var(--dn)"}` }))))))))));
  if ((d.verdicts || []).length)
    wrap.append(el("div", { class: "panel" },
      el("h3", {}, "Per-trade source verdicts (from postmortems)"),
      ...(d.verdicts.slice(0, 12).map(v => el("div", { class: "kv" },
        el("span", { class: "k mono" }, `${v.file} ${v.trade_ids.join(",")}`),
        el("span", { class: "v", style: "white-space:normal" },
          Object.entries(v.sources).map(([s, ok]) =>
            el("span", { class: `badge ${ok ? "buy" : "sell"}`, style: "margin-left:4px" }, `${s} ${ok ? "✓" : "✗"}`))))))));
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "What the bot cited (research + reviews)"),
    ...(d.citations || []).slice(0, 30).map(c => el("div", { class: "alert", style: "border-left-color:var(--acc)" },
      el("div", { class: "t" }, `${c.symbol || "—"} ${c.confidence ? `· conf ${c.confidence}` : ""} `,
        el("span", { class: "dim", style: "font-weight:400" }, `(${c.when || c.file})`)),
      c.sources ? el("div", { class: "d" }, "sources: " + c.sources) : null,
      c.note ? el("div", { class: "d" }, c.note) : null,
      c.invalidates ? el("div", { class: "d warn" }, "invalidates if: " + c.invalidates) : null))));
  for (const [sym, items] of Object.entries(d.symbol_news || {}))
    wrap.append(el("div", { class: "panel" },
      el("h3", {}, `Live headlines — ${sym}`),
      ...(items || []).slice(0, 6).map(n => el("div", { class: "kv" },
        el("a", { href: n.link || "#", target: "_blank", style: "white-space:normal" }, n.title)))));
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "General market news"),
    ...(d.general || []).slice(0, 16).map(n => el("div", { class: "kv" },
      el("a", { href: n.link || "#", target: "_blank", style: "white-space:normal" }, n.title),
      el("span", { class: "dim", style: "font-size:11px" }, n.source)))));
  return wrap;
};

/* ---------------- strategy ---------------- */
Tabs.strategy = (d) => {
  const wrap = el("div", { class: "grid" });
  const cfg = d.config || {};
  const rm = cfg.risk_management || {};
  wrap.append(el("div", { class: "grid cols3" },
    el("div", { class: "panel" }, el("h3", {}, `Live config — v${d.version}`),
      kv("goal", cfg.goal), kv("stop loss", fmt.pct((rm.stop_loss_pct || 0) * 100, 0, false)),
      kv("trailing stop", fmt.pct((rm.trailing_stop_pct || 0) * 100, 0, false)),
      kv("ribbon-sell exit", rm.exit_on_ribbon_sell ? "ON" : "off (advisory)"),
      kv("scale into winners", String(!!rm.scale_into_winners)),
      kv("monthly halt DD", fmt.pct((rm.monthly_halt_dd || 0) * 100, 0, false)),
      kv("min confidence", (cfg.research || {}).min_confidence_to_trade)),
    el("div", { class: "panel" }, el("h3", {}, "Capital bands (ceilings)"),
      ...Object.entries(cfg.capital_allocation || {}).filter(([k]) => !k.startsWith("_"))
        .map(([k, v]) => kv(k.replace(/_/g, " "), typeof v === "number" ? (v * 100).toFixed(0) + "%" : v))),
    el("div", { class: "panel" }, el("h3", {}, "Source weights"),
      ...Object.entries((cfg.research || {}).source_weights || {}).map(([k, v]) =>
        el("div", {}, el("div", { class: "kv" }, el("span", { class: "k" }, k),
          el("span", { class: "v" }, (v * 100).toFixed(1) + "%")),
          el("div", { class: "meter" }, el("i", { style: `width:${v * 250}%;background:var(--acc)` })))))));

  if ((cfg.learned_rules || []).length)
    wrap.append(el("div", { class: "panel" },
      el("h3", {}, "Learned rules"),
      ...cfg.learned_rules.map(r => el("div", { class: "alert", style: "border-left-color:var(--acc)" },
        el("div", { class: "t" }, `${r.id} (${r.added})`),
        el("div", { class: "d" }, r.rule)))));

  // merged timeline: change_events + version_history
  const events = [];
  for (const e of d.change_events || [])
    events.push({ when: e.timestamp, title: `${e.kind}: ${e.target} v${e.version}`,
      detail: e.summary, severity: e.severity, trades: e.trade_ids });
  for (const v of d.version_history || [])
    events.push({ when: v.date, title: `strategy v${v.version ?? "?"}`,
      detail: typeof v.summary === "string" ? v.summary : JSON.stringify(v.summary),
      severity: v.severity, trades: v.trade_ids, vh: true });
  events.sort((a, b) => String(b.when || "").localeCompare(String(a.when || "")));
  const seen = new Set();
  const dedup = events.filter(e => {
    const k = `${String(e.when).slice(0, 16)}|${e.title}`;
    if (seen.has(k)) return false; seen.add(k); return true;
  });
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, `Version timeline (${dedup.length} events)`,
      el("span", { class: "right dim" }, `skills: ${Object.entries(d.skill_versions || {}).map(([k, v]) => `${k.replace("skill_", "s")}v${v.latest}`).join(" · ")}`)),
    el("div", { class: "tl" }, dedup.slice(0, 60).map(e =>
      el("div", { class: `ev ${e.severity === "MAJOR" ? "major" : ""}` },
        el("div", { class: "when" }, `${String(e.when || "").slice(0, 16)} ${e.severity ? "· " + e.severity : ""} ${(e.trades || []).join(",")}`),
        el("div", { style: "font-weight:600;font-size:13px" }, e.title),
        el("div", { class: "dim", style: "font-size:12.5px;white-space:normal" }, (e.detail || "").slice(0, 400)))))));

  wrap.append(el("div", { class: "panel" },
    el("details", { class: "raw" }, el("summary", {}, "full strategy.json (view)"),
      el("pre", { style: "font:11px var(--mono);white-space:pre-wrap;max-height:400px;overflow:auto" },
        JSON.stringify(cfg, null, 1)))));
  function kv(k, v) { return el("div", { class: "kv" }, el("span", { class: "k" }, k), el("span", { class: "v" }, String(v ?? "—"))); }
  return wrap;
};

/* ---------------- shadow ---------------- */
Tabs.shadow = (d) => {
  const wrap = el("div", { class: "grid" });
  const gl = d.go_live || {};
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "Options go-live checklist",
      el("span", { class: "right dim" }, `enabled=${gl.enabled} · shadow=${gl.shadow_mode}`)),
    ...(gl.gates || []).map(g => el("div", { class: "kv" },
      el("span", { class: "k", style: "white-space:normal" }, (g.met ? "✅ " : "⬜ ") + g.gate),
      el("span", { class: "v dim" },
        typeof g.current === "object" ? JSON.stringify(g.current) :
        g.current != null ? fmt.usd(g.current) : "—")))));
  const sh = d.options_shadow || {};
  const summ = sh.summary || {};
  wrap.append(el("div", { class: "grid cols3" },
    el("div", { class: "panel" }, el("h3", {}, "Shadow record"),
      el("div", { class: "kv" }, el("span", { class: "k" }, "open / closed"), el("span", { class: "v" }, `${summ.open ?? 0} / ${summ.closed ?? 0}`)),
      el("div", { class: "kv" }, el("span", { class: "k" }, "win rate"), el("span", { class: "v" }, fmt.pct((summ.win_rate || 0) * 100, 0, false))),
      el("div", { class: "kv" }, el("span", { class: "k" }, "avg % on premium"),
        el("span", { class: `v ${fmt.sign(summ.avg_pct_on_premium)}` }, fmt.pct(summ.avg_pct_on_premium))),
      el("div", { class: "kv" }, el("span", { class: "k" }, "total P&L / contract"),
        el("span", { class: `v ${fmt.sign(summ.total_pnl_per_contract_usd)}` }, fmt.usd(summ.total_pnl_per_contract_usd)))),
    el("div", { class: "panel", style: "grid-column:span 2" },
      el("h3", {}, "RX-3 paper (deterministic rotation)"),
      Object.keys(d.rx3 || {}).length
        ? el("pre", { style: "font:11.5px var(--mono);white-space:pre-wrap;max-height:200px;overflow:auto" },
            JSON.stringify(d.rx3, null, 1).slice(0, 3000))
        : el("div", { class: "dim" }, "no paper days recorded yet — starts on the next market-day cycle"))));
  const positions = (sh.positions || []).slice().reverse();
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, `Shadow option positions (${positions.length})`),
    el("div", { class: "grid cols2" }, positions.map(p => {
      const isOpen = p.status === "open";
      return el("div", { class: "pickcard" },
        el("div", { class: "head" },
          el("span", { class: "sym" }, `${p.underlying} $${p.strike} ${p.type}`),
          el("span", { class: "pill" }, p.expiry),
          el("span", { class: `badge ${isOpen ? "neutral" : (p.pnl_pct_on_premium || 0) >= 0 ? "buy" : "sell"}`, style: "margin-left:auto" },
            isOpen ? "OPEN" : fmt.pct(p.pnl_pct_on_premium))),
        el("div", { class: "field" }, el("b", {}, "Signal: "),
          (p.signal_source || "ema_ribbon") + (p.catalyst ? ` — ${p.catalyst}` : "")),
        el("div", { class: "field" }, el("b", {}, "Premium: "),
          `in ${fmt.usd(p.entry_premium)}${p.exit_premium ? ` → out ${fmt.usd(p.exit_premium)}` : ""}`),
        el("div", { class: "field" }, el("b", {}, "Underlying: "),
          `entry ${fmt.usd(p.entry_underlying)}${p.underlying_price_now ? ` → now ${fmt.usd(p.underlying_price_now)}` : ""}`),
        p.exit_reason ? el("div", { class: "field dim" }, `exit: ${p.exit_reason}`) : null,
        p.oversized_for_account ? el("div", { class: "invalid" }, `⚠ oversized for account (${fmt.pct(p.pct_of_account, 1, false)} — why it's paper-only)`) : null);
    }))));
  if (Object.keys(d.momentum_watch || {}).length)
    wrap.append(el("div", { class: "panel" },
      el("h3", {}, "Today's momentum watch (research-fed)"),
      ...Object.entries(d.momentum_watch).map(([s, m]) => el("div", { class: "kv" },
        el("span", { class: "k sym" }, s),
        el("span", { class: "v", style: "white-space:normal" }, `conf ${m.confidence} — ${m.catalyst}`)))));
  return wrap;
};

/* ---------------- console (alerts + manual actions) ---------------- */
Tabs.console = (d) => {
  const wrap = el("div", { class: "grid" });
  wrap.append(el("div", { class: "panel", id: "alertsPanel" },
    el("h3", {}, "Alerts"), el("div", { class: "dim" }, "loading…")));
  api("/api/alerts").then(r => {
    const p = $("#alertsPanel");
    if (!p) return;
    p.replaceChildren(el("h3", {}, `Alerts (${r.alerts.length})`),
      r.alerts.length ? el("div", {}, r.alerts.map(a =>
        el("div", { class: `alert ${a.level}` },
          el("div", { class: "t" }, a.title),
          a.detail ? el("div", { class: "d" }, a.detail) : null)))
        : el("div", { class: "okbox" }, "✓ no active alerts"));
  }).catch(() => {});
  wrap.append(el("div", { class: "panel" },
    el("h3", {}, "Manual action journal — \"did I do that or did the bot\""),
    (d.actions || []).length ? el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ...["when", "action", "params", "result"].map(h => el("th", {}, h)))),
      el("tbody", {}, d.actions.map(a => el("tr", {},
        el("td", { class: "dim mono", style: "font-size:11px" }, String(a.ts || "").slice(5, 16)),
        el("td", { class: "sym" }, a.action),
        el("td", { class: "dim", style: "font-size:11px;white-space:normal;max-width:340px" },
          JSON.stringify(a.params).slice(0, 140)),
        el("td", {}, (a.result && a.result.ok !== false)
          ? el("span", { class: "up" }, "✓")
          : el("span", { class: "dn", title: JSON.stringify(a.result) }, "✗")))))))
      : el("div", { class: "dim" }, "no manual actions yet — everything so far was the bot")));
  return wrap;
};

/* ============================== boot ============================== */
let pollTimer = null;
async function pollLoop() {
  clearTimeout(pollTimer);
  let interval = 30000;
  try {
    const meta = await api("/api/meta");
    interval = (meta.poll_seconds || 30) * 1000;
  } catch { /* keep default */ }
  if (document.visibilityState === "visible") await refreshTab(true);
  pollTimer = setTimeout(pollLoop, interval);
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refreshTab(true);
});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
refreshTab(true).then(() => pollLoop());
