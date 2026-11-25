#!/usr/bin/env python3
"""Write the registered plans (PREREG v1 §3) as JSON files for bench.py.

    make_plans.py OUTDIR [--capacity CAPS.json] [--e8] [--robust] [--x12] [--x13] [--x14]

E1, E1-RT, E2 and E3 need nothing measured. E4 and E5 set their offered rates relative to the
capacity C of their cell, so they are generated only when CAPS.json (written by analyse.py from
E1 data) is given: {"<build>|<program>|<frame>": C_pps, ...}.

--e8 adds E8 (AMENDMENT-7/9): E8HIT (G10 against each program's declared PATH.json, on the
default build with console logging) and E8 (the measured instances).

--robust adds the §6 robustness checks as AMENDMENT-11 fixes them: R1 (overload), R2 (trial length),
R3 (two port pairs) and R4 (default CPU mode; one plan per replicate and mode, run alternately).

--x12 adds AMENDMENT-12's exploratory cells (post-data): run-time log level `-L off` against compiled-in
macros, and the tutorials' own autotools -O0 -g build (`at-tutorial`).

--x13 adds AMENDMENT-13's E8 extension (post-data): X13HIT (G10) and X13 for the programs that passed.

--x14 adds AMENDMENT-14 (c)'s exploratory iperf3 TCP cells (post-data): one TCP stream through l3 on the
upstream default, the documented build, and the tutorials' build with the tutorials' recording options.

Every plan carries its own shuffle seed; bench.py shuffles instances with it and logs the order.
"""
import json
import os
import subprocess
import sys

REPS = 5
FACTORIAL = [f"cm-o{o}-L{l}-E{e}" for o in (0, 2, 3) for l in (1, 0) for e in (1, 0)]
EXTRAS = ["cm-default", "cm-default-dbg", "cm-release-L0-E0", "cm-o3native-L0-E0", "at-default", "at-docs-perf"]
DEFAULT_LIKE, PERF = "cm-o0-L1-E1", "cm-o3-L0-E0"
SAT = lambda frame: {"kind": "sat", "frame": frame, "pps": 0, "burst": 32, "duration": 10}


def cap(caps, key):
    """C for one cell of the E1 capacities, or a clear error naming the cell (it is missing when
    every trial of it was excluded by a gate; E4/E5 cannot be planned without it)."""
    if key not in caps:
        raise SystemExit(f"make_plans: no E1 capacity for {key} in the --capacity file "
                         f"(all its trials excluded?); have {sorted(caps)[:6]}...")
    return caps[key]


def git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()
    except OSError:
        return None


def inst(iid, build, program, trials, block=0, **kw):
    """block = replicate index: bench.py shuffles within a block and runs blocks in order."""
    return {"id": iid, "build": build, "program": program, "trials": trials, "block": block, **kw}


def plan(name, seed, instances):
    return {"name": name, "shuffle_seed": seed, "harness_git": git_rev(), "instances": instances}


def e1(docker):
    out = []
    for b in FACTORIAL:
        for p in ("min", "l3"):
            for r in range(REPS):
                out.append(inst(f"e1-{b}-{p}-r{r}", b, p, [SAT(64), SAT(1518)], block=r))
    for b in EXTRAS + ([f"docker:{docker}"] if docker else []):
        for r in range(REPS):
            out.append(inst(f"e1x-{b.replace(':', '_')}-l3-r{r}", b, "l3", [SAT(64), SAT(1518)], block=r))
    return plan("E1", 101, out)


def e1rt(docker):
    out = []
    builds = [DEFAULT_LIKE, "cm-o3-L1-E1"] + ([f"docker-grpc:{docker}"] if docker else [])
    for b in builds:
        for rt in ("none", "console", "pcap", "nanolog", "tutorial"):
            for r in range(REPS):
                out.append(inst(f"e1rt-{b.replace(':', '_')}-{rt}-r{r}", b, "l3", [SAT(64)], block=r, runtime=rt))
    return plan("E1-RT", 102, out)


