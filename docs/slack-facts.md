# Verified Slack facts

Facts checked against the `naleidworkspace` test workspace (team `T0CB5LYPVFA`, channel `C0CA5396G03`) from macOS with the Mac App Store build of Slack. The spec marks the facts it relies on as (verified). The raw responses are in `tests/fixtures/spike/`.

## Credentials

- The App Store build keeps its data in `~/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack`.
- Keychain item: service `Slack Safe Storage`, account `Slack App Store Key`, read with `/usr/bin/security find-generic-password -w`.
- The `Cookies` DB has `meta.version = 24`, so the decrypted `d` cookie carries a 32-byte domain-hash prefix that must be stripped. Decryption is AES-128-CBC, PBKDF2-SHA1, salt `saltysalt`, 1003 iterations, IV of 16 spaces, `v10` prefix. The result starts with `xoxd-`.
- `localConfig_v2` in `Local Storage/leveldb` holds an `xoxc-` token for every workspace the desktop app is signed into, including Enterprise Grid orgs with an `E` team id. slarc must select the workspace from the link and never touch the others.
- `ccl_chromium_reader` parsed the leveldb in about 1.5 s. It is not on PyPI and its package pulls native Brotli and zstd wheels; only its pure-Python leveldb, snappy and localstorage modules are needed.
- `WebClient(token=xoxc, headers={"Cookie": f"d={xoxd}"})` works for every documented method tried.

## API behavior with a session token

- `auth.test`, `conversations.list/info/history/replies`, `users.list`, `emoji.list`, `usergroups.list`, `bots.info`, `files.info` and `search.messages` all succeeded, each in 0.2 to 0.45 s. No throttling at this scale; real rate limits still need measuring on a large channel.
- `search.messages` works. `in:<#C0CA5396G03>`, `in:#all-naleid`, `is:thread` and `after:YYYY-MM-DD` all filter as expected. `in:<#D...>` with a DM's id filters to that DM the same way. Matches have no `thread_ts` field, but a reply's `permalink` carries `?thread_ts=...`, which is enough to find the parent of a new reply in an old thread.
- `conversations.info` returns `last_read`; `unread_count` is absent for channels.

## Message shapes

- `/giphy` posts are **not** legacy attachments. The message has the poster's `user`, a `bot_id` and `bot_profile` for the giphy app, no `app_id` at top level, and `blocks` of type `image` (giphy-hosted GIF with `is_animated`, `image_width`, `image_bytes`) and `context` ("Posted using /giphy"). Rendering must handle top-level layout blocks such as `image`, `context` and `section`, not only `rich_text`.
- Link previews are legacy `attachments` with `from_url`, `original_url`, `title`, `title_link`, `text`, `service_name`, `service_icon`, `thumb_url`, `thumb_width/height` and `media_type: external_link`.
- Thread parents carry per-user fields `last_read` and `subscribed`, as well as `reply_users`, `reply_users_count` and `is_locked`. None of them belong in the version hash.
- No message carried a `user_profile` snapshot. Both human users have an empty `profile.display_name`, so name display must fall back to `real_name`, then `name`.
- `wave` and `wave::skin-tone-3` arrive as separate reaction entries; the Slack client shows them as one grouped reaction.
- Edited messages carry `edited: {user, ts}` as documented.
- `emoji.list` returns custom emoji as URLs and aliases as `alias:<name>`. An alias can point at a standard emoji (`white_square` -> `alias:white_large_square`), so alias resolution must fall back to the standard emoji table.

## Files

- Snippets download in full from `url_private_download` with the bearer token and `d` cookie. `10k-snippet.txt`: 228,893 bytes, matching `size`, 10,000 lines. `test-snippet-2.txt`: 13,092 bytes, matching `size`, 600 newline-terminated lines (its message says 601; the original file has not been checked).
- The download's `Content-Type` is `application/force-download` for snippets, so the file object's `mimetype` is the source of truth.
- `files.info` for a snippet returns the full text in a top-level `content` field (all 10,000 lines), plus Slack's syntax-highlighted `content_highlight_html` and `content_highlight_css`, next to the `file` object. Only `file` is the file object; storing the top-level response would put the snippet text in the file's directory entry as well as the blob.
- A deleted file appears inline as `mode: tombstone`, and `files.info` returns the error `file_deleted`. It needs a placeholder and no retry.
- A hosted PNG downloaded with matching size and `Content-Type: image/png`.
