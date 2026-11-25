#!/usr/bin/env python3
"""E8 report (PREREG AMENDMENT-7/9): published programs at the upstream default build vs the
documented performance build.

    report_e8.py RAW_DIR E8_DIR OUT_DIR

RAW_DIR is the 2026-10-21 campaign's raw dir: its pre-round gives the G1/G9 ceilings, which
run/e8.sh did not re-measure. E8_DIR holds e8hit.jsonl and e8.jsonl. Writes OUT_DIR/e8.{json,md}.

Gates, exclusions, cells and the bootstrap are report.py's, so every reading in CHOICES.md
applies (seed 'E8|<cell>'). Each cell gets two capacities (AMENDMENT-9 item 3):
- C from the h1 counter, as in E1;
- C_own = C minus the frames at h1 that are not ours / trial duration, the E8 result (seed 'E8own|<cell>').
  "Not ours" = the receiver's `foreign` + `skipped`: C0272's GNN control frames are 32 bytes, too short
  for bmrecv to parse, so it counts them as `skipped`, not `foreign` (seen in the data: 100 per 10 s
  trial, and foreign + skipped = h1 counter - receiver rx in every trial).
Result per program and frame: at-docs-perf / cm-default, with the interval paired by draw.
AMENDMENT-7 registers no decision rule for E8, only the ratio with its interval, so there are no
verdicts here. The sensitivity run removes G8-failing trials, as for E1-E5.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import report as R  # noqa: E402
from analyse import load  # noqa: E402

BUILDS = ("cm-default", "at-docs-perf")
PAPER = {"C0303": "Fig. 10a (printed p. 306): 9.37 Mbit/s, TCP iperf, benign host H3 -> H4",
         "C0272": "Table 5.3 (= C0110 Table III): 2.144 Mbit/s, 4 TCP iperf3 flows over a 2 Mbit/s bottleneck",
         # AMENDMENT-13 extension (X13; report_e8.py X12_RAW raw/x13 OUT)
         "C0441": "Sec. 5.9 (p. 19): 'maximum capacity of 14 Mbps' of the Southbound Interface (a control-channel "
                  "rate, not a forwarding rate), TCP SYN flood h1 -> h2"}
key = lambda r: (r["build"], r["program"], r["frame"]) if r.get("kind") == "sat" else None


def not_ours(r):
    """Frames at h1 that are not ours: the receiver's foreign (not our format) + skipped (too short)."""
    rc = r.get("recv")
    if not rc or rc.get("foreign") is None:
        return None
    return rc["foreign"] + rc.get("skipped", 0)


def own(r):
    """Steady rate minus the frames at h1 that are not ours, per second of the trial."""
    c, n = r.get("delivered_pps_steady"), not_ours(r)
    return None if c is None or n is None else c - n / r["duration"]


def foreign_pps(r):
    n = not_ours(r)
    return None if n is None else n / r["duration"]


def run(raw, e8dir, extra=()):
    R.Exp.EXTRA = extra
    R.Exp.BOOTS.clear()
    R.POINT.clear()
    pre = R.preround(raw)
    R.PRE.clear()
    R.PRE.update(pre)
    recs = load([os.path.join(e8dir, "e8.jsonl")])
    ex, exo = R.Exp("E8", recs, pre), R.Exp("E8own", recs, pre)
    cells = R.cap_cells(ex, ("sat",), key, lambda k: k[2])
    cells_own = {}
    for k, c in sorted(exo.cells(("sat",), key, value=own).items(), key=lambda kv: R.kstr(kv[0])):
        s = exo.summarise(k, c, frame=k[2])
        if s["median"] is not None:
            R.POINT[("E8own", k)] = s["median"]
        cells_own[k] = s
    fpps, mismatch = {}, []
    for r, fl in ex.counted(("sat",)):
        if not_ours(r) is not None and not_ours(r) != r["delivered"] - r["recv"]["rx"]:
            mismatch.append(r["instance"])                # our own frame lost between h1 and the receiver

        if not ex.excluded(fl) and foreign_pps(r) is not None:
            fpps.setdefault(key(r), []).append(foreign_pps(r))
    ratios = {}
    for prog in sorted({k[1] for k in cells}):
        for f in sorted({k[2] for k in cells}):
            a, b = (BUILDS[1], prog, f), (BUILDS[0], prog, f)
            ratios[f"{prog}|{f}"] = {"counter": R.ratio(a, b, "E8"), "own": R.ratio(a, b, "E8own")}
    R.Exp.EXTRA = ()
    return {"experiment": "E8", "gates": ex.gate_summary(), "cells_counter": R.jkeys(cells),
            "cells_own": R.jkeys(cells_own), "ratio_docs_perf_over_default": ratios,
            "not_ours_vs_counter_mismatch": mismatch,
            "foreign_pps_median": {R.kstr(k): R.med(v) for k, v in sorted(fpps.items(), key=lambda kv: R.kstr(kv[0]))},
            "paper_figure_context_only": PAPER}, cells, cells_own


