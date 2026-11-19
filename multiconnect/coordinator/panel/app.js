/* MultiConnect panel - vanilla JS, no external dependencies (works offline). */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem("mc_" + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("mc_" + k, JSON.stringify(v)); } catch (e) {} },
};

let LANG = store.get("lang", "en");
let DICT = {};
let S = null;              // last state
let TAB = store.get("tab", "overview");
let SOUND = store.get("sound", true);
let lastEventId = 0;
let failCount = 0;
const seenAlerts = new Set();

// ------------------------------------------------------------------ i18n
async function loadLang(l) {
  try { DICT = await (await fetch(`/panel/i18n/${l}.json`)).json(); } catch (e) { DICT = {}; }
  LANG = l; store.set("lang", l);
  document.documentElement.lang = l;
  $("#btn-lang").textContent = l === "en" ? "PL" : "EN";
  $$("[data-i18n]").forEach((e) => { const k = e.dataset.i18n; if (DICT[k]) e.textContent = DICT[k]; });
  $$("[data-i18n-ph]").forEach((e) => { const k = e.dataset.i18nPh; if (DICT[k]) e.placeholder = DICT[k]; });
  renderHelp();
  if (S) render();
}
const t = (k, vars) => {
  let s = DICT[k] ?? k.split(".").pop().replace(/_/g, " ");
  if (vars) for (const [a, b] of Object.entries(vars)) s = s.replace(`{${a}}`, b);
  return s;
};

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || (opts.body !== undefined ? "POST" : "GET"),
    headers: opts.form ? { "x-from-panel": "1" } : { "content-type": "application/json", "x-from-panel": "1" },
    body: opts.form || (opts.body !== undefined ? JSON.stringify(opts.body) : undefined),
    credentials: "same-origin",
  });
  if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || r.statusText);
  return data;
}
const act = async (path, body, okMsg) => {
  try {
    const r = await api(path, { body });
    if (r && r.ok === false) toast(r.error || "failed", "error"); else if (okMsg) toast(okMsg, "ok");
    refresh();
    return r;
  } catch (e) { toast(e.message, "error"); }
};

// ------------------------------------------------------------------ helpers
function dur(s) {
  if (s == null) return "–";
  s = Math.max(0, Math.floor(s));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  return Math.floor(s / 3600) + "h " + String(Math.floor(s % 3600 / 60)).padStart(2, "0") + "m";
}
const fmtN = (n) => n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e4 ? (n / 1e3).toFixed(1) + "k" : String(n ?? 0);
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function toast(msg, kind = "info", ms = 4500) {
  const d = document.createElement("div");
  d.className = "toast " + kind; d.textContent = msg;
  $("#toasts").appendChild(d);
  setTimeout(() => d.remove(), ms);
}
function beep(kind = "info") {
  if (!SOUND) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = kind === "error" ? 330 : kind === "urgent" ? 880 : 620;
    o.connect(g); g.connect(ctx.destination); g.gain.value = 0.08;
    o.start(); o.stop(ctx.currentTime + (kind === "urgent" ? 0.5 : 0.18));
  } catch (e) {}
}
function modal(html) { $("#modal-body").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); $("#modal-body").innerHTML = ""; }

const FIX = {
  E_LOGGED_OUT: "fix.logged_out", E_SELECTOR: "fix.selector", E_RATE_LIMIT: "fix.rate_limit", E_CAPTCHA: "fix.captcha",
  E_TIMEOUT: "fix.timeout", E_SEND: "fix.send", E_PROTOCOL: "fix.protocol", E_MAX_ROUNDS: "fix.max_rounds",
  E_PROVIDER: "fix.provider", E_BROWSER: "fix.browser", E_BROWSER_CRASH: "fix.browser", E_WORKER_OFFLINE: "fix.heartbeat",
  E_LOGIN: "fix.logged_out", E_NO_AGENT: "fix.no_agent", E_BRIDGE: "fix.bridge", E_GODOT: "fix.godot",
};
const fixHint = (code) => (code && FIX[code]) ? t(FIX[code]) : "";

// ------------------------------------------------------------------ login
function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); $("#login-token").focus(); }
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const tok = $("#login-token").value.trim();
  document.cookie = `mc_token=${encodeURIComponent(tok)}; path=/; max-age=${86400 * 30}; SameSite=Strict`;
  try { await api("/api/p/state"); $("#login").classList.add("hidden"); $("#app").classList.remove("hidden"); refresh(); }
  catch (err) { $("#login-err").textContent = t("login.bad"); }
});

// ------------------------------------------------------------------ tabs & header
function setTab(name) {
  TAB = name; store.set("tab", name);
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + name));
  if (name === "tokens") loadTokens();
  if (name === "game") loadBackups();
  if (name === "events") loadEvents();
  render();
}
$$("#tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
$("#btn-lang").onclick = () => loadLang(LANG === "en" ? "pl" : "en");
$("#btn-theme").onclick = () => {
  const d = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = d; store.set("theme", d);
};
$("#btn-sound").onclick = () => { SOUND = !SOUND; store.set("sound", SOUND); $("#btn-sound").textContent = SOUND ? "🔔" : "🔕"; };
$("#btn-pause").onclick = () => act("/api/p/global", { paused_all: !(S && S.paused_all) });
$("#modal-close").onclick = closeModal;
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
$("#msg-claude").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#msg-claude-text").value.trim(); if (!v) return;
  act("/api/p/global", { message_claude: v }, t("claude.sent")); $("#msg-claude-text").value = "";
});
$("#agent-filter").onchange = () => renderAgents();
$("#btn-reload-acc").onclick = () => act("/api/p/global", { reload_accounts: true }, "accounts.yaml ✓");

