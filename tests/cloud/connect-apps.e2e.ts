import { createHash, randomBytes } from 'node:crypto';
import { test } from '@e2e-dev/web';
import { expect } from 'e2e';
import { TEST_APP } from '../support/cloud-fakes.mjs';

// Likelyread Cloud on this computer (e2e.cloud.config.ts): fake Google signs in as
// ann@example.com, and the test app connects the way Claude does. It asks to connect,
// the user signs in and approves on the consent page, the browser goes back to the app
// with a code, and the app trades it for a token that works at /mcp.

const pkce = () => {
  const verifier = randomBytes(32).toString('base64url');
  return { verifier, challenge: createHash('sha256').update(verifier).digest('base64url') };
};

const listTools = (base: string, token: string) =>
  fetch(new URL('/mcp', base), {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      'MCP-Protocol-Version': '2025-11-25',
    },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }),
  });

test('an app connects with a Google sign-in and an approve page, then is disconnected', { retries: 0, requires: ['browser'] }, async ({ app, screen, browser }) => {
  const base = new URL(app.baseUrl as string).origin;
  const mcp = `${base}/mcp`;

  await app.open('/');
  await screen.getByRole('link', 'Continue with Google').tap();
  await expect(screen.getByRole('link', 'Connect')).toBeVisible();

  // Connect offers Claude a prefilled Add custom connector, and the address for ChatGPT.
  await app.open('/#/connect');
  await expect(screen.getByRole('heading', 'Claude and ChatGPT')).toBeVisible();
  await expect(screen.getByRole('link', 'Add to Claude')).toHaveAttribute(
    'href',
    `https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName=Likelyread&connectorUrl=${encodeURIComponent(mcp)}`,
  );
  await expect(screen.getByLabel('MCP server address')).toHaveValue(mcp);

  // The app asks to connect, as Claude does when the user clicks Connect.
  const { verifier, challenge } = pkce();
  const ask = new URLSearchParams({
    response_type: 'code',
    client_id: TEST_APP.clientId,
    redirect_uri: TEST_APP.redirectUri,
    code_challenge: challenge,
    code_challenge_method: 'S256',
    state: 'e2e-state',
    resource: mcp,
  });
  await app.open(`/oauth/authorize?${ask}`);
  await expect(screen.getByRole('heading', `Connect ${TEST_APP.name}`)).toBeVisible();
  await expect(screen.getByText('ann@example.com', { exact: false })).toBeVisible();
  await expect(screen.getByText('runs on this computer', { exact: false })).toBeVisible();
  await screen.getByRole('button', 'Approve').tap();

  // Back on the app, with a code and this server's name in iss.
  await browser.waitForURL(/\/app\/callback\?/);
  await expect(screen.getByRole('heading', 'Signed in')).toBeVisible();
  const back = new URL(await browser.url());
  expect(back.searchParams.get('state')).toBe('e2e-state');
  expect(back.searchParams.get('iss')).toBe(base);

  const answer = await fetch(new URL('/oauth/token', base), {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      code: back.searchParams.get('code') ?? '',
      code_verifier: verifier,
      client_id: TEST_APP.clientId,
      redirect_uri: TEST_APP.redirectUri,
      resource: mcp,
    }),
  });
  expect(answer.status).toBe(200);
  const tokens = (await answer.json()) as { access_token: string; refresh_token: string; expires_in: number };
  expect(tokens.expires_in).toBe(3600);
  const listed = (await (await listTools(base, tokens.access_token)).json()) as { result: { tools: unknown[] } };
  expect(listed.result.tools).toHaveLength(11);

  // Account lists the app; Disconnect ends its access.
  await app.open('/#/account');
  await expect(screen.getByRole('list', 'Connected apps')).toContainText(TEST_APP.name);
  await browser.onDialog('accept');
  await screen.getByRole('button', 'Disconnect').tap();
  await expect(screen.getByText('Connect Claude or ChatGPT')).toBeVisible();
  expect((await listTools(base, tokens.access_token)).status).toBe(401);
});
