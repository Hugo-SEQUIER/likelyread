// End-to-end: real Chromium + the built extension + a fake Gmail on mail.google.com
// (routed to a local HTTPS server) + a real Likelyread server process.
//
// Run from extension/:  npm run build && CHROME_PATH=/path/to/chrome npm run e2e
// Needs: a Chromium/Chrome binary (CHROME_PATH), Python with the server's
// requirements (PYTHON, default python3), openssl, and ports 8443 and 8781 free.
import { chromium } from "playwright-core";
import { spawn, execFileSync } from "node:child_process";
import https from "node:https";
import fs from "node:fs";
import path from "node:path";

import os from "node:os";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const EXT = path.resolve(HERE, "..", "dist");
const WORK = fs.mkdtempSync(path.join(os.tmpdir(), "likelyread-e2e-"));
const CHROME = process.env.CHROME_PATH;
if (!CHROME) { console.error("Set CHROME_PATH to a Chromium or Chrome binary."); process.exit(2); }
const PY = process.env.PYTHON || "python3";
const SERVER_DIR = path.resolve(HERE, "..", "..", "server");
const TOKEN = "e2etoken0123456789abcdef0123456789";
const API = "http://127.0.0.1:8781";
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok, detail }); console.log(`${ok ? "PASS" : "FAIL"} ${name} ${detail}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- fake Gmail over HTTPS
const key = path.join(WORK, "k.pem"), crt = path.join(WORK, "c.pem");
execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt, "-days", "1", "-subj", "/CN=mail.google.com"], { stdio: "ignore" });
const gmail = `<!doctype html><html><head><title>Inbox - you@example.com - Gmail</title></head><body>
<div class="M9"><table><tbody><tr class="bzf"><td><div name="to"><div class="afV" data-hovercard-id="maya@example.org">Maya</div></div></td></tr></tbody></table>
<input name="subjectbox" value="E2E proposal">
<div><div class="Am Al editable" contenteditable="true" role="textbox" g_editable="true">Hi Maya</div>
<table><tbody><tr class="btC"><td class="gU Up"><div class="J-J5-Ji btA"><div class="dC"><div role="button" class="T-I J-J5-Ji aoO v7 T-I-atl L3">Send</div></div></div></td><td></td></tr></tbody></table></div></div>
</body></html>`;
const fake = https.createServer({ key: fs.readFileSync(key), cert: fs.readFileSync(crt) }, (req, res) => {
  res.writeHead(200, { "content-type": "text/html" }); res.end(gmail);
}).listen(8443, "127.0.0.1");

// --- Likelyread server
let server;
function startServer(origin) {
  server = spawn(PY, ["-m", "uvicorn", "app:create_app", "--factory", "--port", "8781", "--log-level", "warning"], {
    cwd: SERVER_DIR, stdio: "inherit",
    env: { ...process.env, LIKELYREAD_TOKEN: TOKEN, DB_PATH: path.join(WORK, "e2e.db"), ALLOWED_ORIGINS: origin, DASHBOARD: "false" },
  });
}
async function waitUp() { for (let i = 0; i < 100; i++) { try { if ((await fetch(API + "/healthz")).ok) return; } catch {} await sleep(100); } throw new Error("server not up"); }
const api = async (p) => (await fetch(API + p, { headers: { Authorization: "Bearer " + TOKEN } })).json();

const ctx = await chromium.launchPersistentContext(path.join(WORK, "profile"), {
  executablePath: CHROME, headless: true,
  args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`, "--headless=new", "--no-sandbox",
    "--host-resolver-rules=MAP mail.google.com:443 127.0.0.1:8443", "--ignore-certificate-errors"],
});
try {
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker");
  // Anything that would show up under "Errors" on chrome://extensions fails the run.
  const errors = [];
  const watch = (p, where) => {
    p.on("console", (m) => { if (m.type() === "error") errors.push(`${where}: ${m.text()}`); });
    p.on("pageerror", (e) => errors.push(`${where}: ${e.message}`));
  };
  sw.on("console", (m) => { if (m.type() === "error") errors.push(`service worker: ${m.text()}`); });
  ctx.on("page", (p) => watch(p, new URL(p.url() || "about:blank").pathname || "page"));
  const extId = new URL(sw.url()).host;
  startServer(`chrome-extension://${extId}`);
  await waitUp();
  for (const p of ctx.pages()) if (p.url().includes("options")) await p.close(); // first-install options tab

  // 7. bad token: options page shows an error, API says 401
  // (The options page's Test button first asks Chrome for host permission; headless
  // Chrome never answers that prompt, so check the same API call through the popup.)
  await sw.evaluate(async (u) => chrome.storage.local.set({ serverUrl: u, token: "wrong-token" }), API);
  const opt = await ctx.newPage();
  await opt.goto(`chrome-extension://${extId}/popup.html`);
  await sleep(1500);
  const err = await opt.textContent("#list");
  check("bad token shows an error in the extension", /Invalid token/.test(err || ""), `"${(err || "").trim()}"`);
  check("bad token gets 401 from the API", (await fetch(API + "/api/ping", { headers: { Authorization: "Bearer wrong" } })).status === 401);

  // setup link: one paste fills the address and the token
  const o2 = await ctx.newPage();
  await o2.goto(`chrome-extension://${extId}/options.html`);
  await o2.fill("#setupLink", `https://t.example.com/#connect=${TOKEN}`);
  check("setup link fills address and token",
    (await o2.inputValue("#serverUrl")) === "https://t.example.com" && (await o2.inputValue("#token")) === TOKEN && (await o2.inputValue("#setupLink")) === "",
    `"${await o2.textContent("#linkHint")}"`);
  await o2.fill("#setupLink", "https://t.example.com/");
  check("a wrong link is explained", /does not look like a setup link/.test(await o2.textContent("#linkHint")));
  await o2.close();

  // configure with the right token via the service worker (extension context)
  await sw.evaluate(async ([u, t]) => chrome.storage.local.set({ serverUrl: u, token: t, trackByDefault: true, campaign: "E2E campaign" }), [API, TOKEN]);
  await opt.close();

  // 2. send a tracked email from "Gmail"
  const g = await ctx.newPage();
  await g.goto("https://mail.google.com/mail/u/0/#inbox");
  await g.waitForSelector(".lr-eye", { timeout: 10000 });
  check("compose control injected", true);
  check("default campaign shown in compose", (await g.textContent(".lr-campaign")) === "E2E campaign");
  // token must not be readable from the page (main world)
  check("page cannot see extension storage", await g.evaluate(() => !(window.chrome && window.chrome.storage)));
  // configChanged path: change the default campaign from the extension side
  await sw.evaluate(() => chrome.storage.local.set({ campaign: "Changed live" }));
  await sleep(800);
  check("settings change reaches the open compose", (await g.textContent(".lr-campaign")) === "Changed live", `"${await g.textContent(".lr-campaign")}"`);
  await g.click(".aoO");
  await sleep(1500);
  const pix = await g.$$eval(`.Am img[src*="/p/"]`, (els) => els.map((e) => e.getAttribute("src")));
  check("exactly one pixel in the body", pix.length === 1, pix[0] || "");
  const id1 = pix[0] && pix[0].match(/p\/([0-9a-f-]{36})\.gif/)[1];
  const e1 = await api(`/api/emails/${id1}`);
  check("registered with subject, recipient, campaign, sender",
    e1.subject === "E2E proposal" && e1.recipients[0] === "maya@example.org" && e1.campaign === "Changed live" && e1.sender === "you@example.com",
    JSON.stringify({ s: e1.subject, r: e1.recipients, c: e1.campaign, f: e1.sender }));

  // 6. server down: the send still works, registration flushes later
  server.kill(); await sleep(800);
  await g.reload(); await g.waitForSelector(".lr-eye");
  await g.click(".aoO"); await sleep(1500);
  const pix2 = await g.$$eval(`.Am img[src*="/p/"]`, (els) => els.map((e) => e.getAttribute("src")));
  const id2 = pix2[0] && pix2[0].match(/p\/([0-9a-f-]{36})\.gif/)[1];
  check("server down: pixel still injected, send not blocked", !!id2);
  const queued = await sw.evaluate(async () => (await chrome.storage.local.get("queue")).queue || []);
  check("server down: registration queued", queued.some((q) => q.email && q.email.id), `queue=${queued.length}`);
  startServer(`chrome-extension://${extId}`); await waitUp();
  // Nothing pokes the extension: the retry alarm (first retry 30 s after a failure) must do it.
  const t0 = Date.now();
  let e2 = null;
  for (let i = 0; i < 150 && !(e2 && e2.id); i++) { await sleep(500); e2 = await api(`/api/emails/${id2}`).catch(() => null); }
  check("server back: queued registration flushed by the retry alarm", e2 && e2.id === id2, `after ${Math.round((Date.now() - t0) / 1000)} s`);
  // Expected while we deliberately use a wrong token or stop the server: network failures.
  const unexpected = errors.filter((x) => !/401|Failed to fetch|ERR_CONNECTION_REFUSED|net::/.test(x));
  check("no extension errors in the console", unexpected.length === 0, unexpected.slice(0, 3).join(" | "));
} catch (err) {
  check("e2e run", false, String(err && err.stack || err));
} finally {
  await ctx.close(); fake.close(); if (server) server.kill();
  fs.rmSync(WORK, { recursive: true, force: true });
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
}
