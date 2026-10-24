// A stand-in for a Likelyread server, for tests that drive the extension: it
// answers the API calls the extension makes from its service worker and popup,
// with fixed email summaries, and records what the extension sends.
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';

export interface FakeEmail {
  id: string;
  subject: string;
  recipients: string[];
  status: 'opened' | 'not_opened' | 'prefetch_only';
  opens: number;
  self: number;
  /** Seconds before now of the last open, for opened emails. */
  lastOpenAgo?: number;
}

export interface FakeLikelyread {
  url: string;
  token: string;
  /** Bodies of POST /api/emails, in order. */
  registered: { id: string; subject: string; recipients: string[] }[];
  close(): Promise<void>;
}

export async function startFakeLikelyread(emails: FakeEmail[]): Promise<FakeLikelyread> {
  const token = 'e2e-token-0123456789abcdef';
  const registered: FakeLikelyread['registered'] = [];
  const now = () => Math.floor(Date.now() / 1000);
  const summary = (e: FakeEmail) => ({
    id: e.id,
    subject: e.subject,
    recipients: e.recipients,
    sent_at: now() - 3600,
    campaign: null,
    sender: 'you@example.com',
    opens: e.opens,
    first_open: e.lastOpenAgo === undefined ? null : now() - e.lastOpenAgo,
    last_open: e.lastOpenAgo === undefined ? null : now() - e.lastOpenAgo,
    prefetch: 0,
    self: e.self,
    status: e.status,
  });

  const send = (res: ServerResponse, status: number, body: unknown) => {
    res.writeHead(status, { 'content-type': 'application/json', ...cors });
    res.end(JSON.stringify(body));
  };
  const cors = {
    'access-control-allow-origin': '*',
    'access-control-allow-headers': 'Authorization, Content-Type',
    'access-control-allow-methods': 'GET, POST, PATCH, DELETE, OPTIONS',
  };
  const readBody = async (req: IncomingMessage) => {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    return raw ? JSON.parse(raw) : null;
  };

  const server = createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', 'http://fake');
    if (req.method === 'OPTIONS') {
      res.writeHead(204, cors);
      return res.end();
    }
    if (req.headers.authorization !== `Bearer ${token}`) return send(res, 401, { detail: 'invalid token' });
    const one = /^\/api\/emails\/([0-9a-f-]{36})$/.exec(url.pathname);
    if (req.method === 'GET' && one) {
      const e = emails.find((x) => x.id === one[1]);
      return e ? send(res, 200, { ...summary(e), hits: [] }) : send(res, 404, { detail: 'not found' });
    }
    if (req.method === 'GET' && url.pathname === '/api/emails') return send(res, 200, emails.map(summary));
    if (req.method === 'POST' && url.pathname === '/api/emails') {
      registered.push(await readBody(req));
      return send(res, 200, { ok: true });
    }
    if (req.method === 'POST' && /^\/api\/emails\/[0-9a-f-]{36}\/suppress$/.test(url.pathname)) return send(res, 200, { ok: true });
    if (req.method === 'GET' && url.pathname === '/api/campaign-names') return send(res, 200, []);
    if (req.method === 'GET' && url.pathname === '/api/senders/health') return send(res, 200, []);
    return send(res, 404, { detail: 'not found' });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as { port: number };
  return {
    url: `http://127.0.0.1:${port}`,
    token,
    registered,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}
