# Weaviate repair: the "~6 s ramp at 5,000 objects" was three probe calls

**Date:** 2026-09-09 · **PR:** #64 · **Issue:** #63 · No new experiment — every number comes from data already committed by #48 and #54.

**What was on `main`.** `weaviate_repair_window/README.md` and the experiment index both carried:

> Healing is a **step at 50–500 objects but a ~6 s ramp at 5,000**, so size decides step-vs-curve.

The 5.8–6.3 s figure was described as *"a genuinely observed ramp a 1 s cadence resolves into 5–6 points."*

**Why it was wrong.** It is the span of the last three stored samples, at a cadence the probe itself sets.

`characterize.py:143-144` keeps `traj[:6]` and `traj[-3:]` and discards everything between. Three samples span two poll intervals. Each run's own implied interval is `repair_s / samples_in_window`:

| seed | `repair_s` | samples stored | poll interval | tail span | span / 2 polls |
|---|---|---|---|---|---|
| 35888 | 38.286 | **17** | 2.252 s | 6.285 s | **1.40** |
| 35949 | 40.787 | **22** | 1.854 s | 5.791 s | **1.56** |

The probe's cost grows with the id count, so at 5,000 objects the whole 38–41 s trajectory is only 17–22 samples. The claim that a 1 s cadence resolves the transition into 5–6 points is the inverse of the truth: the cadence *is* ~2 s, and the "ramp" is what three of those samples span.

**What the truncation hid.** Reading the stored head beside the stored tail:

```
seed 35888   head: 0.0s:140   2.4s:2500  4.7s:2500  7.1s:2500  9.4s:2500  11.7s:2500
             tail: 32.0s:2500  35.0s:3741  38.3s:5000
```

The shape is **burst → plateau → completion**: 29–50% of the objects arrive within 1.7–2.4 s, the count then holds for ~30 s — at 2,500 and 2,000, not at zero — and the rest arrives in the final ~6 s. The published number is the last phase of three, and the phase that was thrown away is the interesting one.

**What replaces it.** A shape, not a rate. "Size decides step-vs-curve" is withdrawn; there is no measured curve to attribute to size. No transfer duration or throughput replaces it: fitting a line to the one study with full trajectories gives OLS residuals of 56–794 objects, and the two studies disagree about total repair time at the same divergence size (38–41 s vs ≥60 s) with corpus size and cadence both uncontrolled between them.

**What was NOT gained.** The analysis that found this also proposed `repair_s = dead_time + divergence / throughput` and **that model is not adopted** — the shape refutes it, since transfer begins immediately rather than after a dead time. Its mapping onto `weaviate_repair_clock/`'s ~32 s young-regime step is explicitly withdrawn: dead times measured from the restart are 13.5–45.4 s against #56's 31.08–33.43 s.

**The transferable lesson is about storage, not about Weaviate.** A harness that keeps the head and the tail of a trajectory will publish the tail as if it were the phenomenon. Six-and-three was chosen to bound file size and it cost a real finding *and* produced a wrong one — the plateau, which no study here had described, sat entirely inside the discarded middle. Filed as a consequence: decimate the full trajectory instead, which costs the same storage and preserves shape.

This is the second Weaviate repair claim withdrawn for the same underlying reason ([`weaviate-repair-two-path-withdrawal.md`](weaviate-repair-two-path-withdrawal.md)): a mechanism inferred from the shape of a measurement taken for another purpose. There the gap was a sampling artifact; here the ramp is.

**Where the withdrawal is recorded:** `weaviate_repair_window/README.md`, `research/README.md`'s index row for `weaviate_repair_window/`, and `weaviate_repair_trajectory/` (the analysis that found it).
