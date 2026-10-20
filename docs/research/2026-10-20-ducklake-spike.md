# Spike 3: DuckLake with a SQLite catalog as slarc's archive store

Run on 2026-10-20 against slarc `main` at `503d1aa`. Throwaway code; nothing in the repo was changed. Background facts with citations: `2026-10-20-open-table-formats.md`.
Hardware: Apple M1 Ultra, 64 GB, APFS in /private/tmp, Python 3.14, DuckDB 1.5.6, ducklake extension
`ac7595b0`, sqlite_scanner `5274128`, DuckLake format 1.0. DuckDB pinned to 4 threads.

Data: spike 2's generator, regenerated unchanged: 994,445 message lines in 300 channels, 297,954
directory lines, 1,980 committed runs; 1,712 MB of JSONL (raw 1.2 GB, directory 203 MB,
runs.jsonl 224 MB). Lines of the 20 uncommitted runs were dropped at load, as a migration would.

## Summary and recommendation

**Go with conditions.** DuckLake is smaller, faster for whole-archive queries, crash-safe in every
kill test, and makes `rm` physically complete in under a second. It also has sharp edges that slarc
must work around in its own code. Each one below was hit in this spike.

What it buys:
- The archive is 23% of the JSONL size (390 MB Parquet plus a 0.2 MB catalog), and a load of the whole archive takes 4.5 s.
- search and conversation_counts run 2.7 to 3 times faster with slarc's own view SQL unchanged.
  - They run 8 to 35 times faster with a simplified current-message view.
  - Opening the index takes 70 ms instead of 358 ms.
- A commit is a catalog transaction. That replaces the runs.jsonl commit log, the committed-run filter and the garbage collector designed in spike 2.
- Tracked items and download states become tables updated in place. No fold and no growing run lines.
- kill -9 during inserts, during 30 random commits and during CHECKPOINT always left exactly the last committed state, and the SQLite integrity check passed every time.

Conditions (each is a measured failure without it):
1. **Put the SQLite catalog in WAL mode** (`METADATA_PARAMETERS MAP {'journal_mode': 'wal'}` on ATTACH; it persists in the file).
   - The default is a rollback journal. There, a reader stalls the writer in 1 s steps (commit p99 2.0 s, max 3.0 s) and slows itself 15 times.
   - Any open read transaction blocks every write with "database is locked".
2. **One writer at a time, enforced by slarc's own lock.**
   - Two writers fail with "database is locked" within milliseconds.
   - Neither DuckLake's retry nor `busy_timeout` covers this. 2 to 3 of 40 runs per writer still failed with 20 retries and backoff.
3. **Run maintenance from the writer every run, with tuned options.**
   - Set `target_file_size` (16MB here). With the default, `merge_adjacent_files` rewrote the whole 330 MB message table every checkpoint (830 ms).
   - Set `rewrite_delete_threshold` to 0 on `tracked_item` and `download_state`. Otherwise their files grow by 2 per run, giving 4,053 files after 2,000 runs.
   - Tuned, a CHECKPOINT takes 120 to 150 ms per run and the lake holds 26 files.
4. **Do not trust the time-based windows.**
   - `cleanup_old_files(older_than => now() - INTERVAL '1 hour')`, and also 6 or 13.5 hours, deleted files scheduled 1 s earlier. 18 hours and 1 day kept them. The window behaves as if compared by calendar day.
   - `expire_snapshots(older_than => now())` gave different results under TZ=Asia/Tokyo.
   - slarc should expire by `versions` and keep its own retention record, or run cleanup only when no reader can be active.
5. **Never expire a snapshot that a live read transaction uses.**
   - A reader inside BEGIN..COMMIT whose snapshot was expired got **0 rows and no error** on its next query. This happened even when the files were still on disk.
   - Autocommit readers (search, export) are safe. A long-lived `serve` transaction is not.
