# slarc architecture

How the code under `src/slarc/` is put together. The spec (`docs/specs/detailed-requirements.md`) says what slarc does; this page says where each part lives and which rules keep it testable. ADR 0003 records why it has this shape.

## Layers

Dependencies point inward: `cli.py` and `bootstrap.py` know everything, adapters know ports and the domain, the service layer knows ports and the domain, and the domain knows only itself.

- **Domain** (`domain/`): pure functions and value types. `links.py` (`MessageTs`, selectors, permalinks), `envelope.py` (meta plus the verbatim Slack object), `hashing.py` (hashed subsets), `tracking.py` (`TrackedItem`, `DownloadState`, `RunRecord`), `directory.py` (`DirectoryView`, the name fallback rules), `plain.py` (`meta.plain`), `emoji.py`, and the HTML renderers in `render/`.
- **Ports** (`ports.py`): one `Protocol` per outside dependency, the value types they pass, and `SlarcError` with its subclasses, whose `str()` is the message the user sees.
- **Service layer** (`service/`): one module per use case (`pull`, `downloads`, `threads`, `selectors`, `appends`, `auth`, `listing`, `search`, `export`, `remove`). Each takes its ports as arguments. `pull` is supported by `workspaces` (one connection per workspace), `held_conversation` (what the archive holds for one conversation) and `directory_refresh`.
- **Unit of work** (`unit_of_work.py`): scopes one pull run; see below.
- **Shared state fold** (`archive_state.py`): turns archive lines into current state, latest `(fetched_at, run)` line winning. `DuckLakeArchiveStore` and the in-memory fake use it, so duplicate and out-of-order lines read the same in tests and on disk.
- **Adapters** (`adapters/`): the real implementations of the ports, plus the lake connection (`lake.py`), the credential readers (`chromium.py`, `leveldb.py`, `snappy.py`) and the rate limiter (`rate_limit.py`).
- **Config** (`config.py`): the layered settings loader and archive lookup. It reads files, so only the CLI calls it.
- **Composition root** (`bootstrap.py`): the only place real adapters are built. `real_services()` returns a `Services` value; `current_services()` returns it unless a test has installed fakes with `override()`. Context managers there open and close the archive stores, the DuckDB index and the HTTP client. `ensure_lake` runs before either archive context manager opens the lake and creates an empty lake in an archive that has no `catalog.sqlite`.
- **CLI** (`cli.py`): Typer commands. Each one parses its arguments, takes adapters from `bootstrap`, calls one service function and prints the result. A `SlarcError` becomes one plain sentence and exit code 1, never a traceback.

**The layering rule.** Domain and service code never import `slack_sdk`, `duckdb`, `httpx` or `keyring`, and never touch the filesystem or environment. Every write goes through a port (`ArchiveStore`, `BlobStore`, `ExportWriter`). The recorded exceptions read immutable package data through `importlib.resources`: `domain/emoji.py` loads `src/slarc/data/emoji.json`, and `service/export.py` loads its Jinja templates and static files under `src/slarc/export/`. Injecting either would thread a fixed table through every caller and needs no fake. `service/listing.py` imports the `Settings` type from `config.py`; it calls nothing there, and moving `Settings` is in the backlog.

## Ports

| Port | Real adapter | Test fake (`tests/fakes.py`) |
| --- | --- | --- |
| `SlackGateway` | `SessionTokenGateway` (`adapters/slack_gateway.py`) | `FakeSlackGateway` |
| `SlackConnector` | `SessionConnector` (`adapters/slack_gateway.py`) | `FakeConnector` |
| `CredentialSource` | `EnvSource`, `KeyringSource` (`adapters/credentials.py`), `DesktopSessionSource` (`adapters/desktop_session.py`), in that priority order under `CompositeSource` | `FakeCredentialSource`; `KeyringSource` runs over `FakeKeyring` |
| `ArchiveStore` | `DuckLakeArchiveStore` (`adapters/lake_store.py`) | `InMemoryArchiveStore` |
| `BlobStore` | `FsBlobStore` (`adapters/blob_store.py`) | `InMemoryBlobStore` |
| `Downloader` | `HttpDownloader` (`adapters/downloader.py`) | `FakeDownloader` |
| `ExportWriter` | `FsExportWriter` (`adapters/export_writer.py`) | `InMemoryExportWriter` |
| `QueryIndex` | `DuckDbQueryIndex` (`adapters/duckdb_index.py`) | none: tested through DuckDB over a temp lake |
| `Clock` | `SystemClock` (`bootstrap.py`) | `FakeClock` |
| `Reporter` | `RichReporter` (`cli.py`) | `RecordingReporter` |

