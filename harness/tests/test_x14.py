#!/usr/bin/env python3
"""AMENDMENT-14 (c): the X14 plan is what the amendment registers, arms.tcp_result reads iperf3's TCP
report, and report_x14.py gives known answers on synthetic records.

    python3 -I harness/tests/test_x14.py [DIR_WITH_THE_RUN_SCRIPTS]
"""
import json
import os
import sys
import tempfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "run"))
import arms  # noqa: E402
import make_plans as M  # noqa: E402
import report_x14  # noqa: E402

fails = 0


def check(name, got, want, tol=1e-9):
    global fails
    ok = (got is not None and abs(got - want) <= tol * max(1, abs(want))) if isinstance(want, float) else got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    fails += not ok


# the plan
p = M.x14()
cells = Counter((i["build"], i.get("runtime", "none")) for i in p["instances"])
check("X14 cells", set(cells), {("cm-default", "none"), ("at-docs-perf", "none"), ("at-tutorial", "tutorial")})
check("X14: 10 instances per cell", sorted(cells.values()), [10, 10, 10])
check("X14: seed 141", p["shuffle_seed"], 141)
check("X14: one 10 s iperf3-tcp trial on l3",
      all(i["program"] == "l3" and i["trials"] == [{"kind": "iperf3-tcp", "duration": 10}] for i in p["instances"]), True)
check("X14: blocks 0-9 per cell", all(sorted(i["block"] for i in p["instances"] if (i["build"], i.get("runtime", "none")) == c)
                                      == list(range(10)) for c in cells), True)

# iperf3 3.16 TCP client report (layout of real --json output, trimmed)
cj = {"end": {"sum_sent": {"start": 0, "end": 10.0, "seconds": 10.0, "bytes": 125000000, "bits_per_second": 1.0e8,
                           "retransmits": 7, "sender": True},
              "sum_received": {"start": 0, "end": 10.04, "seconds": 10.04, "bytes": 124000000,
                               "bits_per_second": 9.88e7, "sender": True}}}
t = arms.tcp_result(cj)
check("goodput from sum_received", t["iperf3_received_bps"], 9.88e7)
check("retransmits from sum_sent", t["retransmits"], 7)
check("no report -> None", arms.tcp_result({"error": "unable to connect"}), None)
check("no JSON -> None", arms.tcp_result(None), None)

# report_x14 on synthetic records
T = tempfile.mkdtemp()
os.makedirs(os.path.join(T, "preround"))
for name, rec in (("g1-calibration.jsonl", {"record": "calibration", "frame": 64, "burst": 32, "gen": {"accepted_pps": 1.2e6}}),
                  ("g9-null.jsonl", {"record": "null-forwarder", "frame": 64, "delivered_pps_steady": 9.0e5, "flags": []})):
    with open(os.path.join(T, "preround", name), "w") as f:
        f.write(json.dumps(rec) + "\n")
FLAT = [(t, None, {"4": 2500000, "6": 2500000, "8": 2500000, "10": 2500000}) for t in (1.0, 2.0, 3.0, 4.0, 5.0)]
WOBBLY = [(t, None, {"4": 2500000 if k % 2 else 3200000, "6": 2500000, "8": 2500000, "10": 2500000})
          for k, t in enumerate((1.0, 2.0, 3.0, 4.0, 5.0))]
GOOD = {("cm-default", "none"): 50e6, ("at-docs-perf", "none"): 1000e6, ("at-tutorial", "tutorial"): 20e6}
recs = []
for (b, rt), g in GOOD.items():
    for r in range(10):
        iid = f"x14-{b}-{rt}-r{r}"
        g8 = b == "at-docs-perf" and r >= 4          # six G8-failing trials at 1100 Mbit/s, four clean at 1000
        v = 1100e6 if g8 else g
        recs += [{"record": "instance", "instance": iid, "build": b, "program": "l3", "runtime": rt, "probe_ok": True},
                 {"record": "trial", "instance": iid, "build": b, "program": "l3", "runtime": rt, "kind": "iperf3-tcp",
                  "frame": 1514, "flows": 1, "duration": 10, "iperf3_received_bps": v, "retransmits": r, "flags": [],
                  "samples": WOBBLY if g8 else FLAT, "cpu_busy_per_cpu": {}, "g4_window_s": 12},
                 {"record": "instance-end", "instance": iid, "binary_unchanged": True}]
# one G4 trial must be excluded wherever it sits
recs.append({"record": "trial", "instance": "x14-cm-default-none-r0", "build": "cm-default", "program": "l3",
             "runtime": "none", "kind": "iperf3-tcp", "frame": 1514, "flows": 1, "duration": 10,
             "iperf3_received_bps": 1.0, "flags": ["G4"], "samples": FLAT, "attempt": 1, "superseded": True})
x14 = os.path.join(T, "x14.jsonl")
with open(x14, "w") as f:
    f.write("\n".join(json.dumps(x) for x in recs) + "\n")
out = os.path.join(T, "out")
report_x14.main([T, x14, out])
o = json.load(open(os.path.join(out, "x14.json")))
c = o["cells_goodput_bps"]
check("cm-default median goodput", c["cm-default|none"]["median"], 50e6)
check("at-docs-perf median (4 x 1000, 6 x 1100 Mbit/s)", c["at-docs-perf|none"]["median"], 1100e6)
check("ratio docs / cm-default", o["ratios"]["at-docs-perf / cm-default (none)"]["ratio"], 22.0)
check("ratio docs / at-tutorial (tutorial)", o["ratios"]["at-docs-perf / at-tutorial (tutorial)"]["ratio"], 55.0)
check("G8 removed: docs median of the four 1000s", o["sensitivity_g8_removed"]["at-docs-perf / cm-default (none)"]["ratio"], 20.0)
check("retransmits median per cell", o["retransmits_median"]["cm-default|none"], 4.5)
check("superseded G4 attempt not counted", c["cm-default|none"]["n"], 10)
check("markdown written", os.path.getsize(os.path.join(out, "x14.md")) > 300, True)
print("ALL OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
