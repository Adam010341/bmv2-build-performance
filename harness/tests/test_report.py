#!/usr/bin/env python3
"""Synthetic-record test for report.py: every registered decision rule must give the verdict the
constructed data imply, and the shared rules (exclusions, superseded attempts, G1, G9, GF,
AMENDMENT-5's file rule) must act on exactly the records they should. No real data is read.

    test_report.py            (REPORT_PY=/path/to/report.py to run the same checks on a mutant)
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.environ.get("REPORT_PY") or os.path.join(HERE, "..", "run", "report.py")
O0, O3 = "cm-o0-L1-E1", "cm-o3-L0-E0"
JIT = (0.998, 0.999, 1.0, 1.001, 1.002)          # per-instance multipliers: tight, symmetric


def trial(inst, build, prog, C, frame=64, runtime="none", flows=1, flags=(), kind="sat", **kw):
    return {"record": "trial", "kind": kind, "instance": inst, "build": build, "program": prog, "runtime": runtime,
            "frame": frame, "flows": flows, "duration": 10, "delivered_pps_steady": C, "flags": list(flags),
            "samples": [], **kw}


def cell(exp, build, prog, C, frame=64, runtime="none", flows=1):
    return [trial(f"{exp}-{build}-{prog}-{runtime}-f{frame}-n{flows}-r{i}", build, prog, C * j, frame, runtime, flows)
            for i, j in enumerate(JIT)]


# ---------------------------------------------------------------- E1: C = 10000 * Of / cost(O, L)
OF = {0: 1.0, 2: 4.0, 3: 4.0}
COST = {0: 6.0, 2: 3.0, 3: 3.0}                 # macro cost: 6x at O0, 3x at O3 -> E1.c RoR = 2 ...


def c_e1(o, l, e, p, f):
    cost = 1.5 if (o, p, f) == (0, "l3", 64) else COST[o]     # ... except l3/64: RoR 1.5/3 = 0.5 (that part fails)
    c = 10000 * OF[o] / (cost if l else 1)
    return c * 0.8 if (o, l, e, p, f) == (2, 1, 0, "min", 1518) else c   # O2 != O3 at one setting only: E1.b fails


e1 = []
for o in (0, 2, 3):
    for l in (1, 0):
        for e in (1, 0):
            for p in ("min", "l3"):
                for f in (64, 1518):
                    e1 += cell("e1", f"cm-o{o}-L{l}-E{e}", p, c_e1(o, l, e, p, f), f)
for b, src in (("cm-default", (0, 1)), ("cm-default-dbg", (0, 1)), ("at-docs-perf", (3, 0)), ("at-default", (2, 1)),
               ("docker:img", (0, 1))):
    for f in (64, 1518):
        e1 += cell("e1x", b, "l3", c_e1(src[0], src[1], 0, "l3", f), f)
e1 += cell("e1x", "x-io", "l3", 600000)           # > 0.5 x G9 ceiling (1e6): possibly I/O-bound
e1 += cell("e1x", "x-gen", "l3", 1200000)         # > G1 ceiling (3e6) / 3: G1 fails, lower bound
e1 += cell("e1x", "x-gf", "l3", 5000)
e1[-1]["flags"] = ["generator-bounded"]           # GF: kept, cell becomes a lower bound
e1.append(trial("e1-drain", O3, "l3", 1.0, flags=["no-drain"]))                    # excluded, counted
e1.append(trial("e1-sup", O3, "l3", 1.0, flags=["G4"], superseded=True, attempt=1))  # dropped
e1.append(trial("e1-g8kept", "x-gf", "l3", 5000, flags=["G3:recv-drops"],          # kept
                cpu_busy_per_cpu={"4": 0.8}, g4_window_s=13.0))   # 0.8 busy over 13 s = 1.04 of the 10 s generator window

# ---------------------------------------------------------------- E1-RT
STEADY = [[0.5 * k, None, {"4": 2500000}] for k in range(1, 20)]
DIP = [x if x[0] != 5.0 else [5.0, None, {"4": 2300000}] for x in STEADY]
e1rt = cell("e1rt", O0, "l3", 2000, runtime="tutorial")          # / E1's O3 none (40000) = 0.05
for r in e1rt:
    r["samples"] = DIP                                            # every tutorial trial fails G8 (kept)
e1rt += cell("e1rt", O0, "l3", 1700, runtime="none")
e1rt.append(trial("e1rt-noval", O0, "l3", None, runtime="none"))  # no steady value: counted, not silently lost
e1rt += cell("e1rt", "cm-o3-L1-E1", "l3", 9000, runtime="none")
e1rt[-1]["flags"] = ["G4"]                                        # G4 again after its rerun: §4 failed cell
e1rt.append({**e1rt[-1], "superseded": True, "attempt": 1})
e1rt.append({"record": "error", "instance": "e1rt-docker-grpc_img-none-r0", "build": "docker-grpc:img",
             "program": "l3", "runtime": "none", "error": "rc 127"})
e1rt.append(trial("e1rt-bogus", "docker-grpc:img", "l3", 1.0))   # AMENDMENT-5: must be ignored
e1rt_grpc = cell("e1rtg", "docker-grpc:img", "l3", 3000)

# ---------------------------------------------------------------- E2: 1/C = t_min + K * t_table
# l3: RR = (100/128.89)/(5/5.8) = 0.900 (interval straddles 0.9: inconclusive); l3_reg: OH 3.0 vs 2.9 (difference
# straddles 0.10: inconclusive); O3 tables cost 0.2 µs extra at K >= 1, so min is off the line (t_b with min = 0.515)
T = {O0: {"min": 100, "l3": 128.89, "l3_reg": 400}, O3: {"min": 5, "l3": 5.8, "l3_reg": 19.5}}
TT, OFF = {O0: 30, O3: 0.5}, {O0: 0.0, O3: 0.2}
e2 = []
for b in (O0, O3):
    for kind, Ks in (("exact", (1, 2, 4, 8)), ("lpm", (1, 4, 8)), ("ternary", (1, 4, 8))):
        for k in Ks:
            T[b][f"{kind}_{k}"] = T[b]["min"] + OFF[b] + k * TT[b]
    for p, t in T[b].items():
        e2 += cell("e2", b, p, 1e6 / t)

# ---------------------------------------------------------------- E3: 1/C(s) = a + b s
E3AB = {O0: (100.0, 0.0), O3: (4.0, 0.004)}       # µs, µs/byte -> O3: C(1518)/C(64) = 0.4226
e3 = []
for b, (a, sl) in E3AB.items():
    for i, j in enumerate(JIT):
        for f in (64, 128, 256, 512, 1024, 1280, 1518):
            e3.append(trial(f"e3-{b}-r{i}", b, "l3", j * 1e6 / (a + sl * f), f))


# ---------------------------------------------------------------- E4 (C from E1: O3 l3 = 40000)
def zl(inst, build, frame, burst, z, lo=800, hi=52000, hi_fails=True):
    lo_ok = z is not None or not hi_fails
    return {"record": "trial", "kind": "zl-result", "instance": inst, "build": build, "program": "l3",
            "runtime": "none", "frame": frame, "burst": burst, "threshold": 0.0, "zero_loss_pps": z,
            "bracket": {"lo_passes": lo_ok, "hi_fails": hi_fails},
            "steps": [{"pps": lo, "loss": 0.0 if lo_ok else 0.7, "ok": lo_ok, "role": "lo", "flags": []},
                      {"pps": hi, "loss": 0.3 if hi_fails else 0.0, "ok": not hi_fails, "role": "hi", "flags": []}]}


e4 = []
for i, j in enumerate(JIT):
    inst = f"e4-{O3}-r{i}"
    e4 += [zl(inst, O3, 64, 1, 38000 * j), zl(inst, O3, 1518, 1, 20000 * j),     # Z_1/C 0.95 holds, 0.5 fails
           zl(inst, O3, 64, 128, None), zl(inst, O3, 1518, 128, 20000 * j),      # failed bracket (lo lost); 0.5 holds
           zl(f"e4-{O0}-r{i}", O0, 64, 128, None, hi=8667, hi_fails=False)]       # failed bracket the other way: hi lost none
    e4.append({"record": "trial", "kind": "zl-step", "instance": inst, "build": O3, "program": "l3", "runtime": "none",
               "frame": 64, "burst": 1, "offered_pps": 30000, "flows": 1, "duration": 10, "loss": 0.0,
               "flags": ["schedule"], "samples": [], "attempt": 2})   # GF still failing after its rerun
    for x, loss in zip((0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0, 1.2, 1.5), (0, 0, 0, 0.4, 3, 10, 20, 30, 40)):
        e4.append({"record": "trial", "kind": "iperf3-rung", "instance": inst, "build": O3, "program": "l3",
                   "runtime": "none", "frame": 64, "flows": 1, "offered_pps": round(x * 40000),
                   "iperf3_aggregate_loss_pct": loss, "iperf3_worst_flow_loss_pct": loss,
                   "iperf3_per_flow": [{"received_bps": 1.0, "lost_percent": loss}], "h0_tx": 1000,
                   "delivered": 1000, "drained": True})      # 0.4 % is clean: highest clean rung 0.5 C


# ---------------------------------------------------------------- E5
def rate(inst, build, prog, acc, lost, frac=0.85, burst=1, flags=()):
    n = len(acc)
    rx = [a - x for a, x in zip(acc, lost)]
    return {"record": "trial", "kind": "rate", "instance": inst, "build": build, "program": prog, "runtime": "none",
            "frame": 64, "flows": n, "burst": burst, "pps_frac_of_sat": frac, "offered_pps": 1000,
            "loss": 1 - sum(rx) / sum(acc), "gen": {"accepted": sum(acc), "per_flow_accepted": acc},
            "recv": {"flows": [{"flow": i, "rx": r} for i, r in enumerate(rx)], "ring_drops": 0},
            "flags": list(flags), "samples": []}


e5 = []
for b, ratios in ((O3, {"l3": 1.0, "flow_exact_256": 1.0, "flow_ternary_256": 0.5}),
                  (O0, {"l3": 0.7, "flow_exact_256": 1.0, "flow_ternary_256": 1.0})):
    for p, r256 in ratios.items():
        e5 += cell("e5", b, p, 100000, flows=1)
        e5 += cell("e5", b, p, 100000 * r256, flows=256)
A16 = [10000] * 16
e5.append(rate("e5-o3-l3-n16-r0", O3, "l3", A16, [100] * 14 + [118, 82], flags=["schedule"]))  # within the null; GF-flagged
e5.append(rate("e5-o3-fe-n16-r0", O3, "flow_exact_256", A16, [0] * 16))          # no loss at all: E5.c not tested
e5.append(rate("e5-o3-l3-n16-r0", O3, "l3", A16, [0] * 16, frac=0.5))            # clean rung 0.5
e5.append(rate("e5-o3-l3-n16-r1", O3, "l3", A16, [1600] + [0] * 15, flags=["G3:recv-drops"]))  # void (G3)
e5.append(rate("e5-o3-l3-n1-r0", O3, "l3", [100000], [500]))                     # n = 1: trivially within
e5.append(rate("e5-o0-l3-n16-r0", O0, "l3", A16, [1600] + [0] * 15))             # concentrated: above
e5.append(rate("e5-o0-fe-n16-r0", O0, "flow_exact_256", A16, [100] * 16))        # perfectly even: below
e5.append(rate("e5-float-n40-r0", "cm-float", "l3", [3] * 40, [2, 1, 1, 1, 1] + [0] * 35))  # obs = 2/3 = null's q97.5
# unequal per-flow counts (round-robin leaves them 1 apart): the 3-frame flow lost 2 (2/3), the null's q97.5 is a
# 4-frame flow losing 2 (0.5): same frame count, larger fraction -> inside only by the tie-aware rule
e5.append(rate("e5-tie-n40-r0", "cm-tie", "l3", [3] + [4] * 39, [2, 1, 1, 1, 1] + [0] * 35))

pre_g1 = [{"record": "calibration", "frame": f, "burst": 32, "rep": 0, "gen": {"accepted_pps": 3e6}}
          for f in (64, 128, 256, 512, 1024, 1280, 1518)]
pre_g9 = [{"record": "null-forwarder", "kind": "sat", "frame": f, "rep": 0, "delivered_pps_steady": 1e6, "flags": []}
          for f in (64, 128, 256, 512, 1024, 1280, 1518)]

# a second data set: E5 only, a build where E5.a holds for both programs and no rate trial lost a frame
e5_untested = []
for p in ("l3", "flow_exact_256", "flow_ternary_256"):
    e5_untested += cell("e5u", O3, p, 100000, flows=1) + cell("e5u", O3, p, 100000, flows=256)
e5_untested += [rate("e5u-l3-n16-r0", O3, "l3", A16, [0] * 16), rate("e5u-l3-n1-r0", O3, "l3", [100000], [0])]


def run(files):
    d = tempfile.mkdtemp()
    raw, out = os.path.join(d, "raw"), os.path.join(d, "out")
    os.makedirs(os.path.join(raw, "preround"))
    for name, rows in list(files.items()) + [("preround/g1-calibration", pre_g1), ("preround/g9-null", pre_g9)]:
        with open(os.path.join(raw, name + ".jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
    p = subprocess.run([sys.executable, "-I", REPORT, raw, out], capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-2000:])
        print("FAILED (report.py crashed)")
        sys.exit(1)
    return {x: json.load(open(os.path.join(out, f"{x}.json"))) for x in ("e1", "e1rt", "e2", "e3", "e4", "e5")}


R = run({"e1": e1, "e1rt": e1rt, "e1rt-grpc": e1rt_grpc, "e2": e2, "e3": e3, "e4": e4, "e5": e5})
R2 = run({"e5": e5_untested})
ok = True


def check(name, got, want):
    global ok
    good = got == want
    print(("  ok   " if good else "  FAIL ") + f"{name}: {got}" + ("" if good else f" (want {want})"))
    ok &= good


E1, V1 = R["e1"]["cells"], R["e1"]["verdicts"]
k = f"{O3}|l3|none|64|1"
print("shared rules")
check("superseded attempt dropped, no-drain trial excluded: n", E1[k]["n"], 5)
check("excluded trial is counted, not dropped silently", [x["instance"] for x in E1[k]["excluded_trials"]], ["e1-drain"])
check("median unaffected by excluded/superseded trials", E1[k]["median"], 40000.0)
check("G9: C > 0.5 x ceiling flagged possibly I/O-bound", (E1["x-io|l3|none|64|1"]["G9_possibly_io_bound"],
                                                          round(E1["x-io|l3|none|64|1"]["G9_ratio"], 3)), (True, 0.6))
check("G9: C below 0.5 x ceiling not flagged", E1[k]["G9_possibly_io_bound"], False)
check("G1: C > ceiling/3 -> lower bound", (E1["x-gen|l3|none|64|1"]["G1_ok"], E1["x-gen|l3|none|64|1"]["lower_bound"]), (False, True))
check("GF: generator-bounded trial kept, cell is a lower bound",
      (E1["x-gf|l3|none|64|1"]["n"], E1["x-gf|l3|none|64|1"]["GF_generator_bounded"], E1["x-gf|l3|none|64|1"]["lower_bound"]), (6, True, True))
print("E1")
check("E1.a ratio", round(V1["E1.a"]["ratio"], 3), 4.0)
check("E1.a verdict (> 2)", V1["E1.a"]["verdict"], "holds")
check("E1.b part at the perturbed setting", V1["E1.b"]["parts"].get("L1E0|min|1518", {}).get("verdict"), "fails")
check("E1.b combined over every setting", V1["E1.b"]["verdict"], "fails")
check("E1.c ratio of ratios (6x / 3x)", round(V1["E1.c"]["parts"]["min|64"]["value"], 3), 2.0)
check("E1.c part l3|64 (1.5x / 3x)", V1["E1.c"]["parts"]["l3|64"]["verdict"], "fails")
check("E1.c registered combination (every part must hold)", V1["E1.c"]["verdict"], "fails")
alt = V1["E1.c"]["alternative_readings"]
pooled = [v for k, v in alt.items() if k.startswith("pooled")][0]
check("E1.c alternative: pooled ratio of ratios = (2*2*2*0.5)^(1/4)", (round(pooled["value"], 4), pooled["verdict"]), (1.4142, "holds"))
check("E1.c alternative: A1.4 three-way over mixed parts", [v for k, v in alt.items() if k.startswith("A1.4")][0]["verdict"][:21],
      "inconclusive (mixed p")
g8 = R["e1"]["gates"]["g8_diagnostic"]
check("G8 busy split raw vs normalised to the generator window", (g8["cpu_ge_90pct_busy"], g8["cpu_ge_90pct_busy_normalised"]),
      ({"fail": 0, "of": 0}, {"fail": 0, "of": 1}))
check("E1.d verdict", V1["E1.d"]["verdict"], "holds")
print("E1-RT")
check("E1-RT.a ratio (tutorial O0 / E1 O3 none)", round(R["e1rt"]["verdicts"]["E1-RT.a"]["ratio"], 4), 0.05)
check("E1-RT.a verdict (< 0.1)", R["e1rt"]["verdicts"]["E1-RT.a"]["verdict"], "holds")
hf = R["e1rt"]["exploratory"].get(f"E1 at-docs-perf none / {O0} tutorial (headline factor)") or {}
check("headline factor: E1 at-docs-perf none / E1-RT tutorial O0 (40000 / 2000)", round(hf.get("ratio") or 0, 4), 20.0)
g = R["e1rt"]["cells"]["docker-grpc:img|l3|none|64|1"]
check("AMENDMENT-5: grpc build only from e1rt-grpc.jsonl", (g["n"], g["median"]), (5, 3000.0))
check("AMENDMENT-5: grpc records in e1rt.jsonl counted as ignored", R["e1rt"]["amendment5_ignored_by_record"], {"error": 1, "trial": 1})
c = R["e1rt"]["cells"][f"{O0}|l3|none|64|1"]
check("a trial without a value is listed, not silently lost", (c["n"], c["no_value_trials"]), (5, ["e1rt-noval"]))
c = R["e1rt"]["cells"]["cm-o3-L1-E1|l3|none|64|1"]
check("§4: G4 on both attempts -> failed cell, estimate secondary",
      (c["n"], [x["first_attempt_failed"] for x in c["failed_cell_s4"]]), (4, [["G4"]]))
check("AMENDMENT-6 sensitivity: E1-RT.a depends on G8-failing trials",
      R["e1rt"]["sensitivity"]["g8_clean"]["changed"].get("E1-RT.a"), ["holds", "no estimate"])
check("... and not on schedule-flagged trials", R["e1rt"]["sensitivity"]["schedule_as_failed"]["changed"], {})
print("E2")
V2 = R["e2"]["verdicts"]
check("E2 RR lpm_1 (0.846 < 0.9) build-dependent", V2["build-dependence (RR, A1.4)"]["lpm_1"]["verdict"], "build-dependent")
check("E2 RR l3_reg (0.975) not build-dependent", V2["build-dependence (RR, A1.4)"]["l3_reg"]["verdict"], "not build-dependent")
check("E2 RR l3 (0.900, interval straddles 0.9) inconclusive", V2["build-dependence (RR, A1.4)"]["l3"]["verdict"], "inconclusive")
check("E2 divergence exact_8 (2.4 vs 0.84)", V2["claim-level divergence"]["exact_8"]["verdict"], "claim-level divergence")
check("E2 divergence l3_reg (3.0 vs 2.9, interval straddles 0.10) inconclusive",
      V2["claim-level divergence"]["l3_reg"]["verdict"], "inconclusive")
check("E2 per-table time O3 exact with min as K = 0", round(R["e2"]["per_table_time_with_min_as_K0"][f"{O3}|exact"]["value"], 3), 0.515)
check("E2 ranking flips", sorted(x["pair"] for x in V2["ranking flips"]["flips"]),
      ["l3 vs exact_1", "l3 vs lpm_1", "l3 vs ternary_1"])
check("E2 per-table time O0 exact (µs)", round(R["e2"]["per_table_time"][f"{O0}|exact"]["value"], 1), 30.0)
check("E2 per-table time O3 lpm, K >= 1 only (µs)", round(R["e2"]["per_table_time"][f"{O3}|lpm"]["value"], 2), 0.5)
print("E3")
F = R["e3"]["fit"][O3]
check("E3 fit a (µs), b (ns/byte)", (round(F["a_us"], 2), round(F["b_ns_per_byte"], 2)), (4.0, 4.0))
check("E3 byte share at 1518", round(F["byte_share_1518"], 3), 0.603)
check("E3.a O0 (ratio 1)", R["e3"]["verdicts"]["E3.a"]["parts"][O0]["verdict"], "holds")
check("E3.a O3 (ratio 0.42)", R["e3"]["verdicts"]["E3.a"]["parts"][O3]["verdict"], "fails")
check("E3.a combined", R["e3"]["verdicts"]["E3.a"]["verdict"], "fails")
print("E4")
V4 = R["e4"]["verdicts"]
check("E4.a O3 64 (Z_1/C 0.95)", V4["E4.a"]["parts"][f"{O3}|64"]["verdict"], "holds")
check("E4.a O3 1518 (Z_1/C 0.5)", V4["E4.a"]["parts"][f"{O3}|1518"]["verdict"], "fails")
check("E4.a combined", V4["E4.a"]["verdict"], "fails")
check("E4.b failed bracket is no estimate", V4["E4.b"]["parts"][f"{O3}|64"]["verdict"], "no estimate (failed bracket 5/5)")
x = V4["E4.b"]["parts"][f"{O3}|64"]
check("E4.b lo lost frames -> 'Z < lo' and the part would hold", (x["bound_if_loss_monotone"], x["verdict_if_loss_monotone"]),
      ("Z < lo = 0.0200 C", "holds"))
x = V4["E4.b"]["parts"][f"{O0}|64"]
check("E4.b hi lost none -> 'Z >= hi', never 'Z < lo'", (x["bound_if_loss_monotone"][:7], x["verdict_if_loss_monotone"]),
      ("Z >= hi", None))
x = V4["E4.a"]["parts"][f"{O3}|64"]
check("E4.a carries the schedule counts", (x["schedule_flagged_steps"], x["searches_with_a_schedule_flagged_step"]), (5, "5/5"))
check("§4 sensitivity: E4.a O3|64 without schedule-flagged searches",
      R["e4"]["sensitivity"]["schedule_as_failed"]["changed"].get(f"E4.a/parts/{O3}|64"), ["holds", "no estimate"])
check("E4.b O3 1518 (Z_128/C 0.5)", V4["E4.b"]["parts"][f"{O3}|1518"]["verdict"], "holds")
check("E4.c highest clean rung / C (0.4 % counts as clean)", round(V4["E4.c"]["parts"][f"{O3}|64"]["ratio"], 4), 0.5)
check("E4.c verdict (< 0.8)", V4["E4.c"]["verdict"], "holds")
print("E5")
V5 = R["e5"]["verdicts"]
check("E5.a O3 l3 / flow_exact", (V5["E5.a"]["parts"][f"{O3}|l3"]["verdict"], V5["E5.a"]["parts"][f"{O3}|flow_exact_256"]["verdict"]),
      ("holds", "holds"))
check("E5.a O0 l3 (0.7)", V5["E5.a"]["parts"][f"{O0}|l3"]["verdict"], "fails")
check("E5.b O3 ternary (0.5) / O0 ternary (1.0)", (V5["E5.b"]["parts"][f"{O3}|flow_ternary_256"]["verdict"],
                                                  V5["E5.b"]["parts"][f"{O0}|flow_ternary_256"]["verdict"]), ("holds", "fails"))
C5 = V5["E5.c"]["cells"]
check("E5.c binomial-like spread within; G3-void trial ignored", (C5[f"{O3}|l3|16"]["verdict"], C5[f"{O3}|l3|16"]["lossy_trials"]), ("holds", 1))
check("E5.c schedule-flagged lossy trials counted", C5[f"{O3}|l3|16"]["lossy_trials_with_schedule_flag"], 1)
check("E5.c no lossy trial -> not tested, never holds", C5[f"{O3}|flow_exact_256|16"]["verdict"], "no estimate (no lossy trial: not tested)")
check("E5.c n = 1 cannot fail", C5[f"{O3}|l3|1"]["verdict"][:11], "cannot fail")
t = R["e5"]["e5c_trials"]["cm-tie|l3|40"][0]
check("E5.c unequal per-flow counts: tie in frames is inside", (C5["cm-tie|l3|40"]["verdict"], t["within_by_fraction_only"],
                                                                t["worst_flow_lost_frames"], t["null_central95_frames"][1]),
      ("holds", False, 2, 2))
check("E5.c loss on one flow (above the null)", C5[f"{O0}|l3|16"]["verdict"], "fails")
check("E5.c perfectly even loss (below the null)", C5[f"{O0}|flow_exact_256|16"]["verdict"], "fails")
check("E5.c observation on the null's edge (2/3) is within", C5["cm-float|l3|40"]["verdict"], "holds")
check("E5.c per build", V5["E5.c"]["builds"], {O0: "fails", O3: "holds"})
rd = V5["E5 reading (A1.4)"]
check("E5 reading O3 (a holds, c holds)", rd[O3], ["a per-flow decline is a property of the criterion and the instrument, not of bmv2"])
check("E5 reading O0 (a fails for l3, holds for flow_exact)", [x.split(" (")[0].split(":")[0] for x in rd[O0]],
      ["the decline is a bmv2 property", "E5.a holds for one program and not the other, or is inconclusive"])
check("E5 v1 criterion: largest rung with worst flow <= 0.5 %", R["e5"]["v1_criterion"][f"{O3}|l3|16|B1"]["perflow"]["median_frac"], 0.5)
V5u = R2["e5"]["verdicts"]
check("E5.c build with no lossy trial: not tested", V5u["E5.c"]["builds"][O3][:11], "no estimate")
check("E5 reading when E5.c is not tested", V5u["E5 reading (A1.4)"][O3], ["no registered reading: E5.c not tested"])
print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
