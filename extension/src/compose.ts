/**
 * Gmail compose integration. Kept free of chrome.* calls so it can be tested
 * against a fake Gmail DOM; content.ts wires it to the extension.
 *
 * Rule: nothing here may break sending. Every handler is wrapped in try/catch
 * and never calls preventDefault or stopPropagation.
 */
import { MAX_COMPOSE_DEPTH, SEL } from "./gmail-selectors";
import { cleanRecipients, extractPixelId, pixelUrl } from "./lib";
import type { ContentConfig, PendingEmail } from "./shared";

export interface ComposeDeps {
  getConfig(): ContentConfig;
  register(email: PendingEmail): void;
  suppress(id: string): void;
  newId(): string;
  now(): number;
}

const ENHANCED = "likelyreadEnhanced";
const TOGGLE_CLASS = "likelyread-toggle";

interface ComposeState {
  root: HTMLElement;
  enabled: boolean;
  toggle: HTMLElement;
  pixelId: string | null;
}

/** Quoted history in a reply. Pixels of earlier emails live here and must never be reused. */
const QUOTED = ".gmail_quote, .gmail_extra, blockquote";

const EYE_ON =
  '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M12 5c-5 0-9 4.5-10 7 1 2.5 5 7 10 7s9-4.5 10-7c-1-2.5-5-7-10-7zm0 11a4 4 0 110-8 4 4 0 010 8zm0-2a2 2 0 100-4 2 2 0 000 4z"/></svg>';
const EYE_OFF =
  '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M3.3 2 2 3.3l3.2 3.2C3.5 7.8 2.4 9.6 2 12c1 2.5 5 7 10 7 1.8 0 3.4-.5 4.8-1.3l3.9 3.9 1.3-1.3L3.3 2zM12 16a4 4 0 01-3.9-4.9l1.6 1.6A2 2 0 0011.3 14l1.6 1.6c-.3.2-.6.4-.9.4zm0-11c5 0 9 4.5 10 7-.5 1.2-1.6 2.9-3.2 4.3l-2.9-2.9A4 4 0 0010.6 8.2L8.4 6C9.5 5.4 10.7 5 12 5z"/></svg>';

