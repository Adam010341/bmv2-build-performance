#!/usr/bin/env python3
"""dryrun_check.py -- verdicts on the outputs of tests/test_dryrun.sh (stdlib only).

The fake network is exact (no kernel, no noise except host CPU load), so anything a counter-based
gate flags here -- G2 gen-vs-h0tx, G3 receiver drops/accounting, no-drain, schedule -- is a
bookkeeping bug in the harness, not a measurement. G4 (foreign CPU) and G8 (frequency) depend on
the host and are ignored. Capacities are known: config switch_pps for the fake switch, null_pps for
the fake bmnull.

  dryrun_check.py bench PLAN.json OUT.jsonl STATE_DIR PRIV [--expect-error ID:TEXT ...]
                        [--expect-probe-fail ID ...] [--expect-hitcheck-fail ID ...]
  dryrun_check.py calibrate OUT.jsonl PRIV
  dryrun_check.py null OUT.jsonl STATE_DIR PRIV
  dryrun_check.py controls OUT.jsonl PRIV
  dryrun_check.py caps CAPS.json KEY ...
Prints one line per problem ("FAIL ...") and exits 1 if there was any.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake"))
import fakenet  # noqa: E402

BAD_FLAGS = {"G2:gen-vs-h0tx", "G2:gen-vs-h2tx", "G3:recv-drops", "G3:recv-accounting", "no-drain", "schedule"}
problems = []


def fail(msg):
    problems.append(msg)
    print(f"FAIL {msg}")


def note(msg):
    print(f"NOTE {msg}")


def load(path):
    out = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError as e:
                    fail(f"{path}:{n}: not JSON ({e})")
    return out


def cfg_of(state):
    os.environ["FAKE_STATE"] = state
    return fakenet.config()


def check_start(path, recs, priv):
    starts = [r for r in recs if r.get("record") == "run-start"]
    if not starts:
        fail(f"{path}: no run-start record")
    for r in starts:
        if r.get("priv") != [priv]:
            fail(f"{path}: run-start priv is {r.get('priv')!r}, expected the fake wrapper [{priv!r}]")


def near(x, want, lo, hi):
    return x is not None and lo * want <= x <= hi * want


def tables_with_entries(prog_json):
    p = json.load(open(prog_json))
    return [t["name"] for pl in p["pipelines"] for t in pl["tables"] if t.get("entries")]


def plan_file(plan_path, p):
    """A program_json / entries_cli path as bench.py resolves it: relative to the plan file."""
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(plan_path)), p))


def file_sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def check_entries(plan_path, out_path, inst, irec, recs, failed):
    """E8 instances (entries_cli): what the instance record says about the load, and the transcript.
    A failed load must cost the instance before anything is measured: no instance or trial record."""
    iid = inst["id"]
    ent = plan_file(plan_path, inst["entries_cli"])
    transcript = os.path.join(os.path.splitext(out_path)[0] + ".logs", f"{iid}.entries.log")
    if not os.path.exists(transcript):
        fail(f"{out_path}: {iid}: no entries transcript {transcript}")
        last = ""
    else:
        last = (open(transcript).read().strip().splitlines() or [""])[-1]
    if failed:
        if not last.startswith("cli_load: FAILED"):
            fail(f"{out_path}: {iid}: transcript does not end in a failed load: {last!r}")
        n = [r.get("record") for r in recs if r.get("instance") == iid and r.get("record") in ("instance", "trial")]
        if n:
            fail(f"{out_path}: {iid}: failed entries load, yet {len(n)} instance/trial records")
        return
    if not last.startswith("cli_load: OK"):
        fail(f"{out_path}: {iid}: transcript does not end in a good load: {last!r}")
    want = {"entries_cli": ent, "entries_sha256": file_sha(ent),
            "entries_commands": sum(1 for l in open(ent) if l.strip() and not l.lstrip().startswith("#")),
            "entries_transcript": transcript}
    for k, v in want.items():
        if irec.get(k) != v:
            fail(f"{out_path}: {iid}: instance record {k} = {irec.get(k)!r}, expected {v!r}")
    if inst.get("hitcheck") and not inst.get("path_profile"):     # a declared profile replaces this rule (check_e8)
        et = irec.get("hitcheck", {}).get("entries_tables")
        if not et or not all(v["hit"] >= 10 and v["miss"] == 0 for v in et.values()):
            fail(f"{out_path}: {iid}: tables filled from entries_cli not hit by every frame: {et}")


def gen_extra_of(gen):
    """The bmgen options bench.py's gen_extra() makes of a plan's "gen" fields."""
    out = []
    for k in ("dst_ip", "src_ip"):
        if k in gen:
            out += ["--" + k.replace("_", "-"), gen[k]]
    if "tos" in gen:
        out += ["--tos", str(gen["tos"])]
    return out


def check_e8(plan_path, out_path, state, inst, irec, cfg, probe_fail, hit_fail):
    """E8 instances (AMENDMENT-9): the generator fields, the path profile and the own-frames probe, as
    they appear in the instance record, the receiver reports and what the fake bmgen was called with.
    probe_fail / hit_fail: the instance is expected to fail that gate (and nothing else)."""
    iid = inst["id"]
    own = bool(inst.get("own_frames"))
    gx = gen_extra_of(inst.get("gen") or {})
    if irec.get("gen_extra") != (gx or None):
        fail(f"{out_path}: {iid}: instance record gen_extra {irec.get('gen_extra')!r}, plan's fields give {gx!r}")
    # every bmgen call of the instance (readiness, probe, hitcheck, trials) carried the fields
    try:
        calls = [json.loads(l) for l in open(os.path.join(state, "bmgen-args.jsonl")) if l.strip()]
    except OSError:
        calls = []
    g = inst.get("gen") or {}
    if len(calls) < 3:        # readiness, probe, hitcheck (and the trials, if the plan has any)
        fail(f"{out_path}: {iid}: only {len(calls)} bmgen calls recorded in {state}")
    for c in calls:
        got = {"dst_ip": c.get("dst-ip"), "src_ip": c.get("src-ip"), "tos": c.get("tos")}
        want = {k: (str(g[k]) if k in g else None) for k in got}
        if got != want:
            fail(f"{out_path}: {iid}: a bmgen call got {got}, plan says {want}")
            break
    # the program sends frames of its own to h1: h1's interface counter cannot tell
    pr = irec["probe"]
    if cfg["own_frames_pps"] > 0 and pr["delivered"] == 200:
        fail(f"{out_path}: {iid}: h1's counter saw exactly 200 frames in the probe: the fake switch sent none of its own")
    if own and irec["probe_ok"] is True and not (pr.get("recv") and pr["recv"]["rx"] == 200):
        fail(f"{out_path}: {iid}: own-frames probe ok, yet receiver rx is {pr.get('recv') and pr['recv']['rx']}")
    if own and irec["probe_ok"] is True and cfg["own_frames_pps"] > 0 and not pr["recv"]["foreign"]:
        fail(f"{out_path}: {iid}: probe receiver saw no foreign frame: {pr['recv']}")
    if not own and probe_fail and pr.get("recv") is not None:
        fail(f"{out_path}: {iid}: no own_frames, yet the probe has a receiver report")
    # the path profile
    hc = irec.get("hitcheck") or {}
    pp = plan_file(plan_path, inst["path_profile"])
    if hc.get("path_profile") != pp or hc.get("path_profile_sha256") != file_sha(pp):
        fail(f"{out_path}: {iid}: hitcheck profile {hc.get('path_profile')!r} / sha vs the plan's {pp}")
    prof = hc.get("profile") or {}
    want = json.load(open(pp))["tables"]
    if prof.get("frames", 0) < 10:
        fail(f"{out_path}: {iid}: hitcheck saw {prof.get('frames')} headline frames, need >= 10")
    if cfg["recirc_copy"] and hc.get("frames_in_log") != 2 * prof.get("frames", -1):
        fail(f"{out_path}: {iid}: log has {hc.get('frames_in_log')} packets for {prof.get('frames')} headline frames: "
             f"the N.2 copies are missing, or were counted as frames")
    for t, w in want.items():
        v = (prof.get("tables") or {}).get(t)
        if not v or v["want"] != w:
            fail(f"{out_path}: {iid}: profile table {t}: {v}, plan's profile says {w}")
        elif not hit_fail and (v["frames_ok"] != prof["frames"] or v["hit" if w == "hit" else "miss"] != prof["frames"]
                               or v["miss" if w == "hit" else "hit"] != 0):
            fail(f"{out_path}: {iid}: profile table {t} ({w}): {v} over {prof['frames']} frames")
    if hit_fail:
        bad = [x for x in prof.get("wrong", []) if x["table"] == "ingress.deny" and x["want"] == "hit"]
        if not bad or bad[0]["got"] != ["miss"]:
            fail(f"{out_path}: {iid}: expected 'ingress.deny declared hit, got miss' among the wrong frames: {prof.get('wrong')}")


def check_pair2(out_path, iid, r, cap):
    """R3 (AMENDMENT-11): h0 -> h1 and h2 -> h3 at once share the fake switch's capacity. Each pair is
    counted on its own interfaces, h3 is a sink (not stray), h2 receives nothing."""
    p2 = r.get("pair2")
    if r.get("pairs") != 2 or not p2:
        fail(f"{out_path}: {iid}: two-pair trial without its second pair: pairs {r.get('pairs')!r}")
        return
    ca, cb = r["counters_before"], r["counters_after"]
    d = lambda ns, ifc, key: cb[ns][ifc][key] - ca[ns][ifc][key] if ns == "sw" else cb[ns][key] - ca[ns][key]
    if p2["gen"]["accepted"] != d("h2", None, "tx_packets") or p2["switch_rx"] != d("sw", "sw2", "rx_packets"):
        fail(f"{out_path}: {iid}: pair 2 generator {p2['gen']['accepted']} / switch rx {p2['switch_rx']} "
             f"vs h2 tx {d('h2', None, 'tx_packets')}")
    if p2["delivered"] != d("h3", None, "rx_packets") or p2["switch_tx"] != d("sw", "sw3", "tx_packets"):
        fail(f"{out_path}: {iid}: pair 2 delivered {p2['delivered']} / switch tx {p2['switch_tx']} vs h3 rx")
    if r["stray_rx"] != {"h2": 0}:
        fail(f"{out_path}: {iid}: two-pair stray {r['stray_rx']} (h3 is a sink, h2 must get nothing)")
    s1, s2 = r["delivered_pps_steady"], p2["delivered_pps_steady"]
    if not (s1 and s2 and s1 > 0.25 * cap and s2 > 0.25 * cap):
        fail(f"{out_path}: {iid}: two pairs share the switch, steady {s1} and {s2} vs fake capacity {cap}")
    elif r["delivered_pps_steady_total"] != s1 + s2 or not near(s1 + s2, cap, 0.85, 1.1):
        fail(f"{out_path}: {iid}: two-pair total {r['delivered_pps_steady_total']} vs {s1} + {s2}, capacity {cap}")
    if len(p2["samples"]) < 5:
        fail(f"{out_path}: {iid}: pair 2 has {len(p2['samples'])} counter samples")
    elif not all(c is None or ca["h3"]["rx_packets"] <= c <= cb["h3"]["rx_packets"] for _, c in p2["samples"]):
        fail(f"{out_path}: {iid}: pair 2 samples are not h3's counter")
    def steady(smp):                                  # bench.py's steady window, recomputed here
        w = [(t, c) for t, c in smp if c is not None and 1.0 <= t <= r["duration"] - 0.5]
        return (w[-1][1] - w[0][1]) / (w[-1][0] - w[0][0]) if len(w) >= 2 else None
    if steady([(x[0], x[1]) for x in r["samples"]]) != s1 or steady([tuple(x) for x in p2["samples"]]) != s2:
        fail(f"{out_path}: {iid}: steady rates {s1}, {s2} are not those of each pair's own samples")


def check_bench(plan_path, out_path, state, priv, expect, probe_fails=(), hit_fails=()):
    cfg = cfg_of(state)
    cap = cfg["switch_pps"]
    plan = json.load(open(plan_path))
    recs = load(out_path)
    check_start(out_path, recs, priv)
    if not recs or recs[-1].get("record") != "run-end":
        fail(f"{out_path}: last record is not run-end")
    errors = [r for r in recs if r.get("record") == "error"]
    for r in errors:
        if not any(r["instance"] == iid and txt in r["error"] for iid, txt in expect):
            fail(f"{out_path}: unexpected error in {r['instance']}: {r['error']}")
    for iid, txt in expect:
        if not any(r["instance"] == iid and txt in r["error"] for r in errors):
            fail(f"{out_path}: expected an error record for {iid} containing {txt!r}")
    expected_err = {iid for iid, _ in expect}
    p4dir = os.path.join(os.environ["BMBENCH_ROOT"], "p4")
    for inst in plan["instances"]:
        iid = inst["id"]
        if iid in expected_err:
            if inst.get("entries_cli"):
                check_entries(plan_path, out_path, inst, None, recs, failed=True)
            continue
        irec = [r for r in recs if r.get("record") == "instance" and r["instance"] == iid]
        if len(irec) != 1:
            fail(f"{out_path}: {iid}: {len(irec)} instance records")
            continue
        irec = irec[0]
        if inst.get("entries_cli"):
            check_entries(plan_path, out_path, inst, irec, recs, failed=False)
        # the switch was started with exactly the recording options its runtime names
        rt = "console" if inst.get("hitcheck") else inst.get("runtime", "none")
        argv = (irec.get("ident") or {}).get("argv") or []
        got = {"console": "--log-console" in argv, "pcap": "--pcap" in argv, "nanolog": "--nanolog" in argv,
               "logoff": any(a == "-L" and argv[k + 1:k + 2] == ["off"] for k, a in enumerate(argv)),
               "level": "-L" in argv or "--log-level" in argv}
        want = {"console": rt in ("console", "tutorial"), "pcap": rt in ("pcap", "tutorial"),
                "nanolog": rt in ("nanolog", "tutorial"), "logoff": rt == "logoff", "level": rt == "logoff"}
        if argv and got != want:
            fail(f"{out_path}: {iid}: runtime {rt!r} but switch options {got}")
        if inst.get("program_json"):
            pj = plan_file(plan_path, inst["program_json"])
            if irec.get("program_json") != pj or irec["ident"]["program_sha256"] != file_sha(pj):
                fail(f"{out_path}: {iid}: program_json {irec.get('program_json')!r} / sha "
                     f"{irec['ident']['program_sha256'][:12]} vs plan's {pj}")
        if iid in probe_fails:       # expected fault: the instance is skipped, nothing measured
            if irec["probe_ok"] is not False:
                fail(f"{out_path}: {iid}: probe_ok {irec['probe_ok']!r}, expected the probe to fail")
            n = [r.get("kind") for r in recs if r.get("instance") == iid and r.get("record") in ("trial",)]
            if n:
                fail(f"{out_path}: {iid}: probe failed, yet trial records {n}")
        elif irec["probe_ok"] is not True:
            fail(f"{out_path}: {iid}: path probe not ok: {irec['probe']}")
        if inst.get("path_profile") or inst.get("gen") or inst.get("own_frames"):
            check_e8(plan_path, out_path, state, inst, irec, cfg, iid in probe_fails, iid in hit_fails)
        if inst.get("hitcheck"):
            # every program must pass, including one without const-entry tables (min), as long as
            # the log shows the frames
            if iid in hit_fails:
                if irec.get("hitcheck_ok") is not False:
                    fail(f"{out_path}: {iid}: hitcheck_ok {irec.get('hitcheck_ok')!r}, expected the profile check to fail")
            elif irec.get("hitcheck_ok") is not True:
                fail(f"{out_path}: {iid}: hitcheck not ok: {irec.get('hitcheck')}")
            if irec.get("runtime") != "console":
                fail(f"{out_path}: {iid}: hitcheck instance labelled runtime {irec.get('runtime')!r}")
        iend = [r for r in recs if r.get("record") == "instance-end" and r["instance"] == iid]
        if len(iend) != 1 or iend[0].get("binary_unchanged") is not True:
            fail(f"{out_path}: {iid}: instance-end missing or binary changed: {iend}")
        if iid in probe_fails:
            continue                 # skipped: no trial was meant to run (none found above)
        trials = [r for r in recs if r.get("record") == "trial" and r["instance"] == iid]
        for r in trials:
            bad = BAD_FLAGS & set(r.get("flags", []))
            if bad:
                fail(f"{out_path}: {iid} {r.get('kind')} f{r.get('frame')} pps {r.get('offered_pps')}: flags {sorted(bad)}")
        final = [r for r in trials if not r.get("superseded")]
        for spec in inst["trials"]:
            k = spec["kind"]
            if k in ("sat", "rate"):
                got = [r for r in final if r["kind"] == k and r["frame"] == spec["frame"]
                       and r["flows"] == spec.get("flows", 1) and r["burst"] == spec.get("burst", 32)
                       and r.get("pairs", 1) == spec.get("pairs", 1) and r["duration"] == spec.get("duration", 10)
                       and r.get("pps_frac_of_sat") == spec.get("pps_frac_of_sat")
                       and (k == "sat" or "pps_frac_of_sat" in spec or r["offered_pps"] == spec.get("pps"))]
                if len(got) != 1:
                    fail(f"{out_path}: {iid}: {len(got)} final records for trial spec {spec}")
                    continue
                r = got[0]
                if spec.get("pairs", 1) == 2:
                    check_pair2(out_path, iid, r, cap)
                elif "pair2" in r or r.get("pairs", 1) != 1:
                    fail(f"{out_path}: {iid} {k}: one-pair trial carries a second pair: {r.get('pairs')}")
                elif k == "sat" and not near(r["delivered_pps_steady"], cap, 0.85, 1.1):
                    fail(f"{out_path}: {iid} sat f{spec['frame']}: steady {r['delivered_pps_steady']} vs fake capacity {cap}")
                if k == "rate":
                    if r["offered_pps"] <= 0.9 * cap and r["loss"] != 0:
                        fail(f"{out_path}: {iid} rate {r['offered_pps']} pps: loss {r['loss']} below capacity")
                if spec.get("recv"):
                    rv = r["recv"]
                    # h1's counter also counts frames the program sends itself (own_frames): those are
                    # the receiver's `foreign`, ours are its `rx`
                    own_f = rv["foreign"] if rv is not None and inst.get("own_frames") else 0
                    if rv is None or rv["rx"] + own_f != r["delivered"]:
                        fail(f"{out_path}: {iid} {k}: receiver rx {rv and rv['rx']} + foreign {own_f} != delivered {r['delivered']}")
                    elif inst.get("own_frames") and cfg["own_frames_pps"] > 0 and not rv["foreign"]:
                        fail(f"{out_path}: {iid} {k}: the fake switch sends frames of its own, the receiver saw no foreign")
                    elif sum(f["rx"] for f in rv["flows"]) != rv["rx"] or \
                            (r["delivered"] >= spec.get("flows", 1) and len(rv["flows"]) != spec.get("flows", 1)):
                        fail(f"{out_path}: {iid} {k}: receiver per-flow accounting {len(rv['flows'])} flows")
                    if r["gen"]["per_flow_accepted"] and len(r["gen"]["per_flow_accepted"]) != spec.get("flows", 1):
                        fail(f"{out_path}: {iid} {k}: generator per-flow list length")
            elif k == "zl":
                res = [r for r in recs if r.get("kind") == "zl-result" and r["instance"] == iid
                       and r["frame"] == spec["frame"] and r["burst"] == spec.get("burst", 1)]
                if len(res) != 1:
                    fail(f"{out_path}: {iid}: {len(res)} zl-result records for {spec}")
                    continue
                r = res[0]
                if not (r["bracket"]["lo_passes"] and r["bracket"]["hi_fails"]):
                    fail(f"{out_path}: {iid} zl: bracket {r['bracket']} (fake capacity {cap}, lo {spec['lo']}, hi {spec['hi']})")
                elif not near(r["zero_loss_pps"], cap, 0.95, 1.05):
                    fail(f"{out_path}: {iid} zl f{spec['frame']} b{spec.get('burst', 1)}: zero-loss rate "
                         f"{r['zero_loss_pps']} vs fake capacity {cap}")
                steps = [x for x in final if x["kind"] == "zl-step" and x["frame"] == spec["frame"]
                         and x["burst"] == spec.get("burst", 1)]
                if len(steps) != len(r["steps"]):
                    fail(f"{out_path}: {iid} zl: {len(steps)} step records vs {len(r['steps'])} steps in the result")
            elif k == "iperf3-tcp":       # AMENDMENT-14 (c): one TCP stream, goodput at the fake's bulk rate
                t = [r for r in final if r["kind"] == "iperf3-tcp"]
                want = cfg["tcp_fake_bps"]
                if len(t) != 1:
                    fail(f"{out_path}: {iid}: {len(t)} iperf3-tcp records")
                elif not (t[0]["iperf3_ok"] and t[0]["server_ok"]):
                    fail(f"{out_path}: {iid} iperf3-tcp: incomplete (ok {t[0]['iperf3_ok']}, server_ok {t[0]['server_ok']})")
                elif not near(t[0]["iperf3_received_bps"], want, 0.85, 1.05):
                    fail(f"{out_path}: {iid} iperf3-tcp: goodput {t[0]['iperf3_received_bps']} vs fake {want}")
                elif not t[0]["samples"] or not t[0]["cpu_busy_per_cpu"] or t[0]["delivered"] <= 0:
                    fail(f"{out_path}: {iid} iperf3-tcp: no samples / busy / delivered frames")
            elif k == "iperf3":
                rungs = [r for r in final if r["kind"] == "iperf3-rung" and r["frame"] == spec["frame"]
                         and r["flows"] == spec.get("flows", 1)]
                if [r["offered_pps"] for r in rungs] != spec["ladder_pps"]:
                    fail(f"{out_path}: {iid} iperf3: rungs {[r['offered_pps'] for r in rungs]} vs ladder {spec['ladder_pps']}")
                for r in rungs:
                    agg = r["offered_pps"] * r["flows"]
                    if r["iperf3_worst_flow_loss_pct"] is None or not all(r["server_ok"]):
                        fail(f"{out_path}: {iid} iperf3 {r['offered_pps']} pps x{r['flows']}: incomplete "
                             f"(per_flow {r['iperf3_per_flow']}, server_ok {r['server_ok']})")
                    elif agg <= 0.85 * cap and r["iperf3_worst_flow_loss_pct"] != 0:
                        fail(f"{out_path}: {iid} iperf3 {agg} pps aggregate: loss {r['iperf3_worst_flow_loss_pct']} % below capacity")
                    if not r["drained"]:
                        fail(f"{out_path}: {iid} iperf3 {r['offered_pps']}: not drained")


def check_calibrate(path, priv):
    recs = load(path)
    check_start(path, recs, priv)
    cal = [r for r in recs if r.get("record") == "calibration"]
    if len(cal) != 7 * 2 * 3:
        fail(f"{path}: {len(cal)} calibration records, expected 42")
    for r in cal:
        if r["h0_tx"] != r["gen"]["accepted"] or r["sw0_rx"] != r["h0_tx"]:
            fail(f"{path}: f{r['frame']} b{r['burst']} r{r['rep']}: gen {r['gen']['accepted']} h0_tx {r['h0_tx']} sw0_rx {r['sw0_rx']}")


def check_null(path, state, priv):
    cap = cfg_of(state)["null_pps"]
    recs = load(path)
    check_start(path, recs, priv)
    nf = [r for r in recs if r.get("record") == "null-forwarder"]
    if len(nf) != 3 * 7:
        fail(f"{path}: {len(nf)} null-forwarder records, expected 21")
    pids = set()
    with open(os.path.join(state, "started.log")) as f:
        for line in f:
            pid, tool, _ = line.split()
            if tool == "bmnull":
                pids.add(pid)
    for r in nf:
        if BAD_FLAGS & set(r["flags"]):
            fail(f"{path}: null r{r['rep']} f{r['frame']}: flags {r['flags']}")
        if not near(r["delivered_pps_steady"], cap, 0.85, 1.1):
            fail(f"{path}: null r{r['rep']} f{r['frame']}: steady {r['delivered_pps_steady']} vs fake bmnull capacity {cap}")
        if not set(r["cpu_switch_threads"]) or not set(r["cpu_switch_threads"]) <= pids:
            fail(f"{path}: null r{r['rep']} f{r['frame']}: cpu_switch_threads are threads of "
                 f"{sorted(r['cpu_switch_threads'])}, not of bmnull (pids {sorted(pids)})")
    summ = [r for r in recs if r.get("record") == "null-forwarder-summary"]
    if len(summ) != 3:
        fail(f"{path}: {len(summ)} null-forwarder-summary records, expected 3")
    for r in summ:
        try:
            s = json.loads(r["bmnull"])
            if s.get("tool") != "bmnull":
                raise ValueError("no tool key")
        except (ValueError, TypeError) as e:
            fail(f"{path}: rep {r['rep']}: bmnull summary unreadable ({e}): {r['bmnull']!r}")


def check_controls(path, priv):
    recs = load(path)
    check_start(path, recs, priv)
    c = [r for r in recs if r.get("record") == "g4-control"]
    if [r["condition"] for r in c] != ["none", "sibling-busy-loop", "short-lived-stream", "none-again"]:
        fail(f"{path}: conditions {[r['condition'] for r in c]}")
    for r in c:
        if r["condition"] in ("sibling-busy-loop", "short-lived-stream") and "G4" not in r["flags"]:
            fail(f"{path}: positive control {r['condition']} did not raise G4 "
                 f"(foreign {r['foreign_cpu_fraction']}, idle {r['idle_cpu_busy']})")
        if BAD_FLAGS & set(r["flags"]):
            fail(f"{path}: control {r['condition']}: flags {r['flags']}")


def main():
    cmd, a = sys.argv[1], sys.argv[2:]
    if cmd == "bench":
        opts, cur = {"--expect-error": [], "--expect-probe-fail": [], "--expect-hitcheck-fail": []}, None
        for x in a[4:]:
            if x in opts:
                cur = opts[x]
            elif cur is None:
                sys.exit(__doc__)
            else:
                cur.append(x)
        check_bench(a[0], a[1], a[2], a[3], [tuple(x.split(":", 1)) for x in opts["--expect-error"]],
                    opts["--expect-probe-fail"], opts["--expect-hitcheck-fail"])
    elif cmd == "calibrate":
        check_calibrate(a[0], a[1])
    elif cmd == "null":
        check_null(a[0], a[1], a[2])
    elif cmd == "controls":
        check_controls(a[0], a[1])
    elif cmd == "caps":
        caps = json.load(open(a[0]))
        for k in a[1:]:
            if k not in caps:
                fail(f"{a[0]}: no capacity for {k} (have {sorted(caps)})")
    else:
        sys.exit(__doc__)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
