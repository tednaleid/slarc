# Tracked items as an observed-remove set: what the literature says

Research notes for the pull/rm race, the sweep, and running from a synced folder. Written 2026-10-20 against commit `ac21cc1`.

Labels: **[source]** a cited primary source says it; **[code]** read from the slarc tree; **[inference]** my own reasoning.

## 1. Summary

**Q1, update vs remove.** Every published map design I checked lets an update that is concurrent with a remove survive it: Riak's `riak_dt_map`, the delta-state ORMap, the Kleppmann and Beresford JSON CRDT, and Preguiça's "remove-as-recursive-reset" and "update-wins" maps. Under those rules a stale pull keeps `#general` tracked, and that is today's bug. Remove-wins sets and maps make a remove beat concurrent updates. They also make it beat a concurrent fresh add of the same key, which the slarc rule forbids.

The slarc rule (a stale update to an observed tag loses, a fresh tag survives) is an OR-Set (Shapiro et al., Spec. 15) whose tag level is a 2P-Set of unique tags (Spec. 12, 13), with a payload per tag that may only be updated while its tag is live. Put another way, it is Preguiça's remove-wins map keyed by the unique tag instead of the item key. I found no paper that names and analyzes exactly this composition. Its parts are analyzed, and a product of join-semilattices is a join-semilattice (Almeida et al. §7.1), so it converges.

**Q2, causal stability.** An operation is causally stable once no operation concurrent with it can still be delivered (Baquero et al. Def. 5.1; Shapiro et al. Def. 4.1). Only then may removed data be discarded. On one machine, holding an exclusive flock acquired after the rm line is durable is a correct local test, with three conditions:
- every run takes its shared lock before it reads the fold;
- the sweep re-reads state while holding the exclusive lock;
- the lock file is one that nothing ever replaces.

Across machines the test does not hold. "Another host has a recent unfinished run" does not establish stability. The literature requires an acknowledgement from every known replica, and a deletion in a synced folder propagates to every host.

**Q6, flock.** A flock lock belongs to an open file description. It is released by an explicit unlock or when the last descriptor sharing it closes, including at process exit. Converting shared to exclusive is not atomic on Linux or macOS. POSIX `fcntl` record locks, which Python's `fcntl.lockf` uses, are dropped when the process closes *any* descriptor for the file. slarc opens and closes `runs.jsonl` on every read, so only `fcntl.flock` is usable.

`O_APPEND` makes "seek to end, then write" one atomic step, and POSIX makes each `write()` atomic with respect to other writes. Writes can be partial, though, and Python's buffered files do not promise one `write(2)` per line. I found no primary source on APFS specifics.

**Does the design direction hold?** Yes on one machine, with corrections:
- tags must be unique, and run ids today are not;
- lock before reading, and lock a dedicated file;
- download states have the same race;
- a held lock shows that *some* run is live, not *which* one;
- the multi-host plan needs a real stability test (or a refusal to sweep) and per-host files.

## 2. Q1: update vs remove in established designs

| Design | Concurrent update or add vs remove of the same entry | Source |
|---|---|---|
| LWW-element-Set | Higher timestamp wins | Shapiro et al. §3.3.3 |
| 2P-Set / U-Set | Remove wins; a removed element is never re-added | Shapiro et al. §3.3.2, Spec. 12-13 |
| OR-Set | Add wins; remove deletes only the tags it observed | Shapiro et al. §3.3.5, Spec. 15 |
| Optimized OR-Set | Same semantics, version vectors in place of tombstones | Bieniusa et al. §4.2 |
| Remove-wins set | Remove wins over a concurrent add | Almeida et al. Fig. 16 |
| OR-Cart | Each add is a new unique triple, so it survives a remove | Shapiro et al. §5.1, Spec. 21 |
| `riak_dt_map` | Field stays with only the concurrent update ("reset-remove") | `riak_dt_map.erl` l. 53-93 |
| delta ORMap | Concurrent update's new dots survive the remove | Almeida et al. Fig. 17 |
| JSON CRDT | Update resurrects the removed element | Kleppmann, Beresford, Fig. 6 |
| Remove-as-reset map | Like Riak | Preguiça §2.1.5, Fig. 3 |
| Remove-wins map | Remove cancels prior and concurrent updates | Preguiça §2.1.5, Fig. 4 |
| Update-wins map | Update cancels a concurrent remove | Preguiça §2.1.5, Fig. 5-6 |