def e2():
    progs = ["min", "l3", "exact_1", "exact_2", "exact_4", "exact_8", "lpm_1", "lpm_4", "lpm_8",
             "ternary_1", "ternary_4", "ternary_8", "l3_reg"]
    out = [inst(f"e2-{b}-{p}-r{r}", b, p, [SAT(64)], block=r) for b in (DEFAULT_LIKE, PERF) for p in progs for r in range(REPS)]
    return plan("E2", 103, out)


def e3():
    frames = [64, 128, 256, 512, 1024, 1280, 1518]
    out = []
    for b in (DEFAULT_LIKE, PERF):
        for r in range(REPS):
            # trial order inside the instance is shuffled by bench.py (seeded per instance)
            out.append(inst(f"e3-{b}-l3-r{r}", b, "l3", [SAT(f) for f in frames], block=r))
    return plan("E3", 104, out)


def e4(caps):
    out = []
    for b in (DEFAULT_LIKE, PERF):
        for r in range(REPS):
            trials = []
            for f in (64, 1518):
                c = cap(caps, f"{b}|l3|{f}")
                for burst in (1, 8, 32, 128):
                    trials.append({"kind": "zl", "frame": f, "burst": burst, "duration": 10,
                                   "lo": round(0.02 * c), "hi": round(1.3 * c), "threshold": 0.0})
                trials.append({"kind": "iperf3", "frame": f, "flows": 1,
                               "ladder_pps": [round(x * c) for x in (0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0, 1.2, 1.5)]})
            out.append(inst(f"e4-{b}-l3-r{r}", b, "l3", trials, block=r))
    return plan("E4", 105, out)


def e5(caps):
    """AMENDMENT-1 design. Table size is fixed (256 entries) while the flow count varies, so the
    number of DISTINCT entries hit is the only thing that changes with n. Per-flow loss is read on
    a ladder of aggregate rates relative to each cell's own capacity, with the receiver on for
    every trial (capacity is measured with the same sink)."""
    out = []
    for b in (DEFAULT_LIKE, PERF):
        for n in (1, 16, 256):
            for prog in ("l3", "flow_exact_256", "flow_ternary_256"):
                for r in range(REPS):
                    trials = [{**SAT(64), "flows": n, "recv": True}]
                    for frac in (0.3, 0.5, 0.7, 0.85):
                        for burst in (1, 32):
                            trials.append({"kind": "rate", "frame": 64, "pps_frac_of_sat": frac, "burst": burst,
                                           "flows": n, "recv": True})
                    out.append(inst(f"e5-{b}-{prog}-n{n}-r{r}", b, prog, trials, block=r, shuffle_trials=False))
    # the previous round's measurement form, reproduced on this harness (E5.d)
    c = cap(caps, f"{PERF}|l3|64")
    for n in (1, 16):
        for r in range(REPS):
            out.append(inst(f"e5d-{PERF}-l3-n{n}-r{r}", PERF, "l3",
                            [{"kind": "iperf3", "frame": 64, "flows": n,
                              "ladder_pps": [round(x * c / n) for x in (0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0, 1.2)]}],
                            block=r))
    return plan("E5", 106, out)


def hitcheck():
    """Before the round: every program, one instance on a logging build, bmv2's log must show
    every const-entry table hit by the probe frames (AMENDMENT-1, G10)."""
    progs = sorted(os.path.basename(p)[:-5] for p in __import__("glob").glob(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "p4", "build", "*.json")))
    out = [inst(f"hit-{p}", "cm-o3-L1-E1", p, [], hitcheck=True) for p in progs]
    return plan("HITCHECK", 100, out)


E8_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "p4", "e8")
E8_PROGRAMS = ("C0303", "C0272")   # distinct programs that passed (C0110 = C0272's program, AMENDMENT-9)
E8_BUILDS = ("cm-default", "at-docs-perf")


