#!/usr/bin/env python3
"""arms.py -- the pieces of PREREG v1 (+ AMENDMENT-1) that are not plain switch trials.

  calibrate OUT.jsonl        G1: generator ceiling per frame size and burst (B=32 and B=1) with no
                             switch running (sw0 up, nothing reads it), 3 x 10 s each
  null OUT.jsonl             G9: harness ceiling -- bmnull, a single-threaded libpcap forwarder
                             opening the ports exactly as bmv2 does, on the switch CPUs; sat trials
                             at every frame size
  controls BUILD OUT.jsonl   G4 positive controls on one switch instance: (a) a busy loop on CPU 5,
                             the SMT sibling of the first switch core; (b) a stream of short-lived
                             processes; both must raise the G4 flag. Plus the same trial without.
  (iperf3_ladder is called by bench.py for trials of kind "iperf3", iperf3_tcp for "iperf3-tcp")

iperf3 arm: client(s) in bmb-h0, server(s) in bmb-h1, so the traffic crosses the switch;
`-l` = frame - 46 (42 B of headers + 4 B FCS), `-b` = pps x payload x 8, default pacing (iperf3's
timer is part of what is being measured). Each rung records iperf3's own report AND the counters.
"""
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402

IPERF3 = "/usr/bin/iperf3"
FRAMES = (64, 128, 256, 512, 1024, 1280, 1518)


def _open(path):
    os.sched_setaffinity(0, {int(bench.HOUSE_CPU)})
    f = open(path, "a", buffering=1)
    # which wrapper/tools/root this ran against, so a dry run (tests/test_dryrun.sh) cannot pass for a real one
    f.write(json.dumps({"record": "run-start", "arm": sys.argv[1] if len(sys.argv) > 1 else None,
                        "priv": bench.PRIV, "tools": bench.TOOLS, "bmbench_root": bench.BMB,
                        "host": os.uname().nodename, "started": time.time(),
                        "host_nics": bench.host_nics_off()}) + "\n")
    return f


def calibrate(out_path, reps=3):
    with _open(out_path) as out:
        for f in FRAMES:
            for burst in (32, 1):
                for r in range(reps):
                    bench.drain(max_s=5)
                    c0 = bench.all_counters()
                    g = bench.gen(f, 0, burst, 10, 1, bench.next_run_id())
                    bench.drain(max_s=5)
                    c1 = bench.all_counters()
                    rec = {"record": "calibration", "frame": f, "burst": burst, "rep": r, "gen": g,
                           "h0_tx": c1["h0"]["tx_packets"] - c0["h0"]["tx_packets"],
                           "sw0_rx": c1["sw"]["sw0"]["rx_packets"] - c0["sw"]["sw0"]["rx_packets"],
                           "sw0_rx_dropped": c1["sw"]["sw0"]["rx_dropped"] - c0["sw"]["sw0"]["rx_dropped"]}
                    out.write(json.dumps(rec) + "\n")
                    print(f"calibration f{f} b{burst} r{r}: accepted {g['accepted_pps']:.0f} pps, "
                          f"errors {g['send_errors']}", flush=True)


