# CI runner

CI is `.github/workflows/ci.yml`. It runs on every pull request, on every push to `main`, and by hand (`workflow_dispatch`), on a self-hosted GitHub Actions runner on Hugo's server, never on GitHub-hosted machines.

- Runner: `likelyread-host-1`, label `likelyread`, registered to this repository only.
- Linux user: `gha-likelyread`. No sudo and no Docker access, so no check may need Docker.
- Limits: 4 GB of memory and 3 CPU cores for this runner, inside a slice shared by all CI on the server. One runner means jobs of this repository run one at a time, so the checks are steps of one `check` job.
- Pull requests from forks run on GitHub's hosted `ubuntu-24.04` instead, so outside code never runs on the server, and runs for outside contributors wait for Hugo's approval.
- Upkeep: `systemctl status actions.runner.Hugo-SEQUIER-likelyread.likelyread-host-1.service`.

## What runs

The `check` job installs the server's Python 3.12 dependencies into `server/.venv` with uv, the npm packages of `extension/` and of the repository root with Node 24, and Playwright's full Chromium (cached in the runner's home), then runs:

- server tests: `pytest -q` in `server/`
- CLI tests: `python -m pytest -q tests` in `cli/`
- extension: `npm run typecheck`, `npm test` and `npm run build` in `extension/`
- extension e2e: `npm run e2e` in `extension/` (real Chromium, fake Gmail, real server). It gives the server 10 seconds to start; on a very busy host it can fail with "server not up", and a rerun passes.
- e2e package: `npx e2e run --config e2e.cloud.config.ts` from the root (Likelyread Cloud with fake Google)

The `security` job calls the shared workflow `Hugo-SEQUIER/ci-workflows/.github/workflows/security.yml@v1` on the same runner. gitleaks scans the commits a push or pull request adds and fails the run on a secret. osv-scanner reads the lockfiles and fails a pull request only when it adds a vulnerable dependency; older findings are listed in the run summary.

## Left out

- `npx e2e run` with `e2e.config.ts` (`tests/extension-listing.e2e.ts`, `tests/gmail-receipts.e2e.ts`): Chromium aborts with "FATAL ... No usable sandbox!" and the run ends with `APP_UNREACHABLE`. `tests/support/chromium-with-extension.mjs` starts Chromium without `--no-sandbox`, and this host sets `kernel.apparmor_restrict_unprivileged_userns = 1`, so the runner user cannot create the sandbox's user namespace. The other browser checks pass because they start Chromium with `--no-sandbox` themselves or through Playwright's launch. To bring it in, either the launcher passes `--no-sandbox` when `CI` is set, or the host gets an AppArmor profile that allows user namespaces for the runner's Chromium.
- `npm run e2e:cloud` in `extension/` (`extension/e2e/cloud-e2e.mjs`): fails on current `main` with 21 of 30 checks passing. The failing checks are the Connect page's agent token ones, for example "no agent token is made when the page opens" and "its token lists the tools on /mcp 401". The cloud pages are covered by `tests/cloud/` through `e2e.cloud.config.ts`.
- No linter or formatter: the repository configures none.
- No deploy step: deployment is the Docker compose stack in `deploy/`, outside CI.