`AssetResolver` (`domain/render/__init__.py`) is a domain-side protocol with the same pattern: `service/export.py` implements it over the archive's blobs, and tests use `FakeAssetResolver`.

`SessionTokenGateway` calls only methods in `READ_METHODS`, sends every call through a per-method token bucket, and maps Slack error codes to `NotVisible`, `ThreadNotFound`, `FileDeleted` and `AuthFailed`. `HttpDownloader` follows redirects itself and sends the token and cookie only to Slack's file host.

`DuckLakeArchiveStore` keeps appended lines and staged drops in an open batch. Its reads include the appended lines. A drop removes matching appended lines at once and deletes committed rows when the batch commits. `commit(Commit)` writes the batch, the tracked items and download states the `Commit` names, and deletes its settled download states, in one DuckDB transaction. Inside that transaction it checks each tracked item's revision, that each complete team's tracked items that are not removed are exactly the items the commit names in that team, each directory row the commit requires, and that every team with directory rows in the batch has a tracked item that is not removed. A failed check rolls back, empties the batch and raises `CommitConflict`. `append_run` commits only the run row, with its `tracked` and `downloads` lists empty, and raises `ValueError` if the open batch is not empty; only `commit` writes tracked items and download states. `InMemoryArchiveStore` applies the same checks.

Every archive write appends rows, except these: `commit` replaces the `tracked_item` and `download_state` rows it updates, `commit` deletes settled download states, and the drops (`drop_messages`, `drop_directory_lines`, `drop_workspace` and `drop_downloads`) delete rows when they commit, as `FsBlobStore.delete` deletes blobs.

`DuckDbQueryIndex` attaches the lake read-only and defines the spec's views in DuckDB's in-memory database, so a view and the lake table it reads (`message_version`, `run`, `tracked_item`) can share a name; `slarc sql` queries the views by default and the tables as `lake.<table>`. The current message is one `QUALIFY` over `message_version`, newest by `(fetched_at, run)`, with the newest message row's content kept for a deleted message. `messages()` and `thread()` read only the conversation's rows, and `thread()` only the rows of messages in the thread.

## Unit of work and the run row

`UnitOfWork` holds the `ArchiveStore`, `BlobStore` and `Clock` for one `pull` or `rm`. Entering it starts a `RunRecord` whose run id ends with a short random suffix, so commands started in the same second get different ids. `commit(items, unchanged=..., downloads_exist=..., complete_teams=...)` commits the open batch with the items, the download states in `run.downloads` that changed since the last commit, the keys passed to `settle`, and the directory refs passed to `require`, and returns the download keys the commit dropped, which leave `run.downloads`. It sends each item with the revision this run last committed for its key, so committing a stale copy of an item works. On `CommitConflict` it calls `discard()`, which empties the batch and puts `run.downloads` back as the last commit left it, and raises the conflict again. Leaving the unit of work, however the run ends (success, `SlarcError`, Ctrl-C, or SIGTERM, which the CLI turns into Ctrl-C), stamps `finished` and appends one run row. Before that, a run that ended normally commits what is left open, and records a `CommitConflict` there in the run's `errors`. A run that raised, or whose last commit failed with another error, discards what it left open, keeps only what it committed, and raises its error after the run row is written. A run killed outright keeps what it committed and writes no run row. There is one class for production and tests; tests hand it the in-memory stores. The next pull writes no duplicates because unchanged messages hash the same.

