# slarc architecture

slarc is a read-only command line tool. It pulls Slack conversations, using the session the user's Slack desktop app already holds, into an archive on local disk, then searches and renders that archive. This page describes how the code is built and why. `docs/spec.md` describes what slarc does, `GLOSSARY.md` defines the terms, and `docs/adr/` records the larger decisions.

## Overview

```
                 slarc pull | list | search | sql | export | rm | auth
                                        |
                           cli.py  (Typer commands, Rich output)
                                        |
                bootstrap.py  (builds real adapters; tests install fakes)
                                        |
        service/  (one module per use case)  +  unit_of_work.py  +  domain/
                                        |
                            ports.py  (Protocols)
                                        |
    adapters/ -----------------+----------------------+--------------------------+
        |                      |                      |                          |
   Slack Web API        the archive directory     XDG cache                 OS keychain,
   (session token)      catalog.sqlite, data/,    locks/, duckdb/            desktop app
                        blobs/                    extensions                 Cookies, LevelDB
```

A command parses its arguments, takes its adapters from `bootstrap`, calls one service function and prints the result. Service code sees only ports and domain types, so pull's hard parts (resuming, reply discovery, delete inference, skipping a workspace) are tested in milliseconds against in-memory fakes (ADR 0002). The archive is a DuckLake: Parquet data files and a SQLite catalog, read and written through DuckDB, with downloaded files in a content-addressed blob store beside it (ADR 0003). Credentials come from the user's own Slack session and never enter the archive (ADR 0001).

## Code layout

Dependencies point inward: `cli.py` and `bootstrap.py` know everything, adapters know ports and the domain, the service layer knows ports and the domain, and the domain knows only itself.

- **Domain** (`domain/`): pure functions and value types. `links.py` (`MessageTs`, selectors, permalinks), `envelope.py` (meta plus the verbatim Slack object), `hashing.py` (hashed subsets), `tracking.py` (`TrackedItem`, `DownloadState`, `RunRecord`), `directory.py` (`DirectoryView`, the name fallback rules), `plain.py` (`meta.plain`), `emoji.py`, and the HTML renderers in `render/`.
- **Ports** (`ports.py`): one `Protocol` per outside dependency, the value types they pass, and `SlarcError` with the subclasses services catch. Every `SlarcError`'s `str()` is the message the user sees; the lake's own (`ExtensionsMissing`, `OwnedElsewhere`, `CatalogNotSynced`, `SyncNotFinished`) live in `adapters/lake.py`, and `NoMachineId` in `adapters/host.py`.
- **Service layer** (`service/`): one module per use case (`pull`, `downloads`, `threads`, `selectors`, `appends`, `auth`, `listing`, `search`, `export`, `remove`). Each takes its ports as arguments. `pull` is supported by `workspaces` (one connection per workspace), `held_conversation` (what the archive holds for one conversation) and `directory_refresh`.
- **Unit of work** (`unit_of_work.py`): scopes one `pull` or `rm` run.
- **Shared archive rules** (`archive_state.py`): picks the current state from appended rows, the newest `(fetched_at, run)` winning, and checks a commit's revisions and teams. `DuckLakeArchiveStore` and `InMemoryArchiveStore` both call it, so reads that see the open batch and commit conflicts behave the same in tests and on disk.
- **Adapters** (`adapters/`): the real implementations of the ports, plus the lake connection (`lake.py`), the machine id (`host.py`), the credential readers (`chromium.py`, `leveldb.py`, `snappy.py`), the rate limiter (`rate_limit.py`) and the retry handler that waits out Slack's 429 responses (`retry_after.py`), counting the wait down for the CLI.
- **Config** (`config.py`): the layered settings loader, archive lookup (`find_archive`: `--root`, `SLARC_ARCHIVE`, the nearest `slarc.toml` above the current directory, then `default_archive` in the global config) and `create_archive`, which writes `slarc.toml` when `pull` creates an archive after confirmation. It reads and writes files, so only the CLI calls it.
- **Composition root** (`bootstrap.py`): the only place real adapters are built. `real_services()` returns a `Services` value; `current_services()` returns it unless a test has installed fakes with `override()`. Context managers there open and close the archive stores, the query index and the HTTP client. `query_index` attaches the lake read-only for `search` and `sql`; `shared_index` builds the index on an open store's DuckDB instance, through `shared_connection()`, so `list`, `export` and `rm`, which read through both, attach the lake once. `bootstrap.py` and `cli.py` import adapters and services with `lazy import`, and the adapters import `cryptography`, `keyring` and `slack_sdk` the same way, so a command that reads only the archive never loads them. The CLI's `main` callback puts the services, with the global `--take-ownership` option, in the Typer context, and each command takes them from there.
- **CLI** (`cli.py`): Typer commands. A `SlarcError` becomes one plain sentence and exit code 1, never a traceback. `pull` and `auth` also exit 1 when their report says something failed, such as a workspace whose credentials were rejected. Ctrl-C and SIGTERM print `INTERRUPTED` and exit 130.

