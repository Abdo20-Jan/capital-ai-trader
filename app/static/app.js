"use strict";
/* ============================================================ estado e utilitários */
const S = { status: {}, account: {}, agents: [], quotes: {}, positions: [], news: [], decisions: [], trades: [],
  equity: [], events: [], sentiment: [], model: null, newsTimes: [], prevQuotes: {} };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null || isNaN(v) ? "—" : (v < 0 ? "-$" : "$") + Math.abs(Number(v)).toFixed(2));
const signed = (v, d = 2) => (v == null || isNaN(v) ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(d));
const pct = (v, d = 0) => (v == null || isNaN(v) ? "—" : (Number(v) * 100).toFixed(d) + "%");
const px = (v) => { if (v == null) return "—"; const a = Math.abs(v); return Number(v).toFixed(a >= 1000 ? 1 : a >= 10 ? 2 : 5); };
const hhmm = (iso) => { if (!iso) return ""; const d = new Date(typeof iso === "number" ? iso * 1000 : iso); return isNaN(d) ? "" : d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); };
const updn = (v) => (v > 0 ? "up" : v < 0 ? "dn" : "");
const AG_COLORS = { scalp: "var(--a-scalp)", day: "var(--a-day)", swing: "var(--a-swing)", long: "var(--a-long)", news: "var(--a-news)" };
const agColor = (k) => AG_COLORS[k] || "var(--a-manual)";
const agName = (k) => (S.agents.find((a) => a.config.key === k)?.config.name) || (k === "manual" ? "Manual" : k || "?");
const sentColor = (v) => (v > 0.05 ? "var(--up)" : v < -0.05 ? "var(--down)" : "var(--mid)");
const DIR = { BUY: "COMPRA", SELL: "VENDA" };
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const throttle = (fn, ms) => { let t = 0, timer = null; return (...a) => { const now = Date.now(); clearTimeout(timer);
  if (now - t >= ms) { t = now; fn(...a); } else timer = setTimeout(() => { t = Date.now(); fn(...a); }, ms - (now - t)); }; };
const tip = $("tip");
function showTip(e, html) { tip.innerHTML = html; tip.style.display = "block";
  const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8), y = Math.min(e.clientY + 14, innerHeight - tip.offsetHeight - 8);
  tip.style.left = x + "px"; tip.style.top = y + "px"; }
const hideTip = () => { tip.style.display = "none"; };

/* ============================================================ abas */
let TAB = "painel";
function route() {
  TAB = (location.hash || "#painel").slice(1);
  if (!$("tab-" + TAB)) TAB = "painel";
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + TAB));
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === TAB));
  renderTab();
}
addEventListener("hashchange", route);
function renderTab() {
  if (TAB === "painel") { renderKpis(); renderEquity(); renderAgentMini(); renderNewsLive(); renderDecLive(); renderPosMini(); }
  if (TAB === "agentes") { renderAgents(); renderRiskForm(); }
  if (TAB === "noticias") { renderSentBars(); renderSources(); renderNewsAll(); }
  if (TAB === "ordens") { renderPosFull(); renderDecTable(); renderTrades(); renderEvents(); }
  if (TAB === "mercados") { renderQuotes(); loadChart(); }
  if (TAB === "conexao") { loadConnection(); renderSys(); }
}

/* ============================================================ topo */
function renderHeader() {
  const st = S.status;
  const sim = st.mode !== "capital";
  $("mode-pill").innerHTML = sim ? `modo <b>SIMULADOR</b>${st.speed > 1 ? ` · ${st.speed}× velocidade` : ""}` : `<b>Capital.com DEMO</b> · ${esc((st.broker_id || "").replace("capital:", "conta "))}`;
  const kb = $("kill-btn");
  kb.textContent = st.kill_switch ? "Retomar agentes" : "Pausar tudo";
  kb.classList.toggle("on", !!st.kill_switch);
  const b = $("banner");
  const msgs = [];
  if (sim) msgs.push(`Você está no <b>simulador</b> (preços e notícias sintéticos). Para operar na sua conta demo, vá em <a href="#conexao">Conexão</a>.`);
  if (st.connection_error) msgs.push(`Falha ao conectar na Capital.com: ${esc(st.connection_error)}`);
  if (S.account.guard && S.account.guard !== "ok") msgs.push(`Novas entradas bloqueadas: ${esc(S.account.guard)}`);
  b.hidden = !msgs.length; b.innerHTML = msgs.join("<br>");
}
let lastMsg = Date.now();
setInterval(() => $("live-pill").classList.toggle("live", Date.now() - lastMsg < 10000), 1000);

