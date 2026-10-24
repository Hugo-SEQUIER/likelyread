/**
 * Gmail compose integration. Kept free of chrome.* calls so it can be tested
 * against a fake Gmail DOM; content.ts wires it to the extension.
 *
 * Rule: nothing here may break sending. Every handler is wrapped in try/catch
 * and never cancels Gmail's own events. Only clicks inside our own controls
 * are stopped, so Gmail does not react to them.
 */
import { MAX_COMPOSE_DEPTH, SEL } from "./gmail-selectors";
import { cleanRecipients, extractPixelId, pixelUrl } from "./lib";
import type { ContentConfig, PendingEmail } from "./shared";

export interface ComposeDeps {
  getConfig(): ContentConfig;
  register(email: PendingEmail): void;
  suppress(id: string): void;
  /** Remember the campaign picked in a compose window as the new default. */
  setCampaign(name: string): void;
  /** The signed-in Gmail address, if it can be read from the page. */
  accountEmail(): string | null;
  newId(): string;
  now(): number;
}

const ENHANCED = "likelyreadEnhanced";
export const CONTROL_CLASS = "lr-control";
const TOAST_MS = 6000;

interface ComposeState {
  root: HTMLElement;
  enabled: boolean;
  campaign: string;
  control: HTMLElement;
  eye: HTMLElement;
  picker: HTMLElement;
  /** Daily pace warning, shown only when the sender is at its suggested limit. */
  pace: HTMLElement;
  pixelId: string | null;
  /** The user changed the eye or the campaign here: config updates no longer override it. */
  touched: boolean;
}

