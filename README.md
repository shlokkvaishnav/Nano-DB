<div align="center">

# Replica-Recall-Divergence

[![C++17](https://img.shields.io/badge/C%2B%2B-17-orange?style=flat-square&logo=cplusplus)](https://en.cppreference.com/w/cpp/17)
[![Build](https://img.shields.io/github/actions/workflow/status/shlokkvaishnav/Replica-Recall-Divergence/ci.yml?style=flat-square&label=build)](https://github.com/shlokkvaishnav/Replica-Recall-Divergence/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

**Does search quality silently diverge across replicas of an approximate index under node failure — and does it ever come back?**

</div>

---

## Research question

Does node-failure chaos cause measurable, replica-level search-quality divergence in a replicated HNSW-based vector database; does that divergence persist after full cluster recovery; and can a ground-truth-free peer-agreement signal detect the degraded replica?

## Why this matters

An exact key-value store that loses a row returns *not found* — an observable event a consistency checker can flag. An approximate nearest-neighbour index that loses vectors, or whose graph degrades, still returns *k* plausible-looking neighbours. Nothing about the response says anything is wrong. **Approximation converts data loss into silence.**

No published Jepsen-style analysis has ever targeted a vector database; no streaming-ANN benchmark injects node failure; no production anti-entropy mechanism (Weaviate's hash-tree replication, Vespa's checksummed reconciliation, Dynamo/Cassandra Merkle repair) can be pointed at an ANN graph, because two correct HNSW graphs over identical data differ bit-for-bit. Full positioning against prior work, and the claims this project must *not* make because someone else already owns them: [`research/RELATED_WORK.md`](research/RELATED_WORK.md).

## What is measured, and on what

The method is the same on every system: **probe each replica directly**, bypassing the coordinator's scatter-gather, which would merge replicas and average the divergence away. "The search is bad" is then decomposed into three ground-truth-backed measurements that move independently — `index_recall` (graph quality, data held constant), `completeness` (data content, no search involved), `e2e_recall` (what a client actually experiences) — plus a fourth, `loo_agreement`, computed with **no ground truth**, to test whether peer disagreement can substitute for it. A quiesce protocol (stop chaos, keep watching) separates *transient replication lag* from *permanent, unrecovered loss*.

Three systems, in order of how much control they give up:

| system | why it is here | what it costs |
|---|---|---|
| **nano-db** (`cluster/`, `include/`, `src/`, `proto/`) | a Raft-replicated vector database written from scratch for this project, so every internal is inspectable and the graph can be read off disk | it is one implementation, and its conclusions do not transfer on their own |
| **Qdrant** | a third-party production system, to test whether the effect is an artifact of nano-db | no source-level access; needed a purpose-built indexing gate before `index_recall` measured a graph at all |
| **Weaviate** | the only one of the three with **real anti-entropy** (hash-tree async replication), which is where the field-level claim **was** tested — and **not confirmed** (#54: both axes heal) | documents no node-targeting read; the probe depends on an undocumented internal API, pinned by image digest |

nano-db is the **experimental apparatus, not the contribution**. It is a distributed vector database built without a consensus library, managed queue, or distributed KV store — the Raft log, quorum write protocol, consistent hash ring and epoch fence are all implemented directly, which is what makes the graph and the replication path observable. *If you need a production vector database, use [Qdrant](https://qdrant.tech) or [Milvus](https://milvus.io).*

## Current findings

**ESTABLISHED** — supported directly by the experiments in this repo:
> On nano-db (2 shards × 3 replicas, from-scratch C++ HNSW + Raft), under node-kill chaos on real SIFT1M data (5 seeds), `index_recall` and `completeness` both degrade measurably and statistically significantly versus a no-chaos baseline (exact Mann-Whitney at the n=5 floor, p = 0.0079). Independently-built healthy replicas agree to 1e-4, so ordinary ANN nondeterminism does not explain the gap. Missing data has not returned in any observed post-recovery window. **Caveat, stated here rather than buried:** the raw per-seed data behind these numbers is not committed — see [Raw data status](#raw-data-status).
>
> On a second system (Qdrant, same 2 × 3 topology, `docker kill` chaos, 100k SIFT1M, 5 new seeds), the graph-quality divergence reproduces **at the replica level**: with the corpus HNSW-indexed before measurement and each sample conditioned on its replica being ≥95% indexed, the worst replica's `index_recall` under chaos is 0.978 vs 0.990 at baseline, every seed separated (p = 0.0079), and the worst replica is the killed node in 4 of 5 runs. The cluster-wide six-replica mean does **not** separate (p = 0.31) — the loss is one replica's, ~1.2 points on the seed mean and up to 5 on that replica, and averaging hides it. `completeness` and `e2e_recall` separate on Qdrant as before. See [`research/qdrant_gated_index_recall/`](research/qdrant_gated_index_recall/) (PR #31) and the instrument that made it measurable, [`research/qdrant_index_gate/`](research/qdrant_index_gate/) (PR #29).
>
> **That loss is a transient of the restart.** Watched for 180s after chaos stopped (≈36 post-chaos samples against the 4–5 an earlier 50s window gave), the worst replica's `index_recall` is back inside its own no-chaos range in **4 of 4 judged seeds**: after the last kill one seed dropped to 0.946 for a single 30s bin, one dipped 0.003 for one bin, and two showed nothing beyond noise — every dip gone by the next bin. The fifth seed is **unmeasured**, not healed: its chaos window fired zero kills, a harness defect since fixed and covered by tests ([#38](https://github.com/shlokkvaishnav/Replica-Recall-Divergence/issues/38), [`research/qdrant_chaos_loop_timeout/`](research/qdrant_chaos_loop_timeout/)). Completeness recovered **100%** of missing ids in all four, where the 50s window had seen 0–100%. On one host, at k = 10 over 100k SIFT, on a metric with ~1% of headroom; the closest seed clears its baseline by 0.0002, i.e. is indistinguishable from it rather than above it. This is the opposite of nano-db's result above — but the two were observed on different horizons and different axes, so it is a difference between two measurements, not yet a demonstrated architectural difference. See [`research/qdrant_index_recall_healing/`](research/qdrant_index_recall_healing/) (PR #37).
>
> **On Weaviate, the instrument exists and the system's repair behaviour is characterized — the divergence experiment has not run.** Four method studies established that the per-replica protocol transfers: an isolated replica reports a divergence it was shown, an undocumented cluster-internal API reads one replica **without perturbing its peers** (20/20 probes at 5s cadence, proven local), and presence is decoded **per id** rather than by response size (8/8 checks against a constructed expectation, 0 false positives on never-written ids). The asymmetry is real and permanent: `completeness` can be sampled continuously, `index_recall` cannot — `_search` on the internal port returns 415 — so the Weaviate leg will report a data-axis time series against a graph-axis snapshot. Repair itself is **timing-determined, not size-determined** (independent of divergence size across 50→5,000), and at a fixed 40s outage a divergence ~38s old reconciles in ~1s while one ~6s old takes ~31s — disjoint, exact p = 0.0022. See [`research/weaviate_probe/`](research/weaviate_probe/), [`weaviate_nonperturbing_probe/`](research/weaviate_nonperturbing_probe/), [`weaviate_probe_per_id/`](research/weaviate_probe_per_id/), [`weaviate_repair_window/`](research/weaviate_repair_window/).

**HYPOTHESIS** — under active investigation, not yet confirmed:
> That a ground-truth-free peer-agreement statistic (`loo_agreement`) can identify the degraded replica above chance, making it usable as a production detector for a failure mode that is currently invisible. That this failure mode generalizes beyond one implementation — now partly moved to ESTABLISHED for the replica-level `index_recall` axis on Qdrant, still a hypothesis for the detector, **for systems with real anti-entropy**, and for the mechanism. ~~That the per-replica `index_recall` loss on Qdrant does not fully heal after chaos stops~~ — **measured and retired 2026-09-04**: at 180s it heals (see ESTABLISHED). The 50s reading that suggested otherwise was a horizon effect, on 4–5 samples. What remains hypothesis-level from that experiment: that Qdrant's *data* healing is horizon-dependent rather than seed-inconsistent — 4 of 4 seeds recovered 100% of missing ids at 180s where PR #6's 50s window saw 0–100%, on runs not designed to test it.
>
> One specific objection to the detector has been tested and did not reproduce: its above-chance performance does **not** appear to be an artifact of the harness's pinned, seeded query set. Three 5-seed conditions — pinned, non-pinned at 100 queries/round, non-pinned at 15 — gave mean hit rates of 0.87 / 0.86 / 0.81 against a 1/3 chance line, with per-seed values spanning 0.65–1.00 and overlapping heavily, and all nine pairwise between-condition comparisons non-significant (p = 0.15–0.90); see [`research/loo_agreement_nonpinned_queries/SPEC.md`](research/loo_agreement_nonpinned_queries/SPEC.md). At 5 seeds per condition the test is a weak instrument, so this weakens that confound rather than eliminating it, and does not by itself move the detector out of HYPOTHESIS.

**OPEN** — unresolved questions this repo does not answer:
> The root cause of why `index_recall` degrades under chaos. A dedicated forensic tool (`graph_forensics.py`) found no average difference in neighbour-list quality between baseline and chaos replicas — except one replica, never itself killed, that lost reachability to 58.7% of its own graph while every structural check on it looked clean. Two specific hypotheses were tested and ruled out with clean reproductions; the actual mechanism is still unknown. Full writeup: [`research/postmortems/catastrophic-disconnection.md`](research/postmortems/catastrophic-disconnection.md). Whether the divergence effect scales with corpus size is also untested. On Weaviate, what selects between a millisecond repair and a ~52s one at a *short* outage is unexplained — two manipulated variables do not account for it.

**DO NOT CLAIM** — statements this evidence does not support:
> "Approximate indexes have no observable correctness criterion under replication" as a general claim (true as a motivating intuition, unproven beyond n=1 system). "Vector databases silently lose data" in general (Milvus #37703 shows a genuinely *loud* failure — the honest claim is that approximation *permits* silence, not that it's universal). "No vector DB repairs missing data" (Weaviate/Vespa do, at the object level — the gap is that object-level repair cannot see graph-level damage). "We understand why recall degrades" (mechanism is open). Anything implying this generalizes to Milvus or production deployments — untested.
>
> ~~"Qdrant's ANN graph resists chaos"~~ — superseded 2026-09-04: measured on an indexed corpus, Qdrant's graph *does* diverge at the replica level (PR #31). What must still not be claimed: **"Qdrant's `index_recall` diverges under chaos" without the unit** — at the cluster level (mean over six replicas) it is a null at p = 0.31, and both statements are true. "We know what a kill does to Qdrant's HNSW" — the loss localizes to the killed replica; the mechanism is not observed. "Qdrant's graph damage persists" — measured at 180s, it does not. Nor the reverse without its qualifiers: **"Qdrant heals" is a claim about 4 of 5 seeds (one unmeasured), at the replica level, within 180s, on one host at 100k vectors, on a metric with ~1% of headroom whose closest seed clears baseline by 0.0002.** "Qdrant repairs graph damage where nano-db does not" as an architectural statement — the two systems were observed on different horizons and axes.
>
> **Nothing about Weaviate's divergence under chaos** — the instrument chain is closed, the experiment has not run. ~~"Weaviate's repair takes ~0.3s, so healing measurement must sample faster than that"~~ and ~~"Weaviate's repair is two discrete paths"~~ — both withdrawn 2026-09-06; see [`research/claim_corrections/weaviate-repair-two-path-withdrawal.md`](research/claim_corrections/weaviate-repair-two-path-withdrawal.md). "`loo_agreement` is robust to realistic query workloads" as a general claim — what was tested is query *pinning* and per-round query *count*, both drawn from SIFT1M's own query distribution.

## Methodology, in brief

- **Probes bypass the coordinator** — direct gRPC calls to each replica, so scatter-gather can't average the divergence away.
- **A settling window** (default 2s) prevents normal replication lag from being counted as loss.
- **A baseline-first protocol** — every chaos run is compared against a no-fault run on the same corpus, because HNSW insertion order alone produces some cross-replica disagreement even when nothing is broken.
- **Corpus choice is load-bearing.** Uniform-random vectors suffer distance concentration and hide the effect entirely (p = 0.31); real SIFT1M data separates cleanly (p = 0.0079). A benchmark built on synthetic data would have concluded there was nothing to find.
- **The seed sweep is what's reported**, not a single run — an exact two-sided Mann-Whitney U test compares 5 seeds per condition. No scipy: the rank tests are enumerated exactly, which is tractable and assumption-free at these n.
- **Experiments are pre-registered.** A `SPEC.md` fixing the question, metric and interpretation is committed *before* implementation, and amended with dated notes rather than rewritten. Results that came out void or negative are kept, not deleted.
- **The apparatus is characterized before compute is spent on it.** Three sweeps were voided by properties of the measuring instrument that were computable in advance, which is why `SPEC_TEMPLATE.md` now requires an *Instrument characterization* section.

Full methodology, every design decision and why, known limits: [`research/replica_recall/README.md`](research/replica_recall/README.md). The experiment index, with every study's status: [`research/README.md`](research/README.md).

## Reproducing

The nano-db experiments launch cluster processes directly and need the C++ binaries built. The Qdrant and Weaviate experiments are pure Python against Docker and need none of this.

```bash
git clone --recurse-submodules https://github.com/shlokkvaishnav/Replica-Recall-Divergence.git
cd Replica-Recall-Divergence
pip install grpcio grpcio-tools numpy

cmake -B build -DCMAKE_BUILD_TYPE=Release -DNANODB_BUILD_SERVER=ON -DNANODB_BUILD_CLUSTER=ON
cmake --build build -j$(nproc)
cd build && ctest --output-on-failure && cd ..     # 9 unit tests

python research/replica_recall/run_experiment.py --duration 180 --no-chaos   # baseline
mv research/replica_recall/results research/replica_recall/results_baseline
python research/replica_recall/run_experiment.py --duration 300              # chaos
python research/replica_recall/analyze.py
```

Requires Linux, CMake 3.16+, g++ 13+, `protobuf-compiler`, `libgrpc++-dev`, `libomp-dev`. `research/replica_recall/test_metrics.py` validates the measurement core the findings depend on (75 checks, no cluster needed).

For the full seed sweep the numbers are reported from, corpus choice, the quiesce/healing protocol, and graph forensics: [`research/replica_recall/README.md`](research/replica_recall/README.md). Each Qdrant and Weaviate study carries its own reproduction commands in its README.

## Raw data status

The nano-db numbers in **ESTABLISHED** came from a 5-seed sweep on real SIFT1M data whose **raw per-seed results were never committed**. That gap is now closed for the divergence claim: [`research/layer1_reproduction/`](research/layer1_reproduction/) (#53) re-ran the unchanged protocol on a fresh 5-seed sweep and commits **1,632 sample rows**. All four metrics separate at the n=5 floor — `index_recall` 0.9973 → 0.9709, `completeness` 1.0000 → 0.9580, `e2e_recall` 0.9994 → 0.9581, within-shard spread 0.0000 → 0.0534, each p = 0.0079.

**Read that as reproducibility, not confirmation.** Same protocol, same binaries, same measurement code, on a new host — it establishes that the pipeline still produces the claimed result and that the evidence is now inspectable, not that the effect has been independently confirmed. That is what the Qdrant and Weaviate legs are for.

**Two things are still not checkable.** The *magnitudes* were never recorded, so the reproduction could only test the claim as written (qualitative plus a p-value); the one committed magnitude that could be compared, the detector's hit rate, matched at 0.8666 vs 0.87. And the quiesce protocol was not re-run, so **"missing data has not returned in any observed post-recovery window" still rests on uncommitted data.**

Every other study *does* commit its data — roughly 13,400 sample rows across 90+ runs under `research/*/results*/`.

No numbers anywhere in this repository are backfilled or estimated.

## Limitations

Three systems now, but only one built from scratch, and only partial axes on each: the replica-level `index_recall` and completeness divergence reproduce on Qdrant; the detector and the mechanism remain nano-db-only or open; the healing question is answered on Qdrant only (180s horizon, 4 of 5 seeds) while nano-db's "missing data has not returned" was observed on shorter windows; and on Weaviate the field-level prediction was tested and **did not hold** (#54, n = 3 matched pairs, 60 s, deficit bound ~0.02) — only the instrument exists so far. **The single biggest open question is still whether any of this holds on a system with real anti-entropy.**

5 seeds sits at the exact statistical floor for the rank test used — p = 0.0079 is the *smallest attainable* value at n=5, so it says the groups separate completely, not that the effect is large. Ground truth is brute-force, practical only to ~10⁵–10⁶ vectors: this is a mechanism study, not a scale study. The gated Qdrant protocol has a duration limit of its own — at 240–250s and ~1.6k writes/s the un-indexed appendable tail keeps every replica near the 0.95 conditioning bar, so long runs retain as few as 29% of their rounds. `chaos_harness.py` uses SIGKILL, which does not lose dirty mmap pages, so machine-level crash consistency is a separate, unaddressed gap. The Weaviate probe depends on an undocumented internal API of one digest-pinned build, and its id-set request fails above ~15,000 ids.

Full list: the "Known limits" section of [`research/replica_recall/README.md`](research/replica_recall/README.md#known-limits).

## Open research questions / next experiments

1. ~~**Commit the Layer-1 raw data** by re-running the nano-db sweep.~~ **Done 2026-09-06 (#53, PR #60).** A fresh 5-seed sweep committed **1,632 sample rows**; all four metrics separate at the n=5 floor (`index_recall` 0.9973 → 0.9709, `completeness` 1.0000 → 0.9580, each p = 0.0079). Read as *reproducibility, not confirmation* — same code, new host. **Still open from it:** the quiesce protocol was not re-run, so *"missing data has not returned in any observed post-recovery window"* remains uncommitted; and the published magnitudes were never recorded, so only the claim as written could be checked.
2. ~~**The Weaviate dissociation experiment** — the prediction that `completeness` heals while `index_recall` does not.~~ **Done 2026-09-07 (#54, PR #61) — and the prediction did not hold.** Outcome (i): **both axes heal**. Against a corpus-matched control, chaos cost no more `index_recall` than the control in any seed; paired differences on the three matched seeds were 0.000, 0.000, +0.015. See `RELATED_WORK.md` §4 for what this does and does not do to the structural argument. **Still open from it, and now the more interesting question:** whether a deficit exists below the ~0.02 bound this design could resolve, and whether the answer holds past a 60 s horizon (#37 is the precedent for a horizon changing a healing verdict).
3. **Root-cause closure** on the 58.7%-loss anomaly.
4. **Larger seed count or bootstrap confidence intervals**, beyond the n=5 floor — an independent replication on real data is worth more than another synthetic seed.
5. **Scale sensitivity** beyond the brute-force ground-truth cap.
6. **Detector robustness against a different query *distribution***. Pinning and per-round count have been tested with no difference detectable at 5 seeds per condition, so "detection is an artifact of a pinned workload" no longer stands unaddressed. What remains untested is a workload drawn from a genuinely different distribution than the corpus's own, or an adversarial one.

## Repository structure

```
research/                     the research: methodology, experiments, findings
  README.md                   research contract + the experiment index (15 studies)
  RELATED_WORK.md             literature positioning, what others already claim
  DECISION_LOG.md             why past decisions were made, newest first
  GIT_WORKFLOW.md             branch/merge/negative-results policy
  AGENT_PIPELINE.md           the researcher -> implementer -> reviewer loop
  SPEC_TEMPLATE.md            pre-registration template
  replica_recall/             Layer 1 — the nano-db measurement harness
  cross_system_replication/   the Qdrant leg
  qdrant_*/                   Qdrant instruments and follow-ons
  weaviate_*/                 the Weaviate instrument chain
  loo_agreement_*/            the ground-truth-free detector
  claim_corrections/          claims withdrawn or narrowed, and why

benchmarks/research/          benchmark_recall.cpp — load-bearing: the tool whose
                              46% recall reading triggered the bug investigation
chaos_harness.py              process-level chaos for nano-db

cluster/, include/, src/, proto/, tests/    nano-db: the experimental apparatus
                                            (Raft + HNSW + gRPC replication)
```

## Related work

The closest prior art is Wang et al.'s *Towards Reliable Vector Database Management Systems* (arXiv:2502.20812), which names the ANN "oracle problem" but never discusses replication or fault injection. Streaming-ANN benchmarks (FreshDiskANN, SPFresh, the NeurIPS'23 Big-ANN track) measure recall decay under churn on one machine, with no replication dimension. Jepsen-family checkers assume a read has one correct value, which an approximate index does not have.

Full positioning, per-paper summaries, and the specific claims this project must *not* make because prior work already covers them: [`research/RELATED_WORK.md`](research/RELATED_WORK.md).

## License / citation

MIT — see [`LICENSE`](LICENSE). If this work is useful, please cite the repository; a formal citation format will be added if and when this research is written up for submission (see `research/RELATED_WORK.md` for candidate venues).