function renderTape() {
  const html = (S.status.epics || Object.keys(S.quotes)).map((e) => {
    const q = S.quotes[e]; if (!q) return `<span><b>${esc(e)}</b>—</span>`;
    const ch = q.open ? (q.mid / q.open - 1) * 100 : 0;
    return `<span data-e="${esc(e)}"><b>${esc(e)}</b>${px(q.mid)} <span class="chg ${updn(ch)}">${signed(ch, 2)}%</span></span>`;
  }).join("");
  $("tape").innerHTML = html;
}
const renderTapeT = throttle(renderTape, 1000);

/* ============================================================ painel */
function equityNow() { return S.account.equity ?? S.status.start; }
function renderKpis() {
  const a = S.account, st = S.status, eq = equityNow();
  const start = st.start || 60, target = st.target || 100;
  const prog = Math.max(0, Math.min(1, (eq - start) / (target - start)));
  const all = S.agents.reduce((acc, x) => ({ n: acc.n + x.stats.trades, w: acc.w + x.stats.wins, p: acc.p + x.stats.pnl }), { n: 0, w: 0, p: 0 });
  const upl = S.positions.reduce((s, p) => s + (p.upl || 0), 0);
  $("kpis").innerHTML = `
    <div class="kpi"><div class="l">Banca do bot</div><div class="v">${money(eq)}</div><div class="s">meta ${money(target)} · ${Math.round(prog * 100)}%</div><div class="goal"><i style="width:${prog * 100}%"></i></div></div>
    <div class="kpi"><div class="l">Resultado</div><div class="v"><span class="pos ${updn(eq - start)}">${signed(eq - start)}</span></div><div class="s">${signed((eq / start - 1) * 100, 1)}% desde ${money(start)}</div></div>
    <div class="kpi"><div class="l">Em aberto</div><div class="v"><span class="pos ${updn(upl)}">${signed(upl)}</span></div><div class="s">${S.positions.length} posições</div></div>
    <div class="kpi"><div class="l">Trades fechados</div><div class="v">${all.n}</div><div class="s">acerto ${all.n ? Math.round(100 * all.w / all.n) + "%" : "—"}</div></div>
    <div class="kpi"><div class="l">Conta na corretora</div><div class="v">${money(a.real_equity)}</div><div class="s">${st.mode === "capital" ? "Capital.com demo" : "simulador"}</div></div>
    <div class="kpi"><div class="l">Agentes ativos</div><div class="v">${S.agents.filter((x) => x.config.enabled).length}/${S.agents.length}</div><div class="s">${st.kill_switch ? "PAUSA GERAL" : esc(a.guard || "")}</div></div>`;
  $("eq-sub").textContent = st.model ? `modelo ML: ${st.model}` : "";
}

function lineChart(el, pts, opts = {}) {
  const W = el.clientWidth || 600, H = el.clientHeight || 240, m = { l: 48, r: 12, t: 12, b: 22 };
  if (pts.length < 2) { el.innerHTML = `<div class="empty">aguardando dados…</div>`; return; }
  const ys = pts.map((p) => p.y); let lo = Math.min(...ys, ...(opts.refs || []).map((r) => r.y)), hi = Math.max(...ys, ...(opts.refs || []).map((r) => r.y));
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; } const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const x = (i) => m.l + (i / (pts.length - 1)) * (W - m.l - m.r), y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  const ticks = [lo + pad, (lo + hi) / 2, hi - pad];
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label || "gráfico")}">
    ${ticks.map((t) => `<line class="gridl" x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}"/>`).join("")}
    <g class="axis">${ticks.map((t) => `<text x="${m.l - 6}" y="${y(t) + 3}" text-anchor="end">${t.toFixed(t > 100 ? 0 : 2)}</text>`).join("")}
      <text x="${m.l}" y="${H - 6}">${hhmm(pts[0].t)}</text><text x="${W - m.r}" y="${H - 6}" text-anchor="end">${hhmm(pts[pts.length - 1].t)}</text></g>
    ${(opts.refs || []).map((r) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(r.y)}" y2="${y(r.y)}" stroke="var(--text-muted)" stroke-dasharray="3 4"/>
      <text x="${W - m.r}" y="${y(r.y) - 4}" text-anchor="end" font-size="10" font-family="var(--mono)" fill="var(--text-muted)">${esc(r.label)}</text>`).join("")}
    <path d="${d}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>
    <line id="${el.id}-x" y1="${m.t}" y2="${H - m.b}" stroke="var(--text-muted)" visibility="hidden"/>
    <circle id="${el.id}-d" r="4" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2" visibility="hidden"/>
    <rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent" id="${el.id}-hit"/></svg>`;
  const hit = $(el.id + "-hit"), cx = $(el.id + "-x"), dot = $(el.id + "-d"), svg = el.querySelector("svg");
  hit.onmousemove = (e) => { const r = svg.getBoundingClientRect(); const i = Math.max(0, Math.min(pts.length - 1, Math.round((((e.clientX - r.left) * W / r.width) - m.l) / (W - m.l - m.r) * (pts.length - 1))));
    cx.setAttribute("x1", x(i)); cx.setAttribute("x2", x(i)); cx.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(pts[i].y)); dot.setAttribute("visibility", "visible");
    showTip(e, opts.tip(pts[i])); };
  hit.onmouseleave = () => { hideTip(); cx.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); };
}
function renderEquity() {
  const pts = S.equity.map((e) => ({ t: e.time, y: e.equity, real: e.real_equity }));
  lineChart($("eq-chart"), pts, { label: "Patrimônio da banca", refs: [{ y: S.status.start || 60, label: "início" }, ...(pts.length && Math.max(...pts.map((p) => p.y)) > (S.status.target || 100) * 0.9 ? [{ y: S.status.target, label: "meta" }] : [])],
    tip: (p) => `${new Date(p.t).toLocaleString("pt-BR")}<br>banca <b>${money(p.y)}</b><br>conta ${money(p.real)}` });
}
const renderEquityT = throttle(() => TAB === "painel" && renderEquity(), 3000);

