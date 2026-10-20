# The archive as a DuckLake: Parquet data, a SQLite catalog, and commits that are transactions

## Intent

slarc's archive is append-only JSONL folded into current state. Making that safe under crashes, concurrent commands and removals led to `2026-10-20-commit-log-design.md`: batches, commit lines, tags, a committed-only filter on every read, dangling-line collection, sweeps and a lock. Most of that rebuilds what an open table format already provides. Much of the rest exists to keep the archive mergeable in git, and git is no longer a requirement: blobs make archives too large for a repository, and nobody will use one that way.

This design stores the archive as a DuckLake: Parquet data files plus a SQLite catalog, written and read through the `duckdb` package slarc already uses. A commit is a catalog transaction. Tracked items and download states become rows updated in place. Removal is a DELETE followed by standard maintenance that physically removes the bytes. Two flock locks in the XDG cache directory order writers and keep physical deletion away from running commands. The JSONL archive is imported once and then no longer read.

It replaces `2026-10-20-commit-log-design.md`, which is not to be built.

Background, all committed:
- `docs/research/2026-10-20-open-table-formats.md`: DuckLake, Iceberg, Delta and SQLite compared, with citations;
- `docs/research/2026-10-20-ducklake-spike.md`: measurements on a 1M-message synthetic archive, and the failures behind every rule under The lake's settings and Locks;
- `docs/research/2026-10-20-commit-log-spikes.md`: the earlier spikes, including the duplicate-check failure that small commits must avoid.

## Goals

- **Atomic commits.** A batch is part of the archive if and only if its transaction committed. A crash at any moment leaves exactly the last committed state. Spike 3 checked this with 39 `kill -9` tests.
- **Small commits.** A crash loses at most one tracked item's work, one page of a backfill, or one group of download results.
- **Removals win.** A pull that started before an rm can never re-track what the rm removed, or restore its messages, files or images.
- **rm never waits.** It commits the removal at once. The bytes are physically deleted when no other slarc command has the archive open: by rm itself if nothing else is running, otherwise by the next command that finds the archive idle.
- **Repair without judgment.** Files and blobs left by a crash are found and deleted automatically.
- **Smaller and faster.** The spike measured a quarter of the JSONL size, and search and conversation counts 2.7 to 3 times faster before the view rewrite below.
- **Open to object storage later.** Data files can move to S3 with plain writes because the catalog arbitrates commits. A frozen, read-only copy of an archive could be published to object storage for others to query (DuckLake's "Frozen DuckLake" pattern). Nothing is built for either now.
- **Swappable storage.** Service code sees only ports, so a different storage format later means new adapters, not new service code (Ports, below).

## Non-goals

- **No git support.** The archive is no longer meant to be merged or diffed in git.
- **No grepping the archive directly.** The archive is read through slarc or DuckDB. A JSONL export can be added later if wanted.
- **No time travel.** Edit history lives in `message_version` rows, which are kept. Old snapshots are expired at every maintenance.
- **No object-storage backend or publish command now.** The documented frozen pattern attaches a DuckDB-format catalog file, and this design's catalog is SQLite, so publishing would likely copy the catalog tables into a DuckDB file beside the Parquet files. That is unverified and left for later.
- **No change to the blob store** (`blobs/sha256/ab/cd/<hash>`), to downloads, or to export output.
- **No new commands.** Migration happens on its own and maintenance runs inside existing commands.

## Anti-goals (a failure even if it works)

- A tracked item whose cursors claim messages that are not stored.
- A stale pull, or a crash, re-tracking a removed item or re-downloading files rm removed.
- A query that returns wrong or empty results, without an error, because maintenance ran underneath it. Spike 3 reproduced this.
- A crash that leaves state only a person can repair.
- Slower reads. On the 1M-message synthetic archive, search and conversation counts must be at least as fast as spike 3's unchanged-view numbers (1.82 s and 1.38 s, against today's 4.90 s and 4.14 s). `messages` and `thread` must be no more than 25% slower than today (0.64 s and 0.155 s).
- An offline machine that fails with an opaque DuckDB error.

