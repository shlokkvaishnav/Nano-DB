#!/usr/bin/env python3
"""Tests for check_research.py. Each builds a fixture tree with ONE defect.

A checker that passes and cannot fail is worse than no checker: it looks like
coverage. So every check gets a fixture that trips it, and a matching clean
fixture that must not trip it -- the false-positive half matters as much,
because the first run of this checker produced six false alarms and a checker
that cries wolf gets switched off.

No Docker, no network, no cluster. Runs in CI beside check_index.py.

Usage:
    python research/test_check_research.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import check_research as cr   # noqa: E402

SPEC_OK = """# Spec: fixture

**Date opened:** 2026-09-05
**Status:** COMPLETE

## Research question
q
## Instrument characterization
The probe costs 50 ms; the effect moves on tens of seconds.
## Results
r
## Interpretation
i
## Decision
MERGE
"""


def build(tmp, spec=SPEC_OK, readme=True, script=True, results=True,
          log="mentions study_a", retired="", extra_md=None):
    """One study directory, plus the three files the checks read."""
    research = os.path.join(tmp, "research")
    d = os.path.join(research, "study_a")
    os.makedirs(d, exist_ok=True)
    if spec:
        io.open(os.path.join(d, "SPEC.md"), "w", encoding="utf-8").write(spec)
    if readme:
        io.open(os.path.join(d, "README.md"), "w", encoding="utf-8").write("# study_a\n")
    if script:
        io.open(os.path.join(d, "run.py"), "w", encoding="utf-8").write("# producer\n")
    if results:
        os.makedirs(os.path.join(d, "results"), exist_ok=True)
        io.open(os.path.join(d, "results", "out.json"), "w", encoding="utf-8").write("{}")
    io.open(os.path.join(research, "SPEC_TEMPLATE.md"), "w", encoding="utf-8").write(
        "## Research question\n## Instrument characterization\n## Results\n"
        "## Interpretation\n## Decision\n")
    io.open(os.path.join(research, "DECISION_LOG.md"), "w", encoding="utf-8").write(log)
    io.open(os.path.join(research, "retired_claims.txt"), "w", encoding="utf-8").write(retired)
    if extra_md:
        io.open(os.path.join(research, "notes.md"), "w", encoding="utf-8").write(extra_md)
    return research


def run(research):
    """Returns (exit_code, captured_output)."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = cr.main(["--root", research])
    finally:
        sys.stdout = old
    return code, buf.getvalue()


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("clean tree passes")
def t_clean(tmp):
    code, out = run(build(tmp))
    return code == 0 and "OK:" in out, out


@case("[1] DRAFT status over a Decision fails")
def t_draft(tmp):
    spec = SPEC_OK.replace("**Status:** COMPLETE",
                           "**Status:** DRAFT — no implementation or results yet")
    code, out = run(build(tmp, spec=spec))
    return code == 1 and "[1]" in out, out


@case("[2] missing Interpretation fails")
def t_missing_section(tmp):
    spec = SPEC_OK.replace("## Interpretation\ni\n", "")
    code, out = run(build(tmp, spec=spec))
    return code == 1 and "[2]" in out and "Interpretation" in out, out


@case("[2] qualified heading counts (no false positive)")
def t_qualified_heading(tmp):
    spec = SPEC_OK.replace("## Interpretation\n",
                           "## Interpretation (current — supersedes the pilot)\n")
    code, out = run(build(tmp, spec=spec))
    return code == 0, out


@case("[2] instrument characterization required after the rule date")
def t_instrument_required(tmp):
    spec = SPEC_OK.replace("## Instrument characterization\n"
                           "The probe costs 50 ms; the effect moves on tens of seconds.\n", "")
    code, out = run(build(tmp, spec=spec))
    return code == 1 and "Instrument characterization" in out, out


@case("[2] specs opened before the rule date are exempt")
def t_instrument_exempt(tmp):
    spec = SPEC_OK.replace("**Date opened:** 2026-09-05", "**Date opened:** 2026-08-23")
    spec = spec.replace("## Instrument characterization\n"
                        "The probe costs 50 ms; the effect moves on tens of seconds.\n", "")
    code, out = run(build(tmp, spec=spec))
    return code == 0, out


@case("[2] inline characterization counts, not just an H2")
def t_instrument_inline(tmp):
    spec = SPEC_OK.replace(
        "## Instrument characterization\n"
        "The probe costs 50 ms; the effect moves on tens of seconds.\n",
        "**Instrument characterization** (from prior artifacts): probe 50 ms.\n")
    code, out = run(build(tmp, spec=spec))
    return code == 0, out


