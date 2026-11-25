#!/usr/bin/env python3
"""Robustness report (PREREG §6 as AMENDMENT-11 fixes it): R1 overload, R2 trial length, R3 two port
pairs, R4 default CPU mode.

    report_robust.py RAW_DIR OUT_DIR

RAW_DIR is raw/robust as archived: preround/ (this run's own G1/G9 ceilings), r1.jsonl, r2.jsonl,
r3.jsonl (if R3 has run) and r4{bench,default}b{0..4}.jsonl. Writes OUT_DIR/robust.{json,md}.

Gates, exclusions, cells and the bootstrap are report.py's (CHOICES.md readings apply; seed
'<R>|<cell>'). No decision rule is registered for §6: every quantity is a ratio of cell medians
with its 95 % interval, paired by bootstrap draw as in report.py. The G8-removed sensitivity is
computed for every quantity. R4's CPU mode is checked against each plan run's run-start record
(governor, no_turbo), not taken from the file name alone.
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R  # noqa: E402
from analyse import load  # noqa: E402

CORNERS = [f"cm-o{o}-L{l}-E{e}" for o in (0, 3) for l in (1, 0) for e in (1, 0)]
DEFAULT_LIKE, PERF = "cm-o0-L1-E1", "cm-o3-L0-E0"


class RExp(R.Exp):
    """report.py's Exp; a trial is also told apart by duration (R2) and pair count (R3), which share an
    instance with a trial of the same kind and frame."""
    @staticmethod
    def tkey(r):
        return R.Exp.tkey(r) + (r.get("duration"), r.get("pairs", 1))


def cells(ex, keyfn, value=lambda r: r.get("delivered_pps_steady"), capacity=True):
    out = {}
    for k, c in sorted(ex.cells(("sat", "rate"), keyfn, value).items(), key=lambda kv: R.kstr(kv[0])):
        s = ex.summarise(k, c, frame=64, capacity=capacity)
        if s["median"] is not None:
            R.POINT[(ex.name, k)] = s["median"]
        out[k] = s
    return out


def rat(name, ka, kb):
    return R.ratio(ka, kb, name)


def r1(raw, pre):
    ex = RExp("R1", load([os.path.join(raw, "r1.jsonl")]), pre)
    key = lambda r: (r["build"], "sat") if r["kind"] == "sat" else (r["build"], "rate2C") \
        if r["kind"] == "rate" and r.get("pps_frac_of_sat") == 2.0 else None
    cl = cells(ex, key)
    q = {}
    for b in CORNERS:
        a, s = (b, "rate2C"), (b, "sat")
        if not R.have("R1", a, s):
            q[b] = None
            continue
        q[b] = {"delivered_at_2C_over_C": rat("R1", a, s),
                "abs_delivered_minus_C_over_C": R.boot_stat(lambda i: abs(R.draw("R1", a, i) / R.draw("R1", s, i) - 1))}
    return {"gates": ex.gate_summary(), "cells": R.jkeys(cl), "per_build": q}, cl


def r2(raw, pre):
    ex = RExp("R2", load([os.path.join(raw, "r2.jsonl")]), pre)
    cl = cells(ex, lambda r: (r["build"], r["duration"]) if r["kind"] == "sat" else None)
    q = {b: rat("R2", (b, 30), (b, 10)) for b in (DEFAULT_LIKE, PERF)}
    return {"gates": ex.gate_summary(), "cells": R.jkeys(cl), "C30_over_C10": q}, cl


def r3(raw, pre):
    path = os.path.join(raw, "r3.jsonl")
    if not os.path.exists(path):
        return {"not_run": True}, {}
    ex = RExp("R3", load([path]), pre)
    total = lambda r: r.get("delivered_pps_steady_total") if r.get("pairs") == 2 else r.get("delivered_pps_steady")
    cl = cells(ex, lambda r: (r["build"], r.get("pairs", 1)) if r["kind"] == "sat" else None, total)
    each = {}
    for i, part in ((1, lambda r: r.get("delivered_pps_steady")),
                    (2, lambda r: (r.get("pair2") or {}).get("delivered_pps_steady"))):
        ex_i = RExp(f"R3pair{i}", ex.recs, pre)
        each.update({(k[0], f"pair{i}-of-2"): v for k, v in
                     cells(ex_i, lambda r: (r["build"], 2) if r["kind"] == "sat" and r.get("pairs") == 2 else None,
                           part, capacity=False).items()})
    q = {b: rat("R3", (b, 2), (b, 1)) for b in CORNERS}
    return {"gates": ex.gate_summary(), "cells": R.jkeys(cl), "per_pair_in_two_pair_trials": R.jkeys(each),
            "two_pair_total_over_one_pair": q}, cl


def r4_mode_check(raw):
    """Each R4 plan run's run-start record against the mode its file name declares."""
    out, bad = {}, []
    for p in sorted(glob.glob(os.path.join(raw, "r4*b[0-9].jsonl"))):
        m = re.fullmatch(r"r4(bench|default)b(\d)\.jsonl", os.path.basename(p))
        if not m:
            continue
        starts = [r for r in load([p]) if r.get("record") == "run-start"]
        got = [(s.get("governor"), s.get("no_turbo")) for s in starts]
        ok = len(got) == 1 and (got[0] == ("performance", "1") if m.group(1) == "bench" else got[0][1] == "0")
        out[os.path.basename(p)] = {"declared": m.group(1), "run_start": got, "ok": ok}
        if not ok:
            bad.append(os.path.basename(p))
    return out, bad


