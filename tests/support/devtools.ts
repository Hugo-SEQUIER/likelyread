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
