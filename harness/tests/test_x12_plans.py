#!/usr/bin/env python3
"""The AMENDMENT-12 plan is what the amendment registers (make_plans.py --x12), and the build and
runtime it adds are wired as registered.

    python3 harness/tests/test_x12_plans.py
"""
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "run"))
import make_plans as M  # noqa: E402

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


p = M.x12()
want = {("cm-o0-L1-E1", "none"), ("cm-o0-L1-E1", "logoff"), ("cm-o0-L1-E1", "tutorial"),
        ("cm-o3-L1-E1", "none"), ("cm-o3-L1-E1", "logoff"), ("cm-o0-L0-E1", "none"), ("cm-o3-L0-E1", "none"),
        ("at-tutorial", "none"), ("at-tutorial", "tutorial"), ("at-docs-perf", "none")}
cells = Counter((i["build"], i.get("runtime", "none")) for i in p["instances"])
check("X12: the 10 registered (build, runtime) cells", set(cells) == want)
check("X12: 5 instances per cell, 50 in all", all(n == 5 for n in cells.values()) and len(p["instances"]) == 50)
check("X12: one replicate per block, blocks 0-4",
      all(sorted(i["block"] for i in p["instances"] if (i["build"], i.get("runtime", "none")) == c) == [0, 1, 2, 3, 4]
          for c in want))
check("X12: l3, E1's sat at 64 B only", all(i["program"] == "l3" and i["trials"] == [M.SAT(64)] for i in p["instances"]))
check("X12: seed 121", p["shuffle_seed"] == 121)
check("X12: instance ids unique", len({i["id"] for i in p["instances"]}) == 50)

# the build at-tutorial: autotools, C++ flags only, as the tutorials' script (matrix.tsv)
rows = {l.split("\t")[0]: l.split("\t") for l in open(os.path.join(HERE, "..", "builds", "matrix.tsv"))
        if l.strip() and not l.startswith("#")}
r = rows.get("at-tutorial", [])
check("at-tutorial: autotools, cflags 'cxx:-O0 -g', options --with-thrift only",
      r[1:4] == ["autotools", "cxx:-O0 -g", "--with-thrift"])
bm = open(os.path.join(HERE, "..", "builds", "build_matrix.sh")).read()
check("build_matrix.sh: cxx: sets CXXFLAGS only", 'cxx:*) cfg+=("CXXFLAGS=${cflags#cxx:}")' in bm)

print("ALL OK" if not fails else f"FAILED: {len(fails)}")
sys.exit(1 if fails else 0)
