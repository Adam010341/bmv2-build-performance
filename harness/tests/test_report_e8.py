#!/usr/bin/env python3
"""report_e8.py on synthetic records: own-frame capacity, the ratio, and the not-ours cross-check.

P1 sends no frames of its own; P2 sends 10 frames/s that bmrecv counts as `skipped` (too short),
as C0272's GNN frames are. Usage: python3 tests/test_report_e8.py [DIR_WITH_report_e8.py]
"""
import json
import os
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "run"))
import report_e8  # noqa: E402

fails = 0


def check(name, got, want, tol=1e-9):
    global fails
    ok = (abs(got - want) <= tol * max(1, abs(want))) if isinstance(want, float) and got is not None else got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    fails += not ok


T = tempfile.mkdtemp()
raw, e8 = os.path.join(T, "raw"), os.path.join(T, "e8")
os.makedirs(os.path.join(raw, "preround"))
os.makedirs(e8)
with open(os.path.join(raw, "preround", "g1-calibration.jsonl"), "w") as f:
    for fr in (64, 1518):
        for _ in range(3):
            f.write(json.dumps({"record": "calibration", "frame": fr, "burst": 32, "gen": {"accepted_pps": 1.0e6}}) + "\n")
with open(os.path.join(raw, "preround", "g9-null.jsonl"), "w") as f:
    for fr in (64, 1518):
        for _ in range(3):
            f.write(json.dumps({"record": "null-forwarder", "frame": fr, "delivered_pps_steady": 9.0e5, "flags": []}) + "\n")

C = {("cm-default", "P1"): 1000.0, ("at-docs-perf", "P1"): 36000.0,
     ("cm-default", "P2"): 6010.0, ("at-docs-perf", "P2"): 32010.0}
SKIP = {"P1": 0, "P2": 100}                      # frames of the program's own per 10 s trial
recs = []
for (b, p), c in C.items():
    for r in range(5):
        iid = f"e8-{b}-{p}-r{r}"
        recs.append({"record": "instance", "instance": iid, "build": b, "program": p, "probe_ok": True})
        for fr in (64, 1518):
            ours = int(c * 10) - SKIP[p]
            bad = p == "P2" and b == "cm-default" and r == 0 and fr == 64      # one of ours lost after h1
            recs.append({"record": "trial", "instance": iid, "build": b, "program": p, "kind": "sat", "frame": fr,
                         "flows": 1, "burst": 32, "duration": 10, "delivered_pps_steady": c,
                         "delivered": int(c * 10), "flags": [], "samples": [],
                         "cpu_busy_per_cpu": {}, "g4_window_s": 10.0,
                         "recv": {"rx": ours - (1 if bad else 0), "foreign": 0, "skipped": SKIP[p], "stale": 0}})
        recs.append({"record": "instance-end", "instance": iid, "binary_unchanged": True})
recs.append({"record": "instance", "instance": "hit-e8-P1", "program": "P1", "probe_ok": True, "hitcheck_ok": True,
             "hitcheck": {"profile": {"frames": 10, "tables": {"t": {"want": "hit", "hit": 10, "miss": 0, "frames_ok": 10}},
                                      "missing_from_profile": []}}, "probe": {"recv": {"rx": 200, "foreign": 0}}})
with open(os.path.join(e8, "e8.jsonl"), "w") as f:
    f.write("\n".join(json.dumps(r) for r in recs if not r["instance"].startswith("hit-")) + "\n")
with open(os.path.join(e8, "e8hit.jsonl"), "w") as f:
    f.write(json.dumps(recs[-1]) + "\n")

report_e8.main([raw, e8, os.path.join(T, "out")])
o = json.load(open(os.path.join(T, "out", "e8.json")))
rt = o["ratio_docs_perf_over_default"]
print("P1: no frames of its own")
check("own ratio = counter ratio = 36", rt["P1|64"]["own"]["ratio"], 36.0)
check("counter ratio", rt["P1|64"]["counter"]["ratio"], 36.0)
print("P2: 10 frames/s of its own, counted by bmrecv as skipped")
check("own C default (6010 - 10)", o["cells_own"]["cm-default|P2|1518"]["median"], 6000.0)
check("own C perf (32010 - 10)", o["cells_own"]["at-docs-perf|P2|1518"]["median"], 32000.0)
check("own ratio 32000/6000", rt["P2|1518"]["own"]["ratio"], 32000.0 / 6000.0)
check("counter ratio 32010/6010", rt["P2|1518"]["counter"]["ratio"], 32010.0 / 6010.0)
check("not-ours pps", o["foreign_pps_median"]["cm-default|P2|64"], 10.0)
print("cross-check: a trial where foreign + skipped != h1 counter - receiver rx is listed")
check("mismatch", o["not_ours_vs_counter_mismatch"], ["e8-cm-default-P2-r0"])
print("G10 summary")
check("P1 hitcheck", o["g10_path_profile"]["P1"]["hitcheck_ok"], True)
md = open(os.path.join(T, "out", "e8.md")).read()
check("md has the ratio row", "| P2\\|1518 | 5.3333 |" in md, True)
print("ALL OK" if not fails else f"FAILED ({fails})")
sys.exit(1 if fails else 0)