def r4(raw, pre):
    files, bad = r4_mode_check(raw)
    recs = []
    for p in sorted(glob.glob(os.path.join(raw, "r4*b[0-9].jsonl"))):
        if os.path.basename(p) not in bad:
            recs += load([p])
    ex = RExp("R4", recs, pre)
    mode = lambda r: "bench" if r["instance"].startswith("r4-bench-") else "default" if r["instance"].startswith("r4-default-") else None
    cl = cells(ex, lambda r: (mode(r), r["build"], r["program"]) if r["kind"] == "sat" and mode(r) else None)
    cell_ids = sorted({(b, p) for _, b, p in cl})
    q = {f"{b}|{p}": rat("R4", ("default", b, p), ("bench", b, p)) for b, p in cell_ids}
    oh, rr, dif = {}, {}, {}
    for m in ("bench", "default"):
        K = lambda b, p: (m, b, p)
        for p in ("l3", "exact_8"):
            for b in (DEFAULT_LIKE, PERF):
                if R.have("R4", K(b, "min"), K(b, p)):
                    oh[f"{m}|{b}|{p}"] = R.boot_stat(lambda i: R.draw("R4", K(b, "min"), i) / R.draw("R4", K(b, p), i) - 1)
            if R.have("R4", *[K(b, x) for b in (DEFAULT_LIKE, PERF) for x in ("min", p)]):
                # E2's quantities (A1.4); OH ratios are not used (OH_O3 can be near 0)
                rel = lambda b, i: R.draw("R4", K(b, p), i) / R.draw("R4", K(b, "min"), i)
                rr[f"{m}|{p}"] = R.boot_stat(lambda i: rel(DEFAULT_LIKE, i) / rel(PERF, i))
                dif[f"{m}|{p}"] = R.boot_stat(lambda i: (1 / rel(DEFAULT_LIKE, i) - 1) - (1 / rel(PERF, i) - 1))
    return {"mode_check": files, "mode_check_failed_files_left_out": bad, "gates": ex.gate_summary(),
            "cells": R.jkeys(cl), "C_default_over_C_bench": q, "OH": oh, "RR": rr, "OH_O0_minus_OH_O3": dif}, cl


def run(raw, extra=()):
    R.Exp.EXTRA = extra
    R.Exp.BOOTS.clear()
    R.POINT.clear()
    pre = R.preround(raw)
    R.PRE.clear()
    R.PRE.update(pre)
    out, cl = {"experiment": "robustness (§6, AMENDMENT-11)", "preround": pre}, {}
    for name, fn in (("R1", r1), ("R2", r2), ("R3", r3), ("R4", r4)):
        out[name], cl[name] = fn(raw, pre)
    R.Exp.EXTRA = ()
    return out, cl


def strip(o):
    """The sensitivity copy keeps the reported quantities only."""
    keep = {"R1": "per_build", "R2": "C30_over_C10", "R3": "two_pair_total_over_one_pair", "R4": "C_default_over_C_bench"}
    return {k: {v: o[k].get(v)} for k, v in keep.items() if isinstance(o.get(k), dict)} | \
        {"R4_RR": o["R4"].get("RR"), "R4_OH_O0_minus_OH_O3": o["R4"].get("OH_O0_minus_OH_O3")}


def fr(x):
    return f"{R.f4(x['ratio'])} {R.fci(x['ci95'])}" if x else "—"


def fv(x):
    return f"{R.f4(x['value'])} {R.fci(x['ci95'])}" if x else "—"