/** Quoted history in a reply. Pixels of earlier emails live here and must never be reused. */
const QUOTED = ".gmail_quote, .gmail_extra, blockquote";
const EMAIL_IN_TEXT = /[^\s<>"'()]+@[^\s<>"'()]+\.[a-z]{2,}/i;

const EYE_ON =
  '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M12 5c-5 0-9 4.5-10 7 1 2.5 5 7 10 7s9-4.5 10-7c-1-2.5-5-7-10-7zm0 11a4 4 0 110-8 4 4 0 010 8zm0-2a2 2 0 100-4 2 2 0 000 4z"/></svg>';
const EYE_OFF =
  '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M3.3 2 2 3.3l3.2 3.2C3.5 7.8 2.4 9.6 2 12c1 2.5 5 7 10 7 1.8 0 3.4-.5 4.8-1.3l3.9 3.9 1.3-1.3L3.3 2zM12 16a4 4 0 01-3.9-4.9l1.6 1.6A2 2 0 0011.3 14l1.6 1.6c-.3.2-.6.4-.9.4zm0-11c5 0 9 4.5 10 7-.5 1.2-1.6 2.9-3.2 4.3l-2.9-2.9A4 4 0 0010.6 8.2L8.4 6C9.5 5.4 10.7 5 12 5z"/></svg>';

export function extractEmail(text: string | null | undefined): string | null {
  const m = (text || "").match(EMAIL_IN_TEXT);
  return m ? m[0].toLowerCase() : null;
}

export class ComposeTracker {
  private composes = new Map<HTMLElement, ComposeState>(); // body -> state
  private lastRegistered = new Map<string, number>();
  private lastSuppressed = new Map<string, number>();
  private popover: { el: HTMLElement; state: ComposeState; cleanup: () => void } | null = null;
  private toast: { el: HTMLElement; timer: number } | null = null;

  constructor(private doc: Document, private deps: ComposeDeps) {}

  /** Wire document-level listeners. Capture phase so we run before Gmail's send. */
  install(): void {
    const opts = { capture: true };
    this.doc.addEventListener("mousedown", (e) => this.safe(() => this.onPointer(e as MouseEvent)), opts);
    this.doc.addEventListener("click", (e) => this.safe(() => this.onPointer(e as MouseEvent)), opts);
    this.doc.addEventListener("keydown", (e) => this.safe(() => this.onKey(e as KeyboardEvent)), opts);
  }

  /** Scan the document for new compose windows and for pixels of our own sent emails. */
  scan(): void {
    this.safe(() => {
      for (const body of Array.from(this.doc.querySelectorAll<HTMLElement>(SEL.composeBody))) {
        if (!body.dataset[ENHANCED]) this.enhance(body);
      }
      for (const [body, st] of this.composes) {
        if (!body.isConnected) {
          this.composes.delete(body);
          if (this.popover?.state === st) this.closePicker();
        } else {
          if (!st.control.isConnected) this.insertControl(st); // Gmail re-rendered the toolbar
          this.updatePace(st); // the From address can change while composing
        }
      }
    });
    this.safe(() => this.scanForOwnPixels());
  }

  /** Apply a new configuration. A compose window opened before the configuration
   * arrived got placeholder defaults, so reseed the ones the user has not touched. */
  refresh(): void {
    const cfg = this.deps.getConfig();
    for (const st of this.composes.values()) {
      this.safe(() => {
        if (!st.touched) {
          st.enabled = cfg.trackByDefault || !!st.pixelId;
          st.campaign = cfg.campaign || "";
        }
        this.render(st);
      });
    }
  }

  isEnabled(body: HTMLElement): boolean {
    return this.composes.get(body)?.enabled ?? false;
  }

  // ---------------------------------------------------------------- compose

  private enhance(body: HTMLElement): void {
    const root = findComposeRoot(body);
    if (!root) return; // not a compose we understand: leave it alone
    body.dataset[ENHANCED] = "1";
    const cfg = this.deps.getConfig();
    const existing = this.pixelIn(body); // a draft or undone send that we already tracked
    const control = this.doc.createElement("div");
    const state: ComposeState = {
      root,
      enabled: cfg.trackByDefault || !!existing,
      campaign: cfg.campaign || "",
      control,
      eye: this.doc.createElement("div"),
      picker: this.doc.createElement("div"),
      pace: this.doc.createElement("span"),
      pixelId: existing,
      touched: false,
    };
    this.buildControl(state);
    this.insertControl(state);
    this.composes.set(body, state);
    this.render(state);
  }

  private insertControl(state: ComposeState): void {
    const send = state.root.querySelector<HTMLElement>(SEL.sendButton);
    const cell = send?.closest("td");
    if (cell?.parentElement) {
      const td = this.doc.createElement("td");
      td.append(state.control);
      cell.after(td);
    } else if (send?.parentElement) {
      send.parentElement.after(state.control);
    }
  }

  private buildControl(state: ComposeState): void {
    const { control, eye, picker, pace } = state;
    control.className = CONTROL_CLASS;
    pace.className = "lr-pace";
    pace.hidden = true;
    eye.className = "lr-eye";
    eye.setAttribute("role", "button");
    eye.setAttribute("tabindex", "0");
    picker.className = "lr-campaign";
    picker.setAttribute("role", "button");
    picker.setAttribute("tabindex", "0");
    picker.setAttribute("aria-haspopup", "dialog");
    control.append(eye, picker, pace);

    const act = (el: HTMLElement, fn: () => void) => {
      const run = (e: Event) => {
        e.preventDefault();
        e.stopPropagation();
        this.safe(fn);
      };
      el.addEventListener("click", run);
      el.addEventListener("mousedown", (e) => e.stopPropagation());
      el.addEventListener("keydown", (e) => {
        const k = (e as KeyboardEvent).key;
        if (k === "Enter" || k === " ") run(e);
      });
    };
    act(eye, () => {
      state.enabled = !state.enabled;
      state.touched = true;
      this.render(state);
    });
    act(picker, () => {
      if (this.popover?.state === state) this.closePicker();
      else this.openPicker(state);
    });
  }

  private render(state: ComposeState): void {
    const cfg = this.deps.getConfig();
    const { eye, picker, control } = state;
    eye.innerHTML = state.enabled ? EYE_ON : EYE_OFF;
    control.classList.toggle("is-on", state.enabled);
    control.classList.toggle("is-unconfigured", !cfg.configured);
    eye.setAttribute("aria-pressed", String(state.enabled));
    const label = !cfg.configured
      ? "Likelyread is not set up: open the extension options"
      : state.enabled
        ? "Likelyread: tracking on. Click to turn off."
        : "Likelyread: tracking off. Click to turn on.";
    eye.title = label;
    eye.setAttribute("aria-label", label);
    picker.textContent = state.campaign || "No campaign";
    picker.classList.toggle("is-empty", !state.campaign);
    picker.title = state.campaign ? `Campaign: ${state.campaign}. Click to change.` : "Pick a campaign for this email";
    picker.hidden = !state.enabled || !cfg.configured;
    this.updatePace(state);
  }

  /** "20/20 today" next to the eye once the From address reaches the daily guide
   * from Sender health. Advice only: sending is never blocked. Touches the DOM
   * only when the text changes, so it is safe to call on every scan. */
  private updatePace(state: ComposeState): void {
    const cfg = this.deps.getConfig();
    const sender = this.senderOf(state.root);
    const pace = sender ? cfg.senderLimits?.[sender] : undefined;
    const show = !!(state.enabled && cfg.configured && pace && pace.limit !== null && pace.sent >= pace.limit);
    const text = show && pace ? `${pace.sent}/${pace.limit} today` : "";
    if (state.pace.textContent !== text) state.pace.textContent = text;
    if (state.pace.hidden === show) state.pace.hidden = !show;
    if (show && pace && sender) {
      const title =
        `Likelyread: ${pace.sent} tracked emails from ${sender} in the last 24 hours. ` +
        `The guide for this address is ${pace.limit} a day; going faster can push it into spam. You can still send.`;
      if (state.pace.title !== title) state.pace.title = title;
    }
  }

  // ---------------------------------------------------------------- campaign picker

  private openPicker(state: ComposeState): void {
    this.closePicker();
    const cfg = this.deps.getConfig();
    const pop = this.doc.createElement("div");
    pop.className = "lr-pop";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "Campaign for this email");

    const title = this.doc.createElement("p");
    title.className = "lr-pop-title";
    title.textContent = "Campaign for this email";
    pop.append(title);

    const names = [...new Set([state.campaign, ...cfg.campaigns].filter(Boolean))];
    const list = this.doc.createElement("div");
    list.className = "lr-pop-list";
    const option = (name: string, label: string) => {
      const b = this.doc.createElement("button");
      b.type = "button";
      b.className = "lr-opt";
      b.textContent = label;
      b.setAttribute("aria-checked", String(state.campaign === name));
      b.setAttribute("role", "menuitemradio");
      b.addEventListener("click", (e) => {
        e.preventDefault();
        this.choose(state, name);
      });
      list.append(b);
    };
    option("", "No campaign");
    for (const n of names) option(n, n);
    pop.append(list);

    const input = this.doc.createElement("input");
    input.className = "lr-new";
    input.type = "text";
    input.maxLength = 100;
    input.placeholder = "New campaign, then Enter";
    input.setAttribute("aria-label", "New campaign name");
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        const v = input.value.trim().replace(/\s+/g, " ");
        if (v) this.choose(state, v);
      }
    });
    pop.append(input);

    // Keep Gmail from reacting to typing and clicks inside the popover.
    for (const type of ["keydown", "keypress", "keyup", "mousedown", "click"]) {
      pop.addEventListener(type, (e) => {
        if (type === "keydown" && (e as KeyboardEvent).key === "Escape") {
          this.closePicker();
          state.picker.focus();
        }
        e.stopPropagation();
      });
    }

    this.doc.body.append(pop);
    const r = state.picker.getBoundingClientRect();
    const win = this.doc.defaultView!;
    const h = pop.offsetHeight || 260;
    const top = r.top - h - 8 > 8 ? r.top - h - 8 : r.bottom + 8;
    pop.style.left = `${Math.max(8, Math.min(r.left, win.innerWidth - 280))}px`;
    pop.style.top = `${Math.max(8, top)}px`;

    const outside = (e: Event) => {
      const t = e.target as Node;
      if (!pop.contains(t) && !state.picker.contains(t)) this.closePicker();
    };
    this.doc.addEventListener("mousedown", outside, true);
    this.popover = { el: pop, state, cleanup: () => this.doc.removeEventListener("mousedown", outside, true) };
    state.picker.setAttribute("aria-expanded", "true");
    (list.querySelector('[aria-checked="true"]') as HTMLElement | null)?.focus();
  }

  private choose(state: ComposeState, name: string): void {
    state.campaign = name;
    state.touched = true;
    this.deps.setCampaign(name);
    this.closePicker();
    this.render(state);
  }

  closePicker(): void {
    if (!this.popover) return;
    this.popover.cleanup();
    this.popover.el.remove();
    this.popover.state.picker.setAttribute("aria-expanded", "false");
    this.popover = null;
  }

  // ---------------------------------------------------------------- send

  private stateForTarget(target: EventTarget | null): [HTMLElement, ComposeState] | null {
    if (!(target instanceof this.doc.defaultView!.Node)) return null;
    for (const [body, st] of this.composes) {
      if (st.root.contains(target as Node) && body.isConnected) return [body, st];
    }
    return null;
  }

  private onPointer(e: MouseEvent): void {
    if (e.type === "mousedown" && e.button !== 0) return;
    const el = e.target as Element | null;
    const send = el?.closest?.(SEL.sendButton);
    if (!send) return;
    const found = this.stateForTarget(send);
    if (found) this.onSend(found[0], found[1]);
  }

  private onKey(e: KeyboardEvent): void {
    const el = e.target as Element | null;
    if (el?.closest?.(".lr-pop")) return;
    const isShortcut = e.key === "Enter" && (e.ctrlKey || e.metaKey);
    const isButton = (e.key === "Enter" || e.key === " ") && !!el?.closest?.(SEL.sendButton);
    if (!isShortcut && !isButton) return;
    const found = this.stateForTarget(el);
    if (found) this.onSend(found[0], found[1]);
  }

  /** Inject the pixel, queue registration and say so. Never blocks or cancels the send. */
  onSend(body: HTMLElement, state: ComposeState): void {
    const cfg = this.deps.getConfig();
    if (!state.enabled) return;
    if (!cfg.configured) {
      this.confirm("Not tracked: Likelyread is not set up. Click its icon in Chrome's toolbar to sign in.");
      return;
    }
    let id = state.pixelId && this.hasPixel(body, state.pixelId) ? state.pixelId : this.pixelIn(body);
    if (!id) {
      id = this.deps.newId();
      const img = this.doc.createElement("img");
      img.src = pixelUrl(cfg.serverUrl, id);
      img.width = 1;
      img.height = 1;
      img.alt = "";
      img.setAttribute("style", "display:none!important");
      body.appendChild(img);
      state.pixelId = id;
      // Let Gmail notice the edit. Optional: registration must not depend on it.
      this.safe(() => body.dispatchEvent(new this.doc.defaultView!.Event("input", { bubbles: true })));
    }
    const now = this.deps.now();
    const last = this.lastRegistered.get(id) ?? 0;
    if (now - last < 3000) return; // mousedown + click of the same send
    this.lastRegistered.set(id, now);
    this.closePicker();
    this.deps.register({
      id,
      subject: this.subjectOf(state.root),
      recipients: this.recipientsOf(state.root, body),
      campaign: state.campaign || null,
      sender: this.senderOf(state.root),
      sentAtMs: now,
    });
    this.confirm(state.campaign ? `Tracked by Likelyread · ${state.campaign}` : "Tracked by Likelyread", {
      href: `${cfg.serverUrl}/`,
      label: "Open dashboard",
    });
  }

  /**
   * The pixel this compose already carries (draft, failed or undone send).
   * Only direct pixel URLs outside quoted history count: a reply quoting an
   * earlier tracked email must get its own new id.
   */
  private pixelIn(body: HTMLElement): string | null {
    const server = this.deps.getConfig().serverUrl;
    if (!server) return null;
    for (const img of Array.from(body.querySelectorAll("img"))) {
      const src = img.getAttribute("src") || "";
      if (!src.startsWith(`${server}/p/`) || img.closest(QUOTED)) continue;
      const id = extractPixelId(src, server);
      if (id) return id;
    }
    return null;
  }

  private hasPixel(body: HTMLElement, id: string): boolean {
    return Array.from(body.querySelectorAll("img")).some((img) => (img.getAttribute("src") || "").includes(`/p/${id}.gif`));
  }

  private subjectOf(root: HTMLElement): string {
    for (const input of Array.from(root.querySelectorAll<HTMLInputElement>(SEL.subject))) {
      const v = input.value?.trim();
      if (v) return v;
    }
    return this.doc.querySelector(SEL.threadSubject)?.textContent?.trim() || "";
  }

  private recipientsOf(root: HTMLElement, body: HTMLElement): string[] {
    const values: string[] = [];
    const usable = (el: Element) => !body.contains(el) && !el.closest(SEL.notRecipients);
    for (const el of Array.from(root.querySelectorAll<HTMLElement>(SEL.recipientChips))) {
      if (usable(el)) values.push(el.getAttribute("data-hovercard-id") || "");
    }
    for (const input of Array.from(root.querySelectorAll<HTMLInputElement>(SEL.recipientInputs))) {
      if (usable(input)) values.push(input.value);
    }
    if (!cleanRecipients(values).length) {
      for (const el of Array.from(root.querySelectorAll<HTMLElement>(SEL.recipientFallback))) {
        if (usable(el)) values.push(el.getAttribute("email") || "");
      }
    }
    return cleanRecipients(values);
  }

  /** The address this email is sent from: the From field, else the signed-in account. */
  senderOf(root: HTMLElement): string | null {
    const from = root.querySelector<HTMLInputElement>(SEL.fromInput);
    return extractEmail(from?.value) || this.deps.accountEmail();
  }

  // ---------------------------------------------------------------- self-views

  /** One of our own sent emails is open in a conversation: tell the server it is us. */
  private scanForOwnPixels(): void {
    const now = this.deps.now();
    for (const { id } of ownPixels(this.doc, this.deps.getConfig(), this.deps.accountEmail())) {
      if (now - (this.lastSuppressed.get(id) ?? 0) < 20_000) continue;
      this.lastSuppressed.set(id, now);
      this.deps.suppress(id);
    }
  }

  // ---------------------------------------------------------------- send confirmation

  /** A short note above Gmail's own "Message sent", so it is plain whether the email is tracked. */
  private confirm(text: string, link?: { href: string; label: string }): void {
    const win = this.doc.defaultView!;
    if (!this.toast?.el.isConnected) {
      const el = this.doc.createElement("div");
      el.className = "lr-toast";
      el.setAttribute("role", "status");
      el.hidden = true;
      // Keep Gmail from reacting to clicks inside it.
      for (const type of ["mousedown", "click"]) el.addEventListener(type, (e) => e.stopPropagation());
      this.doc.body.append(el);
      this.toast = { el, timer: 0 };
    }
    const toast = this.toast!;
    const message = this.doc.createElement("span");
    message.textContent = text;
    const parts: Node[] = [message];
    if (link) {
      const a = this.doc.createElement("a");
      a.href = link.href;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = link.label;
      parts.push(a);
    }
    toast.el.replaceChildren(...parts);
    toast.el.hidden = false;
    win.clearTimeout(toast.timer);
    toast.timer = win.setTimeout(() => (toast.el.hidden = true), TOAST_MS);
  }

  private safe(fn: () => void): void {
    try {
      fn();
    } catch (err) {
      console.warn("[likelyread] ignored error (email sending is unaffected):", err);
    }
  }
}

