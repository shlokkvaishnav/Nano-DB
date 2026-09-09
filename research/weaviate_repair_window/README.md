# What makes Weaviate's async repair measurable?

Issue #48 · branch `method/weaviate-repair-window` · the last instrument prerequisite before the dissociation experiment.

**Answer: it already is — and the follow-on this study was meant to scope should not be built.**

PR #44 measured repair at **~0.3 s** once and named "sub-second sampling" as the remaining prerequisite. Three repetitions at that same divergence size give **44.7 s, 0.008 s, 0.010 s**. The 0.3 s was one draw from a **bimodal** distribution.

| divergence | repair (s) |
|---|---|
| 50 | 44.725, 0.008, 0.010 |
| 500 | 0.169, 50.234, 50.047 |
| 5,000 | 38.286, 40.787 |

Every observation is either **sub-0.2 s** or **36–50 s**, and a **100× increase in divergence does not increase repair time**.

**Is it "remembering" (warming up), or timing?** Separated directly: 10 repetitions at one size with a **randomized 0–50 s delay** before each restart, which scrambles timing while leaving repetition order intact. correlation(repair, repetition index) = **+0.373** — warming predicts strongly negative — and the first run was among the fastest. **Warming refuted; timing decides it** (fast runs' mean pre-delay 10.4 s vs slow runs' 32.3 s).

**And it is not a countdown.** ~~Pooling all 18 observations, **nothing falls between 0.2 s and 36 s** … So: **two discrete paths**.~~ **Withdrawn 2026-09-06.** Step 2c ran 18 repairs at *fixed* timing offsets instead of randomized ones, and **12 landed inside that supposedly empty interval**. The gap was an artifact of how step 2b sampled, not a property of the system. What survives is that the wait is timing-determined and that the earlier "uniform-ish over an interval" wording was also wrong.

**What step 2c found instead.** Holding the outage at 40 s and varying only how long the divergence had existed when the node returned: data ~38 s old reconciled in **~1 s**, data ~6 s old took **~31 s** — disjoint, exact Mann-Whitney p = 0.0022 at 6v6. But *"older divergence repairs faster"* is **withdrawn as an explanation** (review round 1): measured from the **write** rather than from the restart, those two conditions are 38.9 s and 36.9 s — a 30× gap becomes ~5%. Convergence at a roughly fixed latency after the write fits equally well, and would be near-definitional. **The measurement stands; the mechanism does not.** Whether the repair clock is anchored to the write or the restart is the open question. A third condition (6 s outage) is bimodal at ~0.02 s or ~52 s and is **not explained** by either variable. The pre-registered binary fast/slow statistic is **void**: its 1 s threshold was chosen because the gap was believed real, and it splits a tight cluster in half. Full account in `SPEC.md` step 2c.

**Consequences.**

- **Sub-second sampling is not needed.** A 40–50 s window holds 40–50 samples at 1 s cadence, 8–10 at 5 s — the cadence this project already uses. The probe floor is ~50 ms, two orders of magnitude below what's required.
- ~~**The healing signal is a step at 50–500 objects, a short ramp at 5,000.**~~ **Withdrawn 2026-09-09 (#63).** The 5.8–6.3 s "ramp" is the span of the last **three** stored samples: `characterize.py` keeps only `traj[:6]` and `traj[-3:]`, and at 5,000 objects the probe's own cost makes the cadence ~2 s, so the whole 38–41 s trajectory is 17–22 samples. The sentence that a 1 s cadence "resolves it into 5–6 points" is the inverse of the truth. Reading the stored head beside the tail, the real shape is **burst → ~30 s plateau → completion** — 29–50% of objects arrive within 1.7–2.4 s, the count then holds at 2,500 / 2,000 (not zero) for ~30 s, and the rest lands in the final ~6 s. **"Size decides step-vs-curve" is withdrawn**; no transfer duration replaces it. The step figures at 50–500 objects (≤8–16 ms, ≤106–167 ms) are unaffected — they are upper bounds from transitions faster than the probe. See [`../weaviate_repair_trajectory/`](../weaviate_repair_trajectory/) and [`../claim_corrections/weaviate-repair-ramp-truncation.md`](../claim_corrections/weaviate-repair-ramp-truncation.md).
- **A hard probe bound, found by hitting it.** `ids` is base64 in the URL: the probe works to **15,000 ids (800 KB)** and **fails at 20,000 (1.07 MB)**. It failed silently — the run reported `first_answer None` — the same shape as #26 and #38, so it is recorded rather than filed as an anomaly.

Full numbers, the control that separates repair from restart latency, and what is not established: [`SPEC.md`](SPEC.md).

## Reproducing

```bash
python research/weaviate_repair_window/characterize.py --sizes 50,500,5000 --reps 3
```

Digest-pinned Weaviate 1.29.0, 3 nodes, factor 3, 1 shard, one host. n = 2–3 per cell; the sync interval itself was not measured, only the waits it produces.
