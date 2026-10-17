/** Pure helpers shared by the content script, background and tests. */

export const UUID4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const UUID_IN_TEXT = "([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})";

export const MAX_RETRY_MS = 7 * 24 * 3600 * 1000;

/** Normalise a user-entered server URL to an origin like https://t.example.com. */
export function normalizeServerUrl(raw: string): string | null {
  const trimmed = (raw || "").trim();
  if (!trimmed) return null;
  try {
    const u = new URL(/^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`);
    const local = u.hostname === "localhost" || u.hostname === "127.0.0.1";
    if (u.protocol !== "https:" && !(u.protocol === "http:" && local)) return null;
    return u.origin;
  } catch {
    return null;
  }
}

export function pixelUrl(server: string, id: string): string {
  return `${server}/p/${id}.gif`;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Find a likelyread pixel id in an image URL. Works for direct URLs and for
 * Gmail proxy URLs that keep the original after a `#`.
 */
export function extractPixelId(src: string, server: string): string | null {
  if (!src || !server) return null;
  const host = new URL(server).host;
  const re = new RegExp(`//${escapeRe(host)}/p/${UUID_IN_TEXT}\\.gif`, "i");
  const m = src.match(re);
  return m ? m[1].toLowerCase() : null;
}

/** Retry delay: 30s, 1m, 2m, 4m ... capped at 6h. */
export function backoffMs(attempts: number): number {
  return Math.min(30_000 * 2 ** Math.max(0, attempts - 1), 6 * 3600 * 1000);
}

const EMAIL_RE = /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/;

export function cleanRecipients(values: Iterable<string>): string[] {
  const out = new Set<string>();
  for (const raw of values) {
    for (const part of (raw || "").split(/[,;]/)) {
      const m = part.match(/<([^>]+)>/);
      const addr = (m ? m[1] : part).trim().toLowerCase();
      if (EMAIL_RE.test(addr)) out.add(addr);
    }
  }
  return [...out];
}

/** Gmail DNR rules for blocking self-views of the pixel. */
export function dnrRules(server: string): chrome.declarativeNetRequest.Rule[] {
  const host = new URL(server).host;
  const block = { type: "block" } as chrome.declarativeNetRequest.RuleAction;
  return [
    {
      id: 1,
      priority: 1,
      action: block,
      condition: { urlFilter: `|${server}/p/`, resourceTypes: ["image" as chrome.declarativeNetRequest.ResourceType] },
    },
    {
      // Gmail proxy URLs keep the original URL after '#'. Whether Chrome matches
      // the fragment is not guaranteed; the suppress ping covers the gap.
      id: 2,
      priority: 1,
      action: block,
      condition: {
        regexFilter: `^https://[a-z0-9-]+\\.googleusercontent\\.com/.*#https?://${escapeRe(host)}/p/`,
        resourceTypes: ["image" as chrome.declarativeNetRequest.ResourceType],
      },
    },
  ];
}

export function relTime(tsSeconds: number | null | undefined, now = Date.now()): string {
  if (!tsSeconds) return "never";
  const d = now / 1000 - tsSeconds;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  if (d < 30 * 86400) return `${Math.floor(d / 86400)} d ago`;
  return new Date(tsSeconds * 1000).toLocaleDateString();
}
