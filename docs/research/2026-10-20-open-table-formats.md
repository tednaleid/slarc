# Open table formats for the slarc archive

Research date: 2026-10-20. Candidates: DuckLake, Apache Iceberg (PyIceberg, DuckDB `iceberg`
extension), Delta Lake (delta-rs `deltalake`, DuckDB `delta` extension), plus a plain SQLite
file for comparison. Versions checked: DuckDB 1.5.6 (installed in this repo), DuckLake 1.0,
PyIceberg 0.12.0, deltalake 1.6.6, pyarrow 26.0.0.

Usage shape assumed: one hourly cron `pull` plus manual runs, about 30 tiny commits per run,
300 conversations, 1 to 10M message versions, single-digit GB, `rm` that must physically delete,
DuckDB readers while a pull runs, later S3 possible.

## Summary table

| Question | DuckLake | Iceberg (PyIceberg / DuckDB ext) | Delta (delta-rs / DuckDB ext) | SQLite |
|---|---|---|---|---|
| 1. Local, no server | Yes. Catalog = DuckDB file (single client) or SQLite file (multi-process); Parquet in a directory | PyIceberg: SqlCatalog on a SQLite file, docs say dev only. DuckDB ext: writes need a REST catalog; no file catalog | Yes. `_delta_log` dir, no catalog. delta-rs writes locally; DuckDB ext does blind appends | Yes, one file |
| 2. Multi-process | DuckDB catalog: single process at a time (file lock). SQLite catalog: readers and writers across processes; writers serialize with retry. Logical conflicts detected and retried (max 10) | PyIceberg: optimistic compare-and-swap on catalog row; retries (4 by default). SQLite catalog serializes writers | Optimistic; log entry put-if-absent; delta-rs retries conflicts; readers never blocked | WAL: readers never blocked, one writer at a time, `busy_timeout` (Python default 5 s) |
| 3. Atomic commit, crash | Catalog DB transaction; uncommitted Parquet become orphans; `ducklake_delete_orphaned_files` | Metadata file write then CAS of pointer; orphan files; PyIceberg has no orphan cleanup yet (PR open) | Put-if-absent of `N.json`; "zombie" Parquet cleaned by `vacuum` | Journal or WAL; nothing to clean |
| 4. Many small commits | Rows < 10 are inlined into the catalog, no file written; `CHECKPOINT` runs flush, expire, merge, rewrite, cleanup | Each commit: new metadata.json + manifest list + manifest. PyIceberg: expire_snapshots yes; no data compaction, no orphan removal | One JSON per commit; checkpoint every `delta.checkpointInterval`; `optimize.compact`, `vacuum`, `cleanup_metadata` all in Python | n/a, in-place |
| 5. Row deletes and physical removal | Merge-on-read delete files (DV experimental); `rewrite_data_files` + `expire_snapshots` + `cleanup_old_files(older_than)` | DuckDB ext: positional deletes / DVs only. PyIceberg: copy-on-write only; expire removes metadata, not files | Copy-on-write (DV write not supported); `vacuum`, default 7 day retention | `DELETE`, then `VACUUM` to reclaim |
| 6. Object storage | Catalog stays a DB (SQLite/Postgres); data on S3 with plain writes. Read-only: attach a `.ducklake` DuckDB file over HTTPS/S3 | Catalog arbitrates; `iceberg_scan` reads a metadata file from S3 with no catalog | Default S3 store uses conditional put since delta-rs 1.0; `delta_scan('s3://...')` needs no catalog | Not usable on S3 |
| 7. Maturity | Spec 1.0, 2026-04-13, "guaranteed backward-compatibility"; ext ships with DuckDB | Spec v3 adopted. PyIceberg 0.12.0 (pre-1.0). DuckDB iceberg README: "experimental" | Protocol stable; delta-rs rust 1.0.0 and python 1.6.6 (Sep 2026). DuckDB delta ext tags stop at v0.4.0 | Very mature |
| 8. Python weight | DuckDB wheel 14.8 MiB; `ducklake` and `sqlite` extensions downloaded at first use | pyiceberg 0.5 MiB + pyarrow 34.7 MiB (needed to write) + sqlalchemy 2.4 MiB | deltalake 44.9 MiB (abi3), pyarrow optional | stdlib |
| 9. DuckDB query | Native: per-file min/max stats in catalog, partitioning, `AT (VERSION/TIMESTAMP)` | `iceberg_scan` + `snapshot_from_id/timestamp`; `AT` via catalog | `delta_scan`, partition and row-group skipping, `AT (VERSION => n)` | `sqlite` extension; no time travel |

