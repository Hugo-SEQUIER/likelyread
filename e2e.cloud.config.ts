import type { E2EConfig } from 'e2e';
import { web } from '@e2e-dev/web';

// Likelyread Cloud on this computer: the server in cloud mode, with fake Google sign-in
// and a test app that connects over OAuth, all started by tests/support/cloud-server.mjs.
// It needs server/.venv (or PYTHON). A config of its own, because e2e.config.ts drives a
// Chromium with the extension instead:
//   npx e2e run --config e2e.cloud.config.ts tests/cloud/connect-apps.e2e.ts
// The current tests are deterministic, so no model is configured.
export default {
  tests: ['tests/cloud/**/*.e2e.ts'],
  output: '.e2e/cloud',
  workers: 1,
  targets: [{
    name: 'cloud',
    engine: web(),
    app: {
      url: 'http://127.0.0.1:0',
      readyUrl: 'http://127.0.0.1:{port}/healthz',
      command: {
        executable: 'node',
        args: ['tests/support/cloud-server.mjs', '--port', '{port}'],
        inheritEnv: ['PYTHON'],
        log: '.e2e/logs/cloud.log',
      },
    },
  }],
} satisfies E2EConfig;
