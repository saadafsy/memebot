"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = (v) => (v === null || v === undefined || isNaN(v)) ? "–" : (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
const px = (v) => (v === null || v === undefined) ? "–" : Number(v).toPrecision(6);

async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json", "X-Memebot": "1"}, body: JSON.stringify(body)} : {};
  opt.credentials = "same-origin";
  const r = await fetch(path, opt);
  if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || ("HTTP " + r.status));
  return j;
}

function banner(msg, kind) {
  const b = $("#banner");
  b.textContent = msg; b.className = "banner " + (kind || "");
  clearTimeout(banner.t); banner.t = setTimeout(() => b.classList.add("hidden"), 6000);
}

function engineCard(e) {
  const cls = e.state === "ON" ? "on" : (e.state === "PAUSED" ? "paused" : "off");
  return `<div class="card eng ${e.mode === "LIVE" ? "real" : ""}">
    <div class="eng-head"><h2>${esc(e.label)}</h2><span class="pill ${cls}">${esc(e.state)}</span></div>
    <div class="grid">
      <div><b>${e.open_count}/${e.max_open}</b><span>open</span></div>
      <div><b>${e.closed}/${e.target_closed}</b><span>closed</span></div>
      <div><b class="${e.today_pnl < 0 ? "neg" : "pos"}">${money(e.today_pnl)}</b><span>today (cap -$${e.daily_loss_cap})</span></div>
      <div><b class="${e.realized_pnl < 0 ? "neg" : "pos"}">${money(e.realized_pnl)}</b><span>realized</span></div>
    </div>
    <p class="small">W/L ${e.wins}/${e.losses} (${e.win_rate_pct}%) · last scan ${esc(e.last_scan || "not yet")}
    ${e.last_error ? `<br><span class="neg">last error: ${esc(e.last_error.slice(0, 160))}</span>` : ""}</p></div>`;
}

function positionsTable(engines) {
  const rows = [];
  for (const e of engines) for (const p of e.open) rows.push({e, p});
  if (!rows.length) return "<p class='small'>No open positions.</p>";
  return `<table><tr><th>Book</th><th>Coin</th><th>Entry</th><th>Now</th><th>P/L</th><th>TP / SL</th><th>Exit by</th><th></th></tr>` +
    rows.map(({e, p}) => `<tr><td>${esc(e.label)}</td><td>${esc(p.name)}</td><td>${px(p.entry)}</td><td>${px(p.last_price)}</td>
      <td class="${(p.unrealized_net ?? 0) < 0 ? "neg" : "pos"}">${money(p.unrealized_net)}</td>
      <td>${px(p.tp)} / ${px(p.sl)}</td><td>${esc((p.deadline || "").slice(5, 16))}</td>
      <td><button class="sm stop" data-sell="${esc(p.address)}" data-book="${esc(e.label)}" data-name="${esc(p.name)}">Sell</button></td></tr>`).join("") + "</table>";
}

async function refresh() {
  try {
    const s = await api("/api/status");
    $("#conn").textContent = "live"; $("#conn").className = "pill on";
    $("#engines").innerHTML = s.engines.map(engineCard).join("");
    document.querySelectorAll(".live-only").forEach((n) => n.classList.toggle("hidden", !s.live_armed));
    $("#positions").innerHTML = positionsTable(s.engines);
    const book = $("#book");
    const labels = s.engines.map((e) => e.label);
    if (book.options.length !== labels.length) { book.innerHTML = labels.map((l) => `<option>${esc(l)}</option>`).join(""); }
    const appr = s.engines.flatMap((e) => e.pending_approvals || []);
    $("#approvals-card").hidden = !appr.length;
    $("#approvals").innerHTML = appr.map((a) => `<div class="row"><b>${esc(a.name)}</b>
      <button class="sm go" data-approve="${esc(a.id)}">Approve</button><button class="sm stop" data-reject="${esc(a.id)}">Reject</button></div>`).join("");
    const t = await api("/api/trades?engine=" + encodeURIComponent(book.value || "PAPER") + "&limit=25");
    $("#trades").innerHTML = t.length ? `<table><tr><th>Coin</th><th>Closed</th><th>Reason</th><th>Net</th></tr>` +
      t.map((r) => `<tr><td>${esc(r.name)}</td><td>${esc((r.closed || "").slice(5, 16))}</td><td>${esc(r.reason)}</td>
      <td class="${Number(r.net_pnl) < 0 ? "neg" : "pos"}">${money(Number(r.net_pnl))}</td></tr>`).join("") + "</table>" : "<p class='small'>No closed trades yet.</p>";
    const al = await api("/api/alerts");
    $("#alerts").innerHTML = al.map((l) => `<li>${esc(l)}</li>`).join("");
  } catch (e) {
    if (e.message !== "login") { $("#conn").textContent = "offline"; $("#conn").className = "pill off"; }
  }
}

async function loadSettings() {
  try { const s = await api("/api/settings"); $("#settings").textContent = JSON.stringify(s.settings, null, 2) + "\n\nsha256 " + s.sha256; } catch (e) {}
}

document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("button"); if (!b) return;
  try {
    if (b.dataset.act) {
      if (b.dataset.target === "REAL" && b.dataset.act === "start" && !confirm("Turn on REAL-money entries? Each buy still needs your Approve.")) return;
      if (b.dataset.act === "pause" && !confirm("Pause everything, including automatic exits?")) return;
      const r = await api("/api/control", {action: b.dataset.act, target: b.dataset.target || "PAPER"}); banner(r.message, "ok");
    } else if (b.dataset.sell) {
      if (!confirm(`Sell ${b.dataset.name} (${b.dataset.book}) now at market?`)) return;
      const r = await api("/api/control", {action: "sell", target: b.dataset.book, address: b.dataset.sell}); banner(r.message, "ok");
    } else if (b.dataset.approve || b.dataset.reject) {
      const r = await api("/api/control", {action: b.dataset.approve ? "approve" : "reject", id: b.dataset.approve || b.dataset.reject}); banner(r.message, "ok");
    } else if (b.id === "logout") {
      await api("/api/logout", {}); location.href = "/login"; return;
    } else return;
    refresh();
  } catch (e) { if (e.message !== "login") banner(e.message, "err"); }
});
$("#book").addEventListener("change", refresh);

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
refresh(); loadSettings(); setInterval(refresh, 5000);
