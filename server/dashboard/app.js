/* Likelyread dashboard. Plain JS, no dependencies. All data comes from the
 * authenticated API; the token never leaves this origin. */
"use strict";

const TOKEN_KEY = "likelyread.token";
const NO_CAMPAIGN = "__none__";
const state = { token: null, campaign: "", sender: "", campaigns: [], senders: [], config: { mode: "selfhost" }, me: null, agentToken: null, mcpClient: "claude" };
const isCloud = () => state.config.mode === "cloud";
const can = (feature) => !state.me || !state.me.features || state.me.features[feature] !== false;

// ---------------------------------------------------------------- helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style") Object.assign(el.style, v); // CSSOM, allowed under a strict CSP
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const svgNS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...children) {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  for (const c of children.flat()) if (c) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

const pct = (x) => `${Math.round((x || 0) * 100)}%`;
const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const fmtDay = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" });

function relTime(ts) {
  if (!ts) return "never";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  if (d < 30 * 86400) return `${Math.floor(d / 86400)} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

function duration(sec) {
  if (sec === null || sec === undefined) return "n/a";
  if (sec < 60) return `${Math.round(sec)} s`;
  if (sec < 3600) return `${Math.round(sec / 60)} min`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)} h`;
  return `${(sec / 86400).toFixed(1)} d`;
}

const campaignLabel = (name) => (name === null || name === undefined || name === "" ? "No campaign" : name);
const campaignParam = (name) => (name === null || name === undefined || name === "" ? NO_CAMPAIGN : name);
const campaignTag = (name) => h("span", { class: `tag${name ? "" : " none"}` }, campaignLabel(name));
const filters = () => ({ campaign: state.campaign, sender: state.sender });

function statusChip(e) {
  if (e.status === "opened") return h("span", { class: "chip open", title: `Last open ${fmtDate(e.last_open)}` }, `Opened ×${e.opens}`, h("span", { class: "chip-sub" }, relTime(e.last_open)));
  if (e.status === "prefetch_only") return h("span", { class: "chip dim", title: "Apple Mail loaded the image on delivery" }, "Prefetch only");
  return h("span", { class: "chip" }, "Not opened");
}

// ---------------------------------------------------------------- API

class AuthError extends Error {}
class ProError extends Error {}

async function api(path, opts = {}) {
  const headers = { Authorization: "Bearer " + state.token };
  if (opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) throw new AuthError("Invalid token");
  if (res.status === 402) throw new ProError("This needs Likelyread Pro");
  if (!res.ok) {
    let detail = "";
    try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : ""; } catch { /* not JSON */ }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  const data = await res.json();
  return { data, total: Number(res.headers.get("X-Total-Count") || 0) };
}

const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== "" && v !== null && v !== undefined)).toString();

// ---------------------------------------------------------------- auth

function loadToken() {
  return sessionStorage.getItem(TOKEN_KEY) || localStorage.getItem(TOKEN_KEY);
}

function showLogin(message) {
  document.getElementById("shell").hidden = true;
  document.getElementById("login").hidden = false;
  document.getElementById("login-error").textContent = message || "";
  const cloud = isCloud();
  document.getElementById("login-cloud").hidden = !cloud;
  document.getElementById("login-token").hidden = cloud;
  document.getElementById("token-input").required = !cloud;
  if (!cloud) document.getElementById("token-input").focus();
}

function logout() {
  sessionStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_KEY);
  state.token = null;
  state.me = null;
  state.agentToken = null;
  showLogin();
}

