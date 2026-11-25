#!/usr/bin/env python3
"""PREREG AMENDMENT-14 (d) (exploratory): where the switch spends its cycles.

    x15.py OUT_DIR

One `sat` trial (l3, 64 B, 10 s) per build in BUILDS, each in its own switch instance after the
usual path probe. During the trial perf samples the switch process in user mode:
`perf record -e cycles:u -F 999 -g -p <switch pid> -- sleep 11`, run inside the wrapper's `exec` in
bmb-sw on the housekeeping CPU, from a copy of the kernel's perf binary under ~/bmbench/local/bin/
(the wrapper runs only programs under ~/bmbench). perf_event_paranoid must be 2 (x15.sh sets it and
puts 4 back). Writes OUT_DIR/x15.jsonl (one record per build: the trial, the 15 symbols with the
largest self share, and the share of samples whose symbol matches LOG_PATTERN, fixed in the amendment)
and keeps each perf.data. These trials are not capacity measurements and enter no estimate.
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402

PERF = os.path.join(bench.BMB, "local", "bin", "perf")
BUILDS = ("cm-default", "cm-o3-L1-E1", "at-docs-perf")
LOG_PATTERN = re.compile(r"spdlog|fmt::|Logger|logger|debug_log|BMLOG|log_msg")
TOP = 15
ROW = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)%\s+\[[.k]\]\s+(.+?)\s*$")


EVENT = re.compile(r"^# Samples: \S+ of event '([^']+)'")


def parse_sections(text):
    """{event: [(self %, symbol), ...]} of `perf report --stdio --no-children -g none --sort symbol` output.
    perf prints one section per event; on the test machine's kernel `cycles:u` opens both cpu_core and an
    empty-cpumask cpu_atom PMU, so there are two sections, each summing to ~100 %."""
    out, ev = {}, None
    for line in text.splitlines():
        m = EVENT.match(line)
        if m:
            ev = m.group(1)
            out.setdefault(ev, [])
            continue
        if line.lstrip().startswith("#"):
            continue
        m = ROW.match(line)
        if m:
            out.setdefault(ev, []).append((float(m.group(1)), m.group(2)))
    for rows in out.values():
        rows.sort(key=lambda r: -r[0])
    return out


def primary(sections):
    """The section of the PMU that owns the CPUs (cpu_core/... when present), else the first."""
    for ev in sections:
        if ev and ev.startswith("cpu_core/"):
            return ev
    return next(iter(sections), None)


def parse_report(text):
    """Rows of the primary section (see primary())."""
    sec = parse_sections(text)
    ev = primary(sec)
    return sec.get(ev, []) if ev is not None else []


def summarise(rows):
    return {"top": [{"self_pct": s, "symbol": sym} for s, sym in rows[:TOP]],
            "log_pattern_pct": round(sum(s for s, sym in rows if LOG_PATTERN.search(sym)), 2),
            "symbols": len(rows), "total_pct": round(sum(s for s, _ in rows), 2)}


def perf_report(data):
    p = subprocess.run([PERF, "report", "-i", data, "--stdio", "--no-children", "-g", "none", "--sort", "symbol",
                        "--percent-limit", "0"], capture_output=True, text=True, timeout=600)
    return p.returncode, p.stdout, p.stderr[-2000:]


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "x15.jsonl"), "a", buffering=1) as log:
        log.write(json.dumps({"record": "run-start", "harness_git": open(os.path.join(bench.BMB, "harness", "HARNESS-GIT")).read().strip(),
                              "host": os.uname().nodename, "started": time.time(), "perf": PERF,
                              "perf_event_paranoid": open("/proc/sys/kernel/perf_event_paranoid").read().strip(),
                              "governor": open("/sys/devices/system/cpu/cpu4/cpufreq/scaling_governor").read().strip(),
                              "no_turbo": open("/sys/devices/system/cpu/intel_pstate/no_turbo").read().strip()}) + "\n")
        for b in BUILDS:
            inst = {"id": f"x15-{b}", "build": b, "program": "l3", "runtime": "none", "block": 0}
            base = {"instance": inst["id"], "build": b, "program": "l3", "runtime": "none"}
            sw = None
            try:
                sw = bench.Switch(inst, out_dir)
                ident = sw.start()
                probe = bench.trial(sw, "probe", 64, 1000, 1, 0.2, 1, with_recv=False)
                if not bench.probe_ok(probe, False):
                    raise RuntimeError("path probe failed")
                data = os.path.join(out_dir, f"{b}.perf.data")
                perf = subprocess.Popen(bench.PRIV + ["exec", "bmb-sw", bench.HOUSE_CPU, PERF, "record", "-e", "cycles:u",
                                                      "-F", "999", "-g", "-p", str(sw.pid), "-o", data, "--", "sleep", "11"],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                time.sleep(0.5)
                t = bench.trial(sw, "sat", 64, 0, 32, 10, 1, with_recv=False)
                perf_out = perf.communicate(timeout=60)[0]
                rc, text, err = perf_report(data)
                sections = parse_sections(text)
                rows = parse_report(text)
                log.write(json.dumps({"record": "profile", **base, "ident": ident, "trial": t,
                                      "perf_record_rc": perf.returncode, "perf_record_out": perf_out[-2000:],
                                      "perf_report_rc": rc, "perf_report_err": err,
                                      "perf_data_bytes": os.path.getsize(data) if os.path.exists(data) else 0,
                                      "event": primary(sections),
                                      "other_events": {ev: summarise(r)["log_pattern_pct"] for ev, r in sections.items()
                                                       if ev != primary(sections)},
                                      **summarise(rows)}) + "\n")
                with open(os.path.join(out_dir, f"{b}.report.txt"), "w") as f:
                    f.write(text)
                print(f"{b}: C {t['delivered_pps_steady'] and round(t['delivered_pps_steady'])} pps; "
                      f"log-pattern {summarise(rows)['log_pattern_pct']} % of samples; {len(rows)} symbols", flush=True)
                if perf.returncode != 0 or not rows:
                    raise RuntimeError(f"perf record rc {perf.returncode}, report rc {rc}, {len(rows)} rows")
            except Exception as ex:
                log.write(json.dumps({"record": "error", **base, "error": repr(ex)}) + "\n")
                print(f"{b}: ERROR {ex!r}", flush=True)
            finally:
                if sw:
                    sw.stop()
                time.sleep(1)
        log.write(json.dumps({"record": "run-end", "finished": time.time()}) + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: x15.py OUT_DIR")
    main(sys.argv[1])