## DuckLake

Spec and status

- Spec 1.0; "This page contains the specification for the DuckLake format, version 1.0."
  https://ducklake.select/docs/stable/specification/introduction
- 1.0 released 2026-04-13, "production-ready with guaranteed backward-compatibility", "ships a
  stable specification". Attaching a newer extension "no longer auto-migrates the catalog".
  https://ducklake.select/2026/04/13/ducklake-10/
- "DuckLake v1.0 is supported by DuckDB v1.5.2+". The spec and extension release together.
  https://ducklake.select/docs/stable/duckdb/introduction and
  https://duckdb.org/docs/current/core_extensions/ducklake.html
- Format limits: "does not support indexes, primary keys, foreign keys, and UNIQUE or CHECK
  constraints". https://ducklake.select/docs/stable/duckdb/introduction

1. Local filesystem, no server

- Catalog can be a DuckDB file, SQLite file, PostgreSQL or MySQL (MySQL not recommended).
  DuckDB file: "if you are using DuckDB as your catalog database, you're limited to a single
  client." SQLite: recommended for "multiple local clients".
  https://ducklake.select/docs/stable/duckdb/usage/choosing_a_catalog_database
- `ATTACH 'ducklake:...' (DATA_PATH '...')`. For a DuckDB catalog the default data path is
  `<catalog file>.files`; for other catalogs `DATA_PATH` is required. `READ_ONLY` is an attach
  option. https://ducklake.select/docs/stable/duckdb/usage/connecting
- The SQLite catalog needs DuckDB's `sqlite` extension (a downloaded core extension).

2. Multi-process concurrency

- DuckDB catalog file is subject to DuckDB's own file lock: "one process can both read and
  write", or with `access_mode = 'READ_ONLY'` "multiple processes can read from the database,
  but no processes can write". So a running pull (writer) blocks `search` in another process and
  vice versa. https://duckdb.org/docs/current/connect/concurrency.html
- SQLite catalog: "While SQLite doesn't allow concurrent reads and writes, its default mode is
  to ATTACH and DETACH for every query, together with providing a 'retry time-out' for queries
  when a write-lock is encountered. This allows a reasonable amount of multi-processing support
  (effectively hiding the single-writer model)."
  https://ducklake.select/docs/stable/duckdb/usage/choosing_a_catalog_database
- Logical conflicts: snapshot ids are sequential and the primary key on `ducklake_snapshot`
  detects two writers committing the same id; the loser inspects `ducklake_snapshot_changes`
  and retries without rewriting data files if the changes do not overlap, otherwise aborts.
  Concurrent deletes from different data files of one table are allowed; two deletes from the
  same data file conflict; insert vs delete on the same table conflicts.
  https://ducklake.select/docs/stable/duckdb/advanced_features/conflict_resolution
- Retry settings: `ducklake_max_retry_count` default 10, `ducklake_retry_wait_ms` 100,
  `ducklake_retry_backoff` 1.5. https://ducklake.select/docs/stable/duckdb/usage/configuration
- Transactions: "full support for ACID and offers snapshot isolation"; a BEGIN..COMMIT block is
  one snapshot. https://ducklake.select/docs/stable/duckdb/advanced_features/transactions

3. Atomic commit and crash behavior