**The layering rule.** Domain and service code never import `slack_sdk`, `duckdb`, `httpx`, `keyring` or `fcntl`, and never touch the filesystem or environment. Every write goes through a port (`ArchiveStore`, `BlobStore`, `ExportWriter`). Two modules read immutable package data through `importlib.resources`: `domain/emoji.py` loads `src/slarc/data/emoji.json`, and `service/export.py` loads its Jinja templates and static files under `src/slarc/export/`. Injecting either would thread a fixed table through every caller and needs no fake. `service/listing.py` imports the `Settings` type from `config.py` and calls nothing there.

## Ports

| Port | Real adapter | Test fake (`tests/fakes.py`) |
| --- | --- | --- |
| `SlackGateway` | `SessionTokenGateway` (`adapters/slack_gateway.py`) | `FakeSlackGateway` |
| `SlackConnector` | `SessionConnector` (`adapters/slack_gateway.py`) | `FakeConnector` |
| `CredentialSource` | `EnvSource`, `KeyringSource` (`adapters/credentials.py`), `DesktopSessionSource` (`adapters/desktop_session.py`), in that priority order under `CompositeSource` | `FakeCredentialSource`; `KeyringSource` runs over `FakeKeyring` |
| `ArchiveStore` | `DuckLakeArchiveStore` (`adapters/lake_store.py`) | `InMemoryArchiveStore` |
| `ArchiveLock` | `FlockArchiveLock` (`adapters/locks.py`) | `FakeArchiveLock` |
| `BlobStore` | `FsBlobStore` (`adapters/blob_store.py`) | `InMemoryBlobStore` |
| `Downloader` | `HttpDownloader` (`adapters/downloader.py`) | `FakeDownloader` |
| `ExportWriter` | `FsExportWriter` (`adapters/export_writer.py`) | `InMemoryExportWriter` |
| `QueryIndex` | `DuckDbQueryIndex` (`adapters/duckdb_index.py`) | none: tested through DuckDB over a temporary lake |
| `Clock` | `SystemClock` (`bootstrap.py`) | `FakeClock` |
| `Reporter` | `RichReporter` (`cli.py`) | `RecordingReporter` |

`AssetResolver` (`domain/render/__init__.py`) is a domain-side protocol with the same pattern: `service/export.py` implements it over the archive's blobs, and tests use `FakeAssetResolver`.

`SessionTokenGateway` calls only methods in `READ_METHODS`, sends every call through a per-method token bucket, and maps Slack error codes to `NotVisible`, `ThreadNotFound`, `FileDeleted` and `AuthFailed`. `HttpDownloader` follows redirects itself, sends the token and cookie only to Slack's file host, limits each other host to two downloads at a time, and holds a host that answers HTTP 429 for its `Retry-After`; a download that would wait more than 60 seconds is returned not tried, and keeps its attempts.

