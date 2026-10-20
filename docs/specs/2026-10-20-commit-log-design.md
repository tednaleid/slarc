# runs.jsonl as a commit log: commits, tags, a lock, and removals that never wait

> Replaced by `2026-10-20-ducklake-archive-design.md` before it was built. Kept for the reasoning; do not build it.

## Intent

slarc's archive is append-only lines folded into current state, which already makes it most of a log-structured store. Three things are missing, and each one can lose data:

- **A crash leaves half-done work that later runs trust.** A killed pull leaves message lines that no run line describes. A later pull skips those messages because their hashes match, so it relies on lines no record vouches for. If anything ever deletes them, the later item claims a complete backfill over a hole.
- **A stale pull can undo a removal.** A pull that started before `slarc rm` writes its tracked items back as `ok` when it finishes, after rm swept their messages. The item is tracked again, with cursors claiming data that is gone.
- **Deleting is not coordinated with readers.** rm's sweep deletes what no tracked item covers right now, including blobs a running pull has just written but not yet recorded.

This design makes `runs.jsonl` the archive's commit log. Work becomes part of the archive only when a commit says so. Every act of tracking gets a tag, so a removal always beats a stale update. Bytes are deleted only when no running command can still rely on them. The aim is an archive that is atomic and repairable: every failure leaves state the next command can recognize and finish, with no human judgment required. That includes failures from crashes, interrupts, concurrent commands and git merges.

Background, all committed:
- the explainer at `.llm/runs-and-concurrency.html` (gitignored, local);
- `docs/research/2026-10-20-crdt-tracked-items.md` (CRDT semantics, causal stability, flock);
- `docs/research/2026-10-20-storage-step-back.md` (LSM and object-storage comparison).

The spike results are in `docs/research/2026-10-20-commit-log-spikes.md`.

## Goals

- **Commits.** A data line (message, deleted marker or directory line) is part of the archive only if its batch has a commit in `runs.jsonl`. The run that wrote it also sees it while running. Every read ignores everything else, and garbage collection deletes it.
- **Small commits.** A crash loses at most one tracked item's work, or one page of a backfill.
- **Removals win.** A stale update can never re-track what `slarc rm` removed, whatever order the lines land in. Pulling the selector again starts tracking afresh.
- **rm never waits.** It records the removal at once. Bytes are deleted as soon as no running command can rely on them: by rm itself if nothing is running, otherwise by the next command that can take the exclusive lock.
- **Repair without judgment.** Interrupted runs, interrupted sweeps, dangling lines and lines a git merge brought back are all found and finished automatically.
- **Git as a bridge.** Merging two copies of an archive is an ordinary `git merge` or rebase that never needs a decision about the archive's files.
- **Bounded growth.** `runs.jsonl` stops growing by a full snapshot of every item on every run.
- **Open to object storage.** The storage ports express "write a batch, then commit it", so a backend without appends can implement them later.

## Non-goals

