#!/usr/bin/env python3
"""AMENDMENT-13: the X13 plans measure the extension's program exactly as E8 measures its programs.

    python3 -I harness/tests/test_x13_plans.py [DIR_WITH_make_plans.py]
"""
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "run"))
import make_plans as M  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    fails += not ok


h, x, e = M.x13hit(), M.x13(), M.e8()
check("programs", M.X13_PROGRAMS, ("C0441",))
check("hitcheck: one instance on cm-default with console logging",
      [(i["build"], i["program"], i.get("hitcheck")) for i in h["instances"]], [("cm-default", "C0441", True)])
check("hitcheck path profile", h["instances"][0]["path_profile"], "../p4/e8/C0441/PATH.json")
check("measured cells", Counter((i["build"], i["program"]) for i in x["instances"]),
      Counter({("cm-default", "C0441"): 5, ("at-docs-perf", "C0441"): 5}))
e_trials = e["instances"][0]["trials"]
check("trials exactly as E8's (sat 64 and 1518, receiver on)", all(i["trials"] == e_trials for i in x["instances"]), True)
check("own frames counted as in E8", all(i["own_frames"] for i in x["instances"]), True)
check("generator fields from PATH.json", x["instances"][0]["gen"], {"dst_ip": "10.0.2.2", "src_ip": "10.0.1.1", "tos": 0})
check("program JSON", x["instances"][0]["program_json"], "../p4/e8/C0441/INDDoS.json")
check("seeds", (h["shuffle_seed"], x["shuffle_seed"]), (130, 131))
print("ALL OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