// ------------------------------------------------------------------ manual task control (no Claude needed)
const lines = (s) => s.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean);
$("#btn-new-task").onclick = () => {
  const ids = (S.tasks || []).map((x) => x.id);
  const provs = ((S && S.providers) || []).map((p) => p.id);
  modal(`<h2>${t("nt.new")}</h2>
    <div class="form">
      <label>${t("nt.title")}<input id="nt-title" style="width:100%"></label>
      <label>${t("nt.goal")}<textarea id="nt-goal" rows="6" style="width:100%" placeholder="${esc(t("nt.goalhint"))}"></textarea></label>
      <label>${t("nt.create")}<textarea id="nt-create" rows="2" style="width:100%" placeholder="scripts/player.gd&#10;scenes/player.tscn"></textarea></label>
      <label>${t("nt.modify")}<textarea id="nt-modify" rows="2" style="width:100%"></textarea></label>
      <label>${t("nt.context")}<textarea id="nt-context" rows="2" style="width:100%"></textarea></label>
      <label>${t("nt.interface")}<textarea id="nt-iface" rows="3" style="width:100%" placeholder="func take_damage(amount: int) -> void"></textarea></label>
      <label>${t("nt.acceptance")}<textarea id="nt-acc" rows="3" style="width:100%"></textarea></label>
      <label>${t("t.deps")} <select id="nt-deps" multiple size="4">${ids.map((i) => `<option>${esc(i)}</option>`).join("")}</select></label>
      <label>P <select id="nt-prio">${[1, 2, 3, 4, 5].map((p) => `<option ${p === 3 ? "selected" : ""}>${p}</option>`).join("")}</select></label>
      <label>${t("nt.redundancy")} <select id="nt-red"><option>1</option><option>2</option><option>3</option></select></label>
      ${provs.length ? `<label>${t("nt.prov")} <select id="nt-prov"><option value="">–</option>${provs.map((p) => `<option>${esc(p)}</option>`).join("")}</select></label>` : ""}
      <label>${t("nt.notes")}<textarea id="nt-notes" rows="2" style="width:100%"></textarea></label>
      <button class="btn primary" id="nt-go">${t("nt.dispatch")}</button>
    </div>`);
  $("#nt-go").onclick = async () => {
    const task = {
      title: $("#nt-title").value.trim(), goal: $("#nt-goal").value.trim(),
      files_to_create: lines($("#nt-create").value), files_to_modify: lines($("#nt-modify").value),
      context_files: lines($("#nt-context").value), interface: $("#nt-iface").value.trim() || undefined,
      acceptance: $("#nt-acc").value.split("\n").map((x) => x.trim()).filter(Boolean),
      depends_on: [...$("#nt-deps").selectedOptions].map((o) => o.value),
      priority: +$("#nt-prio").value, redundancy: +$("#nt-red").value,
      notes: $("#nt-notes").value.trim() || undefined,
    };
    const pv = $("#nt-prov") && $("#nt-prov").value;
    if (pv) task.provider_pref = [pv];
    if (!task.title || !task.goal || !(task.files_to_create.length || task.files_to_modify.length)) {
      toast(t("nt.required"), "error"); return;
    }
    const r = await act("/api/c/dispatch", { tasks: [task] }, null);
    if (r && r.errors && r.errors.length) { toast(r.errors.map((e) => e.error).join("; "), "error"); return; }
    if (r) { toast((r.created || []).map((c) => c.id).join(", ") + " ✓", "ok"); closeModal(); }
  };
};
$("#btn-contract").onclick = async () => {
  const c = await api("/api/c/contract");
  modal(`<h2>${t("nt.contract")} <span class="pill">v${c.version}</span></h2>
    <p class="muted">${t("nt.contracthint")}</p>
    <textarea id="ct-text" rows="22" style="width:100%;font-family:monospace">${esc(c.text || "")}</textarea>
    <button class="btn primary" id="ct-save">${t("nt.save")}</button>`);
  $("#ct-save").onclick = async () => { await act("/api/c/contract", { text: $("#ct-text").value }, "OK"); closeModal(); };
};
$("#btn-check-game").onclick = async () => {
  toast("…", "info");
  try {
    const r = await api("/api/c/check", { body: {} });
    modal(`<h2>${t("nt.check")} ${r.ok ? '<span class="pill ok">OK</span>' : '<span class="pill bad">ERR</span>'}</h2>
      <pre class="out">${esc(JSON.stringify(r, null, 2))}</pre>`);
  } catch (e) { toast(e.message, "error"); }
};

// ------------------------------------------------------------------ polling
async function refresh() {
  try {
    S = await api("/api/p/state");
    failCount = 0;
    $("#banner").classList.add("hidden");
    render();
    alerts();
  } catch (e) {
    if (e.message === "unauthorized") return;
    failCount++;
    if (failCount > 2) { const b = $("#banner"); b.textContent = t("banner.down"); b.className = "banner bad"; }
  }
}

function alerts() {
  for (const ev of S.events.slice().reverse()) {
    if (ev.id <= lastEventId) continue;
    if (lastEventId && (ev.level === "error" || ev.kind === "claude_message" || ev.kind === "task_done" || ev.kind === "asset_request")) {
      const urgent = ev.kind === "claude_message" && (JSON.parse(ev.data_json || "{}").urgent);
      toast((ev.account ? ev.account + ": " : "") + ev.message, ev.level === "error" ? "error" : ev.kind === "task_done" ? "ok" : "info", urgent ? 15000 : 5000);
      beep(urgent ? "urgent" : ev.level === "error" ? "error" : "info");
    }
  }
  if (S.events.length) lastEventId = Math.max(lastEventId, S.events[0].id);
  for (const a of S.agents) {
    if (a.status === "captcha" && !seenAlerts.has("cap" + a.id)) { seenAlerts.add("cap" + a.id); beep("urgent"); toast(`${a.id}: ${t("st.captcha")}`, "error", 12000); }
    if (a.status !== "captcha") seenAlerts.delete("cap" + a.id);
  }
}

// ------------------------------------------------------------------ render
function render() {
  if (!S) return;
  renderChips();
  const n = S.agents.filter((a) => ["error", "logged_out", "captcha"].includes(a.status)).length;
  $("#tabs [data-tab=overview]").innerHTML = esc(t("tab.overview")) + (n ? `<span class="badge">${n}</span>` : "");
  const qa = S.assets.filter((a) => ["requested", "placeholder"].includes(a.status)).length;
  $("#tabs [data-tab=assets]").innerHTML = esc(t("tab.assets")) + (qa ? `<span class="badge">${qa}</span>` : "");
  $("#btn-pause").textContent = S.paused_all ? t("hdr.resume") : t("hdr.pause");
  $("#btn-pause").classList.toggle("on", !!S.paused_all);
  if (S.paused_all) { const b = $("#banner"); b.textContent = t("banner.paused"); b.className = "banner"; }
  if (TAB === "overview") { renderClaude(); renderQuestions(); renderHosts(); renderAgents(); }
  if (TAB === "tasks") renderTasks();
  if (TAB === "assets") renderAssets();
  if (TAB === "providers") renderProviders();
  if (TAB === "game") renderGame();
}