@case("[3] study with no README fails")
def t_no_readme(tmp):
    code, out = run(build(tmp, readme=False))
    return code == 1 and "[3]" in out, out


@case("[4] results with no producer script fails")
def t_no_producer(tmp):
    code, out = run(build(tmp, script=False))
    return code == 1 and "[4]" in out, out


@case("[5] retired claim stated bare fails")
def t_retired_bare(tmp):
    research = build(tmp, retired="must beat 0.3s :: #48\n",
                     extra_md="Any healing measurement must beat 0.3s.\n")
    code, out = run(research)
    return code == 1 and "[5]" in out, out


@case("[5] retired claim with a withdrawal marker passes")
def t_retired_marked(tmp):
    research = build(tmp, retired="must beat 0.3s :: #48\n",
                     extra_md="Any healing measurement must beat 0.3s.\n\n"
                              "**Corrected 2026-09-06:** the sentence above is withdrawn.\n")
    code, out = run(research)
    return code == 0, out


@case("[5] a marker a few lines away still counts")
def t_retired_window(tmp):
    # The real shape this exists for: a spec quotes the dead sentence, then
    # marks it withdrawn in the block below, separated by a blank line.
    research = build(tmp, retired="must beat 0.3s :: #48\n",
                     extra_md="Any healing measurement must beat 0.3s.\n\n"
                              "filler\n\nThis claim is **withdrawn**.\n")
    code, out = run(research)
    return code == 0, out


@case("[5] a marker far outside the window does NOT count")
def t_retired_window_limit(tmp):
    # The window has to have an edge, or a withdrawal anywhere in a long file
    # would excuse a live claim at the top of it.
    far = "\n".join(["filler"] * 20)
    research = build(tmp, retired="must beat 0.3s :: #48\n",
                     extra_md="Any healing measurement must beat 0.3s.\n\n"
                              + far + "\n\nSomething unrelated was **withdrawn**.\n")
    code, out = run(research)
    return code == 1 and "[5]" in out, out


@case("[6] decision log gap warns but does not fail")
def t_log_warn(tmp):
    code, out = run(build(tmp, log="mentions nothing relevant"))
    return code == 0 and "WARN" in out and "[6]" in out, out


@case("[6] an IN PROGRESS spec is not asked for a log entry yet")
def t_inprogress_not_complete(tmp):
    spec = SPEC_OK.replace("**Status:** COMPLETE", "**Status:** IN PROGRESS")
    spec = spec.replace("## Results" + chr(10) + "r" + chr(10),
                        "## Results" + chr(10) + "*(to be filled)*" + chr(10))
    code, out = run(build(tmp, spec=spec, log="mentions nothing"))
    return code == 0 and "[6]" not in out, out


@case("[1] DRAFT over a Decision still fires regardless of completeness gating")
def t_draft_still_fires(tmp):
    spec = SPEC_OK.replace("**Status:** COMPLETE", "**Status:** DRAFT — nothing yet")
    code, out = run(build(tmp, spec=spec, log="mentions study_a"))
    return code == 1 and "[1]" in out, out


@case("[5] a spec QUOTING the claim it tests passes (exemption works where meant)")
def t_retired_quoted_in_spec(tmp):
    # A pre-registration must name the claim it is about to retire. Six real
    # sites do exactly this; without the exemption every one of them fails.
    quoted_q = '## Research question\nIs the "must beat 0.3s" figure real?\n'
    spec = SPEC_OK.replace("## Research question", quoted_q)
    research = build(tmp, spec=spec, retired="must beat 0.3s :: #48\n")
    code, out = run(research)
    return code == 0, out


@case("[5] a QUOTED claim outside a spec still fails (endorsement is not a report)")
def t_retired_quoted_outside_spec(tmp):
    # Narrowed in review round 1 of #67. In a positioning document a quoted
    # claim is often an endorsement -- 'recent work confirms "X"' asserts X --
    # and that is the pattern a human grep is worst at spotting, so it must not
    # be exempt outside SPEC.md.
    endorsement = 'Recent work confirms "must beat 0.3s" and we build on it.\n'
    research = build(tmp, retired="must beat 0.3s :: #48\n",
                     extra_md=endorsement)
    code, out = run(research)
    return code == 1 and "[5]" in out, out


def main() -> int:
    passed = failed = 0
    for name, fn in CASES:
        tmp = tempfile.mkdtemp(prefix="cr_test_")
        try:
            ok, out = fn(tmp)
        except Exception as e:                      # noqa: BLE001
            ok, out = False, f"EXCEPTION: {e!r}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if ok:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}")
            for line in out.splitlines()[:8]:
                print(f"          {line}")
    print(f"\n{passed}/{passed + failed} checks behave as specified")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
