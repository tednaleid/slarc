# Agent Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Mark a task's heading `[done]` and tick its steps in the same commit that finishes it.

**Goal:** Make slarc's read side (`search`, `sql`, `list`, `export`) usable by agents without wrapper scripts or hand-written joins, and add `slarc guide` to teach them how.

**Architecture:** As today: domain (pure parsing and rendering), service (use cases over ports), adapters (DuckDB views), CLI (Typer). The new pieces are:
- a window parser in `domain/window.py`;
- a markdown renderer in `domain/render/markdown.py`;
- an output formatter in `src/slarc/formats.py`, used only by the CLI;
- a `message_readable` view;
- a packaged `data/guide.md`.

**Tech Stack:** Python 3.15, Typer, Rich, DuckDB 2.0, Jinja2, pytest.

**Spec:** `docs/spec.md`, `docs/architecture.md`, `GLOSSARY.md`. The decisions below were settled with Ted on 2026-10-26 and bind this plan. Each task updates the spec text its behavior changes.

## Decisions

1. Agents are a supported user of the read side. `pull` and `rm` stay human-directed.
2. `--format table|jsonl|csv` on `sql`, `search` and `list`. `table` is the default, with no TTY switch. Non-table formats never truncate.
3. `search --limit N`, default 50, with `0` meaning no limit. When results are cut, stderr says `Showing 50; --limit for more.`
4. One window grammar for `--since` and `--until` on `pull`, `search` and `export`:
   - absolute values: `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM`, `YYYY-MM-DDTHH:MM:SS`, and either with a `Z` or `±HH:MM` offset;
   - relative values: `Nh`, `Nd` and `Nw`, counted back from now;
   - without an offset, a value is read in the machine's local time zone;
   - a date-only `--until` runs through the end of that day.

   `pull` switches from UTC dates to this grammar.
5. No `--since-last-pull`. The guide shows the `first_seen_at` query instead.
6. Markdown is an export format: `export --format html|markdown`. There is no transcript command.
7. Cheap columns go on `message`: `posted_at`, `edited_at`, `thread_root_ts` and `is_reply`. Joined columns go in a new view, `message_readable`:
   - `team_id`, `workspace`, `channel_id`, `conversation`, `ts`, `thread_ts`;
   - `posted_at`, `author`, `text`, `link`, `deleted_at`, `first_seen_at`.
8. `search --mentions USER` matches `<@U…>` and the usergroups that hold the user, but not `@here` or `@channel`.
   - USER is a name, an id or `me`; `me` also works in `--from`.
   - `me` is the user of the workspace's latest recorded auth.
   - QUERY is optional when `--in`, `--from`, `--mentions`, `--since` or `--until` is given.
9. Views and their non-obvious columns carry `COMMENT ON` text, so `duckdb_columns()` shows it. `sql --help` carries examples.
10. Exit codes: errors exit 2 in every command. `search` exits 1 when nothing matches. `sql` exits 0 on an empty result. An interrupt stays at 130.
11. Stdout carries only a command's result. Notes, hints, warnings, "nothing matched" and errors go to stderr.
12. Context around a message: `export LINK --channel` exports the conversation the link is in, and is combined with `--since` and `--until`. In markdown the linked message is marked `(linked)`. There is no `--context`.
13. "A conversation on Tuesday with Arun": `search --in @arun` works (a backlog bug today). Search JSONL carries `thread_ts` and `posted_at`. The window grammar has no weekday names.
14. Markdown export:
    - **Layout:** one `.md` per page in a directory; `--single-file` writes one `.md`. `--out -` writes the page to stdout, for either format.
    - **Assets:** none are copied. A file shows as `[file: NAME, SIZE](permalink)`, and reactions as one line such as `:+1: 3  :eyes: 1`.
    - **Header:** each page starts with a header giving the conversation, workspace, window, and message and thread counts.
    - **System messages:** one line per group.
    - **Body:** rendered from `rich_text` blocks as markdown, falling back to mrkdwn converted to markdown.
15. A window on export:
    - a thread whose parent is older than the window shows the parent marked `(context)` and only the replies inside the window;
    - a workspace export leaves out conversations with no messages in the window.