function renderChips() {
  const on = S.hosts.filter((h) => h.connected).length;
  const run = S.agents.filter((a) => a.host);
  const work = run.filter((a) => a.status === "working").length;
  const idle = run.filter((a) => a.status === "idle").length;
  const bad = S.agents.filter((a) => ["error", "logged_out", "captcha"].includes(a.status)).length;
  const tc = (st) => S.tasks.filter((x) => st.includes(x.status)).length;
  const claudeAgo = S.claude_last_seen ? S.now - S.claude_last_seen : null;
  $("#chips").innerHTML = [
    `🖥 ${t("chip.pcs")} <b>${on}/${S.hosts.length}</b>`,
    `🤖 ${t("chip.agents")} <b>${run.length}</b> · <span style="color:var(--info)">${work} ${t("st.working")}</span> · <span style="color:var(--ok)">${idle} ${t("st.idle")}</span>` + (bad ? ` · <span style="color:var(--bad)">${bad} ⚠</span>` : ""),
    `📋 ${t("chip.tasks")} <b>${tc(["queued", "waiting"])}</b> ${t("chip.queued")} · <b>${tc(["assigned", "running", "question", "revision"])}</b> ${t("chip.active")} · <b>${tc(["done"])}</b> ${t("chip.review")} · <b>${tc(["integrated"])}</b> ✓`,
    `🔤 <b>${fmtN(S.tokens.total)}</b> tok · ${fmtN(S.tokens.per_min)}/min`,
    `🧠 Claude: <b>${claudeAgo == null ? "–" : claudeAgo < 120 ? t("claude.active") : dur(claudeAgo) + " " + t("ago")}</b>`,
    `💾 ${S.last_backup ? dur(S.now - S.last_backup.mtime) + " " + t("ago") : "–"}`,
  ].map((h) => `<span class="chip">${h}</span>`).join("");
}

function renderClaude() {
  const msgs = (S.claude_messages || []).slice().reverse().slice(0, 6);
  const ago = S.claude_last_seen ? S.now - S.claude_last_seen : null;
  $("#claude-box").innerHTML =
    `<div class="muted">${t("claude.last")}: ${ago == null ? t("never") : dur(ago) + " " + t("ago")}</div>` +
    (msgs.length ? msgs.map((m) => `<div class="claude-msg ${m.urgent ? "urgent" : ""}"><span class="muted">${clock(m.ts)}</span> ${esc(m.text)}</div>`).join("")
      : `<div class="muted">${t("claude.none")}</div>`);
}

function renderQuestions() {
  $("#questions").innerHTML = S.questions.length ? S.questions.map((q) =>
    `<div class="q"><b>${esc(q.task_id)}</b> · ${esc(q.account_id)} · <span class="muted">${dur(S.now - q.created)}</span><br>${esc(q.question)}</div>`).join("")
    : `<div class="muted">${t("q.none")}</div>`;
}

function renderHosts() {
  if (!S.hosts.length) { $("#hosts").innerHTML = `<div class="card muted">${t("hosts.none")}</div>`; return; }
  $("#hosts").innerHTML = S.hosts.map((h) => {
    const s = h.sys || {};
    const ram = s.ram_used_pct ?? 0, cpu = s.cpu_pct ?? 0;
    const cls = (v) => v > 90 ? "b" : v > 75 ? "w" : "";
    const running = S.agents.filter((a) => a.host === h.name).length;
    const n = esc(h.name);
    return `<div class="card host ${h.connected ? "" : "off"}">
      <div class="top"><div><div class="name">${h.connected ? "🟢" : "🔴"} ${n}</div>
        <div class="muted" style="font-size:12px">${esc(h.ip || "")} · ${h.connected ? t("host.online") + " " + dur(S.now - h.connected_at) : t("host.offline") + " " + dur(h.since_seen)}</div></div>
        <div class="stepper" title="${t("host.desired")}">
          <button class="btn" data-host="${n}" data-delta="-1">−</button><b>${h.desired}</b>
          <button class="btn" data-host="${n}" data-delta="1">+</button></div></div>
      <div class="kv" style="margin-top:6px">
        <span>${t("host.running")}</span><b>${running} / ${h.desired} (${s.pages_open ?? 0} ${t("host.tabs")})</b>
        <span>RAM</span><b>${ram}% · ${s.ram_free_gb ?? "?"} GB ${t("free")}</b></div>
      <div class="meter ${cls(ram)}"><i style="width:${ram}%"></i></div>
      <div class="kv"><span>CPU</span><b>${cpu}% · ${s.cores ?? "?"} ${t("cores")}</b></div>
      <div class="meter ${cls(cpu)}"><i style="width:${cpu}%"></i></div>
      <div class="kv"><span>Godot</span><b>${s.godot ? "✓" : "✗ " + t("host.nogodot")}</b>
        <span>${t("host.mode")}</span><b><select data-mode="${n}">${["mini", "hidden", "headless"].map((m) => `<option ${(h.mode || s.mode) === m ? "selected" : ""}>${m}</option>`).join("")}</select></b>
        ${s.outbox ? `<span>${t("host.outbox")}</span><b style="color:var(--warn)">${s.outbox}</b>` : ""}</div>
      <div class="actions">
        <button class="btn sm" data-hact="restart" data-host="${n}">${t("host.restart")}</button>
        <button class="btn sm" data-hact="stop_all" data-host="${n}">${t("host.stopall")}</button>
        ${h.connected ? "" : `<button class="btn sm danger" data-hact="forget" data-host="${n}">${t("host.forget")}</button>`}
      </div></div>`;
  }).join("");
  $$("#hosts [data-delta]").forEach((b) => b.onclick = () => act(`/api/p/host/${encodeURIComponent(b.dataset.host)}`, { delta: +b.dataset.delta }));
  $$("#hosts [data-mode]").forEach((s) => s.onchange = () => act(`/api/p/host/${encodeURIComponent(s.dataset.mode)}`, { mode: s.value }, t("host.modeset")));
  $$("#hosts [data-hact]").forEach((b) => b.onclick = () => {
    if (b.dataset.hact !== "restart" && !confirm(t("confirm"))) return;
    act(`/api/p/host/${encodeURIComponent(b.dataset.host)}`, { action: b.dataset.hact }, "OK");
  });
}

