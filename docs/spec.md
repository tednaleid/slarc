# slarc spec

What slarc does. `docs/architecture.md` describes how the code is built.

## Overview

slarc is a read-only CLI that logs into Slack with the session the user's Slack desktop app already holds, pulls channels, DMs, threads and single messages into an archive that is a DuckLake (Parquet data files plus a SQLite catalog) with a content-addressed blob store, and renders them as static HTML (a local web viewer and PDF are planned). The archive is queried by DuckDB through slarc's views and is meant to live on one machine's local disk. slarc is a client, not a Slack app: nothing is installed in the workspace, no admin approval is needed, and it sees exactly what the user's account sees.

Python 3.15 with uv, `slack_sdk`, Typer and DuckDB. Hexagonal architecture in the Cosmic Python style so every Slack interaction is behind a port with an in-memory fake for tests. Terms follow `GLOSSARY.md`; the larger decisions have ADRs in `docs/adr/`; facts verified against a real workspace are in `docs/slack-facts.md`.

## Goals and non-goals

**Goals**

1. Pull any conversation the user can see (public and private channels, DMs, group DMs), a single thread, or a single message, given a Slack link. Optional date range on conversations.
2. Archive faithfully enough to re-render "like Slack" later: formatting, mentions, emoji (custom, animated, skin tones), reactions, files and snippets with full content, images, link previews, threads, edit status.
3. Pulls are incremental, resumable and idempotent. Re-running is cheap and safe; a crash mid-conversation resumes where it stopped.
4. Ease of use: `slarc pull <link>` works on a fresh machine with no setup beyond a signed-in Slack desktop app.
5. Search from the first release, through `slarc search` and through `slarc sql`.
6. Static HTML export for sharing.
7. Decent speed: a 100k-message channel in minutes, not hours.
8. Fast unit tests that never touch the network.
9. Several workspaces in one archive (`team_id` in every key).

**Non-goals**

- Writing anything to Slack. slarc never calls a method that changes anything. Posting to the test workspace happens only in a separate developer script (see Tests).
- Real-time capture through the Events API or Socket Mode.
- Lists, channel sections and sidebar groups. Canvases, huddles, pins and bookmarks are Someday items in `docs/backlog.md`; until then canvases and huddles render as placeholders where they appear in messages.
- Apps beyond being the author of messages or the other party in a DM.
- Recovering pre-edit message text; Slack never exposes it.
- Git support and reading the archive with `rg` or `jq`. The archive is not meant to be merged, diffed or searched as files (ADR 0003).
- Being sanctioned by Slack (ADR 0001).

## CLI

The CLI is the product's surface, so it stays small and each command does a lot. Built with Typer, with Rich for progress and help formatting.

