# Spikes for the commit-log design

Two throwaway spikes run on 2026-10-20 against commit `ac21cc1` to inform `docs/specs/2026-10-20-commit-log-design.md`. The spike scripts were not kept; the measurements and conclusions are below.

## Spike 1: git union merge on archive JSONL

Throwaway git repos built from the fixture archive (`tests/fixtures/workspace`). Lines are written with slarc's own `JsonlArchiveStore`, `append_run` and `drop_messages`. The state after each merge is checked with slarc's folds.

| Scenario | Plain git merge | `*.jsonl merge=union` |
|---|---|---|
| S-A: two branches each append a pull to the same month file and to `runs.jsonl` | Conflicts in both files | Clean merge. The fold shows all 6 messages, and the channel's cursor comes from the later run |
| S-B: one branch's rm drops lines in the middle of a month file; the other branch appends | (not run) | Clean merge. The deletions stay deleted, the append is kept, and the fold shows the channel as removed |
| S-C: one branch's rm drops the last lines of a month file; the other branch appends at the end | (not run) | Clean merge, but the lines rm deleted come back. Union keeps both sides of the overlapping region |
| S-D: one branch's rm deletes the whole month file; the other branch appends to it | (not run) | Modify/delete conflict. The union driver does not apply to that kind of conflict |
| S-D2: like S-D, but rm leaves an empty file | (not run) | Clean merge, with the same resurrection as S-C |
| S-E: S-A done with rebase instead of merge | (not run) | Clean rebase, and the same fold result as S-A |

### Conclusions
- **Append-only branches need the union driver.** Without it, two branches that both pull always conflict at the end of the file. With it, both git merge and git rebase are clean, and the folds give the correct state, because they ignore line order and tolerate duplicate lines.
- **Union merge can bring back lines rm deleted** when the deletion and the other branch's append touch the same region of the file. It happens most often at the end of the file, which is where appends land.
  - The resurrected lines belong to committed runs, but no live tag covers them, so the next sweep deletes them again. The archive's state stays correct; only the bytes return until that sweep.
  - Sweeping after a merge is therefore part of the workflow. A post-merge hook, or the next pull or rm, can do it.
- **Deleting a file loses to an append** with a modify/delete conflict that git won't resolve on its own. Options:
  - rm truncates the file to empty instead of deleting it. That converts the conflict into a clean merge followed by a later sweep, at the cost of leaving empty files behind.
  - Leave the conflict for the user. The documented resolution is to keep the file, then sweep.
  - Whole-workspace rm deletes directories outright, so it hits this conflict for every file in them.
- **Git history keeps everything ever committed.** rm removes data from the working tree, never from history.

## Spike 2: runs.jsonl as the commit log

Rule under test: a message or directory line belongs to the archive only if its `meta.run` is committed in `runs.jsonl`. Lines from uncommitted runs are dangling: reads ignore them, and garbage collection (under the lock) deletes them.

Written 2026-10-20 against commit `ac21cc1`. Throwaway code; nothing in the repo was changed. Hardware: Apple M1 Ultra, 64 GB, APFS, Python 3.14.2, DuckDB 1.5.6. DuckDB numbers are pinned to 4 threads.

### Summary and recommendation

**The rule is cheap to read.** It adds about 2% to the JSONL folds and 0 to 6% to DuckDB queries. The committed set comes from a file pull already decodes.

**The rule requires one change, in pull's duplicate check.** Pull must compare against committed lines plus its own run's lines.
- The real `pull()` reproduces the failure. After a killed run, then a committed run, then GC, the committed item reads `backfill_complete` but is missing 1,500 of 3,000 messages and 2 of 3 users.
- With the filter in the store, nothing is lost.
- The own-run exception is required. Without it, a run never records the avatar blobs it just downloaded.
- The fix belongs in the store's `_read` (and the fake's), so no service code changes.

**Commit size: per item, and per history page while an item backfills.**
- Stamp lines with a batch id (`<run>/<n:04d>`) in `meta.run`.
- Append a fsynced `commit` line (its own kind, routed to `runs.jsonl`) when an item finishes, after each backfill page, after each workspace's directory refresh, and every N results of the download drain.
- Write commit lines only for changed items. Record unchanged items compactly in the end-of-run line.

