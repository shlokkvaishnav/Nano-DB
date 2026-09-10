#!/usr/bin/env python3
"""
Produce this study's results (#69): #54's experiment at a 180 s horizon and 5x
finer resolution.

WHY THIS IS A WRAPPER AND NOT A COPY. The harness is
`../weaviate_dissociation/dissociation.py`, unmodified and shared. #69 changes
only two INSTRUMENT parameters, passed as flags:

    --observe-s 180   (from 60)   whether repair COMPLETES inside the window
    --queries   100   (from 20)   the RESOLUTION of index_recall, 1/(q*K)

Neither is an experimental variable. The manipulation under test -- chaos versus
no-chaos against a corpus-matched control -- is identical to #54's.

Copying the harness would have let the two studies drift apart silently. Editing
its constants in place would have meant re-running #54 no longer reproduces
#54's committed results, which is a worse failure: the study this one audits
would stop being auditable. So the defaults stay at #54's values and the
difference lives here, in one place, executable.

check_research.py [4] exists to stop results being committed with no script
beside them that could have produced them (#49 merged numbers from a scratch
script nobody kept). A wrapper naming the exact invocation satisfies that for
the right reason: the invocation IS the part of the method that is specific to
this study.

Usage:
    python research/weaviate_dissociation_180s/run_180s.py
    python research/weaviate_dissociation_180s/run_180s.py --dry-run
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
HARNESS = os.path.join(ROOT, "research", "weaviate_dissociation", "dissociation.py")

# Pre-registered in SPEC.md before any run. Changing either of these makes the
# results incomparable with #54 and with this study's own committed data.
OBSERVE_S = 180
QUERIES = 100


def main() -> int:
    if not os.path.exists(HARNESS):
        print(f"harness not found: {HARNESS}", file=sys.stderr)
        return 2
    cmd = [sys.executable, "-u", HARNESS,
           "--observe-s", str(OBSERVE_S),
           "--queries", str(QUERIES),
           "--out", os.path.join(HERE, "results")] + sys.argv[1:]
    print("  $ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(main())