def e8_fields(cid):
    """Per-program instance fields; paths relative to the plan in harness/plans/."""
    d = os.path.join(E8_DIR, cid)
    prog = [f for f in os.listdir(d) if f.endswith(".json") and f != "PATH.json"]
    if len(prog) != 1:
        raise SystemExit(f"make_plans: {cid}: expected one program JSON, found {prog}")
    fr = json.load(open(os.path.join(d, "PATH.json")))["frames"]
    gen = {"dst_ip": fr["dst_ip"], "src_ip": fr["src_ip"], "tos": int(str(fr.get("tos", 0)), 0)}
    rel = f"../p4/e8/{cid}/"
    return {"program_json": rel + prog[0], "entries_cli": rel + "entries.txt", "gen": gen, "own_frames": True}


def e8hit():
    out = [inst(f"hit-e8-{c}", "cm-default", c, [], hitcheck=True,
                path_profile=f"../p4/e8/{c}/PATH.json", **e8_fields(c)) for c in E8_PROGRAMS]
    return plan("E8HIT", 110, out)


def e8():
    sat = lambda f: {**SAT(f), "recv": True}         # receiver on: only our frames are C (AMENDMENT-9)
    out = [inst(f"e8-{b}-{c}-r{r}", b, c, [sat(64), sat(1518)], block=r, **e8_fields(c))
           for c in E8_PROGRAMS for b in E8_BUILDS for r in range(REPS)]
    return plan("E8", 109, out)


X13_PROGRAMS = ("C0441",)  # AMENDMENT-13: the one program that passed the extended examination


def x13hit():
    """AMENDMENT-13: G10 for the extension's programs, as E8HIT."""
    out = [inst(f"hit-x13-{c}", "cm-default", c, [], hitcheck=True,
                path_profile=f"../p4/e8/{c}/PATH.json", **e8_fields(c)) for c in X13_PROGRAMS]
    return plan("X13HIT", 130, out)


def x13():
    """AMENDMENT-13: the extension's programs measured exactly as E8."""
    sat = lambda f: {**SAT(f), "recv": True}
    out = [inst(f"x13-{b}-{c}-r{r}", b, c, [sat(64), sat(1518)], block=r, **e8_fields(c))
           for c in X13_PROGRAMS for b in E8_BUILDS for r in range(REPS)]
    return plan("X13", 131, out)


CORNERS = [f"cm-o{o}-L{l}-E{e}" for o in (0, 3) for l in (1, 0) for e in (1, 0)]   # AMENDMENT-11


def r1():
    """§6.1 overload: sat, then rate at 2 x this instance's own C (order fixed)."""
    over = {"kind": "rate", "frame": 64, "pps_frac_of_sat": 2.0, "burst": 32, "duration": 10}
    out = [inst(f"r1-{b}-l3-r{r}", b, "l3", [SAT(64), over], block=r, shuffle_trials=False)
           for b in CORNERS for r in range(REPS)]
    return plan("R1", 111, out)


def r2():
    """§6.2 trial length: sat 10 s and sat 30 s in one instance (order shuffled per instance)."""
    out = [inst(f"r2-{b}-l3-r{r}", b, "l3", [SAT(64), {**SAT(64), "duration": 30}], block=r)
           for b in (DEFAULT_LIKE, PERF) for r in range(REPS)]
    return plan("R2", 112, out)


def r3():
    """§6.3 two concurrent port pairs (h0->h1 and h2->h3) against one pair."""
    out = [inst(f"r3-{b}-l3-r{r}", b, "l3", [SAT(64), {**SAT(64), "pairs": 2}], block=r)
           for b in CORNERS for r in range(REPS)]
    return plan("R3", 113, out)


