# Distributed Nano-DB — Interview Prep

This document is structured around the questions you will actually be asked. Not by component, by question. Each section answers a specific interviewer prompt with the level of detail that distinguishes "I used a library" from "I built it."

---

## 1. "Walk me through the system end-to-end"

**The answer to have ready:**

A client sends `POST /vectors` with a JSON body containing an ID, a 128-dimensional float vector, and optional metadata. The coordinator receives it, hashes the ID with a double-applied FNV-1a hash to get a well-mixed 64-bit key, looks up the key on the consistent hash ring, and finds the target shard ID. It then sends an `Insert` gRPC to every replica of that shard simultaneously via `std::async`. It waits for all futures. If the primary acknowledged AND at least ⌈N/2⌉ total replicas acknowledged (quorum), it responds HTTP 201. Otherwise 502.

For search: `POST /search` with a vector and k. The coordinator fires a `Search` gRPC to one target per shard (strong consistency → primary, eventual → any replica) in parallel. Each shard returns its local top-k. The coordinator merges all local result lists (sort by distance, deduplicate by ID, take global top-k) and responds. If a shard is unreachable, it's omitted and the response includes `"degraded": true`.

---

## 2. "How does your storage engine work?"

**The question behind the question:** Do you understand mmap, or did you just call `fopen`?

**mmap over fread.** Traditional `fread` does a kernel-to-user memcpy on every read — the kernel copies from its page cache into your buffer. `mmap` creates a mapping in the process's virtual address space that points directly at the kernel's page cache. No copy. After the initial page fault (first touch of a 4KB page), subsequent reads hit the CPU cache directly.

**Demand paging.** `mmap` doesn't load the file into RAM. It marks pages "not present" in the page table. First access → page fault → kernel loads 4KB from disk → page table updated → execution resumes. This means a 10GB file can be mapped on a 2GB machine; the OS evicts cold pages using LRU.

**Zero deserialization.** The `Node` struct is POD — no pointers, no vtable, no heap allocations. It's stored on disk exactly as it lives in memory. On startup, `mmap` the file, cast the base pointer to `FileHeader*`, read `element_count` and `entry_point_id`. Done. There is no parse step, no fixup pass.

**Offset addressing.** You can't store `Node*` pointers on disk — the OS maps the file at a different virtual address on every run. Instead, nodes reference each other by `id_t` (uint32_t). To follow a reference: `get_node(id) = (Node*)((char*)base + HEADER_SIZE + id * sizeof(Node))`. This arithmetic produces the correct byte offset regardless of where the OS placed the mapping.

**Resize.** When an insert would place a node past the file boundary, `MMapHandler::resize(new_size)` unmaps the current view, extends the file with `ftruncate`, and remaps. This invalidates every `Node*` derived from the old mapping. A `global_resize_lock_` (shared_mutex) blocks all readers during the window.

---

## 3. "How does HNSW work? Why approximate?"

**Core idea.** HNSW is a multi-layer proximity graph. Layer 0 has all nodes, maximum 32 neighbors each. Upper layers are exponentially sparser — a node at layer k also exists at layer k-1. Search enters at the top layer, greedily navigates to the closest neighbor at that layer, drops to the next layer, repeats until layer 0. At layer 0, a bounded-search greedy traversal returns the `ef_search` closest candidates, from which the top-k are returned.

**Why approximate?** Exact search is O(N × D) per query — you must compute distance to every vector. At 10M vectors of dimension 128, that's 1.28 billion floating point operations per query. HNSW is O(log N × D) amortized — the number of nodes examined grows logarithmically with dataset size. The cost is recall < 100%: HNSW can miss the true nearest neighbor by following a locally optimal but globally suboptimal path. At ef_search = 200 for k=10, recall@10 is around 95% on standard benchmarks.

**Insert path.** Draw a random layer from a geometric distribution (probability of being at layer k is (1/e)^k — same as Poisson process). From the top layer, greedily descend to find the insertion neighborhood. For each layer ≥ new node's max layer: update the node's neighbor list using a heuristic that prunes neighbors whose mutual distances are short (the "diverse neighborhood" heuristic — keeps neighbors spread out, not clustered). Update neighbor links bidirectionally.

