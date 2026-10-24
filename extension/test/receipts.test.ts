/**
 * Receipts against a trimmed copy of an opened Gmail conversation (header markup
 * cross-checked with gmail.js). It checks our logic; it cannot prove that live
 * Gmail still uses this markup.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { describeStatus, Receipts } from "../src/receipts";
import type { ContentConfig, EmailStatus, StatusReply } from "../src/shared";

const SERVER = "https://t.example.com";
const ME = "you@example.com";
const A = "9b2a6f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b";
const B = "0f8fad5b-d9cb-469f-a165-708677289501";
const NOW = 1_800_000_000_000;

const pixel = (id: string) => `<img src="https://ci3.googleusercontent.com/meips/AbC=s0-d-e1-ft#${SERVER}/p/${id}.gif">`;

/** One message of a conversation: the header with sender and date, then the body. */
function message(sender: string, body: string) {
  return `<div class="adn"><div class="gs">
    <div class="gE"><table class="cf gJ"><tbody><tr>
      <td class="gF"><h3 class="iw"><span class="gD" email="${sender}">Sender</span></h3></td>
      <td class="gH"><div class="gK"><span class="g3" title="Oct 24, 2026, 2:02 PM">2:02 PM</span></div></td>
    </tr></tbody></table></div>
    <div class="a3s">${body}</div>
  </div></div>`;
}

function setup(html: string, replies: StatusReply[] = [], cfg: Partial<ContentConfig> = {}) {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`, { url: "https://mail.google.com/mail/u/0/#inbox/abc" });
  const doc = dom.window.document;
  const asked: string[][] = [];
  let now = NOW;
  const pending: (() => void)[] = [];
  const config: ContentConfig = { configured: true, serverUrl: SERVER, trackByDefault: true, campaign: "", campaigns: [], senders: [], ...cfg };
  const receipts = new Receipts(doc, {
    getConfig: () => config,
    accountEmail: () => ME,
    status: (ids) => {
      asked.push(ids);
      const reply = replies.shift() ?? {};
      return new Promise((resolve) => pending.push(() => resolve(reply)));
    },
    now: () => now,
  });
  // Answer every question asked so far, then let the promise callbacks run.
  const answer = async () => {
    for (const resolve of pending.splice(0)) resolve();
    await new Promise((r) => setTimeout(r, 0));
  };
  const badges = () => Array.from(doc.querySelectorAll<HTMLAnchorElement>("a.lr-receipt"));
  return { doc, receipts, asked, answer, badges, tick: (ms: number) => (now += ms) };
}

const opened = (opens: number): EmailStatus => ({ status: "opened", opens, last_open: NOW / 1000 - 300, self: 0 });
const unopened = (self = 0): EmailStatus => ({ status: "not_opened", opens: 0, last_open: null, self });

test("two red ticks once opened, before the date, linking to the email in the dashboard", async () => {
  const { receipts, asked, answer, badges, doc } = setup(message(ME, `Hi${pixel(A)}`), [{ [A]: opened(2) }]);
  receipts.scan();
  assert.deepEqual(asked, [[A]]);
  assert.equal(badges().length, 0, "nothing until the server answers");
  await answer();
  const [badge] = badges();
  assert.equal(badge.textContent, "Opened ×2");
  assert.ok(badge.classList.contains("is-opened"));
  assert.equal(badge.href, `${SERVER}/#/email/${A}`);
  assert.equal(badge.target, "_blank");
  assert.equal(badge.nextElementSibling, doc.querySelector("span.g3"));
  assert.match(badge.getAttribute("aria-label")!, /Opened 2 times, most recently 5 min ago/);
});