The run row records the command and args, the teams touched, an `auth` entry per workspace (team id, user id, domain, team name, credential source, time of `auth.test`), `errors` for skipped workspaces, and `counts`: `messages_seen`, `lines_appended`, `rate_limit_wait_s` (token bucket waits only), `api_calls` per Web API method, and `threads_read` per reason (`link`, `history`, `search`, `active`, `full`; each thread once, under its first reason). The tracked items and download states a run changed are committed as rows of `tracked_item` and `download_state`, which hold the current state; the run row's `tracked` and `downloads` lists are empty. `list`, `search`, `sql` and `export` read the archive and write no run row.

## Archive layout

An archive is a directory with `slarc.toml`, a DuckLake (the SQLite catalog `catalog.sqlite` plus Parquet files under `data/`), and content-addressed blobs under `blobs/sha256/`. The lake's tables are `message_version`, `directory_entry`, `run`, `tracked_item` and `download_state`; `lake.py` creates them and sets the lake's options. `docs/specs/2026-10-20-ducklake-archive-design.md` defines the tables and settings.

slarc does not import archives written before the lake (`raw/`, `directory/` and `runs.jsonl`); it ignores those files.

## How a pull flows

`pull()` in `service/pull.py`:

1. Resolves selectors to tracked items, or takes every tracked item for a bare pull. `workspaces.Workspaces` connects each workspace once per run (each connection has its own rate limiter), runs `auth.test`, records the `auth` entry, and refuses an env session whose `auth.test` names a different workspace than the link.
2. For each workspace, reads tracked threads and messages with `conversations.replies`, then refreshes the directory (`directory_refresh.refresh_directory`) on every pull: `users.list`, `emoji.list`, `usergroups.list`, `bots.info` for bots not yet stored, and `conversations.info` for each conversation involved. Directory lines are appended only when their hash or blob changed. The refresh is committed on its own; a conflict there means the workspace was removed meanwhile, so its items are skipped with a warning.
3. For each tracked conversation:
   - Retries: threads whose read failed in an earlier pull (`retry_threads`) are re-read first.
   - History: a first pull pages from `until` down to `since`, committing each page with the item's cursors moved past it, so an interrupted backfill keeps every committed page and resumes below `oldest_ts`. Later pulls page only newer than `newest_ts`. Every parent with replies in a page has its thread read.
   - Reply discovery: `search.messages` with `in:<#C> after:<date of last_search_at minus two days>`; each match's permalink `thread_ts` names a thread to re-read. A first pull skips this, since its history covered every reply. A failed search warns and leaves `last_search_at` alone, so the next pull retries.
   - Active threads: re-reads every thread with a stored reply newer than `active_thread_days`.
   - `--full` replaces history and search with a re-read of the whole range and every thread in it.
   - Archived skip: an item recorded `archived` whose conversation `conversations.info` still reports archived is recorded as pulled with no reads, unless `--full` is set or a widened `--since` or `--until` left the range unread (a widened since reopens the backfill; a widened until clears `archived`). A pull that finishes a conversation records `archived` from the same `conversations.info` result, unless a thread read failed and is left to retry, so one final refresh follows a conversation's archiving.
4. Appends each message whose hash differs from its stored line and queues its files, images, custom emoji and avatars as download states.
5. Infers deletes with `threads.infer_deletes`, a pure function, only over a range read in full this run (one thread, or a `--full` range), only for messages at or after the oldest message Slack returned, and never when Slack returned nothing. `NotVisible` and `ThreadNotFound` skip the item with `status: not_visible`, and never produce deletes. The warning appears once, when the item turns not visible; later pulls skip it quietly while it stays not visible, and the summary counts such items. Any other failed read (a network error, or a Slack error code slarc does not handle) warns and skips one thread or item: a failed thread under a conversation goes into the item's `retry_threads`, a failed history page leaves the conversation unfinished so the next pull resumes it, and a failed tracked thread or message is left as it was. A workspace with no session or rejected credentials is skipped with a warning and an `errors` entry; the pull exits non-zero when no workspace was pulled or any credentials were rejected.
6. Drains downloads (`service/downloads.py`): requeues earlier runs' pending and failed downloads and too-large files that now fit and commits them, dropping any whose download state an rm deleted, connects each workspace whose Slack files need its session, fetches in parallel, and reports each result to the reporter's downloads bar. Each result commits on its own: a download commits the directory line again with its blob and deletes the target's download state, and any other outcome commits the state's new status. A result whose target's download state an rm deleted meanwhile is dropped, so it writes no directory line and no state, and its blob is left unreferenced. A last commit adds the user and emoji lines whose image was downloaded.
7. Commits: pull commits each item named on the command line before reading it, each directory refresh, each history page, and each tracked item when it finishes or is skipped. Each commit checks the revisions of the items it writes and that every stored file and image it assumed is still stored. A conflict rolls the batch back: a command-line item is left out, a workspace or item is skipped with a warning, and any other failure of a conversation's pull discards what it has not committed. The next pull resumes from the last commit.
8. Writes `rate_limit_wait_s`, `api_calls` and `threads_read` into the run's counts in a `finally`, so an interrupted run records them too.

