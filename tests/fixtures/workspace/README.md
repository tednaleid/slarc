# Recorded workspace fixtures

An archive pulled from the `naleidworkspace` test workspace (team `T0CB5LYPVFA`) by `just record-fixtures`, which runs `scripts/record-fixtures.py`. The recorder pulls with slarc's own pull service and real adapters into a temporary archive, using only the desktop session for the test workspace, then copies that archive here. Tests read it as an archive made by a real pull, without workspace access.

## How it was made

1. Ted made some content by hand in the Slack client (listed below).
2. `just seed-test-workspace` ran `scripts/seed-test-workspace.py`, which posts the rest. It is the only code that writes to Slack, and it refuses any workspace other than `T0CB5LYPVFA`. It skips its work when `#slarc-fixtures` already holds the message `slarc seed v1`.
3. `just record-fixtures` pulled these conversations as conversation items and replaced everything in this directory except this README:
   - `#all-naleid` (`C0CA5396G03`), public
   - `#slarc-fixtures` (`C0CA7SNHTSP`), private
   - `#slarc-fixtures-archived` (`C0CAF9YVDMY`), public and archived
   - the DM between Ted and jim (`D0CAD7JE1QR`)
   - the group DM of Tim, jim and Ted (`C0CAG251117`)

The copy differs from the pulled archive in three ways:

- Email addresses are rewritten to `<local part>@example.invalid`, and the `meta.hash` of each rewritten line is recomputed.
- Blobs over 1 MiB are left out. The lines that reference such a blob keep their `meta.blob`, so a test that reads a blob must not assume every referenced blob is present.
- The recorder fails, and removes what it copied, if any file contains the three-letter prefix every Slack token starts with. This README avoids spelling it out for that reason.

## What it contains

- `#all-naleid`: a thread with a reply from jim carrying the animated custom emoji `:teamwork:` as a reaction, a reply using `:teamwork:` in text and one using its alias `:teamwork2:`; `wave` and `wave::skin-tone-3` reactions on another thread's parent; two snippets (one 10,000 lines long) and a message whose snippet was deleted (`mode: tombstone`); a hosted PNG; a link preview in `attachments`; a `/giphy` post with an `image` block.
- `#slarc-fixtures`: a huddle (`huddle_thread`) with a thread reply; a canvas made from a template, which appears only as a channel tab in the conversation's `properties.tabs` (file `F0CA8RBM16Z`, not in `files.jsonl`); a thread with a `thread_broadcast` reply; an edited message; a `tombstone` parent whose reply survives (a message deleted without replies leaves nothing); a rich text message with nested bullet and ordered lists, a quote, inline code, a code block, strike, a labelled link, `@jim`, `#slarc-fixtures` and `@here`; reactions including `wave::skin-tone-3` and `teamwork`; a PDF, a short MP4, a message with three PNGs and an uploaded `.html` file (Slack turns it into a `text/plain` snippet); a pinned message; `channel_topic` and `channel_purpose` messages.
- `#slarc-fixtures-archived`: three messages, then `channel_archive`.
- The DM and the group DM: a message each, and in the group DM a thread with replies from Ted and jim.
- `directory/`: users, emoji, conversations, bots, files and images. jim changed his name after posting: the spike's `users.list.json` has his old name, these users have `real_name` "Jim Naleid" and `display_name` "Jim".

Messages posted through the API with text only come back with `blocks` that Slack builds on each read, with a new random `block_id` every time, so those messages carry a line from both `conversations.history` and `conversations.replies` when both returned them.
