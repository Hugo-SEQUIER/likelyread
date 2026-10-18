/**
 * ComposeTracker against a trimmed copy of Gmail's compose markup (structure
 * cross-checked with gmail.js). It checks our logic; it cannot prove that live
 * Gmail still uses this markup.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { ComposeTracker, extractEmail } from "../src/compose";
import type { ContentConfig, PendingEmail } from "../src/shared";

const SERVER = "https://t.example.com";
const ME = "you@example.com";

// Recipients and subject sit in a header table; the body and toolbar sit in a
// separate inner block. Only div.M9 holds all of them.
const gmailCompose = (from = "") => `
<div class="M9">
  <table class="header"><tbody>
    <tr class="bzf"><td><div name="to">
      <div class="afV" data-hovercard-id="ann@acme.com"><div class="akl">Ann</div></div>
      <div class="afV" data-hovercard-id="bob@acme.com"><div class="akl">Bob</div></div>
    </div></td></tr>
    <tr class="bzf"><td><div name="cc"><div class="afV" data-hovercard-id="carol@acme.com">Carol</div></div></td></tr>
  </tbody></table>
  ${from}
  <input name="subjectbox" value="Quarterly update">
  <div class="inner">
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
  </div>
</div>`;

function setup(cfg: Partial<ContentConfig> = {}, opts: { from?: string; account?: string | null; hash?: string } = {}) {
  const dom = new JSDOM(`<!doctype html><body><h2 class="hP">Thread subject</h2>${gmailCompose(opts.from)}</body>`, {
    url: `https://mail.google.com/mail/u/0/${opts.hash ?? "#inbox"}`,
  });
  const doc = dom.window.document;
  const registered: PendingEmail[] = [];
  const suppressed: string[] = [];
  const campaignsSet: string[] = [];
  let n = 0;
  let now = 1_000_000;
  const config: ContentConfig = {
    configured: true,
    serverUrl: SERVER,
    trackByDefault: true,
    campaign: "Q4",
    campaigns: ["Q4", "Seed round"],
    senders: [],
    ...cfg,
  };
  const tracker = new ComposeTracker(doc, {
    getConfig: () => config,
    register: (e) => registered.push(e),
    suppress: (id) => suppressed.push(id),
    setCampaign: (c) => campaignsSet.push(c),
    accountEmail: () => (opts.account === undefined ? ME : opts.account),
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
  const eye = () => doc.querySelector<HTMLElement>(".lr-eye")!;
  const picker = () => doc.querySelector<HTMLElement>(".lr-campaign")!;
  return { dom, doc, win, tracker, body, send, click, registered, suppressed, campaignsSet, config, tick, eye, picker };
}

const pixels = (body: HTMLElement) => body.querySelectorAll(`img[src^="${SERVER}/p/"]`);

/** A conversation message as Gmail renders it, with a proxied pixel inside. */
function message(doc: Document, sender: string, id: string) {
  const msg = doc.createElement("div");
  msg.className = "adn";
  msg.innerHTML = `<div class="gE"><span class="gD" email="${sender}">Sender</span></div>
    <div class="a3s"><img src="https://ci3.googleusercontent.com/meips/AbC=s0-d-e1-ft#${SERVER}/p/${id}.gif"></div>`;
  doc.body.append(msg);
  return msg;
}

test("adds the eye and the campaign picker next to Send, on by default", () => {
  const { eye, picker } = setup();
  assert.equal(eye().getAttribute("aria-pressed"), "true");
  assert.equal(picker().textContent, "Q4");
  assert.equal(eye().closest("td")!.previousElementSibling!.className, "gU Up");
});

test("send injects one pixel and registers subject, recipients, campaign and sender", () => {
  const { body, click, registered } = setup();
  click();
  assert.equal(pixels(body).length, 1);
  const img = pixels(body)[0] as HTMLImageElement;
  assert.equal(img.getAttribute("width"), "1");
  assert.match(img.getAttribute("style")!, /display:none/);
  assert.equal(body.lastElementChild, img, "pixel is at the end of the body");
  assert.equal(registered.length, 1, "mousedown + click register once");
  const r = registered[0];
  assert.equal(r.subject, "Quarterly update");
  assert.deepEqual(r.recipients.sort(), ["ann@acme.com", "bob@acme.com", "carol@acme.com"]);
  assert.equal(r.campaign, "Q4");
  assert.equal(r.sender, ME);
  assert.ok(img.src.endsWith(`/p/${r.id}.gif`));
});

test("the From field wins over the account address", () => {
  const { click, registered } = setup({}, { from: '<input name="from" type="hidden" value="Team Sales <Sales@Example.com>">' });
  click();
  assert.equal(registered[0].sender, "sales@example.com");
});

