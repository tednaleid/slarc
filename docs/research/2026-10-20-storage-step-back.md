# Should slarc's storage borrow from LSM trees or object-storage systems?

A step-back opinion on the storage design, written 2026-10-20 against commit `ac21cc1`, by someone reading the design cold. It evaluates three options against Ted's goals and ends with a recommendation.

Labels: **[source]** a cited primary source says it; **[code]** read from the slarc tree; **[inference]** my own reasoning.

## 1. Short answer

Keep the current file layout and build the direction under discussion (option A). It is already an LSM-shaped design in everything that matters, and the lock is the honest local equivalent of the mechanism RocksDB and SlateDB use to keep compaction from deleting what a live reader still needs. Take three small ideas from the LSM world (unique run ids, a run-start record, a dedicated host-local lock) and leave the rest (per-run segments, a manifest file, an object-storage backend). The rest solves scale and multi-writer problems slarc does not have, and each one costs the plain-folder ergonomics that are the point of the project.

The one place I would push on the current direction: the lock and the files Dropbox syncs. Details in sections 5 and 8.

## 2. What slarc already is, in LSM vocabulary

Reading `archive_state.py`, `jsonl_store.py` and `remove.py`, the archive maps onto an LSM tree almost one to one:

| LSM piece | slarc today **[code]** |
|---|---|
| Write path appends to a log, never updates in place | Every pull appends lines; a line is never edited |
| Read path merges versions, newest wins | `LatestMessages` and `latest_by_id` take the latest `(fetched_at, run)` per key |
| Tombstone | A `deleted` line, and an rm run line with `status: removed` |
| Manifest (what state is live) | `runs.jsonl`, folded oldest first |
| Compaction, writing a new file and swapping it in | `_rewrite`: kept lines to a temp file, fsync, `os.replace` |
| Garbage collection of unreferenced data | The blob sweep, `set(blobs.ids()) - referenced` |
| "Compaction must respect live snapshots" | Nothing yet; this is the pull/rm race |

Two things differ from a textbook LSM. First, slarc appends to existing month files instead of writing a new immutable file per flush, so files are mutable at the tail. Second, there is no record of in-flight readers, which is exactly the gap the lock fills. Everything else is the same shape, so the question is not "should slarc become an LSM" but "which two or three LSM mechanisms are worth importing".

Scale also matters. RocksDB's machinery exists to control read, write and space amplification over terabytes with thousands of writes per second. slarc's write rate is bounded by Slack's rate limiter, the recorded fixture archive is 2.8 MB across 12 JSONL files **[code]**, and a personal archive will be megabytes of JSONL plus blobs. Nothing in slarc's workload needs sorted segments, bloom filters or leveled compaction.

## 3. What the real systems do (verified)