### Key numbers

**Fold cost on a synthetic archive** (1M message lines in 300 channels, 298k directory lines, 2,000 runs, 225 MB `runs.jsonl`):
- full fold: 4.06 s today, 4.15 s with the filter (+2.3%);
- reading the committed set: 0.21 s with a decode of only `meta`;
- `tracked_items()` today: 1.82 s.

**DuckDB, real queries** (search, conversation_counts, messages):
- filtering through a temp table filled once when the index opens: -1% to +1%;
- filtering through a view: +4% to +6%.
- Pitfall: declaring only the `meta` column on `runs.jsonl` makes `read_ndjson` 75 times slower.

**Crash at a random moment, lost work:**

| Pull | Per run | Per item | Per page |
|---|---|---|---|
| Hourly, 300 items, 10% active | 14.8 min / 742 lines | 0.1 min / 3 | 0.1 min / 3 |
| First pull, 300 channels, 1M messages | 500 min / 572k lines (about 0.7 GB dangling) | 8.7 min / 10k | 0.5 min / 546 |
| First pull, one 200k-message channel | 100 min / 115k | 100 min / 115k | 0.5 min / 574 |

**runs.jsonl growth, hourly cron over 300 items:**

| Shape | Per run | Per year | Year fold of tracked items |
|---|---|---|---|
| Today: full snapshots in each run line | 112.6 KB | 0.99 GB | 9.0 to 9.4 s |
| Per item, full snapshots | 198.1 KB | 1.74 GB | 41.1 s |
| Per item, changed items only | 20.4 KB | 0.18 GB | about 4 s (estimate) |
| Delta encoding | 3.4 KB | 0.03 GB | 0.11 s |

A commit costs 0.2 ms (plain fsync, 21 open handles, APFS in /private/tmp).

### Surprises
1. **runs.jsonl growth is already a problem, independent of the rule.**
   - After a year of today's shape, `tracked_items()` takes 9.4 s and `downloads()` takes 11.3 s.
   - Pull calls `downloads()` once per conversation with new messages (`HeldConversation.downloads`), so 300 active items would spend about an hour folding.
   - Fixes: fold `runs.jsonl` once per process, and stop writing snapshots of unchanged items. `TrackedItem` also lacks `omit_defaults`.
2. **The own-run exception is required**, for `_Recorder.finish` linking avatar blobs.
3. **DuckDB:** a meta-only `read_ndjson` of `runs.jsonl` per query pushed search to 42 s. Use a temp table.
4. **Migration:**
   - Existing archives may hold lines from runs killed before the rule, which later runs skipped by hash. The first rule-aware run must commit those run ids, or the Q3 gap appears retroactively.
   - Finding them takes one scan, 3.7 s.
5. **SIGTERM:** slarc installs no handler, so cron timeouts, `kill` and shutdown skip `UnitOfWork.__exit__` and act as hard kills.
6. **`slarc list` (not caused by the rule):** it runs one full raw scan per tracked item, about 20 minutes for 300 items on the synthetic archive.
7. **Visibility:** a running pull's lines are invisible to search, export and list until committed. Per-item commits make items appear as they finish.
8. **GC:** a meta-only scan takes 3.7 s, and rewriting the affected files takes 4.0 s.

### Where the duplicate check lives

**Comparisons that read through the store** (all fixed by filtering in `_read`):
- `service/pull.py:618-619`, through `held.stored` (`held_conversation.py:22`);
- `service/appends.py:34` and `:44` (`directory_state`);
- `service/directory_refresh.py:67`;
- `service/downloads.py:289-290` and `outstanding_downloads`;
- `service/threads.py:35` (`infer_deletes`).

**Code to change:**
- `jsonl_store.py` `_read` (filter) and `append` (own batch ids);
- `tests/fakes.py` `InMemoryArchiveStore`, to mirror both;
- the DuckDB source views.
