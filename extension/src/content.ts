/** Content script on mail.google.com: wires ComposeTracker to the extension. */
import { ComposeTracker } from "./compose";
import type { ContentConfig, Message } from "./shared";

let config: ContentConfig = { configured: false, serverUrl: "", trackByDefault: true, campaign: "" };

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

const tracker = new ComposeTracker(document, {
  getConfig: () => config,
  register: (email) => void send({ type: "register", email }),
  suppress: (id) => void send({ type: "suppress", id }),
  newId: () => crypto.randomUUID(),
  now: () => Date.now(),
});

let scheduled = false;
function scheduleScan(): void {
  if (scheduled) return;
  scheduled = true;
  requestAnimationFrame(() => {
    scheduled = false;
    tracker.scan();
  });
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
chrome.storage.onChanged.addListener(() => void refreshConfig().then(scheduleScan));
void refreshConfig().then(scheduleScan);
