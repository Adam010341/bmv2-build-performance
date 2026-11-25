#!/usr/bin/env python3
"""PREREG AMENDMENT-14 (a), exploratory: G8 judged on loaded switch CPUs only ("G8-loaded").

    report_g8loaded.py CAMPAIGN_RAW E8_DIR OUT_DIR [X12_RAW]

E8 takes its G1/G9 ceilings from CAMPAIGN_RAW's pre-round, as report_e8.py does (AMENDMENT-10).

A trial fails G8-loaded iff some switch CPU c has cpu_busy_per_cpu[c] (raw, as recorded) >= 0.90
and carries the flag G8:cpu<c>; a trial with no switch CPU at >= 0.90 busy is judged on all its
CPUs, as registered. A second reading, added after the raw one was computed (PREREG AMENDMENT-14
addendum), uses AMENDMENT-6's normalised busy (busy x g4_window_s / duration) instead of the raw. Every verdict and ratio of E1-E5 (report.py), E8 (report_e8.py) and, if given,
X12 (report_x12.py) is recomputed three times: registered, G8-failing trials removed (AMENDMENT-6),
and G8-loaded-failing trials removed. Writes OUT_DIR/g8loaded.{json,md}. The registered outputs
are not touched; nothing here replaces them.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R  # noqa: E402

BUSY = 0.90
FLAG, FLAG_N = "G8L", "G8N"
_gates = R.gates


def busy_of(r, normalised=False):
    busy = dict(r.get("cpu_busy_per_cpu") or {})
    if normalised:
        scale = (r["g4_window_s"] / r["duration"]) if r.get("g4_window_s") and r.get("duration") else None
        busy = {c: (b * scale if scale else None) for c, b in busy.items()}
    return busy


def g8_loaded_fails(r, flags, normalised=False):
    busy = busy_of(r, normalised)
    loaded = [c for c in R.SW_CPUS if busy.get(c) is not None and busy[c] >= BUSY]
    if not loaded:
        return any(x.startswith("G8") for x in flags)
    return any(f"G8:cpu{c}" in flags for c in loaded)


def gates_with_g8l(r):
    flags = list(_gates(r))
    if g8_loaded_fails(r, flags):
        flags.append(FLAG)
    if g8_loaded_fails(r, flags, normalised=True):
        flags.append(FLAG_N)
    return flags


R.gates = gates_with_g8l     # Exp.fails() looks the name up in report's namespace at call time

MODES = (("registered", ()), ("G8 removed", ("G8",)), ("G8-loaded removed", (FLAG,)),
         ("G8-loaded (normalised busy) removed", (FLAG_N,)))


def flatten(x, path=""):
    """Every (path, value) of ratio/ci95/verdict leaves, so the three readings can be lined up."""
    out = {}
    if isinstance(x, dict):
        if "verdict" in x or "ratio" in x:
            out[path] = {k: x.get(k) for k in ("ratio", "ci95", "verdict") if k in x}
        for k, v in x.items():
            if isinstance(v, (dict, list)):
                out.update(flatten(v, f"{path}/{k}" if path else k))
    return out


def with_verdicts(o):
    out = flatten(o.get("verdicts", {}))
    for path, v in R.verdicts_of(o).items():
        out.setdefault(path, {"verdict": v})
    return out


def campaign(raw):
    pre = R.preround(raw)
    R.PRE.clear()
    R.PRE.update(pre)
    res = {}
    for mode, extra in MODES:
        R.Exp.EXTRA = extra
        try:
            res[mode] = {name: with_verdicts(v[0]) for name, v in R.run_all(raw, pre).items()}
        finally:
            R.Exp.EXTRA = ()
    return res


def e8(raw, e8dir):
    import report_e8 as E
    return {mode: {"E8": flatten(E.run(raw, e8dir, extra)[0]["ratio_docs_perf_over_default"])}
            for mode, extra in MODES}


def x12(raw):
    import report_x12 as X
    return {mode: {"X12": flatten({k: v for k, v in X.run(raw, extra)[0]["ratios"].items()})}
            for mode, extra in MODES}


def trial_counts(raw_files):
    """Counted trials failing G8 / G8-loaded, per experiment file (the size of each removal)."""
    out = {}
    for path in raw_files:
        n = g8 = g8l = g8n = 0
        for r in R.load(path):
            if r.get("record") != "trial" or r.get("superseded") or "samples" not in r:
                continue
            fl = gates_with_g8l(r)
            n += 1
            g8 += any(x.startswith("G8:") or x == "G8" for x in fl)
            g8l += FLAG in fl
            g8n += FLAG_N in fl
        out[os.path.basename(path)] = {"trials": n, "fail_G8": g8, "fail_G8_loaded": g8l,
                                       "fail_G8_loaded_normalised": g8n}
    return out


def fmt(v):
    if not v:
        return "no estimate"
    s = v.get("verdict", "")
    if v.get("ratio") is not None:
        lo, hi = (v.get("ci95") or [None, None])
        s += f" {v['ratio']:.4g}" + (f" [{lo:.4g}, {hi:.4g}]" if lo is not None else "")
    return s.strip()


def main(argv):
    raw, e8dir, out = argv[:3]
    x12raw = argv[3] if len(argv) > 3 else None
    os.makedirs(out, exist_ok=True)
    res = campaign(raw)
    for part in (e8(raw, e8dir), x12(x12raw) if x12raw else {}):
        for mode, d in part.items():
            res[mode].update(d)
    files = [os.path.join(raw, f) for f in sorted(os.listdir(raw)) if f.endswith(".jsonl")]
    files += [os.path.join(e8dir, "e8.jsonl")] + ([os.path.join(x12raw, "x12.jsonl")] if x12raw else [])
    counts = trial_counts([f for f in files if os.path.exists(f)])
    R.dump(os.path.join(out, "g8loaded.json"), {"rule": __doc__.split("\n\n")[1], "trial_counts": counts,
                                                 "readings": res})
    lines = ["# G8 on loaded cores (PREREG AMENDMENT-14 (a), exploratory)", "",
             "Not the registered verdicts. Each row: the registered reading, G8-failing trials removed "
             "(AMENDMENT-6), trials failing G8 on a >= 90 %-busy switch CPU removed (raw busy, registered), "
             "and the same with AMENDMENT-6's normalised busy (added after the raw reading was computed).", "",
             R.md_table(["file", "trials", "fail G8", "fail G8-loaded", "fail G8-loaded (normalised)"],
                        [[k, v["trials"], v["fail_G8"], v["fail_G8_loaded"], v["fail_G8_loaded_normalised"]]
                         for k, v in counts.items()]), ""]
    for exp in res["registered"]:
        rows = []
        for path, v in res["registered"][exp].items():
            alt = [res[m][exp].get(path) for m, _ in MODES[1:]]
            rows.append([path, fmt(v)] + [fmt(a) for a in alt])
        lines += [f"## {exp}", "", R.md_table(["quantity", *[m for m, _ in MODES]], rows), ""]
    with open(os.path.join(out, "g8loaded.md"), "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        sys.exit("usage: report_g8loaded.py CAMPAIGN_RAW E8_DIR OUT_DIR [X12_RAW]")
    main(sys.argv[1:])