test("no detectable sender is sent as null, not guessed", () => {
  const { click, registered } = setup({}, { account: null });
  click();
  assert.equal(registered[0].sender, null);
});

test("picking a campaign in the compose window is used for this email and becomes the default", () => {
  const { doc, win, picker, click, registered, campaignsSet } = setup();
  picker().dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  const pop = doc.querySelector(".lr-pop")!;
  const options = [...pop.querySelectorAll<HTMLElement>(".lr-opt")].map((o) => o.textContent);
  assert.deepEqual(options, ["No campaign", "Q4", "Seed round"]);
  [...pop.querySelectorAll<HTMLElement>(".lr-opt")][2].dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  assert.equal(doc.querySelector(".lr-pop"), null, "popover closes");
  assert.equal(picker().textContent, "Seed round");
  assert.deepEqual(campaignsSet, ["Seed round"]);
  click();
  assert.equal(registered[0].campaign, "Seed round");
});

test("a new campaign can be typed in the picker", () => {
  const { doc, win, picker, click, registered } = setup();
  picker().dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  const input = doc.querySelector<HTMLInputElement>(".lr-new")!;
  input.value = "  Podcast   pitches ";
  let gmailSawKey = false;
  doc.body.addEventListener("keydown", () => (gmailSawKey = true));
  input.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(gmailSawKey, false, "typing in the picker never reaches Gmail");
  assert.equal(picker().textContent, "Podcast pitches");
  click();
  assert.equal(registered[0].campaign, "Podcast pitches");
});

test("'No campaign' sends without a campaign; Escape and outside clicks close the picker", () => {
  const { doc, win, picker, click, registered } = setup();
  picker().dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  doc.querySelector(".lr-pop")!.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(doc.querySelector(".lr-pop"), null);
  picker().dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  doc.body.dispatchEvent(new win.MouseEvent("mousedown", { bubbles: true }));
  assert.equal(doc.querySelector(".lr-pop"), null);
  picker().dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  doc.querySelector<HTMLElement>(".lr-opt")!.dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  assert.equal(picker().textContent, "No campaign");
  click();
  assert.equal(registered[0].campaign, null);
});

test("a compose opened before the settings arrived picks them up", () => {
  // Gmail can render a compose before the background answers getConfig.
  const a = setup({ trackByDefault: true, campaign: "" });
  Object.assign(a.config, { trackByDefault: false, campaign: "Q4 outreach" });
  a.tracker.refresh();
  assert.equal(a.eye().getAttribute("aria-pressed"), "false");
  a.click();
  assert.equal(pixels(a.body).length + a.registered.length, 0, "tracking off by default means no pixel");

  const b = setup({ campaign: "" });
  Object.assign(b.config, { campaign: "Q4 outreach" });
  b.tracker.refresh();
  b.click();
  assert.equal(b.registered[0].campaign, "Q4 outreach");
});

test("a choice made in the compose survives a settings change", () => {
  const { win, eye, picker, doc, config, tracker, click, registered } = setup();
  picker().dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  [...doc.querySelectorAll<HTMLElement>(".lr-opt")][2].dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  Object.assign(config, { campaign: "Something else" });
  tracker.refresh();
  assert.equal(picker().textContent, "Seed round");
  assert.equal(eye().getAttribute("aria-pressed"), "true");
  click();
  assert.equal(registered[0].campaign, "Seed round");
});

test("the picker is hidden while tracking is off", () => {
  const { win, eye, picker } = setup();
  eye().dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
  assert.equal(picker().hidden, true);
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

test("Ctrl+Enter inside the compose triggers tracking; plain Enter does not", () => {
  const a = setup();
  a.body.dispatchEvent(new a.win.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(a.registered.length, 0);
  a.body.dispatchEvent(new a.win.KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }));
  assert.equal(pixels(a.body).length, 1);
  assert.equal(a.registered.length, 1);
});

test("toggling off sends untracked; trackByDefault=false and unconfigured never inject", () => {
  const a = setup();
  a.eye().dispatchEvent(new a.win.MouseEvent("click", { bubbles: true }));
  a.click();
  assert.equal(pixels(a.body).length + a.registered.length, 0);
  const b = setup({ trackByDefault: false });
  b.click();
  assert.equal(pixels(b.body).length + b.registered.length, 0);
  const c = setup({ configured: false });
  c.click();
  assert.equal(pixels(c.body).length + c.registered.length, 0);
});

test("the send event is never cancelled, even when registration throws", () => {
  const { tracker, win, doc, body, send } = setup();
  (tracker as unknown as { deps: { register: () => void } }).deps.register = () => {
    throw new Error("boom");
  };
  let gmailSaw = false;
  doc.addEventListener("click", (e) => {
    gmailSaw = !e.defaultPrevented;
  });
  send.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true, button: 0 }));
  assert.equal(gmailSaw, true, "Gmail's own handler still runs, event not prevented");
  assert.equal(pixels(body).length, 1);
});