## Terms

`GLOSSARY.md` defines **tracked item**, **run**, **batch** and **commit**. Both batch and commit still apply as defined: a batch's rows reach the archive only through its commit. **Tag** is no longer used. Its job is done by the item revision (below), which is an implementation detail and stays out of the glossary. The builder removes the Tag entry.

This document also uses:
- **the lake:** the catalog plus its data files;
- **maintenance:** the step that compacts files and, when the archive is idle, physically deletes what nothing references.

## Behavior

### Ports

Service code never sees DuckDB, SQL, snapshots, Parquet or file locks. It works through these ports, whose values are today's domain types (`Envelope`, `TrackedItem`, `DownloadState`, `RunRecord`):

- **`ArchiveStore`** (exists).
  - **Reads:** stored messages, directory state and objects, tracked and removed items, and outstanding downloads, as today.
  - **Writes:** `append(env)` adds a row to the open batch, and a new commit operation writes the batch together with the tracked-item and download-state changes it earned. The commit either succeeds or reports which revision check failed (Item revisions, below).
  - **Maintenance** is a store operation too, told whether the archive is idle.
  - The rm plan's queries move behind it as well: which messages a set of tracked items covers, and which files, images and blobs are referenced.
- **`QueryIndex`** (exists): search, messages, thread, conversation counts and `sql`.
- **A lock port** (new): the read lock (shared, or exclusive without blocking) and the write lock.
- **`BlobStore`** (exists, unchanged).

The in-memory fakes implement the same contracts, including the revision checks and reads that see the open batch. The contract tests run against both the fakes and the DuckLake adapters, and that is what keeps a later swap honest.

Two couplings remain, by design:
- `slarc sql` runs DuckDB SQL over the spec's views, so any future backend must present those views to DuckDB. DuckDB reads Parquet, Delta and Iceberg, so this ties the command to DuckDB, not to DuckLake.
- The commit contract assumes one atomic commit across tables: message rows together with the tracked-item update. A backend without multi-table transactions would have to provide that inside its adapter.

### Archive layout

```
<archive>/
  slarc.toml          unchanged
  catalog.sqlite      DuckLake catalog (plus -wal and -shm while open)
  data/               Parquet data and delete files, managed by DuckLake
  blobs/sha256/...    unchanged
```

`raw/`, `directory/` and `runs.jsonl` are read only by migration (below).

### Tables

One DuckLake schema. Every table carries `team` first, as today's keys do.

- **`message_version`**, append-only: team, channel, ts, thread_ts, user, kind (`message` or `deleted`), fetched_at, run, source_team, api, hash, plain, object (the verbatim Slack message as JSON). This holds today's `raw/` lines.
- **`directory_entry`**, append-only: kind, team, id, fetched_at, run, api, hash, blob, object. This holds today's `directory/` lines; users, conversations, emoji, files, images, bots and usergroups share one table.
- **`run`**, append-only: run, command, args, started, finished, teams, auth, counts, errors. This is today's run record without `tracked` and `downloads`.
- **`tracked_item`**, one row per item, updated in place. It has today's `TrackedItem` fields plus `revision` (below). Removed items keep their row with status `removed`, because rm accepts selectors that name already-removed items.
- **`download_state`**, one row per target, updated in place, with today's `DownloadState` fields. A state settles by being deleted, in the same commit that writes the directory row recording its blob. That matches today's "downloaded items drop out" rule. The `removed` status is no longer written: rm deletes the rows instead.

**Times** stay ISO strings (`fetched_at` as today). Spike 3 found TIMESTAMPTZ results raise in Python without pytz, so no column that slarc reads back is a timestamp type.

**Sort order.** Rows are written sorted by (team, channel, ts), or (team, kind, id) for directory entries. Spike 3 found sorting matters more than partitioning: an unsorted load made channel reads twice as slow. The tables are not partitioned, because partitioning by conversation tripled maintenance cost and made rm slower.

**Why newest-wins stays.** Edit history, first and last seen times, and delete markers are part of the archive, so `message_version` keeps every version. The current message is still the newest by (fetched_at, run). What goes away is deduplication: commits are serialized, so the duplicate lines that crashes and union merges produced can no longer occur.

