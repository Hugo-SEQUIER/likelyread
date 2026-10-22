import { readFileSync } from 'node:fs';
import { test } from '@e2e-dev/web';
import { expect } from 'e2e';
import { openInEngineTab } from './support/devtools.ts';

// The Chrome Web Store takes the listing's name and version from the manifest,
// and Chrome shows the same two on chrome://extensions for the built extension.
const manifest = JSON.parse(readFileSync(new URL('../extension/manifest.json', import.meta.url), 'utf8')) as {
  name: string;
  version: string;
};
const CDP = 'http://127.0.0.1:9333';

test('Chrome lists the extension as LikelyRead, at the manifest version', { retries: 0, requires: ['browser'] }, async ({ browser, screen }) => {
  // The engine takes the browser's first tab on its first navigation; about:blank is the one non-http(s) URL it opens.
  await browser.goto('about:blank');
  await openInEngineTab(CDP, 'chrome://extensions/');
  await expect(browser).toHaveURL(/^chrome:\/\/extensions\//);

  // The card on the list: the name is its heading.
  await expect(screen.getByRole('heading', 'LikelyRead', { level: 3 })).toBeVisible();

  // The details page shows the version (the card keeps it for developer mode only).
  await screen.getByRole('button', 'Details').tap();
  await expect(screen.getByRole('heading', 'LikelyRead', { level: 1 })).toBeVisible();
  await expect(screen.getByText('Version')).toBeVisible();
  await expect(screen.getByText(manifest.version, { visible: true })).toBeVisible();
});
