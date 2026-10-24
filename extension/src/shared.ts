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
  /** Addresses the current Gmail account has sent tracked email from (send-as aliases). */
  senders: string[];
  /** Per signed-in account, the addresses it sent from. Background fills this; the
   * content script picks its own account's list into `senders`. */
  sendersByAccount?: Record<string, string[]>;
  /** Sender health pace per lower-cased address: tracked emails in the last 24 h
   * and the suggested daily limit (null when the address is marked established). */
  senderLimits?: Record<string, { sent: number; limit: number | null }>;
}

/** What Gmail's ticks need to know about one tracked email. */
export type EmailStatus = Pick<EmailSummary, "status" | "opens" | "last_open" | "self">;

/** Answer to a "status" message: null for an id the server does not know (not
 * registered yet, or past the plan's history). Ids it could not answer are left out. */
export type StatusReply = Record<string, EmailStatus | null>;

export type Message =
  | { type: "register"; email: PendingEmail; account?: string | null }
  | { type: "suppress"; id: string }
  | { type: "status"; ids: string[] }
  | { type: "setCampaign"; campaign: string }
  | { type: "getConfig" }
  | { type: "configChanged" };

/** Sent by the Likelyread Cloud sign-in page (externally_connectable). */
export interface ExternalConnect {
  type: "connect";
  serverUrl: string;
  token: string;
}

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
