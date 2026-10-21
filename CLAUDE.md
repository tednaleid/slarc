# slarc

Read-only CLI that pulls Slack conversations into a DuckLake archive (Parquet plus a SQLite catalog).
Spec: `docs/spec.md`. Architecture: `docs/architecture.md`. Terms: `GLOSSARY.md`. Decisions: `docs/adr/`.
Backlog: `docs/backlog.md`. Verified Slack facts: `docs/slack-facts.md`. Plans for work in progress: `docs/plans/`.

## Commands

- `just check` runs tests, ruff, ty and the format check. It must pass before every commit.
- `just test -k NAME` runs selected tests in parallel (`-n0` runs serially), `just fmt` formats, `just run ARGS` runs slarc from the tree.
- Use `just` recipes, never bare `pytest`, `ruff` or `ty`.

## Rules

- Test first: write a failing test, watch it fail, then make it pass.
- Domain and service layer never import `slack_sdk`, `duckdb`, `httpx` or `keyring`, and never touch the filesystem or environment; see `docs/architecture.md`.
- slarc never calls a Slack method that changes anything. Only `scripts/seed-test-workspace.py` posts, directly or through `just acceptance`, and only to `naleidworkspace` (team `T0CB5LYPVFA`).
- Run `just acceptance` only when asked or when pull's discovery or delete logic changes; it uses the live test workspace.
- Live Slack calls are read-only, against the test workspace only, using the desktop session. The session holds tokens for other workspaces; never use them.
- Never print, log, commit or put in fixtures a token, cookie or keychain secret.
- Use `GLOSSARY.md` terms in code and docs.
- Commit to `main` in small, passing steps; when working from a plan, mark its tasks done, and delete the plan once its work is done.
- Docs describe slarc as it is now, never how it got here.