## Credentials and auth

`CompositeSource` asks each `CredentialSource` for sessions, in priority order: `EnvSource` (`SLARC_XOXC` and `SLARC_XOXD`), `KeyringSource` (pairs stored by `slarc auth --paste`, keyed by team id), then `DesktopSessionSource`. The desktop source copies the Slack app's `Cookies` database and `Local Storage/leveldb` directory to a temporary directory, decrypts the `d` cookie with the key from the macOS keychain or the Linux secret store (`adapters/chromium.py`), and reads each workspace's `xoxc` token from `localConfig_v2` with slarc's own LevelDB and Snappy readers (`adapters/leveldb.py`, `adapters/snappy.py`). Sessions are read fresh on every run and never written to the archive. `SessionConnector` picks the session for the workspace a link or tracked item names and builds a `SessionTokenGateway` for it; it never calls the API for other workspaces.

`service/auth.py` backs `slarc auth`: it runs `auth.test` for each session found and reports who the user is in each workspace, or why Slack rejected the session. `--paste` checks a pasted token and cookie with `auth.test` before storing them in the keychain.

## The archive

```
<archive>/
  slarc.toml          settings (portable, no absolute paths)
  catalog.sqlite      the DuckLake catalog, plus -wal and -shm files while a command has it open
  data/               Parquet data and delete files, managed by DuckLake
  blobs/sha256/ab/cd/<hash>   downloaded files, avatars, emoji and other images
```

The lake has five tables, created by `lake.py`: `message_version` and `directory_entry` (append-only, one row per observed version of a message or of a user, emoji, conversation, bot, usergroup, file or image), `run` (one row per run that ended), and `tracked_item` and `download_state` (one row per item or download target, replaced in place). The spec lists their columns. Rows are written sorted by `(team, channel, ts)`, or `(team, kind, id)` for directory entries; sorting keeps a conversation's rows together in few files, and partitioning would multiply files and make maintenance and rm slower. Times are ISO 8601 strings, never timestamp columns, because DuckDB returns timestamp-with-zone values that Python cannot read without `pytz`.

`connect_lake` in `lake.py` opens every DuckDB connection to the lake. It loads the extensions, attaches the catalog as `lake` and makes it the default catalog. Each of these settings prevents a known failure:

- **WAL mode on the catalog**, set through `METADATA_PARAMETERS` at every attach. SQLite's default rollback journal stalls writers behind readers for seconds, and an open read transaction blocks writes entirely.
- **The data path is passed at every attach** (`DATA_PATH '<archive>/data/'` with `OVERRIDE_DATA_PATH true`), because the catalog stores an absolute path. A moved or restored archive still opens. Paths are quoted with `_sql_string`, since `ATTACH` takes no parameters.
- **`target_file_size` of 16MB.** With the default, every maintenance rewrites the whole message table.
- **`rewrite_delete_threshold` of 0.** DuckLake otherwise never merges files that have deletes, so tables updated in place fragment, and rm's deletes would never be rewritten out of the files.
- **Maintenance results are read with `fetchall()`.** Reading one with `fetchone()` skips its commit without an error.
- **Bulk inserts pass one JSON parameter**, not a parameterized `VALUES` list, which is about 70 times slower.
- **`duckdb` is pinned exactly** in `pyproject.toml`. The ducklake extension is built for one DuckDB release and the catalog does not migrate itself, so an upgrade is a deliberate change, tested against an archive the previous version created.

The `ducklake` and `sqlite_scanner` extensions are not in the DuckDB wheel. DuckDB downloads them on first use into `$XDG_CACHE_HOME/slarc/duckdb/` (falling back to `~/.cache`), and `just install` fetches them ahead of time. When they are missing and cannot be downloaded, `ExtensionsMissing` names them and says one online run installs them.