- **No object-storage backend now.** The ports keep it possible; nothing is built for it.
- **No coordination across machines.** The lock is per machine. The remaining cross-machine risk is narrow and documented under Known limits.
- **No reading of Dropbox conflicted copies.** A run recorded only in a conflicted copy is uncommitted. Its lines are garbage-collected, and the next pull re-reads them from Slack.
- **No change to message, directory or blob file layout,** apart from never deleting a data file (Truncate, don't delete).
- **No per-run segment files.** They were considered and declined: see the storage step-back review.

## Anti-goals (a failure even if it works)

- A tracked item whose cursors claim messages that are not stored.
- A stale pull, a merge or a crash re-tracking a removed item, or re-downloading files rm removed.
- A git merge of two archive copies that stops on a conflict in the archive's own files.
- A crash that leaves state only a person can repair.
- Reads that get noticeably slower: the spike's budget is about 2% on folds and about 1% on DuckDB queries.

## Terms

`GLOSSARY.md` defines **tracked item**, **tag**, **run**, **batch** and **commit**. This document also uses:
- **dangling line:** a data line whose batch has no commit;
- **sweep:** the step that deletes bytes nothing needs: uncovered message lines, unreferenced file and image lines, unreferenced blobs, and dangling lines.

The builder may add those two terms to the glossary.

## Behavior

### Runs, batches and commits

A run is one invocation of `pull` or `rm`. It writes, in order:

1. **A `run_start` line,** once the run holds its lock (below), before it reads anything: run id, command, args, host, pid, start time.
2. **Batches and commits.** Each data line a run writes carries `meta.run = "<run id>/<n>"`, the id of its batch. `n` is zero-padded, so batch ids sort correctly inside `version_key`. When a batch is done, the run fsyncs its data files, then appends a `commit` line to `runs.jsonl` and fsyncs that. A commit line names its batch and carries what the batch earned: the snapshots of the tracked items it advanced (each with its tag) and the download states it queued or settled.
3. **A `run` line at the end,** as today: counts, errors, auth, and the keys of the items pulled without change (see Growth). It is no longer the only record of the run's progress.

`commit` and `run_start` are new kinds routed to `runs.jsonl`. DuckDB's `run` view keeps counting only `run` lines.

**Unique run ids.** Today's `r_20261020T140000Z` collides for two runs in the same second. Run ids gain a host label and a random suffix, for example `r_20261020T140000Z_mbp_3f9a`.

**Batch boundaries in pull:**
- an item named on the command line: committed (its new tag) before it is read, which keeps today's promise that an interrupted pull keeps a newly named item;
- each workspace's directory refresh;
- each tracked item, when it finishes (or is skipped as Slack-archived or not visible);
- each history page while an item backfills, with the cursor moved past that page;
- the download drain, every N results (the builder picks N, around 50), plus the final avatar and emoji linking.

**Batch boundaries in rm:** one commit records the removal (below).

**Reading.** Every fold and DuckDB view reads only committed lines, plus the reading process's own batches:
- **JSONL folds:** `JsonlArchiveStore._read` filters by the committed set, built from the run-line fold that is already done once per process. The fake store mirrors it.
- **DuckDB:** the committed batch ids go into a temp table when the index opens, and every source view filters through it. The spike measured -1% to +1% this way. A per-query view of `runs.jsonl` costs 4% to 6%, and a meta-only `read_ndjson` is 75 times slower: avoid both.

Pull's duplicate check (`held.stored`, `directory_state`, `outstanding_downloads`, `infer_deletes`) reads through the store, so it automatically compares only against committed lines plus its own. The spike showed this is required. Without it, a killed run followed by a committed run and garbage collection leaves the committed item missing half its messages. With it, nothing is lost.

**Legacy lines.** A line is legacy when its `meta.run` has no batch suffix, meaning it was written before this design. Legacy lines are always treated as committed, permanently. That includes lines from legacy runs that were killed before writing a `run` line: later pulls skipped those messages because the hashes matched, so hiding them would create gaps. The rule depends only on the line's format, so it needs no adoption step. Once an archive has been written by the new code, older slarc binaries are not supported on it: there is no release, and every install is updated with `just install`.

### Tags

- **Creating a tag.** Each act of tracking gets a unique tag: the id of the batch whose commit began tracking it. Only an explicit start of tracking creates a tag: the first pull of a selector, or pulling it again after a removal.
- **Updates name their tag.** Every later commit for that item names the tag it read at the start of the run.
- **Removing a tag.** `slarc rm` removes tags. Removed tags are kept forever; `runs.jsonl` is small, so this needs no garbage collection.
- **The fold.** An update to a removed or unknown tag is dropped. A tracked item's state is its live tag's latest committed snapshot, latest by `version_key`.
- **Two live tags for one key.** Two concurrent first pulls of the same channel can create them. The fold reads them as one item: max `newest_ts`, min `oldest_ts`, `backfill_complete` true if either says so, and other fields from the latest snapshot. A later rm removes both.
- **Download states carry the tag** of the item whose messages queued them. A state whose tag is removed is ignored. With nothing outstanding for that target, a stale `pending` or `failed` state can't re-download a file rm removed.
  - **Shared targets.** rm's commit re-tags any outstanding state that a remaining tracked item still references, giving it a surviving tag that covers a referencing message. rm already computes those references. This matters because pull only walks the messages it reads, so an older message in a quiet channel would otherwise never re-queue its target.
  - **Avatars and custom emoji** belong to the workspace, not to one item. Their states carry the workspace tag `workspace:<team id>`, which is live while anything in that workspace is tracked. That matches today's rule of keeping those images while anything in the workspace is tracked.
  - **No `removed` states.** rm no longer writes `removed` download states.

This is an observed-remove set of tags, with one payload per tag that can only be updated while the tag is live. The research notes cover why it converges, and why every published map design lets a concurrent update survive a remove instead.

### The lock

- **Where:** `$XDG_CACHE_HOME/slarc/locks/<sha256 of the archive's real path>.lock`, falling back to `~/.cache` (`config.py` falls back the same way for `XDG_CONFIG_HOME`). It is per machine by design, so it never lives in the archive or in a synced folder.
- **What:** `fcntl.flock`, never `lockf`. A `lockf` lock is dropped when the process closes *any* descriptor for the file.
- **pull** holds it shared from before its first read until after its final `run` line is fsynced. On the way out it releases the shared lock, tries exclusive without blocking (flock can't upgrade atomically), and if it gets it, runs the pending sweeps.
- **rm** takes it shared while it plans and records the removal. It then releases it, tries exclusive without blocking, and sweeps if it gets it. Otherwise it says the bytes will be deleted when the running pull finishes, and exits 0.
- **Waiting for a sweep.** If a sweep holds the exclusive lock, pull and rm wait for their shared lock, printing `Waiting for a cleanup to finish.` Sweeps are short, and "rm never waits" refers to waiting on pulls.
- **`slarc rm --sweep` when the lock is busy.** It records a pending full sweep (a line in `runs.jsonl`), says the full sweep will run when the running pull finishes, and exits 0. The next holder of the exclusive lock runs it.
- **export, search, list and sql** take no lock. They read only committed lines, and a sweep's renames are atomic.
- **Running or interrupted.** A `run_start` with no `run` line belongs to a run that is still going if the lock is held, and to an interrupted run if not. Holding the exclusive lock proves only that this machine's unfinished runs are dead. Every rule that acts on that proof is limited to runs whose `run_start` names this host.
- **The host label** in run ids and in `run_start` is the machine's short hostname. Correctness depends on it being stable per machine. If the hostname changes, the old name's interrupted runs are never collected, which leaves only invisible bytes behind.

The lock is reached through a port, so service code stays free of filesystem access and tests use a fake.

### Sweeps and garbage collection

A sweep runs only while its process holds the exclusive lock, and it recomputes what to delete under the lock. It has three triggers:

1. **Pending removals:** an rm commit with no later `sweep_done` line for it.
2. **Interrupted runs:** a `run_start` with no `run` line, found while holding the exclusive lock. Its uncommitted batches are dangling.
3. **An explicit full sweep:** `slarc rm --sweep`.

**Targeted sweep, for a pending removal.** It works outward from what was removed:
- message lines in the channels the removed tags covered that no live tag covers;
- then file and image directory lines referenced only by those deleted messages;
- then the blobs of those file and image lines that no other directory line references.

It deletes nothing outside that set. When done, it appends `sweep_done` naming the rm's batch.

**Dangling-line collection, for an interrupted run.**
- **Scope.** It acts only on interrupted runs whose `run_start` names this host. Another machine's run may still be running, with its data lines synced ahead of its commits.
- **What it deletes.** Every data line whose `meta.run` belongs to that run's uncommitted batches. `run_start` is written before the run knows its workspaces, so collection either scans every workspace or uses the workspaces the run's commits name; the builder chooses.
- **Afterwards.** It appends a line marking the run collected. The builder picks the line's shape; it could be a `sweep_done` that names the run.
- **Other machines' runs.** Another machine's interrupted runs are collected when that machine next runs slarc. Until then their lines are invisible bytes.

**Full sweep, on request.** `slarc rm --sweep` (it needs no selector) re-checks the whole archive:
- every uncovered message line;
- every unreferenced file and image line;
- every unreferenced blob;
- every dangling line from this host's interrupted runs;
- every non-canonical file under `raw/` and `directory/`, such as a Dropbox conflicted copy (Conflicted copies, below).

It is the cleanup to run after merging archive copies, because a git merge can bring back lines rm deleted (Git, below). It is today's sweep, extended to dangling lines and stray files.

**Missing blobs.** A file or image line whose blob file is missing counts as not downloaded, so the next pull downloads it again. That turns a deleted blob, from any cause, into a re-download.

### Truncate, don't delete

A sweep never deletes a data file. A month file or directory file left with no lines is truncated to zero bytes, and removing a whole workspace truncates every file in it.

The reason is git (spike 1). If one copy of an archive deletes a file while another appends to it, the merge stops on a modify/delete conflict that the union merge driver can't resolve. Resolving it the intuitive way ("keep the deletion") silently loses lines a new tag needs. With truncation the merge is a normal two-sided edit: the union driver keeps both sides, and the next full sweep removes what no tag covers.

Readers ignore empty files. They are never cleaned up later, because cleanup would reintroduce the conflict for any copy not yet merged. Blobs are still deleted: they are immutable, so a blob is never "modified" on another branch.

### Git

- **The union driver.** The archive gets a `.gitattributes` with `*.jsonl merge=union`: slarc writes it when it creates an archive, and adds it to an existing archive that lacks it. All `.jsonl` folds are order-independent and tolerate duplicates, so a union of two copies' lines is the correct merge. Spike 1 confirmed this for merge and rebase, using slarc's own store code and folds.
- **What a merge can do.** It can bring back lines one copy's rm deleted, where the deletion and the other copy's append touched the same region. The state stays correct: those lines are covered by no live tag. The bytes remain until a full sweep. The docs recommend `slarc rm --sweep` after merging; a post-merge hook can run it.
- **History.** rm deletes from the working tree, never from git history. The docs and rm's prompt text should not imply otherwise.

### Conflicted copies

Readers open only canonical names:
- `runs.jsonl`;
- `directory/<team>/<kind>.jsonl`;
- month files matching `YYYY-MM.jsonl`. Today they are globbed as `*.jsonl`, which picks up conflicted copies.

DuckDB's source globs change the same way.

- **New conflicted copies.** Lines that exist only in a conflicted copy are treated as dangling, per Ted's rule: the next pull re-reads them from Slack. The full sweep deletes non-canonical files under `raw/` and `directory/`. Conflicted copies of `runs.jsonl` are never read and are left in place for a person to inspect.
- **Existing conflicted copies.** Today's `*.jsonl` glob already reads conflicted copies of month files, and later pulls may have skipped messages because they matched lines held only in such a copy. Migration therefore appends the lines of each existing conflicted month file to its canonical file, then deletes the copy, so switching to canonical names hides nothing.

### Growth of runs.jsonl

Today every run line carries a full snapshot of every item it pulled: about 1 GB a year for an hourly cron over 300 items. After this change:

- **Changed items only.** A commit carries the snapshot only of an item whose state changed beyond its pull and search times. An unchanged item appears only as a key in the final `run` line's list of items pulled without change.
- **Implied times.** For those items the fold sets `last_pull_at` and `last_search_at` to the run's start time. That is the earliest safe value: `last_search_at` bounds the next search, so an earlier value can only repeat search results, never miss them.
- **Only real successes count as unchanged.** An item whose search or any read failed is not "unchanged". It commits its snapshot as today, so a failed search keeps its old `last_search_at` and is retried.
- **`omit_defaults` on `TrackedItem`,** so null fields are not written (286 bytes against 372 per item).

The spike projected about 0.18 GB a year for this shape, down from 0.99 GB. A delta encoding (0.03 GB a year) is left for later, if needed.

### rm, from the user's side

- **The prompt and confirmation are unchanged.**
- **The result.** On confirmation, rm commits the removal and either sweeps now or prints, for example:
  ```
  Stopped tracking 3 items. Their 2470 lines, 85 files and 12 images will be deleted when the running pull finishes.
  ```
- **Exit status.** rm exits 0 in both cases.
- **A selector naming only already-removed items** is accepted, as today. It finishes any pending sweep it can get the lock for.
- **`--sweep`** runs the full sweep and needs no selector.
- **Help text.** The "do not run rm while a pull is running" warning in the help and spec is removed.

### slarc list

- **Interrupted runs.** `slarc list` shows interrupted runs ("run r_... (pull) was interrupted on 2026-10-20 14:02; its uncommitted lines will be cleaned up") until they are collected.
- **Pending removals.** It shows pending removals until they are swept.

### Migration

Legacy lines stay committed by format (above), so no data needs adopting. The first run that holds the exclusive lock under the new code does three things:

- **Implicit tags.** Each tracked item gets an implicit tag: the id of the first run that recorded it after its latest legacy removal, if it has one. That keeps an item that was removed and then pulled again under the old code tracked. Untagged download states keep today's latest-wins rule until a tagged state replaces them.
- **Existing conflicted month copies** are folded into their canonical files (Conflicted copies, above).
- **Git setup.** It writes `.gitattributes` if it is missing.

## Known limits

- **Two machines** share no lock. Removals still converge everywhere, because tags don't depend on the lock. Dangling-line collection only ever touches the local machine's runs. The remaining risk is narrow: a gap can appear if one machine's targeted or full sweep deletes lines in a channel that the other machine is pulling, before that machine's commits have synced. With one machine pulling, which is the expected setup, it doesn't arise.
- **Durability is `fsync`, not `F_FULLFSYNC`.** A power loss can lose data the drive had acknowledged but not yet written.

## Constraints

- **Layering.** Domain and service code stay free of filesystem, environment and `fcntl` access. The lock, batch stamping, commit writing and the committed-only filter go through ports (`ArchiveStore`, `BlobStore`, a lock port), with in-memory fakes and contract tests against the real adapters.
- **Docs.** ADR 0001 gets an amendment (the archive is now a commit log). A new ADR records the commit-log decision and why object storage is out of scope for now. The spec, `docs/architecture.md` and the README are updated.
- **Tests first,** as always. The race scenarios each get a test:
  - a stale update to a removed tag;
  - a killed pull, then a pull, then collection;
  - a sweep refused while the shared lock is held;
  - a git-merge round trip.
- **Acceptance.** `just acceptance` must pass. This change touches pull's discovery and delete logic, so running it is required.

## Decided by Ted

- `runs.jsonl` is the single source of truth for what is committed. Conflicted copies are never read, and a run only in a conflicted copy is dangling.
- The lock lives in the XDG cache directory, not in the archive.
- rm never waits: the removal is recorded immediately, and the sweep runs when it is safe.
- The sweep is targeted, with a full sweep on request.
- Commit size is decided by spike 2: per item, plus per page during backfills.
- The word "commit", with git's always called "git commits".
- Files are truncated, not deleted.
- No per-run segment files. Object storage later, kept possible by the ports.

## My calls (Claude), open to Ted's correction

- Batch ids live in `meta.run` (`<run>/<n>`) rather than in a new meta field. This keeps `version_key` and every existing reader unchanged.
- A tag is the id of the batch that began tracking. Migration uses the first run that recorded the item.
- Download states carry a tag, which replaces rm's `removed` download states.
- Unchanged items take the run's start time as their implied pull and search time.
- `slarc rm --sweep` is the full sweep's name. No separate command is added.
- rm exits 0 when its sweep is deferred.
- A `.gitattributes` is written into every archive, git or not. It's harmless outside git.
- No `sweep_host` setting. The cross-machine risk is documented instead.
- Legacy lines are committed by format, permanently, and old binaries are unsupported on an archive written by the new code.
- Dangling-line collection acts only on this host's runs, and the host label is the short hostname.
- rm re-tags outstanding download states that surviving items still reference. Avatars and emoji use a workspace tag.
- Pull and rm wait for a running sweep. `rm --sweep` with the lock busy records a pending full sweep.
- Migration folds existing conflicted month copies into canonical files. The full sweep deletes new non-canonical data files, and leaves conflicted copies of `runs.jsonl` in place.

## Left to the builder

- The exact encoding of `commit`, `run_start`, `sweep_done` and pending-full-sweep lines, and of tag removal and re-tagging in rm's commit.
- N for download batches.
- How the committed set and own-batch set are threaded through `JsonlArchiveStore`, the fake and `DuckDbQueryIndex`.
- Task order. A reasonable order is:
  - unique run ids and `run_start`;
  - batches, commits and the committed-only filter;
  - tags;
  - the lock and deferred sweeps;
  - the targeted sweep and dangling-line collection;
  - truncation and conflicted-copy-safe globs;
  - `.gitattributes`;
  - growth;
  - migration;
  - list output and docs.
