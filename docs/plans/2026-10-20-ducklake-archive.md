# DuckLake Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Mark a task's heading `[done]` and tick its steps in the same commit that finishes it.

**Goal:** Store the archive as a DuckLake (Parquet data plus a SQLite catalog), with a commit per batch of work, item revisions so removals win, two flock locks, maintenance that deletes bytes only when the archive is idle, and one owning host.

**Architecture:** Five phases, each leaving `main` working:
1. **Storage swap:** a lake store and lake index behind today's ports, with today's commit-at-end semantics; the bootstrap switch; then the JSONL store is deleted. Task 11b later removes the JSONL import that this phase added.
2. **Commits:** the port gains `commit` and `discard`. `UnitOfWork` commits per batch. Pull, the download drain and rm commit at the design's boundaries, with revision, reference and download checks.
3. **Locks and maintenance:** a write lock around each transaction, a read lock held for a command's life, and maintenance at the end of pull and rm.
4. **Owning host:** a host record in the catalog, plus `--take-ownership`.
5. **Docs, measurements and acceptance.**

Service code never imports `duckdb` or `fcntl`. Every lake detail lives in `src/slarc/adapters/lake.py`, `lake_store.py`, `duckdb_index.py`, `locks.py` and `host.py`.

**Tech Stack:** Python 3.14, uv, DuckDB 1.5.6 with the `ducklake` and `sqlite_scanner` extensions, msgspec, Typer, pytest.

**Spec:** `docs/specs/2026-10-20-ducklake-archive-design.md` (binding). Background: `docs/research/2026-10-20-ducklake-spike.md`, whose failures explain the lake settings. Throwaway spike code for reference, not to copy wholesale: `.llm/spike3-ducklake/lakelib.py` (DDL, attach, JSON-parameter inserts) and `simlib.py`.

## Global Constraints

- **Code style:**
  - Every code file starts with two `# ABOUTME: ` lines.
  - `just check` passes before every commit. Use `just` recipes, never bare tools.
  - Test first: watch each new test fail before making it pass.
  - Comments are evergreen.
- **Layering:** domain and service modules never import `slack_sdk`, `httpx`, `duckdb`, `keyring` or `fcntl`, and never touch the filesystem or environment.
- **Lake rules from spike 3,** each one mandatory:
  - Attach with `METADATA_PARAMETERS MAP {'journal_mode': 'wal'}`.
  - Attach with `DATA_PATH '<archive>/data/'` and `OVERRIDE_DATA_PATH true`.
  - `target_file_size` is `16MB`, and `rewrite_delete_threshold` is `0`.
  - Read every maintenance call's result with `fetchall()`, never `fetchone()`.
  - Pass bulk inserts as one JSON parameter, never as a parameterized VALUES list.
  - No column slarc reads back into Python is TIMESTAMP or TIMESTAMPTZ; times stay ISO strings.
  - `pyproject.toml` pins `duckdb==1.5.6`.
- **Slack:** no task makes live Slack calls except Task 16's `just acceptance`. Only `scripts/seed-test-workspace.py` posts, and only to `naleidworkspace`.
- **Secrets and privacy:**
  - Never print, log or commit a token, cookie or keychain secret.
  - Use only fixture names (`naleidworkspace`, `acme`, `other`, `#all-naleid`) in code, tests and docs.
  - Never read the repo's `archives/` directory.
- **Text:** user-facing messages are one or two plain sentences with no traceback, using glossary terms. No emoji or em dashes anywhere.
- **Docs:** a task that changes behavior the spec, `docs/architecture.md` or the README describes updates that text in the same commit. Task 15 sweeps the rest.

## Review Focus

1. **A pull killed mid-backfill:**
   - every page committed before the kill stays;
   - the page in progress is lost;
   - the next pull resumes from the committed `oldest_ts` with no gap.

   Test: Task 9 `test_interrupted_backfill_keeps_committed_pages_and_resumes`.
2. **rm while a pull of the same item is running:**
   - the pull's later commits for that item roll back;
   - no removed message, file or download state comes back;
   - the pull carries on with its other items.

   Test: Task 9 `test_pull_commit_after_rm_of_its_item_rolls_back`.
3. **Retired:** slarc no longer imports JSONL archives (Task 11b), so there is no first-command import to review.
4. **A first run offline,** with an empty extension directory and no network: the error names the extensions and says how to install them, with no DuckDB traceback. Test: Task 1 `test_missing_extensions_raise_a_clear_error`.
5. **An archive directory that is moved or renamed** still opens with all its data, because the data path is passed at attach, not taken from the catalog. Test: Task 1 `test_moved_archive_still_opens`.

---

## Phase 1: storage swap

### Task 1: Lake connection, schema and extensions [done]

**Files:**
- Create: `src/slarc/adapters/lake.py`
- Modify: `pyproject.toml` (pin `duckdb==1.5.6`), `uv.lock`, `justfile` (`install` also installs the extensions)
- Test: `tests/adapters/test_lake.py`

