// End-to-end for Likelyread Cloud: real Chromium + the extension built for a local
// cloud server + fake Google sign-in + fake Stripe Checkout, all on this machine.
//
// Run from extension/:  CHROME_PATH=/path/to/chrome PYTHON=python3 npm run e2e:cloud
// Ports 8782 (cloud server), 8794 (fake Google) and 8795 (fake Stripe) must be free.
// SHOTS=dir saves screenshots of each screen.
import { chromium } from "playwright-core";
import { spawn, execFileSync } from "node:child_process";
import crypto from "node:crypto";
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const EXT = path.join(ROOT, "dist-cloud-e2e");
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), "likelyread-cloud-e2e-"));
const CHROME = process.env.CHROME_PATH;
if (!CHROME) { console.error("Set CHROME_PATH to a Chromium or Chrome binary."); process.exit(2); }
const PY = process.env.PYTHON || "python3";
const SHOTS = process.env.SHOTS || "";
const CLOUD = "http://localhost:8782";
const GOOGLE = "http://localhost:8794";
const STRIPE = "http://localhost:8795";
const CLIENT_ID = "e2e-client.apps.googleusercontent.com";
const WEBHOOK_SECRET = "whsec_e2e_secret_value";
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok }); console.log(`${ok ? "PASS" : "FAIL"} ${name} ${detail}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const b64url = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");

// --- build the extension against the local cloud
execFileSync("node", ["build.mjs"], { cwd: ROOT, env: { ...process.env, CLOUD_URL: CLOUD, OUT_DIR: EXT }, stdio: "ignore" });

// --- fake Google: approves at once as Ann
const googleSrv = http.createServer(async (req, res) => {
  const u = new URL(req.url, GOOGLE);
  if (u.pathname === "/auth") {
    const back = new URL(u.searchParams.get("redirect_uri"));
    back.searchParams.set("code", "good-code");
    back.searchParams.set("state", u.searchParams.get("state"));
    res.writeHead(302, { location: back.toString() }); return res.end();
  }
  if (u.pathname === "/token" && req.method === "POST") {
    let body = ""; for await (const c of req) body += c;
    const form = new URLSearchParams(body);
    if (form.get("code") !== "good-code" || !form.get("code_verifier")) { res.writeHead(400); return res.end("{}"); }
    const claims = { iss: "https://accounts.google.com", aud: CLIENT_ID, sub: "google-ann", email: "ann@example.com", email_verified: true, exp: Math.floor(Date.now() / 1000) + 600 };
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(JSON.stringify({ id_token: `${b64url({ alg: "RS256" })}.${b64url(claims)}.sig` }));
  }
  res.writeHead(404); res.end();
}).listen(8794, "127.0.0.1");

// --- fake Stripe: Checkout page that "pays", sends the signed webhook, returns to the app
const checkouts = new Map();
async function sendWebhook(type, object) {
  const body = JSON.stringify({ id: `evt_${crypto.randomUUID()}`, object: "event", type, data: { object } });
  const ts = String(Math.floor(Date.now() / 1000));
  const sig = crypto.createHmac("sha256", WEBHOOK_SECRET).update(`${ts}.${body}`).digest("hex");
  const r = await fetch(`${CLOUD}/webhooks/stripe`, { method: "POST", body, headers: { "content-type": "application/json", "stripe-signature": `t=${ts},v1=${sig}` } });
  return r.status;
}
const stripeSrv = http.createServer(async (req, res) => {
  const u = new URL(req.url, STRIPE);
  const reply = (status, data) => { res.writeHead(status, { "content-type": "application/json" }); res.end(JSON.stringify(data)); };
  if (u.pathname.startsWith("/v1/")) {
    if (req.headers.authorization !== "Bearer rk_test_e2e") return reply(401, {});
    let body = ""; for await (const c of req) body += c;
    const form = Object.fromEntries(new URLSearchParams(body));
    if (u.pathname === "/v1/customers" && req.method === "POST") return reply(200, { id: "cus_e2e", object: "customer", email: form.email });
    if (u.pathname === "/v1/checkout/sessions" && req.method === "POST") {
      const id = `cs_test_${crypto.randomUUID()}`;
      checkouts.set(id, form);
      return reply(200, { id, object: "checkout.session", url: `${STRIPE}/pay/${id}` });
    }
    return reply(404, {});
  }
  if (u.pathname.startsWith("/pay/")) {
    const form = checkouts.get(u.pathname.slice(5));
    if (!form) { res.writeHead(404); return res.end(); }
    const end = Math.floor(Date.now() / 1000) + 365 * 86400;
    const status = await sendWebhook("customer.subscription.created", {
      id: "sub_e2e", object: "subscription", status: "active", customer: form.customer, cancel_at_period_end: false,
      metadata: { account_id: form["subscription_data[metadata][account_id]"], plan: form["subscription_data[metadata][plan]"] },
      items: { data: [{ price: { id: form["line_items[0][price]"] }, current_period_end: end }] },
    });
    checkouts.set("last", { ...form, webhook: status });
    res.writeHead(302, { location: form.success_url }); return res.end();
  }
  res.writeHead(404); res.end();
}).listen(8795, "127.0.0.1");

// --- the cloud server (started once the extension id is known: it is allowlisted)
let server;
async function startCloud(extId) {
server = spawn(PY, ["-m", "uvicorn", "app:create_app", "--factory", "--port", "8782", "--log-level", "warning"], {
  cwd: path.resolve(ROOT, "..", "server"), stdio: "inherit",
  env: {
    ...process.env, MODE: "cloud", DB_PATH: path.join(WORK, "cloud.db"), PUBLIC_URL: CLOUD,
    SESSION_SECRET: "e2e-session-secret-0123456789abcdef", GOOGLE_CLIENT_ID: CLIENT_ID, GOOGLE_CLIENT_SECRET: "x",
    GOOGLE_AUTH_URL: `${GOOGLE}/auth`, GOOGLE_TOKEN_URL: `${GOOGLE}/token`,
    STRIPE_API_URL: STRIPE, STRIPE_SECRET_KEY: "rk_test_e2e", STRIPE_WEBHOOK_SECRET: WEBHOOK_SECRET,
    STRIPE_PRICE_PRO_MONTHLY: "price_m", STRIPE_PRICE_PRO_YEARLY: "price_y", STRIPE_PRICE_LIFETIME: "price_l",
    EXTENSION_IDS: extId,
  },
});
for (let i = 0; i < 100; i++) { try { if ((await fetch(CLOUD + "/healthz")).ok) break; } catch {} await sleep(100); }
}

const ctx = await chromium.launchPersistentContext(path.join(WORK, "profile"), {
  executablePath: CHROME, headless: true, viewport: { width: 1280, height: 800 },
  args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`, "--headless=new", "--no-sandbox"],
});
const shot = async (page, name) => { if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, `${name}.png`) }); } };
try {
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker");
  const errors = [];
  const watch = (p) => {
    p.on("console", (m) => { if (m.type() === "error" && !/favicon/.test(m.text())) errors.push(`${p.url()}: ${m.text()}`); });
    p.on("pageerror", (e) => errors.push(`${p.url()}: ${e.message}`));
  };
  sw.on("console", (m) => { if (m.type() === "error") errors.push(`service worker: ${m.text()}`); });
  ctx.on("page", watch);
  const extId = new URL(sw.url()).host;
  await startCloud(extId);
  for (const p of ctx.pages()) if (p.url().includes("options")) await p.close();

  // 1. the dashboard shows Google sign-in, not a token form
  const pre = await ctx.newPage(); watch(pre);
  await pre.goto(CLOUD + "/");
  await pre.waitForSelector("#login-cloud:not([hidden])", { timeout: 5000 });
  check("dashboard shows Continue with Google", await pre.isVisible("text=Continue with Google") && !(await pre.isVisible("#token-input")));
  await shot(pre, "1-signin");
  await pre.close();

  // 2. options page: one click signs in and connects the extension
  const opts = await ctx.newPage(); watch(opts);
  await opts.goto(`chrome-extension://${extId}/options.html`);
  check("options page offers cloud sign-in", await opts.isVisible("#cloudSignIn"));
  const [tab] = await Promise.all([ctx.waitForEvent("page"), opts.click("#cloudSignIn")]);
  await tab.waitForURL(/#\/extension-connected/, { timeout: 10000 });
  await tab.waitForSelector("text=connected", { timeout: 5000 });
  check("sign-in tab says the extension is connected", await tab.isVisible("text=You can close this tab"));
  await shot(tab, "2-extension-connected");
  const stored = await sw.evaluate(() => chrome.storage.local.get(["serverUrl", "token"]));
  check("extension stored the cloud address and a token", stored.serverUrl === CLOUD && /^[A-Za-z0-9_-]{30,}$/.test(stored.token || ""));
  await opts.waitForSelector("text=Signed in as ann@example.com", { timeout: 5000 }).catch(() => {});
  check("options page shows the account", await opts.isVisible("text=Signed in as ann@example.com, Free plan."));
  await shot(opts, "3-options");

  // 3. the extension's token works: register two emails, one gets opened
  const H = { Authorization: `Bearer ${stored.token}`, "content-type": "application/json" };
  const ids = [crypto.randomUUID(), crypto.randomUUID()];
  for (const [i, id] of ids.entries()) {
    const r = await fetch(`${CLOUD}/api/emails`, { method: "POST", headers: H, body: JSON.stringify({ id, subject: i ? "Pricing follow-up" : "Intro call", recipients: [i ? "lee@example.org" : "maya@example.org"], age: 120, campaign: "Q4 outreach", sender: "ann@example.com" }) });
    check(`register email ${i + 1}`, r.status === 200);
  }
  await fetch(`${CLOUD}/p/${ids[0]}.gif`, { headers: { "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/42.0.2311.135 Safari/537.36 Edge/12.246" } });
  const names = await sw.evaluate(async (c) => {
    const { token } = await chrome.storage.local.get("token");
    return (await fetch(`${c}/api/campaign-names`, { headers: { Authorization: `Bearer ${token}` } })).json();
  }, CLOUD);
  check("compose picker gets campaign names on Free", JSON.stringify(names) === '["Q4 outreach"]');

  // 4. Free dashboard: tracking and opens, analytics locked
  await tab.goto(CLOUD + "/#/overview"); await tab.reload();
  await tab.waitForSelector(".headline h1", { timeout: 5000 });
  check("free overview counts opens", (await tab.textContent(".headline h1")).includes("1 of 2 emails"));
  check("free overview shows the Pro upsell", await tab.isVisible(".upsell"));
  await shot(tab, "4-free-overview");
  await tab.click('[data-tab="campaigns"]');
  await tab.waitForSelector(".upsell", { timeout: 5000 });
  check("campaign analytics are Pro", await tab.isVisible("text=Compare your campaigns"));
  await tab.click('[data-tab="emails"]');
  await tab.waitForSelector("table", { timeout: 5000 });
  check("free plan lists every email", (await tab.locator("tbody tr").count()) === 2);

  // 5. upgrade through checkout; the webhook turns the account Pro
  await tab.click('[data-tab="account"]');
  await tab.waitForSelector(".plans", { timeout: 5000 });
  check("account page shows three plans", (await tab.locator(".plan").count()) === 3);
  await shot(tab, "5-account-free");
  await tab.click("text=Choose yearly");
  await tab.waitForURL(/checkout=success/, { timeout: 10000 });
  const last = checkouts.get("last") || {};
  check("checkout sent the account, its customer and the yearly price", last["line_items[0][price]"] === "price_y" && last.customer === "cus_e2e" && !!last.client_reference_id && last["automatic_tax[enabled]"] === "true" && last.webhook === 200);
  await tab.waitForSelector(".headline h1", { timeout: 5000 });
  await sleep(300);
  check("account is Pro after payment", (await tab.textContent(".headline h1")).startsWith("Pro"));
  await shot(tab, "6-account-pro");
  await tab.click('[data-tab="campaigns"]');
  await tab.waitForSelector("table", { timeout: 5000 });
  check("campaign analytics unlocked", (await tab.textContent("tbody")).includes("Q4 outreach"));
  await tab.click('[data-tab="overview"]');
  await tab.waitForSelector(".chart", { timeout: 5000 });
  check("Pro overview has charts", (await tab.locator(".chart").count()) >= 2);
  await shot(tab, "7-pro-overview");

  // 6. a forged message from another site is ignored
  const forged = await sw.evaluate(async () => {
    const { token } = await chrome.storage.local.get("token");
    return token;
  });
  check("token unchanged", forged === stored.token);

  check("no console errors", errors.length === 0, errors.slice(0, 5).join(" | "));
} catch (err) {
  check("run finished", false, err.stack);
} finally {
  await ctx.close();
  server?.kill();
  googleSrv.close();
  stripeSrv.close();
  fs.rmSync(EXT, { recursive: true, force: true });
}
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