def null_forwarder(out_path, reps=3):
    """bmnull on the switch CPUs, sat trials through it; its rate is the ceiling of the I/O path."""
    with _open(out_path) as out:
        for r in range(reps):
            p = subprocess.Popen(bench.PRIV + ["exec", "bmb-sw", bench.SW_CPUS, os.path.join(bench.TOOLS, "bmnull"),
                                               "--in", "sw0", "--out", "sw1", "--duration", str(len(FRAMES) * 20 + 30)],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            time.sleep(1.5)
            # trial() only needs .pid, for the forwarder's per-thread CPU: that is bmnull's pid, not
            # p.pid (sudo, the wrapper's parent, whose threads do no forwarding)
            fake = type("S", (), {"pid": _tool_pid(p.pid, b"bmnull")})()
            if fake.pid is None or p.poll() is not None:
                # without a forwarder G9 would measure an empty path and report a ceiling of ~0
                raise RuntimeError(f"null r{r}: bmnull did not start under pid {p.pid} (rc {p.poll()})")
            for f in FRAMES:
                rec = bench.trial(fake, "sat", f, 0, 32, 10, 1, with_recv=False)
                out.write(json.dumps({"record": "null-forwarder", "rep": r, **rec}) + "\n")
                print(f"null r{r} f{f}: {rec['delivered_pps_steady']:.0f} pps {rec['flags']}", flush=True)
            p.terminate()
            try:
                summary = p.communicate(timeout=30)[0]
            except subprocess.TimeoutExpired:
                p.kill()
                summary = p.communicate()[0]
            out.write(json.dumps({"record": "null-forwarder-summary", "rep": r, "bmnull": summary.strip()}) + "\n")


def controls(build, out_path):
    log_dir = os.path.splitext(out_path)[0] + ".logs"
    os.makedirs(log_dir, exist_ok=True)
    with _open(out_path) as out:
        sw = bench.Switch({"id": f"controls-{build}", "build": build, "program": "l3"}, log_dir)
        try:
            sw.start()
            for label in ("none", "sibling-busy-loop", "short-lived-stream", "none-again"):
                # The loads must NOT be descendants of this orchestrator, or G4 would (rightly)
                # treat them as harness work: `setsid -f` detaches them; the shell writes its pid
                # (= its process group) so it can be stopped afterwards.
                pidfile = os.path.join(log_dir, "control-load.pid")
                if os.path.exists(pidfile):
                    os.remove(pidfile)
                script = {"sibling-busy-loop": "echo $$ > %s; exec taskset -c 5 sh -c 'while :; do :; done'",
                          "short-lived-stream": "echo $$ > %s; while :; do timeout 0.05 sh -c 'while :; do :; done'; done"}
                if label in script:
                    subprocess.run(["setsid", "-f", "sh", "-c", script[label] % pidfile], check=True)
                    time.sleep(1)
                try:
                    r = bench.trial(sw, "sat", 64, 0, 32, 10, 1, with_recv=False)
                finally:
                    if os.path.exists(pidfile):
                        os.killpg(int(open(pidfile).read()), 15)
                out.write(json.dumps({"record": "g4-control", "condition": label, "build": build, **r}) + "\n")
                print(f"control {label}: C {r['delivered_pps_steady']:.0f} pps flags {r['flags']} "
                      f"foreign {r['foreign_cpu_fraction']} idle {r['idle_cpu_busy']}", flush=True)
        finally:
            sw.stop()


def iperf3_ladder(sw, frame, flows, ladder_pps, duration=10, base_port=5201):
    """One iperf3 measurement per rung; returns a list of per-rung records."""
    payload = frame - 46
    out = []
    for pps in ladder_pps:
        bps = int(pps * payload * 8)
        ports = [base_port + i for i in range(flows)]
        bench.drain(max_s=5)
        servers = [subprocess.Popen(bench.PRIV + ["exec", "bmb-h1", bench.RECV_CPU, IPERF3, "-s", "-1",
                                                  "-p", str(p), "--json"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                   for p in ports]
        clients = []
        try:
            time.sleep(1.0)
            c0 = bench.all_counters()
            clients = [subprocess.Popen(bench.PRIV + ["exec", "bmb-h0", bench.GEN_CPU, IPERF3, "-c", "10.0.0.2",
                                                      "-u", "-b", str(bps), "-l", str(payload), "-t", str(duration),
                                                      "-p", str(p), "--json"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                       for p in ports]
            cj = [_json(c.communicate(timeout=duration + 30)[0]) for c in clients]
            drain_s, drained = bench.drain()
            c1 = bench.all_counters()
            sj = []
            for s in servers:
                try:
                    sj.append(_json(s.communicate(timeout=15)[0]))
                except subprocess.TimeoutExpired:
                    sj.append(None)
        finally:
            _stop(clients + servers)
        d = lambda a, b: b - a
        per_flow = []
        for c in cj:
            try:
                e = c["end"]
                per_flow.append({"sent_packets": e["sum_sent"]["packets"],
                                 "lost_packets": e["sum_received"]["lost_packets"],
                                 "lost_percent": e["sum_received"]["lost_percent"],
                                 "received_bps": e["sum_received"]["bits_per_second"],
                                 "sent_bps": e["sum_sent"]["bits_per_second"]})
            except (KeyError, TypeError):
                per_flow.append(None)          # no measurement is not a low-loss reading
        complete = all(per_flow)
        out.append({"kind": "iperf3-rung", "frame": frame, "payload": payload, "flows": flows,
                    "offered_pps": pps, "offered_bps_per_flow": bps, "duration": duration,
                    "iperf3_per_flow": per_flow,
                    "iperf3_worst_flow_loss_pct": max(p["lost_percent"] for p in per_flow) if complete else None,
                    "iperf3_aggregate_loss_pct": (100.0 * sum(p["lost_packets"] for p in per_flow)
                                                  / max(1, sum(p["sent_packets"] for p in per_flow))) if complete else None,
                    "h0_tx": d(c0["h0"]["tx_packets"], c1["h0"]["tx_packets"]),
                    "switch_rx": d(c0["sw"]["sw0"]["rx_packets"], c1["sw"]["sw0"]["rx_packets"]),
                    "switch_tx": d(c0["sw"]["sw1"]["tx_packets"], c1["sw"]["sw1"]["tx_packets"]),
                    "delivered": d(c0["h1"]["rx_packets"], c1["h1"]["rx_packets"]),
                    "drain_s": drain_s, "drained": drained, "server_ok": [bool(s) for s in sj]})
        print(f"  iperf3 f{frame} n{flows} {pps} pps: worst-flow loss {out[-1]['iperf3_worst_flow_loss_pct']}",
              flush=True)
    return out


def tcp_result(cj):
    """The numbers of one iperf3 TCP client report (--json), or None if there is no measurement."""
    try:
        e = cj["end"]
        return {"iperf3_received_bps": e["sum_received"]["bits_per_second"],
                "iperf3_sent_bps": e["sum_sent"]["bits_per_second"],
                "iperf3_received_bytes": e["sum_received"]["bytes"],
                "retransmits": e["sum_sent"].get("retransmits")}
    except (KeyError, TypeError):
        return None


def iperf3_tcp(sw, duration=10, port=5201, sample_s=0.5):
    """PREREG AMENDMENT-14 (c): one iperf3 TCP stream h0 -> h1 through the switch, iperf3's defaults
    and -t DURATION. Client on the generator CPU, server on the receiver CPU. G4 (foreign load) and
    the switch CPUs' frequency samples (G8) are taken as in bench.trial; the capacity value is the
    receiver's goodput."""
    bench.drain(max_s=5)
    server = subprocess.Popen(bench.PRIV + ["exec", "bmb-h1", bench.RECV_CPU, IPERF3, "-s", "-1", "-p", str(port),
                                            "--json"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    client, samples, stop = None, [], threading.Event()

    def sampler():
        t0 = time.time()
        while not stop.is_set():
            samples.append((round(time.time() - t0, 3), None, bench.freq_sample(bench.SW_CPUS)))
            stop.wait(sample_s)
    th = threading.Thread(target=sampler, daemon=True)
    try:
        time.sleep(1.0)
        c0 = bench.all_counters()
        p0, s0 = bench.snapshot(), bench.stat_cpu()
        tp0 = time.time()
        th.start()
        client = subprocess.Popen(bench.PRIV + ["exec", "bmb-h0", bench.GEN_CPU, IPERF3, "-c", "10.0.0.2", "-t",
                                                str(duration), "-p", str(port), "--json"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        cj = _json(client.communicate(timeout=duration + 30)[0])
        drain_s, drained = bench.drain()
        stop.set()
        th.join()
        c1 = bench.all_counters()
        p1, s1 = bench.snapshot(), bench.stat_cpu()
        window = time.time() - tp0
        try:
            server_ok = bool(_json(server.communicate(timeout=15)[0]))
        except subprocess.TimeoutExpired:
            server_ok = False
    finally:
        stop.set()
        _stop([p for p in (client, server) if p])
    foreign_frac, foreign_top, idle_busy = bench.foreign_and_idle(p0, p1, s0, s1, window, os.getpid())
    flags = []
    if foreign_frac > bench.FOREIGN_MAX or any(v > bench.IDLE_BUSY_MAX for v in idle_busy.values()):
        flags.append("G4")
    if not drained:
        flags.append("no-drain")
    res = tcp_result(cj)
    d = lambda a, b: b - a
    rec = {"kind": "iperf3-tcp", "frame": 1514, "flows": 1, "duration": duration, "port": port,
           **(res or {"iperf3_received_bps": None}), "iperf3_ok": res is not None, "server_ok": server_ok,
           "h0_tx": d(c0["h0"]["tx_packets"], c1["h0"]["tx_packets"]),
           "h1_tx": d(c0["h1"]["tx_packets"], c1["h1"]["tx_packets"]),
           "switch_rx": d(c0["sw"]["sw0"]["rx_packets"], c1["sw"]["sw0"]["rx_packets"]),
           "switch_tx": d(c0["sw"]["sw1"]["tx_packets"], c1["sw"]["sw1"]["tx_packets"]),
           "delivered": d(c0["h1"]["rx_packets"], c1["h1"]["rx_packets"]), "drain_s": drain_s,
           "samples": samples,
           "cpu_busy_per_cpu": {c: round((s1[c][0] - s0[c][0]) / max(1, s1[c][1] - s0[c][1]), 4) for c in s1},
           "foreign_cpu_fraction": round(foreign_frac, 4), "g4_window_s": round(window, 3),
           "foreign_top": foreign_top, "idle_cpu_busy": idle_busy, "flags": flags}
    print(f"  iperf3 tcp: {res and round(res['iperf3_received_bps'] / 1e6, 2)} Mbit/s, "
          f"retransmits {res and res['retransmits']} {flags or ''}", flush=True)
    return rec


def _tool_pid(root, name, timeout=10.0):
    """pid of the process below `root` whose argv[:4] names `name` (the same test bench.Switch
    uses: the wrapper's exec chain keeps the pid, and the tool's path is argv[0], or argv[1] when
    it is a script)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for pid in sorted(bench.descendants(bench.snapshot(), root)):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().split(b"\0")
            except OSError:
                continue
            if any(a == name or a.endswith(b"/" + name) for a in argv[:4]) and b"--in" in argv:
                return pid
        time.sleep(0.2)
    return None


def _stop(procs):
    """Stop tools still running under the wrapper. SIGTERM goes to sudo, which relays it to the
    tool; SIGKILL would kill only sudo and leave the tool running (an iperf3 -s -1 that never got
    its client would then hold its port for every later rung and instance)."""
    running = [p for p in procs if p.poll() is None]
    for p in running:
        p.terminate()
    for p in running:
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()


def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "calibrate":
        calibrate(sys.argv[2])
    elif cmd == "null":
        null_forwarder(sys.argv[2])
    elif cmd == "controls":
        controls(sys.argv[2], sys.argv[3])
    else:
        sys.exit("usage: arms.py calibrate OUT.jsonl | null OUT.jsonl | controls BUILD OUT.jsonl")