### The lake's settings

Each rule here comes from a failure spike 3 reproduced.

- **WAL mode on the catalog,** set through `METADATA_PARAMETERS` at every attach. The default rollback journal stalls writers behind readers for seconds, and an open read transaction blocks writes entirely.
- **A relative data path.** The catalog stores an absolute data path, so every attach passes `DATA_PATH '<archive>/data/'` with `OVERRIDE_DATA_PATH true`. An archive can then be moved or restored from a backup.
- **`target_file_size` of 16MB.** With the default, every maintenance run rewrites the whole message table.
- **`rewrite_delete_threshold` of 0 on every table.** Files with deletes are never merged, so tables updated in place fragment without it. It also makes rm's deletes physically rewrite the affected files at the next maintenance.
- **Results are read with `fetchall()`.** Reading a maintenance call with `fetchone()` silently skips its commit.
- **Large inserts pass one JSON parameter,** not a parameterized VALUES list. 1,000 rows took 812 ms as VALUES and 11 ms as JSON.
- **The `duckdb` dependency is pinned exactly** (`==`), because the ducklake extension is built for one DuckDB release and the catalog doesn't migrate itself. Upgrading DuckDB is a deliberate change, with a test against an archive created by the previous version.

### Extensions

The `ducklake` and `sqlite_scanner` extensions (27 MB each) are not in the DuckDB wheel. DuckDB downloads them on first use.

- slarc sets `extension_directory` to `$XDG_CACHE_HOME/slarc/duckdb/`, falling back to `~/.cache` as `config.py` does for config.
- If they are missing and can't be downloaded, slarc raises a `SlarcError` that says so. The message names the extensions and says one online run of any slarc command installs them.
- `just install` installs them, so a machine set up while online works offline afterwards.

### Commits

A commit is one DuckDB transaction against the lake, made while holding the write lock (Locks, below). Writes go through the `ArchiveStore` port as today: `append(env)` adds a row to the open batch, and a new commit operation writes the batch atomically with the tracked-item and download-state changes it earned.

**Batch boundaries in pull**, the same as spike 2 found necessary:
- an item named on the command line, committed before it is read, which keeps today's promise that an interrupted pull keeps a newly named item;
- each workspace's directory refresh;
- each tracked item, when it finishes or is skipped (Slack-archived or not visible);
- each history page while an item backfills, with the cursor moved past that page;
- the download drain, one commit per download result, plus one for the final avatar and emoji linking. A result commits its directory row and state change only if the target's download state still exists; otherwise that result is dropped and its blob is left for idle maintenance.

**Batch boundaries in rm:** one commit records the whole removal.

**The run row** is appended in its own commit when the run ends, as the run line is today. A killed run leaves no run row and needs none: whatever it committed stands, and whatever it did not commit was never part of the archive.

**Reads see the open batch.** A read through the store returns committed rows plus the rows the open batch has appended. Today the JSONL store gives pull this by flushing before every read, and pull's duplicate checks depend on it. The write lock is never held across a Slack call or a download, so the open batch lives in memory until its commit. The builder chooses how to merge it into reads; today's per-channel `LatestMessages` cache already does this for messages.

**Unique run ids.** `run_id` has one-second resolution, so two commands started in the same second collide. Run ids gain a short random suffix, for example `r_20261020T140000Z_3f9a`.

### Item revisions: how removals win

Every `tracked_item` row has an integer `revision`. Each commit that changes an item names the revision it read and sets the next one. The commit checks, inside its transaction, that the row still has the revision it read. If the row has moved on, the whole batch is rolled back and the item is reported as skipped. Two cases cause this:
- **An rm removed the item.** rm sets status `removed` and bumps the revision, so a pull that read the item before the rm can't write it back, or write its messages.
- **Another pull advanced the item.** A second pull of the same item would otherwise write duplicate versions and older cursors.

A skipped item is picked up again by the next pull. Pulling a removed item's selector again starts tracking afresh: status `ok`, the next revision, and empty cursors.