**Deletion.** Tombstone only: `is_deleted = true` in the Node struct. The deleted node stays in the graph — its neighbors still link to it, search still traverses through it. Results are filtered post-search (deleted nodes excluded from the output). This is lazy deletion. Why not clean up immediately: removing a node requires relinking all its neighbors to bypass it, which requires graph traversal, neighbor set recomputation, and lock contention during the relink. Too expensive for a hot write path.

---

## 4. "How does consistent hashing work? What bug did you find?"

**The modulo problem.** Without consistent hashing, `shard = hash(id) % N`. When you go from N=3 to N=4 shards, every key rehashes. The expected fraction that moves is `(N-1)/N = 75%`. For a database with millions of vectors, 75% migration on a shard addition is catastrophic — you'd spend hours moving data with searches degraded the whole time.

**The ring.** In consistent hashing, both keys and shard nodes are placed on a ring (0 to 2^64). A key routes to the first shard node encountered clockwise. Adding a new shard inserts a new point on the ring; only the keys between it and its predecessor need to move. Expected migration: 1/N of all keys — for 3→4 shards, ~25%.

**Virtual nodes.** Without virtual nodes, 3 physical shards get 3 points on the ring. Depending on where FNV-1a hashes them, the ring could be wildly uneven — one shard gets 70% of the traffic, another gets 5%. Virtual nodes place 200 points per shard, spread around the ring, making the distribution approximately uniform. Empirical result with this implementation: ≤6% load imbalance across 3–10 shards.

**Bug 1: virtual node clustering.** The naive approach: `hash("shard-0#0")`, `hash("shard-0#1")`, etc. FNV-1a processes bytes sequentially with minimal avalanche — strings that differ only in the last character produce nearly identical hash values. The 200 virtual node points for shard 0 cluster in a small band on the ring, defeating the entire purpose. Fix: chained rehash — start with `h = fnv1a_64(shard_id_string)`, then for each virtual node, `h = fnv1a_64(h)`. This produces a chain of well-distributed points. Measured result: 24.3% of keys migrate on 3→4 shards vs 25% theoretical minimum.

**Bug 2: sequential ID clustering in routing.** After fixing virtual nodes, the ring was correct but routing was still uneven. Sequential IDs — "user-1", "user-2", "doc-1", "doc-2" — are nearly identical byte strings. FNV-1a hashed them to nearly identical 64-bit values. All sequential IDs would cluster to the same shard. Fix: `well_mixed_hash(id) = fnv1a_64(fnv1a_64(id))` — applying FNV-1a twice fully mixes the bits. This is a standard double-hashing technique.

**Rebalancing protocol.** Adding a shard: pause writes (coordinator returns 503), commit the new shard via Raft (`add_shard` command), apply the command (coordinator creates gRPC stubs to the new shard), list every key on each existing shard (via `ListLocalIds` RPC), migrate keys that hash to the new shard (GetVector → Insert on destination → Delete on source), resume writes. Insert-before-delete is deliberate: a key briefly exists on two shards (handled by deduplication in search merge), but it never vanishes. Delete-before-insert means a key is briefly invisible — unrecoverable if the coordinator crashes mid-migration.

---

## 5. "Explain Raft. Why not Paxos? Walk me through an election."

**Why Raft exists.** Multi-Paxos has no single canonical specification. Different implementations make different choices about leader election, log management, and membership changes. Raft was designed with understandability as an explicit goal — it's a complete specification, not a pattern. This matters when building it from scratch: there's a definitive answer to "what should happen here?" that doesn't require reading 12 academic papers.

**The strong leader model.** In Raft, all writes go through the leader. The leader decides the log order — followers never propose. This is different from classic Paxos where any acceptor can propose. The strong leader model makes log replication simple: leader appends to its log, sends `AppendEntries` to all followers, majority acks → commit, advance `commit_index`, notify followers in the next heartbeat.

