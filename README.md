<div align="center">

# Distributed Nano-DB

[![C++17](https://img.shields.io/badge/C%2B%2B-17-orange?style=flat-square&logo=cplusplus)](https://en.cppreference.com/w/cpp/17)
[![Build](https://img.shields.io/github/actions/workflow/status/shlokkvaishnav/Distributed-Nano-DB/ci.yml?style=flat-square&label=build)](https://github.com/shlokkvaishnav/Distributed-Nano-DB/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?style=flat-square&logo=docker&logoColor=white)](https://github.com/shlokkvaishnav/Distributed-Nano-DB/pkgs/container/nano-db)

**A chaos-tested distributed vector database, built from scratch in C++17.**

</div>

---

## The problem

Every vector database hits the same wall. You start single-node — fast, simple, zero operational overhead. Then you try to scale. At a few million vectors you run out of RAM. At zero fault tolerance, one process crash means every search fails until someone restarts it. The production systems you've heard of (Pinecone, Qdrant, Weaviate, Milvus) solved this years ago: shard vectors across nodes, replicate for durability, use distributed consensus to decide who owns what. The question is whether you actually understand how any of that works.

This project started as an experiment: take a single-node HNSW vector search engine built in C++ and ask, without reaching for a library or a managed datastore, what does it actually take to turn this into a distributed system with real fault tolerance? The answer turned out to be eight phases of progressively harder distributed systems engineering, two real bugs found only through chaos testing, and a system that survives random process kills while maintaining data integrity.

---

## Architecture

![Cluster architecture](docs/images/architecture.png)

**Control plane (Raft group).** Three coordinator nodes form a Raft cluster. The elected leader handles all write coordination — failover decisions, shard membership changes, and primary promotions all flow through Raft consensus. Any coordinator can be killed; the remaining two elect a new leader within a second and resume without data loss.

**Data plane (sharded + replicated).** Vectors are distributed across shards via consistent hashing with 200 virtual nodes. Each shard has 3 replicas. Writes require a quorum — the primary's acknowledgement is mandatory. A write that reaches 2 secondaries but not the primary is correctly rejected, even though it's technically a majority. This matters during failover: the primary is the source of truth.

**Failover.** A background health-check loop on the Raft leader detects primary failures after 3 consecutive missed pings (~3 seconds). It promotes the replica with the highest element count — the most complete one — not just the first reachable one. That distinction was a real bug, found by the chaos harness.

---

## Key features

| Category | What's built |
|----------|-------------|
| **Consensus** | Raft from scratch — leader election, log replication, Figure 8 safety, log compaction + InstallSnapshot |
| **Replication** | Primary-replica with quorum writes. Coordinator fan-out, shard nodes remain simple |
| **Fencing** | Epoch tokens on every write — stale coordinators are rejected by shards after a failover |
| **Failover** | Automatic primary promotion based on replica completeness, not just reachability |
| **Routing** | Consistent hashing with 200 virtual nodes; sequential-ID clustering bug found and fixed |
| **Storage** | Custom HNSW, memory-mapped persistence, SIMD-accelerated (AVX2) distance kernels |
| **Chaos testing** | Continuous random process kills with data integrity invariants validated throughout |
| **Observability** | Prometheus metrics + Grafana dashboard (auto-provisioned) |

---

## Performance

| Metric | Value |
|--------|-------|
| Cluster insert throughput | **163 vectors/sec** (quorum writes, 4 concurrent clients) |
| Search latency p50 | **8 ms** (scatter-gather across 2 shards) |
| Search latency p95 | **20 ms** |
| Failover recovery time | **< 1 second** |
| Single-node insert throughput | **6,500 TPS** (8 threads, no replication) |
| Single-node search latency | **0.15 ms** |
| Recall@10 | **95%** |

Cluster numbers measured with Docker Compose on a single host (2 shards × 3 replicas + 3 Raft coordinators). The bottleneck is virtual network round-trips, not the engine.

---

## Raft consensus

![Raft state machine](docs/images/raft-state-machine.png)

The Raft implementation is the centrepiece of this project, built from the paper with no external library.

**Leader election** uses randomized timeouts (300–600ms) to prevent split votes. A candidate only wins if its log is at least as up-to-date as the voter's — this isn't just term comparison, it's a compound check on both term and index that prevents a stale node from becoming leader.

**The Figure 8 commit rule** — the hardest part of Raft — is implemented as a pure function (`compute_new_commit_index`) and tested with a constructed adversarial 5-node scenario plus a mutation test that proves the check is load-bearing, not incidental.

**Log compaction** snapshots the cluster topology every 64 committed entries and installs snapshots on lagging followers instead of replaying full history.

---

## Bugs found via chaos testing

Two real bugs were found only through the chaos harness, not through unit tests:

**Bug 1: Non-atomic config file write.** `save_cluster_config` opened the config path with `O_TRUNC`, truncating the file before writing new content. A coordinator killed mid-write left a corrupted JSON file. On restart, it read the truncated file and exited with code 1 — unrecoverable without manual intervention. Fix: write to a temp file, then `rename()` (atomic on Linux).

**Bug 2: Failover promoting the wrong replica.** The health check loop originally promoted the *first reachable* non-primary replica when the primary died. If replica 1 had been down for 30 seconds and missed 40 writes, but then came back, it would be promoted over replica 2 (which had all the data) just because it was first in iteration order. 40 confirmed writes silently disappeared. Fix: query `Stats()` on every reachable candidate, promote highest `element_count`.

---

## Quick start

```bash
git clone --recurse-submodules https://github.com/shlokkvaishnav/Distributed-Nano-DB.git
cd Distributed-Nano-DB/deploy
docker compose -f docker-compose.cluster.yml up -d
```

This starts 6 shard replicas + 3 Raft coordinators. Insert a vector:

```bash
curl -X POST localhost:8080/vectors \
  -H "Content-Type: application/json" \
  -d '{"id": "hello", "vector": [0.1, 0.2, ...128 values...], "metadata": "world"}'
```

Search:

```bash
curl -X POST localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"vector": [0.1, 0.2, ...], "k": 5, "consistency": "strong"}'
```

---

## Observability

```bash
docker compose -f docker-compose.cluster.yml -f docker-compose.monitoring.yml up -d
```

Grafana at `localhost:3000` (admin/nanodb) with a pre-built dashboard: cluster throughput, search latency percentiles, Raft term changes, failover events, and per-shard stats.

---

## Chaos testing

```bash
python3 chaos_harness.py --duration 60
```

The harness orchestrates the full cluster, runs continuous writes, randomly kills and restarts processes, and validates three invariants throughout:

- No confirmed write (HTTP 201) ever disappears
- No shard ever has two primaries simultaneously
- The cluster fully recovers once chaos stops

---

## Building from source

```bash
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release \
         -DNANODB_BUILD_PYTHON=OFF \
         -DNANODB_BUILD_SERVER=ON \
         -DNANODB_BUILD_CLUSTER=ON
cmake --build . -j$(nproc)
ctest --output-on-failure   # 10 tests
```

Requires: CMake 3.16+, g++ 13+, protobuf-compiler, libgrpc++-dev, libomp-dev.

---

## License

MIT — see [LICENSE](LICENSE).