# Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A workspace, and optionally a tracked item, can have a retention. Pull and listen read nothing older than it, and expiry removes the messages past it with the files and images only they referenced.

**Architecture:** A pure `domain/retention.py` holds the values, the effective retention of an item and the cutoff. Workspace retention is a new `workspace_setting` table and item retention a new `tracked_item.retention` column, both written through `Commit` with revision checks. rm's deletion planner moves to a shared `service/deletion.py` with retention-aware coverage, and a new `service/expiry.py` uses it. Pull bounds its reads by the cutoff and runs expiry before downloads; listen filters events by it and runs expiry each hour.

**Tech Stack:** Python 3.15, DuckDB with DuckLake, msgspec, Typer, pytest.

**Spec:** `docs/spec.md`. The decisions below were settled with Ted on 2026-10-28 and amend it; Task 9 writes them into the spec. `GLOSSARY.md` already defines Retention and Expiry.

## Decisions

1. Retention is set per workspace and per tracked item, both stored in the archive, never in config. Without one, a workspace keeps everything.
2. Values use the age syntax `--since` takes, limited to days and weeks (`90d`, `12w`), at least one day, or `forever`. Hours are rejected.
3. `slarc pull <workspace> --retention 30d` sets the workspace's retention, and pulls it as usual. The workspace must already have a tracked item. `slarc pull <link> --retention 90d` sets that item's own retention; each later pull of it, bare or by workspace, keeps it. A value equal to the workspace's effective retention at that moment clears the item's own value, so it follows the workspace from then on. `--retention` on a bare `slarc pull` exits 2.
4. An item's effective retention is its own value, else its workspace's, else forever. A message covered by several items is kept while any of them keeps it.
5. A thread's age is that of its newest stored message; a message outside a thread ages from its own `ts`. Edits, reactions and re-reads never reset it. A thread expires whole: parent, replies, broadcast replies, every version and deleted markers.
6. A file or image expires when no kept message references it: its directory rows and download state go, and its blob at the next idle maintenance. Avatars, custom emoji, users, conversations, bots and usergroups never expire.
7. The cutoff (now minus the effective retention) bounds every read: history, backfill, `--full` and a wider `--since`. `since` and `until` still apply to thread parents; the cutoff applies to thread activity. Lengthening a retention reopens the backfill for the gap.
8. A first pull and a `--full` pull of a conversation with a retention also run the reply search from two days before the cutoff, and re-read each thread found, so active threads with older parents are kept.
9. A reply to an expired thread brings it back: pull's search re-reads the whole thread; listen stores the reply, then re-reads the thread after 10 quiet seconds, as it does for reactions. Listen drops edits, reactions and deletes for expired messages.
10. Expiry is its own commit, in every pull for the workspaces it pulls (before downloads drain) and in each hour of listen (before maintenance). No prompt, no new command. It says `Expired N messages, F files and I images from acme.` on stderr when it removed anything, and the run row counts `messages_expired`, `files_expired` and `images_expired`.
11. Read commands show whatever the archive holds; there is no read-time filter.
12. Expiry runs on Slack-archived conversations too. A workspace's retention stays after its last tracked item is removed. Exports already written are untouched. A tracked item whose messages have all expired stays tracked.
13. `slarc list` shows each item's effective retention and where it comes from (`item`, `workspace` or none), and each workspace's retention.

## Global Constraints

- `just check` passes before every commit; test first. Use `just test -k NAME`, never bare pytest.
- Domain and service never import duckdb, httpx, slack_sdk or keyring, and never touch the filesystem, environment or clock directly; time comes from the unit of work's `Clock`.
- Stdout carries only results; notes, warnings and the expiry line go to stderr. Exit codes: errors 2, interrupt 130.
- Use `GLOSSARY.md` terms: retention, expiry, expired, tracked item. Never "subscription", "purge" or "prune".
- New `.py` files start with two `ABOUTME:` lines.
- No emoji or em dashes. Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- An existing archive, created before this change, opens and gains the `tracked_item.retention` column and the `workspace_setting` table without losing rows; a read-only command on it does not fail.
- `--full` on a conversation with a retention never infers deletes for messages older than the cutoff, which it did not re-read.
- A thread whose parent is older than the cutoff and whose newest reply is not: pull, listen, rm and expiry all keep it whole.
- A tracked thread item with `forever` inside a conversation item with `30d`: the thread survives expiry, the rest of the conversation does not.
- An rm, or a retention change, committed while a pull or listen runs makes the expiry commit conflict and retry, never expire with a stale retention.
- Expiry deletes a blob shared by an expired and a kept message only when no kept message references it.

---

### Task 1: Retention values and cutoffs

