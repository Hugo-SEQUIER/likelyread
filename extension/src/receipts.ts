/**
 * Open status on our own tracked emails in an opened Gmail conversation, next to
 * the message's date: one grey tick while nobody has opened it, two red ticks once
 * someone has, two green ticks once someone replied. Each links to the email in the
 * dashboard. Replies seen in the conversation are reported to the server. Like compose.ts it never
 * calls chrome.*, so it can be tested against a fake Gmail DOM; content.ts wires it.
 *
 * Rule: no answer, no ticks. A failed or missing status shows nothing rather than a
 * guess, and the words are the dashboard's: "opened", never "read".
 */
import { ownAddresses, ownPixels } from "./compose";
import { SEL } from "./gmail-selectors";
import { relTime } from "./lib";
import type { ContentConfig, EmailStatus, StatusReply } from "./shared";

export interface ReceiptDeps {
  getConfig(): ContentConfig;
  accountEmail(): string | null;
  /** Ask the server about these emails; resolves with what it could answer. */
  status(ids: string[]): Promise<StatusReply>;
  /** Report a reply to one of our emails; resolves true once the server answered. */
  reply(id: string, from: string, age?: number): Promise<boolean>;
  now(): number;
}

export const RECEIPT_CLASS = "lr-receipt";
/** A status this old is asked for again on the next scan. */
const FRESH_MS = 60_000;
/** Quoted history in a sent message: pixels of earlier emails live there. */
const QUOTED = '[class*="gmail_quote"], blockquote';
/** The oldest reply age the server takes, in seconds. */
const MAX_REPLY_AGE = 30 * 86400;

const ONE_TICK =
  '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" d="m3 8.5 3 3 7-7"/></svg>';
const TWO_TICKS =
  '<svg viewBox="0 0 20 16" width="20" height="16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" d="m1.5 8.5 3 3 7-7m-2.5 7 1.5 1.5 7-7"/></svg>';

export interface ReceiptView {
  opened: boolean;
  replied: boolean;
  /** The few words next to the ticks. */
  label: string;
  /** One sentence for the tooltip and screen readers. */
  detail: string;
}

/** What the ticks say about one email. */
export function describeStatus(st: EmailStatus, now: number): ReceiptView {
  const opened = st.status === "opened";
  if (st.replied_at) return { opened, replied: true, label: "Replied", detail: `Replied ${relTime(st.replied_at, now)}.` };
  if (opened) {
    const when = relTime(st.last_open, now);
    return st.opens > 1
      ? { opened, replied: false, label: `Opened ×${st.opens}`, detail: `Opened ${st.opens} times, most recently ${when}.` }
      : { opened, replied: false, label: "Opened", detail: `Opened once, ${when}.` };
  }
  if (st.status === "prefetch_only") {
    return { opened, replied: false, label: "Prefetch only", detail: "Not opened yet. Apple Mail loaded the image on delivery, which is not an open." };
  }
  if (st.self > 0) {
    return { opened, replied: false, label: "Seen by you only", detail: "Not opened yet. Only you have viewed it, and your own views never count." };
  }
  return { opened, replied: false, label: "Not opened yet", detail: "Tracked. Nobody has opened it yet." };
}

export class Receipts {
  private known = new Map<string, { at: number; status: EmailStatus | null }>();
  private asking = new Set<string>();
  /** Emails to ask about on the next scan, fresh or not: a reply was just reported. */
  private askAgain = new Set<string>();
  /** "id|sender" of the replies already reported from this page. */
  private reported = new Set<string>();

  constructor(private doc: Document, private deps: ReceiptDeps) {}

  /** Place or update the ticks of every own tracked message on screen, and ask the
   * server about the emails not known or no longer fresh. Touches the DOM only when
   * something changed, so it is safe to call on every mutation. */
  scan(): void {
    safe(() => {
      const cfg = this.deps.getConfig();
      if (!cfg.configured) return;
      const now = this.deps.now();
      const ask: string[] = [];
      const own = this.messages(cfg);
      for (const [message, id] of own) {
        const known = this.known.get(id);
        if ((!known || now - known.at > FRESH_MS || this.askAgain.has(id)) && !this.asking.has(id)) ask.push(id);
        this.render(message, id, known?.status ?? null, cfg, now);
      }
      if (ask.length) this.ask(ask);
      this.reportReplies(own, cfg, now);
    });
  }

  /** Forget every status: the server or the token may have changed. */
  refresh(): void {
    this.known.clear();
    for (const el of Array.from(this.doc.querySelectorAll(`.${RECEIPT_CLASS}`))) el.remove();
  }

