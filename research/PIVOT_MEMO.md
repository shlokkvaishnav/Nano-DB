# Status, results, failures, and where to go next

*A retrospective for discussion, not a results section. Every number below is committed to this repository and independently recomputable from the raw data; where a claim was withdrawn, the withdrawal is recorded rather than the claim quietly removed.*

**Status: insurance, not the plan of record.** The current plan is to raise `n` on the existing programme (see `DECISION_LOG.md`, 2026-09-10, for the pre-registered pivot trigger). This memo exists so that a pivot, if triggered, starts from a written position rather than a scramble.

---

## 1. What the project set out to do

A degraded ANN (approximate nearest neighbour) index does not throw an error. It returns slightly worse answers, indefinitely, and nothing in the stack notices — the query succeeds, the latency is normal, the results look plausible.

The thesis: **replicas of the same shard can silently diverge in search quality after node failure, and averaging hides it.** The method decomposes "the search is bad" into three separately measurable axes, probing each replica directly rather than through the coordinator, which merges replicas and averages the divergence away:

| axis | what it measures | needs ground truth? |
|---|---|---|
| `index_recall` | graph quality, **data held constant** | yes |
| `completeness` | data content, no search involved | no |
| `e2e_recall` | what a client actually experiences | yes |

Plus a fourth, `loo_agreement`, computed with **no ground truth** — leave-one-out peer disagreement — to test whether replicas can police each other in production, where ground truth does not exist.

Three systems: **nano-db** (built from scratch, C++ HNSW + Raft, no anti-entropy by design), **Qdrant** (third-party, to test whether the effect is an artifact of our own implementation), and **Weaviate** (the only one with real hash-tree anti-entropy, so the only one that could test the field-level claim).

---

## 2. What is established

