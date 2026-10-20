# Phase 1 Leftovers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Mark a task's heading `[done]` and tick its steps in the same commit that finishes it, and delete the matching lines from `docs/plans/backlog.md` in that commit.

**Goal:** Fix the Phase 1 leftovers that hurt daily use or would make Phase 2 harder: secrets in debug logs, one failed read stopping a whole pull, interrupted first pulls losing items, confusing bare-pull output, slow Ctrl-C during downloads, and an 800-line `pull.py`.

**Architecture:** No new layers. Changes stay inside `adapters/slack_gateway.py`, `adapters/downloader.py`, `service/pull.py`, `domain/tracking.py` and `cli.py`; the last task splits `service/pull.py` into three modules without changing behavior.

**Tech Stack:** As Phase 1: Python 3.14, uv, Typer, slack_sdk, httpx, msgspec, pytest.

**Spec:** `docs/specs/detailed-requirements.md`. Also `docs/architecture.md`, `GLOSSARY.md`, and the Phase 1 leftovers in `docs/plans/backlog.md` that each task names.

## Global Constraints

- Every code file starts with two `# ABOUTME: ` lines saying what it does.
- `just check` passes before every commit. Use `just` recipes, never bare tools.
- Domain and service modules never import `slack_sdk`, `httpx`, `duckdb` or `keyring`, and never touch the filesystem or environment.
- slarc never calls a Slack method that changes anything. No task here makes live Slack calls; all tests use the fakes.
- Never print, log, write to a fixture, or include in an exception message any token, cookie or keychain secret.
- `ts` values are strings everywhere; never floats.
- User-facing errors and warnings are one or two plain sentences with no stack trace. Use glossary terms.
- No emoji or em dashes in code, comments, output or docs. Comments are evergreen.
- When a task changes behavior the spec or `docs/architecture.md` describes, update that text in the same commit.

## Review Focus

1. A thread read that fails with a network error during a first backfill of a big channel: the pull finishes the channel, warns once for that thread, and the next pull reads the thread. Test: Task 2 `test_failed_thread_read_warns_and_is_retried_next_pull`.
2. Slack rejecting the credentials partway through a pull (`AuthFailed`) must still stop that workspace, not be swallowed as a per-thread warning. Test: Task 2 `test_auth_failure_on_a_thread_read_still_skips_the_workspace`.
3. A pull of three new links interrupted during the first: all three stay tracked and a bare pull picks them up. Test: Task 3 `test_interrupted_pull_keeps_every_new_item`.
4. Running `slarc` with `logging` set to DEBUG by some host (a test runner, an embedding script): no request log line carries the `d` cookie. Test: Task 1 `test_debug_logging_never_shows_the_session_cookie`.
5. Ctrl-C while a large file is downloading: the command returns within about a second and leaves no temp file in the blob store. Test: Task 5 `test_closing_fetch_stops_an_in_flight_download`.

---

### Task 1: Keep the session cookie out of slack_sdk's debug log [done]

Backlog line: "slack_sdk's DEBUG request log redacts `Authorization` but not the `Cookie` header".

slack_sdk's `BaseClient` builds its "Sending a request" log line whenever its own logger's `level <= DEBUG`, and an unconfigured logger has level 0. The line prints every header except `Authorization`, so the `d` cookie appears whenever the root logger is at DEBUG.

**Files:**
- Modify: `src/slarc/adapters/slack_gateway.py` (`web_api`)
- Test: `tests/adapters/test_slack_gateway.py`

**Interfaces:**
- Produces: `SDK_LOGGER = "slarc.slack_sdk"` in `slack_gateway.py`; `web_api` passes `logger=logging.getLogger(SDK_LOGGER)` to `WebClient` after setting that logger's level to `logging.INFO`.

- [x] **Step 1: Write the failing test**

Serve one JSON response from a local `http.server.HTTPServer` on `127.0.0.1` port 0 in a daemon thread, and point the client at it.

```python
def test_debug_logging_never_shows_the_session_cookie(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    with local_slack_api({"ok": True, "team_id": "T0CB5LYPVFA"}) as base_url:
        api = web_api(
            Session("T0CB5LYPVFA", "naleidworkspace", "naleid", "desktop", "xoxc-test", "xoxd-test")
        )
        api.client.base_url = base_url
        assert api.call("auth.test", {})["ok"] is True
    assert "xoxd-test" not in caplog.text
    assert "xoxc-test" not in caplog.text
```

`local_slack_api` is a context manager in the test module that starts the server and yields `f"http://127.0.0.1:{port}/api/"`.