**[source]** Riak's source says: "if a field is removed, and concurrently that same field is updated, the field is _in_ the Map (only observed updates are removed) ... only the concurrent update survives." For embedded counters, "the counter remains, with the updated value." The Riak docs agree: "If a field is concurrently added or updated and removed, the add/update will win." **[inference]** A slarc pull writes a full `TrackedItem` snapshot, so under Riak semantics the stale snapshot would survive whole, cursors included. That is exactly the gap.

**[source]** Kleppmann and Beresford call this a known surprise. A to-do item deleted on one replica and marked done on another is "resurrected," and "it may be more desirable to discard one of the concurrent updates." Preguiça's remove-wins map keeps only updates `upd(k, op)` such that every `rmv(k')` on a prefix of `k` happened before them. Keyed by item key, that would also cancel a re-track that runs concurrently with an rm.

**[source]** Shapiro's OR-Set gives half (b) of the slarc rule: "the unique tag generated by add cannot be observed by remove." It is built from unique `(e, u)` pairs, each removed only by a remove that observed it (Spec. 15, lines 7-16). At the level of one pair that is the U-Set rule: "a removed element will never be added again" (§3.3.2).

**[inference]** Attach a payload to each pair, require every update to name its pair, and drop updates to removed pairs. That gives half (a). Because each tag is created exactly once, Preguiça's remove-wins map with key = tag gives the same result. The OR-Cart is the nearest published neighbour, but its updates create new triples, so they survive removes.

**[inference] Convergence.** The state has three parts:
- a grow-only set of created tags, each with its item key;
- a grow-only set of removed tags;
- one register per tag.

An item is tracked if it has a tag that is created and not removed. Each part is a join-semilattice, and so is their product (Almeida et al. §7.1 "Pair"; their 2P-Set in Fig. 6 is `P(E) × P(E)` with `elements = s \ t`). The remove set only grows, so the fold gives the same answer whatever order the lines are in.

**Pitfalls**
- **Updates that implicitly re-add.** This is the shared failure of the Riak, ORMap and JSON designs. A pull's snapshot must only update the tag it names. Only an explicit "began tracking" event may create a tag.
- **Tag uniqueness.** `unique()` must return a unique value, such as a Lamport clock plus replica id, or a random value from a large space (Spec. 15 line 7; §3.3.2 footnote 5). **[code]** `run_id()` is `"r_" + strftime("%Y%m%dT%H%M%SZ")`. It collides for two runs in the same second (cron plus a manual pull, or two hosts).
- **Two live tags for one key.** Two concurrent first pulls create two tags, and a later rm removes both. The fold needs a rule to read one item from two payloads. **[inference]** The cursors can merge as a lattice: max `newest_ts`, min `oldest_ts`, OR of `backfill_complete`.
- **Download states.** **[code]** `downloads()` is also latest-run-line-wins per target. A stale pull's `pending` state therefore undoes rm's `removed`, which is the spec's anti-goal of re-downloading files rm deleted.

## 3. Q2: causal stability and garbage collection

The DAIS 2014 paper is titled "Making Operation-Based CRDTs *Operation-Based*". I could not retrieve its full text. The definitions below come from its extended arXiv version, "Pure Operation-Based Replicated Data Types".

**[source]**
- **Baquero et al., Def. 5.1:** "A timestamp τ ... is causally stable at node i when all messages subsequently delivered at i will have timestamp t > τ," so no concurrent message can still arrive. Their oracle (§5.2) declares τ stable once node i has delivered a later message from every other node in a known set `I`.
- **Shapiro et al., Def. 4.1:** update f is stable when "all updates concurrent to f ... are already delivered." Only then does state kept for concurrent operations serve "no useful purpose." Liveness "requires that the set of replicas be known and that they not crash permanently (undetectably)" (§4.1).
- **Bieniusa et al. §4.2:** Wuu's tombstone GC means "waiting for acknowledgements from each process to every other process" and "requires all processes to be correct."

**[inference] Mapping to one machine.** The operations concurrent with an rm are the runs that read the fold before the rm line existed. On one host every process sees appended bytes at once, so such a run's operations are "delivered" once the run has ended. The design meets the stability condition if:
1. pulls take `LOCK_SH` before reading `runs.jsonl`;
2. the rm line is fsynced before the exclusive attempt (`append_run` already does this);
3. the sweep recomputes coverage under `LOCK_EX`.

Then holding `LOCK_EX` proves that every concurrent run has ended.

Edge cases:
- A run that locked after the rm line reads the removal.
- A run that locked before it blocks the sweep, whenever it reads.
- A crashed run's lines cannot appear later on the same host. What it wrote is visible when it dies, and the kernel drops its lock.

The only hole is a run that reads before it locks.

**[inference] What stability protects.** Tags alone defeat the stale run line. Stability matters for deletions based on non-reference. Example: a pull of a *still tracked* `#random` writes a blob, then its directory line. A sweep in between deletes the blob and leaves a dangling reference. A stale pull's late message lines are harmless orphans that the next sweep removes.

