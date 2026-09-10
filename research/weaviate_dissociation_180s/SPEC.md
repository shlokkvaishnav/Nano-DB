# Spec: does #54's negative survive a 180s horizon and a 5x finer instrument?

**Branch:** `experiment/dissociation-180s`
**Issue:** #69
**Date opened:** 2026-09-10
**Status:** IN PROGRESS — pre-registered, no runs yet

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