The same check covers the other two kinds of commit:
- **A download result** commits only if its `download_state` row still exists. rm deletes the states of what it removes. A pull that downloads a file rm removed meanwhile therefore rolls back. The directory row is never written, and the blob becomes unreferenced and is deleted at the next idle maintenance.
- **A directory refresh** commits only if the workspace still has a tracked item whose status is not `removed`. An rm of a whole workspace therefore can't be undone by a pull that started before it. Items that are `not_visible` still count, so a workspace whose items all turned invisible keeps refreshing.

**Files and images a batch assumed were stored.** Revisions protect the removed item, but a removal can also take something a still-tracked item needs. The scenario:
1. Pull B reads a new message referencing file F.
2. F is already stored, because a message in channel C1 references it, so B's batch holds only the message.
3. rm of C1 commits first. Its plan can't see B's uncommitted message, so it deletes F's directory rows and download state.
4. B's revision check passes, and its message would commit pointing at a file with no metadata and no blob. Incremental pulls don't re-read that message, so nothing would repair it.

So a pull's batch records each file and image it assumed was stored, and its commit checks, inside the transaction, that each still has a directory row. If one doesn't, the commit rolls back like a revision conflict. The rolled-back batch held the cursor moves too, so the next pull reads those messages again, finds the file missing, and stores and queues it as a first sighting.

DuckLake has no primary keys or unique constraints. These checks are reads inside the transaction, and they are safe only because the write lock serializes every transaction.

### Locks

Both locks are `fcntl.flock`, never `lockf`, on files under `$XDG_CACHE_HOME/slarc/locks/`, named by the sha256 of the archive's real path. They are per machine by design and never live in the archive.

- **The write lock** (`<hash>.write`) is held exclusively around each transaction, and around maintenance. Writers block on it. Transactions last milliseconds (spike 3: p50 18 ms, p99 37 ms), so a second pull or an rm waits only that long. Without this lock, two writers fail immediately with "database is locked", and DuckLake's own retries don't queue them.
- **The read lock** (`<hash>.read`) is held shared by every command for as long as it has the archive open. `serve`, which is long-running, holds it per request instead. Physical deletion takes it exclusively, without blocking (Maintenance, below).
- **Order:** read lock before write lock, always. A process doing physical deletion holds the read lock exclusively, so no other process holds the write lock, and the two can't deadlock.

Locking goes through a port, so service code stays free of `fcntl` and filesystem access, and tests use a fake.

### Maintenance

Maintenance runs at the end of every `pull` and `rm`, after the run row commits. It has two parts.

**1. Compaction, every time,** under the write lock:
- flush inlined data to Parquet;
- merge adjacent small files;
- rewrite files that have deletes.

All three write new files and schedule the old ones for deletion without deleting anything. Readers on older snapshots are unaffected.

**2. Physical deletion, only when the archive is idle.** The command releases its shared read lock and tries the read lock exclusively, without blocking. If it gets it, it takes the write lock and then:
- expires every snapshot except the newest, by version number, never by time;
- deletes every file scheduled for deletion, and every orphaned file in the data directory;
- deletes every blob that no `directory_entry` row references;
- runs SQLite `VACUUM` on the catalog when its free pages exceed a threshold the builder picks.

If it doesn't get the lock, another slarc command has the archive open, and the deletion waits for the next maintenance that finds the archive idle.

Why time windows aren't used:
- DuckLake offers `older_than` windows to protect concurrent readers, but spike 3 found them unreliable. They behaved as if comparing calendar days, and `expire_snapshots` depended on the time zone.
- With a 0 s window, `delete_orphaned_files` deleted a concurrent writer's in-flight files, and that writer then committed a broken table.
- Expiring a snapshot under a reader's open transaction made the reader's next query return 0 rows, with no error.

The exclusive read lock rules out all three, so deletion uses no windows at all. Holding it proves no other slarc process has the archive open, so no reader can be using an older snapshot, no writer has files in flight, and no pull has written a blob it hasn't committed yet.

