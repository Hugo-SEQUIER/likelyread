/** Background service worker: registration queue, suppress pings, DNR rules. */
import { backoffMs, dnrRules, MAX_RETRY_MS, normalizeServerUrl, UUID4_RE } from "./lib";
import { ApiError, apiFetch, getSettings, saveSettings, type ContentConfig, type Message, type QueueItem } from "./shared";

const QUEUE_KEY = "queue";
const SENDERS_KEY = "senders";
const CAMPAIGN_CACHE_KEY = "campaignCache";
const CAMPAIGN_CACHE_MS = 60_000;
const FLUSH_ALARM = "likelyread-flush";

async function getQueue(): Promise<QueueItem[]> {
  const { [QUEUE_KEY]: q } = await chrome.storage.local.get(QUEUE_KEY);
  return Array.isArray(q) ? (q as QueueItem[]) : [];
}

async function setQueue(q: QueueItem[]): Promise<void> {
  await chrome.storage.local.set({ [QUEUE_KEY]: q });
}

// Serialise queue mutations: the worker handles messages concurrently.
let chain: Promise<unknown> = Promise.resolve();
function locked<T>(fn: () => Promise<T>): Promise<T> {
  const run = chain.then(fn, fn);
  chain = run.catch(() => undefined);
  return run;
}

function enqueue(item: QueueItem): Promise<void> {
  return locked(async () => {
    const q = (await getQueue()).filter((x) => x.email.id !== item.email.id);
    q.push(item);
    await setQueue(q);
  });
}

function flush(): Promise<void> {
  return locked(async () => {
    const settings = await getSettings();
    const q = await getQueue();
    if (!q.length) return;
    const now = Date.now();
    const keep: QueueItem[] = [];
    for (const item of q) {
      if (now - item.email.sentAtMs > MAX_RETRY_MS) continue; // give up after 7 days
      if (item.nextAt > now || !settings.serverUrl || !settings.token) {
        keep.push(item);
        continue;
      }
      const { email } = item;
      try {
        await apiFetch(settings, "/api/emails", {
          method: "POST",
          body: JSON.stringify({
            id: email.id,
            subject: email.subject,
            recipients: email.recipients,
            campaign: email.campaign,
            sender: email.sender ?? null,
            age: Math.max(0, (now - email.sentAtMs) / 1000),
          }),
        });
      } catch (err) {
        // 4xx other than auth/rate errors means the payload itself is bad: drop it.
        const status = err instanceof ApiError ? err.status : 0;
        if (status >= 400 && status < 500 && status !== 401 && status !== 408 && status !== 429) continue;
        const attempts = item.attempts + 1;
        keep.push({ ...item, attempts, nextAt: now + backoffMs(attempts) });
      }
    }
    await setQueue(keep);
    if (keep.length) {
      const when = Math.max(Math.min(...keep.map((k) => k.nextAt)), Date.now() + 30_000);
      await chrome.alarms.create(FLUSH_ALARM, { when });
    }
  });
}

async function suppress(id: string): Promise<void> {
  if (!UUID4_RE.test(id)) return;
  const settings = await getSettings();
  if (!settings.serverUrl || !settings.token) return;
  try {
    await apiFetch(settings, `/api/emails/${id}/suppress`, { method: "POST" });
  } catch {
    // Best effort: the server's grace window still covers the compose preview.
  }
}

async function rememberSender(sender: string | null | undefined): Promise<void> {
  if (!sender) return;
  const { [SENDERS_KEY]: list } = await chrome.storage.local.get(SENDERS_KEY);
  const prev = Array.isArray(list) ? (list as string[]) : [];
  if (prev[0] === sender) return;
  await chrome.storage.local.set({ [SENDERS_KEY]: [sender, ...prev.filter((s) => s !== sender)].slice(0, 20) });
}

/** Campaign names for the compose picker, cached for a minute. */
async function campaignNames(): Promise<string[]> {
  const settings = await getSettings();
  if (!settings.serverUrl || !settings.token) return [];
  const { [CAMPAIGN_CACHE_KEY]: cache } = await chrome.storage.local.get(CAMPAIGN_CACHE_KEY);
  const c = cache as { at: number; names: string[] } | undefined;
  if (c && Date.now() - c.at < CAMPAIGN_CACHE_MS) return c.names;
  try {
    const rows = await apiFetch<{ name: string | null }[]>(settings, "/api/campaigns");
    const names = rows.map((r) => r.name).filter((n): n is string => !!n).slice(0, 30);
    await chrome.storage.local.set({ [CAMPAIGN_CACHE_KEY]: { at: Date.now(), names } });
    return names;
  } catch {
    return c?.names ?? [];
  }
}

async function contentConfig(): Promise<ContentConfig> {
  const s = await getSettings();
  const { [SENDERS_KEY]: senders } = await chrome.storage.local.get(SENDERS_KEY);
  return {
    configured: !!(s.serverUrl && s.token),
    serverUrl: s.serverUrl,
    trackByDefault: s.trackByDefault,
    campaign: s.campaign,
    campaigns: await campaignNames(),
    senders: Array.isArray(senders) ? (senders as string[]) : [],
  };
}

async function syncBlockRules(): Promise<void> {
  const { serverUrl } = await getSettings();
  const existing = await chrome.declarativeNetRequest.getDynamicRules();
  const server = normalizeServerUrl(serverUrl);
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: existing.map((r) => r.id),
    addRules: server ? dnrRules(server) : [],
  });
}

chrome.runtime.onMessage.addListener((msg: Message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;
  if (msg.type === "getConfig") {
    contentConfig().then(sendResponse);
    return true;
  }
  if (msg.type === "register") {
    const email = msg.email;
    if (!UUID4_RE.test(email.id)) return false;
    enqueue({ email, attempts: 0, nextAt: 0 })
      .then(() => rememberSender(email.sender))
      .then(flush)
      .then(() => chrome.storage.local.remove(CAMPAIGN_CACHE_KEY))
      .then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "setCampaign") {
    saveSettings({ campaign: (msg.campaign || "").slice(0, 100) }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.type === "suppress") {
    suppress(msg.id).then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === FLUSH_ALARM || a.name === `${FLUSH_ALARM}-periodic`) void flush();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.serverUrl) void syncBlockRules();
  if (changes.serverUrl || changes.token) {
    void chrome.storage.local.remove(CAMPAIGN_CACHE_KEY);
    void flush();
  }
});

function boot(): void {
  void syncBlockRules();
  void flush();
  void chrome.alarms.create(`${FLUSH_ALARM}-periodic`, { periodInMinutes: 5 });
}

chrome.runtime.onInstalled.addListener((details) => {
  boot();
  if (details.reason === "install") void chrome.runtime.openOptionsPage();
});
chrome.runtime.onStartup.addListener(boot);
