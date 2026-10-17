import { normalizeServerUrl } from "../lib";
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

async function main(): Promise<void> {
  const s = await getSettings();
  $<HTMLInputElement>("serverUrl").value = s.serverUrl;
  $<HTMLInputElement>("token").value = s.token;
  $<HTMLInputElement>("trackByDefault").checked = s.trackByDefault;
  $<HTMLInputElement>("campaign").value = s.campaign;

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