16. The view is named `message_readable`.
17. `plain` keeps its `Author: ` prefix. `message_readable.text` strips everything through the first `": "`.
18. The `slarc guide` command prints the packaged `data/guide.md`. Top-level `--help` points to it.
19. Nothing is added to Ted's global CLAUDE.md, and there is no skill for now.
20. A test runs every `slarc` command in the guide's `sh` blocks against the recorded fixture archive.
21. The guide says `search`, `sql`, `list`, `export` and `guide` are free to use. `pull` runs only when the user asks for fresh data. `rm` runs only when the user has explicitly asked to remove that item: the agent asks first, and never passes `--yes` on its own. The guide does not mention `--take-ownership`.
22. The guide is workflow-first and under about 150 lines.

**My calls (not discussed):**
- `list --format jsonl|csv` emits tracked items only. Downloads stay in the table and in the `download` view.
- The window's start is exclusive, `ts > since`, as tracked items are today.
- Search JSONL keys match `message_readable`'s column names.

## Global Constraints

- **Code style:**
  - Every code file starts with two `# ABOUTME: ` lines.
  - `just check` passes before every commit.
  - Use `just` recipes, never bare tools.
  - Test first: watch each new test fail before making it pass.
- **Layering:**
  - Domain and service never import `slack_sdk`, `duckdb`, `httpx` or `keyring`, and never touch the filesystem or environment.
  - Writing to stdout goes through the CLI or an `ExportWriter`.
- **Slack:** no task calls Slack. `just acceptance` is not needed: discovery and delete logic are untouched.
- **Privacy:**
  - Never read `archives/`.
  - Use only fixture names (`naleidworkspace`, `#all-naleid`, `acme`) in code, tests and docs.
  - No real workspace or person names anywhere.
- **Text:**
  - No emoji or em dashes.
  - Messages are one or two plain sentences, in glossary terms.
  - Comments are evergreen.
- **Docs:** the spec, `docs/architecture.md` and `README.md` change in the same commit as the behavior they describe.

## Review Focus

1. **Bounds:** a date-only `--until` includes that whole local day, and `--since` with an offset ignores the local zone. Test: Task 3, `test_window_bounds`.
2. **Context parent:** a windowed thread whose parent is outside the window shows the parent once, marked as context, and it is not counted in the header's message count. Test: Task 8, `test_markdown_window_marks_an_older_parent_as_context`.
3. **Stdout stays clean:** `--out -` puts nothing but the page on stdout. Test: Task 8, `test_cli_export_to_stdout_keeps_notes_on_stderr`.
4. **`me` across workspaces:** `--mentions me` in a two-workspace archive matches each workspace's own user. Test: Task 5, `test_mentions_me_resolves_per_workspace`.
5. **Prefix stripping:** a display name containing `": "` strips only through the first `": "`, the documented limit. Test: Task 4, `test_readable_text_strips_the_author_prefix`.

---

### Task 1 [done]: Stdout, stderr and exit codes

**Files:** modify `src/slarc/cli.py` and `docs/spec.md`. Tests: `tests/test_cli.py`, `tests/test_cli_list_search.py`, `tests/test_cli_export.py` and the existing exit-code assertions.

- [x] **Step 1.** Write failing tests:
  - `test_errors_exit_2`: `slarc sql "SELEC"` exits 2, and its message is on stderr.
  - `test_search_without_matches_exits_1_and_says_so_on_stderr`.
  - `test_list_without_tracked_items_says_so_on_stderr`.
  - `test_export_missing_files_note_goes_to_stderr`.
- [x] **Step 2.** Run them and watch them fail.
- [x] **Step 3.** Make the changes:
  - `_fail` and the other `typer.Exit(1)` sites exit 2;
  - `NO_MATCHES`, `NO_TRACKED_ITEMS` and the missing-files note print through `err`;
  - search exits 1 after `NO_MATCHES`.
  - Then update every existing test that asserts exit code 1.
- [x] **Step 4.** Spec: add the stdout rule and the exit-code table under Commands.
- [x] **Step 5.** Run `just check`, then commit: "Keep stdout for results and exit 2 on errors, 1 on no matches".

### Task 2 [done]: Output formats and `sql --format`