A writable connection that finds no catalog builds an empty lake under a temporary name, deleting any that a killed creator left, and renames it into place, so a catalog that exists is complete. Its tables are created in one transaction. It refuses with `CatalogNotSynced`, creating nothing, when `data/` or `blobs/` already holds a file, because a synced folder can deliver those before the catalog. `bootstrap.ensure_lake` runs before any command opens the archive: it checks for the catalog without a lock, and takes the locks only to create one.

## Batches and commits

`DuckLakeArchiveStore` keeps appended rows and staged drops in an open batch in memory. Its reads include the open batch, so pull's duplicate checks see what the run has appended but not committed. A drop removes matching appended rows at once and deletes committed rows when the batch commits. The write lock is never held across a Slack call or a download.

`commit(Commit)` writes the batch, the tracked items and download states the `Commit` names, and deletes its settled download states, in one DuckDB transaction under the write lock. A batch is part of the archive if and only if its transaction committed, so a crash at any moment leaves exactly the last committed state. Inside the transaction the commit checks:

- **Item revisions.** Every `tracked_item` row has an integer revision. A commit that writes an item names the revision it read and sets the next one; if the row has moved on, the commit fails. rm sets an item's status to `removed` and bumps its revision, so a pull that read the item before the rm cannot write it back or write its messages. A second pull of the same item fails the same way instead of writing duplicate versions and older cursors. Revision 0 starts tracking.
- **Complete teams.** rm's commit names every live tracked item in each workspace it touches, so an item a pull started tracking meanwhile makes it fail rather than lose that item's rows.
- **Required directory rows.** A batch records each file and image it assumed was already stored, and the commit checks that each still has a directory row. Otherwise an rm that removed a file a concurrent pull's new message references would leave that message pointing at nothing, and incremental pulls would never re-read it to repair it.
- **Live workspaces.** A batch with directory rows commits only if its workspace still has a tracked item that is not removed, so an rm of a whole workspace cannot be undone by a pull that started before it.
A failed check rolls the transaction back, empties the batch and raises `CommitConflict`.

One more check drops instead of failing. A commit made with `downloads_exist` (each download result) writes a download state, settles it, or writes the file or image rows for its target only if the target's download state still exists, because rm deletes the states of what it removes. The rest of the commit goes through, and the dropped keys come back in `Committed.dropped`. DuckLake has no primary keys or unique constraints; these checks are reads inside the transaction, and they are sound only because the write lock serializes every transaction. `InMemoryArchiveStore` applies the same checks through `archive_state.py`.

Every write appends rows, except that a commit replaces the `tracked_item` and `download_state` rows it updates, deletes settled download states, and deletes the rows its staged drops name (`drop_messages`, `drop_directory_lines`, `drop_workspace`, `drop_downloads`). `append_run` commits only the run row, and raises `ValueError` if the open batch is not empty.

Read and write failures inside the adapters (DuckDB, SQLite or file system errors) become `ArchiveReadFailed` or `ArchiveWriteFailed` through `lake_errors()`. Both are `ArchiveFailed`, which pull and rm re-raise instead of treating as a per-item failure, so a broken archive stops the command with one message.

## Unit of work and the run row

`UnitOfWork` holds the `ArchiveStore`, `BlobStore` and `Clock` for one `pull` or `rm`. Entering it starts a `RunRecord` whose id is `r_<UTC timestamp>_<4 hex digits>`; the suffix tells apart commands started in the same second. `commit(items, unchanged=..., downloads_exist=..., complete_teams=...)` commits the open batch with the items, the download states that changed since the last commit, the keys passed to `settle` and the directory rows passed to `require`. It sends each item with the revision this run last committed for it. On `CommitConflict` it calls `discard()`, which empties the batch and restores the run's download states to the last commit, and raises the conflict again.

