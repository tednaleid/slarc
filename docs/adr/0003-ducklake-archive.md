# The archive is a DuckLake: Parquet data files and a SQLite catalog

An archive stores Slack data as a DuckLake: Parquet data files under `data/` and a SQLite catalog, `catalog.sqlite`, beside downloaded files named by their content hash under `blobs/`. slarc reads and writes it through the `duckdb` package it uses for queries. A commit is one catalog transaction, so a batch is part of the archive if and only if its transaction committed. Tracked items and download states are rows updated in place, each tracked item carries a revision that every commit checks so a removal cannot be undone by a pull that started earlier, and `slarc rm` is a DELETE followed by maintenance that physically removes the bytes. Two `flock` locks in the XDG cache directory order writers and keep physical deletion away from running commands, and one machine owns an archive at a time. We chose it because an archive must stay correct under crashes, concurrent commands and removals, and an open table format provides atomic multi-table commits, snapshots and file maintenance that slarc would otherwise build by hand. `docs/architecture.md` describes how slarc uses it.

## Considered Options

- Append-only JSONL files with a commit log: readable with `rg` and `jq` and mergeable in git, but safe commits, removals and crash repair need a hand-built transaction layer (batches, commit records, a committed-only filter on every read, sweeps). It also measured about four times the lake's size, with search and conversation counts about three times slower on a 1M-message archive.
- Iceberg: without a server, writes mean PyIceberg over a SQLite catalog that its docs mark as development only, while DuckDB's `iceberg` extension writes only through a REST catalog. PyIceberg has no data compaction and no orphan-file cleanup, deletes are copy-on-write, and it needs pyarrow (35 MiB) to write.
- Delta Lake: commits need no catalog and the format is stable, but each table commits on its own, so a message batch and its tracked-item update cannot be one atomic commit. Deletes are copy-on-write, physical removal waits on `vacuum` retention (7 days by default), DuckDB's `delta` extension only appends, and delta-rs adds 45 MiB.
- Plain SQLite, as slackdump uses: crash safety and one-machine multi-process reads with the least machinery, but no file pruning for large scans and no path to publishing a read-only copy on object storage.

## Consequences

- The archive is not meant for git: it is not merged or diffed, and blobs make it too large for a repository. It is read through slarc (`search`, `sql`, `export`) or DuckDB, not with `rg` or `jq`.
- Edit history lives in `message_version` rows, which are kept. Snapshots are expired at every idle maintenance, so there is no time travel.
- Physical deletion uses no time windows. It runs only when an exclusive read lock shows no other slarc command has the archive open.
- One machine opens an archive at a time. The catalog records the owning machine, every command checks it, and `--take-ownership` hands the archive over once a synced folder has finished syncing. The README recommends local disk over a synced folder.
- `duckdb` is pinned exactly, because the ducklake extension is built for one DuckDB release and the catalog does not migrate itself. The `ducklake` and `sqlite_scanner` extensions download on first use into the XDG cache.
- DuckLake is young (spec 1.0 in April 2026). slarc avoids its `older_than` time windows and the empty read after an expire under an open reader, rather than relying on fixes.
- Storage stays behind the `ArchiveStore`, `QueryIndex` and `ArchiveLock` ports, so a different format means new adapters, not new service code. `slarc sql` ties the command to DuckDB, not to DuckLake.
- The data files could later move to object storage, or a frozen read-only copy could be published there for others to query; nothing is built for either.
