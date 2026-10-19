## Overview

A read-only CLI that logs into Slack the way the official client does, archives channels, DMs and threads into append-only JSONL files plus a content-addressed blob store (queried in place by DuckDB, searchable with `rg`, cloneable as a git repo), and renders them later as a local web app, a static HTML bundle, or a PDF. It is a client, not a Slack app: nothing is installed in the workspace, no admin approval is needed, and it sees exactly what my account sees. Separate named archives keep personal and work workspaces apart.

Python 3.12+ with uv, `slack_sdk`, Typer and DuckDB. Hexagonal architecture in the Cosmic Python style (domain, ports, adapters, service layer, unit of work) so every Slack interaction is behind a port with an in-memory fake for tests. The closest prior art is [rusq/slackdump](https://github.com/rusq/slackdump) (Go, AGPL); the credential pattern comes from [gammons/slk](https://github.com/gammons/slk). This project is unsanctioned by Slack by design; if the client protocol changes, the adapter changes.

## Goals and non-goals

**Goals**

1. Authenticate as my own user, using the session the Slack desktop app or a browser already holds, and archive any conversation I can see: public and private channels, DMs, group DMs, and individual threads. Date-range limiting on any of these.
2. Archive faithfully enough to re-render "like Slack" later: formatting, mentions, emoji (custom, animated, skin tones), reactions, files and snippets with full content, link previews, threads, edit status.
3. Incremental, resumable, idempotent syncs. Re-running is cheap and safe; a crash mid-channel resumes where it stopped.
4. Render three ways: a local `serve` web app, a self-contained static HTML bundle for sharing, and optionally PDF.
5. Decent speed: a 100k-message channel in minutes, not hours. Session tokens get the web client's rate limits, so the May 2025 clamp on third-party apps does not apply.
6. Hexagonal architecture with fast unit tests that never touch the network.
7. Multi-workspace from day one (`team_id` in every key), even though the first use is one workspace.

**Non-goals for the MVP**

- Writing anything to Slack: no posting, reacting, or marking read. The archiver never calls a mutating method.
- Real-time capture via Events API or Socket Mode. Polling sync is enough.
- Canvases, huddles, lists, bookmarks, channel sections and sidebar groups.
- Apps beyond being an author of messages or the other party in a DM.
- Recovering pre-edit message versions; Slack never exposes them (see Data-model facts).
- Being sanctioned by Slack. This talks to Slack as the client does; if that breaks, the gateway adapter is what changes.

## Auth

The archiver authenticates as my user with the same two credentials the Slack client uses: a per-workspace `xoxc-` token and the `d` session cookie whose value is `xoxd-`. Every Web API call sends the `xoxc` token as the bearer/`token` parameter and the `d` cookie in the `Cookie` header. In `slack_sdk` that is `WebClient(token=xoxc, headers={"Cookie": f"d={xoxd}"})`; after that, every documented method works as it does for an app.

**Credential sources, in priority order**

| Source | How it gets the pair | When used |
| --- | --- | --- |
| Slack desktop app session (the [slk](https://github.com/gammons/slk) pattern) | Read the app's `Cookies` SQLite DB, decrypt the `d` cookie with the "Slack Safe Storage" key from the OS keychain (macOS Keychain, Linux Secret Service/KWallet), read `xoxc` tokens per workspace from the app's Local Storage (`localConfig_v2`) | Default. Re-read on every run; never copied into the archive. Stays valid as long as the desktop app stays signed in |
| Pasted browser session | In DevTools: `JSON.parse(localStorage.localConfig_v2).teams[...].token` for `xoxc`, and the `d` cookie from Application → Cookies | Fallback when the desktop app isn't installed or its store can't be read; `slarc auth --paste` |
| Internal app user token (`xoxp-`) | Create an app from a manifest in the workspace, add user scopes, install, copy the User OAuth Token | Phase 3 adapter for workspaces where a sanctioned app is preferable, e.g. a corporate Enterprise Grid |
| Import | `slarc import <slack-export.zip \| slackdump.sqlite>` | Seeding history without API calls |

Bot tokens (`xoxb-`) are rejected: they only see invited channels and never DMs.

**Token lifecycle**

- Session credentials are read fresh each run from the desktop app's store; the archive records only `team_id`, the user id from `auth.test`, the credential source, and the last successful `auth.test` time, in `runs.jsonl`.
- A pasted pair is stored in the OS keyring via `keyring`, keyed by `team_id`, never in the archive files, logs, or fixtures. `SLARC_XOXC` / `SLARC_XOXD` env vars override for scripted runs.
- Every command that talks to Slack starts with `auth.test`. On `invalid_auth` or `not_authed` the CLI says which source failed and how to refresh it (sign into the desktop app again, or re-paste), then exits non-zero. Read-only commands (`serve`, `search`, `status`, `sql`, `export`) never need credentials.
- Signing out of the desktop app invalidates the pair. slarc never triggers that itself.

**Rate limits**

- Session tokens get whatever the web client gets. The 2025 change that clamps non-Marketplace apps to 1 request/minute and 15 messages on `conversations.history`/`conversations.replies` applies to app tokens, not sessions. `auth` still runs a probe (`conversations.history` with `limit=200` on the conversation with the most members from \`conversations.list\`, or \`--probe \<channel>\`) and warns if ≤15 messages come back with `has_more`, so a future change is caught immediately.
- Per-method token bucket (default about 50/min for Tier 3 methods, 1/sec sustained as Slack recommends) plus honoring `Retry-After` on 429 with jittered backoff. Pagination loops always pass through the limiter, which is the mistake korotovsky's server made at Enterprise scale (its issue #231).
- Slackdump's README notes that automated access can trigger Slack security alerts or notify admins. Accepted for my own workspaces; the `xoxp` adapter exists for cases where it is not.

**What the session adapter must not do**

- No `client.*` / Edge endpoints. They have no stable contract and aren't needed for archiving; documented methods behave identically with a session token, which keeps the fake gateway honest and the raw JSON shapes stable.
- No custom TLS fingerprinting or user-agent spoofing. Send an honest `User-Agent` like `slarc/0.1`.

## Slack data-model facts

These answer the open questions in the draft and constrain the schema. Verify each against [docs.slack.dev](https://docs.slack.dev/reference/methods/conversations.history/) while building; items marked (inferred) are not documented by Slack.

**Identity and URLs**

- A message's natural key is `(team_id, channel_id, ts)`. `ts` is a string like `"1792410693.663869"`. Keep it as a string or fixed-precision decimal, never a float.
- `https://ws.slack.com/archives/C0CA5396G03` → conversation id `C0CA5396G03`. `C…` public or private channel, `G…` legacy private group or MPIM, `D…` IM.
- `.../archives/C0CA5396G03/p1792410693663869` → `ts = "1792410693.663869"` (insert a dot before the last six digits). A reply permalink also carries `?thread_ts=…&cid=…`; honor `thread_ts` when present, otherwise call `conversations.replies(channel, ts)`, which accepts any message in a thread.
- `conversations.info` classifies the conversation: `is_im`, `is_mpim`, `is_private`, `is_archived`. IMs have no `name`, only `user` (the other party). MPIMs have generated names like `mpdm-alice--bob--carol-1`. Display names for both are synthesized from members.

**History, threads, pagination**

- `conversations.history(channel, oldest, latest, inclusive, limit, cursor)` returns newest-first, top-level messages plus thread broadcasts only. Date ranges are epoch-second strings in `oldest`/`latest`. Page with `response_metadata.next_cursor`.
- Thread parents carry `thread_ts == ts`, `reply_count`, `reply_users`, `latest_reply`. Fetch replies with `conversations.replies`; its first element is the parent.
- Incremental re-sync: a thread needs re-fetching only when the parent's `latest_reply` is newer than the stored value.
- `thread_broadcast` messages appear in both the channel and the thread. Store once, render in both places.

**Edits and deletes**

- Edits keep the same `ts` and add `edited: {user, ts}`. Slack's own docs say the original text is not available. The archive can only version what it observed: a new line on every sync where the content hash changes.
- A polling archiver never sees `message_changed` or `message_deleted` events. Deletes are inferred by absence on re-fetch of a window, or by `subtype: tombstone` on a deleted parent that still has replies. The record of a delete is a `deleted` line appended to the message's month file; nothing is ever hard-deleted.
- Subtypes to expect: `bot_message`, `me_message`, `channel_join`, `channel_leave`, `channel_topic`, `channel_purpose`, `channel_name`, `channel_archive`, `thread_broadcast`, `pinned_item`, `file_share` (legacy), `tombstone`, `huddle_thread`, plus app-specific ones. Render from an allowlist; everything else is a "system" message. A denylist silently readmits every new subtype Slack adds (hermes-agent issue #110778).

**Message text: mrkdwn and rich\_text**

- `text` is mrkdwn, not Markdown: `*bold*`, `_italic_`, `~strike~`, `` `code` ``, triple-backtick blocks, `>` quotes, links `<url|label>`, users `<@U123>`, channels `<#C123|name>`, usergroups `<!subteam^S123>`, specials `<!here>`, `<!channel>`, `<!date^…>`. `&`, `<`, `>` arrive escaped as `&amp;`, `&lt;`, `&gt;`.
- `blocks` with `rich_text` is what the composer emits for every user-typed message: containers `rich_text_section`, `rich_text_list` (bullet/ordered, `indent`), `rich_text_quote`, `rich_text_preformatted`; leaves `text` (style flags bold, italic, strike, code, underline, highlight), `link`, `emoji` (`name` such as `wave::skin-tone-2`, optional `unicode`), `user`, `usergroup`, `channel`, `broadcast`, `date`, `color`. Slack added further element types in 2026 (`canvas`, `file`, `message_mention`, `citation`).
- Store both. Render from `blocks` first (structurally exact, resolved ids, nested lists) and fall back to a mrkdwn parser for bots, legacy messages, and unknown elements. Unknown elements degrade to escaped text, never a crash.
- No maintained Python library converts rich\_text to HTML; most go the other way. The renderer is a pure function of about 300 lines and is written in-house.

**Reactions**

- `reactions: [{name, users[], count}]` on the message. `name` can carry a skin-tone suffix (`thumbsup::skin-tone-3`). `users` may be truncated relative to `count` on popular reactions, so store `count` rather than deriving it.

**Files and snippets**

- File objects carry `id, name, title, mimetype, filetype, user, size, mode, is_external, url_private, url_private_download, permalink, thumb_*` (including `thumb_360_gif`, `deanimate_gif`), `original_w/h`. `mode` ∈ `hosted | snippet | post | external`, plus the redacted states `hidden_by_limit` and `tombstone`.
- Download `url_private_download` with `Authorization: Bearer <xoxc>` and the `d` cookie, streaming to the blob store while hashing. For snippets this yields the full original bytes; `preview` is truncated. `files.info` (Tier 4) returns the full object; it is appended as a \`file\` line in \`directory/\<team>/files.jsonl\`.
- Free-plan workspaces redact files past the storage limit (`mode: hidden_by_limit`, name and URLs stripped) and the API returns no messages older than the plan's retention window. Archive early; render redacted files as explicit placeholders.
- External files (Google Drive and the like) have `mode: external`; store metadata and link, don't download.

**Link previews, app attachments, giphy**

- Previews live in the legacy `attachments` array: `fallback, title, title_link, text, image_url, thumb_url, from_url, original_url, service_name, author_*, footer, color`. Newer apps put `blocks` inside attachments.
- `/giphy` posts are app messages (`bot_id`, usually `app_id`, sometimes `subtype: bot_message`) with an image attachment.
- When the author removes a preview, the attachment is simply absent on the next fetch; there is no tombstone (inferred). Render the latest observed version; the raw envelope history keeps the old one.

**Users and bots**

- `users.list` / `users.info`: `id, team_id, name, real_name, deleted, is_bot, is_app_user, tz, tz_label, tz_offset`, `profile.{display_name, real_name, email, image_24 … image_512, image_original}`. With a session token, `email` is visible when the workspace shows it to members.
- Bot authors carry `bot_id`, `bot_profile`, sometimes `username` and `icons`; `bots.info` fills the rest.
- Messages often inline a `user_profile` snapshot (name and avatar at post time). Keep it; it is the only historical record of display names.
- Guests and Slack Connect users come from other teams (`user_team` ≠ `team_id`).

**Emoji**

- `emoji.list` maps custom names to an image URL or to `alias:other`, resolved recursively. Custom emoji may be animated GIFs. Standard emoji need a vendored shortcode table; [iamcal/emoji-data](https://github.com/iamcal/emoji-data) is what Slack itself uses, including skin-tone variants.
- Deleted custom emoji vanish from `emoji.list`. Download and keep every image ever seen; fall back to `:name:` text.

**Read state**

- `conversations.info` exposes `last_read` (a `ts` high-watermark) and, for some types, `unread_count`. There is no per-message read state in the Web API; the richer `client.counts` is an Edge endpoint and is out of scope. A per-conversation high-watermark is what gets stored, and it is enough.

## Data model

One layer on disk: append-only JSONL envelopes under `raw/`, `directory/` and `runs.jsonl`. The entities below are the DuckDB views the query adapter derives from those files at read time; none of them is stored separately. Every key is prefixed by `team_id`; every archive is a separate set of files. Nothing is ever hard-deleted; an upstream delete is a `deleted` line, and the views expose it as `deleted_at`.

| Entity | Source | Key | Fields |
| --- | --- | --- | --- |
| envelope | `raw/`, `directory/`, `runs.jsonl` | (team, kind, id or ts, fetched\_at, run) | `meta` as specified in the JSONL schema section; `object` verbatim. Append-only |
| workspace | `runs.jsonl`, `object.auth` of the last run per team | team\_id | domain, name, my\_user\_id, credential\_source, last\_auth\_test. From `auth.test`; no separate kind |
| conversation | `directory/<team>/conversations.jsonl`, last line per id | (team\_id, id) | kind ∈ public, private, im, mpim; name (null for IM); im\_user\_id; is\_archived; is\_shared; connected\_team\_ids; created; creator; topic; purpose; member\_ids |
| message | `raw/`, last line per (team, channel, ts) | (team\_id, channel\_id, ts) | source\_team; thread\_ts; is\_thread\_parent; reply\_count and latest\_reply (computed from replies); author\_kind ∈ user, bot, app, system; user\_id; bot\_id; app\_id; subtype; text\_mrkdwn; blocks\_json; attachments\_json; edited\_ts; edited\_by; is\_broadcast; user\_profile\_snapshot\_json; hash; first\_seen\_at; last\_seen\_at; deleted\_at (the `fetched_at` of a trailing `deleted` line, else null) |
| message\_version | `raw/`, every line per (team, channel, ts) | (team\_id, channel\_id, ts, fetched\_at, run) | hash (null for a `deleted` line), edited\_ts. One row per line; distinct hashes are the distinct versions |
| reaction | unnested from message | (team\_id, channel\_id, ts, name) | name includes `::skin-tone-N`; count; user\_ids; emoji resolved as (source\_team or team\_id, name) |
| file | `directory/<team>/files.jsonl`, last line per id | (team\_id, id) | name, title, mimetype, filetype, mode ∈ hosted, snippet, post, external, hidden\_by\_limit, tombstone; size; is\_external; external\_url; permalink; blob (sha256, null until downloaded); preview\_text; width, height |
| message\_file | unnested from message `files[].id` | (team\_id, channel\_id, ts, file\_id) | position |
| user | `directory/<team>/users.jsonl`, last line per id | (team\_id, id) | name, real\_name, display\_name, email, tz, tz\_label, tz\_offset, is\_bot, is\_app\_user, deleted, home\_team\_id, avatar\_blob (sha256 of `profile.image_512`, re-downloaded when the profile hash changes) |
| user\_snapshot | `directory/<team>/users.jsonl`, every line | (team\_id, id, fetched\_at) | hash, profile\_json. Tracks renames and avatar changes |
| bot | `directory/<team>/bots.jsonl`, last line per id | (team\_id, id) | app\_id, name, icon\_blob |
| emoji | `directory/<team>/emoji.jsonl`, last line per name | (team\_id, name) | url, alias\_of (same team), is\_animated, blob, first\_seen, last\_seen\_in\_list |
| usergroup | `directory/<team>/usergroups.jsonl`, last line per id | (team\_id, id) | handle, name, user\_ids |
| run | `runs.jsonl` | run | command, args, archive, started, finished, teams, auth per team, messages\_seen, lines\_appended, rate\_limit\_wait\_s, errors, cursors, thread\_cursors, blob\_queue |
| sync\_cursor | `runs.jsonl`, `object.cursors[]` of the last run that touched the conversation | (team\_id, channel\_id) | newest\_ts, oldest\_ts, backfill\_complete, resume\_cursor, requested\_oldest, requested\_latest, last\_read\_ts (Slack's high-watermark at that run), last\_run\_at |
| thread\_cursor | `runs.jsonl`, `object.thread_cursors[]` of the last run that touched the thread | (team\_id, channel\_id, thread\_ts) | latest\_reply\_seen, last\_run\_at |
| blob\_queue | `runs.jsonl`, `object.blob_queue[]` of the last run | (team\_id, file\_id or emoji name or user id) | status ∈ pending, failed, forbidden, too\_large, redacted, external; attempts; last\_error. Downloaded items drop out of the queue; the `blob` on the directory line is the record |

Notes

- Snippet full text lives in the blob, not in a column; `preview_text` is for search and listing only.
- What `hash` covers is defined once, under Hashed subset in the JSONL schema section.
- `source_team` defaults to `team_id`; it differs only for Slack Connect messages, whose `team` field names the author's home workspace.
- Standard emoji are not records; they come from the vendored iamcal table at render time.
- `runs.jsonl` is per archive, not per team. A run line has no `meta.team`; it lists the teams it touched in `object.teams`, and every cursor and queue entry inside it carries its own `team_id`. The current cursor for a conversation is the entry in the last run line that mentions it, so a clone carries the sync state, and a later `archive --since` earlier than `oldest_ts` knows to backfill rather than skip.
- The view definitions are versioned in code; a change to them needs no migration because the files are the only state.

## Storage

The archive is append-only JSONL files plus a content-addressed blob store, and nothing else. DuckDB queries the JSONL directly, so there is no second format to keep in sync, and the files are readable with `cat`, `rg`, `jq`, or an agent.

**Archives and workspaces**

- An archive is the isolation boundary: its own root directory, files and blobs. Nothing is shared or searchable across archives; deleting the directory deletes everything in it. Named archives (`personal`, `work`) are declared in config, and each workspace domain maps to exactly one archive.
- A workspace is the namespace boundary inside an archive. `team_id` is in every key, so `:foo:` in workspace Alpha and `:foo:` in Bravo are different records pointing at different images. The renderer resolves emoji, users, usergroups and aliases as `(team_id, name)`, never by name alone. Standard emoji are the exception; they come from the vendored unicode table.
- Slack Connect: a shared channel carries messages and reactions from users of another team. Each message records both the archiving `team_id` (the lens it was fetched through) and the author's `source_team` from the message's `team` field. Resolution tries `source_team`, then `team_id`, then falls back to `:name:` or the raw id. `emoji.list` only returns the archiving team's emoji, so another team's custom emoji resolve only when that team is also archived in the same archive; otherwise they render as `:name:`. Blobs are keyed by content hash and shared across teams within the archive.

**Layout of one archive root**

```
<root>/
  slarc.toml                            # root = "."; committed
  raw/<team>/<conv>/<YYYY-MM>.jsonl     # messages, one envelope per line, partitioned by the message's own ts
  directory/<team>/users.jsonl          # also emoji, conversations, bots, usergroups, files; one line per observed version
  runs.jsonl                            # one line per run: command, counts, cursors, download queue
  blobs/sha256/ab/cd/<hash>             # files, snippets, avatars, emoji images; git LFS or excluded
  exports/                              # gitignored by default
```

**Envelope**

Every line is a `meta` header followed by the verbatim Slack object. `meta` comes first so `rg` and humans see the keys before the payload.

```json
{"meta":{"schema":1,"kind":"message","team":"T0A","channel":"C0CA5396G03","ts":"1711234567.000100","source_team":"T0A","fetched_at":"2026-10-19T13:40:11Z","run":"r_20261019T134011","api":"conversations.history","slarc":"0.1.0","hash":"e3b0c442…","plain":"Ted Naleid: can you look at :eyes: the deploy?"},"object":{…Slack message as returned by the API…}}
```

- In `raw/` files `meta.kind` is `message` or `deleted`; a `deleted` line has no `object`, no `hash` and no `plain`. The full kind list is in the JSONL schema section.
- `meta.hash` identifies a version. What it covers per kind is listed once, under Hashed subset. It deliberately excludes `reply_count`, `reply_users` and `latest_reply`, which are derivable from the replies themselves.
- `meta.plain` is the rendered plain text: mentions resolved to names, emoji as `:name:`, links as their label. It is derived, duplicates the payload, and is what makes `rg "Ted"` work. It is a best-effort snapshot at write time: the directory is refreshed before history in every run so names resolve, but a later rename does not rewrite old lines. Everything else in the envelope is verbatim.

**Append rules**

Files are appended to and never rewritten. The current state of a message is the last line for its `(team, channel, ts)` ordered by `(fetched_at, run)`; earlier lines are its version history, and a version's identity is its `hash`, so two clones that observed the same version write lines that the views collapse.

1. A never-seen message is appended to the month file of its own `ts`.
2. A re-fetched message whose `hash` differs from its last stored line is appended as a new line to the same month file, with a later `fetched_at`. The old line stays. An old message edited in 2024 gets one more line in `2024-03.jsonl`, and git sees one added line. A parent whose `subtype` became `tombstone` is still a message and gets a `message` line, not a `deleted` one.
3. A new reply in a dormant thread has a current `ts`, so it lands in the current month file. The parent's file is untouched; threads are reassembled by `thread_ts` at read time and may span many files, as in Slack's own export.
4. Deletes are inferred only inside windows slarc re-fetches: `sync` re-fetches a trailing window of the channel (default 7 days, `--rescan DAYS`) plus every thread whose `latest_reply` advanced, and `archive --since/--until` re-fetches that range. A stored message absent from a re-fetched window gets a `deleted` line in its month file. Nothing is ever hard-deleted, and a message outside every re-fetched window is simply not re-checked.
5. A re-fetch that matches the stored hash writes nothing, so archiving the same range twice produces no diff.
6. Directory objects follow the same rule: a user rename or a new emoji image is one more line in `directory/<team>/users.jsonl` or `emoji.jsonl`. A `file` line is written when the file is first referenced (object = the inline file object, `api` = the method that returned it) and again once `files.info` has been fetched or the blob downloaded, since `blob` is part of what changes.
7. Every run appends one `run` line to `runs.jsonl` carrying the cursors, thread cursors and download queue as they stood at the end of that run.

**Querying**

DuckDB reads the globs directly: `read_ndjson('raw/**/*.jsonl', union_by_name=true)` with `object` forced to the `JSON` type (inference across subtypes produces ugly unions) and `maximum_object_size` raised for large `blocks`. A view per entity extracts fields with JSON functions; the current view is one window function per `(team, channel, ts)` ordered by `fetched_at`, and the rest of the lines are `message_version`.

`serve` materializes those views into an in-memory DuckDB at startup and builds the FTS index there; a million messages takes single-digit seconds. `search`, `status` and `sql` do the same. No index is written to the archive and nothing needs rebuilding after a clone. `list` and `status` also read cursors straight from `runs.jsonl` through `ArchiveStore`, so `list` works without DuckDB. During a sync, `JsonlArchiveStore` loads the touched conversation's month files into a dict keyed by `ts` for hash comparison; a 100k-message channel is a few hundred MB of JSON for the duration of the run, which is acceptable.

**Git and sharing**

JSONL is text, line-oriented and append-only, so git stores each sync as small deltas and diffs are readable. Blobs are the one part git cannot delta: git LFS for `blobs/`, or `export bundle` to ship files plus blobs as one zip. Merging two clones' syncs is a union of lines; the per-line hash makes duplicates harmless.

If a measurement ever shows DuckDB-over-JSONL startup is too slow, a second `QueryIndex` adapter can materialize to a persisted DuckDB file or Parquet behind the same port. That is a phase 3 option, not a design commitment.

## JSONL schema

`meta` is fully specified and validated; `object` is verbatim Slack and only its source method is recorded. The schema is the contract that DuckDB, `rg`, agents and future readers rely on, so it is versioned and checked in.

**File conventions**

- UTF-8, one JSON object per line, `\n` terminated, no BOM, compact encoding with no spaces.
- `meta` is always the first key and its keys are written in the order below, so diffs are stable and `rg` sees the identifiers before the payload.
- `object` keeps the key order Slack returned. The hash is computed over a canonical form (sorted keys, no whitespace) of the hashed subset, so key order never affects identity.
- File names: `raw/<team>/<conversation>/<YYYY-MM>.jsonl` holds `message` and `deleted` kinds only, partitioned by the message's own `ts` in UTC. The directory files are exactly `directory/<team>/users.jsonl`, `emoji.jsonl`, `conversations.jsonl`, `bots.jsonl`, `usergroups.jsonl` and `files.jsonl`, one kind each. `runs.jsonl` at the archive root holds `run` lines only.

**`meta` v1**

| Key | Type | Present on | Meaning |
| --- | --- | --- | --- |
| schema | integer | all | Envelope schema version; `1` |
| kind | enum | all | `message`, `deleted`, `user`, `emoji`, `conversation`, `bot`, `usergroup`, `file`, `run` |
| team | string | all but run | Workspace `team_id` the object was fetched through |
| channel | string | message, deleted | Conversation id |
| ts | string | message, deleted | Slack `ts`, kept as a string |
| id | string | directory kinds | `user.id`, emoji name, conversation id, bot id, usergroup id, file id |
| source\_team | string | message | Author's home team from `object.team`; equals `team` unless Slack Connect |
| fetched\_at | string | all | ISO 8601 UTC with `Z`; ordering key for versions |
| run | string | all | Run id, `r_<UTC timestamp>` |
| api | string | all but deleted, run | Slack method that returned `object`: `conversations.history`, `conversations.replies`, `users.list`, `emoji.list`, `files.info`, … |
| slarc | string | all | slarc version that wrote the line |
| hash | string | all but deleted, run | sha256 hex of the canonical hashed subset of `object` |
| plain | string | message | Rendered plain text: mentions resolved, emoji as `:name:`, links as labels |
| blob | string | file, emoji, user, bot | sha256 of the downloaded bytes once present, else absent |

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.invalid/slarc/envelope-v1.json",
  "type": "object",
  "required": ["meta"],
  "additionalProperties": false,
  "properties": {
    "meta": {
      "type": "object",
      "additionalProperties": false,
      "required": ["schema", "kind", "fetched_at", "run", "slarc"],
      "properties": {
        "schema": {"const": 1},
        "kind": {"enum": ["message", "deleted", "user", "emoji", "conversation", "bot", "usergroup", "file", "run"]},
        "team": {"type": "string", "pattern": "^[TE][A-Z0-9]+$"},
        "channel": {"type": "string", "pattern": "^[CGD][A-Z0-9]+$"},
        "ts": {"type": "string", "pattern": "^[0-9]{10}\\.[0-9]{6}$"},
        "id": {"type": "string"},
        "source_team": {"type": "string"},
        "fetched_at": {"type": "string", "format": "date-time"},
        "run": {"type": "string"},
        "api": {"type": "string"},
        "slarc": {"type": "string"},
        "hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "plain": {"type": "string"},
        "blob": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
      },
      "allOf": [
        {"if": {"properties": {"kind": {"enum": ["message", "deleted"]}}}, "then": {"required": ["team", "channel", "ts"]}},
        {"if": {"properties": {"kind": {"const": "message"}}}, "then": {"required": ["api", "hash", "plain", "source_team"]}},
        {"if": {"properties": {"kind": {"enum": ["user", "emoji", "conversation", "bot", "usergroup", "file"]}}}, "then": {"required": ["team", "id", "api", "hash"]}}
      ]
    },
    "object": {"type": "object"}
  },
  "allOf": [
    {"if": {"properties": {"meta": {"properties": {"kind": {"const": "deleted"}}}}}, "then": {"not": {"required": ["object"]}}, "else": {"required": ["object"]}}
  ]
}
```

**Hashed subset**

- message: `text`, `blocks`, `attachments`, the `id` of each entry in `files`, `edited`, `reactions`, `subtype`, `user`, `bot_id`. Excluded: `reply_count`, `reply_users`, `latest_reply`, `is_locked`, and anything the API adds per request such as `team` ordering.
- user: the whole `profile` object plus `name`, `real_name`, `deleted`, `tz`, `is_bot`, `is_app_user`.
- emoji: the URL or alias target.
- conversation: `name`, `topic`, `purpose`, `is_archived`, `is_private`, `is_shared`, membership.
- file: everything except `shares` and timestamps that change on every read.

**Versioning and enforcement**

- Additive changes to `meta` do not bump `schema`; readers ignore unknown keys. A breaking change bumps it, and the reader keeps supporting every prior version because files are never rewritten.
- The Python model is the source of truth (msgspec for speed and native JSON Schema generation; pydantic v2 is the fallback if msgspec's ergonomics grate). `schema/envelope-v1.json` is generated from it and committed; a test fails if they drift.
- `slarc validate [--fast]` parses every line against the schema and reports file and line of anything off; `--fast` checks `meta` only. Every fixture is validated in CI.
- The DuckDB adapter passes the `meta` columns explicitly to `read_ndjson` and types `object` as `JSON`, so nothing about the files is inferred.

## Config

Config is layered like git's: a global file at the XDG path and a local file discovered by walking up from the current directory, local winning per key.

Resolution order, highest first:

1. Command-line flags (`--archive`, `--root`).
2. Environment: `SLARC_ARCHIVE`, `SLARC_ARCHIVE_ROOT`, `SLARC_CONFIG`.
3. The nearest `slarc.toml` found walking up from the current directory.
4. `$XDG_CONFIG_HOME/slarc/config.toml`, which is `~/.config/slarc/config.toml` on every platform.
5. Built-in defaults: `default_archive = "default"` with its root under `$XDG_DATA_HOME/slarc/default`.

Tables merge key by key, so a local file can add one workspace mapping without restating the global ones. Relative paths resolve against the file that declares them. A directory whose `slarc.toml` says `root = "."` is a self-contained archive you can `cd` into and run bare commands against.

```toml
# ~/.config/slarc/config.toml
default_archive = "personal"

[archives.personal]
root = "~/.local/share/slarc/personal"

[archives.work]
root = "/Volumes/Encrypted/slarc-work"

[workspaces]
naleidworkspace = "personal"
acme = "work"
```

Routing: `--archive`, then `$SLARC_ARCHIVE`, always wins. Otherwise, when a command carries a URL or selector, the workspace domain picks the archive through `[workspaces]`; an unmapped workspace prompts once (or takes `--archive`) and writes the mapping to the global file. With neither flag nor selector, `default_archive`. If `--archive` names a different archive than the selector's mapping, slarc warns and uses `--archive`. `slarc config` prints the merged result and the file each value came from.

## CLI

The command is `slarc`, built with Typer and Rich progress. Every command resolves an archive as described in Config: `--archive NAME`, then `$SLARC_ARCHIVE`, then the workspace mapping when a URL or selector is given, then `default_archive`. `--root DIR` points at an ad-hoc archive directory and bypasses the named archives. `serve`, `search`, `status` and `sql` see one archive only and need no Slack credentials.

| Command | What it does |
| --- | --- |
| `slarc auth [--paste \| --xoxp TOKEN] [--list]` | Discovers desktop-app sessions, runs `auth.test` per workspace, prints user and team, runs the rate-limit probe. `--paste` prompts for an xoxc/xoxd pair and stores it in the keyring |
| `slarc config [--origin]` | Prints the merged config; `--origin` shows which file each value came from |
| `slarc archive <selector> [--since DATE] [--until DATE] [--thread] [--no-files] [--max-file-size MB]` | Archives one conversation or thread into the archive its workspace maps to. Selector: an `/archives/...` URL, a conversation id, `#name`, or `@user` for a DM. Re-running on a tracked conversation is incremental |
| `slarc sync [--all \| <selectors>] [--no-files]` | Incremental: fetches newer than `newest_ts`, re-fetches threads whose `latest_reply` advanced, refreshes users, emoji and conversation metadata, appends a line for anything whose hash changed, then drains the file queue |
| `slarc files [--retry-failed]` | Drains the download queue on its own |
| `slarc list [--kind channel\|im\|mpim] [--archived] [--remote]` | Lists conversations in the archive; `--remote` asks Slack for everything the session can see and marks which are archived locally |
| `slarc search "<query>" [--in #chan] [--from @user] [--after DATE] [--before DATE] [--has-file]` | DuckDB FTS over `meta.plain`; prints permalinks. `rg` over `raw/` works too |
| `slarc serve [--port 8080] [--open] [--tz ZONE]` | Local web app over one archive; loads the files into in-memory DuckDB at startup |
| `slarc export html <selectors> [--out DIR] [--single-file] [--include-dms] [--no-system] [--tz ZONE] [--redact emails,ids]` | Static bundle; `--out` defaults to the current directory |
| `slarc export pdf <selectors>` | Phase 3 |
| `slarc export slack-zip [--workspace X] [--out FILE]` | Native export format, so slarchive, slack-export-viewer and the like work on it; also how one workspace is carved out of an archive |
| `slarc export bundle [--out FILE]` | Files plus blobs as one zip for recipients who won't use git LFS |
| `slarc import <export.zip \| slackdump.sqlite>` | Seeds the archive without API calls |
| `slarc status` | Per-conversation counts, cursors, pending files, last run; reads `runs.jsonl` plus a DuckDB count over `raw/` |
| `slarc sql "<query>"` | Runs a DuckDB query against the archive's views; the escape hatch for anything the CLI doesn't cover |
| `slarc validate [--fast]` | Checks every line against the envelope schema; `--fast` checks `meta` only |
| `slarc fixtures record <selector> --obfuscate [--out DIR]` | Records real API responses, scrubs names, emails and ids, and writes golden fixtures for tests |

URL and selector parsing is a pure function with property tests:

- `/archives/<id>` → conversation. `/archives/<id>/p<16 digits>` → message `ts` with a dot before the last six digits; a `?thread_ts=` query param wins when present.
- `--thread` on a message URL archives that thread only, plus the conversation metadata and every user and emoji it references. It writes a `thread_cursor` and no `sync_cursor`, so the channel is not tracked; `sync --all` re-checks tracked threads with `conversations.replies` and compares `latest_reply` to `latest_reply_seen`.
- A message URL without `--thread` archives the whole conversation and prints the permalink's local equivalent.

## Rendering

One set of Jinja2 partials drives all three outputs: `serve` (Starlette plus HTMX), `export html` (the same partials written to static files with a small JS viewer), and `export pdf` (Playwright/Chromium print-to-PDF over the static HTML).

| Element | Requirement |
| --- | --- |
| Text | Render `blocks` rich\_text when present: sections, nested bullet/ordered lists with `indent`, quotes, preformatted blocks; styles bold, italic, strike, code, underline, highlight; links; user, channel, usergroup, broadcast and date elements. Fall back to a mrkdwn parser for `text`. Unknown elements degrade to escaped text |
| Mentions | Resolve to the current display name, linking to the archived profile; show the `user_profile` snapshot name on hover where present; unknown ids render as the raw id |
| Emoji | Standard → unicode with skin tone applied; custom → local blob (animated GIF in HTML, first frame via `deanimate_gif` in PDF); deleted or unknown → `:name:` text |
| Reactions | Emoji plus count, tooltip listing names |
| Edits | "(edited)" marker with time; optional "observed versions" view in `serve` |
| Files | Inline images and thumbnails from blobs; snippets syntax-highlighted with full content, filename and mimetype; video and audio players in HTML; download links; explicit placeholders for `hidden_by_limit`, `tombstone` and external files |
| Previews and app attachments | Render `attachments`: title, text, image or thumb, service icon, color bar; covers giphy and app posts. Only what the latest observed version carries |
| Threads | Reply count and participant avatars under the parent; side panel in `serve`, inline expandable in static HTML; broadcasts in both places |
| System messages | Joins, leaves, topic changes shown muted and collapsible; hidden entirely with `--no-system` |
| Time | Day dividers and timestamps in the viewer's timezone (configurable, default local); author's local time on hover |
| Navigation | Channel and DM list, jump to date, permalink-stable anchors `#<channel>/<ts>` so Slack links can be rewritten to the archive |
| Exports | Self-contained folder by default; `--single-file` inlines assets as base64 for small exports. DMs excluded unless `--include-dms`; `--redact` strips emails and optionally user ids |

PDF trade-offs, accepted for phase 3: animations become first frames, video becomes a poster and link, non-image attachments become link plus metadata. WeasyPrint is the lighter alternative if Chromium is unwanted, at the cost of modern CSS.

## Architecture

Hexagonal, in the Cosmic Python shape: a pure domain, a service layer of use cases, ports as abstract classes, and adapters that implement them. Nothing in the domain or service layer imports `slack_sdk`, `duckdb`, `httpx`, or `keyring`, and nothing below the adapters touches the filesystem.

&#91;embedded content: hexagonal layout · 3 driving adapters, 7 ports\]

The CLI and viewers call the service layer; the service layer reaches Slack, the archive files, blobs and secrets only through ports, so tests swap in the in-memory adapters.

**Layers**

- Domain: dataclasses for the entities in the data model, value objects for `MessageTs` (string compare, dot insertion from permalinks), `Permalink`, `ContentHash`, `EmojiRef(team_id, name)`. Pure functions for the mrkdwn and rich\_text renderers and for `meta.plain` live here too, since they have no I/O.
- Ports: `SlackGateway` (list\_conversations, conversation\_info, history, replies, users, bots, emoji, file\_info, download); `ArchiveStore` (append envelopes, read the last line per key for a conversation or directory kind, iterate lines in order, read the last run per key, append a run); `QueryIndex` (what `serve`, `search`, `status` and `sql` read); `BlobStore` (put/get/exists by sha256); `SecretStore` (credentials by team\_id); `Clock`. `UnitOfWork` scopes one run: buffer appends, fsync files, append the run record.
- Adapters: `SessionTokenGateway` wraps `slack_sdk.WebClient` with the xoxc token, `d` cookie header, `RateLimitErrorRetryHandler`, `ConnectionErrorRetryHandler`, and a per-method token bucket. `XoxpGateway` (phase 3) is the same wrapper with an app token. `ExportZipGateway` serves a native export through the same port. `JsonlArchiveStore` owns the month, directory and run files. `DuckDbQueryIndex` builds views over the JSONL globs in memory; a persisted cache file is the phase 3 option. `DesktopSessionStore` reads the Slack desktop app's cookie store and local storage. `FakeSlackGateway`, `InMemoryArchiveStore`, `InMemoryQueryIndex`, `InMemoryBlobStore`, `FakeSecretStore` and `FakeClock` exist for tests, composed by `InMemoryUnitOfWork`.
- Service layer: one function per CLI verb, taking the UoW and gateway as arguments. `archive_conversation` refreshes the directory first, pages history, appends envelopes whose hash is new, emits `FileReferenced`, `CustomEmojiSeen` and `UserReferenced` events; handlers on the message bus download or refresh, so a failed download never stalls paging and can be retried by `files --retry-failed`.
- Config: a `Settings` value object built by the layered loader (flags, env, nearest local file, global file, defaults) and passed into the bootstrap; nothing below the entrypoints reads files or environment.
- Entrypoints: Typer CLI, Starlette app, static exporter. Each composes the real adapters in a small bootstrap module.

**Tests**

- Service tests run against `FakeSlackGateway`, `InMemoryArchiveStore`, `InMemoryQueryIndex` and `InMemoryBlobStore`. No network, no disk, milliseconds each.
- Renderer tests are golden-file: obfuscated real messages covering every subtype and rich\_text element, recorded from my workspace with `slarc fixtures record --obfuscate` (Slackdump's obfuscator is the precedent).
- Adapter contract tests use VCR cassettes (`pytest-recording`) with credentials scrubbed; the same test module runs against the fake and the real adapter to prove they agree. `JsonlArchiveStore` and `DuckDbQueryIndex` get the same treatment against a temp directory.
- Property tests (Hypothesis) for permalink parsing, `ts` ordering, and idempotency: archiving the same fixture twice appends no lines, and the DuckDB views over the result are identical.

**Stack**

Python 3.12+, uv, `slack_sdk` ≥ 3.45, `httpx`, Typer + Rich, Jinja2, Starlette + HTMX, `duckdb`, `msgspec`, `jsonschema`, `keyring`, Hypothesis, pytest-recording. Later: Playwright for PDF.

## Phases

Phase 0 is a two-hour spike that proves the credential path and the data shapes before any architecture exists.

1. **Phase 0, spike.** Read the xoxc/xoxd pair from the desktop app's store, call `auth.test`, run the rate-limit probe, and dump one channel's raw `conversations.history` and `conversations.replies` JSON to disk. Also `users.list`, `emoji.list`, and one `files.info` plus download. Those JSON files become the first fixtures, and a DuckDB `read_ndjson` over them proves the query path.
2. **Phase 1, MVP.** Layered config with named archives, `auth` (session sources only), `config`, `archive` (channel, DM, thread, date range), the JSONL store with month files, directory files and `runs.jsonl`, `meta.plain`, `validate`, DuckDB views, users and emoji directory, file and avatar downloads to the blob store, `files`, `sync`, `status`, `list`, `sql`, and a minimal `export html` showing threads, reactions, images and snippets. Ships when archiving the same channel twice appends nothing and a dormant-thread reply and an old-message edit each land as exactly one new line.
3. **Phase 2.** `serve` with HTMX, FTS `search`, observed-version history in the viewer, `import` from export ZIP and Slackdump SQLite, `export slack-zip` and `export bundle`, `--redact`, `fixtures record --obfuscate`, Slack Connect `source_team` resolution. Pins and bookmarks move from the MVP non-goals list into scope here.
4. **Phase 3.** `export pdf`, the `xoxp` adapter and `auth --xoxp`, canvases and huddles (also MVP non-goals), scheduled `sync`. Only if measured: a persisted DuckDB or Parquet cache behind `QueryIndex`, and a `compact` that folds superseded lines out of old month files.

## Prior art

Each project in `.llm/` is worth reading for one thing; none is a dependency.

| Project | What it is | Take from it | Leave behind |
| --- | --- | --- | --- |
| [gammons/slk](https://github.com/gammons/slk) (Go TUI, MIT) | Slack client over the web client's protocol; SQLite-backed scrollback | Desktop-app credential extraction: cookie DB, "Slack Safe Storage" keychain key, `localConfig_v2`; browser-token fallback (v0.25.0); local-first cache of users, channels, messages; terminal mrkdwn and emoji renderer | TUI concerns; its SQLite cache; any Edge endpoints it uses for presence or unread counts |
| [rusq/slackdump](https://github.com/rusq/slackdump) (Go, AGPL-3.0) | Archiver: `archive` to SQLite (resumable), `export` to Slack-ZIP, `view`, `emoji`, `search`, `convert` | Edge cases: resume, channel enumeration (issue #544), file handling, Free-plan redaction, `hidden_by_limit`; its obfuscator for fixtures; its README warning about security alerts | Code. AGPL means read, don't vendor |
| [korotovsky/slack-mcp-server](https://github.com/korotovsky/slack-mcp-server) (Go, MIT) | Community-standard Slack MCP; xoxp, xoxb, or xoxc/xoxd "stealth" mode | Token-type matrix and priority; users and channels cache files; limiter plus `Retry-After` design and the pagination bug in issue #231 | Edge/`client.*` package; custom TLS fingerprint and user-agent spoofing; CSV-for-LLM output |
| [devrelopers/slarchive](https://github.com/devrelopers/slarchive) (HTML, MIT) | Single `index.html` viewer for a Slack export ZIP, client-side with JSZip | Proof that a zero-backend viewer works; its channel, DM, thread, reaction and file rendering as a visual reference for `export html` | Reads export ZIP only; `export slack-zip` is what makes it useful |
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (Python) | Agent platform with a Slack adapter over slack-bolt Socket Mode | Manifest generated from code; subtype allowlist (issue #110778); pin `slack-sdk ≥ 3.45.0` | Bolt and Socket Mode: push-based, irrelevant to a polling archiver |
| [hfaran/slack-export-viewer](https://github.com/hfaran/slack-export-viewer) (Python, Flask) | Export viewer, server or `--html-only` | Jinja template structure for channels and threads | Flask |
| [Cosmic Python](https://www.cosmicpython.com/) (`.llm/cosmic-python-book`) | Repository, service layer, UoW, message bus, hexagonal layout | The whole shape of the Architecture section; the fake-repository and fake-UoW test pattern | SQLAlchemy and its ORM mapping, Flask, and the relational-store assumption: here the "repository" appends to JSONL and the UoW's commit is an fsync |

Slack's official MCP server (GA Feb 2026) is a hosted app aimed at assistants and returns prose rather than message JSON; it is not a data source for this project. Beware the near-collision in names: slarchive is devrelopers' viewer, slarc is this project.

## Open questions

- [ ] Delete policy: when a message disappears upstream, hide it in renders or show it as "deleted"? Default proposed: show in `serve` with a marker, hide in exports.
- [ ] Should exports include DMs or emails by default? Proposed: no, opt-in with `--include-dms`.
- [ ] Max file size and whether to download video. Proposed: 50 MB default, video opt-in.
- [ ] Encryption at rest for an archive that holds DMs: the files are plain JSONL, so this is a filesystem concern (an encrypted volume, as in the `work` example) unless slarc should encrypt lines itself. Proposed: document the encrypted-volume approach and don't build encryption in.
- [ ] Single-file HTML with base64 assets or a folder bundle as the default share format.
- [ ] Local FTS only, or also `search.messages` through the session for pre-archive discovery?
- [ ] Scheduled `sync` via launchd or cron, and whether it should alert on an expired session or a rate-limit clamp.
- [ ] macOS specifics of the desktop-app key extraction: slk's Linux path is documented; confirm the macOS Keychain item name and whether reading it prompts for the login password each run.
- [ ] Blobs in a shared repo: git LFS, or exclude `blobs/` and ship `export bundle` separately? Proposed: `.gitattributes` for LFS by default, with a documented opt-out.