Leaving the unit of work, however the run ends (success, `SlarcError`, Ctrl-C, or SIGTERM, which the CLI turns into Ctrl-C), stamps `finished`, writes one run row and runs maintenance, recording in `maintained` whether its deletion step ran (the archive was idle). Before the run row, a run that ended normally commits what is left open and records a `CommitConflict` there in the run's `errors`. A run that raised discards what it left open, keeps what it committed, and raises its error after the run row is written. A run killed outright keeps what it committed and writes no run row. When the block raised, a failure writing the run row or maintaining is dropped in favor of the block's error. When a normally ending run's last commit fails with an error other than `CommitConflict`, maintenance is skipped and that error is raised after the run row, unless writing the run row also fails (see the backlog).

The run row records the command and args, the teams touched, an `auth` entry per workspace (team id, user id, domain, team name, credential source, time of `auth.test`), `errors` for skipped workspaces, and `counts`: `messages_seen`, `lines_appended`, `rate_limit_wait_s` (token bucket waits only), `api_calls` per Web API method, `threads_read` per reason (`link`, `history`, `search`, `active`, `full`, `retry`; each thread once, under its first reason), and rm's deleted counts. `list`, `search`, `sql` and `export` read the archive and write no run row.

## Locks

`FlockArchiveLock` takes `fcntl.flock` locks on two files under `$XDG_CACHE_HOME/slarc/locks/`, named by the sha256 of the archive's real path. They are per machine and never live in the archive.

- **The write lock** (`<hash>.write`) is held exclusively around each transaction and around maintenance. Transactions take milliseconds, so a second pull or an rm waits only that long. Without it, two writers fail at once with "database is locked", and DuckLake's own retries do not queue them.
- **The read lock** (`<hash>.read`) is held shared by every store and index from open to close. Maintenance takes it exclusively, without blocking, to delete bytes. A long-running command, such as the planned `serve`, should hold it per request so it does not keep maintenance from deleting bytes.
- **Order:** a process always takes the read lock before the write lock, so a process holding the read lock exclusively cannot deadlock with one waiting on the write lock.

`DuckLakeArchiveStore` opens a writable connection under `write()`, so every command that opens a store takes the write lock briefly: `pull` and `rm`, and also `list` and `export`, which read tracked items and the directory through the store beside the query index. `search` and `sql` open only the read-only query index. Every lake connection keeps DuckDB's default of one thread per core: on a 1M-message archive, one thread makes search and conversation counts several times slower. The shared hold is counted per process, and flock locks belong to an open file description, so `bootstrap.archive_lock` gives every store and index of one archive the same lock object. A nested `write()` in one process, or holding the read lock shared while this process holds it exclusively, raises `RuntimeError` instead of waiting on the process's own lock for ever.

## Maintenance

`maintain(blobs)` runs at the end of every `pull` and `rm`, after the run row commits.

1. **Compaction, every time,** under the write lock: `ducklake_flush_inlined_data`, `ducklake_merge_adjacent_files` and `ducklake_rewrite_data_files`. These write new files and schedule the old ones for deletion without deleting anything, so readers of older snapshots are unaffected.
2. **Deletion, only when the archive is idle.** The store releases its shared read lock and, if no other store or index of the same process holds it (`held_shared()`), tries the read lock exclusively without blocking. If another process has the archive open, it holds the lock shared again and returns `False`; the bytes wait for a later run that finds the archive idle. With the exclusive lock, under the write lock, it expires every snapshot but the newest by version number, deletes the `ducklake_snapshot_changes` rows DuckLake leaves behind for expired snapshots, deletes every scheduled and orphaned data file (`cleanup_all => true`), deletes every blob that no `directory_entry` row references, and runs SQLite `VACUUM` on the catalog when free pages are over a quarter of its pages.

Deletion uses no time windows. DuckLake's `older_than` windows compare inconsistently across time zones, a short window lets `delete_orphaned_files` remove a concurrent writer's in-flight files, and expiring a snapshot under a reader's open transaction makes the reader's next query return no rows without an error. Holding the read lock exclusively proves no other slarc process reads an older snapshot, has data files in flight or has written a blob it has not committed, which rules out all three.

