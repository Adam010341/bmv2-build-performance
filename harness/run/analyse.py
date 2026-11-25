#!/usr/bin/env python3
"""analyse.py -- turn bench.py raw JSONL into per-cell results, gate reports and ratios (stdlib).

    analyse.py RAW.jsonl [RAW2.jsonl ...] [--caps-out CAPS.json] [--json OUT.json]

Per sat trial, the capacity C is `delivered_pps_steady`. A cell is (build, program, runtime,
frame, flows); its value is the median over instances, with a 95 % bootstrap interval
(10 000 resamples, seed 1). Ratios between two cells are bootstrapped by resampling each cell's
instances independently.

Gates (PREREG v1 §2) are evaluated here from the raw record, so a reader can recompute them:
  G2 counter conservation, G3 receiver drops, G4 foreign CPU, G7 path probe, G8 frequency spread,
  G6 binary unchanged (instance-end record).
"""
import json
import random
import statistics
import sys
from collections import defaultdict

HZ = 100                      # USER_HZ: /proc/<pid>/stat utime+stime unit
HARNESS_COMMS = {"bmgen", "bmrecv", "sudo", "cat", "python3", "bash", "nsenter", "taskset", "capsh",
                 "env", "iperf3", "bmbench-priv"}
B = 10000


def load(paths):
    recs = []
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
    return recs


def gates(r):
    """Failed gate ids for one trial record. bench.py evaluated G4, GF, G2, G3 at run time and wrote
    them to r["flags"]; G8 (frequency spread) is evaluated here from the samples."""
    bad = list(r.get("flags", []))
    # only the steady window bench.py measures capacity over (1.0 s .. duration - 0.5 s): before the
    # load starts and after it ends an idle CPU reports its idle frequency (AMENDMENT-6)
    dur = r.get("duration", 10)
    fs = [smp[2] for smp in r.get("samples", []) if len(smp) > 2 and smp[2] and 1.0 <= smp[0] <= dur - 0.5]
    for cpu in (fs[0].keys() if fs else []):
        v = [x[cpu] for x in fs if x.get(cpu)]
        if len(v) > 3:
            m = statistics.median(v)
            if max(abs(x - m) for x in v) > 0.03 * m:
                bad.append(f"G8:cpu{cpu}")
    return bad


# Flags that make a trial unusable for a capacity estimate (after the one rerun bench.py did).
# "generator-bounded" keeps the trial but turns the cell into a lower bound.
EXCLUDING = ("G4", "no-drain", "G2:gen-vs-h0tx", "G2:gen-vs-h2tx", "G7:probe", "G6:binary-changed")   # h2tx: R3 only


def _get(d, path):
    for k in path.split("."):
        d = d[k]
    return d


def boot_median(xs, rng):
    return [statistics.median(rng.choices(xs, k=len(xs))) for _ in range(B)]


def ci(samples):
    s = sorted(samples)
    return s[int(0.025 * len(s))], s[int(0.975 * len(s)) - 1]


