# Spec: does #54's negative survive a 180s horizon and a 5x finer instrument?

**Branch:** `experiment/dissociation-180s`
**Issue:** #69
**Date opened:** 2026-09-10
**Status:** COMPLETE — outcome (a). The negative survives at 180 s on **5 corpus-matched pairs, none censored**, at 0.001 resolution; mean paired difference **−0.0002**, deficit bound **~0.01**. #54's "identical arms" were a quantisation artifact. The bound is now **variance**-limited, not resolution-limited.

<!-- The body below is issue #69, copied verbatim. The issue and this spec are
the same content at every stage; Results / Interpretation / Decision are
appended after the run. -->

---

### Type

Experiment (re-run of #54 at a longer horizon and a finer instrument)

### Research question

#54 reported **outcome (i): both axes heal** — but on a 60 s horizon, at n = 3 corpus-matched pairs, with a deficit bound of only ~0.02. Two of five seeds were right-censored because repair did not finish inside the window.

**Does the negative survive a 180 s horizon and a 5× finer instrument, and does it then rest on 5 matched pairs instead of 3?**

### Hypothesis

The negative holds and tightens. Concretely: all five seeds complete repair inside 180 s (observed recovery was 40.08–60.55 s, so 180 s is ~3× the slowest), giving 5 corpus-matched pairs; and at 100 queries × top-10 the resolution floor drops from 0.005 to 0.001, so the excluded deficit tightens from ~0.02 toward ~0.005.

### Null / alternative hypothesis

(i) **A deficit appears between 60 s and 180 s.** Then the 60 s result was a horizon artifact in the *opposite* direction to #37's — early enough to catch the graph mid-rebuild rather than too early to see damage. This would be a genuine positive and must be reported as prominently as #54's negative.

(ii) **A deficit appears once the floor drops to 0.001.** Then #54's "both heal" was a resolution artifact: a real deficit between 0.001 and 0.005 was invisible. This is the outcome that would most change the project's position, and it is the reason the query count is being raised.

(iii) **Seeds are still censored at 180 s.** Then repair is slower than the 40–60 s range suggests, which is itself a finding about the repair-clock work (#56) and would need explaining before the dissociation question can be answered.

### Motivation

`DECISION_LOG.md` (2026-09-10) pre-registers the pivot trigger, and condition (3) depends directly on this run: *"the 180 s re-run leaves the negative unchanged and raising queries-per-snapshot fails to bring the exclusion bound below ~0.01."* This experiment is one of the two inputs to a project-level decision, and it is the cheaper one — it runs on the existing laptop and needs no cluster access.

It also discharges consequence 1 of #54's own Decision, which named the 180 s re-run as "the most valuable follow-up in the project", and the review's note that raising query count is "the cheapest available improvement" and was still unimplemented.

### Experimental design

Identical to #54 in every respect except two **instrument** parameters:

- `OBSERVE_S`: 60 → **180**
- `N_QUERIES`: 20 → **100**

Everything else is unchanged and deliberately so: same corpus-matched control (both arms end at 10,000 objects), same restart-anchored window, same left/right censoring, same `docker pause` isolation, same fresh class per run, same 5 seeds.

**Two parameters change at once, which is normally a do-not-merge condition, so the justification is stated rather than assumed.** They are independent and neither confounds the other:

- `OBSERVE_S` affects whether repair *completes* inside the window — censoring, and therefore how many pairs are corpus-matched. It cannot affect the value of `index_recall` at a given corpus size.
- `N_QUERIES` affects only the **resolution** of `index_recall`. It changes what the ruler can see, not what the system does.

Neither is an experimental variable in the sense of the hypothesis; both are instrument parameters, and the experimental manipulation (chaos vs no-chaos) is untouched.

### Metrics

Unchanged from #54, so the two runs are directly comparable:

- **Primary:** per seed, whether `completeness` returns to 1.0 within the window **and** the chaos arm loses *more* `index_recall` than its corpus-matched control. Paired, matched seeds only.
- Secondary: the exclusion bound; the number of corpus-matched pairs; censoring status per seed; measured snapshot duration.

No threshold is registered on a continuous quantity.

### Instrument characterization

- **Resolution is computed, not assumed.** `index_recall` quantises at `1 / (N_QUERIES × K)`. The harness now records `k` alongside `queries` in each snapshot so the analyser derives the floor rather than mirroring the constant — the defect #17's review found in the kill scheduler.
- **The positive control is already in place** (#54 Amendment 2): the run aborts if a healthy, undisturbed replica scores below 0.90, and aborts if the control snapshot returns nothing at all.
- **Known cost, to be measured:** 100 queries is 5× the round trips inside the isolation window, so the snapshot takes longer and the peers stay paused longer. At `docker stop` a snapshot cost 199 s; `docker pause` (Amendment 2c) reduced it to seconds. The new snapshot duration must be recorded, because if it grows materially it delays the start of chaos after the *before* snapshot.
- **Repair timing from #56:** realized divergence age will be ~0 s, placing every run in the young regime (~32 s expected). Observed in #54: 40.08–60.55 s.

### Baselines / controls

Unchanged: the corpus-matched no-chaos arm per seed, and the #46 topology check before any run counts. The negative control on the analyser — shuffled labels report chance — must still pass.

### Expected outcomes

- **(a)** All 5 seeds uncensored, 5 matched pairs, bound tightens below ~0.01, negative unchanged → #54's result is confirmed at a longer horizon and a finer instrument. Pivot-trigger condition (3) does **not** fire on this input.
- **(b)** A deficit appears → null (i) or (ii); a positive, reported at least as prominently as the negative it overturns.
- **(c)** Seeds still censored at 180 s → null (iii); report as a repair-timing finding and treat the dissociation question as still open.
- **(d)** Apparatus failure (this leg has produced three) → recorded as such, not as a result.

### Interpretation plan

Outcome (a) updates `PIVOT_MEMO.md` §3 with the tightened bound and feeds condition (3) of the pivot trigger. Outcome (b) requires `RELATED_WORK.md` §4 to be revised *again*, in the opposite direction, and the #54 index row corrected — with a `claim_corrections/` entry, since that would retire a claim this project published nine days earlier.

No outcome licenses a claim about *why* the graph does or does not recover. The mechanism is unobserved and stays that way.

### Confounds considered

**Longer isolation windows.** 100 queries means peers are paused ~5× longer per snapshot. For the *after* snapshot this is harmless (the observation window has closed). For the *before* snapshot it delays chaos onset slightly. Measured and reported rather than assumed negligible.

**Comparability with #54.** Changing the instrument means the two runs' `index_recall` values are not directly comparable at the same precision — the new run resolves finer. Comparisons are made on the *paired differences*, not on raw values.

**The Weaviate leg is fragile.** Raft quorum was lost twice, and async repair was stalled permanently once by batch deletes. The fresh-class-per-run design (Amendment 4) addresses both, but a rebuild costs about an hour and should be expected rather than treated as failure.

**One host, one build, 5 seeds.** Unchanged from #54; the n = 5 ceiling is what PARAM Shavak access is meant to lift later.

### Before submitting

- [x] Checked `README.md`'s open questions and `DECISION_LOG.md` — this is consequence 1 of #54's Decision and an input to the 2026-09-10 pivot trigger.
- [x] One answerable question: does the negative survive a longer horizon and a finer instrument?

## Amendment 1 (2026-09-11, after two discarded attempts): stale classes were silently reused

Two sweeps were discarded before the one reported above. Both were apparatus failures, and the second is the more interesting.

**Attempt 1 — a replica-placement race, caught by its guard.** Five of ten runs aborted with `class placed on 2/3 replicas`. Cause: a chaos run restarts the victim at the end, and the next run created its class before that node had rejoined **schema membership**. HTTP-ready is not the same as being in the replica set — node2 was six minutes into a restart while its peers had been up 58.

The Amendment-4 placement guard caught every one, which is the difference from the silent *"cannot achieve consistency level ALL"* partial writes that preceded it. But aborting is the wrong response to something that resolves in seconds, so the harness now **waits for 3/3 membership** before creating a class, keeping the abort as a backstop.

**Attempt 2 — the classes were never actually fresh.** Ten runs completed with no aborts, and five were silently invalid: their `index_recall` read `before == after` and their chaos arms were **left**-censored, meaning the victim already held everything on the first probe.

Amendment 4 promised "a fresh class per run" and only ever *created* one. `create_class()` tolerates an existing class — it returns 200 with *"class already existed with the expected config"* — so every run whose attempt-1 counterpart had **completed** inherited that attempt's populated 10,000-object class, and its before-snapshot was taken on a corpus that was already full.

The correlation identified it: every run whose counterpart completed was broken, every run whose counterpart aborted was correct, with no exceptions.

Fixed by **deleting before creating**, plus an assertion that the class holds zero objects at creation, so it cannot recur silently.

**This is the third recurrence of the same shape in this study**, and the pattern is worth naming:

| amendment | stale state | how it was cleaned | what broke |
|---|---|---|---|
| 2 | 14,200-object shared class | never cleaned | graph axis read 0.23 on a healthy replica |
| 4 | batch-deleted objects | delete, tolerantly | tombstones stalled async repair permanently |
| 1 (here) | previous attempt's class | create, tolerantly | 5 runs inherited a populated corpus |

Every one is **a cleanup that was tolerant rather than assertive**, and every fix has been to assert the post-condition — count is zero, placement is 3/3, the control scores above its floor — instead of trusting that setup worked.

Both discarded attempts are kept beside the results (`dissociation.aborted-attempt.json`, `dissociation.attempt2-stale-class.json`) rather than deleted, because they are the evidence for this amendment.

**Three startup defects were also fixed en route**, all the same root cause — checks that assumed a pre-existing class, which Amendment 4 had made obsolete: the topology check refused to run on a clean cluster (404), and the distance metric was read at startup rather than from the class each run creates. The metric is now read back from the class actually being measured.


---

## Results

**Outcome (a). The negative survives, on 5 corpus-matched pairs with nothing censored, at 5× the resolution.** 10 of 10 runs valid, no aborts — on the third attempt (see Amendment 1).

### Every seed uncensored, every pair matched

| seed | control drift | chaos delta | **paired diff** | `completeness` | censored | recovery |
|---|---|---|---|---|---|---|
| 20260900 | −0.047 | −0.045 | **+0.002** | 1.00 | none | 44.49 s |
| 20260901 | −0.053 | −0.062 | **−0.009** | 1.00 | none | 40.90 s |
| 20260902 | −0.049 | −0.052 | **−0.003** | 1.00 | none | 45.85 s |
| 20260903 | −0.047 | −0.044 | **+0.003** | 1.00 | none | 41.96 s |
| 20260904 | −0.059 | −0.053 | **+0.006** | 1.00 | none | 38.34 s |

**Mean paired difference −0.0002** — two thousandths of one step. The five differences straddle zero: three positive, two negative, none larger than 0.009.

Against #54:

| | #54 | #69 |
|---|---|---|
| window | 60 s | **180 s** |
| resolution | 0.005 | **0.001** |
| corpus-matched pairs | 3 of 5 | **5 of 5** |
| right-censored | 2 | **0** |
| excluded deficit | ~0.020 | **~0.010** |
| mean paired difference | +0.005 | **−0.0002** |

### The hypothesis was right about censoring and wrong about the numbers

The pre-registered expectation was that 180 s would uncensor every seed. It did: recovery ran **38.34–45.85 s**, comfortably inside the window, where #54 saw 40.08–60.55 s with one seed finishing *past* nominal.

But the expectation that the bound would tighten "toward ~0.005" was optimistic. The bound is **~0.010**, because it is driven by the largest observed paired difference (0.009) rather than by the quantisation floor alone. Raising resolution 5× did not tighten the bound 5×; it revealed per-seed variation that the coarse ruler had been rounding to zero.

**That is the substantive finding of this re-run, and it cuts against #54's presentation.** #54 reported paired differences of "0.000, 0.000, +0.015" and leaned on the two exact zeros — *"in 2 of 3 the arms are IDENTICAL"*. At 0.001 resolution no pair is identical. Those zeros were the ruler, not the system.

### The per-seed criterion says 2 of 5, and that is the wrong statistic

Scored per seed, chaos loses more than its control in seeds 20260901 (−0.009) and 20260902 (−0.003) — so the registered binary criterion reports "2 of 5 show the dissociation", against #54's 0 of 5.

**This is not evidence of a dissociation appearing.** A per-seed sign test discards magnitude, and differences straddling zero with a mean of −0.0002 are what a null looks like once the instrument can resolve noise. #54's 0 of 5 and #69's 2 of 5 are the same result seen through rulers of different fineness: the coarse one quantised small negatives to zero.

The analyser now says so in its own output rather than leaving a reader to count seeds.

### The control is not stable, and that matters

`max |drift| = 0.059` across the no-chaos arm — the corpus-size effect from doubling 5,000 → 10,000, which is why the corpus-matched control exists. But the drift itself **varies by seed** from −0.047 to −0.059, a spread of 0.012 that is larger than every paired difference in the table.

So the paired design is doing all the work here. An unpaired comparison gives baseline 0.9222 vs chaos 0.9218, p = 1.0000 — reported as reference only, since it discards the pairing the design produces.

## Interpretation

**#54's conclusion holds and is now better evidenced: at 180 s, with the data fully repaired in every seed, graph quality is indistinguishable from a no-chaos run over the same corpus.**

This is the stronger version of the negative. It rests on five matched pairs rather than three, with nothing censored, and it excludes a chaos-specific deficit above **~0.01** rather than ~0.02. Neither pre-registered alternative fired: no deficit appeared between 60 s and 180 s (null i), and none appeared when the floor dropped to 0.001 (null ii).

**What genuinely changed is the honesty of the bound.** #54's "two arms identical" was an artifact of a ruler with 0.005 steps. The real picture is per-seed variation of ±0.009 around zero — small, unbiased in sign, and larger than the resolution. A future run wanting a tighter bound needs *more seeds*, not more queries: the limit is now seed-to-seed variance, not quantisation. That is a change of regime, and it is the useful thing this re-run establishes for planning.

**Still a horizon claim.** Nothing here observes past 180 s. #37 remains the precedent that horizons change healing verdicts.

**Still one system, one build, one host, n = 5.**

## Decision

**MERGE**, as outcome (a).

**What must not be claimed.** That the dissociation is refuted in general — one system, 180 s, n = 5, deficit bound ~0.01. That "2 of 5 seeds show the dissociation" — that is a sign count on a null, and reporting it as a positive would invert the result. That the bound can be tightened by resolution alone — it is now variance-limited.

**Consequences to file.**

1. `experiment/*` — **more seeds, not more queries.** The bound is set by seed-to-seed variance (±0.009), which is above the 0.001 floor. This is the first result in the project where the n = 5 ceiling is the *binding* constraint on a claim rather than a caveat attached to one, and it is a concrete ask for the PARAM Shavak access.
2. `analysis/*` — **#54's "the arms are IDENTICAL" phrasing should be corrected.** It reported a quantisation artifact as a property of the system. Not a wrong conclusion, but a wrong reason, and this project records those.
3. `method/*` — the analyser carried three hardcoded strings from #54 that misreported #69's data (a seed count, a contamination claim, and a 60 s horizon). All three are now derived. Prose in an analyser goes stale exactly as a constant does.
