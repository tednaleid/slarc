# Agent feedback on 0.1.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Act on two rounds of feedback from an agent that used slarc 0.1.2 against a real archive: names that match nothing fail loudly, real names, linked-message previews rendered once, readable export file names, snippet text in markdown and search, and a `slarc file` command.

**Architecture:** Most changes sit in the DuckDB views and search query (`adapters/duckdb_index.py`), the export service and its markdown renderer, and the CLI. One new service module (`service/files.py`) and one new command (`slarc file`). Search reads downloaded text files at query time through DuckDB `read_text`.

**Tech Stack:** Python 3.15, DuckDB 2.0 with DuckLake, Typer, Jinja2, pytest.

**Spec:** `docs/spec.md`. The decisions below (Q1 to Q11) were settled with Ted on 2026-10-27 and amend it; Task 10 writes them into the spec.

## Decisions

1. Search checks `--in`, `--from` and `--mentions` against the directory; a name it does not know exits 2 ("No conversation named #nope in the archive. See slarc list.", "No user named x in the archive."). A known name with no hits still exits 1.
2. `author_real_name` in `message_readable` and search output, falling back to `author`. Markdown heads read `**Real Name** (display)` when they differ. HTML unchanged.
3. A message unfurl (`is_msg_unfurl`) renders once in markdown: `> **author_name** [linked message](from_url)`, then its `message_blocks` (else its `text`) resolved, every line quoted. No preview label spans lines. HTML already shows unfurls once.
4. Export pages are named after their conversation in HTML and markdown: `general`, `@ada`, the group DM's name, `general-thread-<ts>` for a thread; the channel id is appended only when two names collide. A markdown directory export writes `index.md`.
5. The guide's "latest pull" recipe becomes "new since a time" (`first_seen_at >= <time the user names>`). No new command.
6. Search's `snippet` field becomes `excerpt`, shown in the table and left out of jsonl and csv.
7. `message_readable` gains `reactions` (`:+1: 3, :eyes: 1`) and `files` (a list of `{id, name}`), read from the message object.
8. No `--involving`.
9. Markdown export writes a snippet's text as a fenced block, language from `filetype`, cut at 200 KB with `[cut at 200 KB; the rest: slarc file F...]`; a preview-only snippet shows its preview and "Only the start of this snippet was archived." No opt-out flag.
10. `slarc file FILE_ID|FILE_PERMALINK [--out PATH]` writes one file's bytes; exit 1 with the reason when known but not downloaded, exit 2 when nothing matches. No `--info`, no message-link form.
11. Text search also matches the first 200 KB of downloaded snippets and `text/*` files, returning the message that carries the file, with the excerpt from the file.

## Global Constraints

- `just check` passes before every commit; test first.
- Domain and service never import duckdb, httpx, slack_sdk or keyring, and never touch the filesystem, environment or clock.
- Stdout carries only results; notes and errors go to stderr. Exit codes: errors 2, no match 1, interrupt 130.
- No real names of people anywhere committed: not in tests, docs, guide or commit messages. Use fixture names (`jim`, `contact`) or obvious placeholders (`ada`, `#general`, `acme`). The guide's `arun` placeholder becomes `ada`.
- Never commit anything from `archives/`.
- No emoji or em dashes. Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- `SNIPPET_LIMIT = 200 * 1024` bytes, one constant shared by markdown export and search.

## Review Focus

- A message unfurl whose `text` spans lines, inside a thread reply: every line carries the reply's `> ` and the unfurl's `> `.
- Two conversations with the same name in one export (two workspaces, or a channel and a group DM): file names stay distinct and the HTML index links resolve.
- A snippet blob that is not valid UTF-8, or larger than the limit: markdown and search neither crash nor read past the limit.
- `--from me` or `--mentions me` in an archive whose workspace has no auth user: exits 2 with a clear message, not 1.
- `slarc file` to stdout for a binary file writes bytes unchanged (no newline added, no encoding).

---

### Task 1: Unknown names exit 2

**Files:** `src/slarc/service/search.py`, `src/slarc/ports.py`, `src/slarc/adapters/duckdb_index.py`, `tests/service/test_search.py`, `tests/test_cli_list_search.py`