**[source]** Automerge compaction takes the same care: each process "only deletes data which is incorporated into the compacted document" and tracks what it has loaded.

**[inference] Across hosts.** Another host's lines arrive whenever Dropbox delivers them, perhaps days later. Stability then requires that every known host has shown it saw the removal and has no older unfinished run. A recency heuristic cannot see a host whose `run_start` has not synced yet. Dropbox also syncs deletions, so a sweep on one host deletes blobs on all of them.

## 4. Q3: the CALM theorem

**[source]**
- **Statement.** Hellerstein and Alvaro, §1.4. Definition 1: "A program P is monotonic if for any input sets S, T where S ⊆ T, P(S) ⊆ P(T)." Theorem 1: "A program has a consistent, coordination-free distributed implementation if and only if it is monotonic."
- **Formal result.** Ameloot, Neven and Van den Bussche prove that "the class of monotone queries is captured by coordination-free transducer networks."
- **Garbage collection** is the paper's non-monotone example: a machine "must ensure that it has heard everything there is to hear" (§1.3.2).
- **Deletes as a growing set.** Recording deletes as "another monotonically growing set" makes adds and deletes commute, but an operation that acts on the result (checkout) still needs coordination (§2.1.1).
- **Membership.** Monotone programs are exactly those that never query network membership (§2.2).

**[inference]** "Pull only adds; rm retracts" is a fair heuristic once refined. The theorem covers transducer networks, so applying it to processes sharing files is an analogy.
- **Recording a removal is monotone.** It adds a fact (tag removed) to a grow-only set, so it needs no coordination.
- **Acting on a negation is not.** "Created minus removed" and "referenced by nothing" are negations. Acting on them irreversibly (the sweep) is what needs coordination, as in the GC example.
- **Pull's negations are benign.** Pull's reads also contain negations ("no blob yet, download"). Getting those wrong only repeats work, which is why pull/pull overlap is safe.

## 5. Q4: timestamps

**[source]**
- **Lamport (1978), Clock Condition:** if a → b then C(a) < C(b), and "we cannot expect the converse condition to hold."
- **Shapiro et al.:** an LWW-Register assumes timestamps "consistent with causal order" (§3.2.1). With LWW, outcomes "depend on opaque details of how timestamps are allocated" (§3.3.5).
- **Almeida et al. (§7.2):** a failure to keep timestamps monotonic "is a common source of errors in timestamp based systems."
- **Kulkarni et al. (§1, §2):** physical time has "leap seconds and non-monotonic updates to POSIX time" that "may cause the timestamps to go backwards," and `e hb f ⇒ pt.e < pt.f` does not hold. HLC guarantees `e hb f ⇒ l.e < l.f` while staying close to physical time.
- **Python:** `time.time()` "can return a lower value than a previous call if the system clock has been set back."

