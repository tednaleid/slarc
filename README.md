# slarc

Archive the Slack conversations you can see into a local archive you own: searchable with `slarc search` or DuckDB, and re-renderable as HTML.

slarc logs in with the session your Slack desktop app already holds, so it needs no Slack app install and no admin approval. It only reads from Slack; it never posts, reacts or marks anything read.

Status: early development. `docs/spec.md` describes what slarc does and `docs/architecture.md` how it is built.

## Install

```sh
brew install tednaleid/slarc/slarc
```

Or with uv, which needs Python 3.15:

```sh
uv tool install git+https://github.com/tednaleid/slarc
```

## Usage

```sh
slarc auth                                                                    # the Slack sessions found, and who you are in each workspace
slarc pull https://yourworkspace.slack.com/archives/C0123456789              # pull a conversation and start tracking it
slarc pull https://yourworkspace.slack.com/archives/C0123456789/p1712345678000100  # a thread or single message
slarc pull                                                                    # refresh everything tracked
slarc pull acme                                                               # refresh everything tracked in workspace acme
slarc pull --full                                                             # re-read everything tracked, catching edits and deletes
slarc pull acme --retry-skipped                                               # also try again the downloads skipped in acme
slarc list                                                                    # the archive, its config files and each tracked item
slarc search "deploy" --in '#general'                                         # archived messages, newest first, with their Slack links
slarc sql "SELECT channel_id, count(*) FROM message GROUP BY 1"               # DuckDB over the archive's views
slarc export https://yourworkspace.slack.com/archives/C0123456789 --out ./share  # static HTML
slarc export acme --open                                                      # static HTML of everything tracked in workspace acme, opened in the browser
slarc rm https://yourworkspace.slack.com/archives/C0123456789                 # stop tracking it and delete what only it kept (asks first)
slarc --take-ownership list                                                   # take the archive over from the machine that last used it, once it has synced
```

The first `pull` offers to create an archive (a directory with `slarc.toml`) in the current directory. Every command finds the archive from `--root`, then `SLARC_ARCHIVE`, then the current directory or its parents, then `default_archive` in the global config.

## The archive

An archive is a DuckLake: Parquet data files plus a SQLite catalog, read and written through DuckDB. Its layout:

```
<archive>/
  slarc.toml          settings (portable, no absolute paths)
  catalog.sqlite      the DuckLake catalog (plus -wal and -shm files while a command runs)
  data/               Parquet data files, managed by DuckLake
  blobs/sha256/...    downloaded files, avatars and emoji, named by content hash
```

A command that only reads, such as `search` or `sql`, cannot checkpoint the catalog through its read-only connection, so it may leave an empty `-wal` file and a small `-shm` file beside the catalog; they are harmless and expected.

The archive is read through slarc (`search`, `sql`, `export`) or through DuckDB, not with `rg` or `jq`, and it is not meant to be kept in git. Every pull commits in small steps, so a crash leaves exactly what was last committed. `slarc rm` deletes what only the removed items kept; the bytes are physically deleted when it exits, or by the next pull or rm that finds no other slarc command running.

The first run downloads two DuckDB extensions (`ducklake` and `sqlite_scanner`, about 27 MB each) into `~/.cache/slarc/duckdb`. `just install` fetches them too, so a machine set up while online works offline afterwards.

### Keep the archive on local disk

One machine opens an archive at a time, and slarc's locks work per machine. Keep the archive on local disk, outside synced folders such as Dropbox. SQLite's write-ahead log files and Parquet files that sync at different times can leave a synced copy inconsistent, and two machines with the archive open at once produce a conflicted copy of the catalog.

If you keep an archive in a synced folder anyway:

- Mark the archive folder as available offline (in Dropbox, "Make available offline") so no file is online-only when slarc reads it. slarc does not test online-only files.
- If a command stops with "This archive has data files but no catalog.sqlite", the folder's data files have arrived before the catalog. Wait until the folder has finished syncing and run the command again. slarc creates a catalog only in an archive whose `data/` and `blobs/` hold no files, so to start an archive over, delete `data/` and `blobs/` along with `catalog.sqlite`.
- The catalog records which machine owns the archive. A command on another machine stops with a message naming the owner. Wait until the synced folder has finished syncing, then run the command again with `--take-ownership`, for example `slarc --take-ownership list`. That option says every change from the previous machine has arrived; slarc cannot check that. The run that takes ownership first checks that every data file the catalog references is present, and stops if any is missing. It also skips deleting orphaned files and unreferenced blobs, because files that synced ahead of the catalog look like orphans. The next run on the new machine behaves normally.

Automated access can trigger security alerts in managed Slack workspaces. Check your organization's policies before using slarc there.

## License

MIT