- Commit is a transaction on the catalog database (which "requires a database that supports
  transactions and primary key constraints"). Data files are written first; the catalog commit
  makes them visible. "DuckLake as a concept will never change existing files."
  https://ducklake.select/docs/stable/specification/introduction and
  https://ducklake.select/docs/stable/duckdb/usage/choosing_storage
- Files not tracked by the catalog "may appear due to, for example, a systems failure" and are
  removed by `ducklake_delete_orphaned_files(older_than => ..., dry_run => ...)`.
  https://ducklake.select/docs/stable/duckdb/maintenance/cleanup_of_files

4. Many small commits

- Data inlining is on by default with `data_inlining_row_limit` = 10: inserts and deletes below
  the limit are written to tables inside the catalog, no Parquet file. "Inlined data behaves
  exactly the same as data written to Parquet files." `ducklake_flush_inlined_data` moves it to
  Parquet; flushing is not automatic after writes.
  https://ducklake.select/docs/stable/duckdb/advanced_features/data_inlining
- Each commit adds catalog rows (`ducklake_snapshot`, `ducklake_snapshot_changes`,
  `ducklake_data_file`, `ducklake_file_column_stats`), not files.
  https://ducklake.select/docs/stable/specification/tables/overview
- `ducklake_merge_adjacent_files` merges small files without expiring snapshots (per-row
  snapshot id kept in `_ducklake_internal_snapshot_id`); `auto_compact` only selects tables,
  "Compaction always runs explicitly when you call a maintenance function."
  https://ducklake.select/docs/stable/duckdb/maintenance/merge_adjacent_files
- `CHECKPOINT` runs flush, expire_snapshots, merge_adjacent_files, rewrite_data_files,
  cleanup_old_files, delete_orphaned_files in that order, driven by `expire_older_than`,
  `delete_older_than`, `rewrite_delete_threshold`, `auto_compact`.
  https://ducklake.select/docs/stable/duckdb/maintenance/checkpoint
- All of this is SQL through the DuckDB Python package; no extra library.

5. Deletes

- Merge-on-read: "Delete files contain the row ids of rows that are deleted." Format `parquet`
  (positional) or `puffin` (Iceberg v3 deletion vector, experimental,
  `write_deletion_vectors` default false). Repeated deletes to one data file produce a partial
  delete file. https://ducklake.select/docs/stable/specification/tables/ducklake_delete_file
- Small deletes are inlined into the catalog (inlined deletion table) below the row limit.
- Physical removal: `ducklake_rewrite_data_files` rewrites files whose deleted fraction exceeds
  `rewrite_delete_threshold` (default 0.95; set lower per table); then
  `ducklake_expire_snapshots(older_than|versions)` schedules old files; then
  `ducklake_cleanup_old_files(older_than => ...)` deletes them. "It is generally safe to delete
  files that have been scheduled for deletion more than a few days ago" because active queries
  may still be scanning them. https://ducklake.select/docs/stable/duckdb/maintenance/rewrite_data_files,
  https://ducklake.select/docs/stable/duckdb/maintenance/expire_snapshots,
  https://ducklake.select/docs/stable/duckdb/maintenance/cleanup_of_files
- "DuckLake in normal operation never removes any data, even when tables are dropped or data is
  deleted." Dropping a conversation's rows needs the full three steps above.

6. Object storage

- Data files can live on S3 and compatibles, GCS, Azure, NFS or fsspec. No conditional-write
  requirement is documented; the catalog is the arbiter, so S3 only gets plain PUTs of new,
  uniquely named files. https://ducklake.select/docs/stable/duckdb/usage/choosing_storage
- The catalog must still be a writable database. A DuckDB file on S3/HTTPS is read-only ("Only
  read-only connections are allowed, writing the database via the HTTPS protocol or the S3 API
  is not possible"). https://duckdb.org/docs/current/guides/network_cloud_storage/duckdb_over_https_or_s3.html
- Read-only sharing works: `ATTACH 'ducklake:https://.../space.ducklake'` (Frozen DuckLake,
  catalog file + Parquet in a bucket, "no moving parts other than a cloud storage system").
  https://ducklake.select/2025/10/24/frozen-ducklake/ and
  https://ducklake.select/docs/stable/duckdb/guides/using_a_remote_data_path
- "DuckLake currently does not allow you to change the persisted data path in the catalog"
  (`OVERRIDE_DATA_PATH` is per connection only). https://ducklake.select/docs/stable/duckdb/usage/connecting

7. Distribution

- `ducklake` is a core extension, "transparently autoloaded on first use in an ATTACH clause".
  In the installed duckdb 1.5.6 wheel only `core_functions`, `icu`, `json`, `parquet` are
  statically linked; `ducklake`, `sqlite_scanner`, `httpfs`, `iceberg`, `delta` show
  `NOT_INSTALLED` and are fetched from `extensions.duckdb.org` on first use
  (`autoinstall_known_extensions` and `autoload_known_extensions` default true). Offline use
  needs a pre-populated `extension_directory`.
  https://duckdb.org/docs/current/extensions/installing_extensions.html,
  https://duckdb.org/docs/current/configuration/overview.html

8. Python weight: duckdb 1.5.6 wheel 14.8 MiB (macOS arm64), no pyarrow needed.

9. Querying: native DuckDB catalog; per-file min/max in `ducklake_file_column_stats` prune
   files; optional `PARTITIONED BY` (identity, year/month/day/hour, bucket); partition values
   tracked in the catalog, Hive paths optional. Time travel `AT (VERSION => n)` /
   `AT (TIMESTAMP => ...)` or `ATTACH ... (SNAPSHOT_VERSION n)`.
   https://ducklake.select/docs/stable/duckdb/advanced_features/partitioning,
   https://ducklake.select/docs/stable/duckdb/usage/time_travel

## Apache Iceberg

Spec: "Versions 1, 2 and 3 of the Iceberg spec are complete and adopted by the community.
Version 4 is under active development and has not been formally adopted." v3 replaces position
delete files with Puffin deletion vectors; "Position delete files must not be added to v3
tables". File-system tables (commit by atomic rename) are deprecated and "unsafe on object
stores and local file systems". https://iceberg.apache.org/spec/

1. Local filesystem, no server

- PyIceberg SqlCatalog: `type: sql`, `uri: sqlite:////path/catalog.db`, warehouse path for
  files. Docs: SQLite "not built for concurrency", suited to "exploration and development".
  https://py.iceberg.apache.org/configuration/
- DuckDB `iceberg` extension: "Writing requires an attached catalog" and only Iceberg REST
  catalogs are documented; `iceberg_scan` on a path "require[s] no catalog and is read-only".
  Storage backends for REST catalogs: "S3, S3 Tables, and Google Cloud Storage (GCS)...
  Support for other storage backends is not yet available."
  https://duckdb.org/docs/current/core_extensions/iceberg/overview.html,
  https://duckdb.org/docs/current/core_extensions/iceberg/writing_to_iceberg,
  https://duckdb.org/docs/current/core_extensions/iceberg/catalogs.html
- So a local no-server setup means PyIceberg writes and DuckDB reads via `iceberg_scan`; the
  local read needs a `version-hint.text` or explicit `version`, or
  `unsafe_enable_version_guessing` ("may potentially violate ACID constraints").
  https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_options

2. Multi-process concurrency

- Spec: "Writers create table metadata files optimistically... If the snapshot on which an
  update is based is no longer current, the writer must retry the update".
- PyIceberg SqlCatalog.commit_table writes the new metadata file, then
  `UPDATE ... WHERE metadata_location == current` and raises
  `CommitFailedException("Table has been updated by another process")` when rowcount < 1.
  https://github.com/apache/iceberg-python/blob/main/pyiceberg/catalog/sql.py
- Retries: `commit.retry.num-retries` (Iceberg default 4), min wait 100 ms, max 60 s, total
  30 min; PyIceberg re-executes registered snapshot producers on retry and cleans up manifests
  from failed attempts. https://github.com/apache/iceberg-python/blob/main/pyiceberg/table/__init__.py,
  https://iceberg.apache.org/docs/latest/configuration/
- With SQLite as the catalog DB, writers serialize on SQLite's lock; readers (DuckDB
  `iceberg_scan`) read immutable files and are never blocked.

3. Atomic commit and crash behavior

- "All changes to table state create a new metadata file and replace the old metadata with an
  atomic swap." Failure before the swap leaves data files, manifests and a metadata.json that
  nothing references. PyIceberg docstring: failures "may leave orphan data files in storage...
  Clean these up with expire/orphan-file maintenance jobs", but PyIceberg has no orphan-file
  job: PR #3361 "DeleteOrphanFiles maintenance task" is still open (updated 2026-10-09).
  https://github.com/apache/iceberg-python/pull/3361

4. Many small commits

- Every commit writes a new full metadata.json plus a manifest list plus (fast append) a new
  manifest; "it creates more metadata than a merge commit". Old metadata.json files are kept
  up to `write.metadata.previous-versions-max` = 100 and only deleted when
  `write.metadata.delete-after-commit.enabled` = true (default false). Manifest merging happens
  at `commit.manifest.min-count-to-merge` = 100. https://py.iceberg.apache.org/api/,
  https://iceberg.apache.org/docs/latest/configuration/
- PyIceberg 0.12.0 has: append, overwrite, delete, upsert, add_files,
  `table.maintenance.expire_snapshots()`. It does not have: data file compaction (rewrite data
  files), orphan file removal, or manifest rewrite (issue #270 "Support metadata compaction"
  reopened 2026-01-05, still open). `expire_snapshots` only removes snapshot entries from
  metadata; the class never calls `io.delete`, so data files stay on disk.
  https://github.com/apache/iceberg-python/issues/270,
  https://github.com/apache/iceberg-python/blob/main/pyiceberg/table/update/snapshot.py
- DuckDB extension has no maintenance functions documented (no expire, compaction, orphan
  removal). https://duckdb.org/docs/current/core_extensions/iceberg/overview.html

5. Deletes

- PyIceberg: copy-on-write only. Source: `warnings.warn("Merge on read is not yet supported,
  falling back to copy-on-write")`; whole files are dropped when every row matches, otherwise
  the file is rewritten without the matching rows. Deletion vector reading since 0.10.0;
  Puffin DV writer PR #3474 still open. https://github.com/apache/iceberg-python/blob/main/pyiceberg/table/__init__.py,
  https://github.com/apache/iceberg-python/pull/3474
- DuckDB extension: the opposite. "DuckDB-Iceberg only writes positional deletes (encoded as
  binary deletion vectors for v3 tables)... Copy-on-write functionality is not yet supported."
  https://duckdb.org/docs/current/core_extensions/iceberg/writing_to_iceberg
