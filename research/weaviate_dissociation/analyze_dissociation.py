#!/usr/bin/env python3
"""
Does `completeness` heal on Weaviate while `index_recall` does not? (issue #54)

Reads `results/dissociation.json` and nothing else, and recomputes every figure
the writeup leads with. This exists because #59's review found that study's
headline result was reachable only by reading prose: its committed analyser
printed the aggregate the PR overturned. An analyser that does not reproduce the
conclusion is not a reproducibility artifact.

The pre-registered primary metric, per seed:

    completeness returns to 1.0 within the window
      AND post-chaos index_recall is outside its own pre-chaos range

Both halves are reported per seed, with the censoring status, because a
right-censored completeness series answers "did it heal within 60 s" with
"unknown", not with "no".

Usage:
    python research/weaviate_dissociation/analyze_dissociation.py
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from itertools import combinations

HERE = os.path.dirname(os.path.abspath(__file__))


def ir(rec, when):
    return (rec.get(f"index_recall_{when}") or {}).get("index_recall")


def mannwhitney_exact(a, b):
    """Exact two-sided Mann-Whitney by enumeration. At 5v5 the smallest
    attainable p is 0.0079, which means the groups separate completely -- NOT
    that the effect is large. Every use of this number must say so."""
    def U(x, y):
        return (sum(1 for i in x for j in y if i > j)
                + 0.5 * sum(1 for i in x for j in y if i == j))
    comb = list(a) + list(b)
    n = len(a)
    obs = U(a, b)
    hit = tot = 0
    for idx in combinations(range(len(comb)), n):
        x = [comb[i] for i in idx]
        y = [comb[i] for i in range(len(comb)) if i not in idx]
        u = U(x, y)
        tot += 1
        if min(u, n * len(b) - u) <= min(obs, n * len(b) - obs):
            hit += 1
    return hit / tot


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(HERE, "results", "dissociation.json"))
    a = ap.parse_args()
    rows = json.load(open(a.json))

    aborted = [r for r in rows if r.get("aborted")]
    rows = [r for r in rows if not r.get("aborted")]
    print(f"runs: {len(rows)}"
          + (f"   ABORTED (excluded): {len(aborted)}" if aborted else ""))
    for r in aborted:
        print(f"    seed {r['seed']} chaos={r['chaos']}: {r['aborted']}")

    base = [r for r in rows if not r["chaos"]]
    chaos = [r for r in rows if r["chaos"]]

    # ---- the control comes first: a graph axis that drifts on its own has -----
    # ---- nothing to say about a graph axis that drops under chaos ------------
    print("\n=== control: no chaos, same two snapshots ===")
    print(f"  {'seed':>10}{'before':>9}{'after':>9}{'drift':>9}")
    drift = []
    for r in sorted(base, key=lambda x: x["seed"]):
        d = ir(r, "after") - ir(r, "before")
        drift.append(d)
        print(f"  {r['seed']:>10}{ir(r,'before'):>9.3f}{ir(r,'after'):>9.3f}{d:>9.3f}")
    print(f"\n  max |drift| = {max(abs(x) for x in drift):.4f} over {len(drift)} seeds")
    print("  The measurement is stable when nothing happens. That is what makes")
    print("  a drop under chaos attributable to the chaos.")

    # ---- the two axes -------------------------------------------------------
    print("\n=== chaos: the two axes, per seed ===")
    print(f"  {'seed':>10}{'IR before':>11}{'IR after':>10}{'IR delta':>10}"
          f"{'comp_end':>10}{'censored':>10}{'recovery_s':>12}")
    for r in sorted(chaos, key=lambda x: x["seed"]):
        rec = r.get("recovery_s")
        print(f"  {r['seed']:>10}{ir(r,'before'):>11.3f}{ir(r,'after'):>10.3f}"
              f"{ir(r,'after') - ir(r,'before'):>10.3f}"
              f"{(r.get('completeness_end') if r.get('completeness_end') is not None else float('nan')):>10.2f}"
              f"{str(r.get('censored')):>10}"
              f"{(f'{rec:.2f}' if rec is not None else '--'):>12}")

    # ---- the PAIRED comparison, on corpus-matched seeds only ----------------
    # Review round 2: an unpaired test over all five seeds is contaminated. Two
    # seeds are right-censored, so their chaos arm ended with FEWER objects than
    # its control -- and a smaller corpus scores HIGHER recall, which is exactly
    # what the matched control exists to neutralise. Those rows push the chaos
    # arm up for reasons unrelated to chaos, in the direction of the conclusion.
    # An unpaired test also discards the per-seed control the design produces.
    bym = {r["seed"]: r for r in base}
    print("\n=== the paired comparison, corpus-matched seeds only ===")
    print(f"  {'seed':>10}{'ctl held':>10}{'chaos held':>12}{'matched':>9}"
          f"{'ctl delta':>11}{'chaos delta':>13}{'paired diff':>13}")
    paired = []
    for r in sorted(chaos, key=lambda x: x["seed"]):
        b = bym.get(r["seed"])
        if not b:
            continue
        hb = (b.get("index_recall_after") or {}).get("held")
        hc = (r.get("index_recall_after") or {}).get("held")
        db = ir(b, "after") - ir(b, "before")
        dc = ir(r, "after") - ir(r, "before")
        matched = hb == hc
        if matched:
            paired.append(dc - db)
        print(f"  {r['seed']:>10}{hb:>10}{hc:>12}{('yes' if matched else 'NO'):>9}"
              f"{db:>+11.3f}{dc:>+13.3f}{dc - db:>+13.3f}")
    if paired:
        print(f"\n  corpus-matched pairs: n = {len(paired)}   "
              f"paired differences {[round(x, 4) for x in paired]}")
        print(f"  mean {statistics.mean(paired):+.4f} -- a NEGATIVE value would "
              "mean chaos lost MORE index_recall than its control")
        # Computed, not asserted. This line was hardcoded from #54's data and
        # kept claiming "never loses more, 2 of 3 identical" against #69's
        # results, where chaos loses more in 2 of 5. Prose in an analyser is as
        # capable of going stale as a constant is.
        worse = [x for x in paired if x < 0]
        ident = [x for x in paired if x == 0]
        if not worse:
            print(f"  Chaos never loses more; {len(ident)} of {len(paired)} "
                  "arms are IDENTICAL.")
        else:
            mags = ", ".join(f"{abs(x):.3f}" for x in sorted(worse))
            print(f"  Chaos loses MORE in {len(worse)} of {len(paired)} seeds "
                  f"(by {mags}).")
            print("  Read the mean and the signs together, not the count: a "
                  "per-seed sign")
            print("  test discards magnitude, and differences straddling zero "
                  "with a mean")
            print("  near zero are what a null looks like at this resolution.")

    # ---- what this can and cannot exclude, exactly --------------------------
    snap = (base[0].get("index_recall_before") or {}) if base else {}
    nq = snap.get("queries")
    # k is read from the record, not mirrored. Runs written before k was
    # recorded fall back to 10, which is what those runs used.
    kk = snap.get("k", 10)
    if nq and paired:
        step = 1.0 / (nq * kk)
        bound = max(2 * step, max(abs(x) for x in paired)) + step
        print(f"\n  RESOLUTION: {nq} queries x top-{kk} = {nq * kk} ground-truth "
              f"items, so index_recall moves in steps of 1/{nq * kk} = {step:.4f}.")
        print(f"  Largest chaos-specific deficit consistent with these pairs: "
              f"under ~{bound:.3f}.")
        print("  It does NOT exclude a deficit below the quantisation floor.")
        print("  More queries per snapshot lowers that floor at no cluster cost,")
        print("  and is the cheapest available improvement.")

    ba = [ir(r, "after") for r in base]
    ca = [ir(r, "after") for r in chaos]
    # The reason it is reference-only depends on the data: #54 had two unmatched
    # seeds contaminating it; #69 has none, and it is still the wrong test
    # because it throws away the per-seed pairing the design produces.
    n_unmatched = len(chaos) - len(paired)
    why = (f"contaminated by the {n_unmatched} unmatched seed(s)" if n_unmatched
           else "every seed is corpus-matched here, but it still discards the "
                "per-seed pairing the design produces")
    print(f"\n  (unpaired, REFERENCE ONLY -- {why}: "
          f"baseline {statistics.mean(ba):.4f} vs chaos "
          f"{statistics.mean(ca):.4f}, p = {mannwhitney_exact(ba, ca):.4f})")

    # ---- the pre-registered primary metric ----------------------------------
    print("\n=== the pre-registered dissociation, per seed ===")
    print("  (completeness returned to 1.0 within the window"
          " AND the chaos arm lost MORE index_recall than its corpus-matched control)")
    # The reference is the CORPUS-MATCHED CONTROL's drop, not the pre-chaos
    # value. Amendment 3 made both arms end at 10,000 objects, and doubling the
    # corpus costs ~0.05 of recall on its own -- so "index_recall_after is below
    # every pre-chaos value" is satisfied by the CONTROL too, and testing it
    # that way manufactures a dissociation out of the corpus-size effect.
    # Chaos must lose MORE than the matched control loses.
    ctl_drift = {r["seed"]: ir(r, "after") - ir(r, "before") for r in base}
    hits = 0
    for r in sorted(chaos, key=lambda x: x["seed"]):
        healed = r.get("completeness_end") == 1.0
        my_delta = ir(r, "after") - ir(r, "before")
        ref = ctl_drift.get(r["seed"])
        damaged = (ref is not None) and (my_delta < ref)
        ok = healed and damaged
        hits += bool(ok)
        note = ""
        if not healed and r.get("censored") == "right":
            note = ("  <- completeness RIGHT-CENSORED: did not finish inside the "
                    "60s window. 'Unknown', not 'did not heal'.")
        print(f"    seed {r['seed']}: healed={healed}  chaos_delta={my_delta:+.3f}"
              f"  control_delta={ref:+.3f}  graph_damaged_beyond_control={damaged}"
              f"  -> {'DISSOCIATION' if ok else 'no'}{note}")
    print(f"\n  {hits} of {len(chaos)} seeds show the dissociation.")

    # ---- what the design cannot say -----------------------------------------
    print("\n=== the horizon this cannot see past ===")
    # Read from the data, not hardcoded: #69 runs a 180s window and this section
    # kept saying 60s.
    obs = (chaos[0].get("observe_s") if chaos else None) or 60
    print(f"  index_recall_after is snapshotted when the {obs:g}s completeness window")
    print(f"  closes. So the supported claim is 'at ~{obs:g}s, completeness has healed")
    print("  and index_recall has not' -- a statement about a HORIZON, not about")
    print("  permanence. #37 is the precedent: Qdrant graph damage that a 50s")
    print("  window called permanent was gone by 180s. A long-quiesce re-run is")
    print("  the experiment that would settle it, and it has not been done.")

    # ---- exploratory, flagged as such ---------------------------------------
    part = [r for r in chaos if r.get("completeness_end") not in (None, 1.0)]
    if part:
        full = [r for r in chaos if r.get("completeness_end") == 1.0]
        print("\n=== exploratory, NOT pre-registered, n=1 on the partial arm ===")
        print(f"  seeds that fully repaired (n={len(full)}): mean IR delta "
              f"{statistics.mean([ir(r,'after')-ir(r,'before') for r in full]):+.3f}")
        for r in part:
            print(f"  seed {r['seed']} repaired only {r['completeness_end']:.0%} "
                  f"and lost {ir(r,'after')-ir(r,'before'):+.3f}")
        print("  Less repair, less graph damage. That is the shape a mechanism")
        print("  would have if the REPAIR is what damages the graph rather than")
        print("  the outage -- but it rests on ONE partially-repaired seed and")
        print("  is a hypothesis for a new pre-registration, not a finding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