## Rendering and export

The domain renderers (`render/blocks.py`, `render/mrkdwn.py`, `emoji.py`) turn a message body into HTML: `rich_text` and layout blocks first, otherwise the mrkdwn `text`. They escape every Slack value and return `Markup`, so Jinja does not escape them again. Custom emoji resolve through one alias resolver, `custom_emoji_url`, which `service/downloads.py` also uses.

The Jinja partials under `src/slarc/export/templates/` own the message chrome: `_message.html.j2` (avatar, author, time, edit marker, body, previews, files, reactions, thread), `_attachment`, `_file`, `_thread` and `_thread_panel`. The environment autoescapes with `StrictUndefined`.

`service/export.py` builds view models from `QueryIndex.messages` and `QueryIndex.thread` plus a `DirectoryView` per workspace, and writes every file through `ExportWriter`: `index.html`, one page per conversation, `viewer.css`, `viewer.js` and `assets/<sha256>.<ext>`. `--single-file` writes one HTML file with assets as `data:` URIs; avatars and other repeated chrome images become one CSS class per blob instead of one URI per use. Reaction pills show a CSS popup listing who reacted with each variant. A thread's parent links to a side panel opened by CSS `:target`, holding a copy of the parent and every reply; no panel is open until a link targets one, Close clears the target in place, the panel is full screen on narrow screens and inline when printed, and a thread or message export shows its replies inline. `viewer.js` shows times in the viewer's time zone (unless `--tz` was given) and wires up jump to date.

## Testing

- Service tests run against the fakes in `tests/fakes.py` (`tests/service/pull_harness.py` composes them for pull): no network, no disk.
- Contract tests in `tests/contract/` run the same tests against `DuckLakeArchiveStore` and `InMemoryArchiveStore`, and against `FsBlobStore` and `InMemoryBlobStore`. `DuckDbQueryIndex` is tested over a temp lake loaded from the recorded archive (`recorded_lake` in `tests/recorded_fixtures.py`, which uses the test-only loader in `tests/jsonl_loader.py`), with further rows committed through `DuckLakeArchiveStore`.
- `SessionTokenGateway` is tested with a fake Web API client serving spike-shaped responses. The LevelDB reader and cookie decryption are tested against synthetic files from `tests/leveldb_writer.py` and `tests/cookies_writer.py`.
- Recorded fixtures: `tests/fixtures/workspace/` is a JSONL archive of the test workspace, so it stays reviewable in git. `just record-fixtures` pulls into a temp lake, exports it to JSONL with the script's own export and scrubs secrets line by line; `test_fixture_archive_has_no_secrets` guards it. `tests/fixtures/spike/` holds raw Slack responses from the spike.
- Golden files: every recorded message renders to `tests/golden/`; `just test --update-golden` rewrites them after an intended change.
- Live acceptance: `just acceptance` runs `scripts/acceptance.py` against `naleidworkspace` and checks the Phase 1 ship criteria. It is not part of `just check` or CI; run it only when asked or when pull's discovery or delete logic changes. Its writes go through `scripts/seed-test-workspace.py`.