def hit_summary(e8dir):
    out = {}
    for r in load([os.path.join(e8dir, "e8hit.jsonl")]):
        if r.get("record") == "instance":
            p = (r.get("hitcheck") or {}).get("profile", {})
            out[r["program"]] = {"instance": r["instance"], "probe_ok": r.get("probe_ok"),
                                 "hitcheck_ok": r.get("hitcheck_ok"), "frames": p.get("frames"),
                                 "tables": p.get("tables"), "missing_from_profile": p.get("missing_from_profile"),
                                 "path_profile_sha256": (r.get("hitcheck") or {}).get("path_profile_sha256"),
                                 "probe_recv": (r.get("probe") or {}).get("recv"), "gen_extra": r.get("gen_extra")}
        elif r.get("record") == "error":
            out.setdefault("errors", []).append({"instance": r.get("instance"), "error": r.get("error")})
    return out


def md(o, cells, cells_own, hits, sens):
    rat = o["ratio_docs_perf_over_default"]
    s = ["# E8 — published programs: upstream default build vs documented build", "",
         "PREREG AMENDMENT-7 (selection, measurement) and AMENDMENT-9 (path profile, own frames). Programs: "
         "C0303 (port knocking) and C0272 (GNN ECN marking; C0110 is the same program). No decision rule is "
         "registered: the result is the ratio with its 95 % interval.", "",
         "## G10 with the declared path profiles (plans/e8hit.json, cm-default, console logging)", "",
         R.md_table(["program", "probe (our frames at h1)", "G10", "headline frames", "tables hit / miss as declared"],
                    [[p, f"{h['probe_ok']} ({(h['probe_recv'] or {}).get('rx')} ours, {(h['probe_recv'] or {}).get('foreign')} foreign)",
                      h["hitcheck_ok"], h["frames"],
                      ", ".join(f"{t} {v['want']} {v['frames_ok']}/{h['frames']}" for t, v in (h["tables"] or {}).items()
                                if v["want"] != "not_applied")]
                     for p, h in hits.items() if p != "errors"]), "",
         R.gate_md(o["gates"]), "", R.io_md(cells, R.PRE), "",
         "## Capacity, own frames only (the E8 result: h1 counter minus the receiver's foreign frames / duration)", "",
         R.md_table(["build", "program", "frame"] + R.CELL_HDR, R.cell_rows(cells_own, lambda k: [k[0], k[1], k[2]])), "",
         "## Capacity from the h1 counter (as E1)", "",
         R.md_table(["build", "program", "frame"] + R.CELL_HDR, R.cell_rows(cells, lambda k: [k[0], k[1], k[2]])), "",
         f"Trials where foreign + skipped differs from (h1 counter - receiver rx): {o['not_ours_vs_counter_mismatch'] or 'none'}.", "",
         "Frames at h1 that are not ours (receiver foreign + skipped; median pps per cell): "
         + ", ".join(f"{k} {v:.2f}" for k, v in o["foreign_pps_median"].items()), "",
         "## at-docs-perf / cm-default", "",
         R.md_table(["program|frame", "own frames: ratio", "95% CI", "h1 counter: ratio", "95% CI",
                     "G8-failing trials removed: own ratio", "95% CI"],
                    [[k, R.f4((v["own"] or {}).get("ratio")), R.fci((v["own"] or {}).get("ci95")),
                      R.f4((v["counter"] or {}).get("ratio")), R.fci((v["counter"] or {}).get("ci95")),
                      R.f4(((sens.get(k) or {}).get("own") or {}).get("ratio")),
                      R.fci(((sens.get(k) or {}).get("own") or {}).get("ci95"))] for k, v in rat.items()]), "",
         "Paper figures, for context only (hardware, traffic and metric differ; no comparison is claimed): "
         + "; ".join(f"{p}: {t}" for p, t in o["paper_figure_context_only"].items()) + "."]
    if hits.get("errors"):
        s += ["", "E8HIT error records: " + "; ".join(f"{e['instance']}: {e['error']}" for e in hits["errors"])]
    return "\n".join(s) + "\n"


def main(argv):
    raw, e8dir, out = argv
    os.makedirs(out, exist_ok=True)
    sens, _, _ = run(raw, e8dir, extra=("G8",))
    o, cells, cells_own = run(raw, e8dir)
    o["sensitivity_g8_removed"] = sens["ratio_docs_perf_over_default"]
    hits = hit_summary(e8dir)
    o["g10_path_profile"] = hits
    R.dump(os.path.join(out, "e8.json"), o)
    with open(os.path.join(out, "e8.md"), "w") as f:
        f.write(md(o, cells, cells_own, hits, o["sensitivity_g8_removed"]))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: report_e8.py RAW_DIR E8_DIR OUT_DIR")
    main(sys.argv[1:])
