#!/usr/bin/env python3
"""report_x12.py on synthetic records with known answers (AMENDMENT-12 quantities).

Usage: python3 tests/test_report_x12.py [DIR_WITH_report_x12.py]
"""
import json
import os
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "run"))
import report_x12  # noqa: E402

fails = 0


def check(name, got, want, tol=1e-9):
    global fails
    ok = (got is not None and abs(got - want) <= tol * max(1, abs(want))) if isinstance(want, float) else got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    fails += not ok


T = tempfile.mkdtemp()
raw = os.path.join(T, "x12")
os.makedirs(os.path.join(raw, "preround"))
with open(os.path.join(raw, "preround", "g1-calibration.jsonl"), "w") as f:
    for _ in range(3):
        f.write(json.dumps({"record": "calibration", "frame": 64, "burst": 32, "gen": {"accepted_pps": 1.2e6}}) + "\n")
with open(os.path.join(raw, "preround", "g9-null.jsonl"), "w") as f:
    for _ in range(3):
        f.write(json.dumps({"record": "null-forwarder", "frame": 64, "delivered_pps_steady": 9.0e5, "flags": []}) + "\n")

FLAT = [(t, 1000, {"4": 2500000, "6": 2500000, "8": 2500000, "10": 2500000}) for t in (1.0, 2.0, 3.0, 4.0, 5.0)]
WOBBLY = [(t, 1000, {"4": 2500000 if k % 2 else 3200000, "6": 2500000, "8": 2500000, "10": 2500000})
          for k, t in enumerate((1.0, 2.0, 3.0, 4.0, 5.0))]          # cpu4 spreads > 3 %: G8 fails

# (build, runtime) -> C per replicate; at-docs-perf: r2..r4 fail G8 at 49000, so G8-removed leaves 48000
C = {("cm-o0-L1-E1", "none"): 1000.0, ("cm-o0-L1-E1", "logoff"): 3000.0, ("cm-o0-L1-E1", "tutorial"): 750.0,
     ("cm-o3-L1-E1", "none"): 5000.0, ("cm-o3-L1-E1", "logoff"): 40000.0,
     ("cm-o0-L0-E1", "none"): 8000.0, ("cm-o3-L0-E1", "none"): 50000.0,
     ("at-tutorial", "none"): 1100.0, ("at-tutorial", "tutorial"): 800.0, ("at-docs-perf", "none"): None}
recs = [{"record": "run-start", "governor": "performance", "no_turbo": "1"}]
for (b, rt), c in C.items():
    for r in range(5):
        iid = f"x12-{b}-{rt}-r{r}"
        g8 = b == "at-docs-perf" and r >= 2
        v = (49000.0 if g8 else 48000.0) if c is None else c
        recs += [{"record": "instance", "instance": iid, "build": b, "program": "l3", "runtime": rt, "probe_ok": True},
                 {"record": "trial", "instance": iid, "build": b, "program": "l3", "runtime": rt, "kind": "sat",
                  "frame": 64, "flows": 1, "burst": 32, "duration": 10, "delivered_pps_steady": v, "flags": [],
                  "cpu_busy_per_cpu": {}, "g4_window_s": 10, "samples": WOBBLY if g8 else FLAT},
                 {"record": "instance-end", "instance": iid, "binary_unchanged": True}]
with open(os.path.join(raw, "x12.jsonl"), "w") as f:
    f.write("\n".join(json.dumps(x) for x in recs) + "\n")

out = os.path.join(T, "out")
report_x12.main([raw, out])
o = json.load(open(os.path.join(out, "x12.json")))
q, s = o["ratios"], o["sensitivity_g8_removed"]
check("-O0: L1 -L off / L1 none", q["-O0: C(L1, -L off) / C(L1, none)"]["ratio"], 3.0)
check("-O0: L1 -L off / L0 none", q["-O0: C(L1, -L off) / C(L0, none)"]["ratio"], 0.375)
check("-O3: L1 -L off / L1 none", q["-O3: C(L1, -L off) / C(L1, none)"]["ratio"], 8.0)
check("-O3: L1 -L off / L0 none", q["-O3: C(L1, -L off) / C(L0, none)"]["ratio"], 0.8)
check("at-tutorial / cm-o0-L1-E1, none", q["C(at-tutorial, none) / C(cm-o0-L1-E1, none)"]["ratio"], 1.1)
check("at-tutorial / cm-o0-L1-E1, tutorial", q["C(at-tutorial, tutorial) / C(cm-o0-L1-E1, tutorial)"]["ratio"], 800 / 750)
check("docs / at-tutorial tutorial (G8 trials kept)", q["C(at-docs-perf, none) / C(at-tutorial, tutorial)"]["ratio"], 49000 / 800)
check("... and with G8-failing trials removed", s["C(at-docs-perf, none) / C(at-tutorial, tutorial)"]["ratio"], 60.0)
check("every cell has 5 instances", sorted({v["n"] for v in o["cells"].values()}), [5])
check("markdown written", os.path.getsize(os.path.join(out, "x12.md")) > 500, True)
print("ALL OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