function agentLine(a) {
  if (a.status === "working" && a.chat) return `📝 <b>${esc(a.chat.task)}</b> ${esc(a.chat.title || "")} · r${a.chat.rounds}`;
  if (a.status === "idle") {
    const s = a.idle_for ?? a.status_for;
    const c = s > 900 ? "b" : s > 300 ? "w" : "";
    return `${t("st.idle")} <span class="timer ${c}">${dur(s)}</span>`;
  }
  if (a.status === "cooldown") return `⏳ ${t("st.cooldown")} ${dur(a.cooldown_left)}`;
  if (a.status === "offline" && !a.has_state) return `🔑 ${t("agent.needlogin")}`;
  return esc(a.status_detail || t("st." + a.status));
}

function renderAgents() {
  const f = $("#agent-filter").value;
  let list = S.agents;
  if (f === "problem") list = list.filter((a) => ["error", "logged_out", "captcha", "cooldown"].includes(a.status) || !a.has_state);
  else if (f) list = list.filter((a) => a.status === f);
  if (!list.length) { $("#agents").innerHTML = `<div class="card muted">${t("agents.none")}</div>`; return; }
  $("#agents").innerHTML = list.map((a) => {
    const idle = a.status === "idle" ? (a.idle_for ?? a.status_for ?? 0) : 0;
    const ic = idle > 900 ? "idle-bad" : idle > 300 ? "idle-warn" : "";
    const ctx = a.chat ? Math.min(100, Math.round(100 * a.chat.ctx / (a.ctx_limit || 64000))) : 0;
    return `<div class="card agent st-${esc(a.status)} ${ic} ${a.paused ? "paused" : ""}" data-agent="${esc(a.id)}">
      <div class="top"><span class="id"><span class="dot"></span>${esc(a.id)}</span>
        <span class="prov">${esc(a.provider)}${a.provider_enabled ? "" : " (off)"} · ${esc(a.host || "–")}</span></div>
      <div class="line">${agentLine(a)}</div>
      <div class="line muted">✓${a.tasks_done} ✗${a.tasks_failed} · ${fmtN(a.tokens_in + a.tokens_out)} tok${a.owner ? " · " + esc(a.owner) : ""}</div>
      ${a.chat ? `<div class="ctx" title="context ${ctx}%"><i style="width:${ctx}%"></i></div>` : ""}
    </div>`;
  }).join("");
  $$("#agents [data-agent]").forEach((d) => d.onclick = () => openAgent(d.dataset.agent));
}

async function openAgent(id) {
  const a = S.agents.find((x) => x.id === id); if (!a) return;
  const B = (act, label, cls = "") => `<button class="btn sm ${cls}" data-aa="${act}">${label}</button>`;
  const hosts = S.hosts.filter((h) => h.connected).map((h) => `<option>${esc(h.name)}</option>`).join("");
  modal(`<h2>${esc(a.id)} <span class="pill">${esc(a.provider)}</span> <span class="pill ${a.status === "working" ? "info" : a.status === "idle" ? "ok" : "bad"}">${esc(t("st." + a.status))}</span></h2>
    <div class="kv"><span>${t("agent.host")}</span><b>${esc(a.host || "–")}</b><span>${t("agent.owner")}</span><b>${esc(a.owner)} ${esc(a.label)}</b>
      <span>${t("agent.login")}</span><b>${a.has_state ? "✓ " + (a.state_updated ? dur(S.now - a.state_updated) + " " + t("ago") : "") : "✗"}</b>
      <span>${t("agent.status")}</span><b>${esc(a.status_detail || "")} (${dur(a.status_for)})</b>
      <span>${t("agent.stats")}</span><b>✓${a.tasks_done} ✗${a.tasks_failed} · ${a.rounds_total} ${t("rounds")} · ${a.errors} ${t("errors")}</b>
      ${a.chat ? `<span>${t("agent.chat")}</span><b>${esc(a.chat.task)} · ${esc(a.chat.title || "")} · r${a.chat.rounds} · ctx ${fmtN(a.chat.ctx)}</b>` : ""}</div>
    ${a.status === "logged_out" || !a.has_state ? `<p class="err">${t("fix.logged_out")}</p>` : ""}
    ${a.status === "captcha" ? `<p class="err">${t("fix.captcha")}</p>` : ""}
    <div class="actions">
      ${B("screenshot", "📷 " + t("a.screenshot"))}${B("show", "🪟 " + t("a.show"))}${B("hide", t("a.hide"))}${B("reload", "↻ " + t("a.reload"))}
      ${B("restart", t("a.restart"))}${B(a.paused ? "resume" : "pause", a.paused ? "▶ " + t("a.resume") : "⏸ " + t("a.pause"))}
      ${B("login", "🔑 " + t("a.login"))}${a.cooldown_left ? B("clear_cooldown", t("a.clearcd")) : ""}
      ${B(a.enabled ? "disable" : "enable", a.enabled ? t("a.disable") : t("a.enable"))}${B("stop", t("a.stop"), "danger")}
      ${a.host ? "" : `<select id="start-host">${hosts}</select>${B("start", t("a.start"))}`}
      ${a.chat ? `<button class="btn sm" id="a-transcript">📜 ${t("a.transcript")}</button>` : ""}
    </div>
    <div id="agent-extra"></div><h3>${t("agent.events")}</h3><div id="agent-events" class="events"></div>`);
  $$("#modal [data-aa]").forEach((b) => b.onclick = async () => {
    const action = b.dataset.aa;
    const body = { action };
    if (action === "start") body.host = $("#start-host").value;
    if (action === "stop" && !confirm(t("confirm"))) return;
    const r = await act(`/api/p/agent/${encodeURIComponent(id)}`, body, action === "screenshot" ? null : "OK");
    if (action === "screenshot") {
      $("#agent-extra").innerHTML = r && r.ok !== false ? `<img class="shot" src="/api/p/screenshot/${encodeURIComponent(id)}?t=${Date.now()}">` : "";
    }
    if (action === "login" && r && r.ok) toast(t("a.loginopened", { host: r.host }), "ok", 9000);
  });
  if ($("#a-transcript")) $("#a-transcript").onclick = () => showTranscript(a.chat.id);
  const evs = await api(`/api/p/events?account=${encodeURIComponent(id)}&limit=40`);
  $("#agent-events").innerHTML = evs.map(evRow).join("") || `<div class="muted">–</div>`;
}