- [x] **Step 2: Run it and watch it fail**

Run: `just test tests/adapters/test_slack_gateway.py -k debug_logging`
Expected: FAIL, with `xoxd-test` found in the captured "Sending a request" line.

- [x] **Step 3: Pass a logger pinned at INFO to `WebClient` in `web_api`**

- [x] **Step 4: Run the test and `just check`**

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git commit -m "Keep the session cookie out of slack_sdk's debug log"
```

---

### Task 2: A failed read skips one thread or item, not the whole pull [done]

Backlog line: "A transient error (network, `ratelimited` after retries) on one thread's `conversations.replies` aborts the whole pull".

The gateway turns network failures and unknown Slack error codes into a plain `SlarcError`. Today any of them raised inside `_PullRun` ends the pull. After this task:

- **A thread read under a conversation** (`_append_read`): warn, keep going, and record the thread's ts in the conversation's new `retry_threads`. The next pull re-reads those threads first, counting them under the thread-read reason `retry`.
- **A history page** (`_pull_conversation`): warn and move to the next item. The item keeps the progress recorded so far and is not marked finished, so the next pull resumes from it.
- **A tracked thread or message item** (`_read_thread`): warn and leave the item as it was.
- **A new message link** (`_message_item`): warn; the link is not tracked.
- **`AuthFailed` and `NotVisible`** keep today's behavior: `AuthFailed` skips the workspace, `NotVisible` marks the item `not_visible`.

Warning texts, exactly:
- Thread: `f"Could not read the thread at {link}, so it is retried on the next pull: {error}"`
- Conversation: `f"Could not finish pulling conversation {channel}; the next pull resumes it: {error}"`
- Tracked thread or message item: `f"Could not read {link}; the next pull tries again: {error}"`
- New link: `f"Could not read {link}; pull it again to track it: {error}"`

`link` is `message_link(domain, channel, ts)`. Thread reads happen before the directory refresh, so no conversation label is available yet.

`PullReport.items_pulled` changes from counting `status == "ok"` entries in the run's tracked list to counting the items `_finish` recorded in this run. An item that warned and stopped part way is recorded but not pulled.

**Files:**
- Modify: `src/slarc/domain/tracking.py` (`TrackedItem`)
- Modify: `src/slarc/service/pull.py`
- Modify: `docs/specs/detailed-requirements.md` (tracked_item and run rows near line 312), `docs/architecture.md` (pull steps)
- Test: `tests/service/test_pull_conversation.py`, `tests/service/test_pull_thread.py`

**Interfaces:**
- Produces: `TrackedItem.retry_threads: list[str] = []`, holding thread ts, oldest first.
- Produces: `ThreadReason` gains `"retry"`.
- Produces: `_HeldConversation.failed_threads: set[str]`, the threads whose read failed in this run.
- Produces: `_PullRun.pulled: int`, incremented in `_finish`.
- `retry_threads` on every conversation record is `sorted(held.failed_threads | (set(item.retry_threads) - held.read_threads), key=MessageTs)`, so a pull interrupted after a failure still keeps the thread. Route every `_record` call made while a conversation is held through one helper that applies this.

- [x] **Step 1: Write the failing tests**

In `tests/service/test_pull_conversation.py`:

```python
def test_failed_thread_read_warns_and_is_retried_next_pull(h: Harness, monkeypatch):
    served = h.gateway.replies

    def unreachable(channel: str, ts: str) -> list[dict]:
        raise SlarcError("Could not reach Slack to call conversations.replies.")

    monkeypatch.setattr(h.gateway, "replies", unreachable)
    report = h.pull(BASE)

    link = f"{BASE}/p1792410693663869"
    assert report.skipped == [
        f"Could not read the thread at {link}, so it is retried on the next pull: "
        "Could not reach Slack to call conversations.replies."
    ]
    assert report.items_pulled == 1
    [item] = h.tracked()
    assert (item.backfill_complete, item.retry_threads) == (True, [PARENT_TS])
    assert REPLY_TS not in {env.meta.ts for env in h.lines("message")}

    monkeypatch.setattr(h.gateway, "replies", served)
    h.pull()

    assert REPLY_TS in {env.meta.ts for env in h.lines("message")}
    assert h.last_run().counts["threads_read"]["retry"] == 1
    [item] = h.tracked()
    assert item.retry_threads == []


