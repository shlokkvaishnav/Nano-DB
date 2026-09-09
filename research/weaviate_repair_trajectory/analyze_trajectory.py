#!/usr/bin/env python3
"""
Is Weaviate's "~6 s ramp at 5,000 objects" a transfer phase or a truncation
artifact? (issue #63)

Reads two ALREADY-COMMITTED artifacts and nothing else. No cluster, no compute:

    research/weaviate_dissociation/results/dissociation.json   (#54, merged #61)
    research/weaviate_repair_window/results/sizes.json         (#48)

THE DECISION RESTS ON ONE NUMBER, fixed in SPEC.md before any fit ran: the
5,000-object tail span in sizes.json against the poll interval implied by that
run's own `repair_s / samples_in_window`. `characterize.py` stores only
`traj[:6]` and `traj[-3:]`, so if the stored tail spans about two poll intervals
then the "ramp" measured there is probe latency, not transfer duration.

The throughput and plateau numbers CHARACTERIZE; they do not adjudicate.

Usage:
    python research/weaviate_repair_trajectory/analyze_trajectory.py
"""
from __future__ import annotations

import argparse
import json
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DISS = os.path.join(ROOT, "research", "weaviate_dissociation", "results",
                    "dissociation.json")
SIZES = os.path.join(ROOT, "research", "weaviate_repair_window", "results",
                     "sizes.json")


def ols(xs, ys):
    """Slope, intercept, and the residual standard deviation.

    The residual is returned because SPEC.md registered outcome (c): a curve
    fitted as a line must be visible as a bad fit rather than assumed away.
    """
    n = len(xs)
    if n < 3:
        return None, None, None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None, None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    inter = my - slope * mx
    resid = [y - (slope * x + inter) for x, y in zip(xs, ys)]
    return slope, inter, (statistics.pstdev(resid) if n > 2 else 0.0)