async function showTranscript(chatId) {
  const r = await api(`/api/p/chat/${encodeURIComponent(chatId)}/transcript`);
  $("#agent-extra").innerHTML = `<pre class="out" style="max-height:480px">${esc(r.text)}</pre>`;
}

// ------------------------------------------------------------------ tasks
const COLS = [
  ["queued", ["queued", "waiting"]], ["running", ["assigned", "running", "question"]], ["revision", ["revision"]],
  ["done", ["done"]], ["integrated", ["integrated"]], ["failed", ["failed", "cancelled"]],
];
function renderTasks() {
  $("#kanban").innerHTML = COLS.map(([name, sts]) => {
    const items = S.tasks.filter((x) => sts.includes(x.status)).sort((a, b) => a.priority - b.priority);
    return `<div class="col"><h3><span>${t("ts." + name)}</span><span class="muted">${items.length}</span></h3>${items.map((x) =>
      `<div class="task" data-task="${esc(x.id)}"><span class="tid">${esc(x.id)}</span> <span class="pill">P${x.priority}</span>
        ${x.status === "question" ? `<span class="pill warn">?</span>` : ""}${x.status === "waiting" ? `<span class="pill">⛓</span>` : ""}
        <div>${esc(x.title)}</div>
        <div class="muted">${x.agents.length ? "🤖 " + esc(x.agents.slice(-2).join(", ")) : ""} ${x.deps.length ? "⛓ " + esc(x.deps.join(",")) : ""}</div>
        ${x.error ? `<div class="err">${esc(x.error.slice(0, 120))}</div>` : ""}</div>`).join("")}</div>`;
  }).join("");
  $$("#kanban [data-task]").forEach((d) => d.onclick = () => openTask(d.dataset.task));
  renderGraph();
}

function renderGraph() {
  const tasks = S.tasks;
  if (!tasks.length) { $("#task-graph").innerHTML = `<div class="muted">${t("tasks.none")}</div>`; return; }
  const byId = Object.fromEntries(tasks.map((x) => [x.id, x]));
  const level = {};
  const lv = (id, seen = new Set()) => {
    if (level[id] != null) return level[id];
    if (seen.has(id)) return 0; seen.add(id);
    const deps = (byId[id]?.deps || []).filter((d) => byId[d]);
    return (level[id] = deps.length ? 1 + Math.max(...deps.map((d) => lv(d, seen))) : 0);
  };
  tasks.forEach((x) => lv(x.id));
  const cols = {};
  tasks.forEach((x) => (cols[level[x.id]] = cols[level[x.id]] || []).push(x));
  const W = 150, H = 34, GX = 40, GY = 10, pos = {};
  let maxRows = 0;
  Object.entries(cols).forEach(([l, arr]) => { maxRows = Math.max(maxRows, arr.length); arr.forEach((x, i) => (pos[x.id] = [10 + l * (W + GX), 10 + i * (H + GY)])); });
  const color = { integrated: "var(--ok)", done: "var(--info)", failed: "var(--bad)", cancelled: "var(--muted)", running: "var(--acc)", assigned: "var(--acc)", question: "var(--warn)", revision: "var(--violet)" };
  const w = 20 + Object.keys(cols).length * (W + GX), h = 20 + maxRows * (H + GY);
  let svg = `<svg viewBox="0 0 ${w} ${h}" style="min-width:${Math.min(w, 1600)}px">`;
  tasks.forEach((x) => (x.deps || []).forEach((d) => {
    if (!pos[d]) return;
    const [x1, y1] = pos[d], [x2, y2] = pos[x.id];
    svg += `<path d="M${x1 + W},${y1 + H / 2} C${x1 + W + 20},${y1 + H / 2} ${x2 - 20},${y2 + H / 2} ${x2},${y2 + H / 2}" fill="none" stroke="var(--muted)" stroke-width="1.2"/>`;
  }));
  tasks.forEach((x) => {
    const [px, py] = pos[x.id];
    svg += `<g data-task="${esc(x.id)}" style="cursor:pointer"><rect x="${px}" y="${py}" width="${W}" height="${H}" rx="6" fill="var(--card)" stroke="${color[x.status] || "var(--line)"}" stroke-width="2"/>
      <text x="${px + 7}" y="${py + 14}" font-weight="700">${esc(x.id)} · ${esc(t("tst." + x.status))}</text>
      <text x="${px + 7}" y="${py + 27}">${esc((x.title || "").slice(0, 24))}</text></g>`;
  });
  $("#task-graph").innerHTML = svg + "</svg>";
  $$("#task-graph [data-task]").forEach((g) => g.onclick = () => openTask(g.dataset.task));
}