def r4(mode, block):
    """§6.4 one replicate in one CPU mode; run/robust.sh alternates the modes per replicate."""
    cells = [(b, "l3") for b in CORNERS] + [(b, p) for b in (DEFAULT_LIKE, PERF) for p in ("min", "exact_8")]
    out = [inst(f"r4-{mode}-{b}-{p}-r{block}", b, p, [SAT(64)], block=block, cpu_mode=mode) for b, p in cells]
    return plan(f"R4-{mode}-b{block}", 114, out)


X12_CELLS = [("cm-o0-L1-E1", "none"), ("cm-o0-L1-E1", "logoff"), ("cm-o0-L1-E1", "tutorial"),
             ("cm-o3-L1-E1", "none"), ("cm-o3-L1-E1", "logoff"), ("cm-o0-L0-E1", "none"), ("cm-o3-L0-E1", "none"),
             ("at-tutorial", "none"), ("at-tutorial", "tutorial"), ("at-docs-perf", "none")]


def x12():
    """AMENDMENT-12 (exploratory): 10 cells x 5 instances of E1's sat on l3, one seeded order."""
    out = [inst(f"x12-{b}-{rt}-r{r}", b, "l3", [SAT(64)], block=r, runtime=rt)
           for b, rt in X12_CELLS for r in range(REPS)]
    return plan("X12", 121, out)


X14_CELLS = [("cm-default", "none"), ("at-docs-perf", "none"), ("at-tutorial", "tutorial")]
X14_REPS = 10


def x14():
    """AMENDMENT-14 (c) (exploratory): 3 cells x 10 instances of one 10 s iperf3 TCP stream, one seeded order."""
    out = [inst(f"x14-{b}-{rt}-r{r}", b, "l3", [{"kind": "iperf3-tcp", "duration": 10}], block=r, runtime=rt)
           for b, rt in X14_CELLS for r in range(X14_REPS)]
    return plan("X14", 141, out)


def smoke():
    """Harness check only (PREREG 4): never used for a registered claim. Exercises every code path:
    sat at two sizes, a relative-rate trial with the receiver on and 16 flows, a short zl search,
    and a 3-rung iperf3 ladder."""
    out = []
    for b in (DEFAULT_LIKE, PERF):
        out.append(inst(f"smoke-{b}", b, "l3", [
            {**SAT(64), "flows": 16, "recv": True}, SAT(1518),
            {"kind": "rate", "frame": 64, "pps_frac_of_sat": 0.5, "burst": 32, "flows": 16, "recv": True},
            {"kind": "zl", "frame": 64, "burst": 1, "duration": 5, "lo": 500, "hi": 2000000, "threshold": 0.0},
            {"kind": "iperf3", "frame": 1518, "flows": 2, "ladder_pps": [1000, 5000, 20000], "duration": 5}],
            shuffle_trials=False))
    return plan("SMOKE", 99, out)


def main():
    outdir = sys.argv[1]
    caps = None
    if "--capacity" in sys.argv:
        caps = json.load(open(sys.argv[sys.argv.index("--capacity") + 1]))
    docker = os.environ.get("DOCKER_IMAGE_DIR")      # e.g. latest-b2ca70c6b04e
    os.makedirs(outdir, exist_ok=True)
    plans = [smoke(), hitcheck(), e1(docker), e1rt(docker), e2(), e3()]
    if caps:
        plans += [e4(caps), e5(caps)]
    if "--e8" in sys.argv:
        plans += [e8hit(), e8()]
    if "--robust" in sys.argv:
        plans += [r1(), r2(), r3()] + [r4(m, r) for r in range(REPS) for m in ("bench", "default")]
    if "--x12" in sys.argv:
        plans += [x12()]
    if "--x13" in sys.argv:
        plans += [x13hit(), x13()]
    if "--x14" in sys.argv:
        plans += [x14()]
    for p in plans:
        path = os.path.join(outdir, f"{p['name'].lower().replace('-', '')}.json")
        json.dump(p, open(path, "w"), indent=1)
        print(f"{path}: {len(p['instances'])} instances, {sum(len(i['trials']) for i in p['instances'])} trial specs")


if __name__ == "__main__":
    main()
