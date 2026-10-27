# slarc listen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `slarc listen` keeps the archive current from Slack's pushed events instead of repeated pulls, filtered to tracked items.

**Architecture:** One `rtm.connect` websocket per workspace, read single-threaded by a service loop. Events about tracked items become message versions, deleted versions, re-reads or directory lines, committed in batches inside hour-long units of work that compact when they close. Pull, unchanged, does the catch-up at start, after every unplanned reconnect, and every 15 minutes for tracked items listen cannot hear.

**Tech Stack:** Python 3.15, slack_sdk WebClient (for `rtm.connect`), `websocket-client` (new dependency), DuckLake through the existing stores, Typer.

**Spec:** the Decisions below, settled with Ted on 2026-10-27; `docs/research/2026-10-27-slack-push-events.md`; `docs/slack-facts.md` (Real-time events). Task 8 writes them into `docs/spec.md`.

## Decisions

1. Connect only with the documented `rtm.connect` and the URL it returns, sending the `d` cookie on the handshake (ADR 0001). Never the desktop client's own socket URL, `client.*` methods, `tickle`, `users.setPresence` or any message but `ping`.
2. Nothing visible may change. Verified: manual away stays away; `connection_count` rises by one while connected.
3. Before the 8-hour limit, or on `goodbye`, open the new connection first and close the old one after the new one says `hello`. Duplicate events are harmless because stored versions are deduplicated by hash.
4. An unplanned drop (sleep, network) reconnects with backoff and runs a catch-up pull of that workspace, since RTM has no replay.
5. New messages, replies and edits are stored from the event payload, minus the envelope fields `channel`, `event_ts`, `suppress_notification`, `source_team`, `user_team`. Deletes become a deleted version. Reactions and anything else that names a message mark it stale; a stale message is re-read with `conversations.replies` once it has been quiet for 10 seconds, one read per thread.
6. `user_change` appends that user's line; `emoji_changed` re-lists emoji once per batch. Users, emoji and usergroups are otherwise listed by the catch-up pulls (at most daily, as pull already does).
7. Downloads run after each batch for that batch's new targets only. Earlier failures are retried by the catch-up pulls, as now.
8. `slarc listen [WORKSPACE...]` runs in the foreground until Ctrl-C, which flushes, saves and exits 0. Without arguments it listens to every workspace with a tracked item. It prints one line per event it acts on.
9. Tracked items listen cannot hear (a channel whose `conversations.info` says `is_member: false`, and threads and messages in one) are listed at start and re-pulled every `unheard_pull_minutes` (default 15, `[listen]` in `slarc.toml`).
10. Events for anything not tracked are dropped without being stored. A tracked conversation's window applies: a message at or after its `until` is dropped.

## Global Constraints

- slarc never calls a Slack method that changes anything; `rtm.connect` is the one method added to `READ_METHODS`, at Tier 1 (1 call per minute).
- Domain and service never import `slack_sdk`, `duckdb`, `httpx`, `keyring` or `websocket`, and never touch the filesystem, environment or clock except through ports.
- Never print, log, commit or put in fixtures a token, cookie, keychain secret or the URL `rtm.connect` returns.
- Live Slack calls only against `naleidworkspace` (team `T0CB5LYPVFA`); posting only through `scripts/seed-test-workspace.py`.
- `just check` passes before every commit. Test first. No emoji or em dashes. Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Use GLOSSARY.md terms: tracked item, pull, run. Never "subscription".

## Review Focus

1. The laptop sleeps for an hour: the socket is dead but no close arrives. Expected: no pong within 30 seconds marks it lost, listen reconnects and runs a catch-up pull (Task 5 test `test_missing_pong_counts_as_lost`).
2. `slarc rm` removes a tracked conversation while listen runs. Expected: no message of it is written afterwards; the batch commit conflicts and listen drops that item's changes (Task 4 test `test_batch_for_an_item_removed_meanwhile_writes_nothing`).
3. The desktop app signs out, so `rtm.connect` or a re-read fails with invalid auth. Expected: listen saves what it has and exits 2 naming the workspace (Task 6 test `test_rejected_credentials_stop_listen`).
4. Fifty reactions arrive on one thread in a few seconds. Expected: one `conversations.replies` call after it is quiet for 10 seconds (Task 4 test `test_many_reactions_on_a_thread_reread_it_once`).
5. Two listens, or a listen and a pull, at once. Expected: both run; commits are serialized by the write lock and duplicate versions are not written because hashes match (covered by the existing store contract; Task 6 test `test_events_already_stored_by_a_pull_add_nothing`).

