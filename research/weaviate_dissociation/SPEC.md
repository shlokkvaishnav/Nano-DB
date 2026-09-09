# Spec: does `completeness` heal on Weaviate while `index_recall` does not?

**Branch:** `experiment/weaviate-dissociation`
**Date opened:** 2026-09-06
**Status:** COMPLETE — **outcome (i): both axes heal.** The dissociation is met in 0 of 5 seeds once `index_recall` is scored over the full corpus and compared against a corpus-matched control. The earlier −0.5 "graph damage" was dilution. A 60 s horizon; the comparison is **paired** and only **3 of 5** seeds are corpus-matched (two right-censored ended with fewer objects than their controls), so the negative rests on n = 3 with a deficit bound of ~0.02 — review round 2.

Issue: closes #54. Body copied verbatim below (per `research/AGENT_PIPELINE.md`'s implementer instructions — this is the issue text unmodified, not a paraphrase).

---

## Research question

On a system with **real anti-entropy**, does `completeness` heal after node-kill chaos while `index_recall` does not — the dissociation this project's central argument predicts?

## Hypothesis

Within one observation window after chaos stops, a diverged Weaviate replica's `completeness` returns to 1.0 while its `index_recall` does not return to its own pre-chaos baseline.

## Null / alternative hypothesis

(i) **Both heal.** Weaviate's repair rebuilds enough of the index as a side effect of re-inserting objects that graph quality recovers too. Then the dissociation is false on the one system that could show it, and the project's structural argument is substantially weakened — this must be reported at least as prominently as a positive result.

(ii) **Neither heals** within the window — repair did not fire, or the window was too short. Distinguishable from (i) by the `completeness` series itself; if the step never happens, this is an instrument or timing failure, not a finding.

(iii) **`index_recall` cannot be compared** across the two snapshot moments at useful precision. If snapshot noise exceeds the effect, the design cannot answer the question and says so.

## Motivation

This is the experiment the project has been building toward, and the only one that tests its field-level claim rather than a within-system one.

`RELATED_WORK.md` §4 argues structurally: every production anti-entropy mechanism operates on **exact object identity**, and two correct HNSW graphs over identical data differ bit-for-bit, so object-level repair *cannot* be pointed at the index. The prediction is sharp: after chaos, a replica's missing objects come back and its graph quality does not.

Neither system tested so far can decide it. nano-db has **no** anti-entropy by design, so its 0% healing is close to definitional — which is why `RELATED_WORK.md`'s novelty verdict demoted that result in #55. Qdrant heals both axes at 180 s. Weaviate is the only one of the three where object-level repair exists and can be watched separately from graph quality.