Because a store defers deletion while another store or index of its own process is open, `rm` closes its query index before its unit of work ends. A DuckDB, SQLite or file system failure during maintenance raises `MaintenanceFailed`. `InMemoryArchiveStore.maintain` deletes unreferenced blobs while its `idle` flag is true.

No pull scans the whole blob store: a pull that reads a message whose blob is missing queues the download again.

## One owning machine

One machine opens an archive at a time. The catalog holds an `archive_owner` table outside the lake's own tables, so compaction never moves it into a data file that may not have synced: a machine id (`adapters/host.py` calls `gethostuuid()`, which returns the IOPlatformUUID, on macOS, or reads `/etc/machine-id` on Linux), the host name for messages, and the time it was recorded. Host names change, so the id is what is compared.

The store and the index check the record on the connection each one opens, through a callback from `bootstrap._owner_check`, once per archive and machine in a process. The store's connection is writable and opened under the write lock, so it records the owner itself when needed. The index's read-only connection reads the record; one naming this machine needs nothing more, so `search` and `sql` on an archive this machine owns never wait for the write lock. One naming another machine raises `OwnedElsewhere` with that host's name. With no record, as on the first open, the index opens a writable connection under the write lock to record this machine. `--take-ownership` records this machine after checking that every file in `ducklake_data_file` and `ducklake_delete_file` exists, raising `SyncNotFinished` and writing nothing if one is missing. That run's store is cautious: its maintenance keeps orphaned files and unreferenced blobs, because files that synced ahead of the catalog look exactly like orphans.

## Reading

`DuckDbQueryIndex` attaches the lake read-only and defines the spec's views in DuckDB's in-memory database, so a view and the lake table it reads (`message_version`, `run`, `tracked_item`) can share a name. `slarc sql` queries the views by default and the tables as `lake.<table>`. The current message is one `QUALIFY` over `message_version`, newest by `(fetched_at, run)`, keeping the newest content of a deleted message. `messages()` and `thread()` read only the conversation's rows, and `thread()` only the rows of messages in the thread. The view definitions live in code; changing one needs no migration, because the tables are the only state.

A read-only connection cannot checkpoint the catalog, so a read command may leave an empty `-wal` file and a small `-shm` file beside it. They are harmless.

## How a pull flows

`pull()` in `service/pull.py`:

1. Resolves selectors to tracked items, or takes every tracked item for a bare pull. `workspaces.Workspaces` connects each workspace once per run (each connection has its own rate limiter), runs `auth.test`, records the `auth` entry, and refuses an env session whose `auth.test` names a different workspace than the link. An item named on the command line is committed before it is read, so an interrupted pull keeps it.
2. For each workspace, reads tracked threads and messages with `conversations.replies`, then refreshes the directory (`directory_refresh.refresh_directory`): `users.list`, `emoji.list`, `usergroups.list`, `bots.info` for bots not yet stored, and `conversations.info` for each conversation involved. Directory rows are appended only when their hash or blob changed. The refresh is its own commit; a conflict there means the workspace was removed meanwhile, so its items are skipped with a warning.
3. For each tracked conversation:
   - Retries: threads whose read failed in an earlier pull (`retry_threads`) are re-read first.
   - History: a first pull pages from `until` down to `since`, committing each page with the item's cursors moved past it, so an interrupted backfill resumes below `oldest_ts`. Later pulls page only newer than `newest_ts`. Every parent with replies in a page has its thread read.
   - Reply discovery: `search.messages` with `in:<#C> after:<date of last_search_at minus two days>`; each match's permalink `thread_ts` names a thread to re-read. A first pull skips this, since its history covered every reply. A failed search warns and leaves `last_search_at` alone, so the next pull retries.
   - Active threads: re-reads every thread with a stored reply newer than `active_thread_days`.
   - `--full` replaces history and search with a re-read of the whole range and every thread in it.
   - Slack-archived skip: an item recorded `archived` whose conversation is still archived is recorded as pulled with no reads, unless `--full` is set or a widened `--since` or `--until` left part of the range unread.
