# slarc backlog

Future work, grouped by phase and then by theme. Each line says what is wrong or missing and where. Phase 1 shipped; its plan is `2026-10-19-phase-1.md`.

## Phase 2

### Pull
- `import` (likely `slarc pull <export.zip>`) from Slack export ZIPs and slackdump SQLite.
- Resolve Slack Connect authors' home teams from `source_team`.

### Auth
- A probe that warns when Slack starts limiting session tokens.

### Storage and query
- Full-text search with filters.
- A generated JSON Schema for the envelope and a `validate` check.

### Export and rendering
- `slarc serve`: a local web viewer over one archive (Starlette and HTMX).
- Observed-version history of a message in the viewer.
- Slack export ZIP output, so slarchive and slack-export-viewer can read an archive.

### Tests and tooling
- Obfuscated fixture recording from real workspaces.

## Someday

### Pull
- Pull pins and bookmarks.
- Canvases and huddles; they render as placeholders.
- Scheduled pulls (launchd or cron). The useful part is a distinct exit code for an expired session, so a scheduler can alert on it.
- Concurrent API calls, only if measurement shows the need.

### Auth
- An `xoxp` app-token adapter for workspaces where a sanctioned app is preferable; decide using the Phase 2 token-limit probe.

### Storage and query
- A persisted query cache in `$XDG_CACHE_HOME/slarc/`, only if measurement shows the need.

### Export and rendering
- PDF export (Playwright print-to-PDF over the static HTML).

## Phase 1 leftovers

### Pull
- The downloads drain after a mid-pull `AuthFailed` can re-raise it, so no summary prints; the exit code and run line are still right (`service/downloads.py`).
- When a conversation turns `NotVisible` partway through a pull, deletes already appended for its re-read threads stay (`service/pull.py`).
- A new link whose first read raises `NotVisible` is tracked as `not_visible`, while a `ThreadNotFound` link is not tracked (`service/pull.py` `_message_item`).
- The "Slack returns no message" warning link leaves out `thread_ts` for a reply (`service/pull.py` `_missing_message`).
- Bots are fetched only when not yet stored, so a bot rename is never recorded, and `bots.info` errors other than `AuthFailed` are dropped without a warning (`service/directory_refresh.py` `unknown_bots`).
- A tracked `message` item keeps kind `message` after it gains replies, so `slarc list` shows "message" for a thread (`service/pull.py`).
- `_append_bots` mutates the frozen `DirectoryView`'s `bots` dict (`service/pull.py`).
- Each workspace gets two `auth.test` calls per run, in `SessionConnector.connect` and `Workspaces._open`.
- A tracked DM may fail `search.messages` (DM search syntax is unverified) and warn on every pull; check `in:<#D...>` read-only and skip search for DMs if it fails (`service/pull.py` `_search_replies`).
- `--max-file-size 0` is left out of the run line's args because `0 == False` (`cli.py` `_run_args`).
- A skipped `not_visible` item shows a finished 0-message progress bar (`cli.py` `RichReporter`).
- Images over the size limit stay `too_large` after the limit is raised; only files are retried (`service/downloads.py` `_image_state`).
- A file whose recorded `size` is missing or wrong is queued again on every read (`service/downloads.py` `_file_state`).
- Bot directory lines never get a blob for the bot icon (`service/downloads.py`).
- An uploaded HTML file served as `text/html` would be recorded `failed`, since the job lacks the file's mimetype; Slack serves the fixture upload as a snippet (`adapters/downloader.py`).
- A 403 or 404 after an off-host redirect of a Slack file is recorded as terminal `forbidden` instead of `failed` with the session hint (`adapters/downloader.py`).
- Waits inside slack_sdk's 429 retry handler are not counted in `rate_limit_wait_s`, and the spec's run row does not say so (`adapters/slack_gateway.py`).
- The download status `Literal` is spelled out in `ports.py`, `domain/tracking.py`, `adapters/downloader.py` and `service/downloads.py`; `service/selectors.py` repeats the `ItemKey` type inline.
- Under `--full` or a reopened backfill, a thread in `retry_threads` is read twice in one run: first as a retry, then again as a parent in the paged history, because `_append_history_page` does not skip threads already in `held.read_threads` (`service/pull.py`).
- When a conversation turns `NotVisible` partway through paging, `_skip` records the item as it was before paging, discarding the per-page `oldest_ts` progress; the next pull re-pages more than needed (`service/pull.py` `_pull_conversation`).
- A download worker blocked in a socket read on a stalled connection still waits up to the httpx read timeout (60 s) after Ctrl-C; flowing downloads stop within a chunk (`adapters/downloader.py`).
- A bare `slarc pull --root DIR` where DIR has no `slarc.toml` says "No tracked items yet", which hides that DIR is not an archive; untested (`cli.py`).

