#!/usr/bin/env python3
"""report_robust.py on synthetic records with known answers (AMENDMENT-11 quantities).

Usage: python3 tests/test_report_robust.py [DIR_WITH_report_robust.py]
"""
import json
import os
import sys
import tempfile

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "run"))
import report_robust  # noqa: E402

fails = 0


def check(name, got, want, tol=1e-9):
    global fails
    ok = (got is not None and abs(got - want) <= tol * max(1, abs(want))) if isinstance(want, float) else got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    fails += not ok


T = tempfile.mkdtemp()
raw = os.path.join(T, "robust")
os.makedirs(os.path.join(raw, "preround"))
with open(os.path.join(raw, "preround", "g1-calibration.jsonl"), "w") as f:
    for _ in range(3):
        f.write(json.dumps({"record": "calibration", "frame": 64, "burst": 32, "gen": {"accepted_pps": 1.2e6}}) + "\n")
with open(os.path.join(raw, "preround", "g9-null.jsonl"), "w") as f:
    for _ in range(3):
        f.write(json.dumps({"record": "null-forwarder", "frame": 64, "delivered_pps_steady": 9.0e5, "flags": []}) + "\n")

CORNERS = report_robust.CORNERS
FLAT = [(t, 1000, {"4": 2500000, "6": 2500000, "8": 2500000, "10": 2500000}) for t in (1.0, 2.0, 3.0, 4.0, 5.0)]
WOBBLY = [(t, 1000, {"4": 2500000 if k % 2 else 3200000, "6": 2500000, "8": 2500000, "10": 2500000})
          for k, t in enumerate((1.0, 2.0, 3.0, 4.0, 5.0))]          # cpu4 spreads > 3 %: G8 fails


def trial(iid, b, p, kind, c, dur=10, g8=False, **kw):
    return {"record": "trial", "instance": iid, "build": b, "program": p, "kind": kind, "frame": 64, "flows": 1,
            "burst": 32, "duration": dur, "delivered_pps_steady": c, "flags": [], "cpu_busy_per_cpu": {},
            "g4_window_s": dur, "samples": WOBBLY if g8 else FLAT, **kw}


def inst(iid, b, p, trials):
    return [{"record": "instance", "instance": iid, "build": b, "program": p, "probe_ok": True}] + trials + \
        [{"record": "instance-end", "instance": iid, "binary_unchanged": True}]


def write(name, recs, start=None):
    with open(os.path.join(raw, name), "w") as f:
        if start:
            f.write(json.dumps({"record": "run-start", **start}) + "\n")
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")


# R1: C = 1000 (O0) / 10000 (O3); at 2C the switch delivers 0.9 C (O0) or 1.05 C (O3)
recs = []
for b in CORNERS:
    c = 1000.0 if "-o0-" in b else 10000.0
    for r in range(5):
        recs += inst(f"r1-{b}-l3-r{r}", b, "l3", [trial(f"r1-{b}-l3-r{r}", b, "l3", "sat", c),
                     trial(f"r1-{b}-l3-r{r}", b, "l3", "rate", c * (0.9 if "-o0-" in b else 1.05), pps_frac_of_sat=2.0)])
write("r1.jsonl", recs)
# R2: 30 s gives 2 % less on O0, the same on O3; one O3 30 s trial fails G8 (higher value: the median moves without it)
recs = []
for b, c30 in (("cm-o0-L1-E1", 980.0), ("cm-o3-L0-E0", 10000.0)):
    c10 = 1000.0 if b == "cm-o0-L1-E1" else 10000.0
    for r in range(5):
        iid = f"r2-{b}-l3-r{r}"
        odd = b == "cm-o3-L0-E0" and r in (0, 1, 2)
        t10, t30 = trial(iid, b, "l3", "sat", c10), trial(iid, b, "l3", "sat", 10400.0 if odd else c30, dur=30, g8=odd)
        extra = []
        if b == "cm-o0-L1-E1" and r == 4:      # 30 s: G4 on both attempts; 10 s: no-drain, never rerun
            extra = [{**t30, "flags": ["G4"], "superseded": True, "attempt": 1}]
            t30, t10 = {**t30, "flags": ["G4"], "attempt": 2}, {**t10, "flags": ["no-drain"]}
        recs += inst(iid, b, "l3", extra + [t10, t30])
write("r2.jsonl", recs)
# R3: one pair C; two pairs: pair 1 = 0.6 C, pair 2 = 0.5 C -> total 1.1 C
recs = []
for b in CORNERS:
    c = 1000.0 if "-o0-" in b else 10000.0
    for r in range(5):
        iid = f"r3-{b}-l3-r{r}"
        recs += inst(iid, b, "l3", [trial(iid, b, "l3", "sat", c),
                                    trial(iid, b, "l3", "sat", 0.6 * c, pairs=2, delivered_pps_steady_total=1.1 * c,
                                          pair2={"delivered_pps_steady": 0.5 * c})])