4. Appends each message whose hash differs from its newest stored version, and queues its files, images, custom emoji and avatars as download states.
5. Infers deletes with `threads.infer_deletes`, a pure function, only over a range read in full this run (one thread, or a `--full` range), only for messages at or after the oldest message Slack returned, and never when Slack returned nothing. `NotVisible` and `ThreadNotFound` never produce deletes. On a tracked thread or message, or a conversation that turns `NotVisible`, they skip the item with status `not_visible`, and the warning appears once, when the item turns not visible. A thread under a conversation that returns `ThreadNotFound` is skipped with a warning on each pull that reads it. Any other failed read warns and skips one thread or item: a failed thread under a conversation goes into `retry_threads`, a failed history page leaves the conversation unfinished so the next pull resumes it. A workspace with no session or rejected credentials is skipped with a warning and an `errors` entry; the pull exits non-zero when no workspace was pulled or any credentials were rejected.
6. Commits each tracked item when it finishes or is skipped. A conflict rolls the batch back and the item is skipped with a warning; the next pull resumes from its last commit.
7. With `--retry-skipped`, queues afresh the skipped downloads the pulled items' stored messages reference and commits them (`requeue_skipped`). Then drains downloads (`service/downloads.py`): requeues earlier runs' pending and failed downloads and too-large files that now fit and commits them before any download starts, connects each workspace whose Slack files need its session, fetches in parallel, and reports each result to the downloads bar. Each result commits on its own: a download commits the directory row again with its blob and deletes the target's download state, and any other outcome commits the state's new status. A result whose download state an rm deleted meanwhile is dropped, and its blob is left for maintenance. Then one commit per workspace adds the user and emoji rows whose image was downloaded; a conflict there warns and discards that workspace's rows.
8. Writes `rate_limit_wait_s`, `api_calls` and `threads_read` into the run's counts in a `finally`, so an interrupted run records them too.

## How rm works

`service/remove.py` resolves each selector to tracked items from the archive alone, never calling Slack; a selector that names only removed items finishes an interrupted removal. `plan_removal` computes what removing them deletes: every message version no remaining tracked item covers, the files and images only those messages reference, their download states, and, when nothing in a workspace stays tracked, the workspace's whole directory. The CLI shows the counts and asks first.

On confirmation, `apply_removal` recomputes the plan from fresh reads, stages its drops, and commits the named items as `removed` with a revision check on every tracked item in the affected workspaces (`unchanged` and `complete_teams`). A pull that committed meanwhile makes the commit conflict, and rm recomputes, up to three attempts. Its maintenance then deletes the bytes if the archive is idle; otherwise rm says the space is freed when the running command finishes.

## Rendering and export

The domain renderers (`render/blocks.py`, `render/mrkdwn.py`, `emoji.py`) turn a message body into HTML: `rich_text` and layout blocks first, otherwise the mrkdwn `text`. They escape every Slack value and return `Markup`, so Jinja does not escape them again. Custom emoji resolve through one alias resolver, `custom_emoji_url`, which `service/downloads.py` also uses.

The Jinja partials under `src/slarc/export/templates/` own the message chrome: `_message.html.j2` (avatar, author, time, edit marker, body, previews, files, reactions, thread), `_attachment`, `_file`, `_thread` and `_thread_panel`. The environment autoescapes with `StrictUndefined`.