**[inference]**
- **No clock fixes the race.** The stale run line is concurrent with the rm. Wall clocks, Lamport clocks and HLCs all order concurrent events arbitrarily. Only the observed tag says which state the pull saw.
- **One machine.** **[code]** `version_key` is `(fetched_at, run)` at one-second resolution, and a run line's `fetched_at` is its finish time. That is adequate for update-vs-update on a live tag, where the worst case is a cursor stepping back and some data being re-read.
- **Two machines.** Clock skew decides: a fast clock wins every conflict while the skew lasts. Equal keys from two hosts fall back to file order, which may differ per host.
- **Fixes.** Add a host and random component to run ids. An HLC (updated when a host reads the other's lines) is the documented technique if a cross-host order is ever needed.

## 6. Q5: append-only logs in a synced folder

**[source]**
- **Dropbox:** "A conflicted copy is a file that Dropbox creates when multiple people edit the same file at the same time," including offline edits. Resolution is to "compare both versions and merge them manually."
- **Kleppmann et al. (§3.1.3):** file sync's weakest point is that "if the same file is edited on two different devices, the result is a conflict that needs to be merged manually." After trying "copying files around with Dropbox and USB keys," they "settled on the Hypercore peer-to-peer libraries from Dat" (§4.2.4).
- **Hypercore** is "a secure, distributed append-only log" signed by a key pair.
- **Autobase:** "writers append causal nodes to local cores, Autobase linearizes that graph into an eventually consistent order." It may "reorder previously seen nodes," so `apply` must be deterministic.
- **Scuttlebutt:** a feed is "a list of all the messages posted by a particular identity," an append-only, hash-linked, signed log.
- **automerge-repo** keys each change by content hash, so overwrites are harmless, and it deletes only data it has loaded.

**[inference] Is one file per writer standard?** In local-first peer-to-peer systems, one log per writer merged by a deterministic fold is the common design (Hypercore and Autobase, Scuttlebutt). I found no primary source that prescribes it for Dropbox. Dropbox's documentation does imply that two hosts appending to one file produce conflicted copies. slarc's fold already ignores file order, so a union of per-host files folds correctly.

The costs:
- more files, which readers must glob;
- ids that are unique across hosts;
- sweeps that either rewrite only their own host's files or conflict with the other host's appends;
- blob GC that stays global, because blobs are shared by content;
- a known host set, which is the membership knowledge that CALM ties to non-monotone operations.

## 7. Q6: flock, fcntl locks and O_APPEND

**[source] Linux flock(2)**
- "Locks created by flock() are associated with an open file description." They are released "by an explicit LOCK_UN operation on any of these duplicate file descriptors, or when all such file descriptors have been closed."
- Separate `open()` calls "are treated independently by flock()."
- `LOCK_NB` turns a conflicting request into `EWOULDBLOCK`.
- "Converting a lock (shared to exclusive, or vice versa) is not guaranteed to be atomic": the old lock is removed first.
- "flock() places advisory locks only."

**[source] macOS flock(2), close(2), fcntl(2)**
- Upgrading "results in the previous lock being released and the new lock applied (possibly after other processes have gained and released the lock)."
- NOTES: "Locks are on files, not file descriptors," and `dup` or `fork` copies share one lock. **[inference]** "Files" here means the open-file entry, as on Linux, but the page does not say so outright.
- `close(2)`: "on the last close of a file holding an advisory lock the lock is released."
- `fcntl(2)`: "All locks associated with a file for a given process are removed when the process terminates."

**[source] POSIX record locks**
- Linux `fcntl_locking(2)`: "If a process closes any file descriptor referring to a file, then all of the process's locks on that file are released." Record locks are not inherited across `fork`.
- macOS `fcntl(2)` states the same POSIX rule and calls flock's "last close semantics" "much more rational."
- Python's `fcntl.lockf` "is essentially a wrapper around the fcntl() locking calls." `fcntl.flock` calls flock(2), though "on some systems" it is emulated with fcntl.

**[source] O_APPEND and atomicity**
- POSIX `write()`: with `O_APPEND` "the file offset shall be set to the end of the file prior to each write and no intervening file modification operation shall occur between changing the file offset and the write operation."
- POSIX Rationale: concurrent writes to a regular file are unspecified "except that each write is atomic (see 2.9.7 ...). Applications should use some form of concurrency control." §2.9.7 lists `write()` among calls where "each call shall either see all of the specified effects of the other call, or none of them." The text says threads. Linux `write(2)` BUGS reads it as covering "threads (and processes)" and says Linux fixed a violation in 3.14.
- A write can be partial: if a signal interrupts it after some data, or if there is less room than requested.
- Linux `open(2)`: "O_APPEND may lead to corrupted files on NFS filesystems."
- Python `BufferedWriter` flushes "when the buffer gets too small for all pending data," on `flush()`, `seek()` or close.

**APFS and File Provider.** I found no Apple primary source on `O_APPEND` or flock specific to APFS (the macOS `fcntl(2)` page names APFS only for `F_FULLFSYNC` and `F_BARRIERFSYNC`). I also found nothing on flock under `~/Library/CloudStorage`.

**[code], [inference]** `_handle` opens files with `path.open("a+b")`, which is buffered and uses `O_APPEND`. A long line can straddle two `write(2)` calls, so two pulls appending to the same month file can interleave mid-line. The reader would skip the torn line.

## 8. Implications for slarc

The section numbers in brackets point to the findings each recommendation rests on. The recommendations themselves are my inferences.

1. **Adopt tags.** An OR-Set of unique tags, with a 2P remove set and a payload per tag, converges and gives both halves of the rule [2, 4]. Keep removed tags forever. `runs.jsonl` is small and append-only, so no tombstone GC is needed.
2. **Never let an update create a tag.** The fold drops snapshots whose tag is unknown or removed [2]. Test the race directly: a pull reads tag T, rm removes T, the pull's run line appears later in the file, and T stays removed.
3. **Make tags and run ids unique.** Use host plus a random value or sub-second time [2; Spec. 15].
4. **Give download states the same treatment.** Tie them to tags, or derive "removed," so a stale `pending` cannot requeue what rm removed [2].
5. **Lock, then read.** A pull holds `LOCK_SH` from before its first read until after its run line is fsynced. The sweep takes `LOCK_EX` and then recomputes coverage [3].
6. **Use `fcntl.flock` on a dedicated lock file.** `_read` closes `runs.jsonl` constantly and would drop a `lockf` lock [7]. A dedicated file avoids depending on whether Dropbox ever replaces `runs.jsonl` with a new inode, which I could not verify. The lock is host-local, so the file can carry Dropbox's documented File Provider ignore attribute (`com.apple.fileprovider.ignore#P`). Test flock under `~/Library/CloudStorage` first.
7. **Do not upgrade a held lock.** Upgrades are not atomic [7]. A finishing pull should fsync its run line, close the shared lock, then try `LOCK_EX | LOCK_NB` on a fresh descriptor. **[inference]** Of several finishing pulls, the last to release succeeds unless a new run has started, and that run tries when it ends. A killed process leaves the sweep to the next command.
8. **Label runs individually.** Acquiring `LOCK_EX` proves every unfinished `run_start` is dead. Being refused proves only that some run is live. To label individual runs, use per-run lock files, or pid plus process start time (pids are reused).
9. **Prefer positive deletion criteria.** Delete lines in channels the removed tags covered, minus current coverage, in the spirit of Automerge's rule. Blob deletion is inherently a negation and must run under `LOCK_EX` [3, 4].
10. **Append whole lines per write, or accept torn lines.** One `os.write` per line or per batch of whole lines, with the count checked, avoids interleaving. Otherwise a torn line is silently dropped and later re-fetched [7].
11. **Multiple machines.** "Skip when another host has a recent unfinished run" is not stability [3, 5]. Options, simplest first:
    - (a) refuse to sweep, with a warning, once `runs.jsonl` shows more than one host;
    - (b) acknowledgement-based stability, where each `run_start` records the removals its host has seen;
    - (c) per-host files, where each host sweeps only its own files and blob GC uses (b).

    Two hosts appending to one file also risk conflicted copies that slarc never reads [6].
12. **Clocks.** Keep wall-clock LWW only for update-vs-update on a live tag, and never use a clock to decide remove vs update [5].

## 9. Sources

- Shapiro, Preguiça, Baquero, Zawirski. *A comprehensive study of Convergent and Commutative Replicated Data Types.* Inria RR-7506, 2011. https://inria.hal.science/inria-00555588 (HAL blocked automated download; read from a copy of the HAL PDF at https://www.csa.iisc.ac.in/~raghavan/CleanedPods2021/crdt-shapiro-2011-techreport.pdf)
- Bieniusa, Zawirski, Preguiça, Shapiro, Baquero, Balegas, Duarte. *An Optimized Conflict-free Replicated Set.* Inria RR-8083, 2012. https://arxiv.org/abs/1210.3368
- Almeida, Shoker, Baquero. *Delta State Replicated Data Types.* arXiv 2016; JPDC 2018. https://arxiv.org/abs/1603.01529
- Baquero, Almeida, Shoker. *Making Operation-Based CRDTs Operation-Based.* DAIS 2014. https://doi.org/10.1007/978-3-662-43352-2_11 (full text not retrieved)
- Baquero, Almeida, Shoker. *Pure Operation-Based Replicated Data Types.* arXiv 2017. https://arxiv.org/abs/1710.04469
- Preguiça. *Conflict-free Replicated Data Types: An Overview.* arXiv 2018. https://arxiv.org/abs/1806.10254
- Kleppmann, Beresford. *A Conflict-Free Replicated JSON Datatype.* IEEE TPDS 2017. https://arxiv.org/abs/1608.03960
- Basho. `riak_dt_map.erl`, branch `develop`, commit c2a3e1b. https://github.com/basho/riak_dt/blob/develop/src/riak_dt_map.erl ; Riak docs, *Data Types*. https://docs.riak.com/riak/kv/latest/learn/concepts/crdts/index.html
- Hellerstein, Alvaro. *Keeping CALM: When Distributed Consistency Is Easy.* CACM 2020. https://arxiv.org/abs/1901.01930
- Ameloot, Neven, Van den Bussche. *Relational Transducers for Declarative Networking.* PODS 2011; JACM 2013. https://arxiv.org/abs/1012.2858
- Lamport. *Time, Clocks, and the Ordering of Events in a Distributed System.* CACM 1978. https://lamport.azurewebsites.net/pubs/time-clocks.pdf
- Kulkarni, Demirbas, Madeppa, Avva, Leone. *Logical Physical Clocks and Consistent Snapshots in Globally Distributed Databases.* OPODIS 2014. https://cse.buffalo.edu/tech-reports/2014-04.pdf
- Kleppmann, Wiggins, van Hardenberg, McGranaghan. *Local-First Software: You Own Your Data, in spite of the Cloud.* Onward! 2019. https://www.inkandswitch.com/local-first/static/local-first.pdf
- Hypercore and Autobase docs. https://docs.pears.com/building-blocks/hypercore , https://docs.pears.com/building-blocks/autobase
- *Scuttlebutt Protocol Guide*, Feeds. https://ssbc.github.io/scuttlebutt-protocol-guide/
- Automerge, *Storage*. https://automerge.org/docs/reference/under-the-hood/storage/
- Dropbox Help: *What's a conflicted copy?* https://help.dropbox.com/organize/conflicted-copy ; *Ignored files* https://help.dropbox.com/sync/ignored-files ; *Dropbox on File Provider* https://help.dropbox.com/installs/macos-support-for-expected-changes
- Linux man-pages: flock(2) https://man7.org/linux/man-pages/man2/flock.2.html ; fcntl_locking(2) https://man7.org/linux/man-pages/man2/fcntl_locking.2.html ; write(2) https://man7.org/linux/man-pages/man2/write.2.html ; open(2) https://man7.org/linux/man-pages/man2/open.2.html
- macOS man pages flock(2), fcntl(2), close(2), open(2), write(2), read locally on Darwin 25.6.
- POSIX.1-2024: `write()` https://pubs.opengroup.org/onlinepubs/9799919799/functions/write.html ; XSH §2.9.7 https://pubs.opengroup.org/onlinepubs/9799919799/functions/V2_chap02.html
- Python docs: `io` https://docs.python.org/3/library/io.html ; `fcntl` https://docs.python.org/3/library/fcntl.html ; `time` https://docs.python.org/3/library/time.html