- Physical removal with PyIceberg: copy-on-write delete already rewrites the file, but the old
  file remains referenced by older snapshots; expire those snapshots (metadata only) and then
  delete the unreferenced files yourself, since there is no orphan/expire file deletion in
  PyIceberg. Spark's `expire_snapshots`/`remove_orphan_files` are the reference tools.

6. Object storage

- The catalog does the compare-and-swap, so S3 needs no conditional writes; a SQLite catalog
  would have to stay on the local machine (or move to Postgres/REST).
- Readers without a catalog: `iceberg_scan('s3://bucket/table')` or a specific metadata.json;
  with a version hint or explicit version. https://duckdb.org/docs/current/core_extensions/iceberg/overview.html

7. Maturity

- PyIceberg 0.12.0 released 2026-09-01 (0.11.0 2026-02-10). No 1.0.
  https://github.com/apache/iceberg-python/releases
- DuckDB `iceberg` README: "This extension is experimental. APIs and behavior may change, and
  some catalog-specific features are not yet supported." Writes arrived with DuckDB 1.4.0
  (2025-09-16, "The duckdb-iceberg extension now supports writing to Iceberg").
  https://github.com/duckdb/duckdb-iceberg, https://duckdb.org/2025/09/16/announcing-duckdb-140.html
- Distribution: core extension, downloaded at first use (see DuckLake section 7).