/**
 * Pixels of our own sent emails shown in an opened conversation, each with the
 * message it sits in. Only messages whose sender is this account (or an address
 * we sent from) count: the same pixel shown to a recipient, even in this browser,
 * is a real open. Compose windows are left out; the grace window covers them.
 */
export interface OwnPixel {
  id: string;
  img: HTMLImageElement;
  /** The conversation message holding it, when Gmail's markup shows one. */
  message: Element | null;
}

export function ownPixels(doc: Document, cfg: ContentConfig, account: string | null): OwnPixel[] {
  const server = cfg.serverUrl;
  if (!server) return [];
  const host = new URL(server).host;
  const own = new Set([...cfg.senders, account || ""].filter(Boolean).map((s) => s.toLowerCase()));
  const inSent = /^#sent\b/.test(doc.defaultView?.location.hash || "");
  const found: OwnPixel[] = [];
  for (const img of Array.from(doc.querySelectorAll<HTMLImageElement>("img"))) {
    const src = img.getAttribute("src") || "";
    if (!src.includes(host) || img.closest('[contenteditable="true"]')) continue;
    const id = extractPixelId(src, server);
    if (!id) continue;
    const message = img.closest(SEL.messageContainer);
    const sender = message?.querySelector(SEL.messageSender)?.getAttribute("email")?.toLowerCase();
    // No sender line found: only treat it as ours inside the Sent folder.
    if (sender ? own.has(sender) : inSent) found.push({ id, img, message });
  }
  return found;
}

/** The compose window: Gmail's div.M9, else the smallest ancestor that also holds a Send button. */
export function findComposeRoot(body: HTMLElement): HTMLElement | null {
  const m9 = body.closest<HTMLElement>(SEL.composeRoot);
  if (m9?.querySelector(SEL.sendButton)) return m9;
  let el: HTMLElement | null = body.parentElement;
  for (let i = 0; el && i < MAX_COMPOSE_DEPTH; i++, el = el.parentElement) {
    if (el.querySelector(SEL.sendButton)) return el;
  }
  return null;
}