async function login(token, remember) {
  state.token = token;
  await api("/api/ping");
  state.me = null;
  sessionStorage.setItem(TOKEN_KEY, token);
  if (remember) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

// ---------------------------------------------------------------- filters

async function loadMe() {
  if (state.me) return state.me;
  const { data } = await api("/api/me");
  state.me = data;
  document.querySelector('[data-tab="account"]').hidden = !isCloud();
  document.querySelector('[data-tab="admin"]').hidden = !data.admin;
  return data;
}

async function loadFilters() {
  await loadMe();
  if (!can("campaigns")) {
    // Free plan: emails can carry a campaign, the analytics are Pro.
    const { data: names } = await api("/api/campaign-names");
    state.campaigns = names.map((name) => ({ name }));
    state.senders = [];
    state.campaign = "";
    state.sender = "";
    document.getElementById("campaign-filter").hidden = true;
    document.getElementById("sender-filter").hidden = true;
    return;
  }
  document.getElementById("campaign-filter").hidden = false;
  const [{ data: campaigns }, { data: senders }] = await Promise.all([api("/api/campaigns"), api("/api/senders")]);
  state.campaigns = campaigns;
  state.senders = senders;
  const csel = document.getElementById("campaign-filter");
  csel.replaceChildren(
    h("option", { value: "" }, "All campaigns"),
    ...campaigns.map((c) => h("option", { value: campaignParam(c.name) }, `${campaignLabel(c.name)} (${c.emails})`)),
  );
  if (state.campaign && !campaigns.some((c) => campaignParam(c.name) === state.campaign)) state.campaign = "";
  csel.value = state.campaign;

  const ssel = document.getElementById("sender-filter");
  ssel.replaceChildren(h("option", { value: "" }, "All addresses"), ...senders.map((x) => h("option", { value: x.sender }, `${x.sender} (${x.emails})`)));
  if (state.sender && !senders.some((x) => x.sender === state.sender)) state.sender = "";
  ssel.value = state.sender;
  ssel.hidden = senders.length < 2 && !state.sender;
}

// ---------------------------------------------------------------- charts

function barChart(points, series, { height = 200, width = 760 } = {}) {
  const pad = { l: 26, r: 4, t: 8, b: 22 };
  const max = Math.max(1, ...points.flatMap((p) => series.map((sr) => p[sr.key])));
  const innerH = height - pad.t - pad.b;
  const slot = (width - pad.l - pad.r) / points.length;
  const bw = Math.max(2, (slot - 3) / series.length);
  const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart", role: "img" });
  for (const frac of [0, 0.5, 1]) {
    const y = pad.t + innerH * (1 - frac);
    svg.append(s("line", { x1: pad.l, x2: width - pad.r, y1: y, y2: y, class: "grid" }));
    svg.append(s("text", { x: pad.l - 6, y: y + 4, class: "axis", "text-anchor": "end" }, Math.round(max * frac)));
  }
  points.forEach((p, i) => {
    const x0 = pad.l + i * slot + 1.5;
    series.forEach((sr, j) => {
      const v = p[sr.key];
      const bh = (v / max) * innerH;
      const rect = s("rect", { x: x0 + j * bw, y: pad.t + innerH - bh, width: Math.max(1, bw - 1), height: Math.max(0, bh), class: sr.cls });
      rect.append(s("title", {}, `${p.label}: ${v} ${sr.name}`));
      svg.append(rect);
    });
    const every = Math.ceil(points.length / 8);
    if (i % every === 0 && p.short) svg.append(s("text", { x: x0 + (slot - 3) / 2, y: height - 5, class: "axis", "text-anchor": "middle" }, p.short));
  });
  return svg;
}

const legend = (items) => h("div", { class: "legend" }, items.map(([cls, label]) => h("span", {}, h("i", { class: `sw ${cls}` }), label)));
const tile = (value, label, sub) => h("div", { class: "tile" }, h("div", { class: "tile-value" }, value), h("div", { class: "tile-label" }, label, sub ? h("small", {}, sub) : null));
const sectionHead = (title, ...right) => h("div", { class: "section-head" }, h("h2", {}, title), ...right);
const rateCell = (x) => h("div", { class: "rate-cell" }, h("div", { class: "rate-bar" }, h("div", { class: "rate-fill", style: { width: pct(x) } })), pct(x));

/** Previous and Next under a list shown limit items at a time; go({ page }) moves. */
function pager(page, limit, total, go) {
  if (total <= limit && page === 0) return null;
  const pages = Math.max(1, Math.ceil(total / limit));
  return h("div", { class: "pager" },
    h("button", { class: "btn small", type: "button", disabled: page === 0, onclick: () => go({ page: Math.min(page - 1, pages - 1) }) }, "Previous"),
    h("span", { class: "muted small" }, `Page ${page + 1} of ${pages}`),
    h("button", { class: "btn small", type: "button", disabled: (page + 1) * limit >= total, onclick: () => go({ page: page + 1 }) }, "Next"));
}

// ---------------------------------------------------------------- views

async function viewOverview(root) {
  const tz = -new Date().getTimezoneOffset();
  const { data: st } = await api(`/api/stats?${qs({ ...filters(), days: 30, tz })}`);
  const hourPoints = st.opens_by_hour.map((v, i) => ({ label: `${i}:00`, short: i % 6 === 0 ? `${i}h` : "", opens: v }));
  const dayPoints = st.timeline.map((d) => ({ label: fmtDay(d.date), short: fmtDay(d.date), sent: d.sent, opens: d.opens }));
  const bestHour = st.opens_by_hour.some((v) => v > 0) ? st.opens_by_hour.indexOf(Math.max(...st.opens_by_hour)) : null;
  const scope = [state.campaign ? campaignLabel(state.campaign === NO_CAMPAIGN ? null : state.campaign) : "", state.sender].filter(Boolean).join(", from ");

  root.append(
    h("section", { class: "headline" },
      h("h1", {}, `${st.opened} of ${st.emails} emails`, h("br"), h("span", { class: "rate" }, "opened")),
      h("p", {}, `${pct(st.open_rate)} open rate${scope ? ` for ${scope}` : ""}. ${st.prefetch_only} only prefetched by Apple and ${st.self} of your own views were set aside.`),
    ),
    h("section", { class: "tiles" },
      tile(st.opens, "Opens in total", `${st.opens_last_7d} in the last 7 days`),
      tile(duration(st.median_time_to_open), "Median time to first open"),
      tile(st.prefetch_only, "Prefetch only", "Apple Mail, not counted"),
      tile(bestHour === null ? "n/a" : `${bestHour}:00`, "Busiest hour for opens", "your local time"),
    ),
    h("section", { class: "split" },
      h("div", {},
        sectionHead("Last 30 days", legend([["sent", "Sent"], ["opens", "Opens"]])),
        barChart(dayPoints, [{ key: "sent", name: "sent", cls: "sent" }, { key: "opens", name: "opens", cls: "opens" }], { height: 220, width: 760 }),
        h("div", { style: { marginTop: "40px" } },
          sectionHead("Opens by hour"),
          barChart(hourPoints, [{ key: "opens", name: "opens", cls: "opens" }], { height: 160, width: 760 })),
      ),
      h("div", {},
        sectionHead("Recent opens"),
        st.recent_opens.length
          ? h("ul", { class: "bands" }, st.recent_opens.slice(0, 10).map((r) =>
              h("li", {},
                h("a", { href: `#/email/${r.email_id}` }, r.subject || "(no subject)"),
                h("span", { class: "when", title: fmtDate(r.ts) }, relTime(r.ts)),
                h("span", { class: "sub" }, [r.recipients && r.recipients[0], r.via === "gmail_proxy" ? "via Gmail" : "direct", campaignLabel(r.campaign)].filter(Boolean).join(" · ")),
              )))
          : h("p", { class: "empty" }, "No opens yet."),
      ),
    ),
  );
}

/** Free plan overview, from the email list alone. Charts are Pro. */
async function viewOverviewFree(root) {
  const { data, total } = await api("/api/emails?limit=1000");
  if (!total) return root.append(emptyState());
  const opened = data.filter((e) => e.opens > 0);
  const recent = [...opened].sort((a, b) => b.last_open - a.last_open).slice(0, 10);
  root.append(
    h("section", { class: "headline" },
      h("h1", {}, `${opened.length} of ${data.length} emails`, h("br"), h("span", { class: "rate" }, "opened")),
      h("p", {}, `In the last ${state.me.features.history_days} days. Every open is listed with its time and how it was loaded.`),
    ),
    h("section", { class: "split" },
      upsell("charts", "See what works", ["Open rate per campaign and per sending address", "Sender health: setup checks, a safe daily pace, spam-folder alerts", "30-day timeline and busiest hours", "Your full history, not only 30 days"]),
      h("div", {},
        sectionHead("Recent opens"),
        recent.length
          ? h("ul", { class: "bands" }, recent.map((e) =>
              h("li", {},
                h("a", { href: `#/email/${e.id}` }, e.subject || "(no subject)"),
                h("span", { class: "when", title: fmtDate(e.last_open) }, relTime(e.last_open)),
                h("span", { class: "sub" }, [e.recipients[0], `${e.opens} open${e.opens === 1 ? "" : "s"}`].filter(Boolean).join(" · ")),
              )))
          : h("p", { class: "empty" }, "No opens yet."),
      ),
    ),
  );
}

function upsell(feature, title, points) {
  const me = state.me || {};
  const prices = me.prices || state.config.prices || {};
  return h("div", { class: "upsell", dataset: { feature } },
    h("p", { class: "eyebrow" }, "Likelyread Pro"),
    h("h2", {}, title),
    h("ul", {}, points.map((p) => h("li", {}, p))),
    h("p", { class: "muted small" }, `${prices.pro_monthly || "€5"} a month, or ${prices.pro_yearly || "€36"} a year, plus VAT.`),
    h("a", { class: "btn primary", href: "#/account" }, "Upgrade"),
  );
}

async function viewCampaigns(root) {
  if (!can("campaigns")) {
    root.append(h("section", { class: "headline" },
      h("h1", {}, "Campaigns", h("br"), h("span", { class: "rate" }, "on Pro")),
      h("p", {}, "You can already pick a campaign in Gmail's compose window. Pro shows how each one performs.")),
      upsell("campaigns", "Compare your campaigns", ["Open rate, opens and time to open per campaign", "Filter every view by campaign or sending address", "Rename and merge campaigns", "Unlimited history"]));
    return;
  }
  const data = state.campaigns;
  if (!data.length) return root.append(emptyState());
  const best = data.filter((c) => c.emails >= 3).sort((a, b) => b.open_rate - a.open_rate)[0];
  root.append(
    h("section", {},
      sectionHead(`${data.length} campaign${data.length === 1 ? "" : "s"}`, best ? h("span", { class: "muted" }, `Best open rate: ${campaignLabel(best.name)}, ${pct(best.open_rate)}`) : null),
      h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, ["Campaign", "Emails", "Opened", "Open rate", "Opens", "Time to open", "Last open", "Last sent", ""].map((t) => h("th", {}, t)))),
          h("tbody", {}, data.map((c) =>
            h("tr", { class: "clickable", onclick: () => { location.hash = `#/emails?${qs({ campaign: campaignParam(c.name) })}`; } },
              h("td", { class: "strong" }, campaignLabel(c.name)),
              h("td", { class: "num" }, c.emails),
              h("td", { class: "num" }, c.opened, c.prefetch_only ? h("span", { class: "muted small" }, ` +${c.prefetch_only} prefetch`) : null),
              h("td", {}, rateCell(c.open_rate)),
              h("td", { class: "num" }, c.opens),
              h("td", {}, duration(c.median_time_to_open)),
              h("td", {}, relTime(c.last_open)),
              h("td", {}, relTime(c.last_sent)),
              h("td", {}, c.name ? h("button", { class: "btn ghost small", type: "button", onclick: (ev) => { ev.stopPropagation(); renameCampaign(c.name); } }, "Rename") : null),
            ))),
        ),
      ),
      h("p", { class: "muted small", style: { marginTop: "16px" } }, "Pick a campaign from the menu next to the eye in Gmail's compose window, or reassign an email from its page."),
    ),
  );
}

