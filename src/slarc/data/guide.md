# slarc for agents and scripts

slarc keeps a local archive of the Slack conversations a person can see: messages, threads,
files and the people in them. The commands below read that archive; none of them contact Slack.

## What you may run

- `slarc search`, `slarc sql`, `slarc list`, `slarc export`, `slarc file` and `slarc guide`
  only read the archive (export and file also write the files you ask them for). Run them
  freely.
- `slarc pull` reads Slack with the user's own session, can take minutes, and writes to the
  archive. Run it only when the user asks for fresh data.
- **`slarc rm` permanently deletes archived messages and files. Never run it unless the user
  has explicitly asked you to remove that exact item. Show them the command and ask first, and
  never pass `--yes` yourself.**

## Recipes

Names such as `#general`, `@ada`, `acme`, `F0123456789` and the links below are placeholders;
use the ones the user gives you, or find them with `slarc list`.

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
slarc search --in @ada --since 2026-10-20 --until 2026-10-20 --format jsonl
slarc search --from ada --since 2026-10-20 --until 2026-10-20 --format jsonl
slarc search --mentions ada --since 2026-10-20 --until 2026-10-20 --format jsonl
```

Find something by its words, then read the whole thread a hit is in. Search also matches the
first 200 KB of downloaded snippets and text files, returning the message that carries them:

```sh
slarc search "deploy" --in '#general' --format jsonl --limit 20
slarc export https://acme.slack.com/archives/C0123456789/p1712345678000100 --format markdown --out -
```

What surrounds a message: export the conversation its link is in, for a window around it. The
message the link names is marked `(linked)`.

```sh
slarc export https://acme.slack.com/archives/C0123456789/p1712345678000100 --channel --since 2026-10-19T10:00 --until 2026-10-19T14:00 --format markdown --out -
```

What is new since the user last caught up: ask them when that was. `first_seen_at` is when
a pull stored the message, in UTC, so this also finds older messages that a later pull brought
in, which `--since` (posting time) would miss:

```sh
slarc sql --format jsonl "SELECT conversation, author_real_name, text, link FROM message_readable WHERE first_seen_at >= '2026-10-20T14:00:00Z' ORDER BY posted_at"
```

A file a message carries, such as a PDF, an image or a snippet's full text. Each message's
`files` gives the ids and names. `--out` is the exact path to write, with its directories
created; a different file already there is never replaced (exit 2). Without `--out` the bytes go
to stdout:

```sh
slarc file F0123456789 --out ~/Downloads/proposal.pdf
slarc sql --format jsonl "SELECT f.id, f.name, f.mimetype, f.size, f.blob IS NOT NULL AS downloaded, d.status, d.last_error FROM file f LEFT JOIN download d ON d.team_id = f.team_id AND d.target = f.id WHERE f.id = 'F0123456789'"
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
  before reading on. With `--out DIR`, pages are named after their conversation (`general.md`,
  `@ada.md`) and `index.md` lists them with their counts.
- Stdout carries only results. Notes, such as `Showing 50; --limit for more.`, and errors go
  to stderr.
- Exit 0: success. Exit 1: `search` found nothing, no message of an `export` falls in its
  window, or `slarc file` names a file the archive never downloaded (stderr says why). Exit 2:
  an error, such as a conversation, user or file the archive does not know, or a bad query.
  Exit 130: interrupted.
- `search` returns 50 matches by default, newest first; `--limit 0` returns every one.

## Reading results

- `link` is the message's Slack link; give it to the user so they can open the message.
- `ts` is Slack's id for a message and its posting time; `posted_at` is that time with an
  offset. `thread_ts` is the thread's parent `ts`, on the parent and on every reply, and null
  outside threads.
- `conversation` is `#name`, `@user` for a DM, or a group DM's name.
- `author` is the display name people see in Slack; `author_real_name` is the person's real
  name. Use the real name when writing for people.
- A search hit whose words were found only in a file it carries gives that file's id in
  `matched_file`; read the file with `slarc file`.
- `reactions` reads `:name: count`, joined by commas. `files` lists each file's `id` and
  `name`; `slarc file` takes the id.
- In markdown, authors read `**Real Name** (display name)`. `(context)` marks a thread parent
  older than the window, shown for its replies, and `(edited)` and `(deleted ...)` mark changed
  messages. A linked Slack message is quoted with its author and link. A snippet's text follows
  its file line in a fenced block, cut at 200 KB with a note naming the `slarc file` that gives
  the rest.

## Querying with SQL

`slarc sql` runs DuckDB SQL over views of the archive. Start with `message_readable`: one row
per message with `workspace`, `conversation`, `author`, `author_real_name`, `text`, `link`,
`posted_at`, `thread_ts`, `reactions` and `files`. Join it to `message` on `(team_id,
channel_id, ts)` for raw fields such as `text_mrkdwn`, `is_reply` and `reply_count`. List the
views and their columns, each with a description:

```sh
slarc sql "SELECT view_name, comment FROM duckdb_views() WHERE NOT internal"
slarc sql "SELECT column_name, data_type, comment FROM duckdb_columns() WHERE table_name = 'message_readable'"
```

`slarc sql --help` shows more examples.
