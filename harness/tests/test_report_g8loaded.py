#!/usr/bin/env python3
"""report_g8loaded.py on synthetic records with known answers (AMENDMENT-14 (a)).

Usage: python3 tests/test_report_g8loaded.py [DIR_WITH_report_g8loaded.py]
"""
import json
import os
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "run"))
import report_g8loaded as G  # noqa: E402

fails = 0


def check(name, got, want, tol=1e-9):
    global fails
    ok = (got is not None and abs(got - want) <= tol * max(1, abs(want))) if isinstance(want, float) else got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    fails += not ok


# the rule, case by case
f = G.g8_loaded_fails
check("loaded CPU flagged -> fails", f({"cpu_busy_per_cpu": {"4": 0.95, "6": 0.3}}, ["G8:cpu4"]), True)
check("only an idle CPU flagged -> passes", f({"cpu_busy_per_cpu": {"4": 0.95, "6": 0.3}}, ["G8:cpu6"]), False)
check("exactly 0.90 counts as loaded", f({"cpu_busy_per_cpu": {"4": 0.90}}, ["G8:cpu4"]), True)
check("no loaded CPU -> judged on all, flagged", f({"cpu_busy_per_cpu": {"4": 0.5, "6": 0.4}}, ["G8:cpu6"]), True)
check("no loaded CPU, no G8 -> passes", f({"cpu_busy_per_cpu": {"4": 0.5}}, ["G4"]), False)
check("no busy record -> judged on all", f({}, ["G8:cpu4"]), True)
r = {"cpu_busy_per_cpu": {"4": 0.5, "6": 0.3}, "g4_window_s": 20, "duration": 10}   # normalised: 1.0, 0.6
check("raw: no loaded CPU, cpu6 flagged -> fails", f(r, ["G8:cpu6"]), True)
check("normalised: cpu4 loaded, only cpu6 flagged -> passes", f(r, ["G8:cpu6"], normalised=True), False)
check("normalised: cpu4 loaded and flagged -> fails", f(r, ["G8:cpu4"], normalised=True), True)

# end to end through report_x12's quantities
T = tempfile.mkdtemp()
raw = os.path.join(T, "x12")
os.makedirs(os.path.join(raw, "preround"))
with open(os.path.join(raw, "preround", "g1-calibration.jsonl"), "w") as fh:
    for _ in range(3):
        fh.write(json.dumps({"record": "calibration", "frame": 64, "burst": 32, "gen": {"accepted_pps": 1.2e6}}) + "\n")
with open(os.path.join(raw, "preround", "g9-null.jsonl"), "w") as fh:
    for _ in range(3):
        fh.write(json.dumps({"record": "null-forwarder", "frame": 64, "delivered_pps_steady": 9.0e5, "flags": []}) + "\n")

FLAT = [(t, 1000, {"4": 2500000, "6": 2500000, "8": 2500000, "10": 2500000}) for t in (1.0, 2.0, 3.0, 4.0, 5.0)]
WOBBLY = [(t, 1000, {"4": 2500000 if k % 2 else 3200000, "6": 2500000, "8": 2500000, "10": 2500000})
          for k, t in enumerate((1.0, 2.0, 3.0, 4.0, 5.0))]          # cpu4 spreads > 3 %: G8:cpu4
# at-docs-perf r2..r4 fail G8 on cpu4 at 49000 (r0, r1: 48000). r2: cpu4 is the loaded one -> G8-loaded
# fails; r3, r4: cpu6 is loaded and steady, cpu4 idle -> G8-loaded passes.
BUSY = {2: {"4": 0.95, "6": 0.2}, 3: {"4": 0.3, "6": 0.97}, 4: {"4": 0.3, "6": 0.97}}
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
                  "cpu_busy_per_cpu": BUSY.get(r, {}) if g8 else {}, "g4_window_s": 10,
                  "samples": WOBBLY if g8 else FLAT},
                 {"record": "instance-end", "instance": iid, "binary_unchanged": True}]
with open(os.path.join(raw, "x12.jsonl"), "w") as fh:
    fh.write("\n".join(json.dumps(x) for x in recs) + "\n")

res = G.x12(raw)
key = "C(at-docs-perf, none) / C(at-tutorial, tutorial)"
check("registered: all five kept", res["registered"]["X12"][key]["ratio"], 49000 / 800)
check("G8 removed: r2..r4 dropped", res["G8 removed"]["X12"][key]["ratio"], 48000 / 800)
check("G8-loaded removed: only r2 dropped", res["G8-loaded removed"]["X12"][key]["ratio"], 48500 / 800)
check("G8-loaded counts", G.trial_counts([os.path.join(raw, "x12.jsonl")])["x12.jsonl"],
      {"trials": 50, "fail_G8": 3, "fail_G8_loaded": 1, "fail_G8_loaded_normalised": 1})
print("ALL OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
