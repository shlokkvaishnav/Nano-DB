# Git workflow for this research

`main` is the validated research state, not just working code. Branches are where uncertainty gets explored. This document says how those two things stay separate on purpose.

A branch is not merged because it works or produces a better number. It is merged when it is validated, reproducible, documented, and useful to the research — and a branch that disproves a hypothesis, rules out a mechanism, or produces a negative result can satisfy all four of those without ever changing a line of the implementation.

## Branch types

| Prefix | For | Example |
|---|---|---|
| `research/<topic>` | A substantial research implementation or investigation, usually spanning several experiments | `research/cross-system-replication` |
| `experiment/<name>` | One specific, narrowly-scoped experiment | `experiment/settling-window-sensitivity` |
| `analysis/<name>` | Analysis of an existing result — no new data collection | `analysis/per-seed-variance` |
| `method/<name>` | A new methodological component (a metric, a detector, a protocol) | `method/replica-quality-detector` |
| `reproduction/<target>` | Reproducing an external paper, benchmark, or system's behavior | `reproduction/qdrant-replica-failure` |

Don't create a branch for a cosmetic or purely-editorial change — those go straight to `main` via normal review. Do create one whenever a change could affect an experimental conclusion (see **Isolation**, below).

**SETUP GOES STRAIGHT TO `main` (owner decision, 2026-09-10).** No branch, no PR, no review round. *Setup* is the machinery rather than the findings: tooling and checkers (`check_research.py`, `check_index.py`, their tests), CI config, this document and `AGENT_PIPELINE.md`, hooks, and harness scaffolding that produces no claim. **Research still takes the full path** — spec, branch, PR, review, manual merge.

The reasoning is cost, not a lower bar. A checker either passes its own tests or it does not, so the outcome is mechanically visible rather than a matter of judgement, and PR #68 spent two review rounds on a one-line predicate. What does not change: run `check_research.py`, `check_index.py` and `test_check_research.py` before pushing, and write the commit message to the standard a PR body would have met — for a setup change the commit message *is* the audit trail.

**If a change touches both setup and a research claim, it is research.** Branch it. This is the existing "formatting and research must never share a branch" rule read in the other direction, and it is the case where the shortcut would actually cost something.

## Before writing code: the spec

A substantial branch (`research/*`, most `experiment/*`) starts with a filled-out copy of [`SPEC_TEMPLATE.md`](SPEC_TEMPLATE.md), committed before the implementation that answers it. The point of committing the spec first is that it timestamps the hypothesis — a hypothesis written after seeing the result is not a hypothesis, it's a caption. If a branch turns exploratory partway through (an unexpected observation redirects it), say so explicitly in the spec and in the final writeup; do not backfill a clean hypothesis once the answer is known.

A small `experiment/*` branch (e.g. re-running the existing sweep at a different seed count) can skip the full template and state the one-line question + expected outcome in the first commit message instead — use judgement, but when in doubt, write the spec.

## Isolation

If a change touches any of: model/index configuration, replication behavior, the fault model, the dataset, the query distribution, the evaluation metric, the statistical procedure, or the experimental protocol — put it on its own branch. Mixing two of these in one branch makes "what caused the change" unanswerable, which defeats the point of running the experiment at all.

**Formatting and research must never share a branch.** Process, repo-hygiene and
documentation-structure changes go straight to `main`; research goes through a
PR. That rule already existed — what it lacked was the corollary that the two
cannot ride together, because separating them afterwards is manual surgery.

Doing it once cost: cherry-picking two commits onto `main`, resetting the
branch, rebasing it, force-pushing, resolving three conflicts in the experiment
index, and rewriting the PR description — all because a README rewrite and a
step-2c result shared a branch. None of that work advanced either change.

The test to apply before committing: *would this line belong in the PR
description of an experiment?* A deleted Grafana dashboard would not.

## Lifecycle

1. Research question → 2. Literature check → 3. Hypothesis → 4. Experimental design → 5. Implementation → 6. Validation → 7. Experiment → 8. Analysis → 9. Interpretation → 10. **Decision**

The decision is one of:

- **MERGE** — sufficiently validated; becomes part of the main research codebase.
- **ARCHIVE** — scientifically useful, doesn't belong in the main implementation. Keep the branch (or a documented summary + the branch ref) rather than deleting it.
- **REVISE** — promising, but the experiment or implementation needs another pass.
- **ABANDON** — the question or approach is no longer useful. Still not deleted without inspection — see below.
- **REPRODUCE** — should be repeated under better controls before a decision can be made.