def segment(series):
    """Split one completeness series into plateau and transfer phase.

    The rule is the one fixed in SPEC.md, not one chosen after looking:
    the transfer phase begins at the LAST sample whose count equals the run's
    first successfully-probed count, and ends at the last observed sample.
    """
    ok = [s for s in series if s.get("ok") and s.get("n") is not None]
    if len(ok) < 4:
        return None
    first_n = ok[0]["n"]
    last_at_first = 0
    for i, s in enumerate(ok):
        if s["n"] == first_n:
            last_at_first = i
    transfer = ok[last_at_first:]
    return {
        "first_probe_t": ok[0]["t"],
        "first_n": first_n,
        "plateau_s": transfer[0]["t"] - ok[0]["t"],
        "transfer_start_t": transfer[0]["t"],
        "transfer": transfer,
        "n_ok": len(ok),
        "failed_probes": len(series) - len(ok),
        "cadence_s": (ok[-1]["t"] - ok[0]["t"]) / max(len(ok) - 1, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diss", default=DISS)
    ap.add_argument("--sizes", default=SIZES)
    a = ap.parse_args()

    # =====================================================================
    # THE DECIDING METRIC -- sizes.json truncation ratio
    # =====================================================================
    sizes = json.load(open(a.sizes))
    print("=" * 74)
    print("THE DECIDING METRIC: is the stored tail a transfer phase or 3 polls?")
    print("=" * 74)
    print("  characterize.py keeps traj[:6] and traj[-3:]. Three stored samples")
    print("  span TWO poll intervals. If the tail span is about that, the")
    print("  'ramp' is probe latency.\n")
    print(f"  {'size':>7}{'seed':>7}{'repair_s':>10}{'samples':>9}"
          f"{'poll_int':>10}{'tail span':>11}{'span/2 polls':>14}")
    ratios = {}
    for r in sorted(sizes, key=lambda x: (x["size"], x["seed"])):
        tail = r.get("trajectory_tail") or []
        if len(tail) < 2 or not r.get("repair_s") or not r.get("samples_in_window"):
            continue
        poll = r["repair_s"] / r["samples_in_window"]
        span = tail[-1][0] - tail[0][0]
        ratio = span / (2 * poll) if poll else float("nan")
        ratios.setdefault(r["size"], []).append(ratio)
        print(f"  {r['size']:>7}{r['seed']:>7}{r['repair_s']:>10.3f}"
              f"{r['samples_in_window']:>9}{poll:>10.5f}{span:>11.3f}{ratio:>14.2f}")
    print("\n  ratio ~1.0 => the stored tail IS two poll intervals, i.e. the")
    print("  number is probe-bound and measures nothing about transfer.")
    for size in sorted(ratios):
        v = ratios[size]
        print(f"    size {size:>6}: mean ratio {statistics.mean(v):.2f} "
              f"over {len(v)} run(s)")

    # =====================================================================
    # What the STORED HEAD shows that the tail hides
    # =====================================================================
    print("\n" + "=" * 74)
    print("THE SHAPE THE TRUNCATION HID (5,000-object runs, head + tail)")
    print("=" * 74)
    print("  characterize.py keeps the first six samples and the last three. Put")
    print("  them side by side and the trajectory is not a ramp at all.\n")
    for r in sorted([x for x in sizes if x["size"] == 5000],
                    key=lambda x: x["seed"]):
        head = r.get("trajectory_head") or []
        tail = r.get("trajectory_tail") or []
        if not head or not tail:
            continue
        print(f"  seed {r['seed']}  (repair_s {r['repair_s']}, "
              f"{r['samples_in_window']} samples total)")
        print("    head: " + "  ".join(f"{t:.1f}s:{n}" for t, n in head))
        print("    ...gap the harness did not store...")
        print("    tail: " + "  ".join(f"{t:.1f}s:{n}" for t, n in tail))
        burst_t, burst_n = head[1][0], head[1][1]
        plat_n = head[-1][1]
        print(f"    -> {burst_n} of {r['size']} objects arrive by t={burst_t:.1f}s "
              f"({100 * burst_n / r['size']:.0f}%), then the count sits at "
              f"{plat_n} until t={tail[0][0]:.1f}s,")
        print(f"       then climbs to {tail[-1][1]} by t={tail[-1][0]:.1f}s.")
    print("\n  So the trajectory is BURST -> ~30 s PLATEAU -> FINAL CLIMB, and the")
    print("  quantity reported as 'a ~6 s ramp' is only the final climb. It is")
    print("  not a transfer duration and it is not the whole shape.")
    print("\n  NOTE this also breaks the segmentation rule SPEC.md registered,")
    print("  which assumes the first probed count IS the plateau count. Here the")
    print("  first probe (140, 13) catches the burst instead, so the same rule")
    print("  would report a zero plateau -- which is exactly what it does on")
    print("  dissociation seed 20260903 below. A registered rule that mis-fires")
    print("  on a known case is reported, not quietly patched.")

    # =====================================================================
    # The dissociation trajectories
    # =====================================================================
    diss = json.load(open(a.diss))
    chaos = [r for r in diss if r.get("chaos") and not r.get("aborted")]
    ctrl = [r for r in diss if not r.get("chaos") and not r.get("aborted")]

    print("\n" + "=" * 74)
    print("THE FULL TRAJECTORIES (dissociation, 5,000-object divergence)")
    print("=" * 74)
    print("  'dead time' is measured FROM THE RESTART (t=0), not from the first")
    print("  successful probe: #56's clock is restart-anchored, so a plateau timed")
    print("  from the first answer would understate it by however long the victim")
    print("  spent unreachable. Those failed probes are counted separately.\n")
    print(f"  {'seed':>10}{'probes':>8}{'failed':>8}{'cadence':>9}{'1st ok':>8}"
          f"{'deadtime':>10}{'transfer_s':>12}{'obj/s':>9}{'resid':>9}{'censored':>10}")
    rates, plateaus, deadtimes = [], [], []
    for r in sorted(chaos, key=lambda x: x["seed"]):
        seg = segment(r.get("completeness_series") or [])
        if not seg:
            print(f"  {r['seed']:>10}   too few successful probes to segment")
            continue
        tr = seg["transfer"]
        xs = [s["t"] for s in tr]
        ys = [s["n"] for s in tr]
        slope, _, resid = ols(xs, ys)
        dur = xs[-1] - xs[0]
        if slope is not None:
            rates.append(slope)
        plateaus.append(seg["plateau_s"])
        deadtimes.append(seg["transfer_start_t"])
        print(f"  {r['seed']:>10}{seg['n_ok']:>8}{seg['failed_probes']:>8}"
              f"{seg['cadence_s']:>9.2f}{seg['first_probe_t']:>8.1f}"
              f"{seg['transfer_start_t']:>10.2f}{dur:>12.2f}"
              f"{(slope if slope is not None else float('nan')):>9.1f}"
              f"{(resid if resid is not None else float('nan')):>9.1f}"
              f"{str(r.get('censored')):>10}")

    if deadtimes:
        print(f"\n  DEAD TIME from restart to transfer start: "
              f"{min(deadtimes):.1f}-{max(deadtimes):.1f} s, median "
              f"{statistics.median(deadtimes):.1f} s")
        print("  weaviate_repair_clock/ (#56) young-regime step: ~32 s "
              "(observed 31.08-33.43, timed from the same origin)")
        lo, hi = min(deadtimes), max(deadtimes)
        if 31.08 <= statistics.median(deadtimes) <= 33.43:
            print("  -> median falls INSIDE #56's observed range: the two studies")
            print("     are measuring the same dead time. Decomposition holds.")
        else:
            print("  -> median falls OUTSIDE #56's observed range. Under SPEC.md")
            print("     outcome (d) the mapping onto #56's step is WITHDRAWN, or")
            print("     held only where the ranges overlap. Stated, not smoothed.")
    if rates:
        print(f"  transfer throughput: {min(rates):.0f}-{max(rates):.0f} obj/s, "
              f"median {statistics.median(rates):.0f}")
        print(f"  implied transfer time for 5,000 objects at the median rate: "
              f"{5000 / statistics.median(rates):.1f} s")

    # =====================================================================
    # The registered control, and why it cannot be computed
    # =====================================================================
    print("\n" + "=" * 74)
    print("THE REGISTERED CONTROL IS NOT AVAILABLE")
    print("=" * 74)
    have = sum(1 for r in ctrl if r.get("completeness_series"))
    print(f"  SPEC.md registered the dissociation study's no-chaos control arm as")
    print(f"  the noise floor: a control run's count should be flat across the")
    print(f"  window. {len(ctrl)} control runs are committed and {have} carry a")
    print("  completeness_series -- the harness samples the series only in the")
    print("  chaos branch, so the registered control DOES NOT EXIST in the data.")
    print("  Reported rather than silently substituted.")
    print("\n  The available substitute is internal and weaker: each chaos run's")
    print("  own PLATEAU is a flat reference measured by the same probe on the")
    print("  same corpus. If the probe manufactured climbs, the plateau would not")
    print("  be flat. Every run's plateau is exactly constant by construction of")
    print("  the segmentation rule, so this cannot detect a slow drift -- it")
    print("  bounds step artifacts only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