**Files:** create `src/slarc/domain/retention.py`, `tests/domain/test_retention.py`.

**Interfaces:**
- Produces: `type Retention = int | Literal["forever"]` (days); `parse_retention(text: str) -> Retention` raising `SlarcError` for hours, zero, or anything else; `retention_text(r: Retention) -> str` (`"30d"`, `"forever"`); `effective_retention(item: TrackedItem, workspace: Retention | None) -> Retention`; `retention_source(item, workspace) -> Literal["item", "workspace"] | None`; `cutoff(r: Retention, now: datetime) -> MessageTs | None`; `thread_activity(versions: dict[str, list[Envelope]]) -> dict[str, MessageTs]` mapping each stored ts to the newest ts of its thread (its own ts outside a thread).

- [ ] Tests: `parse_retention` accepts `90d`, `12w` (84), `forever`; rejects `12h`, `0d`, `90`, `ninety`. `cutoff` of 30 at a fixed now is 30 days earlier; of `forever` is None. `thread_activity` gives a parent with a newer reply the reply's ts, a lone message its own, a reply whose parent is not stored its own thread's newest.
- [ ] Watch them fail, implement, `just test -k retention`, commit.

### Task 2: Storing retention

**Files:** `src/slarc/domain/tracking.py` (`TrackedItem.retention: Retention | None = None`, `backfilled_from: str | None = None`), `src/slarc/ports.py` (`WorkspaceSetting`, `Commit.settings`, `ArchiveStore.workspace_settings()`), `src/slarc/adapters/lake.py` (schema), `src/slarc/adapters/lake_store.py`, `tests/fakes.py` (`InMemoryArchiveStore`), `tests/contract/test_archive_store.py`.

**Interfaces:**
- Produces: `WorkspaceSetting(team_id: str, retention: Retention | None, revision: int = 0)`; `ArchiveStore.workspace_settings() -> dict[str, WorkspaceSetting]`; `Commit.settings: Sequence[WorkspaceSetting]`, each checked against the revision it was read with (0 for a new row) and bumped, conflicting like an item. `Committed` gains the settings' new revisions. Table `workspace_setting (team VARCHAR, retention VARCHAR, revision INTEGER)`; column `tracked_item.retention VARCHAR` and `tracked_item.backfilled_from VARCHAR`, added with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for archives created before them. Retention is stored as its `retention_text`.

- [ ] Contract tests (both stores): a setting round-trips and its revision bumps; a stale revision conflicts and writes nothing; an item's retention and `backfilled_from` round-trip; `snapshot()` changes when only a setting changes. Adapter test: a lake created with the old schema opens and gains the column and table.
- [ ] Implement, `just test -k "archive_store or lake"`, commit.

### Task 3: Shared deletion planner with retention-aware coverage

**Files:** create `src/slarc/service/deletion.py` (moved from `service/remove.py`: `_Deletion`, `_TeamDeletion`, `_deletion`, `_team_deletion`, `_directory_images`, `_stage`), `src/slarc/service/selectors.py` (`covered_stamps`), `src/slarc/service/remove.py`, `src/slarc/service/downloads.py`, tests in `tests/service/test_remove.py`, `tests/service/test_selectors.py`.

**Interfaces:**
- Produces: `covered_stamps(versions, items, settings, now)` where a conversation item covers the stamps whose thread activity is after its cutoff, and a thread or message item the same within its own. `plan_deletion(teams, tracked, settings, now, archive, blobs) -> Deletion` and `stage(deletion, archive)`, used by rm and expiry.

- [ ] Tests: existing rm tests pass unchanged with no settings. New: with a 30d workspace retention, `covered_stamps` drops a 40-day-old lone message and keeps a 40-day-old parent with a 2-day-old reply; a `forever` thread item inside a 30d conversation keeps that thread only; rm of one item in a workspace with a retention also leaves out messages already past it.
- [ ] Move the code, add the parameters, `just test -k "remove or selectors or downloads"`, commit.

### Task 4: Expiry

**Files:** create `src/slarc/service/expiry.py`, `tests/service/test_expiry.py`.

**Interfaces:**
- Produces: `expire(uow: UnitOfWork, teams: list[str], blobs: BlobStore) -> Expiry` with counts `messages`, `files`, `images`. Each attempt reads the tracked items and settings, plans with `plan_deletion`, stages, and commits with every tracked item of those teams as `unchanged`, the teams as `complete_teams`, and their settings unchanged; it retries on `CommitConflict` up to `REMOVAL_ATTEMPTS`, then warns and leaves the rest for the next run. Adds the counts to the run row.

