#!/usr/bin/env python3
"""Mechanical checks on the research record. Fails CI on drift a script can see.

`check_index.py` proved the shape works: it checks one thing that can be
mechanised, refuses to judge prose, and turned "the index drifted again" into a
red X. This extends the same philosophy to five more defect classes, every one
of which was found by a HUMAN audit in one session and none of which the
pipeline caught:

  1. a SPEC whose Status says DRAFT while it carries Results and a Decision
  2. a SPEC missing a section SPEC_TEMPLATE.md marks required
  3. a study directory with no README
  4. a results/ directory with no script beside it that could have produced it
  5. a retired claim still stated unqualified somewhere  <- the important one
  6. DECISION_LOG.md not mentioning a completed study    <- advisory only

WHAT THIS DELIBERATELY DOES NOT DO, same as check_index.py: judge whether a
description is accurate. A row reading "Not started" for finished work passes.
Judging prose needs judgement and cannot be mechanised; the honest claim is
narrow.

Two tiers, on purpose. FAIL is for defects with no legitimate instance -- a
DRAFT header over a Decision is always wrong. WARN is for signals with real
exceptions, reported without failing the build, because a checker that cries
wolf gets switched off and then catches nothing at all.

Check 5 is the one worth having. When #48 withdrew the "~0.3 s bound", its
consequence note named two files and four other sites survived for a day. A
retirement recorded as a searchable string in `retired_claims.txt` cannot be
under-enumerated that way.

Usage:
    python research/check_research.py            # exit 1 on any FAIL
    python research/check_research.py --list     # report everything, exit 0
"""
from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))


try:  # Windows cp1252 stdout cannot print the em-dashes these files contain
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _paths():
    """Resolved lazily so tests can point the checks at a fixture tree."""
    return {
        "template": os.path.join(HERE, "SPEC_TEMPLATE.md"),
        "log": os.path.join(HERE, "DECISION_LOG.md"),
        "retired": os.path.join(HERE, "retired_claims.txt"),
    }

# Directories under research/ that are not studies.
NOT_A_STUDY = {"__pycache__", "postmortems", "claim_corrections"}

# A line stating a retired claim is fine if it also carries one of these.
# Deliberately generous: the goal is to catch a claim asserted as live, not to
# police how a withdrawal is worded.
WINDOW = 4          # lines either side searched for a withdrawal marker

WITHDRAWAL_MARKERS = (
    "withdraw", "corrected", "supersed", "retired", "struck", "~~",
    "must not", "do not claim", "no longer", "was wrong", "falsified",
    "void", "amended", "not established", "refuted",
    # Added 2026-09-10 (#67). Every marker above describes a claim being
    # WITHDRAWN by its author. None describes a PREDICTION that was tested and
    # failed, which is a different thing and is written differently: nobody says
    # "we withdraw our prediction", they say it did not hold. The first real use
    # of this check after #54 flagged RELATED_WORK section 4's honest report of
    # exactly that as an unqualified retired claim.
    "did not hold", "not observed", "not confirmed", "did not reproduce",
    "was tested and", "predicted",
)

# Sections a SPEC must have once it carries results. Taken from SPEC_TEMPLATE.md
# at the time of writing and re-derived from the template on every run, so the
# two cannot drift apart.
REQUIRED_WHEN_COMPLETE = ("Results", "Interpretation", "Decision")


def study_dirs():
    out = []
    for name in sorted(os.listdir(HERE)):
        p = os.path.join(HERE, name)
        if os.path.isdir(p) and name not in NOT_A_STUDY and not name.startswith("."):
            out.append(name)
    return out


def template_sections():
    """The `## ` headings SPEC_TEMPLATE.md declares, minus the ones a spec only
    fills in later. Read from the file so adding a required section to the
    template automatically starts being checked."""
    t = _paths()["template"]
    if not os.path.exists(t):
        return []
    text = open(t, encoding="utf-8").read()
    return re.findall(r"^## (.+)$", text, re.M)


def sections_of(text):
    return [h.strip() for h in re.findall(r"^#+ (.+)$", text, re.M)]