---

### Task 1: Extract message writing from pull into `MessageWriter`

**Files:**
- Create: `src/slarc/service/ingest.py`
- Modify: `src/slarc/service/pull.py` (`_append_unknown`, `_append_messages`, `_append_thread`, `_append_deletes`, `_count_seen`, `_count_new` move out)
- Test: `tests/service/test_ingest.py`

**Interfaces:**
- Produces:
  ```
  class MessageWriter:
      def __init__(self, uow: UnitOfWork, max_file_bytes: int, *, retry_skipped: bool = False,
                   on_seen: Callable[[], None] = lambda: None,
                   on_new: Callable[[], None] = lambda: None) -> None
      def append_unknown(self, connection: Connection, messages: list[dict], view: DirectoryView) -> None
      def append_messages(self, held: HeldConversation, messages: list[dict], api: str, view: DirectoryView) -> None
      def append_thread(self, held: HeldConversation, thread_ts: str, messages: list[dict], view: DirectoryView) -> None
      def append_deletes(self, held: HeldConversation, stamps: set[str]) -> None
  ```
  `on_seen` runs once per message counted as seen (pull's progress `seen`), `on_new` once per line appended (pull's `messages_new` and progress `new`). The writer keeps the per-run set of users Slack did not return.

- [ ] **Step 1:** Write `tests/service/test_ingest.py::test_writer_appends_a_message_once_and_counts_it`: an `InMemoryArchiveStore`, a `UnitOfWork`, a `HeldConversation` for `CHANNEL`, `writer.append_messages(held, [message(TS)], "conversations.history", view)` twice; assert one `message` line with `meta.api == "conversations.history"` and `on_new` called once, `on_seen` once.
- [ ] **Step 2:** Run `just test tests/service/test_ingest.py`. Expected: ImportError.
- [ ] **Step 3:** Move the six methods into `MessageWriter` unchanged in behavior; `_PullRun` builds one writer per run with `on_seen`/`on_new` that update `self.messages_new` and `self.progress`.
- [ ] **Step 4:** Run `just check`. Expected: every existing pull test passes unchanged.
- [ ] **Step 5:** Commit "Extract message writing from pull into MessageWriter".

### Task 2: `rtm.connect` and the websocket adapter

**Files:**
- Modify: `pyproject.toml` (add `websocket-client`), `src/slarc/adapters/rate_limit.py`, `src/slarc/adapters/slack_gateway.py`, `src/slarc/ports.py`, `tests/fakes.py`
- Create: `src/slarc/adapters/rtm.py`
- Test: `tests/adapters/test_rtm.py`, `tests/adapters/test_slack_gateway.py`, `tests/adapters/test_rate_limit.py`

**Interfaces:**
- Produces in `ports.py`:
  ```
  class ConnectionLost(SlarcError): ...
  class EventConnection(Protocol):
      def receive(self, timeout: float) -> dict | None: ...  # next event; None on timeout; raises ConnectionLost
      def ping(self) -> None: ...                            # sends {"id": n, "type": "ping"}
      def close(self) -> None: ...
  class EventConnector(Protocol):
      def open(self, workspace: str) -> EventConnection: ...  # returns after the hello event; raises AuthFailed
  ```
  `SlackGateway.rtm_url() -> str` calls `rtm.connect` and returns its `url`. `TIER_RATES[1] = 1`, `METHOD_TIERS["rtm.connect"] = 1`.
  `adapters/rtm.py`: `RtmConnector(connector: SessionConnector, create: Callable[..., WebSocket] = websocket.create_connection)`. `FakeEventConnector` and `FakeEventConnection` in `tests/fakes.py` serve queued events and record pings and closes.