8. Python weight

- pyiceberg 0.12.0 wheel 0.5 MiB, Python >=3.10,<4; required deps include pydantic, fsspec,
  tenacity, pyroaring, requests, rich, click. pyarrow is an extra (`pyiceberg[pyarrow]`, >=18)
  but is imported for every write path (`from pyiceberg.io.pyarrow import ...`), so in practice
  add pyarrow 26.0.0 at 34.7 MiB. `sql-sqlite` extra pulls sqlalchemy 2.1.5 (2.4 MiB).
  https://pypi.org/pypi/pyiceberg/json

9. Querying from DuckDB

- `iceberg_scan` with `snapshot_from_id` / `snapshot_from_timestamp`; catalog tables support
  `AT (VERSION => ...)`. Pruning uses Iceberg manifest column stats and partition data. The
  docs do not spell out file-skipping behavior for direct scans.
  https://duckdb.org/docs/current/core_extensions/iceberg/iceberg_options

## Delta Lake

1. Local filesystem, no server

- A Delta table is a directory of Parquet files plus `_delta_log/` with numbered JSON commits;
  no catalog. https://delta-io.github.io/delta-rs/latest/how-delta-lake-works/architecture-of-delta-table/
- delta-rs writes locally. DuckDB `delta` extension: "read and limited write (blind insert)
  support for delta tables, both local and remote" (INSERT INTO only; no UPDATE/DELETE/MERGE
  documented). https://github.com/duckdb/duckdb-delta, https://duckdb.org/docs/current/core_extensions/delta