def main():
    args = sys.argv[1:]
    caps_out = args[args.index("--caps-out") + 1] if "--caps-out" in args else None
    json_out = args[args.index("--json") + 1] if "--json" in args else None
    paths = [a for a in args if a.endswith(".jsonl")]
    recs = load(paths)
    rng = random.Random(1)

    inst_ok, inst_end = {}, {}
    for r in recs:
        if r.get("record") == "instance":
            inst_ok[r["instance"]] = r.get("probe_ok")
        if r.get("record") == "instance-end":
            inst_end[r["instance"]] = r.get("binary_unchanged")
    cells = defaultdict(list)
    report = {"gate_failures": [], "cells": {}}
    lower_bound = set()
    for r in recs:
        if r.get("record") != "trial" or r.get("kind") != "sat" or r.get("superseded"):
            continue
        fails = gates(r)
        if inst_ok.get(r["instance"]) is False:
            fails.append("G7:probe")
        if inst_end.get(r["instance"]) is False:
            fails.append("G6:binary-changed")
        key = (r["build"], r["program"], r.get("runtime", "none"), r["frame"], r.get("flows", 1))
        if fails:
            report["gate_failures"].append({"instance": r["instance"], "frame": r["frame"], "failed": fails})
        if any(f.startswith(EXCLUDING) for f in fails):
            continue
        if "generator-bounded" in fails:
            lower_bound.add(key)
        if r.get("delivered_pps_steady"):
            cells[key].append(r["delivered_pps_steady"])
    boots = {}
    print(f"{'build':22s} {'program':16s} {'runtime':9s} {'frame':>5s} {'flows':>5s} {'n':>2s} {'C median':>11s}  95% CI")
    for key in sorted(cells):
        xs = cells[key]
        bs = boot_median(xs, rng)
        boots[key] = bs
        lo, hi = ci(bs)
        med = statistics.median(xs)
        lb = key in lower_bound
        report["cells"]["|".join(map(str, key))] = {"n": len(xs), "median": med, "ci95": [lo, hi], "values": xs,
                                                    "lower_bound_generator": lb}
        print(f"{key[0]:22s} {key[1]:16s} {key[2]:9s} {key[3]:5d} {key[4]:5d} {len(xs):2d} {med:11.0f}  "
              f"[{lo:.0f}, {hi:.0f}]{'  >= (generator-bounded)' if lb else ''}")
    if report["gate_failures"]:
        print(f"\n{len(report['gate_failures'])} trial(s) excluded by gates:")
        for g in report["gate_failures"][:20]:
            print(f"  {g['instance']} f{g['frame']}: {', '.join(g['failed'])}")

    def ratio(a, b):
        if a not in boots or b not in boots:
            return None
        rs = [x / y for x, y in zip(boots[a], boots[b])]
        return statistics.median(cells[a]) / statistics.median(cells[b]), ci(rs)

    # E1 knob effects at fixed settings (as registered: ratios with bootstrap intervals)
    print("\nE1 knob ratios (C_left / C_right):")
    for prog in ("min", "l3"):
        for f in (64, 1518):
            for left, right in (("cm-o3-L0-E0", "cm-o0-L0-E0"), ("cm-o2-L0-E0", "cm-o3-L0-E0"),
                                ("cm-o3-L0-E0", "cm-o3-L1-E0"), ("cm-o0-L0-E0", "cm-o0-L1-E0"),
                                ("cm-o3-L0-E0", "cm-o3-L0-E1"), ("cm-o3-L0-E0", "cm-o0-L1-E1")):
                r = ratio((left, prog, "none", f, 1), (right, prog, "none", f, 1))
                if r:
                    print(f"  {prog:4s} {f:5d}  {left} / {right}: {r[0]:.3f}  [{r[1][0]:.3f}, {r[1][1]:.3f}]")
                    report.setdefault("ratios", {})[f"{prog}|{f}|{left}/{right}"] = {"ratio": r[0], "ci95": r[1]}
    # E1 factorial (AMENDMENT-1 A1.4): main effect of a knob = geometric mean over the other
    # knobs' levels of C(knob A)/C(knob B); interaction = ratio of ratios. Bootstrapped jointly.
    import math
    fact = {(o, l, e): f"cm-o{o}-L{l}-E{e}" for o in (0, 2, 3) for l in (1, 0) for e in (1, 0)}
    def gm_ratio(pairs, prog, f, draw):
        logs = []
        for a, b in pairs:
            ka, kb = (fact[a], prog, "none", f, 1), (fact[b], prog, "none", f, 1)
            if ka not in boots or kb not in boots:
                return None
            va = boots[ka][draw] if draw is not None else statistics.median(cells[ka])
            vb = boots[kb][draw] if draw is not None else statistics.median(cells[kb])
            logs.append(math.log(va / vb))
        return math.exp(sum(logs) / len(logs))
    effects = {
        "O3/O0": [((3, l, e), (0, l, e)) for l in (1, 0) for e in (1, 0)],
        "O2/O3": [((2, l, e), (3, l, e)) for l in (1, 0) for e in (1, 0)],
        "macros-off/on": [((o, 0, e), (o, 1, e)) for o in (0, 2, 3) for e in (1, 0)],
        "elogger-off/on": [((o, l, 0), (o, l, 1)) for o in (0, 2, 3) for l in (1, 0)],
        "macros-off/on @O0": [((0, 0, e), (0, 1, e)) for e in (1, 0)],
        "macros-off/on @O3": [((3, 0, e), (3, 1, e)) for e in (1, 0)],
    }
    print("\nE1 factorial effects (geometric-mean ratios of C, 95% bootstrap interval):")
    for prog in ("min", "l3"):
        for f in (64, 1518):
            for name, pairs in effects.items():
                pt = gm_ratio(pairs, prog, f, None)
                if pt is None:
                    continue
                draws = [gm_ratio(pairs, prog, f, i) for i in range(B)]
                lo, hi = ci(draws)
                print(f"  {prog:4s} {f:5d}  {name:20s} {pt:7.3f}  [{lo:.3f}, {hi:.3f}]")
                report.setdefault("effects", {})[f"{prog}|{f}|{name}"] = {"ratio": pt, "ci95": [lo, hi]}
            a = report.get("effects", {}).get(f"{prog}|{f}|macros-off/on @O0")
            b2 = report.get("effects", {}).get(f"{prog}|{f}|macros-off/on @O3")
            if a and b2:
                draws = [gm_ratio(effects["macros-off/on @O0"], prog, f, i) / gm_ratio(effects["macros-off/on @O3"], prog, f, i)
                         for i in range(B)]
                lo, hi = ci(draws)
                print(f"  {prog:4s} {f:5d}  {'E1.c macro cost O0 vs O3':20s} {a['ratio'] / b2['ratio']:7.3f}  [{lo:.3f}, {hi:.3f}]")
    if caps_out:
        caps = {f"{k[0]}|{k[1]}|{k[3]}": statistics.median(v) for k, v in cells.items()
                if k[2] == "none" and k[4] == 1}
        json.dump(caps, open(caps_out, "w"), indent=1)
        print(f"\nwrote {caps_out} ({len(caps)} cells)")
    if json_out:
        json.dump(report, open(json_out, "w"), indent=1)


if __name__ == "__main__":
    main()
