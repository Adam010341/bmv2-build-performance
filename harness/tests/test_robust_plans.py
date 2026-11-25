#!/usr/bin/env python3
"""The robustness plans are what AMENDMENT-11 registers (make_plans.py --robust).

    python3 harness/tests/test_robust_plans.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "run"))
import make_plans as M  # noqa: E402
from bench import ordered_instances  # noqa: E402

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


CORNERS = {"cm-o0-L1-E1", "cm-o0-L1-E0", "cm-o0-L0-E1", "cm-o0-L0-E0",
           "cm-o3-L1-E1", "cm-o3-L1-E0", "cm-o3-L0-E1", "cm-o3-L0-E0"}
cells = lambda p: {(i["build"], i["program"]) for i in p["instances"]}
reps = lambda p: sorted({i["block"] for i in p["instances"]})

r1 = M.r1()
check("R1: the eight -O0/-O3 corner builds on l3", cells(r1) == {(b, "l3") for b in CORNERS})
check("R1: 5 replicates per build, one per block", len(r1["instances"]) == 40 and reps(r1) == [0, 1, 2, 3, 4])
check("R1: sat 64 B first, then rate at 2 x own C, order fixed",
      all(i["trials"][0] == M.SAT(64) and i["trials"][1]["kind"] == "rate"
          and i["trials"][1]["pps_frac_of_sat"] == 2.0 and i["trials"][1]["frame"] == 64
          and i["trials"][1]["burst"] == 32 and i["trials"][1]["duration"] == 10
          and i.get("shuffle_trials") is False for i in r1["instances"]))
check("R1: seed 111", r1["shuffle_seed"] == 111)

r2 = M.r2()
check("R2: the two cells cm-o0-L1-E1 and cm-o3-L0-E0 on l3",
      cells(r2) == {("cm-o0-L1-E1", "l3"), ("cm-o3-L0-E0", "l3")} and len(r2["instances"]) == 10)
check("R2: sat 10 s and sat 30 s, otherwise E1's sat",
      all(sorted(t["duration"] for t in i["trials"]) == [10, 30]
          and all({**t, "duration": 10} == M.SAT(64) for t in i["trials"]) for i in r2["instances"]))
check("R2: seed 112", r2["shuffle_seed"] == 112)

r3 = M.r3()
check("R3: corner builds on l3, 40 instances", cells(r3) == {(b, "l3") for b in CORNERS} and len(r3["instances"]) == 40)
check("R3: one-pair sat and two-pair sat",
      all(sorted(t.get("pairs", 1) for t in i["trials"]) == [1, 2]
          and all({k: v for k, v in t.items() if k != "pairs"} == M.SAT(64) for t in i["trials"]) for i in r3["instances"]))
check("R3: seed 113", r3["shuffle_seed"] == 113)

want4 = {(b, "l3") for b in CORNERS} | {(b, p) for b in ("cm-o0-L1-E1", "cm-o3-L0-E0") for p in ("min", "exact_8")}
for r in range(5):
    pb, pd = M.r4("bench", r), M.r4("default", r)
    check(f"R4 b{r}: same 12 cells in both modes", cells(pb) == cells(pd) == want4)
    check(f"R4 b{r}: one replicate, sat 64 B only", reps(pb) == reps(pd) == [r]
          and all(i["trials"] == [M.SAT(64)] for i in pb["instances"] + pd["instances"]))
    check(f"R4 b{r}: seed 114, mode in every instance", pb["shuffle_seed"] == pd["shuffle_seed"] == 114
          and all(i["cpu_mode"] == "bench" for i in pb["instances"])
          and all(i["cpu_mode"] == "default" for i in pd["instances"]))
    ob = [(i["build"], i["program"]) for i in ordered_instances(pb)]
    od = [(i["build"], i["program"]) for i in ordered_instances(pd)]
    check(f"R4 b{r}: same instance order in both modes", ob == od)
check("R4: instance ids unique across modes and replicates",
      len({i["id"] for r in range(5) for m in ("bench", "default") for i in M.r4(m, r)["instances"]}) == 120)

print(f"{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
