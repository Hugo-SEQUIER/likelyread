import { CLOUD_URL, cloudSignInUrl } from "../cloud";
import { normalizeServerUrl, parseSetupLink } from "../lib";
import { ApiError, apiFetch, getSettings, saveSettings, type Settings } from "../shared";

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

function status(text: string, ok: boolean): void {
  const el = $("status");
  el.textContent = text;
  el.className = ok ? "ok" : "error";
}

function readForm(): Settings | null {
  const serverUrl = normalizeServerUrl($<HTMLInputElement>("serverUrl").value);
  if (!serverUrl) {
    status("Enter a valid https:// server URL.", false);
    return null;
  }
  return {
    serverUrl,
    token: $<HTMLInputElement>("token").value.trim(),
    trackByDefault: $<HTMLInputElement>("trackByDefault").checked,
    campaign: $<HTMLInputElement>("campaign").value.trim(),
  };
}

/** Ask for access to the user's server. Must run inside a click/submit handler. */
async function ensurePermission(serverUrl: string): Promise<boolean> {
  // Call request() first, with no await before it, so the click gesture is still
  // active. It resolves true right away when the permission is already granted.
  return chrome.permissions.request({ origins: [`${serverUrl}/*`] });
}

async function test(s: Settings): Promise<boolean> {
  try {
    await apiFetch(s, "/api/ping");
    status("Connected. The token is valid.", true);
    return true;
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) status("The server rejected this token (401).", false);
    else status(`Could not reach the server: ${(err as Error).message}`, false);
    return false;
  }
}

async function showCurrent(): Promise<void> {
  const s = await getSettings();
  const el = $("current");
  if (!s.serverUrl || !s.token) {
    el.textContent = "Not connected yet.";
    el.className = "hint";
    return;
  }
  if (s.serverUrl !== CLOUD_URL) {
    el.textContent = `Connected to your server, ${new URL(s.serverUrl).host}.`;
    el.className = "hint ok";
    return;
  }
  try {
    const me = await apiFetch<{ email: string; plan: string }>(s, "/api/me");
    el.textContent = `Signed in as ${me.email}, ${me.plan === "pro" ? "Pro" : "Free"} plan.`;
    el.className = "hint ok";
    $<HTMLButtonElement>("cloudSignIn").textContent = "Switch Google account";
  } catch (err) {
    el.textContent = err instanceof ApiError && err.status === 401 ? "Signed out. Sign in again." : "Connected to Likelyread Cloud.";
    el.className = err instanceof ApiError && err.status === 401 ? "hint error" : "hint ok";
  }
}

async function main(): Promise<void> {
  const s = await getSettings();
  $("cloudSignIn").addEventListener("click", () => {
    void chrome.tabs.create({ url: cloudSignInUrl(chrome.runtime.id) });
  });
  // The sign-in tab hands the token to the background worker; reflect it here.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && (changes.token || changes.serverUrl)) {
      void getSettings().then((n) => {
        $<HTMLInputElement>("serverUrl").value = n.serverUrl;
        $<HTMLInputElement>("token").value = n.token;
      });
      void showCurrent();
    }
  });
  void showCurrent();
  if (s.serverUrl && s.serverUrl !== CLOUD_URL) ($("selfhost") as HTMLDetailsElement).open = true;
  $<HTMLInputElement>("serverUrl").value = s.serverUrl;
  $<HTMLInputElement>("token").value = s.token;
  $<HTMLInputElement>("trackByDefault").checked = s.trackByDefault;
  $<HTMLInputElement>("campaign").value = s.campaign;

  const link = $<HTMLInputElement>("setupLink");
  const hint = $("linkHint");
  link.addEventListener("input", () => {
    if (!link.value.trim()) {
      hint.textContent = "It fills in the address and token below.";
      hint.className = "hint";
      return;
    }
    const parsed = parseSetupLink(link.value);
    if (!parsed) {
      hint.textContent = "That does not look like a setup link. It ends with #connect= followed by your token.";
      hint.className = "hint error";
      return;
    }
    $<HTMLInputElement>("serverUrl").value = parsed.serverUrl;
    $<HTMLInputElement>("token").value = parsed.token;
    link.value = "";
    hint.textContent = `Filled in for ${new URL(parsed.serverUrl).host}. Click Save.`;
    hint.className = "hint ok";
    $<HTMLButtonElement>("save").focus();
  });

  $("form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = readForm();
    if (!form) return;
    // Request permission before any await on other work, while the gesture is active.
    if (!(await ensurePermission(form.serverUrl))) {
      status("Permission to contact the server was denied.", false);
      return;
    }
    await saveSettings(form);
    $<HTMLInputElement>("serverUrl").value = form.serverUrl;
    if (await test(form)) status("Saved and connected.", true);
  });

  $("test").addEventListener("click", async () => {
    const form = readForm();
    if (!form) return;
    if (!(await ensurePermission(form.serverUrl))) {
      status("Permission to contact the server was denied.", false);
      return;
    }
    await test(form);
  });
}

void main();
