#!/usr/bin/env python3
"""AMENDMENT-12 report (exploratory, measured after the paper review): run-time log level `-L off`
against compiled-in logging macros, and the tutorials' own autotools -O0 -g build (`at-tutorial`).

    report_x12.py RAW_DIR OUT_DIR

RAW_DIR is raw/x12 as archived: preround/ (this run's own G1/G9 ceilings) and x12.jsonl. Writes
OUT_DIR/x12.{json,md}.

Gates, exclusions, cells and the bootstrap are report.py's (CHOICES.md readings apply). No decision
rule is registered: every quantity is a ratio of cell medians with its 95 % interval, paired by
bootstrap draw as in report.py, and repeated with G8-failing trials removed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R  # noqa: E402
from analyse import load  # noqa: E402
from report_robust import cells, fr  # noqa: E402

NAME = "X12"
DEFAULT_LIKE = "cm-o0-L1-E1"


def quantities():
    rat = lambda a, b: R.ratio(a, b, NAME)
    q = {}
    for o in (0, 3):
        l1, l0 = f"cm-o{o}-L1-E1", f"cm-o{o}-L0-E1"
        q[f"-O{o}: C(L1, -L off) / C(L1, none)"] = rat((l1, "logoff"), (l1, "none"))
        q[f"-O{o}: C(L1, -L off) / C(L0, none)"] = rat((l1, "logoff"), (l0, "none"))
    for rt in ("none", "tutorial"):
        q[f"C(at-tutorial, {rt}) / C({DEFAULT_LIKE}, {rt})"] = rat(("at-tutorial", rt), (DEFAULT_LIKE, rt))
    q["C(at-docs-perf, none) / C(at-tutorial, tutorial)"] = rat(("at-docs-perf", "none"), ("at-tutorial", "tutorial"))
    return q


def run(raw, extra=()):
    R.Exp.EXTRA = extra
    R.Exp.BOOTS.clear()
    R.POINT.clear()
    pre = R.preround(raw)
    R.PRE.clear()
    R.PRE.update(pre)
    ex = R.Exp(NAME, load([os.path.join(raw, "x12.jsonl")]), pre)
    cl = cells(ex, lambda r: (r["build"], r.get("runtime", "none")) if r["kind"] == "sat" else None)
    out = {"experiment": "X12 (AMENDMENT-12, exploratory, post-data)", "preround": pre,
           "gates": ex.gate_summary(), "cells": R.jkeys(cl), "ratios": quantities()}
    R.Exp.EXTRA = ()
    return out, cl


def md(o, cl, sens):
    return "\n".join([
        "# X12 — log level and the tutorials' autotools build (AMENDMENT-12, exploratory)", "",
        "Measured after the paper review; no decision rule is registered. Each quantity is a ratio of cell "
        "medians with its 95 % interval; the last column repeats it with G8-failing trials removed.", "",
        R.gate_md(o["gates"]), "", R.io_md(cl, R.PRE), "",
        R.md_table(["quantity", "ratio", "G8 removed"],
                   [[k, fr(v), fr(sens.get(k))] for k, v in o["ratios"].items()]), "",
        R.md_table(["build", "runtime"] + R.CELL_HDR, R.cell_rows(cl, lambda k: [k[0], k[1]])), ""])


def main(argv):
    raw, out = argv
    os.makedirs(out, exist_ok=True)
    sens = run(raw, extra=("G8",))[0]["ratios"]
    o, cl = run(raw)
    o["sensitivity_g8_removed"] = sens
    R.dump(os.path.join(out, "x12.json"), o)
    with open(os.path.join(out, "x12.md"), "w") as f:
        f.write(md(o, cl, sens))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: report_x12.py RAW_DIR OUT_DIR")
    main(sys.argv[1:])