**Missing blobs** keep today's behavior: a pull that reads a message whose file's blob is missing queues the download again. No pull scans the whole blob store.

### One owning host

An archive is opened by one machine at a time, and slarc enforces it.

- **The record.** The catalog holds the owning host: a stable machine id (the IOPlatformUUID on macOS, `/etc/machine-id` on Linux) plus the hostname for messages. Hostnames change, so the id is what is compared. Reading the id is an adapter's job.
- **The first open** records the current host, including the open that creates the lake by migration.
- **Every command,** reads included, checks the record when it opens the archive. Even a read creates WAL files that a synced folder carries to other machines. If the record names another host, the command stops with a message naming that host. The message says to wait until the synced folder has finished syncing, then run the command again with `--take-ownership`.
- **`--take-ownership`** is a global option. It asserts that every change from the previous host has arrived. slarc cannot verify that, so the option is the user's statement. It records the current host, and the run that takes ownership is cautious:
  - it checks that every data file the catalog references exists, and stops with a "the synced folder has not finished syncing" error if any are missing;
  - its maintenance skips the deletion of orphaned files and unreferenced blobs, because files that synced ahead of the catalog look exactly like orphans.

  The next run on the new host behaves normally.
- **The docs** recommend keeping an archive on local disk, outside synced folders, and explain the hand-off for anyone who keeps one in a synced folder anyway.

### rm

From the user's side, rm is unchanged except for the result message:
- the prompt and confirmation are as today;
- the plan shown before the prompt is computed as today: the messages no remaining tracked item covers, the files and images only they reference, and the download states no kept message references. Avatars and custom emoji stay while anything in the workspace is tracked, as today;
- on confirmation, rm recomputes the plan from fresh reads, as today's sweep does, and commits it with a revision check on every tracked item in the affected workspaces: the removed ones and the kept ones. A pull that committed in between changes a revision, so the commit fails and rm recomputes and tries again, up to three times. A pull may have committed more messages for the item while the prompt was open, and those must go too;
- that one commit deletes the `message_version`, `directory_entry` and `download_state` rows in the recomputed plan, and sets each removed item's status to `removed` with its revision bumped;
- if the recomputed counts differ from the ones shown, the result message reports the actual counts;
- rm then runs maintenance.

If the archive was idle, the bytes are gone when rm exits. If not, rm prints, for example:

```
Stopped tracking 3 items. Their 2470 messages, 85 files and 12 images are no longer in the archive; the space is freed when the running slarc command finishes.
```

rm exits 0 in both cases. The builder adapts the existing wording. The "do not run rm while a pull is running" warning in the help and spec is removed.

Physically removing a conversation of 16,746 versions took 0.58 s in spike 3. Afterwards no Parquet file and no catalog row held it.

### Queries and `slarc sql`

`DuckDbQueryIndex` attaches the lake read-only and keeps the public view names and columns in the spec's data model table (`message`, `message_version`, `reaction`, `file`, `message_file`, `image`, `user`, `user_snapshot`, `bot`, `emoji`, `usergroup`, `conversation`, `workspace`, `run`, `tracked_item`, `download`). Users' `slarc sql` queries keep working.

- **Gone:** the `*_line` source views over JSONL, and the dedupe step in `message_line`.
- **`tracked_item` and `download`** are new views, built to the spec's columns over their tables, with no fold. They don't exist in `duckdb_index.py` today.
- **`run`** keeps its `tracked` and `downloads` columns. They are filled for imported runs and empty for new ones, because new runs record that state in the tables instead.
- **`message`'s current version** becomes a single `QUALIFY` over `message_version`, ordered by (fetched_at DESC, run DESC, kind). It must keep today's rules: a deleted message keeps its last content with `deleted_at` set; a parent stored before its first reply becomes a thread parent once replies are stored; `first_seen_at` and `last_seen_at`; reply counts. The existing index contract tests define the behavior. Spike 3 measured search at 0.64 s with a single QUALIFY, before those rules were ported.
- **`messages` and `thread`** filter `message_version` by team and channel, and thread also by `thread_ts`, at the source. This replaces today's per-channel file selection.

