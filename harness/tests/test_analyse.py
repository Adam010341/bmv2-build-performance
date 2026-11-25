#!/usr/bin/env python3
"""Synthetic-record test for analyse.py: gates must exclude what they should, and only that.
"""
import json, os, subprocess, sys, tempfile

def rec(inst, build, C, accepted=1000, h0=1000, sw0=1000, drop=0, sw1=900, h1=900, flags=None):
    c0 = {"sw": {"sw0": {"rx_packets": 0, "rx_dropped": 0}, "sw1": {"tx_packets": 0}},
          "h0": {"tx_packets": 0}, "h1": {"rx_packets": 0}}
    c1 = {"sw": {"sw0": {"rx_packets": sw0, "rx_dropped": drop}, "sw1": {"tx_packets": sw1}},
          "h0": {"tx_packets": h0}, "h1": {"rx_packets": h1}}
    return {"record": "trial", "kind": "sat", "instance": inst, "build": build, "program": "l3",
            "runtime": "none", "frame": 64, "flows": 1, "duration": 10, "gen": {"accepted": accepted},
            "counters_before": c0, "counters_after": c1, "delivered_pps_steady": C,
            "flags": flags or [], "samples": []}

rows = [rec(f"a{i}", "cm-o3-L0-E0", 100000 + i) for i in range(5)]
rows += [rec(f"b{i}", "cm-o0-L0-E0", 20000 + i) for i in range(5)]
rows.append(rec("bad-drain", "cm-o3-L0-E0", 1, flags=["no-drain"]))          # must be excluded
rows.append(rec("bad-foreign", "cm-o3-L0-E0", 1, flags=["G4"]))              # G4 after rerun: excluded
rows.append(rec("gen-bounded", "cm-o3-L0-E0", 100002, flags=["generator-bounded"]))  # kept, lower bound
# G8 looks at the steady window only (AMENDMENT-6): an idle CPU's frequency before the load starts
# and after it ends is not a spread; a dip inside the window is.
steady = [[0.5 * k, None, {"4": 2500000}] for k in range(1, 20)]
idle_ends = rec("freq-idle-ends", "cm-o0-L0-E0", 20005)
idle_ends["samples"] = [[0.015, None, {"4": 800000}]] + steady + [[10.18, None, {"4": 800000}]]
dip = rec("freq-dip", "cm-o0-L0-E0", 20006)
dip["samples"] = [[0.015, None, {"4": 800000}]] + [x if x[0] != 5.0 else [5.0, None, {"4": 2300000}] for x in steady]
rows += [idle_ends, dip]
d = tempfile.mkdtemp()
raw = os.path.join(d, "raw.jsonl")
open(raw, "w").write("\n".join(json.dumps(r) for r in rows) + "\n")
out = os.path.join(d, "out.json")
subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "run", "analyse.py"), raw, "--json", out],
               check=True, capture_output=True)
rep = json.load(open(out))
failed = {g["instance"]: g["failed"] for g in rep["gate_failures"]}
ok = True
def check(name, got, want):
    global ok
    print(("  ok   " if got == want else "  FAIL ") + f"{name}: {got}" + ("" if got == want else f" (want {want})"))
    ok &= got == want
check("flagged instances", sorted(failed), ["bad-drain", "bad-foreign", "freq-dip", "gen-bounded"])
check("G8 ignores idle samples outside the steady window", failed.get("freq-idle-ends"), None)
check("G8 catches a dip inside the window", failed.get("freq-dip"), ["G8:cpu4"])
check("drain flag", failed.get("bad-drain"), ["no-drain"])
check("lower bound marked", rep["cells"]["cm-o3-L0-E0|l3|none|64|1"]["lower_bound_generator"], True)
check("o3 cell n", rep["cells"]["cm-o3-L0-E0|l3|none|64|1"]["n"], 6)
check("o3 median", rep["cells"]["cm-o3-L0-E0|l3|none|64|1"]["median"], 100002)
r = rep["ratios"]["l3|64|cm-o3-L0-E0/cm-o0-L0-E0"]
check("ratio ~5", round(r["ratio"], 2), 5.0)
print("ALL OK" if ok else "FAILED"); sys.exit(0 if ok else 1)
