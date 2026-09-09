# Spec: analysis/weaviate-repair-trajectory

**Branch:** `analysis/weaviate-repair-trajectory`
**Issue:** #63
**Date opened:** 2026-09-09
**Status:** IN PROGRESS

<!-- The body below is issue #63, copied verbatim. The issue and this spec are
the same content at every stage; Results / Interpretation / Decision are
appended after the analysis runs. -->

---

### Type

Analysis (no new compute — every number comes from data already committed)

### Research question

Weaviate's async repair of a diverged replica is currently described on `main` as, at 5,000 objects, "a genuinely observed ramp" of **5.8–6.3 s** (`research/weaviate_repair_window/README.md`, re-argued 2026-09-06, and the corresponding index row). The `weaviate_dissociation/` study, merged as #61, committed the **full** healing trajectory for five chaos runs at the same 5,000-object divergence — and in those series the object count sits at its starting value for ~30–35 s and then climbs for **~20–25 s**, three to four times longer than the standing claim.

**Is the "5.8–6.3 s ramp at 5,000" a measurement of the transfer phase, or an artifact of `characterize.py` storing only the last three polls of each trajectory?** And does the healing trajectory decompose into a size-independent dead time plus a size-proportional transfer phase?

### Hypothesis

The ramp claim is a truncation artifact, and the true decomposition is

```
repair_s  =  dead_time  +  divergence / throughput
```