- [ ] **Step 1:** Tests: `test_rtm_connect_is_the_only_new_method_and_tier_1` (allowlist gains `rtm.connect`; `MinuteWindow` at rate 1 makes a second call wait 60 s); `test_open_sends_the_cookie_and_waits_for_hello` (a fake `create` receives `header=["Cookie: d=xoxd-1"]`, then frames `{"type":"hello"}`, `{"type":"message",...}`; `open` returns after hello and `receive` returns the message); `test_receive_timeout_returns_none`; `test_closed_socket_raises_connection_lost`; `test_connection_sends_nothing_but_pings` (after `ping()` twice, the fake saw exactly `{"id":1,"type":"ping"}` and `{"id":2,"type":"ping"}`); `test_the_rtm_url_is_never_in_an_error` (a failing `create` raises `ConnectionLost` whose message lacks the URL).
- [ ] **Step 2:** Run `just test tests/adapters`. Expected: failures for the missing names.
- [ ] **Step 3:** Implement. `receive` sets the socket timeout and maps `WebSocketTimeoutException` to `None` and `WebSocketConnectionClosedException` or `OSError` to `ConnectionLost`.
- [ ] **Step 4:** Run `just check`. Expected: PASS.
- [ ] **Step 5:** Commit "Open rtm.connect websockets that send only pings".

### Task 3: Classify events and match them to tracked items

