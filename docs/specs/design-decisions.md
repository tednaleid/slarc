# Design decisions

Decisions made while reviewing `detailed-requirements.md`. Where they conflict, this file wins. Terms follow `GLOSSARY.md`; the larger decisions also have ADRs in `docs/adr/`.

## Command set

The CLI is the product's surface, so it stays small and each command does a lot (deep modules, in Ousterhout's sense). Built with Typer.

| Command | Purpose |
| --- | --- |
| `slarc pull [link ...] [--since DATE] [--until DATE] [--full] [--downloads N] [--max-file-size MB]` | With links: start tracking them and pull. Without: refresh everything tracked. Also drains pending and failed downloads. `--full` re-reads everything instead of only what is new |
| `slarc list [--remote]` | Archive location and config file, then each tracked item with message count, last pull and pending files. `--remote` lists every conversation the session can see and marks which are tracked |
| `slarc search QUERY [--in CONV] [--from USER]` | Matches with author, conversation, time, snippet and Slack link |
| `slarc export LINK... [--out DIR] [--single-file] [--include-deleted]` | Static HTML. PDF later arrives as `--format pdf` |
| `slarc serve` | Phase 2 local viewer |
| `slarc auth [--paste]` | Sessions found and who the user is in each workspace; `--paste` stores a browser token and cookie in the OS keychain |
| `slarc sql QUERY` | Escape hatch over the DuckDB views |

Dropped from the spec: `archive`, `sync`, `files`, `status`, `config`, `validate`, `fixtures`. Developer tooling such as fixture recording is a `just` recipe, not a command. A Phase 2 `import` may become `slarc pull <export.zip>`.

## Links

- A channel or DM link pulls that conversation, with every thread whose parent falls in the optional date range.
- A message link pulls the thread containing that message. A message with no thread is pulled alone, with its files, emoji and users.
- `--channel` on a message link pulls the whole conversation.

## Archive location

- An archive is a directory containing `slarc.toml`. No named archives and no workspace-to-archive mapping. One archive can hold several workspaces.
- Lookup: walk up from the current directory for `slarc.toml`; else a default archive path from the global config if set; else create one in the current directory after a one-time confirmation (`--yes` skips it).
- Global state follows XDG: settings in `~/.config/slarc/config.toml`, derived data such as search indexes in `~/.cache/slarc/`. Pasted credentials go in the OS keychain.
- `slarc.toml` holds only portable settings: no absolute paths, nothing tied to one user.

## Git and shared archives

- slarc knows nothing about git and writes no git files. Its files are append-only text so they suit git (ADR 0001).
- Reading tolerates duplicate and out-of-order lines, as a git merge of two people's pulls produces.
- A tracked conversation the current session cannot see is skipped with a warning and never marked deleted.
- Slack's read marker (`last_read`) is stored per user.

## Pulling

- Each pull of a tracked channel: new history since the last pull; `search.messages` scoped to the channel since the last pull, to find replies under old parents (a reply's permalink carries `thread_ts`); re-read every thread with a reply in the last 7 days. `--full` re-reads everything.
- Tracked threads and single messages are re-read on every pull.
- Deletes are inferred only from `subtype: tombstone`, or from absence in a range that was fully re-read and that Slack still serves completely (retention limits must never produce deletes).
- API calls run one at a time through a per-method rate limiter. File downloads run in parallel (`--downloads`, default 4). Concurrent API calls only if measurement shows the need.
- Files download up to 100 MB each by default, video included; larger files are recorded as skipped and shown by `list`. External files (Google Drive and the like) are linked, not downloaded.
- Every image a message references is downloaded into the archive under the same limit: image blocks (giphy), link preview images and thumbnails, service icons.

## Credentials

- The desktop session is read on macOS and Linux; the paste fallback works everywhere, and is the only option on Windows.
- slarc uses only the workspace a link names. The desktop app holds tokens for every signed-in workspace; slarc never calls the API for the others.
- The Chromium Local Storage reader (LevelDB, Snappy, Chromium key and value encoding) is written in-house, targeted at reading one key. `ccl_chromium_reader` is used only as a dev-time comparison on the developer's machine, never shipped. If the reader fails, slarc says so and points at `slarc auth --paste`.

## Exports

- An export contains exactly what is named; a named DM is included.
- Email addresses are never written into exports.
- Output is a folder by default; `--single-file` inlines everything.
- Deleted messages are hidden unless `--include-deleted`. `serve` shows them with a marker.
- No encryption is built in; the docs recommend an encrypted volume for sensitive archives.

## Architecture

- Hexagonal, Cosmic Python style: domain, ports with in-memory fakes, repository, service layer, and a unit of work that buffers one run's appends and writes one run line. No message bus and no domain events.

## Phase 1 scope

Desktop-session auth plus the paste fallback; `pull` for channels, DMs, threads and single messages with date ranges; JSONL storage with files, users and emoji; `search`, `list`, `auth`, `sql`; a minimal `export`. Moved to Phase 2: the generated JSON Schema and `validate`, the rate-limit clamp probe, `serve`, `import`, `slack-zip` export.

## Toolchain and repo

- Python 3.14, uv, Typer + Rich, DuckDB, slack_sdk, ruff (lint and format), ty (pinned), pytest.
- Installed with `uv tool install`; `just install` does an editable install. Developer scripts use a uv shebang.
- just recipes follow the just-bootstrap conventions: `check`, `test`, `lint`, `fmt`, `build`, `install`, `clean`, `bump`, `retag`, `install-hooks`. A pre-commit hook runs `just check`. GitHub Actions CI runs `just check` on macOS and Linux.
- Public repo at `tednaleid/slarc`, MIT license.

## Test workspace and fixtures

- `naleidworkspace` (team `T0CB5LYPVFA`) is the only workspace that may ever be written to, and only by `scripts/seed-test-workspace.py`, a developer script outside the package that takes a workspace and channel, refuses any other team id, and posts into a dedicated channel.
- Content the script cannot create (anything posted as jim, group DMs, custom emoji uploads, display name changes, huddles, canvases) is created by hand.
- `just record-fixtures` pulls the test workspace into committed fixtures under `tests/fixtures/`. Names stay; tokens and secrets are scrubbed and emails become `@example.invalid`. Tests never need access to the workspace.

## How agents work

- Commit to `main` task by task with `just check` passing; record progress in the plan file so a fresh session can resume.
- Agents may use the developer's desktop session for read-only calls to the test workspace without asking.
- Stop for the human at the end of each phase, for decisions this file does not cover, for test content only a human can create, and when Slack contradicts the plan.