`service/export.py` builds view models from `QueryIndex.messages` and `QueryIndex.thread` plus a `DirectoryView` per workspace, and writes every file through `ExportWriter`: `index.html`, one page per conversation, `viewer.css`, `viewer.js` and `assets/<sha256>.<ext>`. `--single-file` writes one HTML file with assets as `data:` URIs; avatars and other repeated images become one CSS class per blob instead of one URI per use. Reaction pills show a CSS popup listing who reacted with each variant. A thread's parent links to a side panel opened by CSS `:target`, holding the parent and every reply; it is full screen on narrow screens and inline when printed, and a thread or message export shows its replies inline. `viewer.js` shows times in the viewer's time zone (unless `--tz` was given) and wires up jump to date.

## Testing

- `just test` runs pytest in parallel with pytest-xdist; `just test -n0` runs serially, for a debugger. The tests set `lake.DUCKDB_THREADS` to 1 (`tests/conftest.py`), because DuckDB otherwise starts a thread per core for every lake connection, which costs more than it saves on a small lake. Autouse fixtures give each test a fixed machine id, lock files outside the real cache and an empty owner-check memo.
- Service tests for pull, downloads, rm and the unit of work run against the fakes in `tests/fakes.py` (`tests/service/pull_harness.py` composes them for pull): no network, no disk. `InMemoryArchiveStore.elsewhere()` stands in for another command committing mid-run, which is how conflict tests are written. Listing, search and export tests read a real lake built from the recorded archive, since `QueryIndex` has no fake. `FakeArchiveLock` lets tests build a real `DuckLakeArchiveStore` or `DuckDbQueryIndex` without lock files.
- Contract tests in `tests/contract/` run the same tests against `DuckLakeArchiveStore` and `InMemoryArchiveStore`, and against `FsBlobStore` and `InMemoryBlobStore`. `DuckDbQueryIndex` is tested over a temporary lake built from the recorded archive (`recorded_lake` in `tests/recorded_fixtures.py`, which loads it with `tests/jsonl_loader.py`), with further rows committed through `DuckLakeArchiveStore`.
- CLI tests run the Typer app in-process with `CliRunner` over a real lake in a temporary directory, with Slack, credentials, the clock and the downloader swapped in through `bootstrap.override`.
- Lock and maintenance tests use real processes for the cases that need them: concurrent commits, a writer killed mid-commit, deletion deferred while another process has the archive open.
- `SessionTokenGateway` is tested with a fake Web API client serving recorded responses. The LevelDB reader and cookie decryption are tested against synthetic files from `tests/leveldb_writer.py` and `tests/cookies_writer.py`.
- Recorded fixtures: `tests/fixtures/workspace/` holds the test workspace as JSONL, so it is reviewable in git and can be scrubbed line by line. `just record-fixtures` pulls into a temporary lake, exports it to JSONL with the script's own export, rewrites emails to `@example.invalid` (recomputing hashes) and leaves out blobs over 1 MiB. `test_fixture_archive_has_no_secrets` checks that no fixture holds a Slack token, and `test_fixture_emails_scrubbed` that no real email remains. `tests/fixtures/spike/` holds raw Slack responses.
- Golden files: every recorded message renders to `tests/golden/`; `just test --update-golden` rewrites them after an intended change.
- Live acceptance: `just acceptance` runs `scripts/acceptance.py` against `naleidworkspace`. It is not part of `just check` or CI; run it only when asked or when pull's discovery or delete logic changes. Its writes go through `scripts/seed-test-workspace.py`.

## Known limits

- **One machine at a time.** The locks are per machine, and SQLite in WAL mode needs shared memory on one host. Two machines with an archive open at once produce a conflicted copy of the catalog, and data files that sync ahead of the catalog look like orphans. The owner record makes a hand-off explicit, and the README recommends local disk over a synced folder.
- **Durability is DuckDB's and SQLite's.** slarc does not fsync archive files itself.
- **DuckLake is young** (spec 1.0, April 2026). slarc avoids the time windows and the silent empty read after an expire rather than depending on fixes.
- **Fixed cost per command.** Starting Python and attaching the lake take about 250 ms before a command does any work on a small archive; see the backlog.