### Auth
- An `SLARC_DOMAIN` that does not match the env token's workspace still tracks items under the token's team (`service/workspaces.py` `Workspaces._open`).
- On macOS both keychain accounts are read, which can raise two dialogs where both Slack builds are installed (`adapters/desktop_session.py` `_keychain_passwords`).
- `READ_ERRORS` includes `TypeError` and `AttributeError`, so programming bugs show as credential read failures (`adapters/desktop_session.py`).
- `KeyringSource.store` writes the entry before the index, so a failure between them leaves an orphan entry (`adapters/credentials.py`).
- `slarc auth --paste` ignores a WORKSPACE argument without saying so, and credential source warnings show only when no session works (`cli.py` `auth`).
- Cookie decryption does not check the `v10`/`v11` prefix (`adapters/chromium.py`); an unknown LevelDB batch record kind is read as a delete (`adapters/leveldb.py`); `read_varint` lives in `snappy.py`.
- A non-JSON Slack response gives a long error and a slack_sdk ERROR log on stderr (`adapters/slack_gateway.py`).

### Storage and query
- `MessageTs` compares fractions as integers, so a fraction shorter than six digits misorders, and `message_link` assumes six digits; Slack always sends six (`domain/links.py`).
- A non-numeric `ts` or malformed `thread_ts` raises a bare `ValueError` when compared instead of when parsed (`domain/links.py`).
- `int(ts.split(".")[0])` is repeated in `envelope.month_of`, `cli._ts_time`, `export._seconds` and `threads.active_threads`; give `MessageTs` a `seconds` property and a `from_datetime` constructor.
- Hashing a message whose file entry has no `id` raises `KeyError`, and `files: []` hashes differently from a missing `files` key (`domain/hashing.py`).
- Blobs are written with mode 0600 and not fsynced before the rename (`adapters/blob_store.py`).
- `stored_messages` copies the whole map on every call (`adapters/jsonl_store.py`, `archive_state.py`).
- `UnitOfWork.__exit__` can hide the original exception if `append_run` raises (`unit_of_work.py`).
- Every message query rescans all raw files twice; consider a temp table per invocation (`adapters/duckdb_index.py`).
- The message view breaks a same-second tie by the lower hash; a failing `CREATE VIEW` leaks the connection; helper views are visible to `slarc sql` (`adapters/duckdb_index.py`).
- `slarc list` loads every message of each tracked item to count them, through raw SQL on `QueryIndex.sql` instead of a narrow method (`service/listing.py`).
- `service/listing.py` imports `Settings` from `config.py`; move it to a pure module or pass the fields.
- `slarc list` shows the team id as the domain when no run has an `auth` entry for the workspace (`service/listing.py`).
- `slarc search --in @user` is refused although the spec gives `search` pull's selectors (`service/search.py`, `SEARCH` in `adapters/duckdb_index.py`).
- With `--root DIR` and no `slarc.toml`, `NoArchive` still says "here or in any parent directory" (`ports.py`, `cli.py`).
- The global config dict carries a `_path` sentinel key, and `_pick`'s error would print "None:" without it (`config.py`).
- Write failures other than opening a month file surface as a raw `OSError` traceback: `JsonlArchiveStore` write and flush, `FsBlobStore`; `export_writer.py` raises a plain `SlarcError` rather than `ArchiveWriteFailed` (`adapters/jsonl_store.py`, `adapters/blob_store.py`, `adapters/export_writer.py`).

### Export and rendering
- Emoji and image assets with no recorded content type take their extension from the URL, or `bin`; detect the type from the file signature instead (`service/export.py` `_image_extension`).
- A single-file export inlines each custom emoji, file image and preview thumb as a `data:` URI at every use; give each blob one CSS rule, as avatars have (`service/export.py` `_Assets`).
- Snippets show in full in a scroll box, without Slack's 5-line preview and line numbers (`_file.html.j2`, `viewer.css`).
- PDFs show a download link, not a preview card (`_file.html.j2`).
- Several images on one message stack vertically instead of forming a gallery (`_message.html.j2`, `viewer.css`).
- Consecutive messages from one author repeat the name and avatar instead of grouping (`conversation.html.j2`).
- Emoji-only messages are not shown large (`_message.html.j2`, `viewer.css`).
- Pinned messages have no "Pinned" label, though `pinned_to` is stored (`_message.html.j2`).
- The reaction popup aligns to the reactions row's left edge below 1441px and in thread panels instead of centering over its pill, so a right-hand pill's card sits far from it (`viewer.css`).
- While a reaction pill is hovered, its row-wide hover bridge takes clicks meant for content just above it (`viewer.css`).
- A reaction popup is clipped by up to 27px when its pill sits just under a sticky header or the thread panel header; a fix needs CSS anchor positioning (`viewer.css`).
- The reaction popup lists a line for a zero-count variant, and the emoji loop is written twice in `_message.html.j2`.
- Closing a thread panel leaves keyboard focus on the hidden Close link (`_thread_panel.html.j2`).
- A single-file export of a conversation plus one of its thread links writes four duplicate ids (`service/export.py`).
- `Thread.parent` is built on thread and message pages, where it is never rendered (`service/export.py`).
- Replies whose parent is missing render with no label (`service/export.py`).
- Exporting a link to a deleted message that is not in a thread says "Pull it first" (`service/export.py` `_page`).
- mrkdwn styles can nest wrongly, as in `*a _b* c_` (`domain/render/mrkdwn.py`).
- `meta.plain` writes a bare "@" or "#" when an element has no id, drops any exactly repeated part, and unescapes entities inside resolved names; `plain.mention_text`'s default case relies on callers filtering (`domain/plain.py`).
- `DirectoryView.author_display` ignores the bots dict and falls back to "unknown" (`domain/directory.py`).