test("one grey tick says what is known: not opened, seen by you only, or Apple's prefetch", () => {
  assert.deepEqual(describeStatus(unopened(), NOW), { opened: false, label: "Not opened yet", detail: "Tracked. Nobody has opened it yet." });
  assert.equal(describeStatus(unopened(2), NOW).label, "Seen by you only");
  assert.equal(describeStatus({ status: "prefetch_only", opens: 0, last_open: null, self: 1 }, NOW).label, "Prefetch only");
  assert.equal(describeStatus(opened(1), NOW).label, "Opened");
  assert.equal(describeStatus(opened(1), NOW).detail, "Opened once, 5 min ago.");
  for (const st of [unopened(), unopened(1), opened(3)]) assert.doesNotMatch(describeStatus(st, NOW).detail, /\bread\b/i);
});

test("a status the server does not know, or could not give, shows nothing", async () => {
  const { receipts, answer, badges } = setup(message(ME, pixel(A)) + message(ME, pixel(B)), [{ [A]: null }]);
  receipts.scan();
  await answer();
  assert.equal(badges().length, 0);
});

test("the recipient's view and quoted history get no ticks", async () => {
  const quoted = `Thanks<div class="m_123gmail_quote">${pixel(B)}</div>`;
  const { receipts, asked } = setup(message("ann@acme.com", pixel(A)) + message(ME, quoted));
  receipts.scan();
  assert.deepEqual(asked, [], "Ann's email in Ann's inbox is not ours; a quoted pixel is an earlier email's");
});

test("a reply carries its own pixel after the quoted one: the ticks are for its own", async () => {
  const reply = `Sure<div class="gmail_quote">${pixel(B)}</div>${pixel(A)}`;
  const { receipts, asked, answer, badges } = setup(message(ME, reply), [{ [A]: opened(1) }]);
  receipts.scan();
  assert.deepEqual(asked, [[A]]);
  await answer();
  assert.equal(badges()[0].textContent, "Opened");
});

test("rescans leave the DOM alone, ask again only once the status is a minute old, and update the ticks", async () => {
  const { receipts, asked, answer, badges, tick } = setup(message(ME, pixel(A)), [{ [A]: unopened() }, { [A]: opened(1) }]);
  receipts.scan();
  await answer();
  const first = badges()[0];
  assert.equal(first.textContent, "Not opened yet");
  receipts.scan();
  receipts.scan();
  assert.equal(asked.length, 1);
  assert.equal(badges()[0], first);
  tick(61_000);
  receipts.scan();
  receipts.scan();
  assert.equal(asked.length, 2, "one question in flight at a time");
  await answer();
  assert.equal(badges().length, 1);
  assert.equal(badges()[0].textContent, "Opened");
});

test("an unanswered question keeps the last status and asks again later", async () => {
  const { receipts, asked, answer, badges, tick } = setup(message(ME, pixel(A)), [{ [A]: opened(2) }, {}]);
  receipts.scan();
  await answer();
  tick(61_000);
  receipts.scan();
  await answer();
  assert.equal(badges()[0].textContent, "Opened ×2");
  tick(61_000);
  receipts.scan();
  assert.equal(asked.length, 3);
});

test("clicks on the ticks do not reach Gmail's header", async () => {
  const { doc, receipts, answer, badges } = setup(message(ME, pixel(A)), [{ [A]: opened(1) }]);
  let reached = 0;
  doc.querySelector(".gE")!.addEventListener("click", () => reached++);
  receipts.scan();
  await answer();
  badges()[0].dispatchEvent(new doc.defaultView!.MouseEvent("click", { bubbles: true, cancelable: true }));
  assert.equal(reached, 0);
});

test("nothing when the extension is not set up, and a settings change clears the ticks", async () => {
  const off = setup(message(ME, pixel(A)), [], { configured: false });
  off.receipts.scan();
  assert.deepEqual(off.asked, []);

  const on = setup(message(ME, pixel(A)), [{ [A]: opened(1) }, { [A]: opened(1) }]);
  on.receipts.scan();
  await on.answer();
  on.receipts.refresh();
  assert.equal(on.badges().length, 0);
  on.receipts.scan();
  assert.equal(on.asked.length, 2, "asked again after the refresh");
});