| Command | Purpose |
| --- | --- |
| `slarc pull [selector ...] [--since DATE] [--until DATE] [--channel] [--full] [--retry-skipped] [--downloads N] [--max-file-size MB] [--yes]` | With links: start tracking what they name and pull it. With a workspace name: refresh everything tracked in that workspace; `--since` and `--until` do not apply to it. Without: refresh everything tracked. Every pull also drains pending and previously failed downloads. `--full` re-reads everything instead of only what is new. `--retry-skipped` tries again the skipped downloads that the pulled items' stored messages reference, without re-reading them |
| `slarc list [SELECTOR...] [--remote]` | Archive location and the config files in effect, then each tracked item with message count, last pull time, and the pending and skipped downloads its messages reference (files, images, authors' avatars and custom emoji). Selectors, as `pull` and `rm` take them, list only what they name, then one line per download their messages reference (workspace, conversation, type, status, attempts, last error, URL), saved ones first and failures last. The URL ends the line unpadded, as a terminal link, so it is never wrapped or cut. A pull that leaves downloads unsaved ends with the `slarc list` that shows why. `--remote` lists every conversation the session can see and marks which are tracked, and takes no selectors |
| `slarc search QUERY [--in CONV] [--from USER]` | Matches with author, conversation, time, snippet and Slack link |
| `slarc export SELECTOR... [--out DIR] [--single-file] [--include-deleted] [--tz ZONE]` | Static HTML of what the links name; a workspace name exports every tracked conversation of that workspace, and every tracked thread or message outside a tracked conversation, to `slarc-export-<workspace>`. PDF output is planned |
| `slarc rm SELECTOR... [--channel] [--yes]` | Stops tracking what the links name and deletes the messages, files, images and blobs that no remaining tracked item needs. It shows a summary and asks first (default No); `--yes` skips the prompt. A selector naming only already removed items finishes an interrupted removal. The removal is committed at once and rm never waits for other commands. The space is freed when rm ends, or, if another slarc command has the archive open, when a later pull or rm finds it idle; rm then says the messages are no longer in the archive and the space is freed when the running slarc command finishes |
| `slarc auth [--paste]` | Lists the Slack sessions found and who the user is in each workspace. `--paste` prompts for a browser token and cookie and stores them in the OS keychain |
| `slarc sql QUERY` | Runs a DuckDB query against the archive's views; the escape hatch |
| `slarc serve` | Planned: a local web viewer over one archive |

`pull`, `list`, `search`, `export` and `rm` accept the same selectors: a Slack link, a conversation id, `#channel-name` or `@user` for a DM. `pull`, `export` and `rm` also accept a workspace name such as `acme`, which stands for everything tracked in that workspace; `search --in` does not. Only `pull` and `auth` need Slack credentials. Every command that opens the archive checks its owner record, the machine that last used it. On another machine it stops with a message naming that host; once the synced folder holding the archive has finished syncing, the global option `slarc --take-ownership <command>` takes the archive over for this machine. Developer tooling (fixture recording, the LevelDB comparison check, test workspace seeding) lives in `just` recipes, not in the CLI.

**Links**

URL and selector parsing is a pure function with property tests.

- `https://<domain>.slack.com/archives/<id>` names a conversation. `C…` is a public or private channel, `G…` a legacy private channel or group DM, `D…` a DM.
- `/archives/<id>/p<16 digits>` names a message; its `ts` is the digits with a dot before the last six (`p1792410693663869` is `1792410693.663869`). A `?thread_ts=` query parameter, present on reply links, names the thread.
- The workspace domain picks which workspace's credentials to use.

**What a pull covers**

- A conversation link pulls that conversation, every thread whose parent falls in the date range, and every user, emoji, file and image they reference. `--since`/`--until` bound the range; both default to unbounded.
- A message link pulls the thread containing that message. A message that is not part of a thread is pulled alone, with its files, images, emoji and users. `--channel` on a message link pulls the whole conversation instead.
- The first pull of a link starts tracking it, with its date range. A bare `slarc pull` refreshes every tracked item within its range. Pulling a tracked conversation again with a wider range widens it and backfills.

## Archive location and config

- An archive is a directory containing `slarc.toml`. One archive can hold several workspaces. There are no named archives.
- Lookup, highest priority first: `--root DIR`; `$SLARC_ARCHIVE`; the nearest `slarc.toml` walking up from the current directory (as git does with `.git`); `default_archive` in the global config.
- If none of those resolves, `pull` creates an archive in the current directory after a one-time confirmation ("No archive here. Create one in /path? [Y/n]"); `--yes` skips it. A bare `pull` never creates an archive and says nothing is tracked; ending the prompt with Ctrl-C or end of input counts as declining. Commands other than `pull` exit with a message saying no archive was found.
- Global state follows XDG: settings in `$XDG_CONFIG_HOME/slarc/config.toml` (`~/.config/slarc/config.toml` on every platform), per-machine state in `$XDG_CACHE_HOME/slarc/` (`~/.cache/slarc`): the archive's lock files under `locks/`, named by the sha256 of its real path, and the DuckDB extensions under `duckdb/`. Pasted credentials go in the OS keychain through `keyring`.
- `slarc.toml` holds only portable settings: no absolute paths and nothing tied to one user, so a copy of an archive works the same on another machine.
- Settings resolve flags, then environment, then `slarc.toml`, then the global file, then built-in defaults, key by key. `slarc list` shows the archive path and which files were read.

```toml
# slarc.toml at the archive root (all keys optional)
[pull]
downloads = 4              # parallel file downloads
max_file_size_mb = 100
active_thread_days = 7     # threads with a reply this recent are re-read on every pull

[export]
tz = "America/Chicago"     # default: the viewer's local time zone
```

## Auth

slarc authenticates as the user with the two credentials the Slack client uses: a per-workspace `xoxc-` token and the `d` session cookie, whose value starts `xoxd-`. Every Web API call sends the token as the bearer token and the cookie in the `Cookie` header: `WebClient(token=xoxc, headers={"Cookie": f"d={xoxd}"})`. With that, every documented method behaves as it does for an app (verified).

**Credential sources, in priority order**

| Source | How | Notes |
| --- | --- | --- |
| Environment | `SLARC_XOXC` and `SLARC_XOXD` | For scripted runs; applies to the workspace a link names |
| Pasted pair | `slarc auth --paste` stores token and cookie in the OS keychain, keyed by `team_id` | Fallback when the desktop app is not installed or its store cannot be read; the only option on Windows |
| Slack desktop app session | Read the app's `Cookies` SQLite DB and decrypt the `d` cookie; read `xoxc` tokens per workspace from the app's Local Storage | Default. Re-read on every run, never copied into the archive. Valid as long as the desktop app stays signed in |

**Reading the desktop session**

- Data directory on macOS: `~/Library/Application Support/Slack`, or for the Mac App Store build `~/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack`. On Linux: `~/.config/Slack`, plus the Snap and Flatpak locations slk documents.
- Copy `Cookies` (and its journal) and the `Local Storage/leveldb` directory to a temp directory before reading, so a running Slack is never disturbed.
- Cookie key on macOS: keychain item with service `Slack Safe Storage`, account `Slack App Store Key` for the App Store build or `Slack Key` otherwise, read with `/usr/bin/security find-generic-password -w`. The first read raises a macOS dialog; "Always Allow" silences later runs. On Linux: the same secret from Secret Service or KWallet, or the fixed password `peanuts` for `v10` cookies.
- Decryption: strip the `v10`/`v11` prefix; AES-128-CBC with a key from PBKDF2-HMAC-SHA1 (salt `saltysalt`, 16-byte key, 1003 iterations on macOS, 1 on Linux), IV of 16 spaces; strip PKCS#7 padding; when the Cookies DB `meta.version` is 24 or higher, strip the 32-byte domain-hash prefix. The result must start with `xoxd-`.
- Tokens: Chromium stores the web client's `localStorage` as a LevelDB database. The key `localConfig_v2` holds JSON whose `teams` object maps `team_id` to an object with `token` and `domain`. slarc's own reader, written in-house and targeted at this one key, handles: `.log` files (32 KB blocks, records spanning blocks, write batches of put and delete entries); `.ldb` table files (prefix-compressed keys, restart points, index block, footer, optional Snappy-compressed blocks); Snappy decompression; Chromium's key format `_<origin>\x00\x01localConfig_v2` and the value's one-byte encoding prefix (`0x01` Latin-1, `0x00` UTF-16LE). The entry with the highest sequence number wins; if it is a delete, there is no token. Reading every table file and taking the highest sequence number makes stale, not-yet-compacted files harmless.
- The desktop app holds a token for every workspace it is signed into. slarc uses only the workspace a link or tracked item names and never calls the API for the others.
- If the reader fails, slarc says which step failed and points at `slarc auth --paste`; it never shows a stack trace for this.
- A `just` recipe compares the in-house reader against `ccl_chromium_reader` on the developer's machine and prints only "match" or "mismatch". The library is a throwaway dev dependency, never shipped.

**Token lifecycle**

- Every command that talks to Slack starts with `auth.test` for each workspace it will touch. On `invalid_auth`, `not_authed`, `token_revoked`, `token_expired` or `account_inactive` the CLI names the source that failed and how to refresh it (sign into the desktop app again, or re-paste), then exits non-zero.
- A workspace with no session, or whose credentials Slack rejects, is skipped with a warning while the other workspaces are pulled; the run row's `errors` names it. The pull then exits non-zero if no workspace was pulled or any credentials were rejected.
- The archive records only `team_id`, the user id from `auth.test`, the credential source and the time of the last successful `auth.test`, in the run row.
- Signing out of the desktop app invalidates the pair; slarc never does that itself.

**Rate limits**

- Session tokens get the web client's limits; the 2025 limits on non-Marketplace apps apply to app tokens.
- Per-method token bucket (Tier 3 methods such as `conversations.history` and `conversations.replies` at about 50 per minute; Tier 2 methods such as `search.messages` and `users.list` at about 20 per minute) plus honoring `Retry-After` on 429, up to three retries per call. Pagination always passes through the limiter.
- Automated access can trigger Slack security alerts in managed workspaces. Accepted; documented in the README.

**What the Slack adapter must not do**

- No `client.*` or Edge endpoints. Documented methods only, which keeps the fake gateway honest and the raw JSON shapes stable.
- No TLS fingerprinting or user-agent spoofing. Send an honest `User-Agent` such as `slarc/0.1`.
- No mutating methods.

## Slack data-model facts

Items marked (verified) were confirmed against the test workspace; the rest come from docs.slack.dev and should be checked when the code that depends on them is written.

**Identity**

- A message's natural key is `(team_id, channel_id, ts)`. `ts` is a string like `"1792410693.663869"`; never a float.
- `conversations.info` classifies a conversation: `is_im`, `is_mpim`, `is_private`, `is_archived` (Slack-archived), and returns the caller's `last_read` (verified). DMs have no `name`, only `user` (the other party). Group DMs have generated names like `mpdm-alice--bob--carol-1`. Display names for both are built from members.

**History, threads, search**

- `conversations.history(channel, oldest, latest, inclusive, limit, cursor)` returns newest first, top-level messages plus broadcast replies only. Page with `response_metadata.next_cursor`.
- Thread parents carry `thread_ts == ts`, `reply_count`, `reply_users`, `reply_users_count`, `latest_reply`, and per-user fields `last_read` and `subscribed` (verified). `conversations.replies(channel, ts)` accepts any message in a thread and returns the parent first.
- A broadcast reply (`subtype: thread_broadcast`) appears in both history and the thread. Stored once, rendered in both places.
- `search.messages` works with a session token and supports `in:<#C…>`, `in:#name`, `is:thread` and `after:YYYY-MM-DD` (verified). Matches carry no `thread_ts` field, but a reply's `permalink` carries `?thread_ts=…` (verified). Search syntax for DMs and group DMs still needs checking.

**Edits and deletes**

- Edits keep the same `ts` and add `edited: {user, ts}` (verified). The original text is not available.
- A polling archiver never sees `message_changed` or `message_deleted` events. A deleted parent that still has replies comes back as `subtype: tombstone`.
- Subtypes to expect: `bot_message`, `me_message`, `channel_join`, `channel_leave`, `channel_topic`, `channel_purpose`, `channel_name`, `channel_archive`, `thread_broadcast`, `pinned_item`, `file_share`, `tombstone`, `huddle_thread`, plus app-specific ones. Render from an allowlist; anything else is a generic system message, never a crash.

**Message content**

- `text` is mrkdwn, not Markdown: `*bold*`, `_italic_`, `~strike~`, `` `code` ``, triple-backtick blocks, `>` quotes, `<url|label>`, `<@U123>`, `<#C123|name>`, `<!subteam^S123>`, `<!here>`, `<!channel>`, `<!date^…>`. `&`, `<`, `>` arrive as `&amp;`, `&lt;`, `&gt;`.
- `blocks` holds the structured version. User-typed messages carry one `rich_text` block (verified): containers `rich_text_section`, `rich_text_list` (bullet or ordered, `indent`), `rich_text_quote`, `rich_text_preformatted`; leaves `text` (style flags bold, italic, strike, code, underline, highlight), `link`, `emoji` (`name` such as `wave::skin-tone-2`, optional `unicode`), `user`, `usergroup`, `channel`, `broadcast`, `date`, `color`, and newer `canvas`, `file`, `message_mention`, `citation`.
- App posts use layout blocks. A `/giphy` post is a message with the poster's `user`, a `bot_id` and `bot_profile` for the giphy app, and blocks `image` (giphy-hosted GIF with `image_url`, `image_width`, `image_height`, `image_bytes`, `is_animated`) and `context` ("Posted using /giphy") (verified). Expect `section`, `header`, `divider`, `image`, `context` and `actions`.
- Link previews live in the legacy `attachments` array: `from_url`, `original_url`, `title`, `title_link`, `text`, `service_name`, `service_icon`, `thumb_url`, `thumb_width/height`, `image_url`, `author_*`, `footer`, `color` (verified). When the author removes a preview, the attachment is absent on the next read.
- Store `text`, `blocks` and `attachments` verbatim. Render from `blocks` first and fall back to a mrkdwn parser for messages without blocks. Unknown block or element types render as escaped text.

**Reactions**

- `reactions: [{name, users[], count}]`. Skin-tone variants are separate entries (`wave` and `wave::skin-tone-3`) that the Slack client shows as one grouped reaction (verified). `users` may be truncated relative to `count`, so store `count` rather than deriving it.

**Files**

- File objects carry `id, name, title, mimetype, filetype, user, size, mode, is_external, url_private, url_private_download, permalink, thumb_*, original_w/h`. `mode` is `hosted`, `snippet`, `post`, `external`, or the redacted states `hidden_by_limit` and `tombstone`.
- Download `url_private_download` with the bearer token and `d` cookie, streaming to the blob store while hashing. Snippets download in full; their `preview` is truncated (verified). The response's `Content-Type` for snippets is `application/force-download`, so the file object's `mimetype` is the source of truth (verified).
- A deleted file appears inline as `mode: tombstone`, and `files.info` returns `file_deleted` (verified). Free-plan workspaces redact old files as `hidden_by_limit` and stop returning messages past the retention window.
- External files (Google Drive and the like, `mode: external`) are linked, not downloaded.

**Users, bots, emoji**

- `users.list` / `users.info`: `id, team_id, name, real_name, deleted, is_bot, is_app_user, tz, tz_label, tz_offset`, `profile.{display_name, real_name, email, image_24 … image_512, image_original}`. `profile.display_name` is often empty (verified), so display falls back to `profile.real_name`, then `real_name`, then `name`.
- Messages may inline a `user_profile` snapshot of name and avatar at post time; none has appeared in the test workspace. Keep it when present.
- Bot authors carry `bot_id` and `bot_profile`; `bots.info` fills the rest.
- Guests and Slack Connect users come from other teams (`user_team` or `team` differs from the archiving `team_id`).
- `emoji.list` maps custom names to an image URL or to `alias:<name>` (verified). An alias can point at a standard emoji (`white_square` → `alias:white_large_square`), so resolution falls back to the standard table. Custom emoji may be animated GIFs. Deleted custom emoji vanish from the list, so every image ever seen is kept.
- Standard emoji come from a table generated from iamcal/emoji-data (what Slack uses, including skin-tone variants) and committed to the repo.

## Pulling

**Order within a pull**

1. Resolve the archive and the tracked items to pull.
2. `auth.test` each workspace involved.
3. Refresh the workspace directory: `users.list`, `emoji.list`, `usergroups.list`, and `conversations.info` for each conversation involved. Directory first, so `plain` can resolve names.
4. Pull each tracked item (below), adding a message version whenever its hash is new. Each tracked item, directory refresh and history page is its own commit, so a killed pull keeps what it committed.
5. Download pending files and images (below).
6. Commit one run row recording the command, counts, auth and errors, then run maintenance (Storage).

**Tracked conversation**

1. New history: `conversations.history` from the newest stored `ts` (or `--since` on the first pull) up to `--until` or now.
2. Replies under old parents: `search.messages` with `in:<#channel> after:<UTC date of the previous pull minus two days>`, paged. Slack treats `after:` as exclusive and evaluates it in the user's time zone, so two days of margin keep replies from slipping through; the extra re-reads change nothing because unchanged messages add no rows. For every match whose permalink carries a `thread_ts` that was not covered by step 1 or 3, re-read that thread.
3. Active threads: re-read every thread with a stored reply newer than `active_thread_days` (default 7).
4. Every thread whose parent arrived in step 1 with replies is read with `conversations.replies`.
5. `--full` replaces steps 1 and 2 with a complete re-read of the conversation's range and every thread in it.

**Slack-archived conversation**: a pull that finishes a conversation while Slack reports it archived (`is_archived` in `conversations.info`) records `archived: true`, unless a thread read failed and is left to retry. A later pull that finds the item `archived: true` and the conversation still archived reads nothing (no history, search or threads) and records the item as pulled, `0 read, 0 new`. A `--since` or `--until` that widens its range is still read. A conversation no longer archived records `archived: false` and is refreshed as usual. `--full` re-reads an archived conversation.

**Tracked thread or message**: re-read with `conversations.replies` (a lone message comes back as a one-element thread) on every pull.

**Deletes**

- A message is recorded as deleted only when (a) Slack returns it as `subtype: tombstone` (which is a new version, not a delete), or (b) it is missing from a range slarc fully re-read in this run (a whole thread or a `--full` range) and that range lies entirely inside what Slack still serves: if the oldest message Slack returned is newer than the range's start, only the part from that message onward counts.
- A conversation or thread the current session cannot see (`channel_not_found`, `not_in_channel`, `thread_not_found`) is skipped with a warning and never produces deletes, so losing access to a conversation never deletes what the archive holds.

**Downloads**

- Files: everything referenced, video included, up to `max_file_size_mb` (default 100). Larger files are recorded as `too_large` and listed by `slarc list`; raising the limit picks them up on the next pull.
- Images: every image a message references is downloaded under the same limit: `image` blocks (giphy), attachment `image_url` and `thumb_url`, `service_icon`, `bot_profile` icons, custom emoji images and user avatars (`profile.image_512`). External URLs rot, so the archive keeps its own copy.
- Downloads run in parallel (`--downloads N`, default 4), with at most 2 at a time from any one host other than Slack's file host. A download answered with HTTP 429 holds its host for the `Retry-After` (seconds or a date; 5 seconds when absent), so every download from that host waits it out, then retries, up to three times. A `Retry-After` over 60 seconds fails that download at once, and while the hold lasts the host's other downloads are not tried: they stay queued for the next pull with their attempts unchanged and `Not tried: <host> asked to wait Ns` as their last error. API calls run one at a time through the rate limiter; concurrent API calls are added only if measurement shows the need.
- A failed download stays queued and is retried on the next pull. HTTP 403 settles as `forbidden` and 404 as `not_found`; every recorded HTTP error carries the status code and its standard reason phrase (`HTTP 410 Gone`). `forbidden`, `not_found`, `deleted`, `external` and `gave_up` are terminal. A download that is not a Slack file (link previews, attachment images, other outside-site images) settles as `gave_up` once its third attempt fails; Slack file downloads keep retrying without a cap. `slarc pull --retry-skipped` queues the skipped downloads of the items it pulls afresh, with `attempts` reset to 0: `forbidden`, `not_found` and `gave_up` files and images, and images that were `too_large`. It finds them in the messages already stored, so it reads nothing more from Slack. `slarc pull --full` does the same for the messages it walks. A file over the size limit is retried once its recorded size fits.
- A Slack file download, one sent with the session's credentials, times out after 60 seconds; any other download after 15.

**Progress and summary**

- Each item gets a progress bar showing messages read, each counted once per conversation per run, and how many were new. The bar moves after each history page and each thread read. While history is paged, the row also shows the local date of the oldest message reached after `@`, which follows each thread as its replies are read, and after `⌖` the date paging reads back to: the later of `--since` and the conversation's creation, or on a later pull the newest message already stored. Both appear only once history returns a message, so a pull that finds nothing new shows just the counts. The bar then fills by time between `--until` (or now) and that date. Slack gives no sign of a plan's history limit, so on such a plan the row ends with `@` short of `⌖`. Downloads get one more bar, `Downloads`, showing done of total, then how many failed (left pending for the next pull) and how many were skipped (too large, forbidden, not found or given up) when either is nonzero. A last row, `Archive`, shows the unit of work saving the archive: `saving`, `compacting`, `deleting unused files` when the archive is idle, then `saved`. It appears only below other rows, so a pull that fails or reads nothing prints only its result. While a call waits out a rate limit Slack asked for (HTTP 429 with `Retry-After`), a `Slack` row counts the wait down, `rate limited, waiting 28s`, and goes away when the wait ends.
- A pull ends with `Pulled N items: M new messages, D new directory lines (users, emoji, files).`, then, when there were any, `Downloads:` with the files and images saved, those pending for the next pull and those skipped (too large, forbidden, not found or given up).

## Storage

The archive is a DuckLake: Parquet data files plus a SQLite catalog, written and read through the `duckdb` package, and a content-addressed blob store beside it (ADR 0003; `docs/architecture.md` has the details). A commit is a catalog transaction, so a batch is part of the archive if and only if its transaction committed, and a crash leaves exactly the last committed state. An archive with no `catalog.sqlite` and no files in `data/` or `blobs/` gets an empty lake.

**Layout of one archive**

```
<archive>/
  slarc.toml
  catalog.sqlite                        # DuckLake catalog, plus -wal and -shm while open
  data/                                 # Parquet data and delete files, managed by DuckLake
  blobs/sha256/ab/cd/<hash>             # files, snippets, avatars, emoji and other images
```

A command that only reads, such as `search` or `sql`, cannot checkpoint the catalog through its read-only connection, so it may leave an empty `-wal` file and a small `-shm` file beside the catalog; they are harmless and expected.

Exports go wherever `--out` points (default: a new directory named after the export under the current directory).

**Tables**

One DuckLake schema. Every table carries `team` first, as every key does. Times are ISO 8601 UTC strings with `Z`, never timestamp columns.

| Table | Rows | Columns |
| --- | --- | --- |
| `message_version` | Append-only, one per observed version of a message | team, channel, ts, thread_ts, user, kind (`message` or `deleted`), fetched_at, run, source_team, api, slarc, hash, plain, object |
| `directory_entry` | Append-only, one per observed state of a user, emoji, conversation, bot, usergroup, file or image | team, kind, id, fetched_at, run, api, slarc, hash, blob, object |
| `run` | Append-only, one per run that ended | run, command, started, finished, fetched_at, slarc, object (args, teams, auth, counts, errors) |
| `tracked_item` | One row per item, updated in place | the fields of the `tracked_item` entity below, plus `revision` |
| `download_state` | One row per download target, updated in place | team, target_kind, target, url, status, attempts, last_error |

- `object` is the verbatim Slack object as JSON. A `deleted` row has no `object`, `hash` or `plain`.
- `plain` is the rendered plain text: mentions resolved to names, emoji as `:name:`, links as their label, layout block text included. It is derived and best-effort at write time (a later rename does not rewrite old rows), and it is what `slarc search` matches.
- `blob` is the sha256 of the downloaded bytes once present, on the kinds that have one.
- `run` ids are `r_<UTC timestamp>_<4 hex digits>`, for example `r_20261020T140000Z_3f9a`; the suffix tells apart commands started in the same second.
- Rows are written sorted by (team, channel, ts), or (team, kind, id) for directory entries. The tables are not partitioned.

**Write rules**

The current state of a message is its newest version for `(team, channel, ts)`, ordered by `(fetched_at, run)`; earlier versions are kept. Commits are serialized, so duplicate versions cannot occur.

1. A never-seen message is added as a `message_version` row.
2. A re-read message whose `hash` differs from its newest version adds a row. An edit to an old message adds one row.
3. A new reply in a dormant thread is a new message; threads are reassembled by `thread_ts` at read time.
4. A delete (see Pulling) adds a `deleted` version. Only `slarc rm` removes rows.
5. A re-read that matches the stored hash writes nothing, so pulling the same range twice adds no message or directory rows.
6. Directory entries follow the same rule, except that a row is added when either the hash or `blob` differs from the newest row. A rename is one more `user` row; a `file` row is written when the file is first referenced and again once its blob is downloaded.
7. Every run that ends adds one `run` row, in its own commit. A run killed outright writes no run row; what it committed stands and what it did not commit was never part of the archive.
8. `tracked_item` and `download_state` rows are replaced by the commit that changes them. A download state settles by being deleted, in the same commit that writes the directory row recording its blob.

**Commits and removals**

- A commit is one transaction made under the write lock. Pull commits an item named on the command line before reading it, each directory refresh, each tracked item when it finishes or is skipped, each history page of a backfill together with the cursors moved past it, and each download result.
- Every `tracked_item` row has an integer `revision`. A commit that changes an item names the revision it read and fails, rolling the whole batch back, if the row has moved on: an rm removed it, or another pull advanced it. A download result commits only if its `download_state` row still exists, a directory refresh only if the workspace still has a tracked item that is not removed, and a batch that assumed a file or image was stored only if its directory row still exists. A rolled-back item is picked up again by the next pull, so a pull that started before an rm can never re-track what the rm removed.
- `slarc rm` commits its removal in one transaction that deletes the rows no remaining tracked item needs and marks each removed item's status `removed`, with its revision bumped. It recomputes its plan after confirmation and retries up to three times if a pull committed in between.

**Locks and maintenance**

- Two `fcntl.flock` files under `$XDG_CACHE_HOME/slarc/locks/` order commands on one machine. Every command holds the read lock shared while it has the archive open (`serve` holds it per request). Each transaction and each maintenance run holds the write lock exclusively, and the read lock is always taken before the write lock.
- Maintenance runs at the end of every `pull` and `rm`, after the run row commits. It compacts the Parquet files every time. It deletes bytes only when it can take the read lock exclusively without blocking, which shows that no other slarc command has the archive open: it then expires every snapshot but the newest, deletes unreferenced and orphaned data files, deletes blobs no `directory_entry` row references, and vacuums the catalog when its free pages pass a threshold. Otherwise deletion waits for the next maintenance that finds the archive idle. No time windows are used.
- A pull that reads a message whose blob is missing queues the download again. No pull scans the whole blob store.

**One owning machine**

- The catalog records the machine that owns the archive, by a stable machine id with the host name for messages. The first open, including the one that creates the lake, records the current machine.
- Every command, reads included, checks the record when it opens the archive, once per process. On another machine it stops with a message naming the owning host and saying to wait until the synced folder has finished syncing, then run the command again with the global option `--take-ownership`. That option is the user's statement that every change from the previous machine has arrived. It records the current machine, checks that every data file the catalog references exists (stopping with a "has not finished syncing" error if any is missing), and makes the run's maintenance skip deleting orphaned files and unreferenced blobs, because files that synced ahead of the catalog look exactly like orphans.
- A command that would create a catalog because `catalog.sqlite` is missing refuses, and creates nothing, when `data/` or `blobs/` already holds a file, because a synced folder can deliver those before the catalog. A read command on an archive with no catalog and no files creates an empty lake.
- The README recommends keeping an archive on local disk, outside synced folders.

**Querying**

`DuckDbQueryIndex` attaches the lake read-only and defines a view per entity (Data model) in an in-memory DuckDB per invocation. The current message is one `QUALIFY` over `message_version`; the current-state directory views take the newest row per key. `slarc sql` queries the views by default and the tables as `lake.<table>`. The `duckdb` dependency is pinned exactly, and the `ducklake` and `sqlite_scanner` extensions are downloaded on first use into the XDG cache; if they are missing and cannot be downloaded, slarc says which extensions and that one online run of any command installs them.

## Row schema

`object` is verbatim Slack and only its source method (`api`) is recorded. The Python model (msgspec structs) is the source of truth; a generated JSON Schema and a `validate` check are planned.

**Conventions**

- Hashes are computed over a canonical form (sorted keys, no whitespace) of the hashed subset, so key order never affects identity.
- `kind` is `message` or `deleted` in `message_version`, and `user`, `emoji`, `conversation`, `bot`, `usergroup`, `file` or `image` in `directory_entry`.
- `team` is the workspace `team_id` the object was read through (`T…` or `E…`). `source_team` is the author's home team from `object.team`; it equals `team` unless Slack Connect.
- `id` in `directory_entry` is the user id, emoji name, conversation id, bot id, usergroup id, file id, or image URL.
- `api` is the Slack method that returned `object` (`conversations.history`, `users.list`, `files.info`, `download` for images, and so on). `slarc` is the slarc version that wrote the row.
- `fetched_at` is the ordering key for versions.

**Hashed subset**

- message: `text`, `blocks`, `attachments`, the `id` and `mode` of each entry in `files`, `edited`, `reactions`, `subtype`, `user`, `bot_id`. `blocks` and `attachments` are hashed with every `block_id` removed, because Slack regenerates block ids on each read for API-posted messages. Excluded: `reply_count`, `reply_users`, `reply_users_count`, `latest_reply`, `is_locked`, `last_read`, `subscribed`, and anything else per-user or per-request.
- user: the whole `profile` object plus `name`, `real_name`, `deleted`, `tz`, `is_bot`, `is_app_user`.
- emoji: the URL or alias target.
- conversation: `name`, `topic`, `purpose`, `is_archived`, `is_private`, `is_shared`, membership. `last_read` is per-user and lives on the tracked item instead.
- file: everything except `shares` and fields that change on every read.
- image: the URL plus `Content-Type` and size from the download.

**Versioning**: the `slarc` column records which slarc version wrote each row, and readers ignore columns they do not know. The lake does not migrate itself across DuckDB releases, so `duckdb` is pinned exactly; upgrading it is a deliberate change, tested against an archive the previous version created.

## Data model

The entities below are DuckDB views over the lake's tables. Every key is prefixed by `team_id`. `tracked_item` and `download` are views over `tracked_item` and `download_state` with no fold, because those tables already hold current state.

| Entity | Source | Key | Fields |
| --- | --- | --- | --- |
| workspace | `run`, latest `auth` entry per team | team_id | domain, name, user ids seen, credential_source, last_auth_test |
| conversation | `directory_entry` kind `conversation`, newest row per id | (team_id, id) | kind ∈ channel, dm, group_dm; is_private; name (null for DM); dm_user_id; is_archived; is_shared; created; creator; topic; purpose; member_ids |
| message | `message_version`, newest row per key | (team_id, channel_id, ts) | source_team, thread_ts, is_thread_parent, reply_count and latest_reply (computed from stored replies), author_kind ∈ user, bot, system; user_id, bot_id, subtype, text_mrkdwn, blocks_json, attachments_json, edited_ts, edited_by, is_broadcast, plain, hash, first_seen_at, last_seen_at, deleted_at |
| message_version | `message_version`, every row | (team_id, channel_id, ts, fetched_at, run) | hash (null for `deleted`), edited_ts |
| reaction | unnested from message | (team_id, channel_id, ts, name) | name with skin tone, base_name, count, user_ids |
| file | `directory_entry` kind `file`, newest row per id | (team_id, id) | name, title, mimetype, filetype, mode, size, is_external, external_url, permalink, blob, preview_text, width, height |
| message_file | unnested from message | (team_id, channel_id, ts, file_id) | position |
| image | `directory_entry` kind `image`, newest row per URL | (team_id, url) | content_type, size, blob |
| user | `directory_entry` kind `user`, newest row per id | (team_id, id) | name, real_name, display_name, display (fallback chain applied), email, tz, tz_label, tz_offset, is_bot, is_app_user, deleted, home_team_id, avatar_blob |
| user_snapshot | `directory_entry` kind `user`, every row | (team_id, id, fetched_at) | hash, profile_json |
| bot | `directory_entry` kind `bot`, newest row per id | (team_id, id) | app_id, name, icon_blob |
| emoji | `directory_entry` kind `emoji`, newest row per name | (team_id, name) | url, alias_of, is_animated, blob, first_seen, last_seen_in_list |
| usergroup | `directory_entry` kind `usergroup`, newest row per id | (team_id, id) | handle, name, user_ids |
| run | `run` | run | command, args, started, finished, teams, auth per team, messages_seen, lines_appended, rate_limit_wait_s, api_calls (Web API calls per method, pages included), threads_read (threads read per reason ∈ link, history, search, active, full, retry; a thread counts once, under its first reason), errors |
| tracked_item | `tracked_item` table, no fold | (team_id, kind, channel_id, ts) | kind ∈ conversation, thread, message; since, until, newest_ts, oldest_ts, backfill_complete, last_search_at, last_pull_at, status ∈ ok, not_visible, removed (pull warns once when an item turns not visible, then skips it quietly until it reads again; the summary counts not-visible items; a removed item keeps its row so rm accepts selectors that name it); archived (the last finished pull found the conversation Slack-archived); last_read per user_id; retry_threads (threads whose read failed, re-read first on the next pull). An interrupted first pull resumes paging older history from `oldest_ts` while `backfill_complete` is false. An item named on the command line is recorded before it is read, so an interrupted pull keeps it |
| download | `download_state` table, no fold | (team_id, target) | target_kind; target is a file id or image URL; url; status ∈ pending, failed, forbidden, not_found, too_large, deleted, external, gave_up; attempts; last_error. A downloaded target's row is deleted; the `blob` on the directory entry is the record. rm deletes the rows of what it removes |

Notes

- A run row has no `team`; it lists the teams it touched, and every tracked item and download row carries its own `team_id`. The current state of an item is its row, replaced by each commit that changes it.
- Snippet text lives in the blob; `preview_text` is for listing only.
- The view definitions are versioned in code. Changing a view needs no migration, because the tables are the only state.

## Rendering

One set of Jinja2 partials drives every output: `export` (static files plus a small JS viewer), the planned `serve` (Starlette plus HTMX), and a planned PDF export (Playwright print-to-PDF over the static HTML; see `docs/backlog.md`).

| Element | Requirement |
| --- | --- |
| Text | `rich_text` blocks: sections, nested bullet and ordered lists with `indent`, quotes, preformatted; styles bold, italic, strike, code, underline, highlight; links; user, channel, usergroup, broadcast and date elements. Layout blocks: `section`, `header`, `divider`, `image`, `context`, with `actions` shown as inert labels. mrkdwn fallback for messages without blocks. Unknown types render as escaped text |
| Names | User display fallback: `profile.display_name`, `profile.real_name`, `real_name`, `name`. Unknown ids render as the raw id |
| Emoji | Standard: unicode with skin tone applied. Custom: local blob (animated GIF in HTML). Unknown: `:name:` text |
| Reactions | Grouped by base name with skin-tone variants side by side and a combined count; tooltip lists names |
| Edits | "(edited)" marker with time |
| Files | Images and thumbnails from blobs; snippets syntax-highlighted in full with filename; video and audio players; download links; placeholders for `tombstone` ("This file was deleted."), `hidden_by_limit`, `too_large` and external files |
| Previews and app posts | Attachments: title, text, image or thumb, service icon and name, color bar. Giphy and other app posts through their layout blocks, with the app name from `bot_profile` |
| Threads | Reply count, participant avatars and last reply time under the parent, linking to a side panel with the parent and its replies (CSS only, opened by `:target`, full screen on narrow screens, inline when printed); a thread or message link's page shows the replies inline; broadcast replies in both places |
| System messages | Joins, leaves, topic and name changes shown muted and collapsible |
| Deleted | Hidden in exports unless `--include-deleted`; shown with a marker in `serve` |
| Time | Day dividers and timestamps in the viewer's time zone (computed in the browser for static export), or `--tz`; author's local time on hover |
| Navigation | Conversation list, jump to date, stable anchors `#<channel>/<ts>` so Slack links can be rewritten to the archive |
| Exports | A self-contained folder by default; `--single-file` inlines assets as base64. Contains exactly what is named, DMs included when named. Email addresses are never written |

## Architecture

Hexagonal, in the Cosmic Python shape: a pure domain, a service layer of use cases, ports as `typing.Protocol` classes in `src/slarc/ports.py`, and adapters that implement them. Nothing in the domain or service layer imports `slack_sdk`, `duckdb`, `httpx`, `keyring` or touches the filesystem or environment, except reading packaged data through `importlib.resources`. `docs/architecture.md` describes the code in detail, with the full ports table; ADR 0002 records the decision.

- **Domain**: value types (`MessageTs` with numeric ordering and permalink conversion, `ConversationSel`, `MessageSel` and `WorkspaceSel` selectors, envelopes, tracked items and run records), the hashed-subset functions, and pure renderers: rich_text and layout blocks to HTML, mrkdwn to HTML, and `plain`.
- **Ports**: `SlackGateway` and `SlackConnector` (a gateway per workspace session), `CredentialSource`, `ArchiveStore`, `ArchiveLock` (the archive's read and write locks), `BlobStore`, `Downloader`, `ExportWriter` (writes export files), `QueryIndex` (what `search`, `list`, `sql` and `export` read), `Clock`, and `Reporter` (progress and warnings). Every port except `QueryIndex` has an in-memory fake in `tests/fakes.py`; `QueryIndex` is tested through DuckDB over a temp archive.
- **Unit of work**: one `UnitOfWork` class scopes one pull run over the archive store, blob store and clock; tests give it the in-memory stores. Pull commits after each page of results and each item, so an interrupted pull keeps what it committed; the run row is committed when the run ends, including on an exception, Ctrl-C or SIGTERM (a cron timeout or shutdown), followed by maintenance. A run killed outright loses its uncommitted batch and writes no run row, and the next pull re-reads without duplicating rows because hashes match. No message bus or domain events: the service layer collects download jobs during the pull and hands them to the `Downloader` at the end.
- **Service layer**: one module per use case, taking its ports as arguments.
- **Composition**: `bootstrap.py` is the only place real adapters are built; the Typer CLI takes them from it, and tests swap in fakes with `override()`. A `Settings` value from the layered loader in `config.py` configures each command.

## Tests and fixtures

- Service tests run against the fakes: no network, no disk, milliseconds each.
- Renderer tests are golden-file tests over recorded messages covering every subtype, block and element type in the fixtures.
- Adapter contract tests run the same test module against the fake and the real adapter: `DuckLakeArchiveStore` and `DuckDbQueryIndex` against a lake in a temp directory; `SessionTokenGateway` against recorded HTTP responses with credentials scrubbed.
- The LevelDB reader and cookie decryption are tested against synthetic files built by a small writer in the test code (Snappy allows literal-only streams, so no compressor is needed). Real credentials are never committed.
- Property tests (Hypothesis) for link parsing, `ts` ordering, and idempotency: pulling the same fixture twice adds no message or directory rows and leaves the views identical.

**Test workspace**

- `naleidworkspace` (team `T0CB5LYPVFA`) is the only workspace that may ever be written to, and only by `scripts/seed-test-workspace.py`: a uv-shebang developer script outside the package that takes a workspace and channel, refuses any team id other than `T0CB5LYPVFA`, and posts into a dedicated channel such as `#slarc-fixtures`. It covers what one user can create: thread replies (also sent to the channel), edits, deletes with and without replies, rich text, reactions, file uploads (PDF, video, several files on one message), pins, a private channel, topic and name changes, a Slack-archived channel.
- Content it cannot create is made by hand: anything posted as jim, a group DM with a third user, custom and animated emoji uploads, jim's display name change, huddles, canvases.
- Agents may use the developer's desktop session for read-only calls to the test workspace without asking.
- `just record-fixtures` pulls the test workspace into committed fixtures under `tests/fixtures/`. The recorded archive is JSONL so it is reviewable in git, and test code loads it into a lake. Names stay; tokens and secrets are scrubbed and emails become `@example.invalid`. Tests never need workspace access.

## Toolchain

- Python 3.15, uv, Typer + Rich, `slack_sdk` ≥ 3.45, `httpx`, DuckDB, msgspec, `cryptography`, `keyring`, Jinja2; pytest, Hypothesis, ruff (lint and format), ty (pinned).
- Installed with `uv tool install` (`uv tool install git+https://github.com/tednaleid/slarc`); `just install` does an editable install. Developer scripts use a uv shebang.
- `just` recipes follow the just-bootstrap conventions: `check`, `test`, `lint`, `fmt`, `build`, `install`, `clean`, `bump`, `retag`, `install-hooks`. A pre-commit hook runs `just check`. GitHub Actions CI runs `just check` on macOS and Linux.
- Public repo at `tednaleid/slarc`, MIT license.

## Status

slarc pulls conversations, threads and single messages with date ranges, search-based reply discovery, active-thread re-reads, `--full` and delete inference; downloads files and images; keeps the users, emoji, usergroups and bots directory; and provides `auth`, `list`, `search`, `sql`, `export` and `rm`. The desktop session is read on macOS and Linux, with env and paste fallbacks.

**Acceptance criteria**, checked live by `just acceptance` against the test workspace:

- pulling the same channel twice adds no message or directory rows;
- a new reply in a dormant thread is found by a bare `pull` and lands as exactly one new message row;
- an edit to an old message lands as exactly one new version row under `--full`;
- the export of the test channel matches the Slack client in substance.

**Planned:** `serve` with HTMX; full-text search with filters; observed-version history in the viewer; `import` from Slack export ZIPs and slackdump SQLite; Slack export ZIP output so slarchive and slack-export-viewer can read archives; a generated JSON Schema and a `validate` check; a probe that warns if Slack starts limiting session tokens; Slack Connect `source_team` resolution; obfuscated fixture recording from real workspaces. Known issues and later ideas are in `docs/backlog.md`.

## Prior art

Each project in `.llm/` is worth reading for one thing; none is a runtime dependency.

| Project | What it is | Take from it | Leave behind |
| --- | --- | --- | --- |
| [gammons/slk](https://github.com/gammons/slk) (Go TUI, MIT) | Slack client over the web client's protocol | Desktop credential extraction (`internal/slackdesktop/`): data directory candidates, keychain accounts, cookie decryption, domain-hash prefix, `localConfig_v2` | TUI concerns; its SQLite cache; Edge endpoints |
| [rusq/slackdump](https://github.com/rusq/slackdump) (Go, AGPL-3.0) | Archiver to SQLite with resume, export ZIP, viewer | Edge cases: resume, channel enumeration, file handling, Free-plan redaction; its obfuscator; its security-alert warning | Code. AGPL means read, never copy |
| [korotovsky/slack-mcp-server](https://github.com/korotovsky/slack-mcp-server) (Go, MIT) | Slack MCP server with xoxc/xoxd mode | Token-type matrix; limiter plus `Retry-After` design; the pagination-without-limiter bug in its issue #231 | Edge endpoints, TLS fingerprinting, user-agent spoofing |
| slackpipe (Python, MIT) | Dagster pipeline wrapping slackdump into DuckDB | `payload_hash` diffing per message; writing the cookie to a 0600 temp file and redacting secrets from subprocess output; checking download size against metadata | Server ops machinery; mutable storage |
| slack-archive (TypeScript, MIT) | Interactive CLI to static HTML from an xoxp token | Static HTML paging and client-side search index | xoxp-only auth; whole-file rewrites; it never finds replies to old threads |
| [devrelopers/slarchive](https://github.com/devrelopers/slarchive) (HTML, MIT) | Single-file viewer for Slack export ZIPs | Visual reference for channel, thread, reaction and file rendering | Reads export ZIPs only |
| [hfaran/slack-export-viewer](https://github.com/hfaran/slack-export-viewer) (Python) | Export viewer, server or HTML-only | Jinja template structure | Flask |
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (Python) | Agent platform with a Slack adapter | Subtype allowlist (its issue #110778); `slack-sdk ≥ 3.45` | Bolt and Socket Mode |
| [iamcal/emoji-data](https://github.com/iamcal/emoji-data) (MIT) | The emoji table Slack uses | Source for the generated standard emoji table with skin tones | Sprite sheets |
| [cclgroupltd/ccl_chromium_reader](https://github.com/cclgroupltd/ccl_chromium_reader) (Python, MIT) | Forensic readers for Chromium storage | Dev-time check of the in-house LevelDB reader | Shipping it: not on PyPI, native compression dependencies, far more than one key needs |
| [Cosmic Python](https://www.cosmicpython.com/) (`.llm/cosmic-python-book`) | Repository, service layer, unit of work, hexagonal layout | The shape of the Architecture section; fake-repository and fake-UoW tests | SQLAlchemy, Flask, the message bus, the relational-store assumption |

## Open questions

- [ ] `search.messages` query syntax for DMs and group DMs (for reply discovery outside channels).
- [ ] Linux credential details: Secret Service item names for the Slack Safe Storage key, Snap and Flatpak data directories. Check against slk when implementing.
- [ ] How Enterprise Grid (`E…` org ids with `T…` workspaces) shows up in links, `auth.test` and message `team` fields.
- [ ] Scheduled pulls (launchd or cron) and whether they should alert on an expired session.
