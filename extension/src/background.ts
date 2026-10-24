/** Background service worker: registration queue, suppress pings, open status for Gmail's ticks, DNR rules. */
import { backoffMs, dnrRules, MAX_RETRY_MS, normalizeServerUrl, UUID4_RE } from "./lib";
import { CLOUD_URL } from "./cloud";
import {
  ApiError,
  apiFetch,
  getSettings,
  saveSettings,
  type ContentConfig,
  type EmailSummary,
  type ExternalConnect,
  type Message,
  type QueueItem,
  type StatusReply,
} from "./shared";

const QUEUE_KEY = "queue";
// Per account, so a send-as address used in one Gmail account never makes another
// account's view of that email count as yours (a second account is a real reader).
const SENDERS_KEY = "sendersByAccount";
const CAMPAIGN_CACHE_KEY = "campaignCache";
const CAMPAIGN_CACHE_MS = 60_000;
const PACE_CACHE_KEY = "paceCache";
const PACE_CACHE_MS = 120_000;
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

/** Most emails one Gmail conversation asks about at once. */
const MAX_STATUS_IDS = 20;

/** The open status of the emails a Gmail conversation shows, for its ticks. */
async function statuses(ids: string[]): Promise<StatusReply> {
  const settings = await getSettings();
  const reply: StatusReply = {};
  if (!settings.serverUrl || !settings.token) return reply;
  const wanted = [...new Set(ids)].filter((id) => UUID4_RE.test(id)).slice(0, MAX_STATUS_IDS);
  await Promise.all(
    wanted.map(async (id) => {
      try {
        const e = await apiFetch<EmailSummary>(settings, `/api/emails/${id}`);
        reply[id] = { status: e.status, opens: e.opens, last_open: e.last_open, self: e.self };
      } catch (err) {
        // 404: not registered yet, or not this account's. 402: past the Free plan's history.
        const status = err instanceof ApiError ? err.status : 0;
        if (status === 404 || status === 402) reply[id] = null;
      }
    }),
  );
  return reply;
}

async function sendersByAccount(): Promise<Record<string, string[]>> {
  const { [SENDERS_KEY]: map } = await chrome.storage.local.get(SENDERS_KEY);
  return map && typeof map === "object" && !Array.isArray(map) ? (map as Record<string, string[]>) : {};
}

async function rememberSender(account: string | null | undefined, sender: string | null | undefined): Promise<void> {
  if (!account || !sender || account === sender) return;
  const map = await sendersByAccount();
  const prev = Array.isArray(map[account]) ? map[account] : [];
  if (prev[0] === sender) return;
  map[account] = [sender, ...prev.filter((s) => s !== sender)].slice(0, 20);
  await chrome.storage.local.set({ [SENDERS_KEY]: map });
}

/** Campaign names for the compose picker, cached for a minute. */
async function campaignNames(): Promise<string[]> {
  const settings = await getSettings();
  if (!settings.serverUrl || !settings.token) return [];
  const { [CAMPAIGN_CACHE_KEY]: cache } = await chrome.storage.local.get(CAMPAIGN_CACHE_KEY);
  const c = cache as { at: number; names: string[] } | undefined;
  if (c && Date.now() - c.at < CAMPAIGN_CACHE_MS) return c.names;
  try {
    const names = (await fetchCampaignNames(settings)).slice(0, 30);
    await chrome.storage.local.set({ [CAMPAIGN_CACHE_KEY]: { at: Date.now(), names } });
    return names;
  } catch {
    return c?.names ?? [];
  }
}

/** Names for the compose picker. /api/campaign-names works on every plan; servers
 * before 0.4 only have /api/campaigns. */
async function fetchCampaignNames(settings: Awaited<ReturnType<typeof getSettings>>): Promise<string[]> {
  try {
    return await apiFetch<string[]>(settings, "/api/campaign-names");
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 404) throw err;
    const rows = await apiFetch<{ name: string | null }[]>(settings, "/api/campaigns");
    return rows.map((r) => r.name).filter((n): n is string => !!n);
  }
}

/** Sender health pace for the compose warning. Pro / self-host only: other plans
 * (402) and older servers (404) just get no warning. */
