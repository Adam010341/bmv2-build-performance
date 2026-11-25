#!/usr/bin/env python3
"""Objective pre-round checks that must all hold before the campaign starts E1 (PREREG AMENDMENT-1
gates; Adam 2026-10-20: "pre-round passed => run on"). Prints one line per check; exit 0 only if
all pass.

usage: check_preround.py RAW_PREROUND_DIR
"""
import json, re, sys
from pathlib import Path


def jl(p):
    try:
        return [json.loads(l) for l in open(p) if l.strip()]
    except OSError:
        return []


def main(d):
    d = Path(d)
    st = (d / "STATUS").read_text() if (d / "STATUS").exists() else ""
    checks = []

    def ok(name, cond, detail=""):
        checks.append(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name} {detail}".rstrip())

    ok("exec-check", "exec check ok" in st)
    ok("preround-finished", "PREROUND DONE" in st)
    ok("identity", "identity exit=0" in st)
    m = re.search(r"reproducibility: (\d+) identical, (\d+) different", st)
    ok("reproducibility", bool(m) and m.group(2) == "0" and int(m.group(1)) > 0, m.group(0) if m else "")
    for step in ("G1", "G9", "G4", "G10", "smoke"):
        ok(f"{step}-exit0", f"{step} exit=0" in st)
    g1 = [r for r in jl(d / "g1-calibration.jsonl") if r.get("record") != "run-start"]
    ok("G1-records", len(g1) > 0 and not any(r.get("record") == "error" for r in g1), f"n={len(g1)}")
    g9 = [r for r in jl(d / "g9-null.jsonl") if r.get("record") == "null-forwarder"]
    ok("G9-forwarded", len(g9) > 0 and all((r.get("delivered_pps_steady") or 0) > 0 for r in g9), f"n={len(g9)}")
    g4 = {r["condition"]: r.get("flags", []) for r in jl(d / "g4-controls.jsonl") if r.get("record") == "g4-control"}
    ok("G4-controls-fire", all("G4" in g4.get(c, []) for c in ("sibling-busy-loop", "short-lived-stream")),
       json.dumps({c: "G4" in f for c, f in g4.items()}))
    ok("G4-quiet-without-load", all("G4" not in g4.get(c, ["G4"]) for c in ("none", "none-again")))
    g10 = jl(d / "g10-hitcheck.jsonl")
    inst = [r for r in g10 if r.get("record") == "instance"]
    ok("G10-all-hit", len(inst) > 0 and all(r.get("probe_ok") and r.get("hitcheck_ok") for r in inst)
       and not any(r.get("record") == "error" for r in g10),
       f"{sum(1 for r in inst if r.get('hitcheck_ok'))}/{len(inst)} ok")
    sm = jl(d / "smoke.jsonl")
    ok("smoke-clean", any(r.get("record") == "instance" for r in sm)
       and all(r.get("probe_ok", True) for r in sm if r.get("record") == "instance")
       and not any(r.get("record") == "error" for r in sm))
    print("ALL PASS" if all(checks) else "SOME FAILED")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
