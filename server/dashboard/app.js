/* mailpeek dashboard. Plain JS, no dependencies. All data comes from the
 * authenticated API; the token never leaves this origin. */
"use strict";

const TOKEN_KEY = "mailpeek.token";
const NO_CAMPAIGN = "__none__";
const state = { token: null, campaign: "", campaigns: [] };

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

const campaignLabel = (name) => (name === null || name === undefined || name === "" ? "Uncategorized" : name);
const campaignParam = (name) => (name === null || name === undefined || name === "" ? NO_CAMPAIGN : name);

function statusChip(e) {
  if (e.status === "opened") return h("span", { class: "chip ok", title: `Last open ${fmtDate(e.last_open)}` }, `Opened x${e.opens}`, h("span", { class: "chip-sub" }, relTime(e.last_open)));
  if (e.status === "prefetch_only") return h("span", { class: "chip dim", title: "Apple Mail Privacy Protection prefetched the image" }, "Prefetch only (Apple)");
  return h("span", { class: "chip" }, "Not opened");
}

// ---------------------------------------------------------------- API

class AuthError extends Error {}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { Authorization: `Bearer ${state.token}`, ...(opts.body ? { "Content-Type": "application/json" } : {}) },
  });
  if (res.status === 401) throw new AuthError("Invalid token");
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
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
  document.getElementById("token-input").focus();
}

function logout() {
  sessionStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_KEY);
  state.token = null;
  showLogin();
}

async function login(token, remember) {
  state.token = token;
  await api("/api/ping");
  sessionStorage.setItem(TOKEN_KEY, token);
  if (remember) localStorage.setItem(TOKEN_KEY, token);
}

// ---------------------------------------------------------------- campaign filter

async function loadCampaigns() {
  const { data } = await api("/api/campaigns");
  state.campaigns = data;
  const sel = document.getElementById("campaign-filter");
  sel.replaceChildren(
    h("option", { value: "" }, "All campaigns"),
    ...data.map((c) => h("option", { value: campaignParam(c.name) }, `${campaignLabel(c.name)} (${c.emails})`)),
  );
  if (state.campaign && !data.some((c) => campaignParam(c.name) === state.campaign)) state.campaign = "";
  sel.value = state.campaign;
}

// ---------------------------------------------------------------- charts

function barChart(points, series, { height = 180, width = 1100 } = {}) {
  const pad = { l: 28, r: 8, t: 10, b: 22 };
  const max = Math.max(1, ...points.flatMap((p) => series.map((sr) => p[sr.key])));
  const innerH = height - pad.t - pad.b;
  const slot = (width - pad.l - pad.r) / points.length;
  const bw = Math.max(2, (slot - 4) / series.length);
  const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart", role: "img" });
  for (const frac of [0, 0.5, 1]) {
    const y = pad.t + innerH * (1 - frac);
    svg.append(s("line", { x1: pad.l, x2: width - pad.r, y1: y, y2: y, class: "grid" }));
    svg.append(s("text", { x: pad.l - 6, y: y + 3, class: "axis", "text-anchor": "end" }, Math.round(max * frac)));
  }
  points.forEach((p, i) => {
    const x0 = pad.l + i * slot + 2;
    series.forEach((sr, j) => {
      const v = p[sr.key];
      const bh = (v / max) * innerH;
      const rect = s("rect", { x: x0 + j * bw, y: pad.t + innerH - bh, width: Math.max(1, bw - 1), height: Math.max(0, bh), class: sr.cls, rx: 1.5 });
      rect.append(s("title", {}, `${p.label}: ${v} ${sr.name}`));
      svg.append(rect);
    });
    const every = Math.ceil(points.length / 10);
    if (i % every === 0) svg.append(s("text", { x: x0 + (slot - 4) / 2, y: height - 6, class: "axis", "text-anchor": "middle" }, p.short));
  });
  return svg;
}

function legend(items) {
  return h("div", { class: "legend" }, items.map(([cls, label]) => h("span", {}, h("i", { class: `sw ${cls}` }), label)));
}