**Election walkthrough.**
1. Follower's election timer fires (random 300–600ms — randomized to avoid split votes where everyone times out at once and no one wins).
2. Node increments its term, votes for itself, transitions to Candidate, sends `RequestVote(term, last_log_index, last_log_term)` to all peers. Each RPC has a 100ms deadline.
3. A peer grants the vote if: it hasn't voted for anyone in this term AND the candidate's log is at least as up-to-date (`candidate_last_term > voter_last_term`, OR same term and `candidate_last_index >= voter_last_index`). This log-currency check is critical — without it, a stale node with an old log could become leader and overwrite newer committed entries.
4. Candidate receives votes from majority (including itself) → becomes leader. Immediately sets `next_index[peer] = last_log_index + 1` and `match_index[peer] = 0` for each follower. Sends immediate heartbeat (empty AppendEntries) to establish authority.
5. Any node that sees a higher term in any RPC immediately reverts to Follower and updates its term.

**The Figure 8 commit rule — the hardest part.**

Setup: 5 nodes (S1–S5). S1 is leader in term 2. Appends entry at index 2 (term 2). Replicates to S2 only (S3/S4/S5 don't have it). S1 crashes. S5 becomes leader in term 3, appends a *different* entry at index 2 on its log, crashes before replicating. S1 restarts in term 4. Continues replicating its index-2 term-2 entry. Now S1, S2, S3 all have index 2 with term 2 — a majority.

**The wrong behavior:** commit index 2 because it's on a majority. Then S5 restarts (term 3 > term 2 at index 2), wins an election (its log is "more recent" than S3's because S5 has term 3 vs S3's term 2 at the same index), becomes leader, and overwrites index 2 on all nodes with term-3 content. A "committed" entry just got silently overwritten.

**The correct rule:** A leader may only directly commit an entry from its *own current term*. It never commits an entry from a previous term by itself. In the scenario: S1 must append something at index 3 (term 4), wait for that to reach a majority. When index 3 commits, index 2 also commits as a side effect — but now S5 cannot win an election because S1/S2/S3 all have a term-4 entry at index 3, which is more recent than anything S5 has.

**Implementation.** `compute_new_commit_index()` in `raft_node.hpp` — a pure, stateless function that takes match_index values, cluster size, current commit_index, current term, and a `term_at(index)` accessor. It finds the highest index N where a majority of `match_index[peer] >= N` AND `term_at(N) == current_term`. Extracted as a pure function specifically so it can be unit-tested with constructed adversarial scenarios without needing a live cluster.

---

## 6. "How does log compaction work? What is InstallSnapshot?"

**The problem.** Without compaction, the Raft log grows forever. A coordinator that was offline for an hour must replay every log entry from the beginning to catch up. For a control-plane log (shard membership changes), this is bounded — but it's wrong in principle and becomes a real problem if coordinators restart frequently.

**The trigger.** `COMPACTION_THRESHOLD = 64`. After `g_local_applied_count` reaches 64 newly applied commands (post-snapshot), the coordinator serializes the current cluster topology to JSON: `{"shards": [...]}` with each shard's replica endpoints and primary designation. Calls `RaftNode::compact(up_to_index, snapshot_json)`.

**The file format.** `RaftLog` stores entries on disk with a magic header (`0x4E414654` = "NAFT") to distinguish the new format from old format files: `[magic:4][snapshot_last_index:8][snapshot_last_term:8][snapshot_data_len:4][snapshot_data:N][entry1][entry2]...`. Each entry is `[term:8][cmd_len:4][cmd:N]`. On `open_file`, if the magic is present, read the snapshot header first, then the remaining entries. If absent (old format), read entries starting at offset 0.

**compact().** Erases entries up to `up_to_index` from the in-memory `entries_` vector. Updates `snapshot_last_index_`, `snapshot_last_term_`, `snapshot_data_`. Persists the new file format. After compaction, `term_at(i)` for any i ≤ snapshot_last_index returns `snapshot_last_term_` (the log only stores the term of the *last* included entry, not every compacted entry's term).

**InstallSnapshot.** When the leader calls `send_append_entries_to_peer`, it first checks: `prev_log_index < log_.snapshot_last_index()`. If true, the peer needs entries we've already compacted. Instead of AppendEntries, the leader sends `InstallSnapshot(term, leader_id, last_included_index, last_included_term, snapshot_data)`. The follower receives this in `handle_install_snapshot`: trims its `applied_commands_` vector, advances `last_applied_` and `commit_index_` to `last_included_index`, calls `log_.compact(last_included_index, last_included_term, snapshot_data)`. The follower is now at the same state as the leader's snapshot and resumes receiving AppendEntries from `snapshot_last_index + 1`.

---

## 7. "How does your replication work? What is epoch fencing?"

**Fan-out model.** The coordinator is the replication driver. For every write, it fires gRPC `Insert` calls to all replicas in parallel via `std::async`. It collects all futures, counts acks. This is coordinator-driven fan-out, not primary-to-replica propagation. Simpler — shard nodes are completely passive, they just accept or reject writes. Trade-off: the coordinator is the bottleneck; a direct-write model (client writes to primary, primary propagates) would scale better but requires per-shard replication logic.

**Quorum.** For a shard with N replicas, quorum requires ⌈N/2⌉ + 1 acks AND the primary must be among the acks. The primary-mandatory rule is not arbitrary — the primary is the source of truth for reads. If writes could succeed without the primary and the primary failed over to a different node afterward, reads could diverge.

**The split-brain problem.** After a failover, there's a window where the old coordinator doesn't know it's been superseded. It still has gRPC stubs to all shard replicas, still thinks it's allowed to write to the old primary's shard. If the new coordinator is also writing, two coordinators are directing conflicting writes to the same shard — split-brain.

**Epoch fencing.** Each shard node tracks `current_epoch_` (an `atomic<uint64_t>`). The epoch is the Raft log index of the most recent `AddShard` or `SetPrimary` command for this shard — already monotonically increasing, no new machinery needed. When a new primary is elected, the `SetPrimary` command commits at Raft log index N. All coordinators apply this command and update `sc->epoch = N` for that shard's replicas. The old coordinator still has the old epoch value (less than N) cached in its `ShardClient`. When it tries to write, it attaches the old epoch. The shard checks `request_epoch < current_epoch_` → rejects: `"stale epoch 5, this shard has already seen epoch 7"`. No coordinator-to-coordinator communication needed — shards enforce it unilaterally.

**Known gap.** `current_epoch_` is in-memory only. If a shard node crashes and restarts, its epoch resets to 0. A stale coordinator with epoch 0 could write to it again in the narrow window before the health check fires a new `SetPrimary` command and re-establishes the epoch. Fix: persist the epoch to disk on every accepted write. Not implemented; deliberately deferred.

---

## 8. "How does automatic failover work? What bug did you find?"

**The health check loop.** Runs on all coordinators, only the Raft leader acts. This is the correct architecture: if followers also acted on failures they observed, two coordinators could independently propose `SetPrimary` for different replicas at the same time. Only the leader proposes — and Raft serializes everything through the leader anyway, so even if two leaders race, only one's proposal commits.

**Detection.** Every 1000ms: ping every shard primary. If a primary misses 3 consecutive pings (3 seconds), trigger failover.

**Promotion.** Collect all reachable non-primary replicas for the failed shard. Query `Stats()` on each. Pick the one with the highest `element_count`. Propose `SetPrimary(shard_id, best_replica_id)` through Raft. All coordinators apply it, epoch advances.

**Bug found (via chaos harness).** Original code: "promote the first reachable non-primary replica." `replicas_for_shard()` iterates in registration order (replica 0, 1, 2). Scenario from `repro_failover_loss.py`: replica 0 is primary, replica 1 was down for 30 seconds (missed 40 writes), replica 2 stayed up (has all writes). Failover triggers. Old code: found replica 1, it's reachable (it came back up), stop looking, promote it. 40 confirmed writes gone. No error, no log line — just silent data loss.

**Why unit tests didn't catch it.** The bug requires: (a) a specific ordering of events over real wall-clock time (replica down for sustained period then back up before failover triggers), (b) real network processes and Ping RPCs, (c) the automatic 3-consecutive-failure path (not the manual `/admin/shards/set_primary` override). None of that is reproducible in a unit test.

**Why the chaos harness caught it.** The harness runs with real processes and real timing. It tracks every HTTP-201 response as a "confirmed write." After chaos stops, it checks `/stats` against the confirmed-write set. The first run with this failure mode: confirmed 200 writes, /stats shows 160. Violation logged. Then `repro_failover_loss.py` was written to reproduce it deterministically.

---

## 9. "Where does your system sit in CAP? What trade-offs did you make?"

**The honest answer:** this system is CP on the control plane and tunable on the data plane.

**Control plane (Raft).** If the leader can't reach a Raft quorum (1 of 3 coordinators), it steps down. No new failover decisions, no new shard membership changes. The cluster is temporarily unavailable for topology changes but consistent — it won't make contradictory decisions.

**Data plane writes.** Primary-mandatory quorum: if the shard primary is unreachable, writes to that shard fail (503). You get consistency (no write succeeds without the authoritative node) at the cost of availability (shard primary failure = that shard's writes are down until failover completes, ~3 seconds).

**Data plane reads.**
- `"consistency": "strong"` → always routes to primary. Primary down → 503. Consistent, possibly unavailable.  
- `"consistency": "eventual"` → routes to any replica. Primary down → reads still succeed from a secondary, possibly returning stale data (a few writes behind). Available, possibly inconsistent.

**The ANN argument for eventual reads.** This is a real interview point: HNSW search is approximate by design — it misses the true nearest neighbor with probability ~5% even when all data is present. A read from a replica that's 5 writes behind is indistinguishable from a correct read that happened to not find those vectors due to HNSW's approximation. This is a genuine argument that eventual consistency is acceptable for vector search in many real-world use cases, and it's why production systems like Qdrant offer tunable consistency levels.

---

## 10. "What problems did you face? How did you solve them?"

*Have specific answers ready. Vague "I had issues with networking" will not land.*

**Problem 1: FNV-1a doesn't mix sequential keys.** Discovered when 200 inserts with IDs "user-1" through "user-200" all landed on shard 0. FNV-1a is a byte-sequential hash — strings that share a long common prefix produce similar hash values. Fix: double-application (`well_mixed_hash = fnv1a_64(fnv1a_64(id))`). Diagnosed by inserting 10,000 keys and checking the shard distribution via `/stats`. Would not have found this with only random IDs in tests.

**Problem 2: discarding std::async futures blocks immediately.** In the original `replicate_to_followers` implementation, futures were collected in a temporary vector that went out of scope at the end of a loop body, blocking on each future before starting the next. The "parallel" replication was actually sequential. Caught by measuring insert latency — it was 3× higher than expected for 3 replicas. Fix: hold all futures until all have been launched, then collect results. This is a standard C++ gotcha: a `std::future` destructor blocks if the future is still running.

**Problem 3: non-atomic config file write causing crash-loop.** Found by chaos harness. A coordinator killed during `save_cluster_config` left a half-written JSON file. On restart, `load_cluster_config` called `json::parse` on the truncated file, threw an exception, coordinator exited 1. The cluster couldn't recover without manual intervention because every restart produced the same failure. Fix: write to a temp file (`.tmp` suffix), then `rename()`. On POSIX systems, `rename` on the same filesystem is atomic — the file is either fully old or fully new, never partially written.

**Problem 4: `std::async` on a detached thread creating a use-after-free.** The `replicate_to_followers` call spawned a `std::thread` that captured `this` (the RaftNode). If the RaftNode was destroyed before the thread finished (e.g., during shutdown), the thread would dereference a dangling pointer. Fixed by storing a copy of the relevant state (term, next_index snapshot) in the lambda rather than accessing member variables through `this`.

---

## 11. "What would you do differently? What are the weaknesses?"

*Interviewers like this question because it distinguishes builders who think critically from those who just implemented a spec.*

**1. Coordinator is the write bottleneck.** Every insert goes through one coordinator that fans it out. Write throughput is bounded by coordinator network bandwidth divided by replication factor. A primary-driven model (client → shard primary → primary fans out to secondaries) scales better but requires each shard primary to implement its own replication logic — more complex, but that's how Raft-based databases like etcd actually work.

**2. Epoch fencing is not durable.** `current_epoch_` resets to 0 on shard restart. A 2-line fix: persist to a small binary file on every accepted write, read it on startup. Deliberately deferred to keep shard node implementation simple during development.

**3. No write-ahead log on shards.** HNSW uses mmap with MAP_SHARED — dirty pages are flushed to disk by the OS asynchronously. A shard that crashes mid-insert may have a partially written HNSW graph on disk. The graph is not corrupted in the C sense (no undefined behavior), but a partially linked node could degrade search quality. Fix: WAL on the shard — log the insert first, apply to HNSW second. On recovery, replay uncommitted WAL entries.

**4. Automatic rebalancing isn't triggered.** The consistent hash ring is recalculated on shard add/remove, but the decision to add or remove shards is manual. Production would monitor per-shard vector counts and trigger rebalancing automatically when distribution skews beyond a threshold. The Prometheus metrics are already there (`nanodb_vectors_total` per shard) — it's a monitoring → action loop that wasn't implemented.

**5. The failover heuristic (element_count) isn't perfect.** If two replicas each missed different individual writes at different times, they can tie on count while having different content. The current code breaks ties by iteration order (deterministic but arbitrary). A correct solution requires either: (a) all-replica ack on every write (reduces availability), or (b) a reconciliation protocol that diffs key sets between replicas (complex). This gap is documented in comments in `coordinator_main.cpp`.

---

## 12. Quick reference: exact values and file locations

| Constant | Value | File | Line reference |
|----------|-------|------|----------------|
| Election timeout | 300–600ms random | `cluster/raft_node.hpp` | `ELECTION_TIMEOUT_MIN/MAX_MS` |
| Heartbeat interval | 50ms | `cluster/raft_node.hpp` | `HEARTBEAT_INTERVAL_MS` |
| RPC timeout | 100ms (Raft), 800ms (shard), 2000ms (migration) | `raft_node.hpp`, `coordinator_main.cpp` | |
| Virtual nodes per shard | 200 | `cluster/hash_ring.hpp` | `VNODE_COUNT` |
| Failures before failover | 3 consecutive | `cluster/coordinator_main.cpp` | `FAILURES_BEFORE_FAILOVER` |
| Health check interval | 1000ms | `cluster/coordinator_main.cpp` | `HEALTH_CHECK_INTERVAL_MS` |
| Compaction threshold | 64 commands | `cluster/coordinator_main.cpp` | `COMPACTION_THRESHOLD` |
| Raft log magic | `0x4E414654` ("NAFT") | `cluster/raft_log.hpp` | `kMagic` |
| Quorum formula | ⌊N/2⌋ + 1 | `cluster/coordinator_main.cpp` | `quorum_insert()` |
| Vector dimension | 128 | `include/config/constants.hpp` | `VECTOR_DIM` |
| Node neighbors (layer 0) | 32 | `include/config/constants.hpp` | `M_MAX0` |

---

## 13. The two-sentence story for every interview opener

> "I built a distributed vector database in C++ from scratch — custom HNSW storage engine, Raft consensus, consistent hashing, quorum replication, and automatic failover. No external libraries for any of the distributed systems primitives, and I chaos-tested it by randomly killing processes and validating that no confirmed write ever disappeared."

That's it. Don't expand unless asked. Let them pull the threads.
