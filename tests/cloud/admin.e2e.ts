import { test } from '@e2e-dev/web';
import { expect } from 'e2e';
import { FAKE, PEOPLE } from '../support/cloud-fakes.mjs';

// The Admin page of Likelyread Cloud on this computer (e2e.cloud.config.ts). Fake Google signs
// in Ann, whose address is in ADMIN_EMAILS, or Bob, who is not, when the browser carries the
// cookie e2e_person=bob.

const call = (base: string, path: string, token: string, init: RequestInit = {}) =>
  fetch(new URL(path, base), { ...init, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' } });

test('the admin sees every account and what they use', { retries: 0, requires: ['browser'] }, async ({ app, screen, browser }) => {
  const base = new URL(app.baseUrl as string).origin;
  await app.open('/');
  await screen.getByRole('link', 'Continue with Google').tap();
  await expect(screen.getByRole('link', 'Admin')).toBeVisible();

  // Ann tracks an email in a campaign, as the extension does after a send.
  const token = (await browser.evaluate(() => localStorage.getItem('likelyread.token'))) as string;
  const sent = await call(base, '/api/emails', token, {
    method: 'POST',
    body: JSON.stringify({ id: crypto.randomUUID(), subject: 'Proposal for Acme', recipients: ['client@example.com'], campaign: 'Launch' }),
  });
  expect(sent.status).toBe(200);

  await screen.getByRole('link', 'Admin').tap();
  await expect(screen.getByRole('heading', 'Last 30 days')).toBeVisible();
  const campaigns = screen.getByRole('table', 'What they use').getByRole('row').filter({ hasText: 'Campaigns' });
  await expect(campaigns.getByRole('cell').nth(1)).toHaveText('1');

  // Ann's row: her plan, her email counts and how she signed in, never what she sent.
  const ann = screen.getByRole('table', 'Accounts').getByRole('row').filter({ hasText: PEOPLE.ann });
  await expect(ann.getByRole('cell').nth(1)).toHaveText('Free');
  await expect(ann.getByRole('cell').nth(5)).toHaveText('1');
  await expect(ann.getByRole('cell').nth(6)).toHaveText('Dashboard');
  await expect(screen.getByText('Proposal for Acme', { exact: false })).toBeHidden();

  await screen.getByRole('combobox', 'Sort accounts').selectOption('Most emails');
  await expect(browser).toHaveURL(/#\/admin\?sort=emails/);
  await expect(screen.getByRole('table', 'Accounts').getByRole('row').nth(1)).toContainText(PEOPLE.ann);

  const search = screen.getByRole('searchbox', 'Search accounts');
  await search.fill('nobody@');
  await search.press('Enter');
  await expect(screen.getByText('No accounts match.')).toBeVisible();
  await expect(screen.getByRole('heading', '0 accounts matching "nobody@"')).toBeVisible();

  // Not even Ann's own agents can read it.
  const agent = (await (await call(base, '/api/me/agent-token', token, { method: 'POST' })).json()) as { token: string };
  expect((await call(base, '/api/admin/accounts', agent.token)).status).toBe(404);
});

test('an account outside ADMIN_EMAILS has no Admin page', { retries: 0, requires: ['browser'] }, async ({ app, screen, browser }) => {
  const base = new URL(app.baseUrl as string).origin;
  await browser.setCookies([{ name: 'e2e_person', value: 'bob', url: FAKE }]);
  await app.open('/');
  await screen.getByRole('link', 'Continue with Google').tap();
  await screen.getByRole('link', 'Account').tap();
  await expect(screen.getByText(`Signed in as ${PEOPLE.bob}.`)).toBeVisible();
  await expect(screen.getByRole('link', 'Admin')).toBeHidden();

  // Typing the address shows the overview, and the API answers as if the page did not exist.
  await app.open('/#/admin');
  await expect(screen.getByText('Nothing tracked', { exact: false })).toBeVisible();
  const token = (await browser.evaluate(() => localStorage.getItem('likelyread.token'))) as string;
  for (const path of ['/api/admin/stats', '/api/admin/accounts']) {
    expect((await call(base, path, token)).status).toBe(404);
  }
});