- [ ] Tests: a whole thread expires once its newest reply passes the cutoff, versions and deleted markers included; a file referenced only by expired messages loses its directory rows and download state, one also referenced by a kept message stays; avatars and custom emoji images stay; a Slack-archived conversation expires; no retention expires nothing; an rm committed between plan and commit (`InMemoryArchiveStore.elsewhere()`) makes it retry with fresh reads.
- [ ] Implement, `just test -k expiry`, commit.

### Task 5: Pull reads within retention

**Files:** `src/slarc/service/pull.py`, `tests/service/test_pull_conversation.py`, `tests/service/pull_harness.py`.

**Interfaces:**
- Consumes: `effective_retention`, `cutoff`.
- Changes: the backfill and `--full` page from the later of `since` and the cutoff; `infer_deletes` takes that same start; a finished backfill records its start in `backfilled_from`, and a pull whose start is earlier than `backfilled_from` reopens the backfill. When a retention is in effect, a pull with no `last_search_at`, and every `--full`, runs `discover_reply_threads` from two days before the cutoff and re-reads each thread found whose parent is inside `since` and `until`.

- [ ] Tests: a first pull with 30d reads no history before the cutoff; `--full` with 30d infers no deletes before the cutoff; lengthening 30d to 60d pages the gap on the next pull, while the daily move of the cutoff never reopens the backfill; a first pull finds a thread whose parent is 40 days old and whose reply is 2 days old, and stores the whole thread; a later search hit on an expired thread re-reads and stores it whole.
- [ ] Implement, `just test -k pull`, commit.

### Task 6: Setting retention from pull, and expiry in pull

**Files:** `src/slarc/cli.py` (`pull_command`, summary), `src/slarc/service/pull.py` (`PullOptions.retention`, `_ranged`, the run's order), tests in `tests/service/test_pull_*.py`, `tests/test_cli_pull.py`.

**Interfaces:**
- Produces: `--retention VALUE` on `slarc pull`. With links, each named item gets the value, or None when it equals the workspace's effective retention. With a workspace name, the commit writes its `WorkspaceSetting`. With neither, exit 2: `--retention applies to links and workspace names.` Pull runs `expire` for the workspaces it pulled after the items and before the downloads drain, and the summary adds the expiry line.

- [ ] Tests: `pull <link> --retention 90d` stores 90 and a bare pull keeps it; setting the workspace's value on an item stores None; `pull acme --retention 30d` writes the setting and leaves an item's own 90d; a workspace name with nothing tracked exits 2; a bare pull with `--retention` exits 2; a pull that expires prints the line on stderr and counts it in the run row; a download queued for an expired file is not attempted.
- [ ] Implement, `just test -k pull`, commit.

### Task 7: Listen

**Files:** `src/slarc/service/listen.py`, `src/slarc/domain/tracking.py` (`covers` gains the cutoff), `tests/service/test_listen.py`.

**Interfaces:**
- Changes: a new message or reply is kept when an item covers its thread by `since` and `until`, whatever its parent's age. A reply whose parent is not stored, or is past the cutoff, marks the thread for re-read after 10 quiet seconds. An edit, delete or reaction for a message whose stored thread activity is past the cutoff, or that is not stored, is dropped. Each hour runs `expire` for its workspace before maintenance, saying the expiry line. A snapshot change reloads the settings with the tracked items.

- [ ] Tests: a reply to a thread whose parent expired is stored and the thread re-read; a reaction to an expired message reads nothing; an hour with an expired message removes it; a retention set by another process applies at the next hour.
- [ ] Implement, `just test -k listen`, commit.

### Task 8: List

**Files:** `src/slarc/service/listing.py`, `src/slarc/cli.py`, tests in `tests/service/test_listing.py`, `tests/test_cli_list_search.py`.

- [ ] Tests: the table shows each item's retention (`30d`, `90d (own)`, `forever`) and a line per workspace with a retention; jsonl and csv add `retention` and `retention_from` (`item`, `workspace` or empty).
- [ ] Implement, `just test -k list`, commit.

### Task 9: Docs and acceptance

**Files:** `docs/spec.md`, `docs/architecture.md`, the guide source if it describes pull's coverage, `README.md` if it lists pull flags.

- [ ] Spec: `--retention` in the CLI table and `slarc list`'s columns; a Retention section under Pulling (decisions 3 to 10); expiry in the pull order and in Listening; write rule 4 says rm and expiry remove rows; `workspace_setting` and the new `tracked_item` columns in Tables and Data model.
- [ ] Architecture: `service/deletion.py` and `service/expiry.py`, and the schema columns added to old archives.
- [ ] `just check`, then `just acceptance` (pull's discovery and delete logic changed). Commit.
- [ ] Delete this plan in the final commit.