async function renameCampaign(name) {
  const next = prompt(`Rename campaign "${name}" to:`, name);
  if (next === null || next.trim() === name) return;
  await api(`/api/campaigns/${encodeURIComponent(name)}`, { method: "PATCH", body: JSON.stringify({ name: next.trim() || null }) });
  if (state.campaign === name) state.campaign = next.trim() ? next.trim() : "";
  await render();
}

async function viewEmails(root, params) {
  const campaign = params.get("campaign") ?? state.campaign;
  state.campaign = campaign;
  document.getElementById("campaign-filter").value = campaign;
  const sender = state.sender;
  const status = params.get("status") || "";
  const q = params.get("q") || "";
  const page = Math.max(0, Number(params.get("page") || 0));
  const limit = 50;
  const { data, total } = await api(`/api/emails?${qs({ campaign, sender, status, q, limit, offset: page * limit })}`);
  const go = (patch) => { location.hash = `#/emails?${qs({ campaign, status, q, ...patch })}`; };

  const search = h("input", { type: "search", placeholder: "Search subject, address, campaign", value: q, "aria-label": "Search" });
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") go({ q: search.value.trim(), page: 0 }); });
  const statusSel = h("select", { "aria-label": "Status", onchange: (e) => go({ status: e.target.value, page: 0 }) },
    [["", "Any status"], ["opened", "Opened"], ["not_opened", "Not opened"], ["prefetch_only", "Prefetch only"]].map(([v, l]) => h("option", { value: v, selected: v === status }, l)));
  const showFrom = state.senders.length > 1 || data.some((e) => e.sender);

  root.append(
    h("section", {},
      sectionHead(`${total} email${total === 1 ? "" : "s"}${campaign ? ` in ${campaignLabel(campaign === NO_CAMPAIGN ? null : campaign)}` : ""}`, h("div", { class: "filters" }, search, statusSel)),
      data.length
        ? h("div", { class: "table-wrap" },
            h("table", {},
              h("thead", {}, h("tr", {}, ["Subject", showFrom ? "From" : null, "To", "Campaign", "Sent", "Status"].filter(Boolean).map((t) => h("th", {}, t)))),
              h("tbody", {}, data.map((e) =>
                h("tr", { class: "clickable", onclick: () => { location.hash = `#/email/${e.id}`; } },
                  h("td", { class: "strong ellipsis" }, e.subject || "(no subject)"),
                  showFrom ? h("td", { class: "ellipsis muted" }, e.sender || "unknown") : null,
                  h("td", { class: "ellipsis" }, e.recipients.join(", ") || h("span", { class: "muted" }, "unknown")),
                  h("td", {}, campaignTag(e.campaign)),
                  h("td", { title: fmtDate(e.sent_at) }, relTime(e.sent_at)),
                  h("td", {}, statusChip(e)),
                ))),
            ))
        : h("p", { class: "empty" }, "No emails match these filters."),
      pager(page, limit, total, go),
    ),
  );
}