The JSONL folds in `archive_state.py` go away. `stored_messages`, `directory_state`, `directory_objects`, `tracked_items`, `removed_items` and `downloads` become queries against the lake.

### Migration

On opening an archive that has `raw/`, `directory/` or `runs.jsonl` but no `catalog.sqlite`, slarc imports it before doing anything else:

1. It takes both locks exclusively, waiting with a message if another slarc command has the archive open.
2. It builds the lake under temporary names (`catalog.sqlite.importing`, `data.importing/`), deleting any left by an earlier interrupted import.
3. It imports:
   - every message and deleted line into `message_version`, dropping exact duplicates of (team, channel, ts, fetched_at, run, kind, hash);
   - every directory line into `directory_entry`;
   - each run line's record into `run`;
   - the current and removed tracked items from today's `runs.jsonl` fold, each at revision 1;
   - the outstanding download states only: today's `outstanding_downloads` filter, which drops targets whose directory line already has a blob, and drops `removed` states. The fold's stale `pending` entries for downloaded targets are not imported.
4. It verifies the import: row counts per table against the lines read, minus the dropped duplicates. The tracked items read back from the lake must equal the JSONL fold, and the download states must equal the JSONL archive's outstanding downloads.
5. It renames the catalog and data directory into place. The catalog rename comes last, so the lake exists only once it is complete.
6. It prints how many rows it imported, and that `raw/`, `directory/` and `runs.jsonl` are no longer used and can be deleted once the user has checked the archive.

The JSONL files are left untouched. Once `catalog.sqlite` exists they are never read again.

Message files are read with today's `*.jsonl` glob, which includes Dropbox conflicted copies of month files. Today's reads already include them, and later pulls may have skipped messages because they matched lines held only in such a copy, so including them keeps the import equal to what slarc reads today. Conflicted copies of `runs.jsonl` are not read, as today.

Importing 1M lines took 4.5 s in spike 3.

**The import stays.** It is the only reader of JSONL left, kept as a permanent "import a JSONL archive" path:
- **Test fixtures:** the recorded fixture archive (`tests/fixtures/workspace/`) stays JSONL, so it stays reviewable in git and `scripts/record-fixtures.py` keeps its line-by-line scrub. Tests load it through the import into a lake in a temporary directory.
- **`scripts/acceptance.py`** reads and edits `raw/*.jsonl` and `runs.jsonl` directly, so it moves to querying the lake.
- **The Phase 1 criterion** "pulling the same channel twice changes no file" becomes: "pulling the same channel twice adds only a run row".

## Known limits

- **One machine at a time.** The locks are per machine, and SQLite in WAL mode needs shared memory on one host. Machines can take turns with an archive only if each waits for a synced folder to finish syncing before opening it. Otherwise:
  - two machines with it open at once produce a conflicted copy of the catalog;
  - data files that sync ahead of the catalog look like orphans and can be deleted by maintenance.

  Others are meant to read a published, frozen copy instead. One owning host, above, makes the hand-off explicit.
- **The Dropbox folder on one machine works.** A test inside `~/Library/CloudStorage/Dropbox` on 2026-10-20 found:
  - a writer making 300 commits while a reader process queried: no errors, and latency the same as on plain APFS (p50 6 ms, p99 10 ms);
  - 8 `kill -9`s of a writer: no partial commits, and `PRAGMA integrity_check` ok after each;
  - no conflicted copies appeared, and the `-wal` and `-shm` files were removed once every process exited.

  Online-only eviction was not tested, because macOS offers no command to evict a Dropbox file. The docs recommend marking an archive folder in Dropbox as available offline.
- **DuckLake is young.** Spec 1.0 shipped on 2026-04-13, and spike 3 found two behaviors worth reporting upstream: the time windows, and silent empty reads after an expire. The design avoids both rather than depending on fixes.
- **Durability is DuckDB's and SQLite's.** slarc no longer fsyncs files itself.

## Constraints

