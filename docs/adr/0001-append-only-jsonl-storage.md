# Append-only JSONL files are the only stored state

An archive stores Slack data as append-only JSONL files split by month, plus downloaded files named by their content hash. There is no database file. DuckDB queries the JSONL directly at read time, and any index it builds lives in `~/.cache/slarc`, outside the archive. We chose this over SQLite or a persisted DuckDB file so an archive can be kept in git with small, readable diffs, and so `rg`, `jq` and agents can search it with no slarc code at all. Every edit, reaction change or delete becomes a new line; existing lines are never rewritten.

## Considered Options

- SQLite (as slackdump uses): one binary file that changes on every pull, so git stores a new copy each time and diffs are unreadable.
- A persisted DuckDB or Parquet file: same git problem, and a second format to keep in sync with the source of truth.

## Consequences

- Reading the current state of a message means taking its last line; every query goes through views that do this.
- Startup cost for `search` and `serve` grows with archive size. If that becomes a problem, the fix is a rebuildable cache in `~/.cache/slarc`, never a database inside the archive.
- slarc knows nothing about git. It only writes files that git handles well, and reading tolerates the duplicate or out-of-order lines a git merge can produce.