**Files:**
- Create: `src/slarc/domain/events.py`, `tests/fixtures/rtm/events.jsonl` (the probe's events from `naleidworkspace`, unchanged; they hold no token or URL), `tests/fixtures/rtm/replies.json` (a read-only `conversations.replies` of the same thread)
- Modify: `src/slarc/domain/tracking.py`
- Test: `tests/domain/test_events.py`

**Interfaces:**
- Produces:
  ```
  @dataclass(frozen=True) class StoreMessage: channel: str; message: dict
  @dataclass(frozen=True) class DeleteMessage: channel: str; ts: str
  @dataclass(frozen=True) class Reread: channel: str; ts: str
  @dataclass(frozen=True) class UserChanged: user: dict
  @dataclass(frozen=True) class EmojiChanged: ...
  Change = StoreMessage | DeleteMessage | Reread | UserChanged | EmojiChanged
  ENVELOPE_KEYS = ("channel", "event_ts", "suppress_notification", "source_team", "user_team")
  def classify(event: dict) -> Change | None
  def covers(item: TrackedItem, channel: str, ts: str, thread_ts: str | None) -> bool
  ```
  `classify`: `message` without a hidden subtype gives `StoreMessage` with the envelope keys removed; `message_changed` gives `StoreMessage` of `event["message"]`; `message_deleted` gives `DeleteMessage(deleted_ts)`; `message_replied` gives `None`; `reaction_added`/`reaction_removed` give `Reread(item.channel, item.ts)`; `user_change` gives `UserChanged`; `emoji_changed` gives `EmojiChanged`; everything else `None`. `covers` (in `tracking.py`): a conversation item covers its channel within `since` < ts < `until`; a thread item covers its `ts` and replies whose `thread_ts` is its `ts`; a message item covers its `ts`.

- [ ] **Step 1:** Tests over the fixture: the new parent, the reply and both edits classify as `StoreMessage` whose message hashes (`content_hash("message", ...)`) the same as that message's `conversations.replies` copy in `tests/fixtures/rtm/replies.json`; the delete gives `DeleteMessage("C0CAL2LPBHA", "1793134571.481079")`; reactions give `Reread`; `hello`, `pong`, `channel_marked`, `thread_subscribed`, `badge_counts_updated`, `message_replied` give `None`. `covers` table test: conversation with `until`, thread item and its reply, message item, another channel.
- [ ] **Step 2:** Run `just test tests/domain/test_events.py`. Expected: ImportError.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `just check`. Expected: PASS.
- [ ] **Step 5:** Commit "Classify RTM events and match them to tracked items".

### Task 4: Apply a batch of changes

**Files:**
- Create: `src/slarc/service/listen.py` (the `apply_batch` half)
- Test: `tests/service/test_listen_batch.py`

**Interfaces:**
- Consumes: `MessageWriter` (Task 1), `Change` and `covers` (Task 3), `drain_downloads` gains `earlier: bool = True` (False skips `_requeue_earlier`).
- Produces:
  ```
  RTM_API = "rtm"
  QUIET_S = 10.0
  @dataclass class Pending:  # stale messages waiting to be re-read, per (team, channel, thread or ts)
      catch_up: set[str]  # workspaces whose next step is a catch-up pull
      def mark(self, team_id: str, channel: str, ts: str, at: datetime) -> None
      def due(self, now: datetime) -> list[tuple[str, str, str]]  # quiet for QUIET_S
  def apply_batch(uow: UnitOfWork, connection: Connection, changes: list[Change], pending: Pending,
                  downloader: Downloader, options: ListenOptions, reporter: Reporter) -> list[str]
  @dataclass(frozen=True) class ListenOptions: downloads: int; max_file_bytes: int; unheard_pull_minutes: int = 15
  ```
  `apply_batch` reads `uow.archive.tracked_items()` once, drops changes no item covers, stores and deletes through the writer with `api=RTM_API`, marks rereads in `pending` (a reply's thread is its `thread_ts` from `HeldConversation.stored`), re-reads what `pending.due` returns with `gateway.replies` and `writer.append_thread`, appends user lines with `api=RTM_API`, re-lists emoji once if any `EmojiChanged`, commits with `unchanged=` the covering items, then drains this batch's downloads. It returns one line per change acted on, e.g. `#general  new message from Ada Example`, `#general  edited message from Ada Example`, `#general  deleted message 1793134571.481079`, `#general  re-read thread 1793134571.213779`. A `CommitConflict` discards the batch, returns `#general  changed meanwhile; pulled next`, and adds the workspace to `pending.catch_up`.

- [ ] **Step 1:** Tests with the pull `Harness` store and `FakeSlackGateway`: `test_new_message_on_a_tracked_conversation_is_stored_from_the_event` (no `conversations.*` call; one `message` line with `api == "rtm"`); `test_event_for_an_untracked_channel_is_dropped`; `test_delete_appends_a_deleted_version`; `test_many_reactions_on_a_thread_reread_it_once` (50 `reaction_added` on one thread, advance 11 s, one `conversations.replies` call); `test_batch_for_an_item_removed_meanwhile_writes_nothing` (another commit marks the item removed before the batch commits; no new message lines; catch-up queued); `test_batch_downloads_only_its_own_targets` (an earlier failed download is not retried); `test_user_change_appends_the_user`.
- [ ] **Step 2:** Run `just test tests/service/test_listen_batch.py`. Expected: ImportError.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `just check`. Expected: PASS.
- [ ] **Step 5:** Commit "Apply batches of RTM changes to the archive".

### Task 5: Keep connections alive

**Files:**
- Create: `src/slarc/service/listen_connections.py`
- Test: `tests/service/test_listen_connections.py`

**Interfaces:**
- Consumes: `EventConnector`, `EventConnection`, `ConnectionLost` (Task 2), `Clock`.
- Produces:
  ```
  PING_S = 5.0; PONG_DEADLINE_S = 30.0; HANDOVER_AFTER = timedelta(hours=7, minutes=30)
  BACKOFF_S = (5, 10, 20, 40, 60)
  class Connections:
      def __init__(self, connector: EventConnector, clock: Clock, workspaces: list[str]) -> None
      def poll(self, timeout: float) -> list[tuple[str, dict]]  # (workspace, event)
      def take_lost(self) -> set[str]  # reconnected after an unplanned drop; each needs a catch-up pull
      def close(self) -> None
  ```
  `poll` opens missing connections whose backoff has passed, pings every `PING_S`, treats no `pong` within `PONG_DEADLINE_S` of a ping or `ConnectionLost` as lost, hands over on `goodbye` or at `HANDOVER_AFTER` by opening the new connection before closing the old one, and returns events other than `hello`, `pong` and `goodbye`. `AuthFailed` from `open` propagates.

- [ ] **Step 1:** Tests with `FakeEventConnector` and `FakeClock`: `test_pings_every_five_seconds`; `test_missing_pong_counts_as_lost` (ping, advance 31 s with no pong, workspace reconnects and appears once in `take_lost`); `test_goodbye_opens_the_new_connection_before_closing_the_old` (order of open and close recorded); `test_handover_after_seven_and_a_half_hours`; `test_reconnects_back_off` (opens fail with `ConnectionLost`; attempts at 5, 10, 20, 40, 60, 60 s); `test_handover_is_not_lost` (`take_lost` stays empty).
- [ ] **Step 2:** Run `just test tests/service/test_listen_connections.py`. Expected: ImportError.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `just check`. Expected: PASS.
- [ ] **Step 5:** Commit "Keep RTM connections alive with pings, handovers and backoff".

### Task 6: The listen loop and command

**Files:**
- Modify: `src/slarc/service/listen.py` (add `listen`), `src/slarc/cli.py`, `src/slarc/bootstrap.py`
- Test: `tests/service/test_listen.py`, `tests/test_cli_listen.py`

**Interfaces:**
- Consumes: Tasks 1 to 5, `pull`.
- Produces:
  ```
  FLUSH_QUIET_S = 2.0; FLUSH_MAX_S = 10.0; SEGMENT = timedelta(hours=1)
  def listen(workspaces: list[str], options: ListenOptions, open_uow: Callable[[str], UnitOfWork],
             connector: SlackConnector, events: EventConnector, downloader: Downloader,
             reporter: Reporter, clock: Clock, stop: Callable[[], bool]) -> None
  ```
  `open_uow(command)` returns a fresh `UnitOfWork` over the one open archive store. Order: a catch-up pull of the workspaces (`pull([WorkspaceSel(...)], PullOptions(...), ...)` in its own unit of work, command `listen`), then segments: each is a unit of work (command `listen`) that lasts `SEGMENT`, flushing a batch when no event came for `FLUSH_QUIET_S` or the oldest is `FLUSH_MAX_S` old, and ends early when a catch-up is due. `stop()` true flushes, closes the segment and returns. `bootstrap.Services` gains `event_connector`. CLI: `slarc listen [WORKSPACE...] [--downloads N] [--max-file-size MB] [--root DIR]`; prints `Listening to acme (3 tracked items; 1 not heard, pulled every 15 minutes).`, then each line `apply_batch` returns, prefixed with the local time `HH:MM`, then `Stopped.` on Ctrl-C with exit 0.

- [ ] **Step 1:** Service tests with fakes: `test_listen_pulls_first_then_stores_events`; `test_unplanned_reconnect_runs_a_catch_up_pull`; `test_rejected_credentials_stop_listen` (raises `AuthFailed` after committing the open batch); `test_events_already_stored_by_a_pull_add_nothing`; `test_segment_ends_after_an_hour_and_writes_a_run_row`. CLI tests: `test_cli_listen_prints_one_line_per_event_and_stops_cleanly` (fake events, `stop` after two batches; exit 0; stdout lines match the formats above); `test_cli_listen_with_nothing_tracked_exits_2`.
- [ ] **Step 2:** Run `just test -k listen`. Expected: failures.
- [ ] **Step 3:** Implement. Ctrl-C sets the stop flag through a `KeyboardInterrupt` handler around the loop.
- [ ] **Step 4:** Run `just check`. Expected: PASS.
- [ ] **Step 5:** Commit "Add slarc listen".

### Task 7: Tracked items listen cannot hear

**Files:**
- Modify: `src/slarc/config.py` (`unheard_pull_minutes`, `[listen]`, `NEW_ARCHIVE_HEADER`), `src/slarc/service/listen.py`, `src/slarc/cli.py` (`slarc list` settings line)
- Test: `tests/service/test_listen.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `def unheard_items(items: list[TrackedItem], conversations: dict[str, dict]) -> list[TrackedItem]` (in `listen.py`): items whose channel's stored `conversations.info` has `is_member` false; a DM or group DM is always heard. `Settings.unheard_pull_minutes: int = 15`.

- [ ] **Step 1:** Tests: `test_unheard_items_are_channels_not_joined_and_their_threads`; `test_unheard_items_are_pulled_every_fifteen_minutes` (advance 15 min: one pull with those items' links as selectors); `test_unheard_pull_minutes_reads_from_slarc_toml`.
- [ ] **Step 2:** Run `just test -k "unheard"`. Expected: failures.
- [ ] **Step 3:** Implement; the loop's catch-up pull of unheard items uses `message_link` selectors.
- [ ] **Step 4:** Run `just check`. Expected: PASS.
- [ ] **Step 5:** Commit "Pull tracked items listen cannot hear every 15 minutes".

### Task 8: Docs and an acceptance check

**Files:**
- Modify: `docs/spec.md` (command table row, a Listening section under Pulling from the Decisions), `docs/architecture.md`, `GLOSSARY.md` (**Listen**, **Catch-up pull**), `README.md`, `src/slarc/data/guide.md` (Start here: `slarc listen` runs until stopped; run it only when the user asks), `scripts/acceptance.py`
- Delete: this plan, once done.

- [ ] **Step 1:** Add an acceptance step: start `slarc listen naleidworkspace` in the background against a temp archive tracking `#slarc-acceptance`, run `seed-test-workspace.py events`, wait 20 s, stop it, and check the archive holds the parent with its edit, the deleted reply, and that `users.getPresence` stayed as before.
- [ ] **Step 2:** Run `just check`. Expected: PASS. Then ask Ted before running `just acceptance`.
- [ ] **Step 3:** Commit "Document slarc listen and check it against the test workspace".