- **Layering.** Domain and service code stay free of `duckdb`, `fcntl` and filesystem access. The lake store, the locks and maintenance are adapters behind ports. The in-memory fakes mirror the new commit operation, the revision check and the open-batch reads. Contract tests run against both the fakes and the real adapters, the real ones on a lake in a temporary directory.
- **Tests first,** as always. These scenarios each get a test:
  - a stale pull's commit after an rm of its item rolls back;
  - two pulls of one item, where the second commit rolls back;
  - a download result for a removed target rolls back, and its blob is deleted at the next idle maintenance;
  - an rm that removes a file a concurrent pull's batch assumed was stored: the pull's commit restores the directory row and queues the download;
  - an rm whose prompt is open while a pull commits more messages for the item: the recomputed plan removes them too;
  - physical deletion is skipped while another process holds the read lock;
  - a command on another host is refused, and a takeover with a missing data file stops before writing;
  - a killed writer leaves the last committed state, and its files are deleted at the next idle maintenance;
  - migration of the fixture archive reproduces today's view results.
- **Docs:**
  - A new ADR supersedes ADR 0001. It records the move from JSONL to DuckLake, why git and direct grepping were given up, and why Iceberg and Delta were not chosen.
  - ADR 0001 is marked superseded.
  - `2026-10-20-commit-log-design.md` gets a note at the top that this design replaces it.
  - The spec's storage, data model and rm sections, `docs/architecture.md`, `GLOSSARY.md` (drop Tag) and the README are updated.
- **Acceptance.** `just acceptance` must pass. This change touches pull's discovery and delete logic.
- **Performance.** The builder re-runs the spike 3 query timings against the finished code on the synthetic archive and records them in the plan's completion notes.

## Decided by Ted

- Git compatibility is dropped, and with it the commit-log design.
- Reading the archive directly with grep or jq is not needed. An export can come later.
- Iceberg is not used. DuckLake with a SQLite catalog is chosen, per the research and spike 3, over Delta's catalog-free commits. One machine writes an archive, and others can read a frozen copy published to object storage later.
- Storage sits behind ports and adapters, so the service code stays agnostic of the format.
- Archives in synced folders: one owning host is enforced, and the docs recommend local disk (One owning host, above).

## My calls (Claude), open to Ted's correction

- Item revisions, checked inside each transaction, replace tags.
- Two locks in the XDG cache directory. The write lock is per transaction, so concurrent pulls and an rm interleave instead of waiting for each other to finish.
- Physical deletion runs only when the archive is idle, so it needs no time windows. Snapshots are expired at every idle maintenance, which ends time travel.
- No partitioning. Rows are sorted by conversation, with 16 MB files.
- Migration is automatic and leaves the JSONL files in place for the user to delete.
- Conflicted copies of month files are imported, because today's reads include them.
- Extensions live in the XDG cache, and an offline failure is a clear error.
- A download state settles by being deleted, as today's states drop out. rm finds states by reference, as today, so no `channel` column is added.
- The run row is written only at the end, as today. A killed run leaves no row.
- `serve` holds the read lock per request.
- The owning host is identified by a stable machine id, and taking ownership is a global `--take-ownership` option.
- rm recomputes its plan on confirmation and commits it with revision checks on every tracked item in the affected workspaces, retrying up to three times. It reports the actual counts if they changed.
- A pull's commit fails if a file or image row it assumed was stored has been deleted by a concurrent rm, and the next pull repairs it.
- The download drain commits once per result.
- The directory-refresh guard is "any item not removed".
- Migration imports only outstanding download states.
- The `run` view keeps empty `tracked` and `downloads` columns for new runs.
- Missing blobs keep today's re-download-on-read behavior.
- The JSONL import stays as a permanent path, and the test fixtures stay JSONL.

## Left to the builder

- The exact shape of the commit operation on `ArchiveStore`, and how the open batch is merged into reads.
- The SQLite VACUUM threshold.
- The DDL, column types and sort mechanism, within the rules above.
- Task order. A reasonable order is:
  - the lake adapter, settings and extensions;
  - commits and revisions;
  - the locks;
  - maintenance;
  - the query views;
  - rm;
  - migration;
  - removing the JSONL store;
  - docs and ADRs.