function renderAgentMini() {
  $("agent-mini").innerHTML = S.agents.map((a) => {
    const c = a.config, st = a.stats;
    return `<div class="agent-row"><span class="n"><i class="agent-dot" style="background:${agColor(c.key)}"></i>${esc(c.name)}</span>
      <span class="mono small ${c.enabled ? "" : "muted"}">${c.enabled ? "ativo" : "pausado"} · ${a.open} abertas</span>
      <span class="d">${st.trades} trades · acerto ${pct(st.win_rate)} · ${signed(st.pnl)} · ${st.expectancy_r == null ? "—" : signed(st.expectancy_r) + "R"}</span></div>`;
  }).join("");
}

function newsItem(n, isNew) {
  return `<li class="${isNew ? "new" : ""}"><div class="meta"><span class="tag">${esc(n.topic || "—")}</span>${n.ticker ? `<b>${esc(n.ticker)}</b>` : ""}
    <span>${hhmm(n.ingested || n.time)}</span><span><i class="sw-sent" style="background:${sentColor(n.sentiment)}"></i> ${signed(n.sentiment)}</span>
    <span>rel ${Number(n.relevance || 0).toFixed(2)}</span><span>${esc(n.source || "")}</span><span>${esc(n.scorer || "")}</span></div>
    ${n.url ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>` : esc(n.title)}</li>`;
}
function renderNewsLive(newId) {
  $("news-live").innerHTML = S.news.slice(0, 25).map((n) => newsItem(n, n.id === newId)).join("") || `<li class="empty">Aguardando notícias…</li>`;
  const now = Date.now();
  const n = S.news.filter((x) => now - new Date(x.ingested || x.time).getTime() < 3600e3).length;
  $("news-rate").textContent = `${n} na última hora`;
}
function decItem(d, isNew) {
  return `<li class="${isNew ? "new" : ""}"><div class="meta"><i class="agent-dot" style="background:${agColor(d.agent)}"></i><b>${esc(agName(d.agent))}</b>
    <span>${hhmm(d.time)}</span><b>${esc(d.epic)}</b><span>${DIR[d.direction] || ""}</span></div>
    <span class="${d.accepted ? "acc" : "rej"}">${esc(d.reason)}</span>${d.signal ? `<div class="muted small">${esc(d.signal)}</div>` : ""}</li>`;
}
function renderDecLive(isNew) { $("dec-live").innerHTML = S.decisions.slice(0, 25).map((d, i) => decItem(d, isNew && i === 0)).join("") || `<li class="empty">Nenhuma decisão ainda. Os agentes decidem a cada candle fechado.</li>`; }
function posTable(full) {
  if (!S.positions.length) return `<div class="empty">Nenhuma posição aberta.</div>`;
  return `<table><tr><th>agente</th><th>ativo</th><th>lado</th>${full ? `<th class="num">tam</th>` : ""}<th class="num">entrada</th>${full ? `<th class="num">stop</th><th class="num">alvo</th><th class="num">risco</th><th>aberta</th><th>motivo</th>` : ""}<th class="num">resultado</th>${full ? "<th></th>" : ""}</tr>` +
    S.positions.map((p) => `<tr><td><i class="agent-dot" style="background:${agColor(p.agent)}"></i>${esc(agName(p.agent))}</td><td>${esc(p.epic)}</td><td>${DIR[p.direction] || p.direction}</td>
      ${full ? `<td class="num">${p.size}</td>` : ""}<td class="num">${px(p.level)}</td>
      ${full ? `<td class="num">${px(p.stop)}</td><td class="num">${px(p.target)}</td><td class="num">${money(p.risk)}</td><td>${hhmm(p.opened_at)}</td><td class="why">${esc(p.reason || "")}</td>` : ""}
      <td class="num"><span class="pos ${updn(p.upl)}">${signed(p.upl)}</span></td>
      ${full ? `<td><button class="btn danger sm" data-close="${esc(p.deal_id)}" type="button">fechar</button></td>` : ""}</tr>`).join("") + `</table>`;
}
function renderPosMini() { $("pos-mini").innerHTML = posTable(false); $("pos-count").textContent = `${S.positions.length}`; }

/* ============================================================ agentes */
const AG_FIELDS = [["risk_per_trade", "risco/trade %", 100, 0.1], ["margin_share", "margem máx %", 100, 1], ["max_positions", "posições máx", 1, 1],
  ["stop_atr", "stop (×ATR)", 1, 0.1], ["tp_atr", "alvo (×ATR)", 1, 0.1], ["max_hold_min", "tempo máx (min)", 1, 1]];
let agentsBuilt = "";
function renderAgents() {
  const sig = S.agents.map((a) => a.config.key).join(",");
  if (agentsBuilt !== sig) {
    agentsBuilt = sig;
    $("agents").innerHTML = S.agents.map((a) => { const c = a.config;
      return `<div class="agent" id="ag-${c.key}" style="border-top-color:${agColor(c.key)}">
        <header><div><h3>${esc(c.name)}</h3><div class="style">${esc(c.style)} · gráfico ${esc(c.resolution)} · decide a cada candle fechado</div></div>
          <label class="switch" title="ligar/desligar"><input type="checkbox" data-en="${c.key}"><span></span></label></header>
        <p>${esc(c.description)}</p>
        <div class="stats" data-stats></div>
        <form data-agent="${c.key}">${AG_FIELDS.map(([k, l, mult, step]) => `<label>${l}<input name="${k}" type="number" step="${step}" min="0"></label>`).join("")}
          <label class="chk" style="flex-direction:row;grid-column:1/3"><input type="checkbox" name="allow_short"> pode vender (short)</label>
          <button class="btn sm" type="submit">Salvar</button></form>
        <div class="reads" data-reads></div></div>`; }).join("");
    S.agents.forEach((a) => { const f = document.querySelector(`form[data-agent="${a.config.key}"]`);
      AG_FIELDS.forEach(([k, , mult]) => { const v = a.config[k]; f.elements[k].value = v == null ? "" : +(v * mult).toFixed(4); });
      f.elements.allow_short.checked = !!a.config.allow_short; });
  }
  S.agents.forEach((a) => {
    const el = $("ag-" + a.config.key), st = a.stats; if (!el) return;
    el.querySelector("[data-en]").checked = !!a.config.enabled;
    el.querySelector("[data-stats]").innerHTML = `
      <div><div class="l">status</div><div class="v small">${a.config.enabled ? "ativo" : `<span class="badge ${a.paused_reason ? "warn" : ""}">pausado</span>`}</div></div>
      <div><div class="l">trades</div><div class="v">${st.trades} <span class="muted small">${a.open} abertas</span></div></div>
      <div><div class="l">acerto</div><div class="v">${pct(st.win_rate)}</div></div>
      <div><div class="l">resultado</div><div class="v"><span class="pos ${updn(st.pnl)}">${signed(st.pnl)}</span></div></div>`;
    const reads = Object.entries(a.state || {}).map(([e, s]) => `<div><b>${esc(e)}</b><span class="${s.signal ? "" : "muted"}">${esc(s.note || "")}</span></div>`).join("");
    el.querySelector("[data-reads]").innerHTML = (a.paused_reason ? `<div><b>⚠</b><span>${esc(a.paused_reason)}</span></div>` : "") + (reads || `<div class="muted">aguardando o primeiro candle fechado…</div>`);
  });
}
document.addEventListener("change", async (e) => {
  const k = e.target.dataset?.en; if (!k) return;
  await api(`/api/agents/${k}`, { method: "POST", body: JSON.stringify({ enabled: e.target.checked }) }); load();
});
document.addEventListener("submit", async (e) => {
  const k = e.target.dataset?.agent; if (!k) return; e.preventDefault();
  const f = e.target, body = { allow_short: f.elements.allow_short.checked };
  AG_FIELDS.forEach(([n, , mult]) => { if (f.elements[n].value !== "") body[n] = Number(f.elements[n].value) / mult; });
  await api(`/api/agents/${k}`, { method: "POST", body: JSON.stringify(body) }); agentsBuilt = ""; load();
});
let riskFilled = false;
function renderRiskForm() {
  const f = $("risk-form"), st = S.status;
  if (!riskFilled && st.start) { f.start_balance.value = st.start; f.target_balance.value = st.target;
    f.max_daily_loss.value = +(st.max_daily_loss * 100).toFixed(2); f.stop_at_target.checked = !!st.stop_at_target; riskFilled = true; }
  let m = null; try { m = S.model ? JSON.parse(S.model) : null; } catch (e) {}
  $("model-box").innerHTML = m ? `Modelo ML do Day Trader: versão ${esc(m.version)} · ${m.rows} linhas · AUC ${m.auc_long.toFixed(3)}/${m.auc_short.toFixed(3)} · expectativa no teste ${signed(m.expectancy_r, 3)}R em ${m.trades} trades — ${esc(m.note)}` : `Modelo ML: ${esc(S.status.model || "—")}`;
}
$("risk-form").addEventListener("submit", async (e) => {
  e.preventDefault(); const f = e.target;
  S.status = await api("/api/risk", { method: "POST", body: JSON.stringify({ start_balance: +f.start_balance.value, target_balance: +f.target_balance.value,
    max_daily_loss: +f.max_daily_loss.value / 100, stop_at_target: f.stop_at_target.checked }) });
  riskFilled = false; renderTab();
});
$("reset-bank").onclick = async () => { if (confirm("Reiniciar a banca virtual no valor inicial? O histórico é mantido.")) { await api("/api/bankroll/reset", { method: "POST" }); load(); } };
$("retrain").onclick = async () => { await api("/api/model/retrain", { method: "POST" }); alert("Treino iniciado em segundo plano. Acompanhe em Eventos."); };

/* ============================================================ notícias */
function renderSentBars() {
  $("sent-bars").innerHTML = S.sentiment.map((s) => { const w = Math.min(Math.abs(s.score), 1) * 50;
    return `<div class="sent-row"><b>${esc(s.epic)}</b><div class="div-bar" title="${esc(s.ticker)}"><i style="left:${s.score >= 0 ? 50 : 50 - w}%;width:${Math.max(w, .5)}%;background:${sentColor(s.score)}"></i></div><span style="text-align:right">${signed(s.score)}</span></div>`; }).join("");
}
function renderSources() {
  const src = S.status.news_sources || {};
  $("sources").innerHTML = Object.entries(src).map(([k, v]) => `<div class="mono">${v.startsWith("ok") ? "✓" : "✕"} ${esc(k)} <span class="muted">${esc(v)}</span></div>`).join("") || `<div class="muted">iniciando…</div>`;
}
function renderNewsAll(newId) {
  const t = $("nf-ticker").value, rel = $("nf-rel").checked;
  const list = S.news.filter((n) => (!t || n.ticker === t) && (!rel || (n.relevance || 0) >= 0.6));
  $("news-all").innerHTML = list.slice(0, 200).map((n) => newsItem(n, n.id === newId)).join("") || `<li class="empty">Nada com esses filtros.</li>`;
}
$("nf-ticker").onchange = () => renderNewsAll(); $("nf-rel").onchange = () => renderNewsAll();

/* ============================================================ ordens */
function renderPosFull() { $("pos-full").innerHTML = posTable(true); }
document.addEventListener("click", async (e) => {
  const id = e.target.dataset?.close; if (!id) return;
  e.target.disabled = true; await api(`/api/close/${encodeURIComponent(id)}`, { method: "POST" });
});
$("close-all").onclick = async () => { if (confirm("Fechar TODAS as posições abertas agora?")) await api("/api/close-all", { method: "POST" }); };
function renderDecTable() {
  $("dec-table").innerHTML = `<table><tr><th>hora</th><th>agente</th><th>ativo</th><th>lado</th><th>decisão</th></tr>` +
    S.decisions.slice(0, 150).map((d) => `<tr><td>${hhmm(d.time)}</td><td><i class="agent-dot" style="background:${agColor(d.agent)}"></i>${esc(agName(d.agent))}</td><td>${esc(d.epic)}</td><td>${DIR[d.direction] || ""}</td><td class="why"><span class="${d.accepted ? "acc" : "rej"}">${esc(d.reason)}</span></td></tr>`).join("") + `</table>`;
}
function renderTrades() {
  $("trades-table").innerHTML = S.trades.length ? `<table><tr><th>fechado</th><th>agente</th><th>ativo</th><th>lado</th><th class="num">resultado</th><th class="num">R</th><th>saída</th></tr>` +
    S.trades.map((t) => `<tr><td>${hhmm(t.closed)}</td><td><i class="agent-dot" style="background:${agColor(t.agent)}"></i>${esc(agName(t.agent))}</td><td>${esc(t.epic)}</td><td>${DIR[t.direction] || ""}</td>
      <td class="num"><span class="pos ${updn(t.pnl)}">${signed(t.pnl)}</span></td><td class="num">${t.r == null ? "—" : signed(t.r, 1)}</td><td class="muted">${esc(t.reason_close || "")}</td></tr>`).join("") + `</table>` : `<div class="empty">Nenhum trade fechado ainda.</div>`;
}
const LV = { error: "✕", warn: "⚠", win: "▲", loss: "▼", trade: "⇄", model: "◆", info: "·" };
function renderEvents() { $("events").innerHTML = S.events.slice(0, 80).map((e) => `<li><div class="meta"><span>${hhmm(e.time)}</span><span>${LV[e.level] || "·"} ${esc(e.level)}</span></div>${esc(e.message)}</li>`).join(""); }

/* ============================================================ mercados */
let chartEpic = null;
function renderQuotes() {
  const epics = S.status.epics || Object.keys(S.quotes);
  if (!chartEpic) chartEpic = epics[0];
  $("quotes").innerHTML = epics.map((e) => { const q = S.quotes[e] || {}; const ch = q.open ? (q.mid / q.open - 1) * 100 : 0;
    return `<div class="q ${e === chartEpic ? "sel" : ""}" data-q="${esc(e)}"><div class="e"><span>${esc(e)}</span><span class="chg ${updn(ch)} small">${signed(ch, 2)}%</span></div>
      <div class="p">${px(q.mid)}</div><div class="ba">bid ${px(q.bid)} · ask ${px(q.ask)}</div></div>`; }).join("");
}
const renderQuotesT = throttle(() => TAB === "mercados" && renderQuotes(), 1000);
$("quotes").addEventListener("click", (e) => { const q = e.target.closest("[data-q]"); if (!q) return; chartEpic = q.dataset.q; renderQuotes(); loadChart(); });
$("chart-res").onchange = () => loadChart();
async function loadChart() {
  if (TAB !== "mercados" || !chartEpic) return;
  const res = $("chart-res").value;
  try { candles(await api(`/api/candles?epic=${encodeURIComponent(chartEpic)}&res=${res}&count=120`)); } catch (e) { $("px-chart").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  $("chart-title").textContent = `${chartEpic} · ${$("chart-res").selectedOptions[0].text}`;
}
setInterval(loadChart, 4000);
function candles(data) {
  const el = $("px-chart"), W = el.clientWidth || 800, H = el.clientHeight || 380, m = { l: 56, r: 12, t: 10, b: 22 };
  if (!data.length) { el.innerHTML = `<div class="empty">sem dados</div>`; return; }
  const pos = S.positions.filter((p) => p.epic === chartEpic);
  const levels = pos.flatMap((p) => [p.level, p.stop, p.target]).filter((v) => v != null);
  let lo = Math.min(...data.map((d) => d.l), ...levels), hi = Math.max(...data.map((d) => d.h), ...levels); const pad = (hi - lo) * 0.05 || 1; lo -= pad; hi += pad;
  const n = data.length, bw = (W - m.l - m.r) / n, x = (i) => m.l + bw * (i + 0.5), y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  const ticks = [0, 1, 2, 3, 4].map((i) => lo + pad + (hi - lo - 2 * pad) * i / 4);
  const body = Math.max(1, Math.min(10, bw - 2));
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Candles de ${esc(chartEpic)}">
    ${ticks.map((t) => `<line class="gridl" x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}"/>`).join("")}
    <g class="axis">${ticks.map((t) => `<text x="${m.l - 6}" y="${y(t) + 3}" text-anchor="end">${px(t)}</text>`).join("")}
      <text x="${m.l}" y="${H - 6}">${new Date(data[0].t).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</text>
      <text x="${W - m.r}" y="${H - 6}" text-anchor="end">${new Date(data[n - 1].t).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</text></g>
    ${data.map((d, i) => { const up = d.c >= d.o, c = up ? "var(--up)" : "var(--down)";
      return `<g data-i="${i}"><line x1="${x(i)}" x2="${x(i)}" y1="${y(d.h)}" y2="${y(d.l)}" stroke="${c}" stroke-width="1"/>
        <rect x="${x(i) - body / 2}" y="${Math.min(y(d.o), y(d.c))}" width="${body}" height="${Math.max(1, Math.abs(y(d.o) - y(d.c)))}" fill="${c}" rx="1"/>
        <rect x="${x(i) - bw / 2}" y="${m.t}" width="${bw}" height="${H - m.t - m.b}" fill="transparent"/></g>`; }).join("")}
    ${pos.map((p) => [["entrada", p.level], ["stop", p.stop], ["alvo", p.target]].filter(([, v]) => v != null).map(([l, v]) =>
      `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="${agColor(p.agent)}" stroke-dasharray="${l === "entrada" ? "0" : "4 4"}" stroke-width="1.5"/>
       <text x="${W - m.r - 4}" y="${y(v) - 4}" text-anchor="end" font-size="10" font-family="var(--mono)" fill="var(--text-secondary)">${esc(agName(p.agent))} ${l} ${px(v)}</text>`).join("")).join("")}
  </svg>`;
  el.querySelectorAll("g[data-i]").forEach((g) => { const d = data[+g.dataset.i];
    g.onmousemove = (e) => showTip(e, `${new Date(d.t).toLocaleString("pt-BR")}<br>abertura ${px(d.o)} · máx ${px(d.h)}<br>mín ${px(d.l)} · fech. <b>${px(d.c)}</b>`);
    g.onmouseleave = hideTip; });
}

/* ============================================================ conexão */
async function loadConnection() {
  try {
    const c = await api("/api/connection"), box = $("conn-status");
    if (c.mode === "capital") { box.className = "status-box ok"; box.innerHTML = `Conectado à <b>Capital.com DEMO</b> (${esc(c.broker_id)}). Preços: ${esc(c.stream)}.`; }
    else if (c.error) { box.className = "status-box err"; box.innerHTML = `Não conectado: ${esc(c.error)}`; }
    else { box.className = "status-box sim"; box.innerHTML = c.configured ? "Credenciais salvas, mas o app está no simulador." : "Ainda não conectado — o app está no <b>simulador</b>."; }
    const f = $("conn-form"); if (!f.identifier.value) f.identifier.value = c.identifier || "";
    f.api_key.placeholder = c.api_key_hint ? `atual: ${c.api_key_hint} (vazio = manter)` : "cole a chave da API";
    if (c.account_id && ![...f.account_id.options].some((o) => o.value === c.account_id)) f.account_id.add(new Option(`conta ${c.account_id}`, c.account_id));
    f.account_id.value = c.account_id || "";
    $("to-epic").innerHTML = (S.status.epics || []).map((e) => `<option>${esc(e)}</option>`).join("");
  } catch (e) {}
}
function credsBody() { const f = $("conn-form"); return JSON.stringify({ api_key: f.api_key.value.trim(), identifier: f.identifier.value.trim(), password: f.password.value, account_id: f.account_id.value }); }
$("conn-test").onclick = async (e) => {
  e.target.disabled = true; $("conn-result").textContent = "Testando…";
  const r = await api("/api/connection/test", { method: "POST", body: credsBody() }).catch((x) => ({ ok: false, error: x.message }));
  e.target.disabled = false;
  if (!r.ok) { $("conn-result").innerHTML = `<div class="status-box err">${esc(r.error)}<br><span class="muted">Confira: e-mail de login, a chave e a senha da CHAVE (não a da conta). A chave precisa estar ativa.</span></div>`; return; }
  const sel = $("conn-form").account_id, keep = sel.value;
  sel.innerHTML = `<option value="">padrão da Capital.com</option>` + r.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.name || a.id)} · ${esc(a.type || "")} · ${money(a.balance)} ${esc(a.currency || "")}</option>`).join("");
  sel.value = keep || r.current || "";
  $("conn-result").innerHTML = `<div class="status-box ok">Login OK no servidor DEMO. ${r.accounts.length} conta(s) encontrada(s). Modo hedging: ${r.hedging == null ? "?" : r.hedging ? "ligado" : "desligado"}. Escolha a conta e clique em <b>Salvar e conectar</b>.</div>`;
};
$("conn-form").addEventListener("submit", async (e) => {
  e.preventDefault(); const b = e.submitter; if (b) b.disabled = true;
  $("conn-result").textContent = "Salvando e reconectando… (os agentes reiniciam)";
  const r = await api("/api/connection/save", { method: "POST", body: credsBody() }).catch((x) => ({ error: x.message }));
  if (b) b.disabled = false;
  $("conn-result").innerHTML = r.mode === "capital" ? `<div class="status-box ok">Conectado à sua conta demo. Os agentes já estão operando nela.</div>` : `<div class="status-box err">Não conectou: ${esc(r.error || "?")}. O app continua no simulador.</div>`;
  $("conn-form").password.value = ""; $("conn-form").api_key.value = ""; load(); loadConnection();
});
$("conn-sim").onclick = async () => { await api("/api/connection/simulator", { method: "POST" }); load(); loadConnection(); };
$("to-btn").onclick = async (e) => {
  const epic = $("to-epic").value; if (!confirm(`Enviar uma ordem de teste mínima em ${epic} na conta demo e fechá-la em seguida?`)) return;
  e.target.disabled = true; $("to-result").textContent = "Enviando…";
  const r = await api(`/api/connection/test-order?epic=${encodeURIComponent(epic)}`, { method: "POST" }).catch((x) => ({ ok: false, error: x.message }));
  e.target.disabled = false;
  $("to-result").innerHTML = r.ok ? `<div class="status-box ok">Ordem ${esc(r.deal_id)} aberta a ${px(r.level)}, vista na conta e fechada (${esc(r.closed)}). Confira no app da Capital.com.</div>` : `<div class="status-box err">${esc(r.error || JSON.stringify(r))}</div>`;
};
function renderSys() {
  const st = S.status;
  $("sys-status").innerHTML = [["corretora", st.mode === "capital" ? "Capital.com DEMO" : "simulador"], ["preços", st.stream], ["sentimento", st.scorer === "claude" ? "Claude (IA)" : "dicionário (sem chave da Anthropic)"],
    ["modelo ML", st.model], ["relógio do mercado", new Date(st.sim_time).toLocaleString("pt-BR")], ["pausa geral", st.kill_switch ? "LIGADA" : "desligada"]]
    .map(([k, v]) => `<tr><td>${k}</td><td>${esc(v || "—")}</td></tr>`).join("");
}