def test_failed_history_page_skips_the_conversation_and_pulls_the_rest(h: Harness, monkeypatch):
    # A second conversation in the same fake workspace, with one message.
    # CHANNEL serves three messages with page_size=2, and history raises SlarcError on
    # CHANNEL's second page, so the item has been recorded once (Task 3 records it earlier).
    report = h.pull(BASE, SECOND_LINK)

    assert report.items_pulled == 1
    assert any(w.startswith(f"Could not finish pulling conversation {CHANNEL}") for w in report.skipped)
    assert the second conversation's message is stored
    assert the CHANNEL item is tracked with backfill_complete False


def test_auth_failure_on_a_thread_read_still_skips_the_workspace(h: Harness, monkeypatch):
    # replies raises AuthFailed("desktop", "naleidworkspace")
    report = h.pull(BASE)
    assert report.failed is True
    assert h.last_run().errors[0]["team_id"] == TEAM
```

In `tests/service/test_pull_thread.py`:

```python
def test_failed_read_of_a_tracked_thread_leaves_it_for_next_pull(h, monkeypatch):
    h.pull(THREAD_LINK)
    [before] = h.tracked()
    # replies raises SlarcError
    report = h.pull()
    assert report.items_pulled == 0
    assert report.skipped[0].startswith("Could not read ")
    assert report.skipped[0].endswith("; the next pull tries again: <error text>")
    [after] = h.tracked()
    assert (after.status, after.last_pull_at) == ("ok", before.last_pull_at)


def test_failed_read_of_a_new_message_link_is_not_tracked(h, monkeypatch):
    # replies raises SlarcError
    report = h.pull(MESSAGE_LINK)
    assert report.skipped == [
        f"Could not read {MESSAGE_LINK}; pull it again to track it: <error text>"
    ]
    assert h.tracked() == []
```

Use the thread and message link constants the module already defines. Replace each `<error text>` with the `SlarcError` message the test raises.

- [x] **Step 2: Run them and watch them fail**

Run: `just test tests/service -k "failed_ or auth_failure_on_a_thread"`
Expected: the `SlarcError` tests fail with the error propagating out of `pull`. The `AuthFailed` test may already pass; it guards the new `except` clauses.

- [x] **Step 3: Implement**

Catch `SlarcError` after the existing `AuthFailed`, `NotVisible` and `ThreadNotFound` clauses at the four sites above, re-raising `AuthFailed` before the general clause. In `_pull_conversation`, a pull that is not the item's first re-reads `item.retry_threads` (inside the item's range) with reason `"retry"` before paging history.

- [x] **Step 4: Update the spec and architecture text**

- **tracked_item row:** add `retry_threads (threads whose read failed, re-read first on the next pull)`.
- **run row:** add `retry` to the `threads_read` reasons.
- **`docs/architecture.md` pull steps:** say that a failed read warns and skips one thread or item.

- [x] **Step 5: Run `just check`**

Expected: PASS, including the existing pull tests unchanged.

- [x] **Step 6: Commit**

```bash
git commit -m "Skip one thread or item when a read fails, and retry failed threads next pull"
```

---

### Task 3: Record named items before reading them [done]

Backlog line: "Interrupting a first pull before its first history page completes loses the new item or a widened `since`".

**Files:**
- Modify: `src/slarc/service/pull.py` (`_PullRun.resolve`)
- Modify: `docs/specs/detailed-requirements.md` (tracked_item row)
- Test: `tests/service/test_pull_conversation.py`

**Interfaces:**
- Consumes: `_PullRun.pulled` from Task 2. Recording an item at resolve time must not count it as pulled.
- Produces: `resolve` calls `self._record(item)` for each selected item that is new or differs from its tracked state, after `_ranged` has applied the options' range.

- [x] **Step 1: Write the failing tests**

```python
def interrupt_history(h: Harness, monkeypatch, on_page: int) -> None:
    """Raises KeyboardInterrupt when history reaches the given page."""


def test_interrupted_first_pull_keeps_the_new_item(h: Harness, monkeypatch):
    interrupt_history(h, monkeypatch, on_page=1)
    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE)
    [item] = h.last_run().tracked
    assert (item.channel, item.backfill_complete, item.oldest_ts) == (CHANNEL, False, None)

    monkeypatch.undo()
    h.pull()
    [item] = h.tracked()
    assert item.backfill_complete is True


def test_interrupted_pull_keeps_a_widened_since(h: Harness, monkeypatch):
    h.serve([ts_on(17, 9), ts_on(18, 9)])
    h.pull(BASE, options=PullOptions(since="2026-10-18"))
    interrupt_history(h, monkeypatch, on_page=1)
    with pytest.raises(KeyboardInterrupt):
        h.pull(BASE, options=PullOptions(since="2026-10-17"))
    [item] = h.tracked()
    assert (item.since, item.backfill_complete) == (date_to_ts("2026-10-17", end_of_day=False), False)