  /** Each own message on screen, with the pixel it was sent with: its last one
   * outside quoted history (the extension appends the pixel at the very end). */
  private messages(cfg: ContentConfig): Map<Element, string> {
    const out = new Map<Element, string>();
    for (const { id, img, message } of ownPixels(this.doc, cfg, this.deps.accountEmail())) {
      if (message && !img.closest(QUOTED)) out.set(message, id);
    }
    return out;
  }

  /**
   * Replies on screen. In DOM order, a message whose sender is neither this account
   * nor one of its send-as addresses answers the nearest own message above it, and
   * is reported when that one is tracked. Each (email, sender) is reported once per page.
   *
   * Limit: a collapsed message has no body on screen, so a collapsed own email
   * shows no pixel. A reply under it is reported only once the user expands that
   * email; it is never credited to an older one.
   */
  private reportReplies(own: Map<Element, string>, cfg: ContentConfig, now: number): void {
    const account = this.deps.accountEmail();
    if (!account) return;
    const mine = ownAddresses(cfg, account);
    let above: string | null = null;
    for (const sender of Array.from(this.doc.querySelectorAll(SEL.messageSender))) {
      const message = sender.closest(SEL.messageContainer);
      const from = sender.getAttribute("email")?.trim().toLowerCase();
      if (!message || !from || sender.closest('[contenteditable="true"]')) continue;
      if (mine.has(from)) {
        above = own.get(message) ?? null;
        continue;
      }
      const id = above;
      if (!id || this.reported.has(`${id}|${from}`)) continue;
      this.reported.add(`${id}|${from}`);
      this.deps.reply(id, from, replyAge(message, now)).then(
        (ok) => {
          if (!ok) return;
          this.askAgain.add(id);
          this.scan();
        },
        () => undefined,
      );
    }
  }

  private ask(ids: string[]): void {
    for (const id of ids) {
      this.asking.add(id);
      this.askAgain.delete(id);
    }
    this.deps.status(ids).then(
      (reply) => this.settle(ids, reply || {}),
      () => this.settle(ids, {}),
    );
  }

  private settle(ids: string[], reply: StatusReply): void {
    const at = this.deps.now();
    for (const id of ids) {
      this.asking.delete(id);
      // Not answered (offline, server error): keep what we had and ask again later.
      const status = Object.hasOwn(reply, id) ? reply[id] : (this.known.get(id)?.status ?? null);
      this.known.set(id, { at, status });
    }
    this.scan();
  }

  private render(message: Element, id: string, status: EmailStatus | null, cfg: ContentConfig, now: number): void {
    let badge = message.querySelector<HTMLAnchorElement>(`a.${RECEIPT_CLASS}`);
    if (!status) {
      badge?.remove();
      return;
    }
    if (!badge) {
      const date = message.querySelector(SEL.messageDate);
      const sender = message.querySelector(SEL.messageSender);
      if (!date && !sender) return; // a header we do not recognise: show nothing
      badge = this.doc.createElement("a");
      badge.className = RECEIPT_CLASS;
      badge.target = "_blank";
      badge.rel = "noopener";
      // Gmail opens and closes a message on clicks in its header: keep ours to ourselves.
      for (const type of ["mousedown", "click"]) badge.addEventListener(type, (e) => e.stopPropagation());
      if (date) date.before(badge);
      else sender!.after(badge);
    }
    const view = describeStatus(status, now);
    const href = `${cfg.serverUrl}/#/email/${id}`;
    const key = `${href}|${view.label}|${view.detail}`;
    if (badge.dataset.key === key) return;
    badge.dataset.key = key;
    badge.href = href;
    badge.classList.toggle("is-opened", view.opened && !view.replied);
    badge.classList.toggle("is-replied", view.replied);
    badge.innerHTML = view.opened || view.replied ? TWO_TICKS : ONE_TICK;
    const label = this.doc.createElement("span");
    label.textContent = view.label;
    badge.append(label);
    badge.title = `${view.detail} Click to see it in your Likelyread dashboard.`;
    badge.setAttribute("aria-label", `Likelyread: ${view.detail} Open it in your dashboard.`);
  }
}

/** Seconds since a message was sent, from the date in its header. Undefined when
 * the date does not parse (Gmail writes it in the user's language) or is out of
 * the server's range. */
function replyAge(message: Element, now: number): number | undefined {
  const sent = Date.parse(message.querySelector(SEL.messageDate)?.getAttribute("title") || "");
  const age = Math.round((now - sent) / 1000);
  return age >= 0 && age <= MAX_REPLY_AGE ? age : undefined;
}

function safe(fn: () => void): void {
  try {
    fn();
  } catch (err) {
    console.warn("[likelyread] ignored error (Gmail is unaffected):", err);
  }
}
