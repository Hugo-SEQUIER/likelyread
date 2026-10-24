import { test, type Browser } from '@e2e-dev/web';
import { expect, type Screen } from 'e2e';
import { extensionId, openInEngineTab } from './support/devtools.ts';
import { startFakeLikelyread, type FakeEmail, type FakeLikelyread } from './support/fake-likelyread.ts';

// The extension in Chromium against a fake Gmail (served by a route on
// mail.google.com) and a fake Likelyread server: the ticks on your own sent
// emails in a conversation, the note after a tracked send, and the popup's
// label for an email only you have seen.
const CDP = 'http://127.0.0.1:9333';
const OPENED = '9b2a6f3e-1c2d-4e5f-8a9b-0c1d2e3f4a5b';
const TO_MYSELF = '0f8fad5b-d9cb-469f-a165-708677289501';
const EMAILS: FakeEmail[] = [
  { id: OPENED, subject: 'Proposal for the Q1 rollout', recipients: ['maya@example.org'], status: 'opened', opens: 2, self: 1, lastOpenAgo: 300 },
  { id: TO_MYSELF, subject: 'Testing Likelyread', recipients: ['you@example.com'], status: 'not_opened', opens: 0, self: 1 },
];

// Gmail as the content script sees it: the account in the tab title, a
// conversation with two messages you sent (their pixels behind Google's image
// proxy), and a compose window.
function fakeGmail(server: string): string {
  const pixel = (id: string) => `<img alt="" width="1" height="1" src="https://ci3.googleusercontent.com/meips/e2e#${server}/p/${id}.gif">`;
  const message = (body: string, id: string) => `
    <div class="adn"><div class="gs">
      <div class="gE"><table class="cf gJ"><tbody><tr>
        <td class="gF"><h3 class="iw"><span class="gD" email="you@example.com">You</span></h3></td>
        <td class="gH"><div class="gK"><span class="g3">2:02 PM</span></div></td>
      </tr></tbody></table></div>
      <div class="a3s"><p>${body}</p>${pixel(id)}</div>
    </div></div>`;
  return `<!doctype html><html><head><title>Inbox - you@example.com - Gmail</title></head><body>
    <h2 class="hP">Proposal for the Q1 rollout</h2>
    ${message('Hi Maya, here is the proposal.', OPENED)}
    ${message('Does this work?', TO_MYSELF)}
    <div class="M9">
      <table><tbody><tr class="bzf"><td><div name="to"><div class="afV" data-hovercard-id="maya@example.org">Maya</div></div></td></tr></tbody></table>
      <input name="subjectbox" value="E2E follow-up" aria-label="Subject">
      <div><div class="Am Al editable" contenteditable="true" role="textbox" g_editable="true" aria-label="Message Body">Hi Maya</div>
      <table><tbody><tr class="btC"><td class="gU Up"><div class="J-J5-Ji btA"><div class="dC">
        <div role="button" tabindex="1" class="T-I J-J5-Ji aoO v7 T-I-atl L3">Send</div>
      </div></div></td><td></td></tr></tbody></table></div>
    </div>
  </body></html>`;
}

// Connect the extension to the fake server the way its options page stores it.
async function connect(browser: Browser, screen: Screen, server: FakeLikelyread): Promise<string> {
  // The engine takes the browser's first tab on its first navigation; about:blank is the one non-http(s) URL it opens.
  await browser.goto('about:blank');
  const id = await extensionId(CDP);
  await openInEngineTab(CDP, `chrome-extension://${id}/options.html`);
  await expect(screen.getByRole('heading', 'Connect Likelyread')).toBeVisible();
  await browser.evaluate(`chrome.storage.local.set(${JSON.stringify({ serverUrl: server.url, token: server.token, trackByDefault: true, campaign: '' })})`);
  return id;
}

test('your sent emails show their open status in Gmail, and a tracked send says so', { retries: 0, requires: ['browser'] }, async ({ browser, screen }) => {
  const server = await startFakeLikelyread(EMAILS);
  try {
    await connect(browser, screen, server);
    await browser.route('https://mail.google.com/**', (route) => route.fulfill({ contentType: 'text/html', body: fakeGmail(server.url) }));
    await browser.route('https://ci3.googleusercontent.com/**', (route) => route.fulfill({ status: 204, body: '' }));
    await browser.goto('https://mail.google.com/mail/u/0/#inbox/e2e');

    // Two red ticks on the opened email, linking to it in the dashboard.
    const opened = screen.getByRole('link', /^Likelyread: Opened 2 times/);
    await expect(opened).toBeVisible({ timeout: 10_000 });
    await expect(opened).toHaveText('Opened ×2');
    await expect(opened).toHaveAttribute('href', `${server.url}/#/email/${OPENED}`);
    // The test you sent yourself: tracked, and only you have seen it.
    await expect(screen.getByRole('link', /^Likelyread: Not opened yet\. Only you have viewed it/)).toHaveText('Seen by you only');

    // Send from the compose window: the note says it is tracked and offers the dashboard.
    await screen.getByRole('button', 'Send').tap();
    await expect(screen.getByRole('status')).toContainText('Tracked by Likelyread');
    await expect(screen.getByRole('link', 'Open dashboard')).toHaveAttribute('href', `${server.url}/`);
    await expect.poll(() => server.registered.map((e) => e.subject), { timeout: 10_000 }).toEqual(['E2E follow-up']);
  } finally {
    await server.close();
  }
});

test('the popup says when only you have seen an email', { retries: 0, requires: ['browser'] }, async ({ browser, screen }) => {
  const server = await startFakeLikelyread(EMAILS);
  try {
    const id = await connect(browser, screen, server);
    // The test sent to yourself is "Seen by you only", not "Not opened".
    await browser.goto('about:blank');
    await openInEngineTab(CDP, `chrome-extension://${id}/popup.html`);
    await expect(screen.getByText('Testing Likelyread')).toBeVisible({ timeout: 10_000 });
    await expect(screen.getByText('Seen by you only')).toBeVisible();
  } finally {
    await server.close();
  }
});