**RocksDB.** The MANIFEST is "a transactional log of state changes": version edits record added and deleted files (with level, file number, size, key range), the log number and the last sequence number; `CURRENT` points at the latest manifest; atomic groups are never partially applied **[source]** (https://github.com/facebook/rocksdb/wiki/MANIFEST). Compaction inputs "may not qualify for being deleted immediately": each `Version` holds a reference to its SST files, iterators and compactions pin a version, and "If a file's SST reference count drops to 0, it can be deleted"; after a restart the directory is scanned and files no live version uses are deleted, with files above the lowest in-flight job's file number kept **[source]** (https://github.com/facebook/rocksdb/wiki/Delete-Stale-Files). In code, `FindObsoleteFiles` keeps any SST that is live in a version or numbered at or above `min_pending_output`, the smallest entry in `pending_outputs_`, with the comment "don't delete files that might be currently written to from compaction" **[source]** (https://github.com/facebook/rocksdb/blob/main/db/db_impl/db_impl_files.cc). A snapshot is a sequence number, and the compaction iterator "is aware of all the snapshots and ensures that the data visible to each snapshot is preserved" **[source]** (https://github.com/facebook/rocksdb/wiki/Snapshot). Concretely, `compaction_iterator.cc` keeps one version of a key per snapshot stripe and drops a delete marker only when it is visible to every live snapshot (`DefinitelyInSnapshot(ikey_.sequence, earliest_snapshot_)`) and `KeyNotExistsBeyondOutputLevel`, or at the bottommost level when no later version of the key follows **[source]** (https://github.com/facebook/rocksdb/blob/main/db/compaction/compaction_iterator.cc).

The lesson for slarc: RocksDB does not deduce liveness from who wrote a file. It counts who is reading. That is the same conclusion the runs-and-concurrency explainer reaches about provenance versus coverage.

**SlateDB.** Manifests are numbered objects, "The manifest with the highest ID is considered the current manifest", and an update is a read-modify-write committed with a conditional put; on failure "the client must retry the entire process". A new writer increments `writer_epoch` and fences older writers by claiming the next WAL slot, so "If a newer writer beats the current writer to the SST slot, the current writer has been fenced". "A compactor may not delete SSTs that are referenced by any active manifest", and the garbage collector deletes inactive manifests and unreferenced SSTs **[source]** (https://github.com/slatedb/slatedb/blob/main/rfcs/0001-manifest.md). A checkpoint is "a version of SlateDB's manifest" with an expiry (0 means never); a reader creates its own checkpoint with `checkpoint_lifetime`, must poll more than twice per lifetime, and extends the expiry when less than half remains; GC keeps SSTs referenced by checkpoint-pinned manifests and deletes unreferenced ones older than `min_age` **[source]** (https://github.com/slatedb/slatedb/blob/main/rfcs/0004-checkpoints.md). A later RFC fixes an ABA bug in that scheme: "object-store create-if-absent only protects against objects that currently exist", so once GC deletes a sequenced name a stalled writer can recreate it and believe it committed; the fix is a monotonic boundary file GC advances before deleting **[source]** (https://github.com/slatedb/slatedb/blob/main/rfcs/0026-garbage-collector-boundary.md).

The lessons: leases are how you register a reader when you cannot see its process, and never reuse an identifier that GC might have deleted. Both apply to slarc's run ids.

**S3.** `If-None-Match: *` and `If-Match: <etag>` are accepted on `PutObject`, `CompleteMultipartUpload` and `CopyObject`; a failed precondition returns `412 Precondition Failed`, a concurrent delete can return `409 Conflict` or `404`, and SigV4 signing is required **[source]** (https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html). This is what makes SlateDB's manifest CAS possible on plain S3.

**Dropbox.** "A conflicted copy is a file that Dropbox creates when multiple people edit the same file at the same time", including offline edits; "The editor's username, 'conflicted copy', and the save date will be added to the filename"; resolution is manual merge **[source]** (https://help.dropbox.com/organize/conflicted-copy). A file is ignored with `xattr -w com.dropbox.ignored 1 <path>`, or `com.apple.fileprovider.ignore#P` under File Provider; an ignored file stays on the local disk and "will be deleted from the Dropbox server and your other devices" **[source]** (https://help.dropbox.com/sync/ignored-files). Dropbox offers no conditional write and no cross-host lock, which rules out SlateDB-style coordination through the synced folder **[inference]**.

## 4. Option A: the direction under discussion, on today's layout

**Simplifies.** Nothing about the files changes. `grep`, `jq` and DuckDB's `raw/*/*/*.jsonl` glob keep working, git diffs stay small, and the rm spec's invariant ("every stored message is covered by some tracked item") stays the single rule.

**The race.** Tags (an observed-remove set with one payload per tag) make the removal win over a stale pull's update without any clock, which no LSM sequence number can do across two hosts **[inference]**, and the CRDT notes show the composition converges. The shared/exclusive flock is the local version of RocksDB's version refcount and SlateDB's checkpoint: a pull registers itself as a reader before it reads, and bytes are deleted only when no registered reader remains. The "last pull to finish sweeps" rule is RocksDB's "iterator cleanup purges stale files" in miniature.

**Crash recovery.** A killed pull leaves lines with a `meta.run` that no run line names; the next pull re-reads and writes nothing for unchanged hashes. A killed sweep leaves an rm line with no `sweep_done`; the next exclusive-lock holder finishes it. The flock dies with the process. This is the same shape as RocksDB's restart scan ("delete files no live version uses") and needs no fsck. The `run_start` line is the one addition that lets `slarc list` tell a run that is still running from one that died, and it is worth having for that alone.

**Second machine.** The lock cannot see the other host. Pulls are safe to overlap by the CALM argument (they only add). The sweep is the problem, and A's answer today is "the targeted sweep shrinks the window". That is honest, and Ted has said multi-machine is nice, not required. See section 8 for the two cheap mitigations I would add.

**Dropbox conflicted copies.** Two hosts appending to the same month file produce `2026-10 (host conflicted copy 2026-10-20).jsonl` style names. Because `_months` and the DuckDB index glob `*.jsonl` and the fold tolerates duplicates, a conflicted month file is read as a union and gives the right answer; the sweep rewrites it too, since `drop_messages` drops by ts across every month file **[code]**, **[inference]** on Dropbox keeping the `.jsonl` extension, which I did not test. `runs.jsonl` is read by exact name, so a conflicted copy of it is invisible, which is the one real gap.

**Browsability.** Unchanged.

**Migration.** Small: new line kinds in `runs.jsonl`, unique run ids, a lock, the targeted sweep. The fold must learn tags; existing archives without tags need a rule (treat each untagged tracked item as having one implicit tag named by its first run).

## 5. Option B: LSM-style segments on local disk or Dropbox

Each run writes new immutable files, a manifest lists live segments, compaction merges them, removal is a tombstone, deletion happens in compaction while respecting registered snapshots.

**Simplifies.** No file is ever appended to after it is written, so two hosts can never produce a conflicted copy of a data file: unique names, one writer each. Compaction and sweep become "write new file, publish, delete old", which is already how `_rewrite` works, now applied uniformly. Crash recovery is "files not in the manifest are orphans".

**Complicates.**
- *File count.* A cron pull that finds anything new writes one file per touched channel per run. The spec already keeps unchanged re-reads from writing lines, so quiet channels stay quiet, but an active workspace pulled hourly produces thousands of small files a year until compaction folds them. Dropbox and git both cope badly with many small files, and `rg` output and `ls` become noisy.
- *Browsability.* A reader must consult the manifest to know which files are live, or read everything and rely on the fold. Reading everything is what DuckDB does today and it keeps working, but removed data stays greppable in dead segments until compaction runs, which is the opposite of what `rm` promises. The cost of "which files are live" lands on every `jq` one-liner.
- *The manifest is the contended object.* On local disk a manifest is fine: append edits, rename on compaction, like RocksDB. On Dropbox it is the worst file to share: two hosts editing one manifest produce a conflicted copy of the thing that defines what is live. The only Dropbox-safe manifest is an append-only log per host, which is `runs.jsonl` under another name **[inference]**.
- *Snapshots still need a registry.* RocksDB's snapshots are in-process; SlateDB's checkpoints live in the manifest with leases. B on local disk still needs a way to know a pull is reading, which is the flock again, or a lease line that a crashed pull leaves stale until it expires. The lock is simpler and stricter.
- *Month partitioning* is what makes a dormant 2024 thread easy to find and makes an export page's inputs obvious. Per-run segments cut across months; keeping both means segments per channel per month per run.

**The race.** Handled by registered snapshots plus tombstones, same as A, with more moving parts.

**Crash recovery.** Good: orphan segments are deleted on the next start.

**Second machine.** Better than A for data files (no conflicted copies). No better for deletion: without a cross-host lease there is still no way to know the other host has no pull in flight, and a lease file in a Dropbox folder arrives with sync latency measured in seconds to days **[inference]**.

**Migration.** Large: a new store adapter, a manifest format, a compactor, changes to `_months`, the DuckDB globs, export's reading, `list`, and ADR 0001. It would be a rewrite of the storage layer for a gain (no in-place appends) that only matters when two hosts write at once.

## 6. Option C: an object-storage backend

S3 (or compatible) with a SlateDB-style manifest committed by conditional put, writer epochs for fencing, reader checkpoints with leases, GC that respects checkpoints, and local files as a cache or export.

**Simplifies.** This is the only option that solves the second-machine problem properly. CAS on the manifest makes "who is the writer" and "which readers are live" global facts rather than local guesses; a stale cron pull on a laptop gets fenced instead of racing. Blob storage is native. Durability and backup come for free.

**Complicates.**
- *Every command needs the network and credentials.* `list`, `search`, `export` and `jq` on a local folder stop being the default experience. DuckDB can read JSONL from S3 through its httpfs extension (I did not verify this against DuckDB's docs for this note), but `grep` and `jq` cannot, and "the archive is plain files in a folder I can zip and hand to someone" becomes "the archive is a bucket plus a tool".
- *A local cache is a second copy with its own staleness* and its own invalidation rules, which is a fourth kind of state (archive, cache, DuckDB index, export).
- *Fencing, leases and GC boundaries are real protocol work.* SlateDB needed RFC 0026 to close an ABA hole in its own manifest scheme. slarc would be re-deriving a subset of SlateDB, in Python, for one user.
- *Cost* is negligible at slarc's size (cents), so that is not an argument against it. Latency per request (hundreds of milliseconds) is tolerable for a cron pull.

**The race.** Solved properly.

**Crash recovery.** Solved by the manifest plus GC, as in SlateDB.

**Dropbox.** Removed from the picture, which also removes the easy sharing and the "it is just a folder" story.

**Browsability.** Worst of the three unless a local mirror is kept, in which case it is A with an extra layer.

**Migration.** The hexagonal ports make this a backend swap rather than a redesign: a new `ArchiveStore` and `BlobStore`, plus `QueryIndex` reading remote files. That is real credit to the current architecture. But the read path, the export writer and the CLI's defaults all assume a local root, and the CAS protocol is new code with new failure modes.

My read: C is a good answer to a question Ted is not asking. Usually one machine pulls, from cron, into a Dropbox folder, and the archive's value is that it is plain files. Keep C as something the ports allow later, not something to build now.

## 7. Hybrids: which LSM ideas earn their place in A

1. **Unique run ids** (host plus random). Adopt. The CRDT notes show second-resolution ids collide, and SlateDB RFC 0026 is the cautionary tale about reusing identifiers a sweep may have touched. Cheap.
2. **A `run_start` line.** Adopt. It is the minimal "manifest knows what is in flight" and it makes `slarc list` able to show interrupted runs. Cheap.
3. **A dedicated lock file.** Adopt, with a placement decision (section 8).
4. **A snapshot registry with leases** (SlateDB checkpoints) instead of flock. Decline on one machine: flock is released by the kernel on death and needs no expiry tuning; a lease needs a lifetime, a refresh loop and a rule for stale leases. Across Dropbox a lease is not visible promptly, so it buys nothing there either.
5. **Per-run or per-host segment files.** Decline per-run (file explosion, dead segments stay greppable). Per-host suffixes on appended files are worth considering only if the second machine turns out to be used regularly; see section 8.
6. **A manifest listing live files.** Decline; `runs.jsonl` plus the directory listing is that manifest, and it is already append-only and per-run.
7. **Tombstones instead of rewriting.** Decline. `rm` exists to delete bytes, for privacy and for sharing. A tombstone that leaves the bytes in place until a later compaction defeats that. Today's temp-and-rename rewrite already is compaction at file granularity.
8. **Treat a missing blob as not downloaded** (item 5 of the direction). Adopt; it is the same rule RocksDB applies after a restart: trust what is on disk, not what you remember writing.

## 8. Two things I would change in the direction

**Put the lock outside the archive.** The lock is host-local by construction, so it does not need to be in a synced folder at all. `~/.cache/slarc/locks/<sha256 of the archive's realpath>.lock` needs no Dropbox ignore attribute, cannot be replaced by a sync, and sidesteps the open question of whether flock behaves under `~/Library/CloudStorage` (the CRDT notes found no Apple source on it, and neither did I) **[inference]**. The ignore attribute in the archive works too, but it depends on File Provider versus classic Dropbox naming, and an ignored file that someone copies or un-ignores by accident starts syncing. If Ted prefers the lock inside the archive for discoverability, test flock there first.

**Make conflicted copies harmless by reading them.** Readers of `runs.jsonl` should glob `runs*.jsonl` (and `directory/<team>/users*.jsonl` and friends) the way month files already glob `*.jsonl`. The fold is order-independent and duplicate-tolerant, so a conflicted copy becomes a union rather than a silent loss. This needs one test against Dropbox's real naming to confirm the extension survives. With that in place, the second-machine story becomes: pulls on two hosts are safe and converge; a sweep on host A while host B appends to the same file may resurrect deleted lines in B's conflicted copy, and the next sweep on A deletes them again, so the archive converges with no gap in a tracked item but with a window where removed bytes are back on disk **[inference]**. That window is the cost of not having a cross-host lock, and it is acceptable for a rare manual pull.

Then pick one sweep policy for the multi-host case, from cheapest up: sweep only on one named host (a `sweep_host` in `slarc.toml` is the simplest statement of "usually one machine pulls"), or refuse to sweep while `runs*.jsonl` shows another host with a `run_start` younger than some window and no run line, or the acknowledgement scheme in the CRDT notes. I would take the first and document it.

## 9. Recommendation

Build option A with the adjustments above. The design is already an LSM in shape: append log, newest-wins fold, tombstone lines, temp-and-rename compaction, blob GC. What it lacked was the one thing every LSM has and the explainer correctly identified: a record of live readers so deletion respects them. flock is the right local primitive for that, cheaper and stricter than a lease. Tags are the right rule for rm-beats-stale-pull, and no sequence number or clock does better.

Options B and C both move the design toward multi-writer coordination that slarc does not need on one machine and cannot get on Dropbox (no CAS, no prompt visibility). B costs browsability and file count for a gain that only matters under concurrent writers; C costs the plain-folder experience and adds a protocol to maintain. The ports keep C open if the second-machine case ever becomes central.

Cost of A: days, not weeks, and no change to the on-disk format readers see. Cost of switching to B or C: a storage-layer rewrite touching the store adapters, the DuckDB index, export, list and ADR 0001.

**Decisions for Ted.**
1. Lock location: `~/.cache/slarc` keyed by realpath (my preference), or inside the archive with the Dropbox ignore attribute, after testing flock under File Provider.
2. Second machine: name a sweep host in `slarc.toml`, or refuse to sweep when another host has an unfinished recent run. I would name a host.
3. Whether readers glob conflicted copies of `runs.jsonl` and directory files, after one test of Dropbox's naming.
4. Whether to record an ADR saying the object-storage backend is out of scope for now and why, so the question does not reopen each time the presentations come to mind.

## 10. Sources

- RocksDB wiki, *MANIFEST*: https://github.com/facebook/rocksdb/wiki/MANIFEST
- RocksDB wiki, *Delete Stale Files*: https://github.com/facebook/rocksdb/wiki/Delete-Stale-Files
- RocksDB wiki, *Snapshot*: https://github.com/facebook/rocksdb/wiki/Snapshot
- RocksDB wiki, *Compaction* (compaction styles; says nothing about snapshots or tombstones): https://github.com/facebook/rocksdb/wiki/Compaction
- RocksDB source, `db/db_impl/db_impl_files.cc` (`FindObsoleteFiles`, `PurgeObsoleteFiles`, `MinObsoleteSstNumberToKeep`): https://github.com/facebook/rocksdb/blob/main/db/db_impl/db_impl_files.cc
- RocksDB source, `db/compaction/compaction_iterator.cc` (snapshot stripes, tombstone dropping): https://github.com/facebook/rocksdb/blob/main/db/compaction/compaction_iterator.cc
- SlateDB RFC 0001, *Manifest*: https://github.com/slatedb/slatedb/blob/main/rfcs/0001-manifest.md
- SlateDB RFC 0004, *Checkpoints*: https://github.com/slatedb/slatedb/blob/main/rfcs/0004-checkpoints.md
- SlateDB RFC 0026, *Garbage collector boundary*: https://github.com/slatedb/slatedb/blob/main/rfcs/0026-garbage-collector-boundary.md
- Amazon S3, *How to prevent object overwrites with conditional writes*: https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
- Dropbox Help, *What's a conflicted copy?*: https://help.dropbox.com/organize/conflicted-copy
- Dropbox Help, *Ignored files*: https://help.dropbox.com/sync/ignored-files
- slarc: `docs/adr/0001-append-only-jsonl-storage.md`, `docs/architecture.md`, `docs/specs/2026-10-19-rm-design.md`, `docs/research/2026-10-20-crdt-tracked-items.md`, `.llm/runs-and-concurrency.html`, `src/slarc/archive_state.py`, `src/slarc/adapters/jsonl_store.py`, `src/slarc/service/remove.py`, `src/slarc/adapters/duckdb_index.py`
- Ted Naleid, *The Rise of Object Store Architectures* and *Understanding RocksDB* (presentations)

Not verified in this note: flock behaviour under macOS File Provider; whether Dropbox keeps the `.jsonl` extension on a conflicted copy; DuckDB's httpfs reading JSONL from S3; the slatedb.io docs site (404 at the time of writing, so SlateDB facts come from the RFCs in the repository).