**Interfaces:**
- Produces: `QueryIndex.knows_conversation(channel: str | None, dm_user: str | None) -> bool` and `QueryIndex.knows_user(user: str) -> bool`, using the same matching rules as SEARCH (`USER_MATCH`, channel id or name, DM user by id or name).

- [ ] Tests: `test_unknown_channel_names_nothing` (`--in '#nope'` raises SlarcError "No conversation named #nope in the archive. See slarc list."), `test_unknown_dm_user_names_nothing` ("No DM with @x in the archive. See slarc list."), `test_unknown_from_user_names_nothing` and `test_unknown_mentions_user_names_nothing` ("No user named x in the archive."), `test_me_without_auth_user_names_nothing`, `test_known_channel_without_hits_is_no_match` (empty result, no error). CLI: `test_search_unknown_channel_exits_2` asserts exit 2 and the message on stderr.
- [ ] Run, watch them fail, implement, run `just test -k search`.
- [ ] Commit.

### Task 2: Real names

**Files:** `src/slarc/adapters/duckdb_index.py` (READABLE_COLUMNS, COLUMN_COMMENTS), `src/slarc/ports.py` (SearchHit), `src/slarc/domain/directory.py`, `src/slarc/service/export.py` (MessageView), `src/slarc/service/export_markdown.py`, tests.

**Interfaces:**
- Produces: column `author_real_name` after `author`; `SearchHit.author_real_name: str`; `DirectoryView.user_real_name(user_id) -> str` (profile real name, then `real_name`, then `user_display`); `MessageView.author_real_name: str`.

- [ ] Tests: the view returns the real name for a fixture user with one and falls back to `author` for a bot; markdown head shows `**Real** (display)` when they differ and only the name when equal.
- [ ] Implement, `just test -k "readable or markdown or search"`, commit.

### Task 3: Linked-message previews

**Files:** `src/slarc/service/export_markdown.py`, `tests/service/test_export_markdown.py`

- [ ] Tests: an unfurl with `message_blocks` renders `> **author** [linked message](url)` then the blocks' text with mentions resolved, once; an unfurl with only multi-line `text` quotes every line; inside a thread reply every line starts `> > `; a non-unfurl attachment whose fallback spans lines yields a one-line label.
- [ ] Implement: in `_Renderer.attachments`, branch on `is_msg_unfurl`; render `message_blocks[0].message.blocks` through `render_message_markdown({"blocks": ...}, view)`, else `mrkdwn_markdown(text)`; collapse whitespace in any label.
- [ ] `just test -k markdown`, commit.

### Task 4: Export file names and index.md

**Files:** `src/slarc/service/export.py`, `src/slarc/service/export_markdown.py`, tests in `tests/service/test_export.py`, `tests/service/test_export_markdown.py`, `tests/test_cli_export.py`, golden files if any.

**Interfaces:**
- Produces: `page_name(label: str) -> str`, the conversation label made safe for a file name (`#` dropped, `UNSAFE_FILENAME` runs replaced by `-`); pages get `{name}.html` / `{name}-thread-{ts}.html`, with `-{channel}` added when two pages would share a name. `index_markdown(pages, domains, zone, tz) -> str`.

- [ ] Tests: a channel page is `general.html` / `general.md`; a DM page `@jim.md`; a thread `general-thread-<ts>.md`; two workspaces' `#general` get distinct names with the channel id; the HTML index links match the files; a markdown directory export writes `index.md` with one line per page (file link, conversation, window, messages, threads).
- [ ] Implement, `just test -k export`, commit.

### Task 5: Excerpt

**Files:** `src/slarc/ports.py`, `src/slarc/adapters/duckdb_index.py`, `src/slarc/cli.py`, tests.

- [ ] Tests: `SearchHit.excerpt` replaces `snippet`; `search --format jsonl` rows have no `excerpt` key; the table still prints it.
- [ ] Implement (`_write_dataclasses` gains an `omit` parameter), `just test -k search`, commit.

### Task 6: Reactions and files columns