// ---------------------------------------------------------------- views

function kpi(label, value, sub) {
  return h("div", { class: "kpi" }, h("div", { class: "kpi-label" }, label), h("div", { class: "kpi-value" }, value), sub ? h("div", { class: "kpi-sub" }, sub) : null);
}

async function viewOverview(root) {
  const tz = -new Date().getTimezoneOffset();
  const { data: st } = await api(`/api/stats?${qs({ campaign: state.campaign, days: 30, tz })}`);
  const hourPoints = st.opens_by_hour.map((v, i) => ({ label: `${i}:00`, short: i % 3 === 0 ? `${i}h` : "", opens: v }));
  const dayPoints = st.timeline.map((d) => ({ label: fmtDay(d.date), short: fmtDay(d.date), sent: d.sent, opens: d.opens }));
  const bestHour = st.opens_by_hour.some((v) => v > 0) ? st.opens_by_hour.indexOf(Math.max(...st.opens_by_hour)) : null;

  root.append(
    h("div", { class: "kpis" },
      kpi("Tracked emails", st.emails),
      kpi("Opened", st.opened, `${pct(st.open_rate)} open rate`),
      kpi("Total opens", st.opens, `${st.opens_last_7d} in the last 7 days`),
      kpi("Median time to first open", duration(st.median_time_to_open)),
      kpi("Prefetch only", st.prefetch_only, "Apple Mail, not counted as opened"),
      kpi("Self views filtered", st.self, "your own views, ignored"),
    ),
    h("div", { class: "grid2" },
      h("section", { class: "card wide" },
        h("div", { class: "card-head" }, h("h2", {}, "Last 30 days"), legend([["sent", "Sent"], ["opens", "Opens"]])),
        h("div", { class: "chart-wrap" }, barChart(dayPoints, [{ key: "sent", name: "sent", cls: "sent" }, { key: "opens", name: "opens", cls: "opens" }], { height: 200, width: 1100 })),
      ),
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Opens by hour"), h("span", { class: "muted" }, bestHour === null ? "no opens yet" : `peak around ${bestHour}:00`)),
        h("div", { class: "chart-wrap" }, barChart(hourPoints, [{ key: "opens", name: "opens", cls: "opens" }], { height: 200, width: 520 })),
      ),
      h("section", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "Recent opens")),
        st.recent_opens.length
          ? h("ul", { class: "feed" }, st.recent_opens.slice(0, 10).map((r) =>
              h("li", {},
                h("a", { href: `#/email/${r.email_id}` }, r.subject || "(no subject)"),
                h("div", { class: "muted small" }, `${relTime(r.ts)} · ${r.via === "gmail_proxy" ? "via Gmail" : "direct"} · ${campaignLabel(r.campaign)}`),
              )))
          : h("p", { class: "empty" }, "No opens yet."),
      ),
    ),
  );
}

