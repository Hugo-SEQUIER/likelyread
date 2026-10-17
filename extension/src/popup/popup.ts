import { relTime } from "../lib";
import { apiFetch, getSettings, saveSettings, type EmailSummary, type Hit } from "../shared";

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string | null, ...children: (Node | string | null)[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  for (const c of children) if (c !== null) el.append(c);
  return el;
}

function chip(e: EmailSummary): HTMLElement {
  if (e.status === "opened") return h("span", "chip ok", `Opened x${e.opens}`, h("small", null, ` ${relTime(e.last_open)}`));
  if (e.status === "prefetch_only") return h("span", "chip dim", "Prefetch only (Apple)");
  return h("span", "chip", "Not opened");
}

const KIND: Record<Hit["kind"], string> = { open: "Open", self: "Self", prefetch: "Prefetch" };

async function main(): Promise<void> {
  const settings = await getSettings();
  const list = document.getElementById("list")!;
  const dash = document.getElementById("dashboard") as HTMLAnchorElement;
  const campaignInput = document.getElementById("campaign") as HTMLInputElement;
  campaignInput.value = settings.campaign;
  campaignInput.addEventListener("change", () => void saveSettings({ campaign: campaignInput.value.trim() }));

  if (!settings.serverUrl || !settings.token) {
    dash.hidden = true;
    const link = h("a", null, "Open options");
    link.href = "#";
    link.addEventListener("click", () => chrome.runtime.openOptionsPage());
    list.replaceChildren(h("p", "muted", "mailpeek is not set up yet. ", link));
    return;
  }
  dash.href = `${settings.serverUrl}/`;

  try {
    const [emails, campaigns] = await Promise.all([
      apiFetch<EmailSummary[]>(settings, "/api/emails?limit=50"),
      apiFetch<{ name: string | null }[]>(settings, "/api/campaigns").catch(() => []),
    ]);
    const dl = document.getElementById("campaign-list")!;
    dl.replaceChildren(
      ...campaigns.filter((c) => c.name).map((c) => {
        const o = document.createElement("option");
        o.value = c.name!;
        return o;
      }),
    );
    if (!emails.length) {
      list.replaceChildren(h("p", "muted", "No tracked emails yet. Send one from Gmail with the eye icon on."));
      return;
    }
    list.replaceChildren(...emails.map((e) => row(settings, e)));
  } catch (err) {
    list.replaceChildren(h("p", "error", `Could not load: ${(err as Error).message}`));
  }
}

function row(settings: Awaited<ReturnType<typeof getSettings>>, e: EmailSummary): HTMLElement {
  const details = h("div", "hits");
  details.hidden = true;
  const item = h(
    "div",
    `item ${e.status === "prefetch_only" ? "greyed" : ""}`,
    h(
      "div",
      "top",
      h("div", "subject", e.subject || "(no subject)"),
      chip(e),
    ),
    h("div", "meta", `${e.recipients.join(", ") || "no recipients"} · ${relTime(e.sent_at)}${e.campaign ? ` · ${e.campaign}` : ""}`),
    details,
  );
  item.addEventListener("click", async () => {
    details.hidden = !details.hidden;
    if (details.hidden || details.dataset.loaded) return;
    details.textContent = "Loading...";
    try {
      const full = await apiFetch<EmailSummary & { hits: Hit[] }>(settings, `/api/emails/${e.id}`);
      details.dataset.loaded = "1";
      if (!full.hits.length) {
        details.textContent = "No hits yet.";
        return;
      }
      details.replaceChildren(
        ...[...full.hits].reverse().map((x) =>
          h(
            "div",
            `hit ${x.kind}`,
            h("span", "kind", KIND[x.kind]),
            h("span", null, new Date(x.ts * 1000).toLocaleString()),
            h("span", null, x.via === "gmail_proxy" ? "Gmail proxy" : "Direct"),
            h("span", "ua", x.ua),
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
