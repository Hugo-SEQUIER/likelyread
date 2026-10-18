/** Content script on mail.google.com: wires ComposeTracker to the extension. */
import { ComposeTracker, extractEmail } from "./compose";
import type { ContentConfig, Message } from "./shared";

let config: ContentConfig = {
  configured: false,
  serverUrl: "",
  trackByDefault: true,
  campaign: "",
  campaigns: [],
  senders: [],
};

function send(msg: Message): Promise<unknown> {
  try {
    return chrome.runtime.sendMessage(msg).catch(() => undefined);
  } catch {
    return Promise.resolve(undefined); // extension reloaded: the old content script is orphaned
  }
}

async function refreshConfig(): Promise<void> {
  const c = (await send({ type: "getConfig" })) as ContentConfig | undefined;
  if (c) config = c;
}

/** The signed-in address: Gmail puts it in the tab title ("Inbox - you@example.com - Gmail"). */
function accountEmail(): string | null {
  const fromTitle = extractEmail(document.title);
  if (fromTitle) return fromTitle;
  const account = document.querySelector('a[aria-label*="@"][href*="accounts.google.com"], a[aria-label*="@"]');
  return extractEmail(account?.getAttribute("aria-label"));
}

/** The config with `senders` narrowed to the account signed in to this tab. */
function tabConfig(): ContentConfig {
  const account = accountEmail();
  return { ...config, senders: (account && config.sendersByAccount?.[account]) || [] };
}

const tracker = new ComposeTracker(document, {
  getConfig: tabConfig,
  register: (email) => void send({ type: "register", email, account: accountEmail() }),
  suppress: (id) => void send({ type: "suppress", id }),
  setCampaign: (campaign) => void send({ type: "setCampaign", campaign }),
  accountEmail,
  newId: () => crypto.randomUUID(),
  now: () => Date.now(),
});

let scheduled = false;
function scheduleScan(): void {
  if (scheduled) return;
  scheduled = true;
  // A timer, not requestAnimationFrame: rAF pauses in background tabs, and Gmail
  // can render a conversation while its tab is hidden.
  setTimeout(() => {
    scheduled = false;
    tracker.scan();
  }, 30);
}

// Listen and observe right away so early compose windows are not missed;
// config arrives a moment later and triggers a rescan.
tracker.install();
new MutationObserver(scheduleScan).observe(document.documentElement, {
  childList: true,
  subtree: true,
  attributes: true,
  attributeFilter: ["src"],
});

// The background tells us when settings change. This script never reads extension
// storage itself: that is where the token lives, and it is closed to content scripts.
chrome.runtime.onMessage.addListener((msg: Message, sender) => {
  if (sender.id !== chrome.runtime.id || msg.type !== "configChanged") return;
  void refreshConfig().then(() => {
    tracker.refresh();
    scheduleScan();
  });
});
void refreshConfig().then(() => {
  tracker.refresh();
  scheduleScan();
});
