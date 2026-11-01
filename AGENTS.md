# Agent guide

How to run the server, the extension, the CLI and their tests: the README's "Development" section. CI and its runner: [docs/ci-runner.md](docs/ci-runner.md).

## Hermes review loop

Every pull request gets an automatic **Hermes review** comment about 10 minutes after each push: code review items `C1, C2...`, simplify items `S1...` (AI slop to cut, never blocking) and a security verdict. Before you call a pull request done:

1. Wait for the review of your current head: the `head` in the comment's hidden `hermes-pr-review-data` JSON block must equal `gh pr view <n> --json headRefOid`. The read command is in [review-loop.md](https://github.com/Hugo-SEQUIER/ci-workflows/blob/main/docs/review-loop.md).
2. Fix every item with `blocking: true`. Apply each simplify item you agree with, and decline the others with a concrete reason.
3. Run the checks, push one commit `refactor: apply Hermes review (round <n>)`, then post one comment starting with `<!-- hermes-pr-fix head=<new sha> reviewed=<reviewed sha> -->` that lists `Applied:` and `Declined:` IDs.

At most 2 fix rounds per pull request. Never merge, approve or enable auto-merge: Hugo merges.
