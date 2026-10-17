export interface Settings {
  serverUrl: string;
  token: string;
  trackByDefault: boolean;
  campaign: string;
}

export interface PendingEmail {
  id: string;
  subject: string;
  recipients: string[];
  campaign: string | null;
  sender: string | null;
  sentAtMs: number;
}

export interface QueueItem {
  email: PendingEmail;
  attempts: number;
  nextAt: number;
}

/** Config shared with the content script. The token never goes to Gmail's tab. */
export interface ContentConfig {
  configured: boolean;
  serverUrl: string;
  trackByDefault: boolean;
  /** The current campaign: default for new compose windows. */
  campaign: string;
  /** Existing campaign names, most recent first, for the compose picker. */
  campaigns: string[];
  /** Addresses this browser has sent tracked email from. */
  senders: string[];
}

export type Message =
  | { type: "register"; email: PendingEmail }
  | { type: "suppress"; id: string }
  | { type: "setCampaign"; campaign: string }
  | { type: "getConfig" };

export const DEFAULT_SETTINGS: Settings = { serverUrl: "", token: "", trackByDefault: true, campaign: "" };

export async function getSettings(): Promise<Settings> {
  const s = await chrome.storage.local.get(DEFAULT_SETTINGS as unknown as Record<string, unknown>);
  return s as unknown as Settings;
}

export async function saveSettings(patch: Partial<Settings>): Promise<void> {
  await chrome.storage.local.set(patch);
}

export interface EmailSummary {
  id: string;
  subject: string;
  recipients: string[];
  sent_at: number;
  campaign: string | null;
  sender: string | null;
  opens: number;
  first_open: number | null;
  last_open: number | null;
  prefetch: number;
  self: number;
  status: "opened" | "not_opened" | "prefetch_only";
}

export interface Hit {
  ts: number;
  ip: string | null;
  ua: string;
  kind: "open" | "self" | "prefetch";
  via: "gmail_proxy" | "direct";
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function apiFetch<T>(settings: Settings, path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${settings.serverUrl}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${settings.token}`,
      ...(init.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (!res.ok) throw new ApiError(res.status, res.status === 401 ? "Invalid token" : `Server error ${res.status}`);
  return (await res.json()) as T;
}