**Interfaces:**
- **Produces:**
  - `extension_directory(env: Mapping[str, str], home: Path) -> Path`: returns `$XDG_CACHE_HOME/slarc/duckdb`, falling back to `<home>/.cache/slarc/duckdb`.
  - `connect_lake(root: Path, ext_dir: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection`. It attaches `<root>/catalog.sqlite` as `lake` and runs `USE lake`. When not read-only, it creates the catalog, the tables and the options if they are missing.
  - `install_extensions(ext_dir: Path) -> None`.
  - `class ExtensionsMissing(SlarcError)`, with the message: `slarc needs the DuckDB extensions ducklake and sqlite_scanner and could not install them ({reason}). Run any slarc command once while online to install them.`
  - `CATALOG = "catalog.sqlite"` and `DATA_DIR = "data"`.
  - Tables, all in schema `main`, string columns `VARCHAR` and JSON columns `JSON`:
    - `message_version`: team, channel, ts, thread_ts, user, kind, fetched_at, run, source_team, api, slarc, hash, plain, object.
    - `directory_entry`: team, kind, id, fetched_at, run, api, slarc, hash, blob, object.
    - `run`: run, command, started, finished, fetched_at, slarc, object. `object` holds the whole `RunRecord` as JSON, `tracked` and `downloads` included.
    - `tracked_item`: team, kind, channel, ts (empty string for a conversation, as `ItemKey` does), revision INTEGER, plus every other `TrackedItem` field. `last_read` and `retry_threads` are JSON.
    - `download_state`: team, target_kind, target, url, status, attempts INTEGER, last_error.

- [x] **Step 1: Write the failing tests:**
  - `test_connect_creates_catalog_tables_and_settings`: after `connect_lake`, `PRAGMA journal_mode` on `catalog.sqlite` (read with `sqlite3`) is `wal`. `ducklake_options()` shows `target_file_size` 16MB and `rewrite_delete_threshold` 0. The five tables exist.
  - `test_moved_archive_still_opens`: insert a row, close, rename the archive directory, connect at the new path, and read the row back.
  - `test_missing_extensions_raise_a_clear_error`: point `ext_dir` at an empty temp directory and disable autoinstall through a test-only `autoinstall=False` keyword. `connect_lake` raises `ExtensionsMissing`, whose message contains `ducklake` and `Run any slarc command once while online`.
  - `test_read_only_connect_cannot_write`.
  - `test_extension_directory_follows_xdg_cache_home`.
- [x] **Step 2: Run `just test tests/adapters/test_lake.py` and watch them fail.**
- [x] **Step 3: Implement `lake.py`.** `SET extension_directory` comes before `ATTACH`. Turn DuckDB's download and not-found errors on `INSTALL`/`LOAD` into `ExtensionsMissing`.
- [x] **Step 4: Add the extension install to `just install`:** `uv run python -c "from slarc.adapters.lake import install_extensions, extension_directory; import os, pathlib; install_extensions(extension_directory(os.environ, pathlib.Path.home()))"`. Pin duckdb and run `uv lock`.
- [x] **Step 5: Run `just check` and watch it pass.**
- [x] **Step 6: Commit:** `Add the lake connection: SQLite catalog in WAL mode, schema and extensions`.

### Task 2: A lake ArchiveStore with today's semantics [done]

**Files:**
- Create: `src/slarc/adapters/lake_store.py`
- Modify: `tests/contract/test_archive_store.py` (the `store` fixture gains a `lake` parameter)
- Test: `tests/contract/test_archive_store.py`, `tests/adapters/test_lake_store.py` (lake-only behavior)

**Interfaces:**
- **Consumes:** `connect_lake` (Task 1), and `archive_state.LatestMessages`, `directory_state` and `directory_objects` for the overlay.
- **Produces:** `class DuckLakeArchiveStore`, built as `DuckLakeArchiveStore(root: Path, ext_dir: Path)`, implementing today's `ArchiveStore` protocol unchanged, plus `close()`. Its semantics:
  - **The open batch.** `append` adds to an in-memory open batch. Reads return committed rows overlaid with the open batch: `stored_messages`, `directory_state`, `directory_objects`, `channels` and `message_lines`. `flush` is a no-op.
  - **`append_run(run, version)`** commits, in one transaction: the open batch's rows; the run row; an upsert of every `TrackedItem` in `run.tracked`; and an upsert of every `DownloadState` in `run.downloads`. Today's downloaded-targets-drop-out rule holds through `outstanding_downloads`, unchanged.
  - **`tracked_items()`, `removed_items()` and `downloads()`** read the tables, with no fold.
  - **The `drop_*` methods** each run their DELETE in a transaction immediately, as today's rewrites do.
  - **`blob_references()`** reads `directory_entry`.
  - **The current-version rules** (newest by `(fetched_at, run)`, deleted lines, tie-breaks) match `archive_state.LatestMessages` and the existing contract tests exactly.

- [x] **Step 1: Parametrize the `store` fixture over fake, jsonl and lake.** Mark the JSONL-only file-layout tests to skip for the lake. Add lake-only tests in `tests/adapters/test_lake_store.py`:
  - `test_lines_without_a_run_line_are_not_kept`: append, close without `append_run`, reopen, and nothing is there;
  - `test_reads_see_the_open_batch`;
  - `test_append_run_commits_batch_items_and_downloads_together`.