6. **Run `delete_orphaned_files` only under the writer lock, or with a window of hours.**
   - With a 0 s window, it deleted the Parquet files of a large insert in flight in another process. The insert then committed, and the table became unreadable ("Cannot open file").
   - A 1 hour window protected the files (this function's window works, unlike `cleanup_old_files`).
7. **Ship or pre-seed the extensions.** ducklake and sqlite_scanner are 27 MB each, are downloaded on first ATTACH, and are pinned to the DuckDB version.
   - With no network and an empty extension directory, ATTACH fails.
   - A DuckDB upgrade is a catalog migration step.
8. **Code-level rules:**
   - Read DuckLake maintenance function results with `fetchall()`. With `fetchone()` the call silently did not commit.
   - Insert batches as one JSON parameter, not a big parameterized VALUES: 11 ms against 812 ms for 1,000 rows.
   - Without pytz, any TIMESTAMPTZ result raises in Python. Cast it or count it.

If slarc is unwilling to own items 2 to 6, the JSONL commit-log design from spike 2 has fewer ways to lose data silently.

## Key numbers

### Q1: size and query speed (median of 5, 4 threads)

| | JSONL | DuckLake flat | flat, 16 MB files | partitioned by team, channel |
|---|---|---|---|---|
| On disk | 1,712 MB | 390 MB + 0.18 MB catalog (22.8%) | 390 MB, 14 files | 400 MB + 0.75 MB, 304 files |
| Load from JSONL | n/a | 4.5 s | 4.7 s | 5.9 s |
| Open index | 358 ms | 72 ms | 70 ms | 68 ms |
| search | 4.90 s | 1.82 s | 1.73 s | 1.87 s |
| conversation_counts | 4.14 s | 1.38 s | 1.35 s | 1.43 s |
| messages, largest channel (79k rows) | 0.64 s | 0.80 s | 0.78 s | 0.77 s |
| messages, mid channel (2.5k rows) | 0.094 s | 0.076 s | 0.076 s | 0.030 s |
| thread (13 rows) | 0.155 s | 0.212 s | 0.211 s | 0.206 s |

- With a one-QUALIFY current view, search takes 0.64 s and counts 0.117 s. With a writer-maintained `message_current` table, search takes 0.40 s and counts 0.009 s.
- Spike 2's JSONL numbers were search 4.82 s, counts 4.23 s, messages_largest 4.29 s and messages_mid 3.57 s.
  - Those messages numbers predate `bca17bf`, which reads only the channel's own files. Today's JSONL messages take 0.09 to 0.64 s.

### Q2: hourly pulls, 2,000 runs (83 simulated days, about 64k commits), flat 16 MB lake

| Variant at run 2,000 | commit p50 / p99 | maintenance per run | catalog | files | snapshots | inlined rows | open | search |
|---|---|---|---|---|---|---|---|---|
| No maintenance | 75 / 111 ms | none | 238 MB | 97 | 63,760 | 271,091 | 348 ms | 2.65 s |
| CHECKPOINT every run, default options | 43 / 84 ms | 370 ms | 2.6 MB | 4,053 | 3 | 0 | 73 ms | 2.18 s |
| CHECKPOINT every run, tuned (1,000 runs) | 18 / 37 ms | 123 ms | 0.35 MB | 26 | 3 | 0 | 71 ms | 2.06 s |
| CHECKPOINT every 24 runs | 25 / 48 ms | 221 ms per checkpoint | 3.3 MB | 198 | 257 | 1,073 | 78 ms | 2.24 s |
| Partitioned, CHECKPOINT every run | 46 / 92 ms | 1,000 ms | 3.4 MB | 4,343 | 3 | 0 | 79 ms | 2.35 s |

- Search times come from a quiet re-probe (`q2_probe_final.py`). The archive grew by about 27% over the runs, and search grew by a similar share.
- Extrapolating the no-maintenance run linearly to a year (8,760 runs, about 280k commits): a 1 GB catalog, 1.2M inlined rows, commit p50 near 240 ms and an open time near 1.4 s.
- One catch-up CHECKPOINT on the 2,000-run lake took 3.4 s. A SQLite VACUUM then shrank the catalog from 238 MB to 0.2 MB.

### Q3: concurrency across processes (40 runs of 32 commits plus a 1,000-row page every 5 runs)

| Setup | Writer commits p50 / p99 / max | Writer wall | Reader search p50 | Reader messages p50 | Errors |
|---|---|---|---|---|---|
| Writer alone | 19 / 35 / 43 ms | 27 s | n/a | n/a | none |
| Reader alone | n/a | n/a | 1.80 s | 75 ms | none |
| Both, rollback journal (default) | 21 / 2,047 / 3,053 ms | 117 s | 2.86 s | 1,088 ms | none |
| Both, rollback journal, busy_timeout 10000 | 19 / 2,047 / 3,047 ms | 115 s | 2.83 s | 1,087 ms | none |
| Both, WAL | 24 / 46 / 1,054 ms | 37 s | 1.81 s | 78 ms | none |
| Two writers, WAL, no retry | n/a | n/a | n/a | n/a | 34 to 38 of 40 runs failed per writer |
| Two writers, WAL, 20 attempts, 0.1 s backoff | 21 / 148 to 388 / 4,168 ms | 53 to 58 s | n/a | n/a | 2 and 3 runs failed |

### Q4: kill -9 (all checks: SQLite integrity ok, state equal to the last commit)

| Kill point | State | Leftover files | Cleanup |
|---|---|---|---|
| Large insert (985k rows, 1.04 s), at 20/50/80/95% | equal, 4 of 4 | 3 to 9 files, up to 288 MB | `delete_orphaned_files` with a 1 h window kept them; with 0 s it removed all |
| 30 random kills in a loop of 5, 50 or 2,000-row commits | consistent 30 of 30, no partial commit | 19 small files | removed with 0 s window |
| CHECKPOINT, at 10/30/50/70/90% | equal, 5 of 5 (hot journal at 10% and 50% rolled back) | 0 to 8 files | removed; a fresh CHECKPOINT then succeeded |

### Q5: rm of one conversation (16,746 versions, lake with 100 runs of history)

| Step | flat 16 MB | partitioned |
|---|---|---|
| DELETE messages, tracked item, download states (1 transaction) | 77 ms | 87 ms |
| flush_inlined_data | 114 ms | 1,735 ms |
| rewrite_data_files (threshold 0) | 295 ms | 82 ms |
| expire_snapshots (older_than now) | 71 ms | 70 ms |
| cleanup_old_files | 3 ms | 3 ms |
| delete_orphaned_files | 4 ms | 31 ms |
| SQLite VACUUM of the catalog | 13 ms | 20 ms |
| Total | 0.58 s | 2.03 s |

After these steps, no Parquet file holds a row of the channel, and no catalog table row mentions it.
The channel id stays in the catalog file's raw bytes until VACUUM. Time travel to before the rm fails with "No snapshot found".

## Schema and design choices

Tables (all in one DuckLake schema; full DDL in `lakelib.py`):
- `message_version(team, channel, ts, thread_ts, user, kind, fetched_at, run, source_team, api, hash, plain, object JSON)`: append-only. `kind = 'deleted'` is the deleted marker, as today.
- `directory_entry(kind, team, id, fetched_at, run, hash, blob, object JSON)`: users, channels, emoji, files, images, bots and usergroups in one table.
- `run(run, command, started, finished, fetched_at, object JSON)`: the run record without `tracked` and `downloads`.
- `tracked_item(team, kind, channel, ts, since, until, newest_ts, oldest_ts, backfill_complete, last_search_at, last_pull_at, status, archived, last_read JSON, retry_threads JSON)`: one row per item, updated in place.
- `download_state(team, target_kind, target, channel, url, status, attempts, last_error)`: updated in place.
  - The `channel` column is new. rm needs it to find a conversation's download states, and today's DownloadState has no conversation.

Partitioning: none, with rows sorted by (team, channel, ts) at load and `target_file_size = 16MB`.
- Partitioning by (team, channel) only helped mid-size channel reads (30 ms against 76 ms).
- It made every flush write one file per active channel. It also tripled checkpoint time (1.0 s against 0.37 s untuned), and made the rm flush take 1.7 s and double the file count.
- An unsorted load cost about 5% on search and 2 times on channel reads, so sorting matters more than partitioning here.

Current version of a message: newest (fetched_at, run) still works and is still needed, because history (edits, first_seen and last_seen, deletes) is part of the archive. What becomes simpler:
- Commits serialize, so there are no duplicate lines from crashes or union merges. The dedupe QUALIFY in `message_line` can go.
- The three-CTE `message_current` fold can become one
  `QUALIFY row_number() OVER (PARTITION BY team, channel, ts ORDER BY fetched_at DESC, run DESC, kind) = 1`
  over the version table, with `thread_ts` and `user` as columns. That gives search 0.64 s against 1.82 s.
  - This view is not yet equivalent: a deleted message loses its content columns, and thread parents are not inferred from replies. Porting those rules is real work.
- A writer-maintained `message_current` table is faster again (search 0.40 s). It costs an UPDATE per changed message, which means delete files on a hot table. Not measured.
- Tracked items and download states need no newest-wins at all.

## Q1 details

- `LakeIndex` subclasses slarc's `DuckDbQueryIndex` and swaps only the source views:
  - `raw_line` becomes the version table;
  - the directory views become `struct_pack` over `directory_entry`;
  - `run_line` becomes the `run` table.
  - search, conversation_counts and thread are slarc's own methods.
  - `_messages` filters the version table by team and channel, the counterpart of today's per-channel files.
- messages and thread are slightly slower than today's JSONL path (1.2 to 1.4 times). Both scan a whole channel through the full fold, and the JSONL reader of one channel's files is already fast.
  - With `thread_ts` as a column, thread could filter at the source. Not measured.
- Parquet is Snappy-compressed by default, with 122,880-row row groups.

## Q2 details

- `q2_year.py`, per run:
  - one directory commit (0 to 5 user versions);
  - 30 conversation commits, each with 1 to 5 message versions (20% edits), one tracked_item UPDATE, and in 20% of commits a download_state insert plus an update;
  - one run-row commit;
  - every 24th run, a 1,000-message backfill commit.
  - In all, 32 commits per run.
- Four variants ran in parallel. Their commit latencies are about 5 ms higher than the solo numbers in Q3 (19 ms) and in the tuned run (18 ms), which ran mostly alone.
- Without maintenance, every small commit stays inlined in SQLite. The catalog grows 0.12 MB per run, and commit latency grows with it (25 ms at run 100, 75 ms at run 2,000). Opening the lake grows too (105 ms to 348 ms).
- Maintenance step costs on a 40-run lake, default options (`probe_maint.py`):
  - flush 101 ms;
  - expire 33 ms;
  - merge_adjacent_files 828 ms, all of it rewriting the 330 MB message file plus the 58 MB directory file;
  - rewrite 13 ms;
  - cleanup 8 ms;
  - orphans 4 ms.
- With `target_file_size = 64MB`, the merge took 243 ms and left the message file alone. With 16 MB, the directory file is also left alone.
- Files that have delete files are never merged. The tables updated in place (`tracked_item`, `download_state`) accumulate a file and a delete file per run until 95% of a file's rows are deleted.
- The expire and cleanup windows in the sim were "30 seconds" of wall time. Given the window behavior in Q3, they probably expired everything older than the current day, so the "3 snapshots" columns say little about retention.

## Q3 details

- With the default rollback journal, a reader process holds SQLite's shared lock while DuckLake reads the catalog. Writer stalls come in 1 s steps (58 of 1,280 commits took over 1 s), and reader queries wait on the writer in turn.
- `busy_timeout` is accepted as a metadata parameter (unknown names are rejected) but changed nothing measurable.
- A reader holding an explicit transaction (BEGIN plus a query) blocked the writer completely: "Failed to commit DuckLake transaction ... database is locked" (`q3c_output_rollback_journal.txt`).
- With WAL, readers and the writer stop interfering.
- Two writers fail with `TransactionException: ... Failed to flush changes into DuckLake: database is locked`, under WAL and with busy_timeout alike.
  - The failure is immediate. It looks like SQLite's busy-snapshot case, where a read transaction cannot upgrade to a write; busy handlers do not apply there.
  - DuckLake's own retry (`ducklake_max_retry_count` 10) is for snapshot-id conflicts and does not cover it.
  - Data stayed consistent: 300 tracked items, one per channel.
- Cleanup against readers (WAL; `q3_cleanup_reader.py`). The writer deletes 1/16 of rows, rewrites every file, expires all snapshots and runs `cleanup_old_files`:

| Reader | cleanup window | Result |
|---|---|---|
| BEGIN, scan, writer cleans, scan again | 0 s (files removed) | second scan returns 0 rows, no error |
| same | 1 day (files kept) | 0 rows, no error |
| same | no cleanup, expire only | 0 rows, no error |
| a 13 s query in flight | 0 s | `IOException: Cannot open file ...parquet: No such file or directory` |
| a 13 s query in flight | 1 day | completes correctly |
| ATTACH with SNAPSHOT_VERSION of an expired snapshot | n/a | `No snapshot found at version 6` |

- Window granularity (`probe_window.py`): files scheduled seconds earlier were removed by `older_than => now() - INTERVAL 'N'` for N = 1, 4, 6, 12 and 13.5 hours. They were kept for 18 hours, 1 day and a 2020 timestamp. The results were the same with TZ=UTC.
  - Under TZ=Asia/Tokyo, `expire_snapshots(older_than => now())` kept the two newest snapshots that it expired under America/Chicago and UTC.
  - Probably a timestamp handling bug in the extension with the SQLite catalog (the catalog stores times as text with an offset). Worth an upstream report before relying on windows.

## Q4 details

- The 30-kill loop mixed inlined commits (5 rows), small Parquet commits (50) and larger ones (2,000). No commit ever appeared partially. The rows found always equalled the rows the victim reported, or exceeded them by one commit when the kill landed after COMMIT returned.
- Two CHECKPOINT kills left `catalog.sqlite-journal`. SQLite rolled it back on the next open.
- Orphans were found by comparing the files on disk with every path in `ducklake_data_file`, `ducklake_delete_file` and `ducklake_files_scheduled_for_deletion`.
  - `delete_orphaned_files` removed exactly that set and changed no table state.
  - Its 1 hour window kept files written seconds earlier.
- The danger is a 0 s window while another process writes (condition 6). After a crash, under the lock, a 0 s window is safe.

## Q5 details

- Inlined rows of the conversation lived in `ducklake_inlined_data_*` tables in SQLite. DELETE only ends them; flush plus expire removes them.
- The raw-byte check on Parquet is weak, because Snappy hides most strings. Per-file row scans are the real check: before the rm, 3 of 20 files (flat) held the channel's rows; after it, none did.
- rm has to expire **every** snapshot older than itself, so it also ends all time travel for the rest of the archive. Expiring by version, keeping only the rm's own snapshot, has the same effect.
- Partitioning did not make rm cheaper at this size. The rewrite is cheaper (82 ms, whole-partition files) but the flush is far more expensive (1.7 s). With partitions, rewrite_data_files still rewrote files rather than dropping them, because the inlined and flushed rows of the channel were spread over several files.

## Q6: operational notes

- Installed into the spike's `extension_directory`:
  - `ducklake` `ac7595b0` (27.6 MB);
  - `sqlite_scanner` `5274128` (27.1 MB);
  - both from the core repository, for DuckDB v1.5.6 (`069cc9f9b5`).
  - Versions are git hashes.
- The catalog records its version in `ducklake_metadata`: `version = 1.0`, `created_by = DuckDB 069cc9f9b5`, `data_path` (absolute), `encrypted = false`. `ducklake_options()` shows the same values.
- The data path is stored as an absolute path. A copied lake needs `OVERRIDE_DATA_PATH true`. File paths are relative to the table directory.
- Offline (`q6_offline.py`):
  - With an empty extension directory and the repository unreachable, ATTACH fails: `IO Error: Failed to download extension "ducklake" at URL ... (ERROR Could not establish connection)`.
  - With autoinstall off, it fails with `Extension ... not found ... Install it first using "INSTALL ducklake"`.
  - With a pre-populated directory, it works offline.
  - `custom_extension_repository` alone does not redirect autoloading; `autoinstall_extension_repository` does.
- `~/.duckdb/extensions` is the default directory. Pointing `extension_directory` at a slarc-owned directory keeps the installs together.

## Surprises

1. The SQLite catalog uses a rollback journal by default. A single reader slows writes to seconds, and an open read transaction blocks them.
2. `busy_timeout` has no visible effect, and two writers fail at once regardless.
3. An expired snapshot makes a pinned reader return an empty table silently.
4. The `cleanup_old_files` older_than window works at day granularity, and expire_snapshots depends on the time zone.
5. `delete_orphaned_files` with a short window destroys a concurrent writer's committed data.
6. Default `merge_adjacent_files` rewrites the whole table on every checkpoint while files are below the target size.
7. Files with deletes are never merged, so tables updated in place fragment until `rewrite_delete_threshold` is lowered.
8. A maintenance function read with `fetchone()` does not commit.
9. A large parameterized VALUES is the slow part of an insert, not DuckLake.
10. The data inlining limit (10 rows) counts the whole insert, not rows per partition.
11. The SQLite catalog never shrinks without VACUUM (238 MB to 0.2 MB in a few milliseconds).
12. The extensions are 54 MB on disk, more than three times the DuckDB wheel.

## Scripts

The throwaway scripts and their raw outputs are kept outside git in `.llm/spike3-ducklake/`. They reuse spike 2's synthetic archive generator.