**Files:** create `src/slarc/formats.py`; modify `src/slarc/cli.py`. Test: create `tests/test_formats.py`, and extend `tests/test_cli.py`.

**Produces:**
- `OutputFormat = Literal["table", "jsonl", "csv"]`.
- `write_rows(stream: TextIO, fmt: OutputFormat, columns: list[str], rows: Iterable[Sequence[object]]) -> None`. It handles `jsonl` and `csv` only.
  - JSONL writes one object per row, keyed by column. Values are converted as follows: datetimes become ISO 8601 with their offset, `Decimal` and UUIDs become strings, lists and dicts stay as they are, and bytes become base64.
  - CSV writes a header row, then the rows, with `None` as an empty cell.

- [x] **Step 1.** Write failing tests:
  - `test_jsonl_writes_one_object_per_row_with_iso_times`;
  - `test_csv_writes_header_and_empty_cells_for_null`;
  - `test_cli_sql_format_jsonl_is_parseable_and_untruncated`, which takes a 500-character text value from `sql "SELECT repeat('x', 500) AS t"`.
- [x] **Step 2.** Run them and watch them fail.
- [x] **Step 3.** Implement. `sql` gets `--format` (`typer.Option`, a `click.Choice` of the three values). The table path is unchanged.
- [x] **Step 4.** Spec: update the `sql` row of the command table.
- [x] **Step 5.** Run `just check`, then commit: "Add --format jsonl and csv to slarc sql".

### Task 3 [done]: Window grammar

**Files:** create `src/slarc/domain/window.py`; modify `src/slarc/cli.py` (`pull_command` and `_check_date`), `src/slarc/service/pull.py` (`date_to_ts` and `PullOptions`), `docs/spec.md` and `GLOSSARY.md` (already has Window). Test: create `tests/domain/test_window.py`; update `tests/test_cli_pull.py`.

**Produces:**
- `parse_bound(text: str, now: datetime, zone: tzinfo, end: bool) -> datetime`. It returns an aware datetime and raises `ValueError` with a message that names the accepted forms. With `end=True`, a date-only value means the start of the next day.
- `Window(since: MessageTs | None, until: MessageTs | None)`, a frozen dataclass whose bounds are ts strings like tracked items' bounds.
  - `Window.parse(since: str | None, until: str | None, now: datetime, zone: tzinfo) -> Window` raises `ValueError` when `since` is not before `until`.
  - `Window.contains(ts: str) -> bool`.

- [x] **Step 1.** Write failing tests:
  - `test_window_bounds`: a parametrized table covering a date, a date as the end bound, a minute, a second, `Z`, `+02:00`, `12h`, `7d`, `2w`, and garbage, using `zone=ZoneInfo("America/Chicago")` and a fixed `now`.
  - `test_window_rejects_since_after_until`.
  - `test_cli_pull_since_reads_local_time`: the stored item's `since` equals the local-midnight ts. Run the CLI with `TZ` set through the runner's env.
- [x] **Step 2.** Run them and watch them fail.
- [x] **Step 3.** Make the changes:
  - the CLI builds the `Window` with `datetime.now().astimezone().tzinfo` and a `Clock`-free `now`, and passes the ts strings into `PullOptions`;
  - `PullOptions.since` and `until` hold ts strings;
  - `date_to_ts` is deleted;
  - the error text is `--since must be a date, a time or an age such as 2026-10-19, 2026-10-19T14:30 or 7d.`
- [x] **Step 4.** Spec: change pull's "YYYY-MM-DD in UTC" to the window grammar. Replace "date range" with "window" throughout.
- [x] **Step 5.** Run `just check`, then commit: "Read --since and --until as local times, dates or ages".

### Task 4 [done]: Friendlier message columns and `message_readable`

**Files:** modify `src/slarc/adapters/duckdb_index.py` (`ENTITY_VIEWS` and the `workspace` view), `docs/spec.md` (Data model table) and `docs/architecture.md`. Test: `tests/contract/test_duckdb_index.py`.

**Produces:**
- `message` gains:
  - `posted_at TIMESTAMPTZ` (`to_timestamp(CAST(ts AS DOUBLE))`);
  - `edited_at TIMESTAMPTZ`;
  - `thread_root_ts` (`coalesce(thread_ts, ts)`, using the computed `thread_ts`);
  - `is_reply` (`thread_ts IS NOT NULL AND thread_ts <> ts`).
