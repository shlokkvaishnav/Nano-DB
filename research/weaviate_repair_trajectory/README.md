# The repair trajectory: the "~6 s ramp" was three probe calls

Issue #63 · branch `analysis/weaviate-repair-trajectory` · **Outcome (a): the claim on `main` is a truncation artifact and is corrected.** Zero compute — every number comes from data already committed.

## The claim under test

`main` says, of Weaviate's async repair: *"Healing is a step at 50–500 objects but a ~6 s ramp at 5,000, so size decides step-vs-curve."* The 5.8–6.3 s figure comes from `weaviate_repair_window/results/sizes.json` (#48).

## The deciding number

`characterize.py` stores only `traj[:6]` and `traj[-3:]`. Three samples span two poll intervals. Against each run's own implied cadence (`repair_s / samples_in_window`):

| size | seed | `repair_s` | samples stored | poll interval | tail span | span / 2 polls |
|---|---|---|---|---|---|---|
| 5000 | 35888 | 38.286 | **17** | 2.252 s | **6.285 s** | **1.40** |
| 5000 | 35949 | 40.787 | **22** | 1.854 s | **5.791 s** | **1.56** |

The entire 38–41 s trajectory is 17–22 samples, because the probe's cost grows with the id count and at 5,000 objects it polls about once every 2 s. **The "ramp" is the span of the last three samples.** It measures the sampler.

## What the truncation hid — and it is not a ramp

Put the stored head next to the stored tail:

```
seed 35888   head: 0.0s:140   2.4s:2500  4.7s:2500  7.1s:2500  9.4s:2500  11.7s:2500
             tail: 32.0s:2500  35.0s:3741  38.3s:5000

seed 35949   head: 0.0s:13    1.7s:1432  3.8s:2000  5.7s:2000  7.4s:2000   9.2s:2000
             tail: 35.0s:2026  38.1s:3741  40.8s:5000
```

**Burst → plateau → completion.** 29–50% of the objects arrive within 1.7–2.4 s; the count then holds unchanged for ~30 s — at 2,500 and 2,000, *not* at zero; the rest arrives in the final ~6 s.

That shape is new. No study here had reported that repair delivers a large fraction immediately and then stalls.

## What this does NOT establish

The hypothesis was `repair_s = dead_time + divergence / throughput`. It is **not** adopted, and two registered outcomes say why:

- **(c) The transfer is not linear.** OLS residuals over the dissociation trajectories are 56.5–794.3 objects. No rate is quoted as a constant.
- **(d) The dead time does not match #56.** Measured from the restart, dissociation dead times are 13.5–45.4 s (median 36.4) against #56's young-regime 31.08–33.43 s. **The mapping onto #56's step is withdrawn.**

More basically, the *shape* is wrong for that model: transfer starts immediately, then stalls. There is no leading dead time to decompose.

It sharpens #56 without explaining it. The ~32 s step is not an interval in which nothing happens — much of the data is already across before it begins. **No mechanism is claimed.**

## Two honest failures in this analysis's own design

**The registered segmentation rule mis-fires.** It assumes the first probed count is the plateau count. In both `sizes.json` runs the first probe catches the *burst* (140, 13), so the rule reports a zero plateau — exactly what it does on dissociation seed 20260903 (plateau 0.00 s, residual 794.3, worst fit in the set). That seed is flagged and kept, not re-segmented under a rule invented after seeing it.

**The registered control does not exist.** SPEC.md named the dissociation study's no-chaos arm as the probe noise floor. Five control runs are committed and **none carries a `completeness_series`** — the harness samples it only in the chaos branch. Reported rather than silently substituted.

## What is not reconciled

`sizes.json` converges at 5,000 objects in **38–41 s total**. The dissociation runs show ~36 s of dead time *plus* 15–50 s of transfer — 60 s or more, which is why two of five were right-censored. The two studies disagree at the same divergence size, with different total corpora (5,000 vs 10,000) and cadences. This analysis cannot separate those, and claims no transfer duration.

## Reproducing

```bash
python research/weaviate_repair_trajectory/analyze_trajectory.py
```

Reads `weaviate_dissociation/results/dissociation.json` and `weaviate_repair_window/results/sizes.json`. No cluster.

Full pre-registration, results and decision: [`SPEC.md`](SPEC.md).