async function senderLimits(): Promise<ContentConfig["senderLimits"]> {
  const settings = await getSettings();
  if (!settings.serverUrl || !settings.token) return {};
  const { [PACE_CACHE_KEY]: cache } = await chrome.storage.local.get(PACE_CACHE_KEY);
  const c = cache as { at: number; limits: ContentConfig["senderLimits"] } | undefined;
  if (c && Date.now() - c.at < PACE_CACHE_MS) return c.limits;
  let limits: ContentConfig["senderLimits"] = {};
  try {
    const rows = await apiFetch<{ sender: string; ramp: { sent_24h: number; limit: number | null } }[]>(
      settings,
      "/api/senders/health?dns=false",
    );
    limits = Object.fromEntries(rows.map((r) => [r.sender.toLowerCase(), { sent: r.ramp.sent_24h, limit: r.ramp.limit }]));
  } catch {
    limits = c?.limits ?? {};
  }
  await chrome.storage.local.set({ [PACE_CACHE_KEY]: { at: Date.now(), limits } });
  return limits;
}

async function contentConfig(): Promise<ContentConfig> {
  const s = await getSettings();
  return {
    configured: !!(s.serverUrl && s.token),
    serverUrl: s.serverUrl,
    trackByDefault: s.trackByDefault,
    campaign: s.campaign,
    campaigns: await campaignNames(),
    senders: [],
    sendersByAccount: await sendersByAccount(),
    senderLimits: await senderLimits(),
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
      .then(() => rememberSender(msg.account?.toLowerCase(), email.sender))
      .then(flush)
      .then(() => chrome.storage.local.remove([CAMPAIGN_CACHE_KEY, PACE_CACHE_KEY]))
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
  if (msg.type === "status") {
    statuses(Array.isArray(msg.ids) ? msg.ids : []).then(sendResponse);
    return true;
  }
  return false;
});

// Likelyread Cloud sign-in: the page the Google sign-in lands on hands over the
// account token. Only that exact origin is accepted, and only a plausible token.
chrome.runtime.onMessageExternal.addListener((msg: ExternalConnect, sender, sendResponse) => {
  const origin = sender.origin || (sender.url ? new URL(sender.url).origin : "");
  if (origin !== CLOUD_URL || msg?.type !== "connect" || msg.serverUrl !== CLOUD_URL) return false;
  if (typeof msg.token !== "string" || !/^[A-Za-z0-9._~-]{16,200}$/.test(msg.token)) return false;
  saveSettings({ serverUrl: CLOUD_URL, token: msg.token })
    .then(() => sendResponse({ ok: true }))
    .catch(() => sendResponse({ ok: false }));
  return true;
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === FLUSH_ALARM || a.name === `${FLUSH_ALARM}-periodic`) void flush();
});

// Content scripts can read chrome.storage.local by default; the token lives there.
// Restrict it to extension pages and the service worker. Gmail tabs get only the
// token-free ContentConfig, by message.
void chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" }).catch(() => undefined);

const CONTENT_KEYS = ["serverUrl", "token", "trackByDefault", "campaign", SENDERS_KEY, CAMPAIGN_CACHE_KEY, PACE_CACHE_KEY];

async function notifyGmailTabs(): Promise<void> {
  const tabs = await chrome.tabs.query({ url: "https://mail.google.com/*" });
  for (const t of tabs) {
    if (t.id !== undefined) void chrome.tabs.sendMessage(t.id, { type: "configChanged" } satisfies Message).catch(() => undefined);
  }
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (CONTENT_KEYS.some((k) => k in changes)) void notifyGmailTabs().catch(() => undefined);
  if (changes.serverUrl) void syncBlockRules();
  if (changes.serverUrl || changes.token) {
    void chrome.storage.local.remove([CAMPAIGN_CACHE_KEY, PACE_CACHE_KEY]);
    void flush();
  }
});

function boot(): void {
  // 0.2.0 kept send-as addresses in one global list; 0.2.1+ keeps them per account.
  // The old list cannot be attributed to an account, so drop it: aliases are
  // relearned on the next send from them.
  void chrome.storage.local.remove("senders");
  void syncBlockRules();
  void flush();
  void chrome.alarms.create(`${FLUSH_ALARM}-periodic`, { periodInMinutes: 5 });
}

chrome.runtime.onInstalled.addListener((details) => {
  boot();
  if (details.reason === "install") void chrome.runtime.openOptionsPage();
});
chrome.runtime.onStartup.addListener(boot);