- [x] **Step 2: Run them and watch the lake cases fail.**
- [x] **Step 3: Implement `DuckLakeArchiveStore`.** Insert rows sorted by (team, channel, ts), or by (team, kind, id) for directory rows.
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Add a DuckLake archive store with commit-at-end semantics`.

### Task 3: DuckDbQueryIndex over the lake [done]

**Files:**
- Modify: `src/slarc/adapters/duckdb_index.py`, `tests/contract/test_duckdb_index.py`
- Test: `tests/contract/test_duckdb_index.py`

**Interfaces:**
- **Consumes:** `connect_lake(read_only=True)` (Task 1), and `DuckLakeArchiveStore` (Task 2) to build test data.
- **Produces:** `DuckDbQueryIndex(root: Path, ext_dir: Path)`, which keeps every public view name and column from the spec's data model table:
  - `message`, `message_version`, `reaction`, `file`, `message_file`, `image`, `user`, `user_snapshot`, `bot`, `emoji`, `usergroup`, `conversation`, `workspace` and `run`;
  - the new `tracked_item` and `download` views, over their tables;
  - `run` keeps its `tracked` and `downloads` columns, read from `object`.

  The changes inside it:
  - `SOURCES` and the `*_line` views are replaced by views over the lake tables, shaped as the existing views expect.
  - The dedupe in `message_line` goes.
  - `message_current` becomes one `QUALIFY row_number() OVER (PARTITION BY team, channel, ts ORDER BY fetched_at DESC, run DESC, kind) = 1` over `message_version`, keeping the deleted-content, `first_seen_at`/`last_seen_at` and thread-parent rules.
  - `messages()` and `thread()` filter `message_version` by team and channel at the source, and `thread()` also by `thread_ts`.

- [x] **Step 1: Rebuild the index contract tests' data through `DuckLakeArchiveStore`** (`append` then `append_run`) instead of JSONL files. Add `test_tracked_item_and_download_views` and `test_run_view_keeps_tracked_and_downloads_columns`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement the views,** keeping every existing contract test's expectations.
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Query the lake: views over its tables, one QUALIFY for the current message`.

### Task 4: JSONL import and JSONL export [done]

**Files:**
- Create: `src/slarc/adapters/jsonl_import.py`
- Test: `tests/adapters/test_jsonl_import.py`