**Files:** `src/slarc/adapters/duckdb_index.py`, `src/slarc/ports.py` (SearchHit), tests.

**Interfaces:**
- Produces: `message_readable.reactions VARCHAR` (`:name: count` joined by `, `, null without reactions) and `message_readable.files STRUCT(id VARCHAR, name VARCHAR)[]` (null without files), both computed from `m.object`; `SearchHit.reactions: str | None`, `SearchHit.files: list[dict] | None`.

- [ ] Tests: fixture messages with reactions and files show them; jsonl renders `files` as a JSON list.
- [ ] Measure: time `slarc sql "SELECT count(*) FROM message_readable"` and a search on the largest test fixture before and after; record in the commit message.
- [ ] Commit.

### Task 7: Snippet text in markdown

**Files:** `src/slarc/service/export.py` (`file`, `snippet`, `SNIPPET_LIMIT`), `src/slarc/service/export_markdown.py`, tests.

**Interfaces:**
- Produces: `FileView.text: str` (plain snippet text, at most `SNIPPET_LIMIT` bytes), `FileView.cut: bool`, `FileView.language: str`, `FileView.file_id: str`.

- [ ] Tests: a downloaded snippet writes its file line then a fenced block tagged with its filetype; a snippet over the limit ends with `[cut at 200 KB; the rest: slarc file F...]`; a preview-only snippet shows the preview and the note; text containing a ``` fence uses a longer fence.
- [ ] Implement, `just test -k "export"`, commit.

### Task 8: slarc file

**Files:** Create `src/slarc/service/files.py`, `tests/service/test_files.py`; modify `src/slarc/ports.py`, `src/slarc/adapters/duckdb_index.py`, `src/slarc/cli.py`, `tests/test_cli.py` or a new `tests/test_cli_file.py`.

**Interfaces:**
- Consumes: `BlobStore.read`, the `file` and `download` views.
- Produces: `QueryIndex.file_record(file_id: str, domain: str | None) -> FileRecord | None` with `team_id, id, name, blob, reason`; `service.files.file_bytes(index, blobs, selector: str) -> tuple[str, bytes]` (name, bytes); raises `SlarcError` for an unreadable selector or unknown file, `FileNotArchived(SlarcError)` with the reason when known but missing.

- [ ] Tests: by id and by permalink; stdout bytes unchanged; `--out DIR` writes under the file's name; `--out PATH` writes that path; unknown id exits 2; known but not downloaded exits 1 with the download reason on stderr.
- [ ] Implement, `just test -k file`, commit.

### Task 9: Search over file text

**Files:** `src/slarc/adapters/duckdb_index.py`, tests in `tests/service/test_search.py`.

- [ ] Tests: a word only in a downloaded snippet finds the message carrying it, with the excerpt from the file text; a `text/plain` hosted file counts too; a word past 200 KB does not match; a non-UTF-8 blob does not crash.
- [ ] Implement: when text is given, collect the blob paths of text files with blobs, read them with `read_text` (or Python if `read_text` cannot take a parameter list), keep the first `SNIPPET_LIMIT` characters, and OR the match into SEARCH through the message's files. Measure on the largest fixture.
- [ ] `just test -k search`, commit.

### Task 10: Guide, spec and docs

**Files:** `src/slarc/data/guide.md`, `tests/test_guide.py`, `src/slarc/cli.py` (SQL_EXAMPLES), `docs/spec.md`, `docs/architecture.md`, `README.md`, `GLOSSARY.md` (Snippet), `docs/backlog.md`.

- [ ] Guide: `ada` for `arun`; "new since a time" replaces "what the latest pull added" (also in SQL_EXAMPLES); a `slarc file` recipe; the file metadata query over `file` and `download`; `author_real_name`, `reactions`, `files`, `excerpt`; markdown snippet text; exit codes for `slarc file`.
- [ ] Spec and architecture describe the new command and behaviour; backlog drops the snippet question.
- [ ] `just check`, commit.

### Task 11: Final review and cleanup

- [ ] Whole-branch review by a fresh reviewer, fix pass, delete this plan, report to Ted. Ask before pushing.
