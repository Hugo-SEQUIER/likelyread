/**
 * ComposeTracker against a trimmed copy of Gmail's compose markup.
 * This checks our logic; it cannot prove that live Gmail still uses this markup.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { ComposeTracker } from "../src/compose";
import type { ContentConfig, PendingEmail } from "../src/shared";

const SERVER = "https://t.example.com";

const GMAIL_COMPOSE = `
<div role="dialog" class="nH Hd">
  <form>
    <div class="aoD hl"><div name="to">
      <div role="option" data-hovercard-id="ann@acme.com">Ann</div>
      <span email="bob@acme.com">Bob</span>
    </div></div>
    <input name="cc" type="hidden" value="Carol &lt;carol@acme.com&gt;">
    <input name="subjectbox" value="Quarterly update">
  </form>
  <div class="Am Al editable" contenteditable="true" role="textbox" g_editable="true">
    <div>Hello team</div>
    <div class="gmail_quote"><span email="quoted@old.com">quoted</span></div>
  </div>
  <table><tbody><tr class="btC">
    <td class="gU Up"><div class="J-J5-Ji btA"><div class="dC">
      <div role="button" class="T-I J-J5-Ji aoO v7 T-I-atl L3" tabindex="1">Send</div>
    </div></div></td>
    <td class="other">...</td>
  </tr></tbody></table>
</div>`;

function setup(cfg: Partial<ContentConfig> = {}) {
  const dom = new JSDOM(`<!doctype html><body><h2 class="hP">Thread subject</h2>${GMAIL_COMPOSE}</body>`);
  const doc = dom.window.document;
  const registered: PendingEmail[] = [];
  const suppressed: string[] = [];
  let n = 0;
  let now = 1_000_000;
  const config: ContentConfig = { configured: true, serverUrl: SERVER, trackByDefault: true, campaign: "Q4", ...cfg };
  const tracker = new ComposeTracker(doc, {
    getConfig: () => config,
    register: (e) => registered.push(e),
    suppress: (id) => suppressed.push(id),
    newId: () => `0f8fad5b-d9cb-469f-a165-70867728950${n++}`,
    now: () => now,
  });
  tracker.install();
  tracker.scan();
  const win = dom.window;
  const body = doc.querySelector<HTMLElement>('[contenteditable="true"]')!;
  const send = doc.querySelector<HTMLElement>(".aoO")!;
  const click = () => {
    send.dispatchEvent(new win.MouseEvent("mousedown", { bubbles: true, button: 0 }));
    send.dispatchEvent(new win.MouseEvent("click", { bubbles: true, button: 0 }));
  };
  const tick = (ms: number) => (now += ms);
  return { dom, doc, win, tracker, body, send, click, registered, suppressed, config, tick };
}

const pixels = (body: HTMLElement) => body.querySelectorAll(`img[src^="${SERVER}/p/"]`);

test("adds a toggle next to Send, on by default", () => {
  const { doc } = setup();
  const toggle = doc.querySelector(".mailpeek-toggle")!;
  assert.ok(toggle, "toggle injected");
  assert.equal(toggle.getAttribute("aria-pressed"), "true");
  assert.match(toggle.getAttribute("title")!, /campaign: Q4/);
  assert.equal(toggle.closest("td")!.previousElementSibling!.className, "gU Up");
});

test("send injects exactly one pixel and registers once with subject, recipients and campaign", () => {
  const { body, click, registered } = setup();
  click();
  assert.equal(pixels(body).length, 1);
  const img = pixels(body)[0] as HTMLImageElement;
  assert.equal(img.getAttribute("width"), "1");
  assert.equal(img.getAttribute("alt"), "");
  assert.match(img.getAttribute("style")!, /display:none/);
  assert.equal(body.lastElementChild, img, "pixel is at the end of the body");
  assert.equal(registered.length, 1, "mousedown + click register once");
  const r = registered[0];
  assert.equal(r.subject, "Quarterly update");
  assert.deepEqual(r.recipients.sort(), ["ann@acme.com", "bob@acme.com", "carol@acme.com"]);
  assert.equal(r.campaign, "Q4");
  assert.ok(img.src.endsWith(`/p/${r.id}.gif`));
});

test("a second send of the same compose (failed send, undo) reuses the pixel", () => {
  const { body, click, registered, tick } = setup();
  click();
  tick(10_000);
  click();
  assert.equal(pixels(body).length, 1);
  assert.equal(registered.length, 2);
  assert.equal(registered[0].id, registered[1].id);
});

test("Ctrl+Enter inside the compose triggers tracking", () => {
  const { body, win, registered } = setup();
  body.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }));
  assert.equal(pixels(body).length, 1);
  assert.equal(registered.length, 1);
});

test("plain Enter in the body does not trigger tracking", () => {
  const { body, win, registered } = setup();
  body.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(pixels(body).length, 0);
  assert.equal(registered.length, 0);
});

test("toggling off sends untracked", () => {
  const { doc, body, click, registered, win } = setup();
  doc.querySelector(".mailpeek-toggle")!.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  click();
  assert.equal(pixels(body).length, 0);
  assert.equal(registered.length, 0);
});

test("trackByDefault=false starts off", () => {
  const { body, click, registered } = setup({ trackByDefault: false });
  click();
  assert.equal(pixels(body).length + registered.length, 0);
});

test("unconfigured extension never injects", () => {
  const { body, click, registered } = setup({ configured: false });
  click();
  assert.equal(pixels(body).length + registered.length, 0);
});

test("the send event is never cancelled, even when registration throws", () => {
  const { tracker, win, doc, body, send } = setup();
  // Break the dependency to simulate an extension failure.
  (tracker as unknown as { deps: { register: () => void } }).deps.register = () => {
    throw new Error("boom");
  };
  let gmailSaw = false;
  doc.addEventListener("click", (e) => {
    gmailSaw = !e.defaultPrevented;
  });
  const ev = new win.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 });
  send.dispatchEvent(ev);
  assert.equal(gmailSaw, true, "Gmail's own handler still runs, event not prevented");
  assert.equal(pixels(body).length, 1);
});

test("a compose without a recognisable Send button is left untouched", () => {
  const dom = new JSDOM(`<body><div contenteditable="true" role="textbox" g_editable="true">x</div></body>`);
  const t = new ComposeTracker(dom.window.document, {
    getConfig: () => ({ configured: true, serverUrl: SERVER, trackByDefault: true, campaign: "" }),
    register: () => assert.fail("should not register"),
    suppress: () => undefined,
    newId: () => "x",
    now: () => 0,
  });
  t.scan();
  assert.equal(dom.window.document.querySelector(".mailpeek-toggle"), null);
});

test("viewing our own sent email sends a suppress ping, throttled, but not for the compose pixel", () => {
  const { doc, click, suppressed, tick, tracker } = setup();
  click(); // pixel now sits in the contenteditable: must not trigger suppress
  tracker.scan();
  assert.equal(suppressed.length, 0);
  const id = "9b2a6f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b";
  const msg = doc.createElement("div");
  msg.className = "a3s";
  msg.innerHTML = `<img src="https://ci3.googleusercontent.com/meips/AbC=s0-d-e1-ft#${SERVER}/p/${id}.gif">`;
  doc.body.append(msg);
  tracker.scan();
  tracker.scan();
  assert.deepEqual(suppressed, [id]);
  tick(25_000);
  tracker.scan();
  assert.deepEqual(suppressed, [id, id]);
});