def md(o, cl, sens):
    s = ["# Robustness checks (PREREG §6, AMENDMENT-11)", "",
         "No decision rule is registered; each quantity is a ratio of cell medians with its 95 % interval. "
         "The last column of each table repeats it with G8-failing trials removed.", ""]
    s += ["## R1 — overload: rate at 2 × C", "",
          R.gate_md(o["R1"]["gates"]), "", R.io_md(cl["R1"], R.PRE), "",
          R.md_table(["build", "delivered at 2C / C", "|delivered − C| / C", "G8 removed: delivered / C"],
                     [[b, fr((v or {}).get("delivered_at_2C_over_C")), fv((v or {}).get("abs_delivered_minus_C_over_C")),
                       fr(((sens["R1"]["per_build"] or {}).get(b) or {}).get("delivered_at_2C_over_C"))]
                      for b, v in o["R1"]["per_build"].items()]), "",
          R.md_table(["build", "trial"] + R.CELL_HDR, R.cell_rows(cl["R1"], lambda k: [k[0], k[1]])), ""]
    s += ["## R2 — trial length: 30 s against 10 s", "", R.gate_md(o["R2"]["gates"]), "",
          R.md_table(["build", "C(30 s) / C(10 s)", "G8 removed"],
                     [[b, fr(v), fr(sens["R2"]["C30_over_C10"].get(b))] for b, v in o["R2"]["C30_over_C10"].items()]), "",
          R.md_table(["build", "duration (s)"] + R.CELL_HDR, R.cell_rows(cl["R2"], lambda k: [k[0], k[1]])), ""]
    s += ["## R3 — two concurrent port pairs", ""]
    if o["R3"].get("not_run"):
        s += ["Not run yet.", ""]
    else:
        s += [R.gate_md(o["R3"]["gates"]), "",
              R.md_table(["build", "two-pair total / one-pair C", "G8 removed"],
                         [[b, fr(v), fr(sens["R3"]["two_pair_total_over_one_pair"].get(b))]
                          for b, v in o["R3"]["two_pair_total_over_one_pair"].items()]), "",
              R.md_table(["build", "pairs"] + R.CELL_HDR, R.cell_rows(cl["R3"], lambda k: [k[0], k[1]])), "",
              "Each pair's own steady rate in the two-pair trials (median, 95 % CI): "
              + "; ".join(f"{k} {R.f0(v['median'])} {R.fci(v['ci95'], R.f0)}" for k, v in o["R3"]["per_pair_in_two_pair_trials"].items()), ""]
    r4o = o["R4"]
    s += ["## R4 — default CPU mode (powersave, turbo on) against bench mode", "",
          "Mode check (run-start governor / no_turbo of every plan run): "
          + ("all as declared" if not r4o["mode_check_failed_files_left_out"]
             else f"FAILED, left out: {r4o['mode_check_failed_files_left_out']}"), "",
          R.gate_md(r4o["gates"]), "",
          R.md_table(["build|program", "C(default) / C(bench)", "G8 removed"],
                     [[k, fr(v), fr(sens["R4"]["C_default_over_C_bench"].get(k))] for k, v in r4o["C_default_over_C_bench"].items()]), "",
          R.md_table(["mode|build|program", "OH = C(min)/C(p) − 1"], [[k, fv(v)] for k, v in r4o["OH"].items()]), "",
          R.md_table(["mode|program", "RR = [C_O0(p)/C_O0(min)] / [C_O3(p)/C_O3(min)]", "G8 removed",
                      "OH_O0 − OH_O3", "G8 removed"],
                     [[k, fv(v), fv((sens["R4_RR"] or {}).get(k)), fv(r4o["OH_O0_minus_OH_O3"].get(k)),
                       fv((sens["R4_OH_O0_minus_OH_O3"] or {}).get(k))] for k, v in r4o["RR"].items()]), "",
          R.md_table(["mode", "build", "program"] + R.CELL_HDR, R.cell_rows(cl["R4"], lambda k: [k[0], k[1], k[2]])), ""]
    return "\n".join(s)


def main(argv):
    raw, out = argv
    os.makedirs(out, exist_ok=True)
    sens, _ = run(raw, extra=("G8",))
    sens = strip(sens)
    o, cl = run(raw)
    o["sensitivity_g8_removed"] = sens
    R.dump(os.path.join(out, "robust.json"), o)
    with open(os.path.join(out, "robust.md"), "w") as f:
        f.write(md(o, cl, sens))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: report_robust.py RAW_DIR OUT_DIR")
    main(sys.argv[1:])