async function viewCampaigns(root) {
  const data = state.campaigns;
  if (!data.length) {
    root.append(emptyState());
    return;
  }
  const best = data.filter((c) => c.emails >= 3).sort((a, b) => b.open_rate - a.open_rate)[0];
  root.append(
    h("section", { class: "card" },
      h("div", { class: "card-head" },
        h("h2", {}, `Campaigns (${data.length})`),
        best ? h("span", { class: "muted" }, `Best open rate: ${campaignLabel(best.name)} at ${pct(best.open_rate)}`) : null,
      ),
      h("div", { class: "table-wrap" },
        h("table", {},
          h("thead", {}, h("tr", {}, ["Campaign", "Emails", "Opened", "Open rate", "Opens", "Time to open", "Last open", "Last sent", ""].map((t) => h("th", {}, t)))),
          h("tbody", {}, data.map((c) =>
            h("tr", { class: "clickable", onclick: () => { location.hash = `#/emails?${qs({ campaign: campaignParam(c.name) })}`; } },
              h("td", { class: "strong" }, campaignLabel(c.name)),
              h("td", {}, c.emails),
              h("td", {}, c.opened, c.prefetch_only ? h("span", { class: "muted small" }, ` +${c.prefetch_only} prefetch`) : null),
              h("td", {}, h("div", { class: "rate" }, h("div", { class: "rate-bar" }, h("div", { class: "rate-fill", style: { width: pct(c.open_rate) } })), pct(c.open_rate))),
              h("td", {}, c.opens),
              h("td", {}, duration(c.median_time_to_open)),
              h("td", {}, relTime(c.last_open)),
              h("td", {}, relTime(c.last_sent)),
              h("td", {}, c.name ? h("button", { class: "btn ghost small", onclick: (ev) => { ev.stopPropagation(); renameCampaign(c.name); } }, "Rename") : null),
            ))),
        ),
      ),
    ),
    h("p", { class: "muted small" }, "Set the active campaign from the extension popup before sending, or assign emails to a campaign from their detail page."),
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
  const status = params.get("status") || "";
  const q = params.get("q") || "";
  const page = Math.max(0, Number(params.get("page") || 0));
  const limit = 50;
  const { data, total } = await api(`/api/emails?${qs({ campaign, status, q, limit, offset: page * limit })}`);
  const go = (patch) => { location.hash = `#/emails?${qs({ campaign, status, q, ...patch })}`; };

  const search = h("input", { type: "search", placeholder: "Search subject, recipient, campaign", value: q });
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") go({ q: search.value.trim(), page: 0 }); });
  const statusSel = h("select", { onchange: (e) => go({ status: e.target.value, page: 0 }) },
    [["", "Any status"], ["opened", "Opened"], ["not_opened", "Not opened"], ["prefetch_only", "Prefetch only"]].map(([v, l]) => h("option", { value: v, selected: v === status }, l)));

  root.append(
    h("section", { class: "card" },
      h("div", { class: "card-head" },
        h("h2", {}, `${total} email${total === 1 ? "" : "s"}${campaign ? ` in ${campaignLabel(campaign === NO_CAMPAIGN ? null : campaign)}` : ""}`),
        h("div", { class: "filters" }, search, statusSel),
      ),
      data.length
        ? h("div", { class: "table-wrap" },
            h("table", {},
              h("thead", {}, h("tr", {}, ["Subject", "Recipients", "Campaign", "Sent", "Status"].map((t) => h("th", {}, t)))),
              h("tbody", {}, data.map((e) =>
                h("tr", { class: "clickable", onclick: () => { location.hash = `#/email/${e.id}`; } },
                  h("td", { class: "strong ellipsis" }, e.subject || "(no subject)"),
                  h("td", { class: "ellipsis muted" }, e.recipients.join(", ")),
                  h("td", {}, h("span", { class: "tag" }, campaignLabel(e.campaign))),
                  h("td", { title: fmtDate(e.sent_at) }, relTime(e.sent_at)),
                  h("td", {}, statusChip(e)),
                ))),
            ))
        : h("p", { class: "empty" }, "No emails match these filters."),
      total > limit
        ? h("div", { class: "pager" },
            h("button", { class: "btn ghost small", disabled: page === 0, onclick: () => go({ page: page - 1 }) }, "Previous"),
            h("span", { class: "muted small" }, `Page ${page + 1} of ${Math.ceil(total / limit)}`),
            h("button", { class: "btn ghost small", disabled: (page + 1) * limit >= total, onclick: () => go({ page: page + 1 }) }, "Next"))
        : null,
    ),
  );
}

async function viewEmail(root, id) {
  const { data: e } = await api(`/api/emails/${encodeURIComponent(id)}`);
  const listId = "campaign-options";
  const campaignInput = h("input", { list: listId, value: e.campaign || "", placeholder: "Uncategorized", maxlength: 100 });
  const saveMsg = h("span", { class: "muted small" });
  const save = async () => {
    await api(`/api/emails/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ campaign: campaignInput.value.trim() || null }) });
    saveMsg.textContent = "Saved";
    await loadCampaigns();
  };
  const del = async () => {
    if (!confirm("Delete this email and all of its hits? This cannot be undone.")) return;
    await api(`/api/emails/${encodeURIComponent(id)}`, { method: "DELETE" });
    location.hash = "#/emails";
  };
  const kindLabel = { open: "Open", self: "Self view", prefetch: "Prefetch" };

  root.append(
    h("a", { href: "#/emails", class: "back" }, "Back to emails"),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, e.subject || "(no subject)"), statusChip(e)),
      h("dl", { class: "meta" },
        h("dt", {}, "To"), h("dd", {}, e.recipients.join(", ") || "n/a"),
        h("dt", {}, "Sent"), h("dd", {}, fmtDate(e.sent_at)),
        h("dt", {}, "Opens"), h("dd", {}, `${e.opens}${e.first_open ? ` (first ${fmtDate(e.first_open)}, ${duration(e.first_open - e.sent_at)} after sending)` : ""}`),
        h("dt", {}, "Filtered"), h("dd", {}, `${e.self} self view${e.self === 1 ? "" : "s"}, ${e.prefetch} prefetch`),
        h("dt", {}, "Campaign"),
        h("dd", { class: "inline-form" },
          campaignInput,
          h("datalist", { id: listId }, state.campaigns.filter((c) => c.name).map((c) => h("option", { value: c.name }))),
          h("button", { class: "btn small", onclick: save }, "Save"), saveMsg),
      ),
    ),
    h("section", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, `Hits (${e.hits.length})`)),
      e.hits.length
        ? h("div", { class: "table-wrap" },
            h("table", {},
              h("thead", {}, h("tr", {}, ["Time", "Kind", "Via", "User agent", "IP"].map((t) => h("th", {}, t)))),
              h("tbody", {}, [...e.hits].reverse().map((x) =>
                h("tr", { class: x.kind === "open" ? "" : "dimrow" },
                  h("td", { title: fmtDate(x.ts) }, relTime(x.ts)),
                  h("td", {}, h("span", { class: `chip ${x.kind === "open" ? "ok" : "dim"}` }, kindLabel[x.kind] || x.kind)),
                  h("td", {}, x.via === "gmail_proxy" ? "Gmail proxy" : "Direct"),
                  h("td", { class: "ellipsis mono small", title: x.ua }, x.ua || ""),
                  h("td", { class: "mono small" }, x.ip || ""),
                ))),
            ))
        : h("p", { class: "empty" }, "No hits yet."),
    ),
    h("div", { class: "danger-zone" }, h("button", { class: "btn danger small", onclick: del }, "Delete email")),
  );
}

function emptyState() {
  return h("section", { class: "card empty-state" },
    h("h2", {}, "Nothing tracked yet"),
    h("p", { class: "muted" }, "Install the mailpeek Chrome extension, enter this server URL and token in its options, then send an email from Gmail with tracking on."));
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
  root.replaceChildren(h("div", { class: "loading" }, "Loading..."));
  try {
    await loadCampaigns();
    const frag = h("div", {});
    if (section === "campaigns") await viewCampaigns(frag);
    else if (section === "emails") await viewEmails(frag, params);
    else if (section === "email" && arg) await viewEmail(frag, arg);
    else if (!state.campaigns.length) frag.append(emptyState());
    else await viewOverview(frag);
    root.replaceChildren(...frag.childNodes);
  } catch (err) {
    if (err instanceof AuthError) return logout();
    root.replaceChildren(h("section", { class: "card" }, h("p", { class: "error" }, `Could not load data: ${err.message}`)));
  }
}

// ---------------------------------------------------------------- boot

document.addEventListener("DOMContentLoaded", async () => {
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
  document.getElementById("refresh").addEventListener("click", render);
  document.getElementById("campaign-filter").addEventListener("change", (e) => {
    state.campaign = e.target.value;
    if (location.hash.startsWith("#/emails")) location.hash = `#/emails?${qs({ campaign: state.campaign })}`;
    else render();
  });
  window.addEventListener("hashchange", render);

  state.token = loadToken();
  if (!state.token) return showLogin();
  document.getElementById("shell").hidden = false;
  await render();
});