async function openTask(id) {
  const x = await api(`/api/p/task/${encodeURIComponent(id)}`);
  const sp = x.spec || {};
  modal(`<h2>${esc(x.id)} · ${esc(x.title)} <span class="pill">${esc(x.status)}</span></h2>
    <div class="actions">
      ${["done", "failed", "revision"].includes(x.status) || x.chats.some((c) => c.result_version > 0) ?
        `<button class="btn sm primary" data-int="0">${t("nt.integrate")}</button>
         <button class="btn sm" data-int="1" title="${esc(t("nt.forcehint"))}">${t("nt.force")}</button>` : ""}
      <button class="btn sm" data-ta="reassign">${t("t.reassign")}</button>
      <button class="btn sm" data-ta="requeue">${t("t.requeue")}</button>
      <button class="btn sm danger" data-ta="cancel">${t("t.cancel")}</button>
      <label class="muted">P <select id="t-prio">${[1, 2, 3, 4, 5].map((p) => `<option ${p === x.priority ? "selected" : ""}>${p}</option>`).join("")}</select></label>
    </div>
    ${x.error ? `<p class="err">${esc(x.error)}</p>` : ""}
    <div class="kv"><span>${t("t.goal")}</span><b>${esc(sp.goal || "")}</b>
      <span>${t("t.files")}</span><b>${esc((sp.files_to_create || []).concat(sp.files_to_modify || []).join(", "))}</b>
      <span>${t("t.deps")}</span><b>${esc((x.deps || []).join(", ") || "–")}</b>
      <span>${t("t.attempts")}</span><b>${x.attempts}</b></div>
    <h3>${t("t.chats")}</h3>
    <table><tr><th>chat</th><th>${t("agent")}</th><th>${t("status")}</th><th>${t("rounds")}</th><th>tok</th><th></th></tr>
    ${x.chats.map((c) => `<tr><td>${esc(c.id)}</td><td>${esc(c.account_id)}@${esc(c.host || "")}</td><td>${esc(c.status)} ${c.error ? `<span class="err">${esc(c.error.slice(0, 80))}</span>` : ""}</td>
      <td class="n">${c.rounds}</td><td class="n">${fmtN(c.tokens_in + c.tokens_out)}</td>
      <td><button class="btn sm" data-tr="${esc(c.id)}">📜</button></td></tr>`).join("")}</table>
    <div id="agent-extra"></div>
    ${x.questions.length ? `<h3>${t("q.title")}</h3>` + x.questions.map((q) => `<div class="q">${esc(q.question)}<br><b>→</b> ${esc(q.answer || "…")}</div>`).join("") : ""}
    ${x.report ? `<h3>REPORT.md</h3><pre class="out" style="max-height:420px">${esc(x.report)}</pre>` : ""}
    <h3>${t("nt.followup")}</h3>
    <textarea id="t-follow" rows="3" style="width:100%" placeholder="${esc(t("nt.followhint"))}"></textarea>
    <button class="btn sm" id="t-follow-send">${t("nt.send")}</button>`);
  $$("#modal [data-int]").forEach((b) => b.onclick = async () => {
    const force = b.dataset.int === "1";
    if (force && !confirm(t("confirm"))) return;
    const r = await act("/api/c/integrate", { task_id: id, force }, null);
    if (r) modal(`<h2>${esc(id)} · ${t("nt.integrate")}</h2><pre class="out">${esc(JSON.stringify(r, null, 2))}</pre>`);
  });
  $("#t-follow-send").onclick = async () => {
    const msg = $("#t-follow").value.trim(); if (!msg) return;
    await act("/api/c/followup", { task_id: id, message: msg }, "OK"); closeModal();
  };
  $$("#modal [data-ta]").forEach((b) => b.onclick = async () => {
    if (b.dataset.ta === "cancel" && !confirm(t("confirm"))) return;
    await act(`/api/p/task/${encodeURIComponent(id)}`, { action: b.dataset.ta }, "OK"); closeModal();
  });
  $("#t-prio").onchange = (e) => act(`/api/p/task/${encodeURIComponent(id)}`, { action: "priority", value: +e.target.value });
  $$("#modal [data-tr]").forEach((b) => b.onclick = () => showTranscript(b.dataset.tr));
}

// ------------------------------------------------------------------ assets
function renderAssets() {
  if (!S.assets.length) { $("#assets").innerHTML = `<div class="card muted">${t("assets.none")}</div>`; return; }
  const who = store.get("who", "");
  $("#assets").innerHTML = S.assets.map((a) => {
    const st = { delivered: "ok", placeholder: "warn", claimed: "info", requested: "bad" }[a.status] || "";
    const isImg = a.kind === "image" || /\.(png|jpg|webp)$/i.test(a.path);
    return `<div class="card asset">
      <div class="top" style="display:flex;justify-content:space-between;gap:6px"><span class="path">${esc(a.path)}</span>
        <span class="pill ${st}">${esc(t("as." + a.status))}</span></div>
      <div style="margin:6px 0">${esc(a.spec)}</div>
      <div class="muted">${a.width ? `${a.width}×${a.height}px` : ""}${a.frames > 1 ? ` · ${a.frames} ${t("frames")}` : ""} · P${a.priority}${a.for_task ? " · " + esc(a.for_task) : ""}${a.claimed_by ? " · 👤 " + esc(a.claimed_by) : ""}</div>
      ${a.notes ? `<div class="err">${esc(a.notes)}</div>` : ""}
      ${isImg && a.status !== "requested" ? `<img src="/api/p/game/raw?path=${encodeURIComponent(a.path)}&t=${Math.floor(a.delivered || a.created)}" alt="" onerror="this.remove()">` : ""}
      <div class="actions">
        <button class="btn sm" data-claim="${esc(a.id)}">${a.claimed_by ? t("as.unclaim") : t("as.claim")}</button>
        <label class="btn sm">⬆ ${t("as.upload")}<input type="file" hidden data-up="${esc(a.id)}"></label>
      </div></div>`;
  }).join("");
  $$("#assets [data-claim]").forEach((b) => b.onclick = () => {
    const a = S.assets.find((x) => x.id === b.dataset.claim);
    let name = "";
    if (!a.claimed_by) { name = prompt(t("as.who"), who) || ""; if (!name) return; store.set("who", name); }
    act(`/api/p/asset/${encodeURIComponent(a.id)}/claim`, { who: name });
  });
  $$("#assets [data-up]").forEach((inp) => inp.onchange = async () => {
    const fd = new FormData(); fd.append("file", inp.files[0]); fd.append("who", store.get("who", ""));
    try {
      const r = await api(`/api/p/asset/${encodeURIComponent(inp.dataset.up)}/upload`, { form: fd, method: "POST" });
      toast(r.ok === false ? r.error : t("as.delivered") + (r.warnings && r.warnings.length ? " ⚠ " + r.warnings.join("; ") : ""), r.ok === false ? "error" : "ok", 8000);
      refresh();
    } catch (e) { toast(e.message, "error"); }
  });
}
const dz = $("#dropzone");
["dragenter", "dragover"].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => dz.addEventListener(e, (ev) => { ev.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", async (ev) => {
  for (const f of ev.dataTransfer.files) {
    const fd = new FormData(); fd.append("file", f);
    try { await api("/api/p/asset_drop", { form: fd, method: "POST" }); toast(f.name + " ✓", "ok"); } catch (e) { toast(f.name + ": " + e.message, "error"); }
  }
  refresh();
});

