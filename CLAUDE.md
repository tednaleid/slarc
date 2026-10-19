# slarc

Read-only CLI that pulls Slack conversations into an archive of append-only JSONL files.
Spec: `docs/specs/detailed-requirements.md`. Terms: `GLOSSARY.md`. Decisions: `docs/adr/`.
Plan and progress: `docs/plans/`. Verified Slack facts: `docs/specs/phase0-spike-findings.md`.

## Commands

- `just check` runs tests, ruff, ty and the format check. It must pass before every commit.
- `just test -k NAME` runs selected tests, `just fmt` formats, `just run ARGS` runs slarc from the tree.
- Use `just` recipes, never bare `pytest`, `ruff` or `ty`.

## Rules

- Test first: write a failing test, watch it fail, then make it pass.
- Domain and service layer never import `slack_sdk`, `duckdb`, `httpx` or `keyring`, and never touch the filesystem or environment. Every port has an in-memory fake.
- slarc never calls a Slack method that changes anything. Only `scripts/seed-test-workspace.py` posts, and only to `naleidworkspace` (team `T0CB5LYPVFA`).
- Live Slack calls are read-only, against the test workspace only, using the desktop session. The session holds tokens for other workspaces; never use them.
- Never print, log, commit or put in fixtures a token, cookie or keychain secret.
- Use `GLOSSARY.md` terms in code and docs.
- Commit to `main` after each plan task and mark it done in the plan.
