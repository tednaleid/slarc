# slarc backlog

Known problems and future work, grouped by theme. Each line says what is wrong or missing and where.

## Known issues

### Pull
- The downloads drain after a mid-pull `AuthFailed` can re-raise it, so no summary prints; the exit code and run row are still right (`service/downloads.py`).
- When a conversation turns `NotVisible` partway through a pull, deletes already appended for its re-read threads stay (`service/pull.py`).
- A new link whose first read raises `NotVisible` is tracked as `not_visible`, while a `ThreadNotFound` link is not tracked (`service/pull.py` `_message_item`).
- The "Slack returns no message" warning link leaves out `thread_ts` for a reply (`service/pull.py` `_missing_message`).
- Bots are fetched only when not yet stored, so a bot rename is never recorded, and `bots.info` errors other than `AuthFailed` are dropped without a warning (`service/directory_refresh.py` `unknown_bots`).
- A tracked `message` item keeps kind `message` after it gains replies, so `slarc list` shows "message" for a thread (`service/pull.py`).
- `_append_bots` mutates the frozen `DirectoryView`'s `bots` dict (`service/pull.py`).
- `--max-file-size 0` is left out of the run row's args because `0 == False` (`cli.py` `_run_args`).
- A skipped `not_visible` item shows a finished 0-message progress bar (`cli.py` `RichReporter`).
- Images over the size limit stay `too_large` after the limit is raised until a pull with `--retry-skipped`; files are retried on their own (`service/downloads.py` `_image_state`).
- A file whose recorded `size` is missing or wrong is queued again on every read (`service/downloads.py` `_file_state`).
- Bot directory entries never get a blob for the bot icon (`service/downloads.py`).
- A 403 or 404 after an off-host redirect of a Slack file is recorded as terminal `forbidden` or `not_found` instead of `failed` with the session hint (`adapters/downloader.py`).
- The download status `Literal` is spelled out in `ports.py`, `domain/tracking.py`, `adapters/downloader.py` and `service/downloads.py`; `service/selectors.py` repeats the `ItemKey` type inline.
- Under `--full` or a reopened backfill, a thread in `retry_threads` is read twice in one run: first as a retry, then again as a parent in the paged history, because `_append_history_page` does not skip threads already in `held.read_threads` (`service/pull.py`).
- When a conversation turns `NotVisible` partway through paging, `_skip` records the item as it was before paging, discarding the per-page `oldest_ts` progress; the next pull re-pages more than needed (`service/pull.py` `_pull_conversation`).
- A bare `slarc pull --root DIR` where DIR has no `slarc.toml` says "No tracked items yet", which hides that DIR is not an archive; untested (`cli.py`).
- A maintenance failure after a clean pull raises `MaintenanceFailed` out of the unit of work, so the command prints only the error and loses the pull summary and exit handling, although everything was committed (`unit_of_work.py` `__exit__`, `cli.py` `pull_command`).
- `_skip` warns before it records the item, and `_record` can raise `CommitConflict`, which the caller reports a second time as "Could not finish pulling" (`service/pull.py` `_skip`).
- The rm run row's `blobs_deleted` is the planned count even when maintenance deferred the deletion (`service/remove.py` `apply_removal`).
- When a normally ending run's last commit fails with an error other than `CommitConflict`, maintenance is skipped, and if writing the run row then fails too, that error replaces the commit's error (`unit_of_work.py` `__exit__`).
- A run that raises, such as an rm that keeps conflicting, writes a run row with no `errors` entry, so `slarc sql` cannot say why it failed (`unit_of_work.py` `__exit__`, `service/remove.py`).
- The pull summary and the spec say "directory lines" and the run row counts `lines_appended`, though directory entries and messages are rows (`cli.py` `_summary`, `docs/spec.md`).

### Auth
- An `SLARC_DOMAIN` that does not match the env token's workspace still tracks items under the token's team (`service/workspaces.py` `Workspaces._open`).
- `READ_ERRORS` includes `TypeError` and `AttributeError`, so programming bugs show as credential read failures (`adapters/desktop_session.py`).
- `KeyringSource.store` writes the entry before the index, so a failure between them leaves an orphan entry (`adapters/credentials.py`).
- `slarc auth --paste` ignores a WORKSPACE argument without saying so, and credential source warnings show only when no session works (`cli.py` `auth`).
- Cookie decryption does not check the `v10`/`v11` prefix (`adapters/chromium.py`); an unknown LevelDB batch record kind is read as a delete (`adapters/leveldb.py`); `read_varint` lives in `snappy.py`.
- A non-JSON Slack response gives a long error and a slack_sdk ERROR log on stderr (`adapters/slack_gateway.py`).

