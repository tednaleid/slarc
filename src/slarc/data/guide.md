# slarc for agents and scripts

slarc keeps a local archive of the Slack conversations a person can see: messages, threads,
files and the people in them. The commands below read that archive; none of them contact Slack.

## What you may run

- `slarc search`, `slarc sql`, `slarc list`, `slarc export` and `slarc guide` only read the
  archive (export also writes the files you ask it for). Run them freely.
- `slarc pull` reads Slack with the user's own session, can take minutes, and writes to the
  archive. Run it only when the user asks for fresh data.
- **`slarc rm` permanently deletes archived messages and files. Never run it unless the user
  has explicitly asked you to remove that exact item. Show them the command and ask first, and
  never pass `--yes` yourself.**

## Recipes

Names such as `#general`, `@arun`, `acme` and the links below are placeholders; use the ones
the user gives you, or find them with `slarc list`.

What the archive holds, one tracked conversation or thread per line:

```sh
slarc list --format jsonl
```

Catch up on a conversation, or a whole workspace, since a date, as markdown with threads
quoted under their parents:

```sh
slarc export '#general' --since 2026-10-19 --format markdown --out -
slarc export acme --since 7d --format markdown --out -
```

What mentions me (directly or through a usergroup) this week:

```sh
slarc search --mentions me --since 7d --format jsonl
```

A conversation with a person on a day: their DM, then the channels they posted in or were
mentioned in. Each hit's `link` or `thread_ts` leads to its thread.

```sh
slarc search --in @arun --since 2026-10-20 --until 2026-10-20 --format jsonl
slarc search --from arun --since 2026-10-20 --until 2026-10-20 --format jsonl
slarc search --mentions arun --since 2026-10-20 --until 2026-10-20 --format jsonl
```

Find something by its words, then read the whole thread a hit is in:

```sh
slarc search "deploy" --in '#general' --format jsonl --limit 20
slarc export https://acme.slack.com/archives/C0123456789/p1712345678000100 --format markdown --out -
```

What surrounds a message: export the conversation its link is in, for a window around it. The
message the link names is marked `(linked)`.

```sh
slarc export https://acme.slack.com/archives/C0123456789/p1712345678000100 --channel --since 2026-10-19T10:00 --until 2026-10-19T14:00 --format markdown --out -
```

What the latest pull added:

```sh
slarc sql --format jsonl "SELECT conversation, author, text, link FROM message_readable WHERE first_seen_at >= (SELECT max(started) FROM run WHERE command = 'pull') ORDER BY posted_at"
```

## Windows

`--since` and `--until` take a date (`2026-10-19`), a time (`2026-10-19T14:30`), either with
an offset (`Z`, `+02:00`) or else in this machine's time zone, or an age counted back from now
(`12h`, `7d`, `2w`). A date alone as `--until` runs through the end of that day.

## Output and exit codes

- `--format jsonl` (on `search`, `sql` and `list`) prints one JSON object per line and never
  truncates; `csv` prints a header row then the rows. `table`, the default, is for people.
- `export --format markdown --out -` prints the pages to stdout. Each page starts with a
  header giving its window, time zone and message and thread counts, so you can judge its size
  before reading on.
- Stdout carries only results. Notes, such as `Showing 50; --limit for more.`, and errors go
  to stderr.
- Exit 0: success. Exit 1: `search` found nothing. Exit 2: an error, such as a selector that
  names nothing or a bad query. Exit 130: interrupted.
- `search` returns 50 matches by default, newest first; `--limit 0` returns every one.

## Reading results

- `link` is the message's Slack link; give it to the user so they can open the message.
- `ts` is Slack's id for a message and its posting time; `posted_at` is that time with an
  offset. `thread_ts` is the thread's parent `ts`, on the parent and on every reply, and null
  outside threads.
- `conversation` is `#name`, `@user` for a DM, or a group DM's name.
- In markdown, `(context)` marks a thread parent older than the window, shown for its replies,
  and `(edited)` and `(deleted ...)` mark changed messages.

## Querying with SQL

`slarc sql` runs DuckDB SQL over views of the archive. Start with `message_readable`: one row
per message with `workspace`, `conversation`, `author`, `text`, `link`, `posted_at` and
`thread_ts`. Join it to `message` on `(team_id, channel_id, ts)` for raw fields such as
`text_mrkdwn`, `is_reply` and `reply_count`. List the views and their columns, each with a
description:

```sh
slarc sql "SELECT view_name, comment FROM duckdb_views() WHERE NOT internal"
slarc sql "SELECT column_name, data_type, comment FROM duckdb_columns() WHERE table_name = 'message_readable'"
```

`slarc sql --help` shows more examples.