### Tests and tooling
- Only link parsing has Hypothesis tests; the spec also asks for `ts` ordering and idempotency properties.
- Nothing checks that the `user.display` column and `SEARCH`'s labels agree with `DirectoryView` (`tests/contract/test_duckdb_index.py`).
- LevelDB and cookie tests: the highest-sequence test also passes under first-seen-wins; no cross-file delete, `v11`, journal copy, "no d cookie" or known-answer decryption test (`tests/adapters/`).
- Credential source tests miss the no-d-cookie mapping, a missing `Cookies` file, the `Network/Cookies` fallback, the Snap directory and a malformed keyring entry (`tests/adapters/test_desktop_session.py`, `test_credentials.py`).
- Config tests: the tilde test asserts only `is not None`, the default `resolve` test is tautological, and precedence lacks `active_thread_days` and `tz` (`tests/test_config.py`).
- No test that `stored_messages` does not rescan, or that a failing chunk iterator removes the temp blob (`tests/contract/`).
- No test of the non-read-method refusal in `SessionTokenGateway._call`; `FakeSlackGateway.list_conversations` and `users` do not page.
- Plain text tests miss a subteam without a label, lists and preformatted blocks, and several `rich_text` blocks (`tests/domain/test_plain.py`).
- Pull tests miss an interrupted newer-history fetch, `--channel` with a date range, and a search hit for a thread already read this run.
- The rename test has no precondition assert; `sql --root`, reaction `user_ids`, run view fields and the `list` count "14" are weakly asserted; `list --remote` with no workspace is untested.
- Export tests do not cover the video and audio players or the system-message group (`tests/service/test_export.py`).
- The chmod tests assume a non-root user.
- `record-fixtures.py` checks `auth.test`'s team only after the pull, its blob limit is 1 MiB while its message says 1 MB, and its `xox` scan includes binary blobs (`scripts/record-fixtures.py`).
- The seed script hard-codes ffmpeg's Homebrew path, an ffmpeg failure shows a traceback, and the "repeating this is harmless" comment describes a branch that runs once (`scripts/seed-test-workspace.py`).
- `ensure-acceptance` does not check that R is a reply under P (`scripts/seed-test-workspace.py`).
- Acceptance check (b)'s `new_lines()` slices the channel's lines by position across month files, so a run that crosses a month boundary can falsely FAIL (`scripts/acceptance.py`).
- Acceptance notes recorded before an exception in a check are lost from the output (`scripts/acceptance.py` `attempt`).
- `appends.counted()` returns 0 without complaint for a dict value, and the counter tests repeat `isinstance` narrowing (`service/appends.py`).
- No test pins a history-page `SlarcError` on page 1 of a new conversation, or that page-1 progress (`oldest_ts`) is kept and a second pull finishes the backfill; extend `test_failed_history_page_skips_the_conversation_and_pulls_the_rest` (`tests/service/test_pull_conversation.py`).
- The `retry_threads` rule is untested for a thread that fails again on retry and for a pull interrupted before any record; `test_interrupted_pull_keeps_every_new_item` does not assert `backfill_complete` is false right after the interrupt (`tests/service/test_pull_conversation.py`).

## Open questions

- `search.messages` query syntax for DMs and group DMs, for reply discovery outside channels.
- Linux credential details: Secret Service item names for the Slack Safe Storage key, and the Snap and Flatpak data directories.
- How Enterprise Grid (`E…` org ids with `T…` workspaces) shows up in links, `auth.test` and message `team` fields. Hosts like `foo.enterprise.slack.com` parse to domain `foo.enterprise` and are refused for env sessions (`domain/links.py`).