### Storage and query
- `int(ts.split(".")[0])` is repeated in `cli._ts_time`, `export._seconds` and `threads.active_threads`; give `MessageTs` a `seconds` property and a `from_datetime` constructor.
- A message with `files: []` hashes differently from one with no `files` key (`domain/hashing.py`).
- Blobs are written with mode 0600 and not fsynced before the rename (`adapters/blob_store.py`).
- `slarc list` loads every message of each tracked item to count them, through raw SQL on `QueryIndex.sql` instead of a narrow method (`service/listing.py`).
- `service/listing.py` imports `Settings` from `config.py`; move it to a pure module or pass the fields.
- `slarc list` shows the team id as the domain when no run has an `auth` entry for the workspace (`service/listing.py`).
- With `--root DIR` and no `slarc.toml`, `NoArchive` still says "here or in any parent directory" (`ports.py`, `cli.py`).
- `FsBlobStore` write failures surface as a raw `OSError` traceback, and `export_writer.py` raises a plain `SlarcError` rather than `ArchiveWriteFailed` (`adapters/blob_store.py`, `adapters/export_writer.py`).
- A failing `ROLLBACK` replaces the error that caused it, in the store's `_transaction` and in `_record_owner` (`adapters/lake_store.py`, `adapters/lake.py`).
- The first connect runs `INSTALL` for the DuckDB extensions while holding the archive's write lock, so a slow download blocks every other slarc command on that archive (`bootstrap.py` `ensure_lake`, `adapters/lake.py` `_load_extensions`).
- A takeover checks that each file the catalog references exists, not that its size matches `file_size_bytes`, so a partly synced Parquet file passes (`adapters/lake.py` `_missing_files`).
- Any file in `data/` or `blobs/`, such as `.DS_Store`, makes `connect_lake` refuse to create a catalog with `CatalogNotSynced` (`adapters/lake.py` `_has_files`).

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
- The reaction popup lists a line for a zero-count variant, and the emoji loop is written twice in `_message.html.j2`.
- Closing a thread panel leaves keyboard focus on the hidden Close link (`_thread_panel.html.j2`).
- Replies whose parent is missing render with no label (`service/export.py`).
- Exporting a link to a deleted message that is not in a thread says "Pull it first" (`service/export.py` `_page`).
- `meta.plain` writes a bare "@" or "#" when an element has no id, drops any exactly repeated part, and unescapes entities inside resolved names; `plain.mention_text`'s default case relies on callers filtering (`domain/plain.py`).
- `DirectoryView.author_display` ignores the bots dict and falls back to "unknown" (`domain/directory.py`).