2. Multi-process concurrency

- "The storage system must allow Delta Lake to write the file, only if it does not exist
  already, and error out otherwise." Writers "optimistically write new data and simply abandon
  the transaction if it conflicts at the end"; isolation level governs which concurrent commits
  conflict. https://delta-io.github.io/delta-rs/latest/how-delta-lake-works/delta-lake-acid-transactions/
- delta-rs retries commits (`CommitProperties.max_retries`, `DEFAULT_RETRIES`) and has a
  conflict checker; 1.6.4 added a demo of "conditional put/conflict checker behavior".
  https://github.com/delta-io/delta-rs/blob/main/crates/core/src/kernel/transaction/mod.rs,
  https://github.com/delta-io/delta-rs/releases
- Readers only read immutable files and are never blocked.

3. Atomic commit and crash behavior

- Parquet files are written first; the commit is the creation of `_delta_log/N.json`. Files
  from a failed transaction are unreferenced: "The zombie Parquet files can be easily cleaned
  up via subsequent vacuum operations." https://delta-io.github.io/delta-rs/latest/how-delta-lake-works/delta-lake-acid-transactions/

4. Many small commits

- One JSON file per commit. delta-rs creates checkpoints automatically: "Checkpoints are by
  default created based on the delta.checkpointInterval config setting" (`PostCommitHookProperties
  create_checkpoint=True`); log cleanup follows `delta.enableExpiredLogCleanup` and
  `delta.logRetentionDuration` (30 days). https://github.com/delta-io/delta-rs/blob/main/python/deltalake/transaction.py
- `dt.optimize.compact()` merges small files (logical remove, files stay for time travel);
  `dt.vacuum()` physically deletes; `dt.cleanup_metadata()` deletes expired log files;
  `dt.create_checkpoint()`. https://delta-io.github.io/delta-rs/latest/usage/managing-tables/,
  https://delta-io.github.io/delta-rs/latest/api/delta_table/

5. Deletes

- `dt.delete(predicate)`: copy-on-write. Issue #4512 (open, 2026-06-03): "all write-side data
  modification operations" rewrite whole Parquet files; delta-rs reads deletion vectors but
  cannot write them. https://github.com/delta-io/delta-rs/issues/4512
- Physical removal: `vacuum(retention_hours=None, dry_run=True, enforce_retention_duration=True)`;
  retention from `delta.deletedFileRetentionDuration`, default "interval 1 week"; shortening
  it is "not recommended for production" because readers may still hold old versions.
  https://delta-io.github.io/delta-rs/latest/usage/optimize/small-file-compaction-with-optimize/,
  https://docs.delta.io/latest/table-properties.html
- Open vacuum bugs involving deletion vectors (#4857, #4898, Oct 2026) only matter for tables
  where another engine wrote DVs.

6. Object storage

- S3 commits rely on conditional put: "S3-based conditional put semantics are now the default
  supported mode for writes on S3" (2026-05-26); rust 1.0.0 "removes the S3DynamoDbLogStore
  since it is no longer needed". S3-compatible stores (MinIO, R2) need
  `aws_conditional_put: etag`. Parts of the docs still describe DynamoDB and
  `AWS_S3_ALLOW_UNSAFE_RENAME`; the code now logs an error if `dynamodb` is requested.
  https://github.com/delta-io/delta-rs/discussions/4482, https://github.com/delta-io/delta-rs/releases,
  https://delta-io.github.io/delta-rs/latest/integrations/object-storage/s3-like/,
  https://github.com/delta-io/delta-rs/blob/main/crates/aws/src/lib.rs
- Readers need no catalog: `delta_scan('s3://bucket/table')`.

7. Maturity

- delta-rs python 1.6.6 (2026-09-24), rust 1.0.0 (2026-09-28). Protocol features: column
  mapping, CDF, constraints, table features supported; no row in the feature table for
  deletion vectors or V2 checkpoints. https://delta-io.github.io/delta-rs/latest/feature-table/
- DuckDB `delta` extension: GitHub tags stop at v0.4.0; the extension is now built per DuckDB
  release and autoloaded; platforms limited to linux/osx/windows amd64+arm64. No
  "experimental" label, but the docs note a kernel regression on OneLake.
  https://duckdb.org/docs/current/core_extensions/delta

8. Python weight: deltalake 1.6.6 wheel 44.9 MiB (cp310-abi3, macOS arm64); required deps
   `arro3-core`, `deprecated`; pyarrow is an optional extra (>=21).
   https://pypi.org/pypi/deltalake/json

9. Querying from DuckDB: `delta_scan` does "skipping complete files (based on Delta partition
   information)" and "skipping row groups in file (based on Parquet metadata)" with
   `PUSHDOWN_FILTERS`; time travel `AT (VERSION => n)` or `ATTACH ... VERSION n`; timestamp
   travel not documented. https://duckdb.org/docs/current/core_extensions/delta

## SQLite (for comparison)

- Single file, stdlib `sqlite3`. WAL mode: "readers do not block writers and a writer does not
  block readers", "there can only be one writer at a time"; "WAL does not work over a network
  filesystem". https://sqlite.org/wal.html
- Lock waits: `sqlite3.connect(timeout=5.0)` "Default five seconds" before OperationalError.
  https://docs.python.org/3/library/sqlite3.html
- Commit is atomic via journal/WAL; crash recovery is automatic (hot journal rollback).
  https://sqlite.org/lockingv3.html
- Row deletes are in place; `VACUUM` reclaims space. No snapshots or time travel, no object
  storage path, no file pruning story; DuckDB reads it through the `sqlite` extension.
- Blobs (attachments) would still live beside it as files, as today.

## Gotchas for slarc's usage shape

- DuckLake with a DuckDB-file catalog cannot serve `search`/`serve` while a pull is running in
  another process: DuckDB's file lock is one writer process or many readers, never both. Use a
  SQLite catalog (`ducklake:sqlite:archive.sqlite`) from day one; the data path layout is the
  same, so this is a catalog choice, not a format choice.
- With a SQLite catalog, DuckLake attaches/detaches SQLite per query and retries on write
  locks; a long `pull` holding many small transactions should not starve readers because each
  commit is short, but two pulls on the same machine will serialize and may hit the retry limit
  (10 attempts, 100 ms base, 1.5x backoff, under 6 s total). Keep a process lock anyway.
- DuckLake never deletes data by itself. `rm` of a conversation is a DELETE (inlined or a
  delete file), then `rewrite_data_files` with a low `rewrite_delete_threshold` for that table
  (or a conversation-partitioned table so whole files drop), then `expire_snapshots`, then
  `cleanup_old_files(older_than)`. Blob deletion stays a slarc job. Time travel to before the
  `rm` remains possible until snapshots expire; decide whether that is acceptable.
- Data inlining means 30 tiny commits per hour write rows into the SQLite catalog, not Parquet.
  Run `CHECKPOINT` (flush + merge + expire + cleanup) on a schedule, from the pull process,
  because nothing runs automatically after writes. Catalog table growth per snapshot is small
  but unbounded until expiry.
- DuckLake deletion vectors are experimental; leave `write_deletion_vectors` off.
- Spec 1.0 is new (2026-04). The extension version is pinned to the DuckDB release, and the
  catalog does not auto-migrate across extension versions any more, so DuckDB upgrades become
  explicit migration steps.
- Iceberg via PyIceberg today cannot compact data files, cannot delete expired or orphaned
  files, and deletes are copy-on-write with the old files left on disk. Every hourly commit adds
  three metadata files. slarc would own the file GC. DuckDB's Iceberg writer only works against
  a REST catalog, so DuckDB cannot be the writer for a local directory.
- Delta is the simplest no-catalog option and its maintenance (compact, vacuum, log cleanup) is
  all in Python, but deletes rewrite whole Parquet files (fine at this size), the DuckDB writer
  is append only, and the package is 45 MiB. S3 conditional put is now default, so a later move
  needs no DynamoDB.
- All three DuckDB extensions (`ducklake`, `sqlite`, `iceberg`, `delta`, `httpfs`) are
  downloaded on first use; a fresh machine needs network once, or a pre-seeded
  `extension_directory`.
- Every format relies on a retention window before physical deletion so that in-flight readers
  on old snapshots are safe. Hourly pulls and ad-hoc `search` make a window of hours generous;
  the defaults (DuckLake: none set; Delta: 7 days) are tuned for shared warehouses.
- SQLite alone meets crash safety and multi-process reads on one machine with the least
  machinery, but gives up time travel, Parquet-based pruning and any S3 story.

## Could not confirm

- DuckLake: how long the SQLite catalog retry timeout is, and whether it is configurable
  separately from `ducklake_retry_*`.
- DuckLake: whether a `READ_ONLY` attach to a DuckDB-file catalog can coexist with a writer
  process (DuckDB's general docs say no; not tested here).
- DuckLake: exact catalog-row growth per snapshot and the cost of `ducklake_snapshot_changes`
  scans at 100k+ snapshots.
- DuckLake extension version numbering: the GitHub repo has no releases or tags; versions
  track DuckDB.
- DuckDB `delta` extension: which DuckDB release introduced blind-append writes (GitHub tags
  end at v0.4.0; docs now describe writes).
- delta-rs: the default value of `delta.checkpointInterval` when the property is unset (the
  Delta protocol default is 10; not verified in delta-rs source).
- delta-rs docs still describe DynamoDB locking and `AWS_S3_ALLOW_UNSAFE_RENAME`; the code and
  discussion say DynamoDB is removed in 1.0. Treat the docs pages as stale.
- PyIceberg: whether writes really require pyarrow (inferred from imports in `delete`/`append`
  code paths, not from docs).
- DuckDB `iceberg_scan` file pruning details for direct (catalog-less) reads.
- Whether PyIceberg's `expire_snapshots` will gain file deletion before 1.0, and the merge
  status of PR #3361 (orphan files) and #3474 (DV writer), both open as of 2026-10-09.
