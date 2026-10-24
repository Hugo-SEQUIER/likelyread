import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

// The id Chrome gives the extension loaded unpacked from extension/dist: the
// first 32 hex digits of the SHA-256 of its path, each written as a letter a to p.
// It must have a running service worker, which tells it apart from Chrome's own
// component extensions.
export async function extensionId(cdp: string): Promise<string> {
  const dist = fileURLToPath(new URL('../../extension/dist', import.meta.url));
  const id = [...createHash('sha256').update(dist).digest('hex').slice(0, 32)]
    .map((c) => String.fromCharCode(97 + parseInt(c, 16)))
    .join('');
  const targets = (await (await fetch(`${cdp}/json/list`)).json()) as { type: string; url: string }[];
  if (!targets.some((t) => t.type === 'service_worker' && t.url.startsWith(`chrome-extension://${id}/`))) {
    throw new Error(`no service worker for ${id} (${dist}); targets: ${targets.map((t) => `${t.type} ${t.url}`).join(', ')}`);
  }
  return id;
}

// The runner navigates only to http(s) URLs. Chrome's own pages, such as
// chrome://extensions, are reached by asking the browser's DevTools endpoint
// to navigate the engine's tab: the one page target, which the engine leaves
// at about:blank when an attempt starts.
export async function openInEngineTab(cdp: string, url: string): Promise<void> {
  const targets = (await (await fetch(`${cdp}/json/list`)).json()) as {
    type: string;
    url: string;
    webSocketDebuggerUrl: string;
  }[];
  const page = targets.find((t) => t.type === 'page' && t.url === 'about:blank');
  if (!page) throw new Error(`no about:blank page target; targets: ${targets.map((t) => `${t.type} ${t.url}`).join(', ')}`);
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise<void>((resolve, reject) => {
    socket.onopen = () => resolve();
    socket.onerror = () => reject(new Error(`DevTools socket to ${page.webSocketDebuggerUrl} failed`));
  });
  try {
    const answered = new Promise<void>((resolve, reject) => {
      socket.onmessage = (message) => {
        const reply = JSON.parse(String(message.data)) as { id?: number; error?: { message: string } };
        if (reply.id !== 1) return;
        if (reply.error) reject(new Error(`Page.navigate failed: ${reply.error.message}`));
        else resolve();
      };
    });
    socket.send(JSON.stringify({ id: 1, method: 'Page.navigate', params: { url } }));
    await answered;
  } finally {
    socket.close();
  }
}