// ------------------------------------------------------------------ tokens
async function loadTokens() {
  const d = await api("/api/p/tokens?minutes=120");
  const w = 760, h = 170, n = d.series.length, max = Math.max(1, ...d.series, ...d.series_claude);
  const pts = (arr) => arr.map((v, i) => `${(i / (n - 1)) * w},${h - (v / max) * (h - 12)}`).join(" ");
  $("#tok-chart").innerHTML = `<svg viewBox="0 0 ${w} ${h + 18}" style="width:100%">
    <polyline points="0,${h} ${pts(d.series)} ${w},${h}" fill="var(--acc)" fill-opacity=".18" stroke="var(--acc)" stroke-width="1.5"/>
    <polyline points="${pts(d.series_claude)}" fill="none" stroke="var(--warn)" stroke-width="1.5"/>
    <text x="0" y="${h + 15}" fill="var(--muted)" font-size="11">-2h</text><text x="${w - 30}" y="${h + 15}" fill="var(--muted)" font-size="11">${t("now")}</text>
    <text x="4" y="12" fill="var(--muted)" font-size="11">max ${fmtN(max)}/min · <tspan fill="var(--acc)">agents</tspan> · <tspan fill="var(--warn)">Claude</tspan></text></svg>`;
  $("#tok-totals").innerHTML = `<div class="kv"><span>${t("tok.all")}</span><b>${fmtN(S.tokens.total)}</b>
    <span>${t("tok.in")}</span><b>${fmtN(S.tokens.in)}</b><span>${t("tok.out")}</span><b>${fmtN(S.tokens.out)}</b>
    <span>Claude</span><b>${fmtN(S.tokens.claude)}</b><span>${t("tok.avg")}</span><b>${fmtN(d.avg_per_done)}</b></div>
    <p class="muted" style="font-size:12px">${t("tok.note")}</p>`;
  const amax = Math.max(1, ...d.per_agent.map((a) => a.tokens_in + a.tokens_out));
  $("#tok-agents").innerHTML = `<table><tr><th>${t("agent")}</th><th></th><th>in</th><th>out</th><th>✓</th><th>${t("tok.pertask")}</th></tr>` +
    d.per_agent.map((a) => `<tr><td>${esc(a.id)} <span class="muted">${esc(a.provider)}</span></td>
      <td style="width:30%"><div class="bar" style="width:${(100 * (a.tokens_in + a.tokens_out)) / amax}%"></div></td>
      <td class="n">${fmtN(a.tokens_in)}</td><td class="n">${fmtN(a.tokens_out)}</td><td class="n">${a.tasks_done}</td>
      <td class="n">${a.tasks_done ? fmtN(Math.round((a.tokens_in + a.tokens_out) / a.tasks_done)) : "–"}</td></tr>`).join("") + "</table>";
  const tbl = (rows, key) => `<table>${rows.map((r) => `<tr><td>${esc(r[key] || "–")}</td><td class="n">${fmtN((r.tin || 0) + (r.tout || 0))}</td></tr>`).join("")}</table>`;
  $("#tok-prov").innerHTML = tbl(d.per_provider, "provider") + "<br>" + tbl(d.per_host, "host") +
    `<h3>${t("tok.toptasks")}</h3><table>${d.top_tasks.map((r) => `<tr><td>${esc(r.task_id)}</td><td class="n">${fmtN(r.total)}</td><td class="n">${r.rounds} r</td></tr>`).join("")}</table>`;
}

// ------------------------------------------------------------------ providers
function renderProviders() {
  const sel = $("#sel-agent"), cur = sel.value;
  sel.innerHTML = S.agents.filter((a) => a.host).map((a) => `<option ${a.id === cur ? "selected" : ""}>${esc(a.id)}</option>`).join("") || `<option value="">${t("sel.noagent")}</option>`;
  if (document.activeElement && document.activeElement.closest && document.activeElement.closest("#providers")) return; // don't wipe edits
  $("#providers").innerHTML = S.providers.map((p) => `<div class="card provider" data-p="${esc(p.id)}">
    <div style="display:flex;justify-content:space-between;align-items:center"><h2 style="margin:0">${esc(p.id)}</h2>
      <label class="switch"><input type="checkbox" data-pen="${esc(p.id)}" ${p.enabled ? "checked" : ""}><span></span></label></div>
    <div class="muted">${p.accounts} ${t("prov.accounts")} · ${S.provider_counts[p.id] || 0} ${t("prov.running")} · ctx ${fmtN(p.context_tokens)} · <a href="${esc(p.home_url)}" target="_blank" rel="noopener">${esc(p.home_url)}</a></div>
    ${p.notes ? `<div class="muted" style="font-size:12px">${esc(p.notes)}</div>` : ""}
    <details><summary>${t("prov.selectors")}</summary><div class="sel">
      ${Object.entries(p.selectors).map(([k, v]) => `<span>${esc(k)}</span><input data-sk="${esc(k)}" value="${esc(v)}">`).join("")}</div>
      <div class="actions"><button class="btn sm primary" data-psave="${esc(p.id)}">${t("prov.save")}</button></div></details>
  </div>`).join("");
  $$("#providers [data-pen]").forEach((c) => c.onchange = () => act(`/api/p/provider/${c.dataset.pen}`, { enabled: c.checked }, c.dataset.pen + (c.checked ? " ON" : " OFF")));
  $$("#providers [data-psave]").forEach((b) => b.onclick = () => {
    const card = b.closest("[data-p]"); const selectors = {};
    $$("[data-sk]", card).forEach((i) => (selectors[i.dataset.sk] = i.value));
    act(`/api/p/provider/${b.dataset.psave}`, { selectors }, t("prov.saved"));
    b.blur();
  });
}
$("#sel-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api("/api/p/selector_test", { body: { account: $("#sel-agent").value, selector: $("#sel-input").value } }).catch((er) => ({ error: er.message }));
  $("#sel-out").textContent = JSON.stringify(r, null, 1);
});
$("#sel-shot").onclick = async () => {
  const id = $("#sel-agent").value; if (!id) return;
  const r = await act(`/api/p/agent/${encodeURIComponent(id)}`, { action: "screenshot" });
  if (r && r.ok !== false) modal(`<img class="shot" src="/api/p/screenshot/${encodeURIComponent(id)}?t=${Date.now()}">`);
};