def test_interrupted_pull_keeps_every_new_item(h: Harness, monkeypatch):
    # BASE plus two more new links (a second conversation and a thread link), interrupted on BASE's first page.
    # All three keys are in h.tracked() afterwards, and a bare pull finishes all three.
```

`interrupt_history` follows the pattern in `test_interrupted_backfill_resumes_from_oldest_ts`.

- [x] **Step 2: Run them and watch them fail**

Run: `just test tests/service/test_pull_conversation.py -k interrupted_`
Expected: the three tests fail; `test_interrupted_backfill_resumes_from_oldest_ts` still passes.

- [x] **Step 3: Implement, then add to the tracked_item row:** "An item named on the command line is recorded before it is read, so an interrupted pull keeps it."

- [x] **Step 4: Run `just check`**

- [x] **Step 5: Commit**

```bash
git commit -m "Record items named on the command line before reading them"
```

---

### Task 4: Bare pull with nothing tracked, and an aborted create prompt [done]

Backlog lines: "A bare `slarc pull` with nothing tracked prints "Pulled 0 items"" and "Ctrl-C at the create-archive prompt exits 1 with "Aborted!"".

Behavior after this task:
- **No archive found:** a bare `slarc pull` (no selectors) prints `NO_TRACKED_ITEMS`, exits 0, creates nothing and calls no Slack method. It never offers to create an archive, even with `--yes`, since an empty archive would track nothing.
- **Archive with nothing tracked:** a bare `slarc pull` prints `NO_TRACKED_ITEMS` instead of the summary and exits 0.
- **Aborted create prompt:** Click raises `click.exceptions.Abort` for both Ctrl-C and end of input at the prompt. Treat it as declining: `_fail("No archive created.")`, exit 1, with no "Aborted!" line. Nothing has run yet, so the interrupt message ("progress so far is saved") would be wrong.

**Files:**
- Modify: `src/slarc/service/pull.py` (`PullReport`), `src/slarc/cli.py` (`pull_command`, `_archive_for_pull`)
- Test: `tests/test_cli_pull.py`

**Interfaces:**
- Produces: `PullReport.items_selected: int`, the number of items `resolve` returned.

- [x] **Step 1: Write the failing tests**

```python
def test_bare_pull_without_an_archive_says_nothing_is_tracked(f: Fixture):
    result = f.pull("--yes")
    assert result.exit_code == 0, result.output
    assert "No tracked items yet. Pull a Slack link to start tracking it." in result.output
    assert list(f.work.iterdir()) == []
    assert f.gateway.calls == []


def test_bare_pull_with_nothing_tracked_says_so(f: Fixture):
    create_archive(f.work)
    result = f.pull()
    assert result.exit_code == 0, result.output
    assert "No tracked items yet. Pull a Slack link to start tracking it." in result.output
    assert "Pulled" not in result.output


def test_create_prompt_aborted_creates_nothing(f: Fixture):
    result = f.pull(BASE, input="")
    assert result.exit_code == 1
    assert "No archive created." in result.output
    assert "Aborted!" not in result.output
    assert list(f.work.iterdir()) == []
