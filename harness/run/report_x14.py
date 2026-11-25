#!/usr/bin/env python3
"""AMENDMENT-14 (c) report (exploratory, measured after the paper review): one iperf3 TCP stream
through `l3` on the upstream default, the documented build, and the tutorials' build with the
tutorials' recording options.

    report_x14.py PRE_RAW X14_JSONL OUT_DIR

PRE_RAW is the directory holding X12's pre-round (raw/x12, whose preround/ gives the amendment's ceilings). Writes
OUT_DIR/x14.{json,md}. Gates, exclusions and the bootstrap are report.py's; the cell value is the
receiver's goodput (iperf3 sum_received bits/s). No decision rule is registered: medians with 95 %
intervals, retransmits, and the ratio of the documented build to each other cell, repeated with
G8-failing trials removed.
"""
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R  # noqa: E402
from analyse import load  # noqa: E402

NAME = "X14"
CELLS = [("cm-default", "none"), ("at-docs-perf", "none"), ("at-tutorial", "tutorial")]
DOCS = ("at-docs-perf", "none")


def key(r):
    return (r["build"], r.get("runtime", "none")) if r.get("kind") == "iperf3-tcp" else None


def run(pre_dir, x14, extra=()):
    R.Exp.EXTRA = extra
    R.Exp.BOOTS.clear()
    R.POINT.clear()
    pre = R.preround(pre_dir)
    R.PRE.clear()
    R.PRE.update(pre)
    recs = load([x14])
    ex = R.Exp(NAME, recs, pre)
    cells = {}
    for k, c in sorted(ex.cells(("iperf3-tcp",), key, value=lambda r: r.get("iperf3_received_bps")).items(),
                       key=lambda kv: R.kstr(kv[0])):
        s = ex.summarise(k, c, capacity=False)
        if s["median"] is not None:
            R.POINT[(NAME, k)] = s["median"]
        cells[k] = s
    retx = {}
    for r, fl in ex.counted(("iperf3-tcp",)):
        if not ex.excluded(fl) and r.get("retransmits") is not None:
            retx.setdefault(key(r), []).append(r["retransmits"])
    ratios = {f"{DOCS[0]} / {b} ({rt})": R.ratio(DOCS, (b, rt), NAME) for b, rt in CELLS if (b, rt) != DOCS}
    R.Exp.EXTRA = ()
    return {"experiment": "X14 (AMENDMENT-14 (c), exploratory, post-data)", "gates": ex.gate_summary(),
            "cells_goodput_bps": R.jkeys(cells),
            "retransmits_median": {R.kstr(k): statistics.median(v) for k, v in sorted(retx.items(), key=lambda kv: R.kstr(kv[0]))},
            "ratios": ratios}, cells


def fr(v):
    return "no estimate" if not v else f"{v['ratio']:.3g} [{v['ci95'][0]:.3g}, {v['ci95'][1]:.3g}]"


def main(argv):
    pre_dir, x14, out = argv
    os.makedirs(out, exist_ok=True)
    sens = run(pre_dir, x14, extra=("G8",))[0]["ratios"]
    o, cells = run(pre_dir, x14)
    o["sensitivity_g8_removed"] = sens
    R.dump(os.path.join(out, "x14.json"), o)
    rows = [[b, rt, s["n"], f"{s['median'] / 1e6:.2f}" if s["median"] else "—",
             f"[{s['ci95'][0] / 1e6:.2f}, {s['ci95'][1] / 1e6:.2f}]" if s["ci95"] else "—",
             o["retransmits_median"].get(R.kstr((b, rt)), "—"), dict(s["gate_failures"]) or "—"]
            for (b, rt), s in cells.items()]
    with open(os.path.join(out, "x14.md"), "w") as f:
        f.write("\n".join([
            "# X14 — one iperf3 TCP stream through l3 (AMENDMENT-14 (c), exploratory)", "",
            "Measured after the paper review; no decision rule is registered. Goodput is iperf3's receiver-side "
            "bit rate; ratios are of cell medians with 95 % bootstrap intervals.", "",
            R.gate_md(o["gates"]), "",
            R.md_table(["build", "runtime", "n", "goodput median (Mbit/s)", "95% CI", "retransmits (median)",
                        "gate failures"], rows), "",
            R.md_table(["ratio", "estimate", "G8 removed"],
                       [[k, fr(v), fr(sens.get(k))] for k, v in o["ratios"].items()]), ""]))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: report_x14.py PRE_RAW X14_JSONL OUT_DIR")
    main(sys.argv[1:])