Four method studies (#41, #43, #46, #48) exist solely to make this runnable, and all four are closed.

## Experimental design

**Every parameter comes from a measurement, not a guess.**

- **Topology:** 1 shard × 3 replicas, `weaviate@sha256:4d2eceef…` (digest-pinned, #46), with `verify_class()` asserting factor 3 / 1 shard so a stale auto-schema class cannot silently make it sharded-not-replicated (#46's hazard).
- **Divergence size: 5,000 objects.** #48 found repair is size-independent, so size is free to choose on other grounds — and at 5,000 the completeness transition is a genuine ~6 s ramp rather than a sub-200 ms step, which a 1 s cadence resolves into 5–6 points. Below 500 the transition is faster than the probe and only endpoints are observable.

  *(Note added 2026-09-09, #63 — the design rationale above is left as written because this is a pre-registration and records what was believed when the size was fixed. The "genuine ~6 s ramp" it cites has since been **withdrawn**: it was the span of the last three stored samples at a probe-bound ~2 s cadence. The choice of 5,000 is unaffected — the real shape, burst → ~30 s plateau → completion, is still resolvable at a 1 s cadence — but the stated reason for it is not what the data showed.)*
- **Cadence: 1 s**, sampling `objects_present_ids` per replica (#43: 20/20 probes at 5 s with peers up, proven local; #46: per-id set, 0 false positives).
- **Observation: ≥60 s** after the last kill, which outlasts the longest repair latency seen in #48 (~52 s).
- **Id set ≤15,000 per probe call** — #48 found base64 ids in the URL work at 15,000 and fail *silently* at 20,000. At 5,000 there is ample margin.
- **`index_recall`:** snapshot before chaos and after the observation window, via the isolation probe (#41), accepting ~10 min of node health per snapshot and therefore no sampling.
- **Seeds: 5**, with the exact two-sided Mann-Whitney at the 5v5 floor.

**Ground truth for `index_recall` is computable locally and needs no server support.** The corpus is generated from a seeded RNG, so the exact top-k over *the subset the replica actually holds* — which `objects_present_ids` reports — can be computed with numpy. `index_recall` is then the replica's ANN result against exact search over its own data, holding data constant, which is the definition this project already uses on nano-db and Qdrant.

~~**One parameter is blocked.** The observation window must be timed from the right origin, and whether Weaviate's repair clock is anchored to the write or the restart is exactly what **#56** is measuring right now. Until it reports, this spec fixes the window as *"≥60 s measured from the later of (write, restart)"*, which is safe under either answer at the cost of a longer run. If #56 settles the anchor, this is tightened and the amendment dated.~~

**Unblocked by #56 — see Amendment 1 (2026-09-06) below.** The window is now timed from the **restart**, and a `t = 0` completion is recorded as **censored**, not as an instant recovery.

## Metrics

**Primary: the dissociation** — per seed, whether `completeness` on the victim returns to 1.0 within the window **and** post-chaos `index_recall` is outside its own pre-chaos range.

Secondary: time-to-completeness-recovery; the `index_recall` delta with its snapshot-to-snapshot noise stated.

No threshold is registered on a continuous quantity. "Returns to 1.0" is exact set equality on ids, not a cutoff, and the `index_recall` comparison is against a measured no-chaos noise floor rather than a fixed number — per `SPEC_TEMPLATE.md`'s rule after #48 step 2c.

## Instrument characterization

Everything below is measured, from the four closed method studies:

- **Per-replica reads work without perturbing peers** (#43): 20/20 probes at 5 s cadence, provably local — refuses when the node is down, returned 0 bytes while peers returned 31,190.
- **Presence is a set, not a byte count** (#46): 8/8 checks against a constructed expectation, 0 false positives on never-written ids.
- **Repair is timing-determined and size-independent** (#48) across 50→5,000, so size is free and 5,000 is chosen for the resolvable ramp.
- **The probe's hard ceiling is ~15,000 ids**, found by hitting it — it fails *silently*.
- **`_search` is unavailable on the internal port** (#43, 415 across 12 content types), which is why `index_recall` is snapshot-only and the experiment is asymmetric by construction.
- **Writes at `consistency_level=ONE` with a node down occasionally take minutes** (#56, observed mid-run). The harness must time the outage from realized timestamps, not requested ones, and must not assume the write returns promptly.

## Baselines / controls

- **A no-chaos run per seed** is required and is what makes the `index_recall` comparison interpretable: two isolation-probe snapshots the same interval apart, with no chaos, give the snapshot-to-snapshot noise floor. Without it any post-chaos difference is uninterpretable, since the snapshots are separated in time on a live corpus.
- **A healthy-replica control** — a node never killed, probed alongside the victim in the same runs.
- **The #46 topology check** must pass before any run counts.

## Expected outcomes

(a) `completeness` recovers, `index_recall` does not → the dissociation holds on the one system with real anti-entropy; the project's strongest possible result. (b) Both recover → outcome (i); report as prominently, and revise `RELATED_WORK.md` §4. (c) The `index_recall` delta sits inside the no-chaos noise floor → outcome (iii); the instrument bounds the question rather than answering it. (d) Repair never fires → outcome (ii), an apparatus failure, recorded as such.

## Interpretation plan

Whatever the outcome, the write-up carries **the asymmetry of the instrument in the claim itself**: the data axis is a 1 s time series, the graph axis is two endpoints. A dissociation shown between a series and a pair of snapshots is weaker than one between two series, and must never be written as though both were watched continuously.

(a) updates the README's ESTABLISHED box with that qualifier attached. (b) or (c) updates `RELATED_WORK.md` §4 and the DO-NOT-CLAIM list, and is recorded in `DECISION_LOG.md` as a negative result with its design intact.

## Confounds considered

**The snapshot cost perturbs the thing measured.** The isolation probe leaves a node UNHEALTHY for ~10 minutes (#41), so the post-chaos `index_recall` snapshot cannot be taken until the completeness window has closed — the two axes are measured at *different moments*. This is the central design compromise and must be stated in the result, not buried.

**Write duration is outage duration.** Writing 5,000 objects while the victim is down makes the divergence and the outage the same interval (#48), and #56 found the write can occasionally take minutes.

~~**The repair clock's origin is unresolved** (#56, running) — hence the conservative window above.~~ **Resolved, partially, by #56 — see Amendment 1.** Within a regime the clock runs from the restart; *which* regime is selected by divergence age. The window is anchored accordingly and the realized age is recorded per run so the regime is known rather than assumed.

**Undocumented API.** The probe depends on an internal endpoint of one pinned build; a Weaviate upgrade invalidates the instrument, not just the numbers.

**One host, one topology, 5 seeds** — the same scale limit every study here carries.

## Amendment 1 (2026-09-06, before any run): the window is anchored to the restart, and `t = 0` is censored

The blocked parameter above is unblocked. #56 (PR #59) has reported, and this
amendment applies its answer. **No run of this spec has happened yet** — the
harness has never been executed against a cluster and `results/` holds no data —
so nothing here is a post-hoc choice. It is dated anyway, because a window
origin changed after a dependency reported is exactly the kind of edit that has
to be legible later.

### What #56 found, and the part of it this spec depends on

#56 asked whether repair is anchored to the write or the restart and returned
**neither, cleanly**. Its aggregate statistic said write-anchored — CV(`repair_s`)
0.766 against CV(`age + repair`) 0.132, corr(age, `repair_s`) = −0.942 — and
disaggregating by divergence age **reverses** it: within either regime `repair_s`
is flat (spreads of 2.34 s and 1.45 s across 13 s and 8 s of age variation) while
`age + repair` tracks age at +0.99. The aggregate slope is produced entirely by a
step *between* regimes. Simpson's paradox.

Two consequences, and this spec needs both:

1. **Within a regime, the clock runs from the restart.** So the window is timed
   from the restart.
2. **Divergence age selects the regime.** Below ~15 s the victim waits ~32 s;
   above ~30 s it reconciles in ~2 s. The threshold is bracketed to (15 s, 30 s)
   and was never sampled.

**This experiment sits in the young regime by construction.** The harness restarts
the victim immediately after the divergence write returns, so realized age is
≈ 0 s — younger than any age #56 sampled, and its nearest observations (realized
ages 0.126 s and 0.186 s) gave `repair_s` of 30.4 s and 19.4 s. Expect the ~32 s
wait, not the ~2 s one.

Provenance note, so this is not read as firmer than it is: PR #59 is at
`stage:changes-requested` as of this writing. Its five review findings concern a
step-1 number that disagrees with its own analyzer output, an unscoped "absence is
irrelevant" claim, run accounting, and the disaggregation not being reproducible
from committed code. **None of them bear on the two consequences above** — review
round 1 recomputed the regime split independently from the committed raw JSON and
reproduced it exactly, with the two clusters disjoint (31.08–33.43 s and
0.82–2.27 s, nothing between). If a later round overturns the split itself, this
amendment is void and the window returns to `max(write, restart)`.

### The three changes

**(1) Origin: the restart, not `max(write, restart)`.** `origin = t_restart`.
Under the old rule a write that takes minutes pushes the origin past the restart
and the window opens *after* repair has already fired — the failure that voided
#24 and #9, arriving from the direction #56 warned about. The realized divergence
age `age_s = t_restart − t_write` is recorded per run, and a run whose age lands
above 15 s is reported as **out of the young regime** rather than pooled with the
others. #56's own methodological finding is that pooling across a step is what
produced its wrong answer; this spec will not repeat it at n = 5.

**(2) A `t = 0` completion is censored, not instant.** #56's probe-perturbation
check was voided by exactly this: its three slow-poll runs report `repair_s = 0.000`
with `first = 50/50` — the victim already held every id on its *first* sample. That
does not show repair was instantaneous. It shows the first sample landed after
convergence, and at a 1 s cadence the two are indistinguishable.

This spec plans that same 1 s cadence, so it inherits the same blind spot. The
harness therefore records the first sample's offset from the origin
(`first_sample_offset_s`), and:

- if the **first** sample is already complete, `recovery_s` is **left-censored**:
  recorded as `censored: "left"` with the observed bound, and reported as
  *"recovered at or before t = <offset>"*, never as a recovery time;
- if the **last** sample is still incomplete, it is **right-censored**
  (`censored: "right"`) — recovery did not occur within the window;
- only a run with an incomplete first sample and a complete later one yields an
  uncensored `recovery_s`.

Censored runs are **kept, not dropped**, and the censoring status is reported
alongside every recovery figure. This matters for the primary metric: the
dissociation asks whether `completeness` returns to 1.0 within the window, and a
left-censored run answers that **yes** — the censoring bounds *when*, not
*whether*. Secondary time-to-recovery is the quantity that degrades to a bound.

**(3) The ≥60 s window stands, and here is its actual margin.** It is not
generous. The slowest repair observed anywhere in this project is **53.3 s**
(#56, `short6`), against a 60 s window — 6.7 s of margin, not the comfortable
outlasting the original line implied. It is kept rather than lengthened because
the expected young-regime wait is ~32 s and every young-regime observation across
#48 and #56 falls in 19.4–33.4 s, so 60 s covers the expected behaviour with
roughly 2× margin and the tail case with little. Right-censoring (change 2) is
what makes that honest: a run that does not converge by t = 60 s is recorded as
censored rather than as a failure to heal, and outcome (ii) — "neither heals" —
may not be concluded from a right-censored series.

### What this does not fix

The threshold between the regimes is unlocated, so "this experiment sits in the
young regime" rests on realized age ≈ 0 s being far below a bracket of (15 s, 30 s),
not on knowing where the boundary is. And #56's short-outage bimodality (six runs
at ~0.02 s against four at 43–53 s at identical parameters) is unexplained; if it
reaches this experiment it will appear as a mixture across seeds, which n = 5
cannot resolve. Both are stated here so a mixed result is not later reframed as
a surprise.

## Amendment 2 (2026-09-06, after a discarded partial run): the graph axis was not measuring graph quality

The first attempt at the sweep was **stopped 12 minutes in and its data
discarded**, because the very first number it produced was impossible.

**What was seen.** The no-chaos control for seed 20260900 — an undisturbed,
healthy replica, nothing killed, nothing diverged — reported
`index_recall = 0.23`, identical before and after. A healthy replica scored
against exact search over its own data must sit near 1.0; that is what
"the graph is intact" means. 0.23 is not a finding about Weaviate, it is an
instrument returning noise.

**Why this is not an outcome-dependent amendment.** What was read was a
*control*, not a comparison: no chaos condition had completed, and nothing about
`completeness`, the dissociation, or the difference between conditions was
observed. The amendment changes how the instrument computes a number, on
evidence that the number was invalid on its own terms — a healthy replica cannot
score 0.23 — not on evidence about which way the result came out. The run's
partial output is discarded rather than kept, so nothing measured under the
broken instrument reaches the analysis.

### Defect 1 — ground truth used the wrong distance metric

`exact_topk()` ranked neighbours by **L2** (`np.linalg.norm(sub - query)`) while
the class's HNSW index is configured with **`distance: cosine`**, read from the
live schema:

```
vectorIndexType : hnsw
distance        : cosine
```

So `index_recall` compared Weaviate's cosine-nearest-10 against exact
L2-nearest-10 — two different neighbour sets. The metric was never a measure of
graph quality; it measured how often the cosine and L2 orderings happen to
agree on random-normal vectors.

Fixed by reading `distance` from the live schema and computing ground truth with
it, refusing to run on a metric it cannot reproduce. Derived from the artifact
rather than duplicated, which is the fix #17's review applied to the kill
scheduler's mirrored `FIXED_DOWN_S`: a constant that can silently disagree with
reality is a defect even when it is currently correct.

### Defect 2 — the class was shared scratch and had never been cleared

The decisive one. `RrdVector` is written by **every** Weaviate study — #41, #43,
#46, #48, #56 — and nothing has ever emptied it. At the start of the run it held:

```
count: 14,200        against a per-run corpus of 5,000
```

`index_recall` scores the replica's ANN answer against exact search over *the
ids this run wrote*. With 9,200 objects from older studies also in the index,
Weaviate's nearest ten are drawn from the superset while the ground truth is
drawn from the subset. The two disagree for reasons that have nothing to do with
the graph, and the disagreement grows with every study that ever ran.

Fixed by deleting and recreating the class at the start of **each run**, so it
holds exactly that run's corpus. Recreating is safe: the class is scratch
infrastructure and every study that wrote into it has its own committed
`results/`, so no evidence lives there.

### The fixes, verified against the live cluster before restarting

| condition | `index_recall`, healthy undisturbed replica |
|---|---|
| as originally written (14,200-object class, L2 truth) | **0.23** |
| class reset to 5,000, still L2 truth | 0.32 |
| class reset to 5,000, cosine truth — **both fixes** | **0.9750** |

Both fixes are necessary; neither alone is sufficient. 0.9750 is what an intact
HNSW graph is supposed to look like, and it is now the baseline against which
any chaos-induced drop is measured.

### Defect 3, the one that let the other two through — there was no positive control

This is the part worth carrying to other specs.

The Baselines/controls section required a **no-divergence control** — and got
one, for `completeness`: "the victim is complete on its first answer." Nothing
anywhere required the **graph** axis to be shown reading ~1.0 when nothing is
wrong. The Instrument characterization section characterizes the completeness
probe in detail (cadence, locality, per-id decoding, the 15,000-id bound) and
inherits `index_recall` from #41 without ever asking what it returns on a
healthy replica.

So the spec could pass its own pre-flight checks — topology verified, probe
proven local, ids decoded per-id — with the graph axis measuring nothing at all.
Every check was on the axis that was already working.

The harness now asserts `index_recall >= 0.90` on the no-chaos control and
**aborts the run** if it fails, naming the class count and the distance metric
in the message, so the same failure can never again be discovered by reading a
result. A measurement axis with no positive control is an axis that cannot fail
visibly, and this experiment's central claim is a *dissociation* — one axis
moving while the other does not — which a silently dead axis would have produced
for free.

**This is why the run is worth its four hours only now.** Had it completed as
written, it would have reported `index_recall` failing to heal — the
pre-registered hypothesis — from an instrument that could not have shown healing
under any circumstances.

## Results

**Outcome (i): BOTH AXES HEAL. The dissociation does not hold on Weaviate.** 10 of 10 runs, no aborts, after four amendments and three discarded sweeps.

### The corpus-matched control is the whole story

| seed | control before → after | control drift | chaos before → after | chaos delta | `completeness` | censored | recovery |
|---|---|---|---|---|---|---|---|
| 20260900 | 0.970 → 0.930 | −0.040 | 0.975 → 0.955 | **−0.020** | 0.60 | right | — |
| 20260901 | 0.980 → 0.945 | −0.035 | 0.980 → 0.945 | **−0.035** | **1.00** | none | 56.95 s |
| 20260902 | 0.965 → 0.930 | −0.035 | 0.965 → 0.930 | **−0.035** | **1.00** | none | 60.55 s |
| 20260903 | 0.980 → 0.915 | −0.065 | 0.975 → 0.925 | **−0.050** | **1.00** | none | 40.08 s |
| 20260904 | 0.980 → 0.930 | −0.050 | 0.990 → 0.970 | **−0.020** | 0.17 | right | — |

**In no seed does chaos cost more `index_recall` than the corpus-matched control costs.**

**The comparison must be paired, and only three seeds qualify** (review round 2). The two right-censored seeds ended with *fewer* objects than their controls — 9,500 and 6,748 against 10,000 — and a smaller corpus scores *higher* recall, which is the very effect the matched control exists to neutralise. Including them biases the test toward the conclusion, so they are excluded rather than counted as evidence:

| seed | control delta | chaos delta | paired difference |
|---|---|---|---|
| 20260901 | −0.035 | −0.035 | **0.000** |
| 20260902 | −0.035 | −0.035 | **0.000** |
| 20260903 | −0.065 | −0.050 | **+0.015** |

Mean **+0.005**; a negative value would mean chaos lost more. **In two of three the arms are identical.** That is a sharper statement than a null p-value, and it rests on **n = 3**, not 5.

*(The unpaired figure — baseline 0.9300 vs chaos 0.9450, p = 0.3016 — is printed by the analyser as reference only and labelled contaminated. It should not be quoted as the result.)*

**The pre-registered dissociation is met in 0 of 5 seeds.**

In the three seeds where repair completed — `completeness` exactly 1.0, victim holding all 10,000 objects — the chaos arm's `index_recall` landed on its control's value **to the third decimal in two of them** (−0.035 vs −0.035, twice). Same corpus, same size, same measurement: the graph is as good after chaos-plus-repair as it is after the same writes with no chaos at all.

### Where the entire original effect went

The −0.05 drop that remains is **corpus size, not damage**, and the control proves it: doubling the index from 5,000 to 10,000 objects costs 0.035–0.065 of recall with no chaos anywhere. The first sweep's "0.0000 drift" control could not see this because it was not corpus-matched.

The original −0.5 "graph damage" was dilution (Amendment 3), verified on one replica at one moment: **0.495 → 0.935** when ground truth was corrected to cover the full corpus.

### The primary metric as written was also wrong

Recorded because the correction changes the answer. The registered test — *"post-chaos `index_recall` is outside its own pre-chaos range"* — is satisfied by the **control** too, once both arms end at 10,000 objects, because the corpus-size effect alone pushes it outside that range. Scored that way the data reports 3 of 5 "dissociations" that are nothing but corpus size.

The test that answers the question is **paired against the matched control**: does chaos lose *more* than the control loses? That is what `analyze_dissociation.py` now computes, and the answer is no, in five seeds out of five.

### What the design still cannot say

Two of five seeds are **right-censored** — repair had reached 60% and 17% when the window closed. Observed recovery is **40.08–60.55 s**, against ~38 s in the pre-Amendment-4 sweep, so **the 60 s window is now marginal rather than comfortable** and one uncensored seed finished 0.55 s past nominal. The negative rests on three uncensored seeds. A longer window is required before this is a clean five.

And this remains a **60 s horizon**: nothing here observes what either axis does at 180 s.

## Interpretation

**The project's central structural prediction is not confirmed on the one system that can test it.**

`RELATED_WORK.md` §4 argues that anti-entropy operates on exact object identity, that two correct HNSW graphs over identical data differ bit-for-bit, and therefore that object-level repair *cannot* restore graph quality. The sharp prediction was: missing objects come back, graph quality does not.

On Weaviate, at 60 s, **both come back**. In the seeds where repair completed, graph quality is indistinguishable from a no-chaos run over the same corpus.

The structural argument is not thereby refuted — it says repair cannot *target* the index, which remains true. What the data shows is that it does not need to: re-inserting the objects appears to rebuild enough of the graph as a side effect that no residual deficit is measurable at this precision. That is exactly null hypothesis **(i)** as pre-registered — *"Weaviate's repair rebuilds enough of the index as a side effect of re-inserting objects that graph quality recovers too"* — and the spec commits to reporting it at least as prominently as a positive result.

**This does not generalize to the other systems.** nano-db has no anti-entropy, and its divergence is committed and reproduced (#53). Qdrant's replica-level `index_recall` divergence stands (#31), and heals at 180 s (#37). What falls is the claim that a system *with* real anti-entropy would show the data axis healing while the graph axis does not.

**Precision bounds the claim, and the bound is exact.** Each snapshot scores 20 queries × top-10 = **200 ground-truth items**, so `index_recall` moves in steps of **1/200 = 0.005** — every observed value is a multiple of 0.005, which confirms it. The three paired differences are 0.000, 0.000 and +0.015, all neutral or favouring chaos, so any real chaos-specific deficit is **under ~0.02**.

It does **not** exclude a deficit below that quantisation floor. "Both heal" means "no deficit resolvable at 0.005 granularity across three matched pairs", not "provably identical". Raising the query count per snapshot lowers the floor directly at no cluster cost, and is the cheapest available improvement.

## Decision

**MERGE**, as outcome (i), and revise `RELATED_WORK.md` §4 and the README's HYPOTHESIS box to match.

**What must not be claimed.** That the dissociation is refuted in general — one system, one build, 60 s, n = 5, two seeds censored. That Weaviate's repair rebuilds the graph *by design* — the mechanism is not observed, only its effect. That the earlier −0.5 result meant anything — it was dilution, and it is retired.

**Consequences to file.**

1. `analysis/*` — **revise `RELATED_WORK.md` §4 and the README HYPOTHESIS box.** The dissociation moves from "predicted, untested" to "tested on one system with real anti-entropy and not observed."
2. `experiment/*` — **re-run with a 180 s window.** Recovery now runs to 60.55 s, so the current window is marginal and two seeds censored; a longer window also answers the horizon question #37 raised.
3. `method/*` — **`index_recall` should be measured under a filtered search restricted to the base set.** The two axes are entangled through corpus size: the chaos arm's final corpus size *is* the dependent variable, so a corpus-matched control only works when repair completes. Filtering removes the coupling.
4. `method/*` — **`internal_api.shard_name()` is not scoped to a class** (Amendment 4a), and **`characterize.write_objects()` reports success on a batch in which every object failed** (Amendment 2b). Both are shared code used by #43/#46/#48/#56.
