#!/usr/bin/env python3
"""AMENDMENT-14 (d) report (exploratory): the profile of one sat trial per build, from X15's raw dir.

    report_x15.py RAW_X15 OUT_DIR

Re-parses each <build>.report.txt with x15.parse_sections (the run-time summary in x15.jsonl added
the cpu_core and cpu_atom sections together; see x15.parse_sections), and writes OUT_DIR/x15.{json,md}:
per build the trial's C, the share of samples matching x15.LOG_PATTERN in the primary (cpu_core)
section and in the other, and the 15 largest symbols. No estimate uses these trials.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x15  # noqa: E402


def main(raw, out):
    os.makedirs(out, exist_ok=True)
    recs = [json.loads(l) for l in open(os.path.join(raw, "x15.jsonl")) if l.strip()]
    res = {}
    for r in recs:
        if r.get("record") != "profile":
            continue
        b = r["build"]
        sec = x15.parse_sections(open(os.path.join(raw, f"{b}.report.txt")).read())
        ev = x15.primary(sec)
        res[b] = {"C_pps": r["trial"]["delivered_pps_steady"], "event": ev,
                  **x15.summarise(sec[ev]),
                  "log_pattern_pct_other_events": {e: x15.summarise(v)["log_pattern_pct"] for e, v in sec.items() if e != ev},
                  "perf_record_rc": r["perf_record_rc"], "perf_report_rc": r["perf_report_rc"]}
    errors = [r for r in recs if r.get("record") == "error"]
    json.dump({"experiment": "X15 (AMENDMENT-14 (d), exploratory, post-data)", "pattern": x15.LOG_PATTERN.pattern,
               "builds": res, "errors": errors}, open(os.path.join(out, "x15.json"), "w"), indent=1)
    lines = ["# X15 — where the switch spends its cycles (AMENDMENT-14 (d), exploratory)", "",
             f"One 10 s sat trial (l3, 64 B) per build, `perf record -e cycles:u -F 999 -g` on the switch process. "
             f"Logging share = samples whose symbol matches `{x15.LOG_PATTERN.pattern}` (fixed in the amendment), in the "
             "cpu_core section; the cpu_atom section perf also writes on this host is shown for comparison.", "",
             "| build | C (pps) | logging share (cpu_core) | (cpu_atom) | symbols |", "|---|---|---|---|---|"]
    for b, v in res.items():
        oth = ", ".join(f"{x:.1f} %" for x in v["log_pattern_pct_other_events"].values())
        lines.append(f"| {b} | {v['C_pps']:.0f} | {v['log_pattern_pct']:.1f} % | {oth} | {v['symbols']} |")
    for b, v in res.items():
        lines += ["", f"## {b}: 15 largest symbols (self %)", ""]
        lines += [f"- {t['self_pct']:.2f} % `{t['symbol'][:110]}`" for t in v["top"]]
    lines += ["", f"Errors: {len(errors)}", ""]
    open(os.path.join(out, "x15.md"), "w").write("\n".join(lines))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: report_x15.py RAW_X15 OUT_DIR")
    main(*sys.argv[1:])