/* ============================================================ dados */
async function load() {
  try {
    const d = await api("/api/state");
    Object.assign(S, { status: d.status, account: d.account || {}, agents: d.agents, quotes: { ...S.quotes, ...d.quotes }, positions: d.positions,
      news: d.news, sentiment: d.sentiment, decisions: d.decisions, trades: d.trades, events: d.events, model: d.model,
      equity: d.equity.map((e) => ({ time: e.time, equity: e.equity, real_equity: e.real_equity })) });
    const sel = $("nf-ticker"), cur = sel.value; const tks = [...new Set(d.sentiment.map((s) => s.ticker))];
    sel.innerHTML = `<option value="">todos os ativos</option>` + tks.map((t) => `<option>${esc(t)}</option>`).join(""); sel.value = cur;
    renderHeader(); renderTape(); renderTab();
  } catch (e) { console.warn(e); }
}
function connect() {
  const es = new EventSource("/api/stream");
  const on = (k, fn) => es.addEventListener(k, (ev) => { lastMsg = Date.now(); try { fn(JSON.parse(ev.data).data); } catch (e) { console.warn(e); } });
  on("quote", (q) => { S.quotes[q.epic] = { ...q, open: S.quotes[q.epic]?.open ?? q.open }; renderTapeT(); renderQuotesT(); });
  on("news", (n) => { S.news.unshift(n); S.news.length = Math.min(S.news.length, 400); S.newsTimes.push(Date.now());
    if (TAB === "painel") renderNewsLive(n.id); if (TAB === "noticias") renderNewsAll(n.id); });
  on("decision", (d) => { S.decisions.unshift(d); S.decisions.length = Math.min(S.decisions.length, 300);
    if (TAB === "painel") renderDecLive(true); if (TAB === "ordens") renderDecTable(); });
  on("positions", (p) => { S.positions = p; if (TAB === "painel") { renderPosMini(); renderKpis(); } if (TAB === "ordens") renderPosFull(); });
  on("closed", (t) => { S.trades.unshift(t); if (TAB === "ordens") renderTrades(); });
  on("account", (a) => { S.account = a; S.equity.push({ time: new Date(a.time * 1000).toISOString(), equity: a.equity, real_equity: a.real_equity });
    if (S.equity.length > 720) S.equity.shift(); renderHeader(); if (TAB === "painel") { renderKpis(); renderEquityT(); } });
  on("event", (e) => { S.events.unshift(e); if (TAB === "ordens") renderEvents(); });
  on("status", (s) => { S.status = { ...S.status, ...s }; renderHeader(); });
  es.onerror = () => { $("live-pill").classList.remove("live"); };
}
$("kill-btn").onclick = async () => { const on = !S.status.kill_switch; await api(`/api/kill?on=${on}`, { method: "POST" }); S.status.kill_switch = on; renderHeader(); };
$("theme-btn").onclick = () => { const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = cur === "dark" ? "light" : "dark"; try { localStorage.setItem("theme", document.documentElement.dataset.theme); } catch (e) {} };
try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
addEventListener("resize", throttle(() => renderTab(), 300));
route(); load(); connect(); setInterval(load, 15000);
