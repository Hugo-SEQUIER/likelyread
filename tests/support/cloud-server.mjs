// Starts a local Likelyread Cloud for the e2e run (e2e.cloud.config.ts), with the two
// outside services it talks to replaced, on FAKE_PORT:
//   - Google sign-in, which signs in at once as ann@example.com, the admin, or as
//     bob@example.com when the browser carries the cookie e2e_person=bob;
//   - an app that connects over OAuth the way Claude Code does: its client metadata
//     document, and the loopback page Likelyread sends the browser back to.
// The server runs on 127.0.0.1, so it accepts that app's document on this computer.
//
//   node tests/support/cloud-server.mjs --port 8783
//
// Billing stays off (no Stripe values). SIGTERM stops everything and deletes the database.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { FAKE, PEOPLE, REVIEWER, TEST_APP } from "./cloud-fakes.mjs";

const args = process.argv.slice(2);
const port = Number(args[args.indexOf("--port") + 1] || 8783);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const python = process.env.PYTHON || path.join(root, "server", ".venv", "bin", "python");
if (!existsSync(python)) {
  console.error(`no Python at ${python}: create server/.venv (see the README's Development section) or set PYTHON`);
  process.exit(2);
}
const CLOUD = `http://127.0.0.1:${port}`;
const CLIENT_ID = "e2e-client.apps.googleusercontent.com";
const b64url = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");

const fakes = createServer(async (req, res) => {
  const u = new URL(req.url, FAKE);
  if (u.pathname === "/google/auth") {
    // A cookie reaches every port of 127.0.0.1, so a test sets it for FAKE.
    const named = /(?:^|;\s*)e2e_person=(\w+)/.exec(req.headers.cookie || "")?.[1];
    const back = new URL(u.searchParams.get("redirect_uri"));
    back.searchParams.set("code", `good-code-${named && Object.hasOwn(PEOPLE, named) ? named : "ann"}`);
    back.searchParams.set("state", u.searchParams.get("state"));
    res.writeHead(302, { location: back.toString() });
    return res.end();
  }
  if (u.pathname === "/google/token" && req.method === "POST") {
    let body = "";
    for await (const chunk of req) body += chunk;
    const who = /^good-code-(\w+)$/.exec(new URLSearchParams(body).get("code") || "")?.[1];
    if (!who || !Object.hasOwn(PEOPLE, who)) {
      res.writeHead(400, { "content-type": "application/json" });
      return res.end("{}");
    }
    const claims = { iss: "https://accounts.google.com", aud: CLIENT_ID, sub: `google-${who}`, email: PEOPLE[who], email_verified: true, exp: Math.floor(Date.now() / 1000) + 600 };
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(JSON.stringify({ id_token: `${b64url({ alg: "RS256" })}.${b64url(claims)}.sig` }));
  }
  if (u.pathname === "/app/client.json") {
    res.writeHead(200, { "content-type": "application/json", "cache-control": "max-age=60" });
    return res.end(JSON.stringify({
      client_id: TEST_APP.clientId, client_name: TEST_APP.name, redirect_uris: ["http://127.0.0.1/app/callback"],
      grant_types: ["authorization_code", "refresh_token"], response_types: ["code"], token_endpoint_auth_method: "none",
    }));
  }
  if (u.pathname === "/app/callback") {
    const ok = u.searchParams.has("code");
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    return res.end(`<!doctype html><title>Test app</title><h1>${ok ? "Signed in" : "Not signed in"}</h1>`);
  }
  res.writeHead(404);
  res.end();
});
fakes.listen(new URL(FAKE).port, "127.0.0.1");

const work = mkdtempSync(path.join(os.tmpdir(), "likelyread-cloud-e2e-"));
const server = spawn(python, ["-m", "uvicorn", "app:create_app", "--factory", "--host", "127.0.0.1", "--port", String(port), "--log-level", "warning"], {
  cwd: path.join(root, "server"),
  stdio: ["ignore", "inherit", "inherit"],
  env: {
    PATH: process.env.PATH, HOME: process.env.HOME,
    MODE: "cloud", DB_PATH: path.join(work, "cloud.db"), PUBLIC_URL: CLOUD,
    SESSION_SECRET: "e2e-session-secret-0123456789abcdef", GOOGLE_CLIENT_ID: CLIENT_ID, GOOGLE_CLIENT_SECRET: "x",
    GOOGLE_AUTH_URL: `${FAKE}/google/auth`, GOOGLE_TOKEN_URL: `${FAKE}/google/token`,
    ADMIN_EMAILS: PEOPLE.ann,
    REVIEW_EMAIL: REVIEWER.email, REVIEW_PASSWORD: REVIEWER.password,
  },
});

let stopping = false;
const stop = (code) => {
  if (stopping) return;
  stopping = true;
  fakes.close();
  server.kill("SIGTERM");
  rmSync(work, { recursive: true, force: true });
  process.exit(code);
};
process.on("SIGTERM", () => stop(0));
process.on("SIGINT", () => stop(0));
server.on("exit", (code) => {
  if (!stopping) console.error(`the cloud server exited with ${code}`);
  stop(1);
});
console.log(`Likelyread Cloud on ${CLOUD}, fake Google and test app on ${FAKE}`);