test("a compose without a recognisable Send button is left untouched", () => {
  const dom = new JSDOM(`<body><div contenteditable="true" role="textbox" g_editable="true">x</div></body>`);
  const t = new ComposeTracker(dom.window.document, {
    getConfig: () => ({ configured: true, serverUrl: SERVER, trackByDefault: true, campaign: "", campaigns: [], senders: [] }),
    register: () => assert.fail("should not register"),
    suppress: () => undefined,
    setCampaign: () => undefined,
    accountEmail: () => ME,
    newId: () => "x",
    now: () => 0,
  });
  t.scan();
  assert.equal(dom.window.document.querySelector(".lr-control"), null);
});

const OLD = "9b2a6f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b";

test("our own sent message in a conversation is suppressed, throttled; the compose pixel is not", () => {
  const { doc, click, suppressed, tick, tracker } = setup();
  click();
  tracker.scan();
  assert.equal(suppressed.length, 0, "compose pixel is handled by the grace window");
  message(doc, ME, OLD);
  tracker.scan();
  tracker.scan();
  assert.deepEqual(suppressed, [OLD]);
  tick(25_000);
  tracker.scan();
  assert.deepEqual(suppressed, [OLD, OLD]);
});

test("the recipient's view in this same browser is NOT suppressed: it is a real open", () => {
  const { doc, tracker, suppressed } = setup({}, { account: "ann@acme.com" });
  message(doc, ME, OLD); // Hugo's email, opened in Ann's inbox
  tracker.scan();
  assert.deepEqual(suppressed, []);
});

test("a send-as address used before counts as ours", () => {
  const { doc, tracker, suppressed } = setup({ senders: ["sales@example.com"] });
  message(doc, "sales@example.com", OLD);
  tracker.scan();
  assert.deepEqual(suppressed, [OLD]);
});

test("without a sender line, only the Sent folder counts as ours", () => {
  const inbox = setup();
  const m = inbox.doc.createElement("div");
  m.innerHTML = `<img src="https://ci3.googleusercontent.com/meips/x#${SERVER}/p/${OLD}.gif">`;
  inbox.doc.body.append(m);
  inbox.tracker.scan();
  assert.deepEqual(inbox.suppressed, []);
  const sent = setup({}, { hash: "#sent" });
  const m2 = sent.doc.createElement("div");
  m2.innerHTML = `<img src="https://ci3.googleusercontent.com/meips/x#${SERVER}/p/${OLD}.gif">`;
  sent.doc.body.append(m2);
  sent.tracker.scan();
  assert.deepEqual(sent.suppressed, [OLD]);
});

test("a reply quoting an earlier tracked email gets a new pixel id", () => {
  const { body, click, registered } = setup();
  body.querySelector(".gmail_quote")!.insertAdjacentHTML(
    "beforeend",
    `<img src="${SERVER}/p/${OLD}.gif"><img src="https://ci3.googleusercontent.com/meips/x#${SERVER}/p/${OLD}.gif">`,
  );
  click();
  assert.equal(registered.length, 1);
  assert.notEqual(registered[0].id, OLD);
  assert.equal(pixels(body).length, 2, "old quoted pixel untouched, one new pixel appended");
});

test("a quoted pixel does not force tracking on", () => {
  const dom = new JSDOM(`<body>${gmailCompose()}</body>`);
  dom.window.document.querySelector(".gmail_quote")!.insertAdjacentHTML("beforeend", `<img src="${SERVER}/p/${OLD}.gif">`);
  const t = new ComposeTracker(dom.window.document, {
    getConfig: () => ({ configured: true, serverUrl: SERVER, trackByDefault: false, campaign: "", campaigns: [], senders: [] }),
    register: () => undefined,
    suppress: () => undefined,
    setCampaign: () => undefined,
    accountEmail: () => ME,
    newId: () => "x",
    now: () => 0,
  });
  t.scan();
  assert.equal(dom.window.document.querySelector(".lr-eye")!.getAttribute("aria-pressed"), "false");
});

test("the control comes back if Gmail re-renders the toolbar", () => {
  const { doc, tracker } = setup();
  doc.querySelector(".lr-control")!.closest("td")!.remove();
  assert.equal(doc.querySelector(".lr-control"), null);
  tracker.scan();
  assert.ok(doc.querySelector(".lr-control"));
});

test("extractEmail reads Gmail's tab title", () => {
  assert.equal(extractEmail("Inbox (3) - Sam.Rivera@gmail.com - Gmail"), "sam.rivera@gmail.com");
  assert.equal(extractEmail("Gmail"), null);
});