def has_section(secs, want):
    """A heading counts if it STARTS WITH the required name.

    Real specs qualify their headings -- `## Interpretation (current --
    supersedes the pilot section above)` is an Interpretation section, and an
    exact-string check calls it missing. That false positive was caught the
    first time this checker ran.
    """
    w = want.lower()
    return any(h.lower().startswith(w) for h in secs)


INSTRUMENT_RULE_DATE = "2026-09-03"   # DECISION_LOG: when the template gained it


def date_opened(text):
    m = re.search(r"^\*\*Date opened:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text, re.M)
    return m.group(1) if m else ""


def status_of(text):
    m = re.search(r"^\*\*Status:\*\*\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else ""


def quoted(line, phrase):
    """Is `phrase` inside a quoted span on this line?

    A retired claim appearing in quotes is being reported, not asserted:
    a spec names the claim it is about to test, a decision quotes the sentence
    it is replacing. Distinguishing the two is what keeps this check usable on
    the documents that do retirement properly.
    """
    low, p = line.lower(), phrase.lower()
    spans = re.finditer(
        '"[^"]*"'          # straight double quotes
        '|“[^”]*”'   # curly double quotes
        '|`[^`]*`'         # backticks
        "|'[^']*'",        # single quotes
        low)
    return any(p in m.group(0) for m in spans)


def load_retired():
    """(phrase, provenance) pairs. Comments and blank lines ignored."""
    out = []
    r = _paths()["retired"]
    if not os.path.exists(r):
        return out
    for line in open(r, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        phrase, _, prov = line.partition("::")
        phrase = phrase.strip()
        if phrase:
            out.append((phrase, prov.strip()))
    return out


def markdown_files():
    """Every tracked-looking .md in the repo, minus vendored/ignored trees."""
    skip = {".git", "__pycache__", "extern", "build", "node_modules"}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                yield os.path.join(base, f)


def main(argv=None) -> int:
    global HERE, ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true",
                    help="report everything and exit 0 regardless")
    ap.add_argument("--root", help="check this research/ directory instead "
                                   "(used by test_check_research.py)")
    args = ap.parse_args(argv)
    if args.root:
        HERE = os.path.abspath(args.root)
        ROOT = os.path.abspath(os.path.join(HERE, ".."))

    fails, warns = [], []
    studies = study_dirs()
    tmpl = template_sections()

    # ---- 1, 2, 3: per-study SPEC and README checks -----------------------
    for name in studies:
        d = os.path.join(HERE, name)
        spec = os.path.join(d, "SPEC.md")
        readme = os.path.join(d, "README.md")

        if not os.path.exists(readme):
            fails.append(
                f"[3] research/{name}/ has no README.md. Its results are only "
                f"legible to someone who reads the SPEC end to end.")

        if not os.path.exists(spec):
            continue
        text = open(spec, encoding="utf-8").read()
        secs = sections_of(text)
        status = status_of(text)
        # "Carries results" means the study is DONE, not merely that it has
        # the headings. A spec written up-front has `## Results` as a
        # placeholder, and treating that as complete asks a study that has not
        # run yet for a DECISION_LOG entry. Caught the first time a new spec was
        # written after this checker landed.
        complete = "complete" in status.lower()
        has_results = (has_section(secs, "Results")
                       and has_section(secs, "Decision")
                       and complete)
        # The DRAFT check is deliberately NOT gated on `complete` -- a spec
        # whose header says DRAFT while it carries a real Decision is exactly
        # the defect, and gating it on the header would make it unfireable.
        draft_over_decision = (has_section(secs, "Results")
                               and has_section(secs, "Decision"))

        if draft_over_decision and re.match(r"^DRAFT\b", status, re.I):
            fails.append(
                f"[1] research/{name}/SPEC.md says 'Status: {status[:60]}' while "
                f"carrying Results and a Decision. A reader trusts the header.")

        if has_results:
            for want in REQUIRED_WHEN_COMPLETE:
                if not has_section(secs, want):
                    fails.append(
                        f"[2] research/{name}/SPEC.md carries results but has no "
                        f"'## {want}' section (SPEC_TEMPLATE.md requires it).")
            # Substance, not formatting: three specs characterize their
            # instrument in a bolded paragraph rather than under an H2, and
            # demanding a heading level would have generated churn for nothing.
            # The requirement is that the characterization EXISTS.
            opened = date_opened(text)
            has_instr = (has_section(secs, "Instrument characterization")
                         or "instrument characterization" in text.lower())
            if opened and opened >= INSTRUMENT_RULE_DATE and not has_instr:
                fails.append(
                    f"[2] research/{name}/SPEC.md (opened {opened}) has no "
                    f"'## Instrument characterization'. Required by "
                    f"SPEC_TEMPLATE.md from {INSTRUMENT_RULE_DATE}, because "
                    f"three sweeps were voided by apparatus properties that "
                    f"were computable in advance. Specs opened before that "
                    f"date are exempt.")

        # ---- 4: results with no producer ---------------------------------
        res = [x for x in os.listdir(d)
               if x.startswith("results") and os.path.isdir(os.path.join(d, x))]
        if res and not any(x.endswith(".py") for x in os.listdir(d)):
            fails.append(
                f"[4] research/{name}/ commits {res[0]}/ but has no .py beside "
                f"it. #49 merged results produced by a scratch script that was "
                f"never committed; nobody can re-derive those numbers.")

        # ---- 6: decision log coverage (advisory) -------------------------
        _log = _paths()["log"]
        if has_results and os.path.exists(_log):
            log = open(_log, encoding="utf-8").read()
            if name not in log:
                warns.append(
                    f"[6] DECISION_LOG.md never mentions research/{name}/, which "
                    f"has a Decision. The log's own policy requires decisions "
                    f"that stop or redirect work be logged, not just commented.")

    # ---- 5: retired claims stated unqualified ----------------------------
    retired = load_retired()
    if not retired:
        warns.append("[5] research/retired_claims.txt is empty or missing -- the "
                     "retired-phrase check is not doing anything.")
    for path in markdown_files():
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if rel.endswith("retired_claims.txt"):
            continue
        # claim_corrections/ exists to state retired claims and say why they
        # died. Every file in it would trip this check by construction.
        if "/claim_corrections/" in "/" + rel:
            continue
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            low = line.lower()
            # A withdrawal is usually written in the lines AROUND the quoted
            # claim, not on the same line -- a spec quotes the dead sentence and
            # marks it withdrawn in the paragraph below. Checking only the line
            # itself produced false positives the first time this ran.
            window = " ".join(
                lines[max(0, i - 1 - WINDOW):i + WINDOW]).lower()
            for phrase, prov in retired:
                if phrase.lower() in low:
                    if any(m in window for m in WITHDRAWAL_MARKERS):
                        continue
                    # A retired phrase inside quotation marks is being QUOTED,
                    # not asserted -- and the documents that must quote it are
                    # exactly the ones that retire it. claim_corrections/ is
                    # already exempt wholesale; a study's own SPEC has the same
                    # need, because a pre-registration names the claim it is
                    # about to test. Added 2026-09-10 (#67), after arming the
                    # registry produced six failures and every one of them was
                    # a quotation in a spec that retired the claim.
                    if quoted(line, phrase):
                        continue
                    fails.append(
                        f"[5] {rel}:{i} states a retired claim without a "
                        f"withdrawal marker:\n        \"{phrase}\"\n"
                        f"        retired by: {prov}\n"
                        f"        line: {line.strip()[:110]}")

    # ---- report -----------------------------------------------------------
    print(f"studies checked : {len(studies)}")
    print(f"retired phrases : {len(retired)}")
    print()
    for w in warns:
        print(f"WARN  {w}")
    if warns:
        print()
    for f in fails:
        print(f"FAIL  {f}")

    if not fails:
        print("\nOK: no mechanical defects found."
              + (f" ({len(warns)} advisory warning(s) above.)" if warns else ""))
        return 0
    print(f"\n{len(fails)} failure(s). Each is mechanical: fix the file, or if the "
          f"check is wrong, change the check and say why in the same PR.")
    return 0 if args.list else 1


if __name__ == "__main__":
    sys.exit(main())