- `workspace` gains `user_id`, the latest auth's user.
- `message_readable` is a view over `message`, `user`, `bot`, `conversation` and `workspace`. Its columns, in order: `team_id`, `workspace` (domain, else team id), `channel_id`, `conversation` (`#name`, `@user` or the group DM's name), `ts`, `thread_ts`, `posted_at`, `author`, `text`, `link`, `deleted_at`, `first_seen_at`.
  - `author` uses the same rules as `SEARCH`.
  - `text` is `plain` with everything through the first `": "` removed.
  - `link` is the permalink `message_link` builds, built in SQL.

- [x] **Step 1.** Write failing tests:
  - `test_message_has_time_columns`;
  - `test_readable_text_strips_the_author_prefix`, including a display name containing `": "`, which shows the documented limit;
  - `test_readable_link_matches_message_link`, which compares every recorded message's `link` with `domain.links.message_link`;
  - `test_workspace_names_the_auth_user`.
- [x] **Step 2.** Run them and watch them fail.
- [x] **Step 3.** Implement. Move `SEARCH`'s author and conversation expressions into `message_readable`, so that `SEARCH` can select from it in Task 5.
- [x] **Step 4.** Update the Data model table.
- [x] **Step 5.** Run `just check`, then commit: "Add time columns to message and a message_readable view".

### Task 5 [done]: Search filters, formats and limit

**Files:** modify `src/slarc/service/search.py`, `src/slarc/adapters/duckdb_index.py` (`SEARCH` and `DuckDbQueryIndex.search`), `src/slarc/ports.py` (`SearchHit`, `QueryIndex.search` and a new `SearchFilter`), `src/slarc/cli.py`, `docs/spec.md` and `docs/backlog.md` (delete the `--in @user` item). Tests: `tests/service/test_search.py` and `tests/test_cli_list_search.py`.

**Produces:**
- `SearchFilter`, a frozen dataclass:
  - `text: str | None`;
  - `in_channel: str | None` (an id or name);
  - `in_dm_user: str | None`;
  - `from_user: str | None`;
  - `mentions: str | None`;
  - `window: Window`;
  - `limit: int` (0 for none).
- `QueryIndex.search(filter: SearchFilter) -> list[SearchHit]`.
- `SearchHit` gains `team_id`, `workspace`, `channel_id`, `thread_ts`, `posted_at: datetime`, `text`, and keeps `snippet`. Its JSONL keys are `message_readable`'s columns plus `snippet`.
- `search(index, text, in_conv, from_user, mentions, window, limit)` returns `(hits, more: bool)`. It queries `limit + 1` rows to learn `more`.

- [x] **Step 1.** Write failing tests:
  - `test_search_without_text_lists_the_window`;
  - `test_search_since_until_bounds_posting_time`;
  - `test_search_in_dm_user`;
  - `test_search_mentions_user_and_their_usergroups`;
  - `test_mentions_me_resolves_per_workspace`: append a second workspace's lines with the `commit` helper the contract tests use;
  - `test_search_limit_reports_more`;
  - `test_search_without_any_filter_or_text_is_an_error`;
  - `test_cli_search_format_jsonl_keys`;
  - `test_cli_search_limit_hint_on_stderr`.
- [x] **Step 2.** Run them and watch them fail.
- [x] **Step 3.** Implement:
  - `SEARCH` selects from `message_readable`;
  - `me` maps to `workspace.user_id` inside the SQL, per team;
  - mentions match `text_mrkdwn` for `<@ID>`, `<@ID|`, and `<!subteam^GID` for groups whose `user_ids` hold the user;
  - the CLI gets `--since`, `--until`, `--mentions`, `--limit` and `--format`;
  - QUERY becomes optional.
- [x] **Step 4.** Spec: update the `search` row and its paragraph.
- [x] **Step 5.** Run `just check`, then commit: "Search by window, mentions and DM, with --limit and --format".

### Task 6 [done]: `list --format`

**Files:** modify `src/slarc/cli.py` and `docs/spec.md`. Test: `tests/test_cli_list_search.py`.

- [x] **Step 1.** Write a failing test, `test_cli_list_format_jsonl_emits_one_item_per_line`. Its keys are `ListRow`'s fields, with `last_pull` as an ISO time.
- [x] **Step 2.** Run it and watch it fail.
- [x] **Step 3.** Implement using `write_rows`. In non-table formats, the Archive, Config and Settings lines and the downloads table and footer are not printed.
- [x] **Step 4.** Run `just check`, then commit: "Add --format to slarc list".

### Task 7 [done]: Markdown message renderer

**Files:** create `src/slarc/domain/render/markdown.py`. Test: create `tests/domain/test_render_markdown.py`.

**Produces:** `render_message_markdown(message: dict, view: DirectoryView) -> str`. It has the same block coverage as `render_message_html`:
- **rich_text:**
  - bold `**`, italic `_`, strike `~~` and inline code;
  - links as `[label](url)`, or a bare URL;
  - mentions resolved by `mention_text`, and emoji as `:name:`;
  - lists with nesting, as `- ` or `1. `;
  - quotes as `> `, and preformatted text as fenced blocks.
- **Sections and headers:** their text.
- **Context:** text elements and image alt text.
- **Images:** `[image: alt](url)`.
- **Dividers:** `---`.
- **Without blocks:** mrkdwn converted (`*b*` to `**b**`, `~s~` to `~~s~~`, `<url|label>` to `[label](url)`, mentions resolved, entities unescaped).
- **Unrendered subtypes:** their text, or the subtype name.

- [x] **Step 1.** Write failing tests for each case above. Take the inputs from `tests/fixtures/workspace` messages where they exist (bold, list, code block, giphy, snippet), as the HTML renderer's tests do.
- [x] **Step 2.** Run them and watch them fail.
- [x] **Step 3.** Implement. Share list nesting with `blocks.py` by moving `_List`, `_Item` and the grouping walk into a helper both renderers call, so the nesting logic exists once.
- [x] **Step 4.** Run `just check`, then commit: "Render message bodies as markdown".

### Task 8 [done]: Export markdown, windows, `--channel` and stdout

**Files:**
- Modify: `src/slarc/service/export.py`, `src/slarc/cli.py`, `src/slarc/bootstrap.py` (a stdout `ExportWriter`), `src/slarc/ports.py` if `ExportWriter` needs it, `docs/spec.md` and `README.md`.
- Create: `src/slarc/export/templates/conversation.md.j2` (or a Python renderer, whichever reads more simply).
- Tests: `tests/service/test_export.py` and `tests/test_cli_export.py`.

**Produces:**
- `export(...)` gains `fmt: Literal["html", "markdown"]`, `window: Window` and `whole_conversation: bool`.
- `MessageView` gains `source: dict` (the message object), `context: bool` and `linked: bool`.
- In markdown, `_Workspace` skips the HTML-only fields (`body`, `attachments`, `reactions`), so no asset is resolved.
- `Page` gains `window: Window`, and `Page.count` counts only the messages inside the window.

**Markdown page shape** (exact strings):

```
# #all-naleid (naleidworkspace)
Window: 2026-10-09 00:00 to now, America/Chicago. 12 messages, 2 threads.

## Monday, October 20, 2026

**Ted** 2:03 PM (linked) https://naleidworkspace.slack.com/archives/C0CA5396G03/p1792410693663869
Body text
[file: report.pdf, 1.2 MB](https://...)
:+1: 3  :eyes: 1

> **Arun** 2:05 PM https://...
> Reply text
```

- A thread's replies follow the parent as `> ` blockquotes, and fenced code inside a reply keeps its `> ` prefix.
- A system group is one italic line of its messages' texts.
- `(edited)`, `(deleted TIME)` and `(context)` follow the time.
- A window with no start reads `the start`, and one with no end reads `now`.

**Steps:**
- [x] **Step 1.** Write failing tests:
  - `test_markdown_page_has_header_threads_files_and_reactions`;
  - `test_markdown_window_marks_an_older_parent_as_context`;
  - `test_window_drops_workspace_conversations_without_messages_in_it`;
  - `test_channel_flag_exports_the_conversation_and_marks_the_link`;
  - `test_html_export_honors_the_window`;
  - `test_cli_export_to_stdout_keeps_notes_on_stderr`, where stdout equals the rendered page exactly;
  - `test_cli_export_markdown_directory_has_one_md_per_page`.
- [x] **Step 2.** Run them and watch them fail.
- [x] **Step 3.** Implement:
  - `--out -` makes the export one file written by the stdout writer, and the CLI prints the "Exported" lines to stderr;
  - the default directory name stays `slarc-export-<name>`, with `.md` files;
  - `--channel` with a `MessageSel` becomes a `ConversationSel` of its channel, keeping the linked ts;
  - context parents come from `index.thread` and are added once, before their first windowed reply.
- [x] **Step 4.** Spec and README: update the export row and section, with an example of `--format markdown --out -`.
- [x] **Step 5.** Run `just check`, then commit: "Export markdown, honor windows, and export a link's whole conversation".

### Task 9: Schema comments and `sql --help` examples

**Files:** modify `src/slarc/adapters/duckdb_index.py` (a `VIEW_COMMENTS` dict that the index applies after creating the views) and `src/slarc/cli.py` (the `sql` docstring as help, with Typer's `rich_markup_mode` off for that text, or examples in an epilog). Tests: `tests/contract/test_duckdb_index.py` and `tests/test_cli.py`.

- [ ] **Step 1.** Write failing tests:
  - `test_every_view_has_a_comment`, using `duckdb_views()` where `internal = false`;
  - `test_readable_columns_have_comments`;
  - `test_sql_help_shows_examples`.
- [ ] **Step 2.** Run them and watch them fail.
- [ ] **Step 3.** Implement. Comments are one line each, in glossary terms. Measure `slarc sql "SELECT 1"` with and without them. If the comments add more than 10 ms, apply them in one `execute` with the view DDL.

  The help examples:
  - list the views: `SELECT view_name, comment FROM duckdb_views() WHERE NOT internal`;
  - describe one: `SELECT column_name, data_type, comment FROM duckdb_columns() WHERE table_name = 'message_readable'`;
  - a conversation since a date;
  - mentions of me;
  - what the latest pull added: `first_seen_at >= (SELECT max(started) FROM run WHERE command = 'pull')`, with the cast the column needs.
- [ ] **Step 4.** Run `just check`, then commit: "Describe the views in DuckDB comments and show sql examples".

### Task 10: `slarc guide`

**Files:**
- Create: `src/slarc/data/guide.md`.
- Modify: `src/slarc/cli.py` (the `guide` command, plus a top-level help line: `Agents and scripts: run slarc guide for workflows, output formats and exit codes.`), `docs/spec.md` (Goals gains agent use; the command table gains `guide`) and `README.md`.
- Test: create `tests/test_guide.py`.

- [ ] **Step 1.** Write failing tests:
  - `test_guide_prints_the_packaged_guide`;
  - `test_help_points_to_the_guide`;
  - `test_guide_examples_run_against_the_recorded_archive`. It extracts each line starting with `slarc ` from the guide's sh blocks, except `pull` and `rm`, and runs it through `CliRunner` with `--root` set to a `recorded_lake`. It asserts exit 0 or 1 and valid JSON on every stdout line when `--format jsonl` is present. Guide examples use fixture names (`#all-naleid`) and dates inside the fixture data.
- [ ] **Step 2.** Run them and watch them fail.
- [ ] **Step 3.** Write the guide, in this order:
  - **What slarc is:** two lines.
  - **What an agent may run** (Decision 21, with `rm` in bold).
  - **Recipes**, each a sh block:
    - catch up on conversations since a date;
    - what mentions me;
    - a conversation with a person on a day;
    - what surrounds a link;
    - what the latest pull added;
    - read a whole thread as markdown.
  - **Output formats and exit codes.**
  - **Reading results:** links, `ts` and `posted_at`, `thread_ts`.
  - **The views worth knowing,** with `message_readable` first, and how to list the rest.
- [ ] **Step 4.** Run `just check`, then commit: "Add slarc guide for agents".

### Task 11: Finish

- [ ] Delete this plan. Check `docs/backlog.md` for items this work resolved, and the spec's Status section for the new commands. Run `just check`, then commit: "Remove the agent interface plan".
- [ ] Ask Ted before pushing.