write("r3.jsonl", recs)
# R4: default mode 1.2x faster on O3, 1.0x on O0; min/exact_8 to give OH; b3's default file was actually in bench mode
CB = {("cm-o0-L1-E1", "min"): 2000.0, ("cm-o0-L1-E1", "exact_8"): 1000.0,
      ("cm-o3-L0-E0", "min"): 20000.0, ("cm-o3-L0-E0", "exact_8"): 16000.0}
for r in range(5):
    for m in ("bench", "default"):
        recs = []
        for b in CORNERS:
            c = 1000.0 if "-o0-" in b else 10000.0
            CB[(b, "l3")] = c
        for (b, p), c in CB.items():
            iid = f"r4-{m}-{b}-{p}-r{r}"
            v = c * (1.2 if m == "default" and "-o3-" in b else 1.0)
            if m == "default" and r == 3:
                v = 999999.0                                          # must be left out by the mode check
            recs += inst(iid, b, p, [trial(iid, b, p, "sat", v)])
        start = {"governor": "performance", "no_turbo": "1"} if m == "bench" or r == 3 else {"governor": "powersave", "no_turbo": "0"}
        write(f"r4{m}b{r}.jsonl", recs, start)

report_robust.main([raw, os.path.join(T, "out")])
o = json.load(open(os.path.join(T, "out", "robust.json")))
md = open(os.path.join(T, "out", "robust.md")).read()

print("R1 overload")
check("O0 delivered at 2C / C", o["R1"]["per_build"]["cm-o0-L1-E1"]["delivered_at_2C_over_C"]["ratio"], 0.9)
check("O0 |delivered - C| / C", o["R1"]["per_build"]["cm-o0-L1-E1"]["abs_delivered_minus_C_over_C"]["value"], 0.1)
check("O3 |delivered - C| / C", o["R1"]["per_build"]["cm-o3-L0-E0"]["abs_delivered_minus_C_over_C"]["value"], 0.05)
print("R2 trial length")
check("O0 C30/C10", o["R2"]["C30_over_C10"]["cm-o0-L1-E1"]["ratio"], 0.98)
check("O3 C30/C10 (3 of 5 at 10400)", o["R2"]["C30_over_C10"]["cm-o3-L0-E0"]["ratio"], 1.04)
check("O3 C30/C10, G8 removed", o["sensitivity_g8_removed"]["R2"]["C30_over_C10"]["cm-o3-L0-E0"]["ratio"], 1.0)
check("R2 cells kept apart by duration (4 cells)", len(o["R2"]["cells"]), 4)
check("30 s trial: rerun also failed", o["R2"]["cells"]["cm-o0-L1-E1|30"]["excluded_trials"][0]["first_attempt_failed"], ["G4"])
check("10 s trial of the same instance: never rerun", o["R2"]["cells"]["cm-o0-L1-E1|10"]["excluded_trials"][0]["first_attempt_failed"], None)
print("R3 two pairs")
check("total / one pair", o["R3"]["two_pair_total_over_one_pair"]["cm-o3-L1-E0"]["ratio"], 1.1)
check("pair 2 alone", o["R3"]["per_pair_in_two_pair_trials"]["cm-o3-L1-E0|pair2-of-2"]["median"], 5000.0)
check("pair 1 alone", o["R3"]["per_pair_in_two_pair_trials"]["cm-o3-L1-E0|pair1-of-2"]["median"], 6000.0)
print("R4 CPU mode")
check("mode check catches b3 default", o["R4"]["mode_check_failed_files_left_out"], ["r4defaultb3.jsonl"])
check("O3 l3 default/bench", o["R4"]["C_default_over_C_bench"]["cm-o3-L0-E0|l3"]["ratio"], 1.2)
check("O0 l3 default/bench", o["R4"]["C_default_over_C_bench"]["cm-o0-L1-E1|l3"]["ratio"], 1.0)
check("default cells n = 4 (b3 left out)", o["R4"]["cells"]["default|cm-o0-L1-E1|l3"]["n"], 4)
check("OH bench O0 exact_8 = 2000/1000 - 1", o["R4"]["OH"]["bench|cm-o0-L1-E1|exact_8"]["value"], 1.0)
check("OH bench O3 exact_8 = 20000/16000 - 1", o["R4"]["OH"]["bench|cm-o3-L0-E0|exact_8"]["value"], 0.25)
check("RR bench exact_8 = (1000/2000) / (16000/20000)", o["R4"]["RR"]["bench|exact_8"]["value"], 0.625)
check("OH_O0 - OH_O3 bench exact_8 = 1 - 0.25", o["R4"]["OH_O0_minus_OH_O3"]["bench|exact_8"]["value"], 0.75)
check("RR default exact_8 (O3 x1.2 on both: unchanged)", o["R4"]["RR"]["default|exact_8"]["value"], 0.625)
check("markdown has all four sections", all(f"## R{i}" in md for i in (1, 2, 3, 4)), True)

print(f"{fails} failure(s)")
sys.exit(1 if fails else 0)
