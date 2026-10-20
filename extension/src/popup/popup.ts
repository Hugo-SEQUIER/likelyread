import { relTime } from "../lib";
import { apiFetch, getSettings, saveSettings, type EmailSummary, type Hit, type Settings } from "../shared";

type HitWithReason = Hit & { reason?: string };

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string | null, ...children: (Node | string | null)[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  for (const c of children) if (c !== null) el.append(c);
  return el;
}

function chip(e: EmailSummary): HTMLElement {
  if (e.status === "opened") return h("span", "chip open", `Opened ×${e.opens}`, h("small", null, ` ${relTime(e.last_open)}`));
  if (e.status === "prefetch_only") return h("span", "chip dim", "Prefetch only");
  return h("span", "chip", "Not opened");
}

const KIND: Record<Hit["kind"], string> = { open: "Open", self: "Self view", prefetch: "Prefetch" };

async function main(): Promise<void> {
  const settings = await getSettings();
  const list = document.getElementById("list")!;
  const dash = document.getElementById("dashboard") as HTMLAnchorElement;
  const campaignInput = document.getElementById("campaign") as HTMLInputElement;
  campaignInput.value = settings.campaign;
  campaignInput.addEventListener("change", () => void saveSettings({ campaign: campaignInput.value.trim().replace(/\s+/g, " ") }));

  if (!settings.serverUrl || !settings.token) {
    dash.hidden = true;
    const link = h("a", null, "Open the options");
    link.href = "#";
    link.addEventListener("click", () => chrome.runtime.openOptionsPage());
    list.replaceChildren(h("p", "note", "Likelyread is not set up yet. ", link, " and paste your setup link."));
    return;
  }
  dash.href = `${settings.serverUrl}/`;

  try {
    const [emails, campaigns] = await Promise.all([
      apiFetch<EmailSummary[]>(settings, "/api/emails?limit=50"),
      apiFetch<{ name: string | null }[]>(settings, "/api/campaigns").catch(() => []),
    ]);
    document.getElementById("campaign-list")!.replaceChildren(
      ...campaigns.filter((c) => c.name).map((c) => {
        const o = document.createElement("option");
        o.value = c.name!;
        return o;
      }),
    );
    if (!emails.length) {
      list.replaceChildren(h("p", "note", "No tracked emails yet. Send one from Gmail with the red eye on."));
      return;
    }
    list.replaceChildren(...emails.map((e) => row(settings, e)));
  } catch (err) {
    list.replaceChildren(h("p", "error", `Could not load: ${(err as Error).message}`));
  }
}

function row(settings: Settings, e: EmailSummary): HTMLElement {
  const details = h("div", "hits");
  details.hidden = true;
  const to = e.recipients.length ? `to ${e.recipients[0]}${e.recipients.length > 1 ? ` +${e.recipients.length - 1}` : ""}` : "";
  const meta = [to, e.sender ? `from ${e.sender}` : "", relTime(e.sent_at), e.campaign || ""].filter(Boolean).join(" · ");
  const item = h(
    "div",
    `item${e.status === "prefetch_only" ? " greyed" : ""}`,
    h("div", "top", h("div", "subject", e.subject || "(no subject)"), chip(e)),
    h("div", "meta", meta),
    details,
  );
  item.addEventListener("click", async () => {
    details.hidden = !details.hidden;
    if (details.hidden || details.dataset.loaded) return;
    details.textContent = "Loading…";
    try {
      const full = await apiFetch<EmailSummary & { hits: HitWithReason[] }>(settings, `/api/emails/${e.id}`);
      details.dataset.loaded = "1";
      if (!full.hits.length) {
        details.replaceChildren(h("p", "note", "No hits yet. Nothing has loaded the image."));
        return;
      }
      details.replaceChildren(
        ...[...full.hits].reverse().map((x) =>
          h(
            "div",
            `hit ${x.kind}`,
            h("span", "kind", KIND[x.kind]),
            h("span", null, `${new Date(x.ts * 1000).toLocaleString()} · ${x.via === "gmail_proxy" ? "Gmail proxy" : "Direct"}`),
            h("span", "why", x.reason || x.ua),
          ),
        ),
      );
    } catch (err) {
      details.textContent = `Could not load: ${(err as Error).message}`;
    }
  });
  return item;
}

void main();