"Not merged" is not a synonym for "failed." A branch that cleanly rules out a mechanism (e.g. the two hypotheses ruled out for the catastrophic-disconnection anomaly) is a successful branch that ends in ARCHIVE, not a failed one.

## Merge criteria

Before merging into `main`, check all nine — not every one needs to be perfect, but any real weakness has to be written down, not glossed over:

**Scientific relevance** (addresses an approved research question) · **Correctness** (implementation does what it claims) · **Experimental validity** (controls/baselines/metrics are appropriate) · **Reproducibility** (another researcher could rerun it) · **Documentation** (purpose and methodology are written down) · **Interpretation** (we know what the result does and doesn't establish) · **Research integrity** (negative results and limitations are honestly recorded, not smoothed over) · **Integration** (doesn't make the codebase harder to understand without justification) · **Evidence** (enough of it to justify moving from "branch" to "validated state")

A positive result is not a merge criterion. A negative result is not a merge blocker.

## When *not* to merge

Irreproducible. Unstable implementation. Fundamentally flawed methodology. Result depends on an uncontrolled confound. Multiple important variables changed at once with no way to attribute the effect. Result uninterpretable. Substantial technical debt with no research justification. Purely exploratory with no validated conclusion yet. Contradicts the established methodology without an approved reason. Based on cherry-picked runs. Makes an unsupported scientific claim.

Any of these → archive the branch and write down what was learned instead of merging or deleting.

## Negative results

Don't hide them. A hypothesis being false, a method not working, an expected effect not appearing, a detector failing under realistic conditions, a mechanism turning out not to be responsible — these are findings, and they get recorded the same way a positive result would. A negative-result branch merges into `main` only if the *implementation itself* becomes validated infrastructure (e.g. `graph_forensics.py` merged despite its leading hypothesis coming back negative, because the tool itself is now how that class of question gets asked). Otherwise the branch and its writeup are preserved, not deleted, and the conclusion is what travels forward — see the catastrophic-disconnection postmortem for the shape this should take.

## Commit messages

Prefix by what changed, and say so explicitly when a commit changes methodology, not just implementation:

```
research: add replica-level probing
experiment: add 20-seed sweep
analysis: compute bootstrap confidence intervals
method: add non-pinned query distribution
fix: correct replica completeness calculation
docs: document settling-window protocol
```

Not: `stuff`, `changes`, `final`, `final-final`, `working`, `update`, `fixed`.

## Mini peer review before merging

For any substantial branch, answer these before merging — in the PR description or the branch's own doc, not just in your head:

What question did this branch answer? What was the hypothesis? What evidence was collected? What does the result actually establish — and what does it *not* establish? What confounds remain? What assumptions were made? Could another researcher reproduce it? Did the implementation introduce any unintended changes elsewhere? Does this change the research thesis in `README.md`? Merge / archive / revise / abandon / reproduce?

## Milestone tags

Tag validated milestones, not every commit:

```
research-v0.1-baseline
research-v0.2-replica-divergence
research-v0.3-layer1-complete
```

A tag means "at this commit, `main` is a coherent, reproducible state that supports the claims documented at that point." See `git tag -n99` in this repo for the current list and what each one represents.

## Decision log

Significant research decisions — why an experiment was designed a certain way, why a baseline was chosen, why a metric changed, why a hypothesis was rejected, why a branch was merged or archived — are recorded in [`DECISION_LOG.md`](DECISION_LOG.md). The repository should not depend on anyone's memory of why a call was made.

### Retiring a claim: write a string, not a file list

When a result withdraws a claim that has already reached `main`, the Decision's
consequences must name **the searchable phrase**, not the files someone happened
to think of — and that phrase goes into
[`retired_claims.txt`](retired_claims.txt) **in the same PR**.

Write: *the phrase `must beat 0.3s` must not appear unqualified anywhere.*
Not: *correct the ~0.3 s claim in these two files.*

The second form is what #48 wrote, and four other sites survived the withdrawal
for a day — still telling readers that sub-second sampling was "the only
remaining prerequisite" — because a consequence note is only as complete as the
grep behind it. `check_research.py` enforces the first form: any line containing
a retired phrase must carry a withdrawal marker within a few lines, or CI fails.

The original text still stays in place, struck or quoted where it is withdrawn.
This changes how the *consequence* is recorded, not how the correction is
written.

## What this is not

This is not a tool for making the repository look cleaner. Failed experiments, wrong hypotheses, bugs, dead ends, negative results, and abandoned methods are expected to exist in the history and on preserved branches — that *is* the research record. History does not get rewritten to make the project look more linear than it was, and a branch is not deleted without first checking whether it holds unique work (`git log <branch> --oneline`, `git diff main...<branch>`).
