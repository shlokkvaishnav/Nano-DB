# Does #54's negative survive a longer horizon and a finer ruler?

Issue #69 · branch `experiment/dissociation-180s` · **pre-registered, no runs yet.**

## Why re-run a result that already landed

#54 concluded **outcome (i): both axes heal** — the project's central prediction, tested on the only system with real anti-entropy, and not confirmed. That negative is only as good as the window and the ruler behind it, and both were marginal:

| | #54 | here |
|---|---|---|
| observation window | 60 s | **180 s** |
| queries per snapshot | 20 | **100** |
| `index_recall` resolution | 0.005 | **0.001** |
| corpus-matched pairs | **3 of 5** | 5 of 5, expected |
| excluded deficit | ~0.02 | ~0.005, expected |

Two of five seeds were **right-censored** because repair did not finish inside 60 s — observed recovery ran 40.08–60.55 s, one seed finishing *past* nominal. And the paired differences that carried the conclusion were **0.000, 0.000 and +0.015** against a ruler whose smallest step is 0.005. Zero, zero, and three steps.

A bound of "no deficit above ~0.02" is a statement about the ruler, not about Weaviate.

## What the two changes do, and why it is not two variables

`OBSERVE_S` decides whether repair **completes** inside the window — censoring, and therefore how many pairs are corpus-matched. It cannot change `index_recall` at a fixed corpus size.

`N_QUERIES` decides only the **resolution** of `index_recall`: the metric quantises at `1 / (N_QUERIES × K)`. It changes what the ruler can see, not what the system does.

Neither is an experimental variable. The manipulation under test — chaos versus no-chaos, against a corpus-matched control — is untouched. That is stated explicitly because "multiple variables changed at once" is on `GIT_WORKFLOW.md`'s do-not-merge list, and the exemption should be argued rather than assumed.

## Both defaults stay at #54's values

`dissociation.py` still defaults to 60 s and 20 queries; #69 passes `--observe-s 180 --queries 100`. Editing the constants in place would have meant that re-running #54 no longer reproduces #54's committed results — a silent break of the study this one is auditing.

Both parameters are now **recorded in every run record**, so the analyser derives the horizon and the resolution from the artifact rather than from a constant that may since have changed. #63 is the standing lesson there: a published number that turned out to come from a sampler setting nobody had recorded.

## What would change the project's position

Outcome **(ii)** is the one to watch: a deficit that appears once the floor drops to 0.001 would mean #54's "both heal" was a *resolution* artifact, and a real deficit between 0.001 and 0.005 had been invisible. That is the outcome that would most change where this project stands, and it is the reason the query count is being raised rather than only the window.

Outcome **(i)** — a deficit appearing between 60 s and 180 s — would be a horizon artifact in the opposite direction to #37's, where Qdrant damage a 50 s window called permanent was gone by 180 s.

Either would be a positive, and would require `RELATED_WORK.md` §4 to be revised again, in the opposite direction, with a `claim_corrections/` entry retiring a claim this project published nine days earlier.

## This run is an input to a project-level decision

`DECISION_LOG.md` (2026-09-10) pre-registers a pivot trigger whose condition (3) reads: *the 180 s re-run leaves the negative unchanged **and** raising queries-per-snapshot fails to bring the exclusion bound below ~0.01.* This is the cheaper of the two inputs — it needs no cluster access.

## Running it

```bash
python research/weaviate_dissociation/dissociation.py --observe-s 180 --queries 100 \
  --out research/weaviate_dissociation_180s/results
python research/weaviate_dissociation/analyze_dissociation.py \
  --json research/weaviate_dissociation_180s/results/dissociation.json
```

Full pre-registration, outcomes and confounds: [`SPEC.md`](SPEC.md).