```

- [x] **Step 2: Run them and watch them fail**

Run: `just test tests/test_cli_pull.py -k "nothing_tracked or prompt_aborted"`
Expected: FAIL.

- [x] **Step 3: Implement**

- [x] **Step 4: Run `just check`**

Existing tests that run a bare pull already create the archive with an earlier `--yes BASE` pull, so they should pass unchanged. If one relies on a bare pull creating an archive, change the test and say so in the report.

- [x] **Step 5: Commit**

```bash
git commit -m "Say nothing is tracked on a bare pull, and treat an aborted prompt as declining"
```

---

### Task 5: Ctrl-C stops in-flight downloads within one chunk

Backlog line: "Ctrl-C during the drain waits for every in-flight download, up to 60 s and 100 MB each".

`HttpDownloader.fetch` calls `pool.shutdown(cancel_futures=True)` in its `finally`. That drops queued jobs but waits for running ones to finish.

**Files:**
- Modify: `src/slarc/adapters/downloader.py`
- Modify: `docs/specs/detailed-requirements.md` if it describes interrupt behavior during downloads (search for "Ctrl-C")
- Test: `tests/adapters/test_downloader.py`

**Interfaces:**
- Produces: `fetch` creates a `threading.Event` and sets it in its `finally` before `pool.shutdown`. Each worker checks the event before each request hop and in `_limited` before yielding each chunk, and stops by raising a private `Cancelled` exception. `_fetch` turns `Cancelled` into `_result(job, "failed", "Download interrupted.")`. `FsBlobStore.write` already removes its temp file when the chunk iterator raises.

- [ ] **Step 1: Write the failing test**

```python
def test_closing_fetch_stops_an_in_flight_download(tmp_path: Path):
    streaming = threading.Event()

    def slow_body() -> Iterator[bytes]:
        for _ in range(2000):
            streaming.set()
            yield b"x" * 1024
            time.sleep(0.05)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slow":
            return httpx.Response(200, content=slow_body())
        return httpx.Response(200, content=b"fast")

    jobs = [
        job("https://cdn.example.com/slow", max_bytes=10**7),
        job("https://cdn.example.com/fast"),
    ]
    results = downloader(handler, FsBlobStore(tmp_path)).fetch(jobs, 2)

    assert next(results).job.url.endswith("/fast")
    assert streaming.wait(5)
    started = time.monotonic()
    results.close()

    assert time.monotonic() - started < 1.0
    assert list(tmp_path.rglob(".tmp-*")) == []
```

If `MockTransport` turns out to read an iterator body in full before returning, serve `/slow` from a local `http.server` thread instead and say so in the report.

- [ ] **Step 2: Run it and watch it fail**

Run: `just test tests/adapters/test_downloader.py -k closing_fetch`
Expected: FAIL on the elapsed-time assertion (about 100 s without the fix). Run it with `--timeout` if the plugin is installed. Otherwise lower the loop count to 200 (10 s) while checking that it fails, then restore it.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run `just check`**

- [ ] **Step 5: Commit**

```bash
git commit -m "Stop in-flight downloads within one chunk on Ctrl-C"
```

---

### Task 6: Split `service/pull.py`

Backlog line: "`pull.py` is over 800 lines; split it at the `_PullRun` / `_HeldConversation` seam, merge the near-duplicate `_page_older` and `_page_newer` loops, and rename `_Workspaces.connect` / `connection`".

This is a refactor with no behavior change: the existing test suite is the check, and no test changes beyond imports. Move code as it stands after Tasks 2 and 3.

**Files:**
- Create: `src/slarc/service/workspaces.py`: `Connection` (was `_Connection`), `Workspaces` (was `_Workspaces`), `_require_workspace`.
- Create: `src/slarc/service/held_conversation.py`: `HeldConversation` (was `_HeldConversation`).
- Create: `src/slarc/service/directory_refresh.py`: `refresh_directory`, `unknown_bots` (was `_unknown_bots`).
- Modify: `src/slarc/service/pull.py`. It keeps `pull`, `PullOptions`, `PullReport`, `date_to_ts`, `_PullRun`, `_ItemProgress`, `_within`, `_two_days_before` and `_outermost`, and re-exports nothing.
- Modify: `docs/architecture.md` (service module list and pull steps), `docs/specs/detailed-requirements.md` only if it names these modules.

**Interfaces:**
- `Workspaces.connection(workspace)` becomes `Workspaces.connection_to(workspace) -> Connection`. `Workspaces.connect(workspace) -> SlackGateway` keeps its name, since it implements `SlackConnector` for the download drain.
- `_page_older` and `_page_newer` become one `_page_history(connection, held, item, view, oldest, latest, reason, *, newest_each_page: bool) -> TrackedItem`.
  - With `newest_each_page=True` (the backfill, which pages from the top or below `oldest_ts`), it records `newest_ts` and `oldest_ts` after every page and returns the item with `backfill_complete=True`.
  - With `newest_each_page=False` (newer history and `--full`), it records `oldest_ts` after every page and moves `newest_ts` only when paging ends, so an interrupted pull pages the same range again.
  - Keep the docstring sentences that explain both.

- [ ] **Step 1: Move the three groups of code into their modules, fixing imports**

Run `just check` after each move.

- [ ] **Step 2: Merge the two paging loops and rename `connection` to `connection_to`**

- [ ] **Step 3: Verify**

Run: `just check` and `wc -l src/slarc/service/*.py`
Expected: all tests pass unchanged; `pull.py` is under 650 lines.

- [ ] **Step 4: Update `docs/architecture.md`**

- [ ] **Step 5: Commit**

```bash
git commit -m "Split service/pull.py into workspaces, held_conversation and directory_refresh modules"
```