**Layer 1 — nano-db (#53, reproduced with raw data committed).** Node-kill chaos on real SIFT1M data, 5 seeds:

| metric | baseline | chaos | p |
|---|---|---|---|
| `index_recall` | 0.9973 | 0.9709 | 0.0079 |
| `completeness` | 1.0000 | 0.9580 | 0.0079 |
| `e2e_recall` | 0.9994 | 0.9581 | 0.0079 |
| within-shard spread | 0.0000 | 0.0534 | 0.0079 |

1,632 sample rows committed. **p = 0.0079 is the floor** for an exact Mann-Whitney at 5v5 — it means the groups separate completely, *not* that the effect is large. This is a **reproduction**, not independent confirmation: same protocol, same binaries, new host.

**Qdrant (#31, #37) — the unit of measurement is the finding.** Worst-replica `index_recall` under chaos is 0.978 vs 0.990 at baseline, every seed separated, p = 0.0079 — while the **mean over the same six replicas does not separate (p = 0.31)**. The killed node is the worst replica in 4 of 5 runs, losing up to 5 points where the seed mean loses ~1.2. A monitor that averages is structurally blind to this.

The loss is a **transient**: at a 180 s horizon it heals (#37), which retired an earlier claim that it did not.

**The measurement itself is hard, and that is a result.** Getting Qdrant to measure a graph at all required a purpose-built indexing gate (#28), after the first attempt measured an un-indexed corpus for 60–84% of its window.

---

## 3. What failed

This is the honest part, and it is why a pivot is on the table.

### The field-level prediction was tested and did not hold (#54)

`RELATED_WORK.md` §4 argues structurally that every production anti-entropy mechanism operates on *exact object identity*, and two correct HNSW graphs over identical data differ bit-for-bit — so object-level repair cannot be pointed at the index. The sharp prediction: after chaos, missing objects come back and graph quality does not.

On Weaviate, at a 60 s horizon, **both axes heal.** Against a corpus-matched control, chaos cost no more `index_recall` than the control in *any* seed. On the three corpus-matched seeds the paired differences were **0.000, 0.000, +0.015** — two identical to within the metric's 0.005 resolution. The dissociation was observed in **0 of 5 seeds**.

The structural argument survives — repair still cannot *target* the index. Its predicted observable consequence does not. The most likely reading is that re-inserting the objects rebuilds enough of the graph as a side effect that no deficit remains resolvable.

Bounds, because "both heal" is as easy to overstate as the prediction was: one system, one pinned build, one host, 60 s, **n = 3** matched pairs, excluding a deficit above ~0.02 and saying nothing below it.

### The ground-truth-free detector is at chance on the axis that matters (#52)

Scoring `loo_agreement` over 51 committed runs at zero compute: against `e2e_recall` it works — 0.908 chaos vs a 0.635 no-chaos control, p = 0.0023, so Layer 3 replicates on a second system. Against **`index_recall` it collapses to 0.348 — the chance line is 0.333** — versus a 0.670 baseline, separating at p = 0.0001 *in the wrong direction*.

The detector cannot find the replica behind the project's own headline finding. It appears to point at the data-poor replica instead: `loo_agreement` compares *returned results*, so a replica missing objects is easy to flag, while a replica holding all the data with a slightly worse graph returns nearly the same answers — subject to the same silence the project exists to study.

### A published claim turned out to measure the sampler — withdrawn (#63)

"Healing is a ~6 s ramp at 5,000 objects" was **withdrawn** because it was the span of the **last three stored samples**. The harness keeps only `traj[:6]` and `traj[-3:]`, and at that size the probe's own cost sets a ~2 s cadence, so the entire 38–41 s repair is 17–22 samples.

The real shape, visible only by reading head against tail, is **burst → ~30 s plateau → completion**: 29–50% of objects arrive within 2.4 s, then nothing for ~30 s, then the rest. The interesting phase sat entirely inside the discarded middle.

### The Weaviate instrument consumed the experiment

Four amendments, three discarded sweeps:

- the graph axis scored **0.23 on a healthy, undisturbed replica** — an L2-vs-cosine metric mismatch *plus* a shared scratch class holding 14,200 objects against a 5,000-object ground truth;
- isolating a replica with `docker stop` dropped Raft below quorum and **bricked the cluster twice**, forcing full rebuilds;
- clearing the corpus by batch-deleting objects **stalled async repair permanently** under `deletionStrategy: NoAutomatedResolution` — disabling the exact mechanism under study;
- and the first apparently-positive result was **dilution**: ground truth covering 5,000 objects while the replica answered from 10,000. A parameter-free model predicted all five observed values to a mean absolute error of 0.027. It was caught in review, before publication.

---

## 4. What the workflow cost

Measured across all 25 studies in the index:

| signal | value |
|---|---|
| Method-typed studies | 8 of 25 |
| Weaviate leg | **5 method studies : 1 experiment** |
| Instrument-characterization sections backed by a runnable check | **0 of 9** |
| …written retroactively | 5 of 9 |
| Harnesses with a positive control that aborts | **1** |
| Review-round findings about measurement validity | **8 of 11** |
| Review-round findings about science | 3 of 11 |

The last two rows are the crux. Under a "we discovered a phenomenon" framing, spending 8 of 11 review rounds on measurement validity reads as a project that cannot get out of its own way. Read differently, it is a **catalogue of the ways replica-level ANN measurement silently fails** — each one dated, with the control that did or did not catch it.

The single fixable defect: **instrument validity is prose, not code.** #54's graph axis read 0.23 on a healthy replica because nothing asserted that it worked before the run.

---

## 5. Where that leaves the contribution

Defensible today:

1. **The measurement unit is the finding** — replica-level separation with the effect concentrated on one replica, while the cluster mean is a null on the same runs.
2. **The three-way decomposition** — `index_recall` / `completeness` / `e2e_recall` moving independently. Vendor writing gestures at "index quality vs retrieval quality"; isolating `completeness` as a replication-damage diagnostic appears unclaimed.
3. **Cross-system reproduction under a fixed instrument**, including that the first measurement was wrong, was caught, and was withdrawn in both directions.
4. **Two clean negatives** — the dissociation, and the detector at chance — both reported at full precision.

Gone: the dissociation as a positive result, and the detector as a deployable product.

Honest venue assessment: a **methodology / experiments-and-analysis** contribution (PVLDB E&A, DBTest) is reachable. A top-tier systems paper is not without a mechanism — and the mechanism has resisted two attempts. `graph_forensics.py` found no average difference in neighbour-list quality between baseline and chaos replicas, **except one replica, never itself killed, that lost reachability to 58.7% of its own graph while every structural check on it looked clean.**

---

## 6. Pivot options

**A. Learned ground-truth-free detection.** #52 tested one hand-crafted statistic. Whether graph degradation is invisible *to that statistic* or *in the observables at all* is a supervised-learning question with **14,334 labelled rows already committed**. A positive returns the deployable detector; a negative gives an information-theoretic result with a learned upper bound — strictly stronger than #52. Zero new compute.

*Caveat governing the design:* those rows are ~50–90 runs sampled every few seconds, so effective n is the number of **runs**, not rows. Grouped splits by seed and permutation nulls throughout; gradient boosting, not deep learning.

**B. Cross-system transfer.** Train on nano-db, test on Qdrant and back. Does a degradation signature transfer across implementations? Nobody else has cross-system labelled ANN-degradation data, because nobody else built three harnesses. Hardest part for a reviewer to dismiss as fitting a model to our own logs.

**C. The HNSW mechanism.** Attack the 58.7% reachability anomaly with graph-structural features or graph ML. Highest payoff — it is the open question — and highest risk.

**D. Incremental / churn-driven degradation.** The project measures recall decay under **failure**; the adjacent open area is recall decay under **churn and incremental update**, already cited in `RELATED_WORK.md` (FreshDiskANN, SPFresh, Big-ANN'23). The existing harness and three-axis decomposition transfer directly.

**E. Shore up and write the methodology paper.** Larger n, the 180 s re-run, more queries per snapshot. *This is the current plan of record.*

---

## 7. Questions for the group

1. **Why the CocoIndex link?** It could not be connected to replicas, HNSW quality, or anti-entropy — it is incremental data processing with content-hash change detection and lineage tracking. Best guesses: it points at **incremental maintenance of derived state** (option D), or at **lineage as an alternative to detection** — rather than detect a silently-degraded index, track provenance so you know what is stale. Both are plausible directions; neither is what the project currently does. Worth asking directly rather than inferring.

2. **Is a well-evidenced negative acceptable as a headline?** Two of the four surviving contributions are negatives.

3. **What compute is available, and does it permit Docker?** Everything to date ran on one laptop — 7.7 GB RAM, no GPU, single Docker host — which caps seeds at 5 and puts p = 0.0079 (the floor) on every result. PARAM Shavak would lift the memory ceiling 8× and allow parallel seeds. **But HPC facilities commonly disallow Docker in favour of Singularity/Apptainer, and all three harnesses are Docker Compose.** The Weaviate leg additionally depends on container-level operations (`docker pause`, `docker stop`) whose Singularity equivalents differ.

4. **Is the goal a paper, or a system?** The measurement discipline is the strongest asset; a tool or benchmark built on it is a different project from a paper about it.

---

## 8. Standing caveats on every number above

n = 5 seeds throughout, at the exact statistical floor for the rank test used. One host, one topology per system. Ground truth is brute-force, so this is a mechanism study, not a scale study. The Weaviate probe depends on an undocumented internal API of one digest-pinned build. The nano-db numbers are reproducible but **not independently confirmed** — same protocol, same binaries, new host.