with `dead_time` ≈ 30–35 s (the quantity `weaviate_repair_clock/` calls the young regime's step) and `throughput` a roughly constant objects-per-second rate during the transfer phase. `characterize.py:143-144` keeps `traj[:6]` and `traj[-3:]` only, so at 5,000 objects the stored tail spans exactly three probe calls — and the probe's own cost grows with the id count. Under this hypothesis the "ramp" measured there is three probe latencies, not a transfer duration.

This also reconciles two claims on `main` that read as contradictory: repair being "independent of divergence size across 50→5,000" is what a decomposition dominated by a ~30 s dead time looks like at small sizes, where `divergence / throughput` is milliseconds.

### Null / alternative hypothesis

The null is that the two studies measured the same thing and the difference is not truncation: the dissociation runs' longer climb is caused by something other than the stored-sample limit — a larger total corpus (10,000 vs 5,000 objects), a 1.2 s probe cadence versus sub-second polling, or run-to-run variance. Concretely, the null stands if the repair_window 5,000-object runs' tail span cannot be shown to be probe-bound, or if the dissociation ramp rates are not separated from the tail-implied rates by more than their within-study spread.

A second, sharper null: if `dead_time` estimated from the dissociation series does **not** fall in the range `weaviate_repair_clock/` reports for the young regime (~30 s), the decomposition above is wrong and the two phases are not the ones named here.

### Motivation

Three reasons, in order of weight.

1. **A live claim on `main` may be wrong.** "Healing is a step at 50–500 objects but a ~6 s ramp at 5,000, so size decides step-vs-curve" appears in the experiment index and in the study README. If the 6 s figure is three probe calls, the sentence that rests on it is unsupported, and this project's own policy (`claim_corrections/`) is to record that, not to quietly leave it.
2. **It gives repair a mechanism, which no study here has.** `weaviate_repair_clock/` established *when* repair starts (age selects a regime; within a regime the clock runs from the restart) and explicitly declined to explain the step. A dead-time-plus-throughput decomposition says what the step is: the interval before transfer begins.
3. **Zero compute, per `AGENT_PIPELINE.md` query 3b.** The `completeness_series` in `weaviate_dissociation/results/dissociation.json` records every probe of every chaos run, and no study has ever read its shape — the merged analyser extracts only the crossing time (`recovery_s`) and the end value.

### Experimental design

No cluster runs. Two committed artifacts are re-read:

- `research/weaviate_dissociation/results/dissociation.json` — 5 chaos runs and 5 controls, divergence 5,000, corpus 10,000, full `completeness_series` at ~1.2 s realized cadence, 60 s window.
- `research/weaviate_repair_window/results/sizes.json` — 10 runs at 50 / 500 / 5,000 / 20,000, with `repair_s`, `samples_in_window`, `trajectory_head` (6 samples) and `trajectory_tail` (3 samples).

Per dissociation chaos run, segment the series into a **plateau** and a **transfer phase** by a rule fixed here: the transfer phase begins at the last sample whose count equals the run's first successfully-probed count, and ends at the last observed sample. Fit throughput by ordinary least squares over the transfer-phase samples and report the fit residual, so a curve masquerading as a line is visible rather than assumed away.

For `sizes.json`, compute the mean inter-poll interval as `repair_s / samples_in_window` at each size and compare the 5,000-object tail span against three times that interval. If the tail span is of the order of three polls, the ramp figure is probe-bound.

### Metrics

Registered as estimands, not thresholds:

- **Transfer-phase throughput** (objects/second) per run, with its OLS residual, compared between studies by an exact rank test on run-level rates.
- **Plateau duration** (seconds from first successful probe to transfer start) per dissociation run, compared against the young-regime step in `weaviate_repair_clock/`.
- **Tail span versus poll interval** in `sizes.json` at 5,000 — this ratio decides the truncation question, and it is computed from committed fields rather than a cut-off chosen now.

The decision rests on the third metric. The first two characterize; they do not adjudicate.

### Baselines / controls

The dissociation study's **no-chaos control arm** is committed alongside the chaos arm and has no divergence to repair, so it is the noise floor for the probe series itself: a control run's count should be flat across the window. If a control shows a comparable climb, the segmentation is detecting probe behaviour rather than repair and the analysis is void.

The 50- and 500-object runs in `sizes.json` are the size control for the decomposition: under the hypothesis their transfer phase is milliseconds and their `repair_s` is essentially all dead time.

### Expected outcomes

- **(a)** Tail span ≈ 3 poll intervals, and dissociation throughput far below the tail-implied rate → the 6 s ramp is a truncation artifact; the claim on `main` is corrected and the decomposition stands.
- **(b)** Tail span ≫ 3 poll intervals → the repair_window ramp was genuinely measured and the between-study difference is corpus size or cadence, not truncation. The claim survives and the dissociation runs become the anomaly.
- **(c)** Transfer phase is not linear (large OLS residual) → "throughput" is the wrong model even if the truncation finding holds; report the shape, do not fit a rate to it.
- **(d)** Plateau duration inconsistent with the clock study's young-regime step → the two phases are real but are not the ones named here, and the mapping onto `weaviate_repair_clock/` is withdrawn.
- **(e)** Underpowered — 5 runs against 2 — so the rank test separates nothing. Then the truncation ratio is reported alone and the throughput comparison is labelled underpowered, not null.

### Interpretation plan

Outcome (a) requires editing `research/weaviate_repair_window/README.md`, the experiment index row, and adding a `claim_corrections/` entry — a withdrawn claim here is recorded, not silently overwritten. It does **not** establish that throughput is constant across systems, across sizes above 5,000, or under load; it establishes it for one divergence size on one pinned image digest.

Outcome (b) is equally worth having and more interesting for the dissociation study, because it would mean that study's 60 s window is marginal for a reason its own writeup did not identify.

Outcomes (c) and (d) narrow the issue to the truncation question alone, which is answerable regardless.

No outcome licenses a claim about *why* the dead time exists. `weaviate_repair_clock/` deliberately reports the step without a mechanism; this analysis adds resolution to the same observation, not a cause.

### Confounds considered

- **Corpus size differs between the studies** (10,000 vs 5,000 total objects). The probe returns the replica's own ids, so its cost — and therefore the realized cadence — grows with corpus size. This is a real alternative explanation for a slower observed climb and cannot be eliminated from committed data; it can only be bounded by reporting each study's realized cadence alongside its rates, and any conclusion must say so.
- **Polling load may perturb repair.** The `sizes.json` runs poll with `poll_sleep = 0` (8,532 samples in 44.7 s); the dissociation runs poll at ~1.2 s. If polling steals resources from repair, the heavily-polled study should be *slower*, which is the opposite of what is observed — so this confound works against the hypothesis rather than for it, and that asymmetry is worth stating.
- **Right-censoring.** Two of the five dissociation chaos runs never completed repair inside the window (`completeness_end` 0.60 and 0.174). Their transfer phases are observed but truncated at the window edge; they may enter a throughput estimate but must never enter a completion-time estimate.
- **This question was formed by looking at the data.** The trajectory transitions were dumped before this issue was written — which is what `AGENT_PIPELINE.md` query 3b instructs. So this is registered as an **analysis**, not a confirmatory test: the segmentation rule and metrics above are fixed before any fit runs, but the ramp discrepancy is the observation that prompted the question, not a prediction that preceded it. Nothing here should be reported with the epistemic weight of a pre-registered sweep.

### Before submitting

- [x] Checked `README.md`'s open research questions and `research/DECISION_LOG.md` — the trajectory *shape* appears in neither; the closest entry is the 2026-09-06 re-argument that produced the claim this issue challenges.
- [x] One answerable question: is the 5,000-object ramp figure probe-bound?
