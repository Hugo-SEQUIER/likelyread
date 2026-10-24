// Starts a Chromium with the built extension loaded and keeps it running for
// the e2e run. Playwright's own launch cannot pass extension flags, so
// e2e.config.ts starts this script as the "app" and attaches the web engine to
// the browser over CDP (its DevTools port).
//
//   node tests/support/chromium-with-extension.mjs --cdp-port 9333 --control-port 9334 --ready-port 12345 [--headed]
//
// It answers http://127.0.0.1:<ready-port>/ once the browser is up, which is
// the runner's readiness probe. The engine wants a fresh browser for every
// test attempt: http://127.0.0.1:<control-port>/fresh hands over the browser
// started first, then replaces it with a new one on a new profile (the same
// CDP port) at each later call, and answers once it is ready. SIGTERM stops
// the browser and deletes its temporary profile.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(name);
  return i === -1 ? fallback : args[i + 1];
};
const cdpPort = Number(flag("--cdp-port", "9333"));
const readyPort = Number(flag("--ready-port", "0"));
const headed = args.includes("--headed");

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const ext = path.join(root, "extension", "dist");
if (!existsSync(path.join(ext, "manifest.json"))) {
  console.error(`no built extension at ${ext}: run \`npm run build\` in extension/ first`);
  process.exit(2);
}
const executable = chromium.executablePath();
if (!existsSync(executable)) {
  console.error(`no Chromium at ${executable}: run \`npx playwright-core install chromium\` (the headless shell cannot load extensions)`);
  process.exit(2);
}

const controlPort = Number(flag("--control-port", "9334"));

const targets = async () => {
  try {
    return await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
  } catch {
    return null;
  }
};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let current = null; // { process, profile }
let stopping = false;
let server;
let control;

const stop = (code) => {
  if (stopping) return;
  stopping = true;
  server?.close();
  control?.close();
  if (current) {
    current.process.kill("SIGTERM");
    rmSync(current.profile, { recursive: true, force: true });
  }
  process.exit(code);
};
process.on("SIGTERM", () => stop(0));
process.on("SIGINT", () => stop(0));

/** A new browser on a new profile, with the extension freshly installed. */
async function startBrowser() {
  const profile = mkdtempSync(path.join(os.tmpdir(), "likelyread-e2e-"));
  const child = spawn(
    executable,
    [
      ...(headed ? [] : ["--headless=new"]),
      `--remote-debugging-port=${cdpPort}`,
      `--user-data-dir=${profile}`,
      `--disable-extensions-except=${ext}`,
      `--load-extension=${ext}`,
      "--no-first-run",
      "--no-default-browser-check",
      "about:blank",
    ],
    { stdio: ["ignore", "inherit", "inherit"] },
  );
  const browser = { process: child, profile, replaced: false };
  child.on("exit", (code) => {
    if (stopping || browser.replaced) return;
    console.error(`chromium exited with ${code}`);
    stop(1);
  });

  let list = null;
  for (const deadline = Date.now() + 30_000; !list && Date.now() < deadline; ) {
    list = await targets();
    if (!list) await sleep(100);
  }
  if (!list) {
    console.error(`DevTools did not answer on port ${cdpPort}`);
    stop(1);
  }
  // On install the extension opens its options page, which takes over the
  // browser's first tab. Let that happen before the engine takes that tab, so
  // it is not navigated away mid-test.
  for (const deadline = Date.now() + 3_000; Date.now() < deadline; ) {
    if (list?.some((t) => t.url.startsWith("chrome-extension://"))) break;
    await sleep(100);
    list = await targets();
  }
  return browser;
}

/** Stop the current browser and wait until its DevTools port is free again. */
async function stopBrowser(browser) {
  browser.replaced = true;
  const exited = new Promise((resolve) => browser.process.once("exit", resolve));
  browser.process.kill("SIGTERM");
  await exited;
  rmSync(browser.profile, { recursive: true, force: true });
  for (const deadline = Date.now() + 10_000; (await targets()) && Date.now() < deadline; ) await sleep(100);
}

current = await startBrowser();
let handedOver = false;
let fresh = Promise.resolve();
control = createServer((_req, res) => {
  // One at a time: a second request waits for the browser the first one asked for.
  fresh = fresh.then(async () => {
    if (handedOver) {
      await stopBrowser(current);
      current = await startBrowser();
    }
    handedOver = true;
  });
  fresh.then(
    () => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ cdp: `http://127.0.0.1:${cdpPort}` }));
    },
    (err) => {
      res.writeHead(500);
      res.end(String(err));
    },
  );
});
control.listen(controlPort, "127.0.0.1");

server = createServer((_req, res) => {
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify({ cdp: `http://127.0.0.1:${cdpPort}`, extension: ext }));
});
server.listen(readyPort, "127.0.0.1", () => {
  console.log(`chromium with ${ext} on CDP port ${cdpPort}, control on port ${controlPort}, ready on port ${server.address().port}`);
});