// ------------------------------------------------------------------ game & backups
function renderGame() {
  const c = S.last_check || {};
  $("#game-info").innerHTML = `<div class="kv"><span>${t("game.dir")}</span><b>${esc(S.game_dir)}</b>
    <span>Godot</span><b>${esc(S.godot || "✗ " + t("host.nogodot"))}</b>
    <span>${t("game.lastcheck")}</span><b>${c.ts ? (c.ok ? "✓ OK" : `✗ ${c.errors ? c.errors.length : "?"} ${t("errors")}`) + " · " + dur(S.now - c.ts) + " " + t("ago") : "–"}</b>
    <span>${t("game.integrated")}</span><b>${S.tasks.filter((x) => x.status === "integrated").length} / ${S.tasks.length}</b></div>`;
}
$("#btn-check").onclick = async () => {
  $("#game-out").textContent = "…";
  try { const r = await api("/api/p/game/check", { body: {} }); $("#game-out").textContent = (r.ok ? "OK\n" : "FAILED\n") + (r.errors || []).join("\n") + (r.output ? "\n\n" + r.output : ""); }
  catch (e) { $("#game-out").textContent = e.message; }
};
$("#btn-tree").onclick = async () => {
  const r = await api("/api/p/game/tree");
  $("#game-out").textContent = Array.isArray(r) ? r.map((f) => `${(f.path || f).padEnd(50)} ${f.owner || ""}`).join("\n") : (r.text || JSON.stringify(r, null, 1));
};
async function loadBackups() {
  const list = await api("/api/p/backups");
  $("#backups").innerHTML = `<table>${list.slice(0, 40).map((b) => `<tr><td>${esc(b.name)}</td><td class="n">${(b.size / 1e6).toFixed(1)} MB</td>
    <td>${dur(Date.now() / 1000 - b.mtime)} ${t("ago")}</td><td><a class="btn sm" href="/api/p/backup/${encodeURIComponent(b.name)}">⬇</a>
    <button class="btn sm danger" data-restore="${esc(b.name)}">${t("bk.restore")}</button></td></tr>`).join("")}</table>`;
  $$("#backups [data-restore]").forEach((b) => b.onclick = async () => {
    if (!confirm(t("bk.confirm", { name: b.dataset.restore }))) return;
    await act("/api/p/backup", { restore: b.dataset.restore }, t("bk.restored")); loadBackups();
  });
}
$("#btn-backup").onclick = async () => { await act("/api/p/backup", {}, t("bk.done")); loadBackups(); };
$("#btn-diag").onclick = (e) => { e.preventDefault(); location.href = "/api/p/diagnostics"; };

// ------------------------------------------------------------------ events
function evRow(e) {
  const hint = fixHint(e.code);
  return `<div class="ev ${esc(e.level)}"><span class="t">${clock(e.ts)}</span><span>${esc(e.level)}</span>
    <span>${e.code ? `<span class="code">${esc(e.code)}</span> ` : ""}${e.account ? `<b>${esc(e.account)}</b> ` : ""}${e.host ? `<span class="muted">@${esc(e.host)}</span> ` : ""}${e.task ? `<span class="pill">${esc(e.task)}</span> ` : ""}${esc(e.message)}
    ${hint ? `<div class="fix">💡 ${esc(hint)}</div>` : ""}</span></div>`;
}
async function loadEvents() {
  const p = new URLSearchParams({ level: $("#ev-level").value, q: $("#ev-q").value, account: $("#ev-agent").value, task: $("#ev-task").value, limit: 400 });
  const evs = await api("/api/p/events?" + p);
  $("#events").innerHTML = evs.map(evRow).join("") || `<div class="muted">–</div>`;
}
$("#ev-form").addEventListener("submit", (e) => { e.preventDefault(); loadEvents(); });

// ------------------------------------------------------------------ help
function renderHelp() {
  const codes = Object.keys(FIX).map((c) => `<tr><td class="code" style="font-family:Consolas">${c}</td><td>${esc(t(FIX[c]))}</td></tr>`).join("");
  $("#help").innerHTML = `<h2>${t("help.title")}</h2><div>${t("help.body")}</div><h3>${t("help.codes")}</h3><table>${codes}</table>`;
}

// ------------------------------------------------------------------ boot
(async function boot() {
  document.documentElement.dataset.theme = store.get("theme", matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  $("#btn-sound").textContent = SOUND ? "🔔" : "🔕";
  const qs = new URLSearchParams(location.search);
  if (qs.get("token")) {
    document.cookie = `mc_token=${encodeURIComponent(qs.get("token"))}; path=/; max-age=${86400 * 30}; SameSite=Strict`;
    history.replaceState(null, "", "/");
  }
  await loadLang(LANG);
  try { await api("/api/p/state"); $("#app").classList.remove("hidden"); setTab(TAB); refresh(); }
  catch (e) { if (e.message !== "unauthorized") { $("#app").classList.remove("hidden"); setTab(TAB); } }
  setInterval(() => { if (!$("#app").classList.contains("hidden")) refresh(); }, 1500);
  setInterval(() => { if (TAB === "tokens") loadTokens().catch(() => {}); if (TAB === "events") loadEvents().catch(() => {}); }, 10000);
})();