### Tests and tooling
- Only link parsing has Hypothesis tests; the spec also asks for `ts` ordering and idempotency properties.
- Nothing checks that the `user.display` column and `SEARCH`'s labels agree with `DirectoryView` (`tests/contract/test_duckdb_index.py`).
- LevelDB and cookie tests: the highest-sequence test also passes under first-seen-wins; no cross-file delete, `v11`, journal copy, "no d cookie" or known-answer decryption test (`tests/adapters/`).
- Credential source tests miss the no-d-cookie mapping, a missing `Cookies` file, the `Network/Cookies` fallback, the Snap directory and a malformed keyring entry (`tests/adapters/test_desktop_session.py`, `test_credentials.py`).
- No test that a failing chunk iterator removes the temp blob (`tests/contract/`).
- No test of the non-read-method refusal in `SessionTokenGateway._call`; `FakeSlackGateway.list_conversations` and `users` do not page.
- Plain text tests miss a subteam without a label, lists and preformatted blocks, and several `rich_text` blocks (`tests/domain/test_plain.py`).
- Pull tests miss an interrupted newer-history fetch, `--channel` with a date range, and a search hit for a thread already read this run.
- Export tests do not cover the video and audio players or the system-message group (`tests/service/test_export.py`).
- Acceptance notes recorded before an exception in a check are lost from the output (`scripts/acceptance.py` `attempt`).
- No test pins a history-page `SlarcError` on page 1 of a new conversation, or that page-1 progress (`oldest_ts`) is kept and a second pull finishes the backfill; extend `test_failed_history_page_skips_the_conversation_and_pulls_the_rest` (`tests/service/test_pull_conversation.py`).
- The `retry_threads` rule is untested for a thread that fails again on retry and for a pull interrupted before any record; `test_interrupted_pull_keeps_every_new_item` does not assert `backfill_complete` is false right after the interrupt (`tests/service/test_pull_conversation.py`).
- No contract test shows that a `CommitConflict` clears a staged drop of a committed row; the conflict test drops only an appended row (`tests/contract/test_archive_store.py`).
- The rm idle test lets the DuckLake column-stats tables mention the removed id for any table, so a leak through `message_version` statistics would pass (`tests/test_cli_rm.py` `test_rm_physically_removes_a_conversation_when_idle`).
- Untested: `ensure_lake`'s lock order, the index releasing its shared hold when its open fails, two concurrent creators of a catalog, `SyncNotFinished` and `OwnedElsewhere` on a store-opening command, and that later opens in a process taking ownership keep the cautious flag (`tests/adapters/test_locks.py`, `tests/adapters/test_lake_owner.py`).

## Planned

### Pull
- `import` (likely `slarc pull <export.zip>`) from Slack export ZIPs and slackdump SQLite.
- Resolve Slack Connect authors' home teams from `source_team`.

### Auth
- A probe that warns when Slack starts limiting session tokens.
- A global config list of workspaces slarc must never read, refused before any Slack call even when the desktop session holds their tokens, so an agent digging into something cannot pull from them by accident. Discuss before building: matching by domain or team id, which commands it blocks, and whether `list --remote` and `auth` hide them too.

### Storage and query
- Full-text search with filters.
- `list` reads every message of each tracked item, one query per item, to match its pending and skipped downloads, so its time grows with the archive; counting them in one SQL query would not (`service/listing.py`). One DuckDB thread would save about 55 ms per connection on a small archive but makes large scans several times slower; a thread count chosen from the archive's size could have both (`adapters/lake.py`).
- On Linux a machine without `/etc/machine-id` raises `NoMachineId`; fall back to `/var/lib/dbus/machine-id` (`adapters/host.py` `machine_id`).

### Export and rendering
- `slarc serve`: a local web viewer over one archive (Starlette and HTMX).
- Observed-version history of a message in the viewer.

### Tests and tooling
- Obfuscated fixture recording from real workspaces.
- No contract test pins the message view's `first_seen_at` and `last_seen_at` (`tests/contract/test_duckdb_index.py`).

## Someday

### Pull
- Pull pins and bookmarks.
- Canvases and huddles; they render as placeholders.
- Scheduled pulls (launchd or cron). The useful part is a distinct exit code for an expired session, so a scheduler can alert on it.

### Storage and query
- Less space for link-preview images, if a large workspace shows the need. In a sample conversation (793 messages, 33 MB of blobs) uploads were 78% of the bytes (three PDFs alone 48%), link-preview images, thumbnails and site icons about 20%, and avatars and emoji 2%. Every skipped download was a preview image or site icon. zstd at level 19 saved 3.2% overall (0.2 to 0.5% on PDFs and JPEGs, 6% on PNGs, 10% on preview images), so compressing blobs is not worth it. Options: export loads preview images from their sites lazily instead of copying them (the export about 20% smaller, but viewing it contacts those sites); pull skips preview images behind a setting (the archive smaller too, but a rotted preview is lost for good); or keep downscaled copies (lossy, needs an image library).

### Export and rendering
- PDF export (Playwright print-to-PDF over the static HTML).

## Open questions

- Linux credential details: Secret Service item names for the Slack Safe Storage key, and the Snap and Flatpak data directories.
- How Enterprise Grid (`E…` org ids with `T…` workspaces) shows up in links, `auth.test` and message `team` fields. Hosts like `foo.enterprise.slack.com` parse to domain `foo.enterprise` and are refused for env sessions (`domain/links.py`).
