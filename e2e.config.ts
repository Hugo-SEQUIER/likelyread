import type { E2EConfig } from 'e2e';
import { web } from '@e2e-dev/web';
import { gateway } from 'ai';

// The browser under test is a Chromium with the built extension loaded
// (extension/dist), started by tests/support/chromium-with-extension.mjs because
// Playwright's own launch cannot pass extension flags. The engine attaches to it
// over CDP and rides its default context, the only one where the extension and
// chrome:// pages exist. Every attempt gets a fresh browser from the launcher's
// control port; tests still set retries: 0.
const CDP_PORT = 9333;
const CONTROL_PORT = 9334;
const cdp = () => `http://127.0.0.1:${CDP_PORT}`;
const freshBrowser = async (signal: AbortSignal) => {
  const res = await fetch(`http://127.0.0.1:${CONTROL_PORT}/fresh`, { signal });
  if (!res.ok) throw new Error(`the Chromium launcher could not start a fresh browser: ${await res.text()}`);
  return ((await res.json()) as { cdp: string }).cdp;
};

export default {
  // tests/cloud runs against a local Likelyread Cloud instead: e2e.cloud.config.ts.
  tests: ['tests/**/*.e2e.ts', '!tests/cloud/**'],
  // The Vercel AI Gateway serves the model id and reads AI_GATEWAY_API_KEY, or the OIDC token of a linked Vercel project.
  // Only agent.* steps need it; the current tests are deterministic.
  agents: {
    default: {
      model: gateway('openai/gpt-6-luna-fast'),
      system: 'You are a thorough QA agent. Verify every outcome.',
    },
  },
  workers: 1,
  targets: [{
    name: 'extension',
    engine: web({ connect: { cdpEndpoint: freshBrowser, reconnectEndpoint: cdp } }),
    app: {
      // Answered by the launcher once the browser is up; tests navigate the
      // browser itself (chrome://extensions) and never open this URL.
      url: 'http://127.0.0.1:0',
      command: {
        executable: 'node',
        args: ['tests/support/chromium-with-extension.mjs', '--cdp-port', String(CDP_PORT), '--control-port', String(CONTROL_PORT), '--ready-port', '{port}'],
        log: '.e2e/logs/chromium.log',
      },
    },
  }],
} satisfies E2EConfig;