async function viewEmail(root, id) {
  const { data: e } = await api(`/api/emails/${encodeURIComponent(id)}`);
  const listId = "campaign-options";
  const campaignInput = h("input", { list: listId, value: e.campaign || "", placeholder: "No campaign", maxlength: 100, "aria-label": "Campaign" });
  const saveMsg = h("span", { class: "muted small" });
  const save = async () => {
    await api(`/api/emails/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ campaign: campaignInput.value.trim() || null }) });
    saveMsg.textContent = "Saved";
    await loadFilters();
  };
  const del = async () => {
    if (!confirm("Delete this email and all of its hits? This cannot be undone.")) return;
    await api(`/api/emails/${encodeURIComponent(id)}`, { method: "DELETE" });
    location.hash = "#/emails";
  };
  const kindLabel = { open: "Open", self: "Self view", prefetch: "Prefetch" };
  const fact = (label, value) => h("div", { class: "fact" }, h("dt", {}, label), h("dd", {}, value));

  root.append(
    h("section", { class: "detail-head" },
      h("a", { href: "#/emails", class: "back" }, "← All emails"),
      h("h1", {}, e.subject || "(no subject)"),
      h("div", {}, statusChip(e)),
    ),
    h("dl", { class: "facts" },
      fact("From", e.sender || "unknown"),
      fact("To", e.recipients.join(", ") || "unknown"),
      fact("Sent", fmtDate(e.sent_at)),
      fact("First open", e.first_open ? `${duration(e.first_open - e.sent_at)} after sending` : "not yet"),
    ),
    h("section", {},
      sectionHead(`${e.hits.length} hit${e.hits.length === 1 ? "" : "s"}`, h("span", { class: "muted" }, `${e.opens} open${e.opens === 1 ? "" : "s"}, ${e.self} self view${e.self === 1 ? "" : "s"}, ${e.prefetch} prefetch`)),
      e.hits.length
        ? h("div", { class: "table-wrap" },
            h("table", {},
              h("thead", {}, h("tr", {}, ["Time", "Verdict", "Why", "Via", "User agent", "IP"].map((t) => h("th", {}, t)))),
              h("tbody", {}, [...e.hits].reverse().map((x) =>
                h("tr", { class: x.kind === "open" ? "" : "dimrow" },
                  h("td", { title: fmtDate(x.ts), class: "num" }, `${fmtTime(x.ts)}`, h("div", { class: "muted small" }, relTime(x.ts))),
                  h("td", {}, h("span", { class: `chip ${x.kind === "open" ? "open" : "dim"}` }, kindLabel[x.kind] || x.kind)),
                  h("td", { class: "why" }, x.reason || ""),
                  h("td", {}, x.via === "gmail_proxy" ? "Gmail proxy" : "Direct"),
                  h("td", {}, h("div", { class: "ua", title: x.ua }, x.ua || "")),
                  h("td", { class: "small muted num" }, x.ip || ""),
                ))),
            ))
        : h("p", { class: "empty" }, "No hits yet. Nothing has loaded the image."),
    ),
    h("section", {},
      sectionHead("Campaign"),
      h("div", { class: "inline-form" },
        campaignInput,
        h("datalist", { id: listId }, state.campaigns.filter((c) => c.name).map((c) => h("option", { value: c.name }))),
        h("button", { class: "btn", type: "button", onclick: save }, "Save"), saveMsg),
    ),
    h("div", { class: "danger-zone" }, h("button", { class: "btn danger small", type: "button", onclick: del }, "Delete this email")),
  );
}

// The agent instructions download the CLI and the skill from the public repository.
const REPO_RAW = "https://raw.githubusercontent.com/Hugo-SEQUIER/likelyread/main";
const MASK = "•".repeat(12);

/** What a user pastes to an AI agent without MCP: install the CLI and the skill, then
 * connect with the setup link. */
function agentInstructions(link) {
  return [
    "Set up Likelyread so you can track whether the emails you send for me get opened.",
    "",
    "1. Install the likelyread command (one Python 3.9+ file, no dependencies):",
    "   mkdir -p ~/.local/bin",
    `   curl -fsSL ${REPO_RAW}/cli/likelyread.py -o ~/.local/bin/likelyread`,
    "   chmod +x ~/.local/bin/likelyread",
    "   If ~/.local/bin is not on your PATH, call it by its full path.",
    "",
    "2. Install the Likelyread skill. For Claude Code:",
    "   mkdir -p ~/.claude/skills/likelyread",
    `   curl -fsSL ${REPO_RAW}/skills/likelyread/SKILL.md -o ~/.claude/skills/likelyread/SKILL.md`,
    "   Other agents: save that file where you keep skills, or read it now and follow it whenever you send email for me.",
    "",
    `3. Connect it to my ${isCloud() ? "account" : "server"}:`,
    `   likelyread setup '${link}'`,
    "   likelyread ping",
    "   The link contains my private token. Use it only in that command: never print it, log it or put it in an email.",
    "",
    "4. Tell me when it works. Then follow the skill: ask me before you track any campaign.",
  ].join("\n");
}

/** What a user pastes to an agent that can change its own settings, such as Hermes: it
 * adds the MCP server itself, installs the skill and checks the connection. */
function agentPrompt(url, auth) {
  return [
    "Connect yourself to Likelyread, which tracks whether the emails you send for me get opened. Do the setup yourself.",
    "",
    "1. Add Likelyread's MCP server to your own configuration so its tools are available to you:",
    "   - name: likelyread",
    `   - transport: streamable HTTP, URL ${url}`,
    `   - header: Authorization: ${auth}`,
    "   The token in that header is private. Keep it where your configuration stores secrets, and never print it, log it or put it in an email.",
    "",
    `2. Install the Likelyread skill from ${REPO_RAW}/skills/likelyread/SKILL.md and follow it whenever you send email for me.`,
    "",
    "3. Load the new tools, then check that you have these 11: account, new_pixel, text_to_html, track_email, list_emails, get_email, set_campaign, campaign_names, list_campaigns, get_stats, sender_health. If they only appear in a new session, tell me.",
    "",
    "4. Call account and tell me my plan. Track nothing until I ask: the skill says when to ask me first.",
    "",
    "5. Tell me which files you changed, without the token.",
  ].join("\n");
}

/** How each MCP client adds Likelyread's server: streamable HTTP with the token in a header. */
const MCP_CLIENTS = [
  { id: "claude", name: "Claude Code", where: "Run this in a terminal. The tools appear in your next Claude Code session, in every project.",
    text: (url, auth) => `claude mcp add --transport http --scope user likelyread ${url} --header "Authorization: ${auth}"` },
  { id: "cursor", name: "Cursor", where: "Add this to ~/.cursor/mcp.json, or to .cursor/mcp.json in one project. If the file already has servers, add likelyread next to them.",
    text: (url, auth) => JSON.stringify({ mcpServers: { likelyread: { url, headers: { Authorization: auth } } } }, null, 2) },
  { id: "vscode", name: "VS Code", where: "Run MCP: Open User Configuration from the command palette and add likelyread under servers.",
    text: (url, auth) => JSON.stringify({ servers: { likelyread: { type: "http", url, headers: { Authorization: auth } } } }, null, 2) },
  { id: "codex", name: "Codex", where: "Add this to ~/.codex/config.toml. The Codex CLI, IDE extension and app all read it.",
    text: (url, auth) => `[mcp_servers.likelyread]\nurl = "${url}"\nhttp_headers = { "Authorization" = "${auth}" }` },
  { id: "agent", name: "Any agent", where: "Paste this to an agent that can change its own settings, such as Hermes. It adds the server, installs the skill and checks the connection.",
    text: agentPrompt },
  { id: "other", name: "Other clients", where: "Most MCP clients take a streamable HTTP server: this address, with this Authorization header.",
    text: (url, auth) => JSON.stringify({ mcpServers: { likelyread: { type: "http", url, headers: { Authorization: auth } } } }, null, 2) },
];

/** Claude's Add custom connector dialog with Likelyread's name and address filled in. The
 * user still confirms there, then signs in with Google (oauth.py on the server). */
const claudeInstallUrl = (url) =>
  `https://claude.ai/customize/connectors?${new URLSearchParams({ modal: "add-custom-connector", connectorName: "Likelyread", connectorUrl: url })}`;

/** A secret in a masked field with Copy and Show, as the setup link and the agent token use. */
function secretField(label, copyLabel) {
  const field = h("input", { type: "password", readonly: true, "aria-label": label, class: "link-field" });
  const msg = h("span", { class: "muted small", role: "status" });
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(field.value);
      msg.textContent = "Copied.";
    } catch {
      field.type = "text";
      field.select();
      msg.textContent = "Press Ctrl+C (Cmd+C) to copy.";
    }
  };
  const row = h("div", { class: "inline-form" },
    field,
    h("button", { class: "btn", type: "button", onclick: copy }, copyLabel),
    h("button", { class: "btn ghost", type: "button", onclick: () => { field.type = field.type === "password" ? "text" : "password"; } }, "Show"),
    msg);
  return { row, field, msg };
}

/** Copy text. When the clipboard is blocked, show it in place, selected, to copy by hand. */
async function copyOrSelect(text, shownIn, msg, done) {
  try {
    await navigator.clipboard.writeText(text);
    msg.textContent = done;
  } catch {
    shownIn.textContent = text;
    getSelection().selectAllChildren(shownIn);
    msg.textContent = "Press Ctrl+C (Cmd+C) to copy.";
  }
}

function viewConnect(root) {
  const cloud = isCloud();
  const place = cloud ? "your account" : "this server";
  // On the cloud an agent gets a token of its own, made on request and kept only in
  // this page. A self-hosted server has one token, the one this dashboard uses.
  const token = () => (cloud ? state.agentToken : state.token);
  const mcpUrl = `${location.origin}/mcp`;
  const setupLink = (t) => `${location.origin}/#connect=${t}`;

  const link = secretField("Setup link", "Copy link");
  link.field.value = setupLink(state.token);
  // The token on its own, for a client that asks for it alone.
  const tokenBox = secretField(cloud ? "Agent token" : "Token", "Copy token");
  tokenBox.row.classList.add("token-row");
  tokenBox.row.prepend(h("span", { class: "token-label" }, cloud ? "Agent token" : "Token"));

  // On screen the token is masked; only what is copied carries it.
  const client = () => MCP_CLIENTS.find((c) => c.id === state.mcpClient) || MCP_CLIENTS[0];
  const snippet = h("pre", { class: "agent-prompt", "aria-label": "Setup for your agent" });
  const where = h("p", { class: "muted client-where" });
  const snippetMsg = h("span", { class: "muted small", role: "status" });
  const copySnippet = h("button", { class: "btn primary", type: "button", onclick: () =>
    copyOrSelect(client().text(mcpUrl, `Bearer ${token()}`), snippet, snippetMsg, "Copied. The token is in the copy, not on screen.") }, "Copy");
  const picker = h("div", { class: "client-picker", role: "group", "aria-label": "Your agent" });
  const prompt = h("pre", { class: "agent-prompt" }, agentInstructions(setupLink(MASK)));
  const promptMsg = h("span", { class: "muted small", role: "status" });
  const copyPrompt = h("button", { class: "btn", type: "button", onclick: () =>
    copyOrSelect(agentInstructions(setupLink(token())), prompt, promptMsg, "Copied. Paste it to your agent.") }, "Copy instructions");

  const show = () => {
    tokenBox.field.value = token() || "";
    tokenBox.row.hidden = !token();
    const c = client();
    snippet.textContent = c.text(mcpUrl, `Bearer ${MASK}`);
    where.textContent = c.where;
    snippetMsg.textContent = "";
    for (const b of picker.children) b.setAttribute("aria-pressed", String(b.dataset.client === c.id));
    for (const b of [copySnippet, copyPrompt]) {
      b.disabled = !token();
      b.title = token() ? "" : "Create an agent token first";
    }
  };
  picker.append(...MCP_CLIENTS.map((c) => h("button", { type: "button", dataset: { client: c.id }, onclick: () => { state.mcpClient = c.id; show(); } }, c.name)));

  const createMsg = h("span", { class: "muted small", role: "status" });
  // Made on click, never on page load: each one is a new credential.
  const createButton = h("button", { class: "btn primary", type: "button", onclick: async () => {
    createButton.disabled = true;
    createMsg.textContent = "Creating…";
    try {
      const { data } = await api("/api/me/agent-token", { method: "POST" });
      state.agentToken = data.token;
      tokenBox.field.type = "password";
      tokenBox.msg.textContent = "";
      createMsg.textContent = "Created. Only this page has it: copy the token or a setup below before you leave.";
      createButton.textContent = "Create another";
    } catch (err) {
      createMsg.textContent = err.message;
    }
    createButton.disabled = false;
    show();
  } }, state.agentToken ? "Create another" : "Create agent token");

  // The address alone, for apps that ask for it (no secret in it).
  const address = h("input", { type: "text", readonly: true, value: mcpUrl, "aria-label": "MCP server address", class: "link-field" });
  const addressMsg = h("span", { class: "muted small", role: "status" });
  const copyAddress = async () => {
    try {
      await navigator.clipboard.writeText(mcpUrl);
      addressMsg.textContent = "Copied.";
    } catch {
      address.select();
      addressMsg.textContent = "Press Ctrl+C (Cmd+C) to copy.";
    }
  };

  const step = (n, title, body) => h("li", { class: "connect-step" }, h("span", { class: "connect-n" }, n), h("div", {}, h("h3", {}, title), body));
  root.append(...[
    h("section", { class: "headline" },
      h("h1", {}, "Connect", h("br"), h("span", { class: "rate" }, "your tools")),
      h("p", {}, cloud
        ? "Claude and ChatGPT connect with your Google sign-in. Other AI agents connect over MCP with one command and a token of their own. The Chrome extension signs in with Google."
        : "Your AI agent connects over MCP with one command. The Chrome extension and other browsers use your setup link. Both carry your private token: treat them like a password."),
    ),
    cloud
      ? h("section", { "aria-label": "Claude and ChatGPT" },
          sectionHead("Claude and ChatGPT"),
          h("p", { class: "muted agent-note" }, "Ask about your emails from a chat: who opened the proposal, who to follow up with this week. You sign in with Google and approve the app, so there is no token to copy."),
          h("ol", { class: "connect-steps two" },
            step("1", "Claude", [
              h("p", {}, "Opens Claude with Likelyread filled in. Click Add, then Connect, and sign in with Google. It works on claude.ai and in Claude's desktop and mobile apps. On a Team or Enterprise plan, an Owner adds it for the organization first."),
              h("a", { class: "btn primary app-add", href: claudeInstallUrl(mcpUrl), target: "_blank", rel: "noopener" }, "Add to Claude"),
            ]),
            step("2", "ChatGPT", h("p", {}, "In ChatGPT, open ", h("a", { href: "https://chatgpt.com/plugins", target: "_blank", rel: "noopener" }, "Plugins"), ", click +, then Add custom MCP server. Name it Likelyread, enter the address below as a public endpoint and choose OAuth. Then connect and sign in with Google."))),
          h("div", { class: "inline-form copy-row app-address" }, address, h("button", { class: "btn", type: "button", onclick: copyAddress }, "Copy address"), addressMsg))
      : null,
    h("section", {},
      sectionHead("Connect your agent"),
      h("p", { class: "muted agent-note" }, `Claude Code, Cursor, VS Code, Codex and other MCP clients, with nothing to install, or any agent that sets itself up from a prompt, such as Hermes. Your agent gets Likelyread's tools and its rules: it asks before tracking a campaign, keeps a safe pace and reports opens honestly.`),
      cloud
        ? h("div", { class: "agent-token" },
            h("div", { class: "inline-form" }, createButton, createMsg),
            h("p", { class: "muted small agent-note" }, "An agent token tracks emails and reads results. It cannot change, export or delete your account, and stops working after 180 days unused. You can keep 5: a sixth replaces the oldest. Revoke them under Account."))
        : null,
      tokenBox.row,
      picker,
      where,
      snippet,
      h("div", { class: "inline-form copy-row" }, copySnippet, snippetMsg),
    ),
    h("section", {},
      sectionHead("Agents without MCP, and scripts", h("div", { class: "inline-form" }, promptMsg, copyPrompt)),
      h("p", { class: "muted agent-note" }, `For an agent that runs commands but has no MCP, or for a script. The instructions install the likelyread command and its skill, then connect them to ${place}${cloud ? " with the agent token" : ""}. The token goes in when you copy.`),
      prompt,
    ),
    h("section", {},
      sectionHead("Your setup link"),
      link.row,
      h("ol", { class: "connect-steps two" },
        cloud
          ? step("1", "Chrome extension", h("p", {}, h("a", { href: STORE_URL, target: "_blank", rel: "noopener" }, "Install the Likelyread extension"), " and click Sign in with Google in its settings. Then write an email in Gmail: the red eye next to Send means it is tracked."))
          : step("1", "Chrome extension", h("p", {}, h("a", { href: STORE_URL, target: "_blank", rel: "noopener" }, "Install the Likelyread extension"), ", open its settings, paste the setup link and click Save. Then write an email in Gmail: the red eye next to Send means it is tracked.")),
        step("2", "Another browser", h("p", {}, "Open the setup link there to sign in to this dashboard."))),
    ),
  ].filter(Boolean));
  show();
}

async function viewAccount(root) {
  state.me = null;
  const me = await loadMe();
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const msg = h("p", { class: "muted small", role: "status" });
  const go = async (path, body) => {
    msg.textContent = "Opening…";
    try {
      const { data } = await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      location.href = data.url;
    } catch (err) {
      msg.textContent = err.message;
    }
  };
  const isPro = me.plan === "pro";
  const planName = me.billing_plan === "lifetime" ? "Pro, lifetime" : isPro ? "Pro" : "Free";
  const until = me.plan_until ? new Date(me.plan_until * 1000).toLocaleDateString(undefined, { dateStyle: "medium" }) : "";
  const usage = me.usage || {};
  const fact = (label, value) => h("div", { class: "fact" }, h("dt", {}, label), h("dd", {}, value));
  const p = me.prices || {};
  const plan = (key, name, price, per, note, best) =>
    h("div", { class: `plan${best ? " best" : ""}` },
      h("h3", {}, name), h("p", { class: "plan-price" }, price, h("small", {}, per)), h("p", { class: "muted small" }, note),
      h("button", { class: `btn${best ? " primary" : ""}`, type: "button", onclick: () => go("/api/billing/checkout", { plan: key }) }, `Choose ${name.toLowerCase()}`));

  const { data: apps } = await api("/api/me/apps");
  const appRow = (a) => h("li", { class: "app-row" },
    h("div", {}, h("span", { class: "strong" }, a.name), " ", h("span", { class: "muted" }, a.site)),
    h("span", { class: "muted small" }, `Connected ${fmtDate(a.created_at)}, last used ${relTime(a.last_used)}`),
    h("button", { class: "btn small", type: "button", onclick: async () => {
      if (!confirm(`Disconnect ${a.name}? It stops working until you connect it again.`)) return;
      await api(`/api/me/apps/${encodeURIComponent(a.id)}`, { method: "DELETE" });
      await render();
    } }, "Disconnect"));
  root.append(...[
    h("section", { class: "headline" },
      h("h1", {}, planName, h("br"), h("span", { class: "rate" }, "plan")),
      h("p", {}, params.get("checkout") === "success"
        ? "Thank you. Your payment is confirmed in a few seconds; refresh if Pro does not show yet."
        : `Signed in as ${me.email}.`),
    ),
    h("dl", { class: "facts" },
      fact("Account", me.email),
      fact("Plan", planName),
      fact(isPro && me.billing_plan !== "lifetime" ? (me.cancel_at_period_end ? "Ends on" : "Renews on") : "History", isPro && until ? until : me.features.history_days ? `${me.features.history_days} days` : "Unlimited"),
      fact("Tracked today", `${usage.today || 0} of ${usage.daily_limit || "unlimited"}`),
    ),
    !isPro && me.billing
      ? h("section", {},
          sectionHead("Upgrade to Pro", h("span", { class: "muted" }, "Campaign analytics, sender health, charts, filters and full history")),
          h("div", { class: "plans" },
            plan("pro_yearly", "Yearly", p.pro_yearly || "€36", " / year", "Three months free compared with monthly.", true),
            plan("pro_monthly", "Monthly", p.pro_monthly || "€5", " / month", "Cancel any time."),
            me.lifetime_left > 0 ? plan("lifetime", "Lifetime", p.lifetime || "€59", " once", `Pay once, keep Pro. ${me.lifetime_left} left at this price.`) : null),
          h("p", { class: "muted small" }, "Prices exclude VAT, which is added at checkout where it applies."),
          msg)
      : null,
    isPro && me.billing && me.billing_plan !== "lifetime"
      ? h("section", {}, sectionHead("Billing"),
          h("div", { class: "inline-form" }, h("button", { class: "btn", type: "button", onclick: () => go("/api/billing/portal") }, "Manage subscription and invoices"), msg))
      : null,
    h("section", {},
      sectionHead("Connected apps"),
      apps.length
        ? h("ul", { class: "apps", "aria-label": "Connected apps" }, apps.map(appRow))
        : h("p", { class: "muted" }, "None yet. ", h("a", { href: "#/connect" }, "Connect Claude or ChatGPT"), " to ask about your emails from a chat.")),
    h("section", {},
      sectionHead("Security"),
      h("div", { class: "inline-form" },
        h("button", { class: "btn", type: "button", onclick: async () => {
          if (!confirm("Sign out on every device, including the Chrome extension, agents and connected apps?")) return;
          await api("/api/me/signout-all", { method: "POST" });
          logout();
        } }, "Sign out everywhere"),
        h("button", { class: "btn", type: "button", onclick: exportData }, "Download my data"),
        me.agent_tokens
          ? h("button", { class: "btn", type: "button", onclick: async () => {
              if (!confirm("Revoke every agent token? Agents connected with them stop working until you connect them again.")) return;
              await api("/api/me/agent-tokens", { method: "DELETE" });
              state.agentToken = null;
              await render();
            } }, `Revoke agent tokens (${me.agent_tokens})`)
          : null),
    ),
    h("div", { class: "danger-zone" },
      h("button", { class: "btn danger small", type: "button", onclick: async () => {
        if (!confirm("Delete your account and every tracked email? This cannot be undone. Any subscription is cancelled.")) return;
        try {
          await api("/api/me", { method: "DELETE" });
          logout();
        } catch (err) {
          alert(err.message);
        }
      } }, "Delete my account and data")),
  ].filter(Boolean));
}

async function exportData() {
  const res = await fetch("/api/me/export", { headers: { Authorization: "Bearer " + state.token } });
  if (!res.ok) return alert(`Export failed: ${res.status}`);
  const url = URL.createObjectURL(await res.blob());
  const a = h("a", { href: url, download: "likelyread-export.json" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// ---------------------------------------------------------------- admin (ADMIN_EMAILS only)

const ACCOUNT_SORTS = [["new", "Newest first"], ["seen", "Last seen"], ["emails", "Most emails"]];

function planLabel(a) {
  if (a.billing_plan === "lifetime") return "Pro, lifetime";
  if (a.plan !== "pro") return "Free";
  const until = a.plan_until ? new Date(a.plan_until * 1000).toLocaleDateString(undefined, { dateStyle: "medium" }) : "";
  return a.cancel_at_period_end && until ? `Pro, ends ${until}` : "Pro";
}

/** Every account at a glance, for the operator. Counts only: the server sends no
 * subjects, recipients or opens of anyone's emails. */
async function viewAdmin(root, params) {
  const tz = -new Date().getTimezoneOffset();
  const q = params.get("q") || "";
  const sort = params.get("sort") || "new";
  const page = Math.max(0, Number(params.get("page") || 0));
  const limit = 50;
  const [{ data: a }, { data: accounts, total }] = await Promise.all([
    api(`/api/admin/stats?${qs({ tz })}`),
    api(`/api/admin/accounts?${qs({ q, sort, limit, offset: page * limit })}`),
  ]);
  const go = (patch) => { location.hash = `#/admin?${qs({ q, sort, ...patch })}`; };
  const days = a.timeline.map((d) => ({ label: fmtDay(d.date), short: fmtDay(d.date), signups: d.signups, active: d.active, emails: d.emails }));
  const uses = [...a.channels, { name: "Campaigns", accounts: a.campaigns_30d }];
  const share = (n) => (a.seen_30d ? n / a.seen_30d : 0);

  const search = h("input", { type: "search", placeholder: "Search by email", value: q, "aria-label": "Search accounts" });
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") go({ q: search.value.trim(), page: 0 }); });
  const sortSel = h("select", { "aria-label": "Sort accounts", onchange: (e) => go({ sort: e.target.value, page: 0 }) },
    ACCOUNT_SORTS.map(([v, l]) => h("option", { value: v, selected: v === sort }, l)));
  const listed = `${total} account${total === 1 ? "" : "s"}`;

  root.append(
    h("section", { class: "headline" },
      h("h1", {}, `${a.accounts} account${a.accounts === 1 ? "" : "s"}`, h("br"), h("span", { class: "rate" }, `${a.paying_now} paying`)),
      h("p", {}, `${a.subscriptions} on a subscription and ${a.lifetime} lifetime, with ${a.lifetime_left} lifetime deals left. Only the addresses in ADMIN_EMAILS can open this page.`),
    ),
    h("section", { class: "tiles" },
      tile(a.signups_7d, "New in 7 days", `${a.signups_30d} in 30 days`),
      tile(a.active_7d, "Active in 7 days", `${a.active_30d} in 30 days, by tracked emails`),
      tile(a.emails_24h, "Emails in 24 h", `${a.emails_7d} in 7 days`),
      tile(a.hits_24h, "Pixel loads in 24 h", "Opens, prefetches and self views"),
    ),
    h("section", { class: "split" },
      h("div", {},
        sectionHead("Last 30 days", legend([["signups", "New accounts"], ["actives", "Active accounts"]])),
        barChart(days, [{ key: "signups", name: "new accounts", cls: "signups" }, { key: "active", name: "active accounts", cls: "actives" }], { height: 200, width: 760 }),
        h("div", { style: { marginTop: "40px" } },
          sectionHead("Emails tracked"),
          barChart(days, [{ key: "emails", name: "emails", cls: "sent" }], { height: 160, width: 760 })),
      ),
      h("div", {},
        sectionHead("What they use"),
        h("div", { class: "table-wrap" },
          h("table", { "aria-label": "What they use" },
            h("thead", {}, h("tr", {}, ["Feature", "Accounts", "Share"].map((t) => h("th", {}, t)))),
            h("tbody", {}, uses.map((u) =>
              h("tr", {},
                h("td", { class: "strong" }, u.name),
                h("td", { class: "num" }, u.accounts),
                h("td", {}, rateCell(share(u.accounts)))))))),
        h("p", { class: "muted small", style: { marginTop: "12px" } },
          `In the last 30 days, out of the ${a.seen_30d} accounts seen: they signed in, used a token or an app, or sent a tracked email. An agent token connects MCP or the CLI. Campaigns: tagged at least one email.`),
      ),
    ),
    h("section", {},
      sectionHead(q ? `${listed} matching "${q}"` : listed, h("div", { class: "filters" }, search, sortSel)),
      accounts.length
        ? h("div", { class: "table-wrap" },
            h("table", { class: "accounts", "aria-label": "Accounts" },
              h("thead", {}, h("tr", {}, ["Account", "Plan", "Signed up", "Last seen", "Emails, 30 days", "Emails, all time", "Uses"].map((t) => h("th", {}, t)))),
              h("tbody", {}, accounts.map((x) =>
                h("tr", {},
                  h("td", { class: "strong ellipsis", title: x.email }, x.email),
                  h("td", {}, planLabel(x)),
                  h("td", { title: fmtDate(x.created_at) }, relTime(x.created_at)),
                  h("td", { title: fmtDate(x.last_seen) }, relTime(x.last_seen)),
                  h("td", { class: "num" }, x.emails_30d),
                  h("td", { class: "num" }, x.emails),
                  h("td", {}, x.uses.length ? h("div", { class: "uses" }, x.uses.map((u) => h("span", { class: "chip" }, u))) : h("span", { class: "muted" }, "Nothing")),
                ))),
            ))
        : h("p", { class: "empty" }, "No accounts match."),
      pager(page, limit, total, go),
    ),
  );
}

// ---------------------------------------------------------------- sender health

const healthLabel = { ok: "Healthy", warn: "Check", bad: "Fix now", unknown: "Unknown", fail: "Missing" };

async function viewSenders(root) {
  if (!can("senders")) {
    root.append(
      h("section", { class: "headline" },
        h("h1", {}, "Sender health", h("br"), h("span", { class: "rate" }, "on Pro")),
        h("p", {}, "Keep every address you send from out of spam, without fake engagement.")),
      upsell("senders", "Stay out of spam", [
        "SPF, DKIM and DMARC checked for each sending domain, with the exact fix",
        "A daily volume guide for new addresses, with a warning in Gmail before you go over",
        "An alert when an address's open rate drops: the first sign of the spam folder",
      ]));
    return;
  }
  const { data } = await api("/api/senders/health");
  root.append(
    h("section", { class: "headline" },
      h("h1", {}, "Sender", h("br"), h("span", { class: "rate" }, "health")),
      h("p", {}, "For each address you send from: is it set up so inboxes trust it, are you sending at a safe pace, and are people still opening? Real signals only: nothing here sends email for you.")),
  );
  if (!data.length) {
    root.append(h("p", { class: "empty" }, "No sending addresses yet. They appear after your first tracked email."));
    return;
  }
  for (const r of data) root.append(senderCard(r));
}

function senderCard(r) {
  const check = (c) => h("li", { class: `check ${c.status}` },
    h("span", { class: "check-label" }, c.label),
    h("span", { class: "check-status" }, healthLabel[c.status] || c.status),
    h("div", { class: "check-body" }, h("p", {}, c.detail), c.fix ? h("p", { class: "check-fix" }, c.fix) : null));
  const ramp = r.ramp;
  const pct2 = (x) => (x === null || x === undefined ? "n/a" : `${Math.round(x * 100)}%`);
  const toggle = h("input", { type: "checkbox", checked: r.established, "aria-label": "Established address" });
  toggle.addEventListener("change", async () => {
    toggle.disabled = true;
    try {
      await api(`/api/senders/health/${encodeURIComponent(r.sender)}`, { method: "PUT", body: JSON.stringify({ established: toggle.checked }) });
      await render();
    } catch (err) {
      alert(err.message);
      toggle.disabled = false;
    }
  });
  const rampText = ramp.phase === "established"
    ? "No daily guide: marked as an established address."
    : `${ramp.sent_24h} of ${ramp.limit} in the last 24 hours (day ${ramp.day} of tracked sending${ramp.phase === "ramping" ? ", ramping up" : ""}).`;
  return h("section", { class: `sender-card ${r.status}` },
    h("div", { class: "sender-head" },
      h("h2", {}, r.sender),
      h("span", { class: `chip health ${r.status}` }, healthLabel[r.status])),
    r.issues.length ? h("ul", { class: "issues" }, r.issues.map((i) => h("li", {}, i))) : h("p", { class: "muted" }, "Nothing to fix."),
    h("div", { class: "sender-grid" },
      h("div", {},
        h("h3", {}, "Setup"),
        r.dns ? h("ul", { class: "checks" }, r.dns.checks.map(check)) : h("p", { class: "muted" }, "Not checked.")),
      h("div", {},
        h("h3", {}, "Pace"),
        h("p", {}, rampText),
        ramp.limit ? h("div", { class: "rate-bar wide" }, h("div", { class: "rate-fill", style: { width: `${Math.min(100, (ramp.sent_24h / ramp.limit) * 100)}%` } })) : null,
        h("label", { class: "check-inline" }, toggle, " Established address (it already sends every day): skip the guide"),
        h("h3", {}, "Opens"),
        h("p", {}, r.trend.recent.emails
          ? `Last 7 days: ${pct2(r.trend.recent.open_rate)} of ${r.trend.recent.emails} emails opened.`
          : "No emails in the last 7 days yet.",
          " ",
          r.trend.before.emails
            ? `The 4 weeks before: ${pct2(r.trend.before.open_rate)} of ${r.trend.before.emails}.`
            : "No earlier emails to compare with yet."),
        h("p", { class: "muted small" }, "Emails from the last 24 hours are left out: they have not had time to be opened."))),
  );
}

const STORE_URL = "https://chromewebstore.google.com/detail/likelyread-email-open-tra/gbpbnkffdkbjgjnmgdlicbpgpaompmdd";

function emptyState() {
  return h("section", { class: "headline" },
    h("h1", {}, "Nothing tracked", h("br"), h("span", { class: "rate" }, "yet")),
    h("p", {}, isCloud()
      ? [h("a", { href: STORE_URL, target: "_blank", rel: "noopener" }, "Install the Likelyread Chrome extension"), " and click Sign in with Google in its settings, then send an email from Gmail with the red eye on."]
      : [h("a", { href: STORE_URL, target: "_blank", rel: "noopener" }, "Install the Likelyread Chrome extension"), " and paste your setup link in its settings (see Connect), then send an email from Gmail with the red eye on."]));
}

// ---------------------------------------------------------------- router

async function render() {
  const root = document.getElementById("view");
  const raw = location.hash.replace(/^#\/?/, "") || "overview";
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  const [section, arg] = path.split("/");
  const tab = section === "email" ? "emails" : section;
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  root.replaceChildren(h("div", { class: "loading" }, "Loading…"));
  try {
    await loadFilters();
    const frag = h("div", {});
    if (section === "connect") viewConnect(frag);
    else if (section === "account" && isCloud()) await viewAccount(frag);
    else if (section === "admin" && state.me.admin) await viewAdmin(frag, params);
    else if (section === "senders") await viewSenders(frag);
    else if (section === "extension-connected") viewExtensionConnected(frag);
    else if (section === "campaigns") await viewCampaigns(frag);
    else if (section === "emails") await viewEmails(frag, params);
    else if (section === "email" && arg) await viewEmail(frag, arg);
    else if (!can("stats")) await viewOverviewFree(frag);
    else if (!state.campaigns.length) frag.append(emptyState());
    else await viewOverview(frag);
    root.replaceChildren(frag);
  } catch (err) {
    if (err instanceof AuthError) return logout();
    if (err instanceof ProError) {
      state.me = null;
      return root.replaceChildren(upsell("history", "This needs Pro", ["Emails older than 30 days", "Campaign analytics and charts", "Filters by campaign and sending address"]));
    }
    root.replaceChildren(h("p", { class: "error" }, `Could not load data: ${err.message}`));
  }
}

// ---------------------------------------------------------------- boot

/** Sign in from a setup link (https://server/#connect=TOKEN), on load or when the
 * fragment changes. The token is in the fragment, so it never travels in a request;
 * it is removed from the address bar. Returns false when the link was rejected. */
async function connectFromLink() {
  const m = location.hash.match(/^#connect=([A-Za-z0-9._~-]{16,})(?:&ext=([a-p]{32}))?$/);
  if (!m) return true;
  const ext = m[2];
  history.replaceState(null, "", ext ? "/#/extension-connected" : isCloud() ? "/#/overview" : "/#/connect");
  try {
    // Google sign-in on the cloud: stay signed in on this device.
    await login(m[1], isCloud());
    if (ext) state.extensionResult = await handToExtension(ext, m[1]);
    return true;
  } catch (err) {
    state.token = null;
    showLogin(err instanceof AuthError ? "This setup link is not valid for this server." : `Could not reach the server: ${err.message}`);
    return false;
  }
}

/** Give the token to the Chrome extension that started the sign-in. Chrome only
 * exposes chrome.runtime here when the extension lists this site under
 * externally_connectable, and the extension checks that the message came from it. */
function handToExtension(id, token) {
  return new Promise((resolve) => {
    const rt = window.chrome && window.chrome.runtime;
    if (!rt || !rt.sendMessage) return resolve(false);
    const timer = setTimeout(() => resolve(false), 3000);
    try {
      rt.sendMessage(id, { type: "connect", serverUrl: location.origin, token }, (reply) => {
        clearTimeout(timer);
        void rt.lastError;
        resolve(!!(reply && reply.ok));
      });
    } catch {
      clearTimeout(timer);
      resolve(false);
    }
  });
}

function viewExtensionConnected(root) {
  const ok = state.extensionResult;
  root.append(h("section", { class: "headline" },
    h("h1", {}, ok ? "Extension" : "Almost", h("br"), h("span", { class: "rate" }, ok ? "connected" : "there")),
    h("p", {}, ok
      ? "You can close this tab. Write an email in Gmail: the red eye next to Send means it is tracked."
      : "The extension did not answer. Copy your setup link from Connect and paste it in the extension's settings."),
    ok ? null : h("a", { class: "btn primary", href: "#/connect" }, "Open Connect")));
}

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    if (res.ok) state.config = await res.json();
  } catch { /* self-host servers before 0.4 have no /api/config */ }
}

document.addEventListener("DOMContentLoaded", async () => {
  await loadConfig();
  if (new URLSearchParams(location.search).get("signin") === "failed") {
    history.replaceState(null, "", "/");
    showLogin("Google sign-in did not complete. Try again.");
  }
  document.getElementById("login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const token = document.getElementById("token-input").value.trim();
    try {
      await login(token, document.getElementById("remember").checked);
      document.getElementById("token-input").value = "";
      document.getElementById("login").hidden = true;
      document.getElementById("shell").hidden = false;
      await render();
    } catch (err) {
      showLogin(err instanceof AuthError ? "That token was rejected." : `Could not reach the server: ${err.message}`);
    }
  });
  document.getElementById("logout").addEventListener("click", logout);
  if (isCloud()) document.getElementById("remember").checked = true;
  document.getElementById("refresh").addEventListener("click", render);
  document.getElementById("campaign-filter").addEventListener("change", (e) => {
    state.campaign = e.target.value;
    if (location.hash.startsWith("#/emails")) {
      const cur = new URLSearchParams(location.hash.split("?")[1] || "");
      location.hash = `#/emails?${qs({ campaign: state.campaign, status: cur.get("status") || "", q: cur.get("q") || "" })}`;
    }
    else render();
  });
  document.getElementById("sender-filter").addEventListener("change", (e) => {
    state.sender = e.target.value;
    render();
  });
  window.addEventListener("hashchange", async () => {
    if (!(await connectFromLink())) return;
    if (!state.token) return showLogin();
    document.getElementById("login").hidden = true;
    document.getElementById("shell").hidden = false;
    await render();
  });

  if (!(await connectFromLink())) return;
  state.token = state.token || loadToken();
  if (!state.token) return document.getElementById("login").hidden ? showLogin() : undefined;
  document.getElementById("shell").hidden = false;
  await render();
});