**Interfaces:**
- **Consumes:** `connect_lake` and `DuckLakeArchiveStore`; `archive_state.latest_runs` for the tracked-item fold; the existing `JsonlArchiveStore`, used only as the test oracle.
- **Produces:**
  - `has_jsonl(root: Path) -> bool`: true when `raw/`, `directory/` or `runs.jsonl` exists.
  - `import_jsonl(root: Path, ext_dir: Path) -> ImportReport`, with `ImportReport(messages: int, directory: int, runs: int, tracked: int, downloads: int, duplicates: int)`. It follows the spec's Migration steps 2 to 5:
    - it builds under `catalog.sqlite.importing` and `data.importing/`, deleting any left over first;
    - message files are read with the `*.jsonl` glob, conflicted copies included;
    - exact duplicates of (team, channel, ts, fetched_at, run, kind, hash) are dropped;
    - tracked and removed items come from the `runs.jsonl` fold, at revision 1;
    - only outstanding download states are imported (today's `outstanding_downloads` rule, minus `removed`);
    - it verifies counts and the fold, raising `SlarcError` on a mismatch;
    - it renames the data directory, then the catalog, into place.
  - `export_jsonl(root: Path, ext_dir: Path, target: Path) -> None`. It writes today's layout: `raw/<team>/<channel>/<YYYY-MM>.jsonl`, `directory/<team>/<kind file>`, and `runs.jsonl` with one run line per run row. The last run line's `tracked` and `downloads` are replaced with the current tables, so importing the export reproduces the state. `scripts/record-fixtures.py` uses this in Task 5.
  - Neither function holds locks; Task 12 adds them at the call site.

- [x] **Step 1: Write the failing tests:**
  - `test_import_of_recorded_fixture_matches_jsonl_reads`: copy `tests/fixtures/workspace`, import it, and compare the lake store with `JsonlArchiveStore` on the copy. The comparison covers `tracked_items`, `removed_items`, `stored_messages` and `directory_objects` per channel and kind, and the lake's `downloads()` against `outstanding_downloads`.
  - `test_import_drops_exact_duplicates_and_counts_them`.
  - `test_import_reads_conflicted_month_copies`.
  - `test_interrupted_import_is_redone`: leave a `catalog.sqlite.importing` and `data.importing/`, run the import, and both are replaced.
  - `test_jsonl_files_are_untouched`, checked by hashing them before and after.
  - `test_export_then_import_round_trips`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement both functions.**
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Import JSONL archives into the lake, and export a lake back to JSONL`.

### Task 5: Switch to the lake [done]

**Files:**
- Modify:
  - `src/slarc/bootstrap.py` (`archive_stores` and `query_index` open the lake, importing first when `has_jsonl` and no catalog);
  - `src/slarc/cli.py` (prints the import report once);
  - `tests/recorded_fixtures.py` (new `recorded_lake(target: Path) -> Path`, which copies and imports);
  - the tests that used `copy_recorded` with `DuckDbQueryIndex` or `JsonlArchiveStore` (`tests/service/test_listing.py`, `test_search.py`, `test_export.py`, the CLI tests);
  - `scripts/acceptance.py`;
  - `scripts/record-fixtures.py`.
- Test: `tests/test_cli.py` (or the CLI test module that owns archive opening)

**Interfaces:**
- **Consumes:** Tasks 1 to 4.
- **Produces:**
  - `bootstrap.archive_stores(root)` yields `(DuckLakeArchiveStore, FsBlobStore)`, and `bootstrap.query_index(root)` yields the lake index. Both get the extension directory from `extension_directory(os.environ, Path.home())`.
  - **Import message,** printed once by the command that triggers it: `Imported this archive into the new storage format: {messages} message versions, {directory} directory entries, {runs} runs and {tracked} tracked items. raw/, directory/ and runs.jsonl are no longer used and can be deleted once you have checked the archive.`
  - **`scripts/acceptance.py`:**
    - it reads and edits the lake through DuckDB: `forget_reply` deletes R's `message_version` rows and updates `tracked_item.last_search_at`;
    - criterion (a) becomes `"(a) pulling the same channels twice adds no message or directory rows"`, checked by comparing row counts of `message_version` and `directory_entry`.
  - **`scripts/record-fixtures.py`** pulls into a temp lake, then runs `export_jsonl` into a temp directory before its existing scrub and copy. The fixture archive stays JSONL.

- [x] **Step 1: Write the failing tests:**
  - `test_first_command_imports_once`: a JSONL archive is imported by `slarc list` with the message printed, and a second `slarc list` prints no import message.
  - `test_new_archive_creates_a_lake`: `slarc pull --yes` in an empty directory creates `catalog.sqlite`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement the bootstrap switch, the fixture helper and the test updates.** Update both scripts. Neither runs in `just check`, so `just acceptance` is run in Task 16.
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Open archives as DuckLakes, importing JSONL archives on first use`.

### Task 6: Delete the JSONL store [done]

**Files:**
- Delete: `src/slarc/adapters/jsonl_store.py`
- Modify:
  - `tests/contract/test_archive_store.py`: drop the `jsonl` parameter and the file-layout tests;
  - `tests/adapters/test_jsonl_import.py`: the oracle becomes reading the fixture JSONL through `archive_state` folds directly;
  - `src/slarc/archive_state.py`: keep only what the import and the overlay use;
  - `docs/architecture.md`.

- [x] **Step 1: Delete the store and its tests.** Rewrite the import oracle on `archive_state`, and delete any `archive_state` function nothing calls.
- [x] **Step 2: Run `just check` and watch it pass.**
- [ ] **Step 3: Commit:** `Remove the JSONL archive store`.

## Phase 2: commits

### Task 7: The commit port, revisions and run ids [done]

**Files:**
- Modify: `src/slarc/domain/tracking.py`, `src/slarc/domain/envelope.py`, `src/slarc/ports.py`, `src/slarc/adapters/lake_store.py`, `tests/fakes.py`
- Test: `tests/contract/test_archive_store.py`, `tests/domain/test_envelope.py` (or the module that tests `run_id`)

**Interfaces:**
- **Produces:**
  - `TrackedItem.revision: int = 0`.
  - `run_id(now: datetime, suffix: str = "") -> str` appends `_<suffix>` when the suffix is not empty.
  - In `ports.py`:
    - `type DownloadKey = tuple[str, str]`.
    - `type DirectoryRef = tuple[str, Kind, str]`, meaning (team, kind, id).
    - `class CommitConflict(SlarcError)`, with the message `Another slarc command changed the archive at the same time.`
    - `@dataclass(frozen=True) class Commit` with fields `items`, `unchanged`, `downloads`, `settled`, `requires` (each defaulting empty) and `downloads_exist: bool = False`.
    - `@dataclass(frozen=True) class Committed` with fields `revisions: dict[ItemKey, int]` and `dropped: set[DownloadKey]`.
  - **`ArchiveStore` changes:**
    - `flush` is removed.
    - `commit(change: Commit) -> Committed` and `discard() -> None` are added.
    - `drop_messages`, `drop_directory_lines` and `drop_workspace` now stage into the open batch.
    - `drop_downloads(team: str, targets: set[str]) -> None` is added and also stages.
    - `append_run(run, version)` keeps Task 2's behavior for now: it commits the open batch, the run row, and upserts of `run.tracked` and `run.downloads`. That keeps pull and rm working until Tasks 9 to 11 move them to `commit`, and Task 11 reduces it to the run row alone.
  - **`commit` rules,** all checked in one transaction. Any failure rolls back everything, raises `CommitConflict` and clears the open batch, except the `downloads_exist` case.
    - **An item with revision 0** starts tracking. It succeeds if no row exists or the row is `removed`, and writes revision `row.revision + 1`, or 1 with no row. A live row is a conflict.
    - **An item with revision n > 0** must match the row's revision, and writes n + 1.
    - **An `unchanged` item** must match the row's revision. Nothing is written for it.
    - **Every `requires` ref** must have a `directory_entry` row.
    - **A team with directory rows in the open batch** must have a tracked item that is not `removed`, counting this commit's items.
    - **With `downloads_exist`,** a download state or settled key with no row is dropped, along with any open-batch directory row for the same (team, target). The key is reported in `Committed.dropped`, and the rest of the commit goes ahead.
    - **On success:** the open batch's rows are inserted, staged drops applied, items upserted, `downloads` upserted and `settled` rows deleted.
  - The fake implements the same rules over in-memory tables.

- [x] **Step 1: Write the failing contract tests,** over fake and lake:
  - `test_commit_bumps_revision`;
  - `test_stale_revision_conflicts_and_clears_the_batch`;
  - `test_revision_zero_retracks_a_removed_item`;
  - `test_revision_zero_conflicts_with_a_live_item`;
  - `test_unchanged_item_conflict`;
  - `test_missing_required_directory_row_conflicts`;
  - `test_directory_rows_need_a_live_item_in_their_team`;
  - `test_downloads_exist_drops_missing_keys_and_their_directory_rows`;
  - `test_drops_wait_for_commit`;
  - `test_discard_empties_the_batch`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement the domain, port, fake and lake store changes.**
  - Remove every `flush()` call in the services; they're no-ops since Task 2.
  - `remove.sweep` ends with `archive.commit(Commit())`, so its staged drops apply as they do today.
  - Nothing else in the services changes yet.
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Add commits with item revisions to the archive store`.

### Task 8: UnitOfWork commits [done]

**Files:**
- Modify: `src/slarc/unit_of_work.py`, `src/slarc/bootstrap.py` (passes a random run-id suffix)
- Test: `tests/test_unit_of_work.py`

**Interfaces:**
- **Consumes:** Task 7.
- **Produces:**
  - `UnitOfWork(archive, blobs, clock, command, args, slarc_version, run_suffix: str = "")`.
  - `commit(items: Sequence[TrackedItem] = (), *, unchanged: Sequence[TrackedItem] = (), downloads_exist: bool = False) -> None`. It does four things:
    - it replaces each item's revision with the last one this run committed for that key, if any;
    - it sends the download states in `run.downloads` that differ from the last committed snapshot, the keys passed to `settle` since then, and the refs passed to `require`;
    - on success it records the new revisions, takes a new snapshot, removes `Committed.dropped` keys from `run.downloads`, and clears the settled keys and refs;
    - on `CommitConflict` it calls `discard()` and re-raises.
  - `discard() -> None`: calls `archive.discard()`, restores `run.downloads` from the snapshot, and clears the settled keys and refs.
  - `require(ref: DirectoryRef) -> None` and `settle(key: DownloadKey) -> None`.
  - `__exit__`: when the block raised, it calls `discard()`. Otherwise it calls `commit()` for anything left open, and on `CommitConflict` it records the error in `run.errors` and discards. Then it calls `append_run`.
  - The bootstrap passes `secrets.token_hex(2)`, and tests keep the default `""`.

- [x] **Step 1: Write the failing tests:**
  - `test_commit_threads_revisions_through_stale_copies`: committing the same `TrackedItem` object twice succeeds;
  - `test_commit_sends_changed_download_states_only`;
  - `test_conflict_restores_downloads_and_reraises`;
  - `test_exit_after_an_exception_discards_the_open_batch_then_writes_the_run_row`;
  - `test_exit_commits_what_is_left_open`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Commit batches through the unit of work`.

### Task 9: Pull commits per batch [done]

**Files:**
- Modify: `src/slarc/service/pull.py`, `src/slarc/service/appends.py`, `src/slarc/service/downloads.py` (`queue_downloads` only), `src/slarc/service/held_conversation.py` (only if `flush` callers remain)
- Test: `tests/service/test_pull.py` (or the existing pull service test module)

**Interfaces:**
- **Consumes:** `UnitOfWork.commit`, `discard` and `require` (Task 8).
- **Produces these behaviors:**
  - **`_PullRun._record(item, held)`** calls `self.uow.commit([item])` after setting `retry_threads`. `uow.run.tracked` is no longer written.
  - **After `refresh_directory`,** `pull_workspace` calls `self.uow.commit()`. A `CommitConflict` there skips the workspace with the warning `Workspace {team_id} was removed while this pull was running, so its tracked items were not pulled.`
  - **A conversation's failure path,** the `except SlarcError` branch in `_pull_conversation`, calls `self.uow.discard()` before warning. A `CommitConflict` takes the same path, so the warning ends with its message.
  - **A thread item** whose append or finish raises `CommitConflict` is skipped with the same warning shape: `Could not finish pulling {label}; the next pull resumes it: {error}`.
  - **A `CommitConflict` on a command-line item's first record** in `resolve` drops that item with a warning.
  - **`append_directory(..., keep_blob=True)`** calls `uow.require((team_id, kind, id_))` for each object it skips as unchanged. When `queue_downloads` treats a file or image as downloaded, it calls `uow.require((team_id, target_kind, target))`.
  - **Every `uow.archive.flush()` call is removed.**

- [x] **Step 1: Write the failing tests,** using the fake store and a `FakeSlackGateway` hook that runs a callback before a given history page. Add that small optional hook to the fake.
  - `test_interrupted_backfill_keeps_committed_pages_and_resumes`: raise `KeyboardInterrupt` on page 3. Pages 1 and 2 are stored, `oldest_ts` is page 2's, and a second pull fetches from there.
  - `test_pull_commit_after_rm_of_its_item_rolls_back`: the callback commits the item as `removed` with its revision bumped. No messages from later pages are stored, the item stays removed, and other items finish.
  - `test_second_pull_of_one_item_rolls_back`: the callback commits a newer revision.
  - `test_missing_required_file_rolls_back_and_next_pull_restores_it`: the callback drops file F's directory row. The batch rolls back, and the next pull stores F and queues its download.
  - `test_directory_refresh_after_workspace_removal_skips_the_workspace`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Commit pull work per item and per page, rolling back stale batches`.

### Task 10: The download drain commits per result [done]

**Files:**
- Modify: `src/slarc/service/downloads.py`
- Test: `tests/service/test_downloads.py` (or the module that tests the drain)

**Interfaces:**
- **Consumes:** `UnitOfWork.commit(downloads_exist=True)` and `settle` (Task 8).
- **Produces these behaviors:**
  - **After each `_Recorder.record(result)`,** `drain_downloads` calls `uow.commit(downloads_exist=True)`.
  - **`_Recorder._replace(job, None)`** calls `uow.settle(key)`.
  - **`_Recorder.finish()`** ends with `uow.commit()`.
  - **A dropped result** is not counted in `files_saved` or `images_saved`. `_Recorder.record` learns of the drop because the key leaves `run.downloads`.
  - **The `removed` status** is no longer read: delete the `prior.status == "removed"` branches in `_file_state` and `_image_state`, and remove `"removed"` from `DownloadState.status`.

- [x] **Step 1: Write the failing tests:**
  - `test_drain_commits_each_result`: interrupt after the second result, and the first two are recorded.
  - `test_result_for_a_removed_target_is_dropped`: drop the target's state row before its result commits. No directory row is written, and it's not counted as saved.
  - `test_downloaded_state_row_is_deleted`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Commit each download result, dropping results for removed targets`.

### Task 11: rm commits with revision checks [done]

**Files:**
- Modify: `src/slarc/service/remove.py`, `src/slarc/cli.py` (`rm_command`), `src/slarc/adapters/lake_store.py` and `tests/fakes.py` (`append_run`)
- Test: `tests/service/test_remove.py`, `tests/test_cli_rm.py`, `tests/contract/test_archive_store.py`

**Interfaces:**
- **Consumes:** `UnitOfWork.commit(items, unchanged=...)` and the staged `drop_*` methods (Task 7).
- **Produces:**
  - **`apply_removal(removal: Removal, uow: UnitOfWork, blobs: BlobStore) -> Removal`** returns the plan it actually committed:
    - it recomputes the deletion from fresh reads, with today's `_deletion`;
    - it stages the drops, including `drop_downloads` for the plan's download keys;
    - it calls `uow.commit(removed_items, unchanged=kept_items_in_those_teams)`;
    - on `CommitConflict` it retries up to 3 attempts in total, then raises `SlarcError("Other slarc commands kept changing these items; run rm again.")`.
  - `Removal.downloads` becomes `list[DownloadKey]`, and rm no longer writes `removed` download states.
  - **Blobs:** `sweep` keeps only the blob deletion, called after the commit. Task 13 replaces it with maintenance.
  - **The result message** uses the counts `apply_removal` returns.
  - **`append_run`** now commits only the run row, with `tracked` and `downloads` emptied, and raises `ValueError` if the open batch is not empty. Nothing fills those lists any more, apart from `run.downloads` as the drain's working list.

- [x] **Step 1: Write the failing tests:**
  - `test_rm_after_prompt_includes_messages_committed_meanwhile`: plan, commit new messages for the item, then apply. The new messages are deleted, and the printed counts include them.
  - `test_rm_retries_then_gives_up` uses a fake store that always conflicts.
  - `test_append_run_writes_only_the_run_row`, as a contract test over fake and lake.
  - Keep every existing rm test passing.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Commit removals with revision checks, recomputing when a pull got there first`.

### Task 11b: Remove the JSONL import from slarc [done]

Ted decided (2026-10-20) that slarc will not import JSONL archives. slarc has only run on his machine, was never released, and he has deleted his JSONL archives. slarc itself must have no JSONL code before a release. The recorded test fixture stays JSONL, because it is real Slack output from the test workspace that is reviewed in git and scrubbed line by line by `scripts/record-fixtures.py`.

**Files:**
- Delete: `src/slarc/adapters/jsonl_import.py`, `tests/adapters/test_jsonl_import.py`
- Modify:
  - `src/slarc/bootstrap.py`: `ensure_lake` no longer checks for JSONL or imports; it only creates an empty lake when there is no catalog. Rename it if its name no longer fits.
  - `src/slarc/cli.py`: the import message and its printing go.
  - `src/slarc/archive_state.py`: `latest_runs`, `LatestRuns`, `REMOVED_DOWNLOAD` and any other run-line fold code go from the product if only the import and the fixture loader use them.
  - `tests/recorded_fixtures.py`: `recorded_lake(target: Path) -> Path` keeps its signature and its once-per-session template copy, but builds the template with a test-only loader instead of `import_jsonl`.
  - `tests/contract/test_duckdb_index.py`: the two direct `import_jsonl` calls (about lines 226 and 236) use the test loader or are rewritten to build their data through `DuckLakeArchiveStore`.
  - `scripts/record-fixtures.py`: keeps a lake-to-JSONL export of its own (the code from `export_jsonl`, trimmed to what the script needs), so it can still pull into a temp lake, export to JSONL and run its scrub. It does not run in `just check` and makes live Slack calls; do not run it.
  - Docs: `docs/architecture.md` (lines about the JSONL import and export, and line 56 on archives written before the lake); `docs/specs/2026-10-20-ducklake-archive-design.md` (replace the Migration section with a short note that archives written before the lake are not imported, and fix the sentences in Intent, Goals, Archive layout, Queries and "Decided by Ted" that mention the import, recording the decision under "Decided by Ted"); `docs/plans/2026-10-20-ducklake-archive.md` (Review Focus item 3 is retired with a one-line note; Task 12 drops the import-lock bullet and the `jsonl_import.py` file entry; Task 15 drops the README "one-time import" item; add this task as "Task 11b" after Task 11, marked `[done]`).

**The test-only loader** (in `tests/recorded_fixtures.py` or a small sibling module under `tests/`):
- reads `raw/*/*/*.jsonl`, `directory/*/*` and `runs.jsonl` of a JSONL archive;
- appends every message, deleted and directory line through `DuckLakeArchiveStore.append`;
- commits the tracked and removed items from the last state the run lines describe, and the outstanding download states, through the store's `commit`;
- writes each run line as a run row (the store's `append_run`, or the row helper it uses), keeping the fixture's run ids and their `tracked`/`downloads` lists in the run objects only if a test reads them;
- needs no temporary names, no verification against a fold, and no duplicate counting. Keep it short; it is test code. If the run-line fold it needs is small, it lives in the test module, not in `archive_state.py`.

**Tests:** every test that used `recorded_lake` or `import_jsonl` passes with the loader. Add one test that the loader reproduces the fixture: the lake built from it has the fixture's message count per channel, its tracked items, and its outstanding download states (compare with simple counts and keys read straight from the fixture files).

- [x] **Step 1:** Write the loader test and watch it fail (the loader does not exist).
- [x] **Step 2:** Write the loader; switch `recorded_lake` and the two `import_jsonl` call sites to it.
- [x] **Step 3:** Delete `jsonl_import.py` and its tests; remove the import from `bootstrap.py` and `cli.py`; move the export into `scripts/record-fixtures.py`; prune `archive_state.py`.
- [x] **Step 4:** Update the docs listed above.
- [x] **Step 5:** `grep -rn -i jsonl src/` finds nothing. `just check` passes.
- [x] **Step 6:** Commit: `Remove the JSONL import; build test lakes from the JSONL fixture in the tests`.

## Phase 3: locks and maintenance

### Task 12: Locks [done]

**Files:**
- Create: `src/slarc/adapters/locks.py`
- Modify: `src/slarc/ports.py` (`ArchiveLock` protocol), `src/slarc/adapters/lake_store.py`, `src/slarc/adapters/duckdb_index.py`, `src/slarc/bootstrap.py`, `tests/fakes.py`
- Test: `tests/adapters/test_locks.py`

**Interfaces:**
- **Produces:**
  - `class ArchiveLock(Protocol)` with these methods:
    - `hold_shared() -> None` and `release_shared() -> None`, for the read lock, counted per process;
    - `try_exclusive() -> bool` and `release_exclusive() -> None`;
    - `write() -> AbstractContextManager[None]`, for the blocking write lock.
  - `FlockArchiveLock(root: Path, cache_dir: Path)`, with lock files `<cache_dir>/slarc/locks/<sha256 of root.resolve()>.read` and `.write`. It uses `fcntl.flock`, never `lockf`.
  - `archive_lock(root: Path) -> FlockArchiveLock` returns one object per archive per process. flock locks belong to an open file description, so two descriptors in one process would block each other.
  - The fake: `FakeArchiveLock`.
  - **Wiring:**
    - `DuckLakeArchiveStore` and `DuckDbQueryIndex` take a `lock: ArchiveLock`;
    - each holds the shared read lock from open to close;
    - `commit`, `append_run` and the staged drops run inside `lock.write()`;
    - lock order is read lock before write lock.

- [x] **Step 1: Write the failing tests,** using `subprocess` with the repo venv's python for the cross-process cases:
  - `test_two_processes_commit_concurrently_without_errors`: two processes make 50 commits each, and every commit lands.
  - `test_try_exclusive_fails_while_another_process_holds_shared`.
  - `test_one_lock_object_per_archive_per_process`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Add read and write locks in the XDG cache directory`.

### Task 13: Maintenance [done]

**Files:**
- Modify: `src/slarc/ports.py` (`ArchiveStore.maintain`), `src/slarc/adapters/lake_store.py`, `tests/fakes.py`, `src/slarc/unit_of_work.py`, `src/slarc/service/remove.py` (delete `sweep`), `src/slarc/cli.py` (rm result message, help text)
- Test: `tests/adapters/test_lake_maintenance.py`, `tests/test_cli_rm.py`

**Interfaces:**
- **Produces:**
  - **`ArchiveStore.maintain(blobs: BlobStore) -> bool`** returns whether physical deletion ran.
    - **Compaction, always,** inside `lock.write()`: `ducklake_flush_inlined_data`, `ducklake_merge_adjacent_files` and `ducklake_rewrite_data_files`.
    - **Physical deletion,** only if `release_shared()` then `try_exclusive()` succeeds. Inside `lock.write()`:
      - `ducklake_expire_snapshots`, by version list, keeping only the newest;
      - `ducklake_cleanup_old_files` with `cleanup_all => true`, and `ducklake_delete_orphaned_files` with `cleanup_all => true`;
      - every blob in `blobs.ids()` that no `directory_entry` row references is deleted;
      - a SQLite `VACUUM` of the catalog when `freelist_count` exceeds a quarter of `page_count`.

      Then it releases the exclusive lock and holds the shared lock again. Verify each DuckLake function's exact name and parameters against the installed extension. If `cleanup_all` doesn't exist, pass `older_than => now() + INTERVAL 1 DAY`, which is safe under the exclusive lock.
  - **`UnitOfWork.maintained: bool | None`,** set in `__exit__` by calling `archive.maintain(self.blobs)` after `append_run`.
  - **The rm result message.** When maintenance did not delete, the message is: `Stopped tracking {n} items. Their {lines} messages, {files} files and {images} images are no longer in the archive; the space is freed when the running slarc command finishes.` When it did, keep today's wording.
  - The rm help text loses `Do not run while a pull is running on the same archive.`
  - `sweep` and `RUN_RM_AGAIN`'s sweep path are deleted.

- [x] **Step 1: Write the failing tests:**
  - `test_rm_physically_removes_a_conversation_when_idle`: after rm, scan every Parquet file under `data/` with `read_parquet` and every catalog table. None holds the channel, and the blob is gone.
  - `test_deletion_waits_while_another_process_holds_the_archive`: a subprocess holds the shared lock. rm prints the deferred message, the bytes remain, and a later pull deletes them.
  - `test_killed_writer_leaves_last_commit_and_orphans_are_removed`: kill a subprocess writer with SIGKILL mid-insert. The state equals the last commit, and the next idle maintenance removes the leftover files.
  - `test_catalog_and_file_count_stay_bounded`: 30 small commits with maintenance after each keep the file count under 10, and the catalog's size after commit 30 equals its size after commit 10.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Compact after every run and delete bytes only when the archive is idle`.

## Phase 4: one owning host

### Task 14: Owning host and --take-ownership [done]

**Files:**
- Create: `src/slarc/adapters/host.py`
- Modify: `src/slarc/adapters/lake.py` (an `archive_owner` table with `machine_id`, `host_name` and `since`), `src/slarc/adapters/lake_store.py`, `src/slarc/adapters/duckdb_index.py`, `src/slarc/bootstrap.py`, `src/slarc/cli.py` (a global option on `main`)
- Test: `tests/adapters/test_host.py`, `tests/adapters/test_lake_owner.py`, `tests/test_cli.py`

**Interfaces:**
- **Produces:**
  - `machine_id() -> str` reads the IOPlatformUUID through `ioreg -rd1 -c IOPlatformExpertDevice` on macOS, or `/etc/machine-id` on Linux. `host_name() -> str`.
  - **`check_owner(con, machine: str, host: str, take_ownership: bool) -> bool`** returns whether this run is cautious:
    - with no row, it records this host;
    - with a row naming this machine, it does nothing;
    - with a row naming another machine and no `take_ownership`, it raises `OwnedElsewhere(SlarcError)`: `This archive is owned by {host_name}. Wait until your synced folder has finished syncing, then run the command again with --take-ownership.`;
    - with `take_ownership`, it checks that every path in `ducklake_data_file` and `ducklake_delete_file` exists, raising `SyncNotFinished(SlarcError)` if not: `Some of this archive's files have not arrived yet; wait until your synced folder has finished syncing.` Then it records this host and returns True.
  - **A cautious store's `maintain`** skips orphan-file and blob deletion.
  - **The global option:** `slarc --take-ownership <command>`, help text `Take this archive over from the machine that last used it, once your synced folder has finished syncing.` It reaches the bootstrap through `Services`.

- [x] **Step 1: Write the failing tests,** with an injectable machine id:
  - `test_first_open_records_the_host`;
  - `test_other_host_is_refused_with_its_name`;
  - `test_take_ownership_with_a_missing_data_file_stops`;
  - `test_takeover_run_skips_orphan_and_blob_deletion`;
  - `test_cli_take_ownership_option`.
- [x] **Step 2: Run them and watch them fail.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Run `just check` and watch it pass.**
- [x] **Step 5: Commit:** `Record the archive's owning host and add --take-ownership`.

## Phase 5: docs, measurements, acceptance

### Task 15: Docs and ADRs

**Files:**
- Create: `docs/adr/0004-ducklake-archive.md`
- Modify: `docs/adr/0001-append-only-jsonl-storage.md` (status superseded by 0004), `docs/specs/detailed-requirements.md`, `docs/architecture.md`, `GLOSSARY.md`, `README.md`

- [ ] **Step 1: Write ADR 0004 in the existing ADR format.** It records the move to DuckLake with a SQLite catalog, why git and direct grepping were given up, why Iceberg and Delta were not chosen (citing the research doc), and the one-owning-host rule.
- [ ] **Step 2: Update the spec:**
  - the storage section;
  - the data model table: sources become tables, `tracked_item` and `download` have no fold, and `run` keeps its columns;
  - the rm section, without the do-not-run-during-pull warning and with the deferred message;
  - the Phase 1 ship criterion, now "pulling the same channel twice adds no message or directory rows";
  - the versioning paragraph.
- [ ] **Step 3: Update `docs/architecture.md`:** the ports, the adapters, the locks and maintenance. Add a note that `serve`, when built, holds the read lock per request.
- [ ] **Step 4: Remove the Tag entry from `GLOSSARY.md`.**
- [ ] **Step 5: Update the README:**
  - the archive layout;
  - keeping an archive on local disk;
  - marking a Dropbox archive folder available offline;
  - the `--take-ownership` hand-off.
- [ ] **Step 6: Run `just check` and watch it pass.**
- [ ] **Step 7: Commit:** `Document the DuckLake archive: ADR 0004, spec, architecture, README`.

### Task 16: Measurements and acceptance

**Files:**
- Modify: this plan's completion notes (below)

- [ ] **Step 1: Measure the queries.** Regenerate the synthetic JSONL archive with `.llm/spike3-ducklake/gen_synthetic.py`, adapted to write into a scratch directory, and build the synthetic lake from the generated JSONL with `load_jsonl` from `tests/jsonl_loader.py`. Time `search`, `conversation_counts`, `messages` (largest channel) and `thread` through `DuckDbQueryIndex`, with the median of 5 and 4 threads. The spec's budget: search at most 1.82 s and counts at most 1.38 s; messages at most 0.80 s and thread at most 0.194 s (today's 0.64 s and 0.155 s plus 25%). Record the numbers below, and delete the synthetic data.
- [ ] **Step 2: Run `just acceptance`** and record its result below. A failure is fixed before the plan is called done.
- [ ] **Step 3: Commit:** `Record DuckLake query timings and acceptance results`.

## Completion notes

(Filled in by Task 16.)