export class ComposeTracker {
  private composes = new Map<HTMLElement, ComposeState>(); // body -> state
  private lastRegistered = new Map<string, number>();
  private lastSuppressed = new Map<string, number>();

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
        if (!body.isConnected) this.composes.delete(body);
        else if (!st.toggle.isConnected) this.insertToggle(st); // Gmail re-rendered the toolbar
      }
    });
    this.safe(() => this.scanForOwnPixels());
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
    const state: ComposeState = {
      root,
      enabled: cfg.trackByDefault || !!existing,
      toggle: this.doc.createElement("div"),
      pixelId: existing,
    };
    state.toggle = this.makeToggle(state);
    this.insertToggle(state);
    this.composes.set(body, state);
    this.renderToggle(state);
  }

  private insertToggle(state: ComposeState): void {
    const send = state.root.querySelector<HTMLElement>(SEL.sendButton);
    const cell = send?.closest("td");
    if (cell?.parentElement) {
      const td = this.doc.createElement("td");
      td.append(state.toggle);
      cell.after(td);
    } else if (send?.parentElement) {
      send.parentElement.after(state.toggle);
    }
  }

  private makeToggle(state: ComposeState): HTMLElement {
    const btn = this.doc.createElement("div");
    btn.className = TOGGLE_CLASS;
    btn.setAttribute("role", "button");
    btn.setAttribute("tabindex", "0");
    btn.style.cssText =
      "display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;margin-left:6px;border-radius:50%;cursor:pointer;user-select:none;";
    const flip = (e: Event) => {
      e.preventDefault();
      e.stopPropagation();
      state.enabled = !state.enabled;
      this.renderToggle(state);
    };
    btn.addEventListener("click", flip);
    btn.addEventListener("keydown", (e) => {
      if ((e as KeyboardEvent).key === "Enter" || (e as KeyboardEvent).key === " ") flip(e);
    });
    return btn;
  }

  private renderToggle(state: ComposeState): void {
    const cfg = this.deps.getConfig();
    const t = state.toggle;
    t.innerHTML = state.enabled ? EYE_ON : EYE_OFF;
    t.style.color = state.enabled ? "#1a73e8" : "#80868b";
    t.setAttribute("aria-pressed", String(state.enabled));
    const campaign = cfg.campaign ? ` (campaign: ${cfg.campaign})` : "";
    const label = !cfg.configured
      ? "likelyread is not configured: open the extension options"
      : state.enabled
        ? `likelyread: tracking ON${campaign}. Click to turn off.`
        : "likelyread: tracking OFF. Click to turn on.";
    t.title = label;
    t.setAttribute("aria-label", label);
  }

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
    const isShortcut = e.key === "Enter" && (e.ctrlKey || e.metaKey);
    const isButton = (e.key === "Enter" || e.key === " ") && !!el?.closest?.(SEL.sendButton);
    if (!isShortcut && !isButton) return;
    const found = this.stateForTarget(el);
    if (found) this.onSend(found[0], found[1]);
  }

  /** Inject the pixel and queue registration. Never blocks or cancels the send. */
  onSend(body: HTMLElement, state: ComposeState): void {
    const cfg = this.deps.getConfig();
    if (!state.enabled || !cfg.configured) return;
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
    this.deps.register({
      id,
      subject: this.subjectOf(state.root),
      recipients: this.recipientsOf(state.root, body),
      campaign: cfg.campaign || null,
      sentAtMs: now,
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
    const input = root.querySelector<HTMLInputElement>(SEL.subject);
    const v = input?.value?.trim();
    if (v) return v;
    return this.doc.querySelector(SEL.threadSubject)?.textContent?.trim() || "";
  }

  private recipientsOf(root: HTMLElement, body: HTMLElement): string[] {
    const values: string[] = [];
    for (const input of Array.from(root.querySelectorAll<HTMLInputElement>(SEL.recipientInputs))) {
      values.push(input.value);
    }
    for (const el of Array.from(root.querySelectorAll<HTMLElement>(SEL.recipientChips))) {
      if (body.contains(el) || el.closest(SEL.fromRow)) continue;
      values.push(el.getAttribute("email") || el.getAttribute("data-hovercard-id") || "");
    }
    return cleanRecipients(values);
  }

  // ---------------------------------------------------------------- self-views

  /** Our own sent emails rendered in a thread: tell the server it is us. */
  private scanForOwnPixels(): void {
    const server = this.deps.getConfig().serverUrl;
    if (!server) return;
    const host = new URL(server).host;
    const now = this.deps.now();
    for (const img of Array.from(this.doc.querySelectorAll<HTMLImageElement>("img"))) {
      const src = img.getAttribute("src") || "";
      if (!src.includes(host)) continue;
      if (img.closest('[contenteditable="true"]')) continue; // compose, handled by the grace window
      const id = extractPixelId(src, server);
      if (!id) continue;
      if (now - (this.lastSuppressed.get(id) ?? 0) < 20_000) continue;
      this.lastSuppressed.set(id, now);
      this.deps.suppress(id);
    }
  }

  private safe(fn: () => void): void {
    try {
      fn();
    } catch (err) {
      console.warn("[likelyread] ignored error (email sending is unaffected):", err);
    }
  }
}

/** Climb from the body to the smallest ancestor that also holds a Send button. */
export function findComposeRoot(body: HTMLElement): HTMLElement | null {
  let el: HTMLElement | null = body.parentElement;
  for (let i = 0; el && i < MAX_COMPOSE_DEPTH; i++, el = el.parentElement) {
    if (el.querySelector(SEL.sendButton)) return el;
  }
  return null;
}
