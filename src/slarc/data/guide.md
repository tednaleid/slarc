# slarc for agents and scripts

Read this whole guide once per session; the sections below answer most errors. The first
section alone is enough to search, query with SQL and read a file.

## Start here

- slarc keeps a local archive of the Slack conversations a person can see. `search`, `sql`,
  `list` (without `--remote`), `export`, `file` and `guide` only read it (`export` and `file` also write the files
  you ask for), so run them freely.
- `slarc pull`, `slarc listen` and `slarc list --remote` contact Slack, and `pull` and
  `listen` write the archive; `listen` runs until stopped. Run them only when the user asks.
- **Never run `slarc rm` unless the user explicitly asked to remove that exact item. Show them
  the command and ask first, and never pass `--yes`.**
- `slarc sql` runs DuckDB SQL, not SQLite: there is no `datetime()`; write
  `now() - INTERVAL 7 DAY` and `strftime(posted_at, '%Y-%m-%d')`.
- Query the view `message_readable`, one row per message. Its columns: `workspace`,
  `conversation` (`#name`, `@user` for a DM, or a group DM's name), `author` (display name),
  `author_real_name`, `posted_at` (TIMESTAMPTZ), `text`, `link` (the Slack link to give the
  user), `ts` (the message's id), `thread_ts` (its thread parent's `ts`, null outside threads),
  `reactions`, `files` (a list of `{id, name}`), `deleted_at`, `first_seen_at` (when slarc
  first stored it), `team_id` and `channel_id`.
- `first_seen_at` and `deleted_at` are UTC text such as `2026-10-20T14:00:00Z`; compare them as
  text. `posted_at` compares with a date string (`posted_at >= '2026-10-20'`) or an interval.
- Add `--format jsonl` to `search`, `sql` and `list`: one JSON object per line, never cut.
- Exit 0: success. 1: nothing found. 2: an error, such as an unknown name or a bad query;
  stderr says what. 130: interrupted. Results go to stdout; notes and errors go to stderr.
- Names such as `#general`, `@ada`, `acme` and `F0123456789` below are placeholders; use the
  user's, or find them with `slarc list --format jsonl`.

```sh
slarc search "deploy" --in '#general' --since 7d --format jsonl
slarc sql --format jsonl "SELECT conversation, author_real_name, posted_at, text, link FROM message_readable WHERE posted_at >= now() - INTERVAL 7 DAY ORDER BY posted_at"
slarc file F0123456789 --out ~/Downloads/proposal.pdf
```

## Querying with SQL

`message_readable` covers most questions. Join it to `message` on `(team_id, channel_id, ts)`
for raw fields such as `text_mrkdwn`, `is_reply` and `reply_count`. Every view and column has a
description:

```sh
slarc sql "SELECT view_name, comment FROM duckdb_views() WHERE NOT internal"
slarc sql "SELECT column_name, data_type, comment FROM duckdb_columns() WHERE table_name = 'message_readable'"
```

Who wrote most in a conversation, and a thread in order:

```sh
slarc sql --format jsonl "SELECT author_real_name, count(*) AS messages FROM message_readable WHERE conversation = '#general' GROUP BY author_real_name ORDER BY messages DESC"
slarc sql --format jsonl "SELECT author_real_name, posted_at, text, link FROM message_readable WHERE thread_ts = '1712345678.000100' ORDER BY ts"
```

Messages per day, and the files messages carry:

```sh
slarc sql --format jsonl "SELECT strftime(posted_at, '%Y-%m-%d') AS day, count(*) AS messages FROM message_readable WHERE posted_at >= '2026-10-01' GROUP BY day ORDER BY day"
slarc sql --format jsonl "SELECT link, f.id, f.name FROM message_readable, unnest(files) AS t(f) WHERE conversation = '#general'"
```

What is new since the user last caught up: ask them when that was. `first_seen_at` is when slarc
first stored the message, by a pull or by listen, so this also finds older messages that a later
pull brought in, which a `posted_at` filter would miss:

```sh
slarc sql --format jsonl "SELECT conversation, author_real_name, text, link FROM message_readable WHERE first_seen_at >= '2026-10-20T14:00:00Z' ORDER BY posted_at"
```

A file's details and why it was not downloaded:

```sh
slarc sql --format jsonl "SELECT f.id, f.name, f.mimetype, f.size, f.blob IS NOT NULL AS downloaded, d.status, d.last_error FROM file f LEFT JOIN download d ON d.team_id = f.team_id AND d.target = f.id WHERE f.id = 'F0123456789'"
```

`slarc sql --help` shows more examples.

## Search, output and exit codes

- `search` returns 50 matches by default, newest first; `--limit 0` returns every one. Its
  jsonl rows carry the `message_readable` columns. It also matches the first 200 KB of
  downloaded snippets and text files: a hit whose words were found only in a file gives that
  file's id in `matched_file`; read the file with `slarc file`.
- `--in` takes `#channel`, `@user` for a DM, or a conversation id; `--from` and `--mentions`
  take a user name, id or `me`. A name the archive does not know exits 2.
- `csv` prints a header row then the rows. `table`, the default, is for people.
- Exit 1 also means no message of an `export` falls in its window, or `slarc file` names a file
  the archive never downloaded (stderr says why).
- `slarc file` writes to stdout, or with `--out` to that exact path, creating its directories;
  a different file already there is never replaced (exit 2).

## Recipes

What the archive holds, one tracked conversation or thread per line:

```sh
slarc list --format jsonl
```

An item's `retention` (such as `30d`, or `forever`) says how long the archive keeps its threads:
a thread with no message newer than that is removed, so the archive can hold less than Slack.

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
slarc search --in @ada --since 2026-10-20 --until 2026-10-20 --format jsonl
slarc search --from ada --since 2026-10-20 --until 2026-10-20 --format jsonl
slarc search --mentions ada --since 2026-10-20 --until 2026-10-20 --format jsonl
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

## Windows

`--since` and `--until` take a date (`2026-10-19`), a time (`2026-10-19T14:30`), either with
an offset (`Z`, `+02:00`) or else in this machine's time zone, or an age counted back from now
(`12h`, `7d`, `2w`). A date alone as `--until` runs through the end of that day.

## Reading markdown exports

- `export --format markdown --out -` prints the pages to stdout. Each page starts with a
  header giving its window, time zone and message and thread counts, so you can judge its size
  before reading on. With `--out DIR`, pages are named after their conversation (`general.md`,
  `@ada.md`) and `index.md` lists them with their counts.
- Authors read `**Real Name** (display name)`; use the real name when writing for people.
  `(context)` marks a thread parent older than the window, shown for its replies, and
  `(edited)` and `(deleted ...)` mark changed messages. A linked Slack message is quoted with
  its author and link. A snippet's text follows its file line in a fenced block, cut at 200 KB
  with a note naming the `slarc file` that gives the rest.
- Reactions read `:name: count`, joined by commas.
