#!/usr/bin/env python3
"""bench.py -- orchestrator for bmbench trials on testbed (stdlib only).

One *instance* = one freshly started switch process (build x program x runtime options), with
4 ports; traffic enters port 0 and leaves port 1, ports 2-3 are the stray-traffic check. Each
instance is checked before it is measured:
  * identity -- sha256 of the binary file at the resolved path, at start and again at stop (the
                kernel hides /proc/<pid>/exe from us: the switch holds capabilities we lack);
  * path     -- a 200-frame probe at 1 kpps arrives exactly (200/200) at h1 and nowhere else;
  * hits     -- optional (hitcheck): on a logging build, bmv2's own log shows every table with
                const entries HIT for probe frames; with a declared path profile (E8,
                AMENDMENT-9) every headline frame must take exactly the declared hits and misses.
An instance may name a program JSON outside the generated set ("program_json") and a file of
simple_switch_CLI commands ("entries_cli") that fill its tables at run time (E8, AMENDMENT-7);
relative paths are relative to the plan file. The entries are loaded through cli_load.sh as soon
as the switch process exists (cli_load waits for the Thrift port) and before the readiness probe,
since a program with empty tables may forward nothing; a failed load fails the instance.
E8 instances (AMENDMENT-9) may also set "gen" (generator addresses and TOS byte, used for every frame
of the instance), "path_profile" (PATH.json for G10) and "own_frames" (readiness and the 200-frame
probe count our frames at the receiver, since a program may send frames of its own to h1).
Every trial writes one JSON line with what is needed to recompute it: counters on all ports
(read after they have stopped moving), generator/receiver reports, per-thread switch CPU,
per-CPU busy time, and foreign CPU by process.

Trial kinds
  sat     offered = generator maximum (pps 0, burst 32); capacity C = sink rate over the steady window
  rate    offered = fixed pps with burst B; loss = 1 - delivered/accepted, exact counts
  zl      zero-loss search: lo must pass, hi must fail, then bisection to 1 %
  iperf3  iperf3 UDP through the switch on a ladder of offered rates (arms.iperf3_ladder)
  iperf3-tcp  one iperf3 TCP stream through the switch (arms.iperf3_tcp, PREREG AMENDMENT-14 (c))
A sat or rate trial with "pairs": 2 (R3, AMENDMENT-11) runs a second generator h2 -> port 2 -> port 3
-> h3 at the same time, same frames; its counts go to the record's "pair2", G2 and GF apply to it too.

Gates evaluated here, at run time, so a failure can be rerun once (PREREG v1 + AMENDMENT-1):
  G4  foreign load: CPU of processes outside this orchestrator's process tree (kernel threads
      excluded) > 5 % of one CPU, or any CPU outside the harness's own > 5 % busy
  GF  generator fidelity: sat -> accepted >= 3 x C ("generator-bounded" otherwise, reported, not
      rerun); paced -> no catch-up burst and <= 1 % late bursts
Usage (on testbed):  bench.py PLAN.json OUT.jsonl
"""
import hashlib
import ipaddress
import json
import os
import random
import re
import select
import shlex
import subprocess
import sys
import threading
import time

# Test hooks (harness/tests/test_dryrun.sh points these at test doubles). Unset = the real lab.
# Every run-start record logs the values in use, so a dry run cannot pass for a real one.
BMB = os.environ.get("BMBENCH_ROOT") or os.path.expanduser("~/bmbench")
PRIV = shlex.split(os.environ.get("BMBENCH_PRIV", "")) or ["sudo", "-n", "/usr/local/sbin/bmbench-priv"]
TOOLS = os.environ.get("BMBENCH_TOOLS") or os.path.join(BMB, "harness", "bin")
P4DIR = os.path.join(BMB, "p4")
CLI_LOAD = os.environ.get("BMBENCH_CLI_LOAD") or os.path.join(BMB, "harness", "run", "cli_load.sh")
BMV2_SRC = os.path.join(BMB, "src", "behavioral-model")
THRIFT_PORT = "9090"
SW_CPUS = "4,6,8,10"     # four physical cores for the switch
GEN_CPU = "2"
RECV_CPU = "1"           # sibling of the orchestrator's CPU 0
GEN2_CPU = RECV_CPU      # R3's second generator (AMENDMENT-11): its trials run no receiver
HOUSE_CPU = "0"
IDLE_CPUS = ("3", "5", "7", "9", "11")    # SMT siblings of the generator and switch cores: must stay idle
NPORTS = 4
FOREIGN_MAX = 0.05
IDLE_BUSY_MAX = 0.05
_run_id = [0]


# ----------------------------------------------------------------------------------- helpers
def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def next_run_id():
    _run_id[0] = _run_id[0] % 65535 + 1
    return _run_id[0]


def netdev(ns):
    r = sh(PRIV + ["netdev", ns])
    if r.returncode != 0:
        raise RuntimeError(f"netdev {ns}: {r.stderr.strip()}")
    out = {}
    for line in r.stdout.splitlines()[2:]:
        name, rest = line.split(":", 1)
        f = rest.split()
        out[name.strip()] = {"rx_bytes": int(f[0]), "rx_packets": int(f[1]), "rx_dropped": int(f[3]),
                             "tx_bytes": int(f[8]), "tx_packets": int(f[9]), "tx_dropped": int(f[11])}
    return out


def all_counters():
    c = {"sw": netdev("bmb-sw")}
    for k in range(NPORTS):
        c[f"h{k}"] = netdev(f"bmb-h{k}")[f"h{k}"]
    return c


def drain(max_s=15.0, quiet_s=0.4, pairs=1):
    """Wait until nothing moves on the switch's ingress or the sink (of every pair in use): the run
    is over only then."""
    t0 = time.time()
    last = None
    still_since = time.time()
    while time.time() - t0 < max_s:
        sw = netdev("bmb-sw")
        cur = (sw["sw0"]["rx_packets"], netdev("bmb-h1")["h1"]["rx_packets"])
        if pairs == 2:
            cur += (sw["sw2"]["rx_packets"], netdev("bmb-h3")["h3"]["rx_packets"])
        if cur != last:
            last, still_since = cur, time.time()
        elif time.time() - still_since >= quiet_s:
            return round(time.time() - t0, 3), True
        time.sleep(0.1)
    return round(time.time() - t0, 3), False


def proc_stat(pid, reaped=False):
    """(ppid, utime+stime, comm) or None. reaped=True adds cutime+cstime, the CPU of children this
    process has already waited for (only meaningful for a process, not for one of its threads)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        comm = s[s.index("(") + 1:s.rindex(")")]
        p = s.rsplit(") ", 1)[1].split()
        return int(p[1]), int(p[11]) + int(p[12]) + (int(p[13]) + int(p[14]) if reaped else 0), comm
    except (OSError, ValueError, IndexError):
        return None


def snapshot():
    # reaped=True: a process born and reaped between two snapshots is never seen itself; its CPU
    # shows up only in its parent's cutime/cstime (the G4 short-lived-stream control, AMENDMENT-4).
    procs = {}
    for e in os.listdir("/proc"):
        if e.isdigit():
            st = proc_stat(e, reaped=True)
            if st:
                procs[int(e)] = st
    return procs


def descendants(procs, root):
    kids = {}
    for pid, (ppid, _, _) in procs.items():
        kids.setdefault(ppid, []).append(pid)
    out, todo = set(), [root]
    while todo:
        p = todo.pop()
        out.add(p)
        todo.extend(kids.get(p, []))
    return out


def is_kthread(procs, pid):
    return pid == 2 or procs.get(pid, (0,))[0] == 2


def stat_cpu():
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                v = [int(x) for x in line.split()[1:]]
                busy = v[0] + v[1] + v[2] + v[5] + v[6] + v[7]
                out[line.split()[0][3:]] = (busy, busy + v[3] + v[4], v[6])
    return out


def thread_jiffies(pid):
    out = {}
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            st = proc_stat(f"{pid}/task/{tid}")
            if st:
                out[tid] = st[1]
    except OSError:
        pass
    return out


def freq_sample(cpus):
    out = {}
    for c in cpus.split(","):
        try:
            with open(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq") as f:
                out[c] = int(f.read())
        except OSError:
            out[c] = None
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def build_info(build_id):
    info = {}
    with open(os.path.join(BMB, "builds", build_id, "BUILD-INFO")) as f:
        for line in f:
            if "=" in line and not line.startswith("ldd:"):
                k, v = line.rstrip("\n").split("=", 1)
                info[k] = v
    return info


def host_nics_off():
    """rx/tx checksum and sg offload off on every host veth (bmnic), as the p4 tutorials do: with
    TX checksum offload, kernel-stack traffic (iperf3) crosses bmv2 with a partial checksum and the
    far host drops it (AMENDMENT-4). Returns the read-back state for the run-start record."""
    return {h: run_tool(f"bmb-{h}", HOUSE_CPU, "bmnic", [h], timeout=30) for h in ("h0", "h1", "h2", "h3")}


def run_tool(ns, cpu, tool, args, timeout):
    r = sh(PRIV + ["exec", ns, cpu, os.path.join(TOOLS, tool)] + args, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{tool} failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


GEN_EXTRA = []                                   # the current instance's generator fields (gen_extra)


def gen_extra(inst):
    """bmgen options for an instance's "gen" fields (E8, AMENDMENT-9): dst_ip, src_ip, tos."""
    g = inst.get("gen") or {}
    unknown = set(g) - {"dst_ip", "src_ip", "tos"}
    if unknown:
        raise ValueError(f"unknown generator field(s) {sorted(unknown)}")
    out = []
    for k in ("dst_ip", "src_ip"):
        if k in g:
            try:
                ipaddress.IPv4Address(g[k])
            except ValueError:
                raise ValueError(f"gen.{k} {g[k]!r} is not an IPv4 address") from None
            out += ["--" + k.replace("_", "-"), g[k]]
    if "tos" in g:
        if not isinstance(g["tos"], int) or not 0 <= g["tos"] <= 255:
            raise ValueError(f"gen.tos {g['tos']!r} is not a byte")
        out += ["--tos", str(g["tos"])]
    return out


def gen(frame, pps, burst, duration, flows, run_id, host=0, cpu=GEN_CPU):
    args = ["--if", f"h{host}", "--frame", str(frame), "--pps", str(pps), "--burst", str(burst),
            "--duration", str(duration), "--flows", str(flows), "--run-id", str(run_id),
            "--dst-mac", "02:00:00:00:ff:01"]   # not the sink's MAC: its stack drops the frame at once
    return run_tool(f"bmb-h{host}", cpu, "bmgen", args + GEN_EXTRA, timeout=duration + 30)


def steady_rate(samples, duration):
    """Counter rate over the steady window [1 s, duration - 0.5 s] of (t, count) samples."""
    win = [(t, c) for t, c in samples if c is not None and 1.0 <= t <= duration - 0.5]
    return (win[-1][1] - win[0][1]) / (win[-1][0] - win[0][0]) if len(win) >= 2 else None


LOG_PKT = re.compile(r"\[(\d+)\.(\d+)\] \[cxt \d+\] (.*)$")
LOG_TABLE = re.compile(r"Table '([^']+)': (hit|miss)")


def path_profile_check(lines, profile, added, const_tables, port=0, min_frames=10):
    """G10 with a declared path profile (AMENDMENT-9), from bmv2 log lines. Headline frames are the
    packets bmv2 numbers N.0 that are received on the generator port; clones and recirculated copies
    (N.1, N.2, ...) and frames from other ports are not checked. Each headline frame must hit every
    `hit` table and never miss it, miss every `miss` table and never hit it, and never apply a
    `not_applied` table. Every table given a table_add must be in the profile; a const-entry table
    the profile does not name keeps the E1 rule (hit)."""
    for t, want in profile.items():
        if want not in ("hit", "miss", "not_applied"):
            raise ValueError(f"path profile: {t}: {want!r} is not hit/miss/not_applied")
    expect = {**{t: "hit" for t in const_tables}, **profile}
    frames = {}
    for ln in lines:
        m = LOG_PKT.search(ln)
        if not m or m.group(2) != "0":
            continue
        pid, msg = m.group(1), m.group(3)
        if msg == f"Processing packet received on port {port}":
            frames[pid] = {}
        elif pid in frames:
            t = LOG_TABLE.match(msg)
            if t:
                frames[pid].setdefault(t.group(1), []).append(t.group(2))
    tables = {t: {"want": w, "hit": 0, "miss": 0, "frames_ok": 0} for t, w in expect.items()}
    wrong = []
    for pid, seen in frames.items():
        for t, w in expect.items():
            got = seen.get(t, [])
            tables[t]["hit"] += got.count("hit")
            tables[t]["miss"] += got.count("miss")
            good = (not got) if w == "not_applied" else (bool(got) and all(g == w for g in got))
            if good:
                tables[t]["frames_ok"] += 1
            elif len(wrong) < 10:
                wrong.append({"packet": pid, "table": t, "want": w, "got": got})
    missing = sorted(set(added) - set(profile))
    ok = len(frames) >= min_frames and not missing and all(v["frames_ok"] == len(frames) for v in tables.values())
    return ok, {"frames": len(frames), "tables": tables, "missing_from_profile": missing, "wrong": wrong}


def probe_ok(probe, own_frames):
    """200/200 path probe: by h1's interface counter, or (own_frames, AMENDMENT-9) by the receiver's
    count of our frames, so frames the program sends itself do not fail it; never any stray frame."""
    if any(probe["stray_rx"].values()) or probe["gen"]["accepted"] != 200:
        return False
    if own_frames:
        return bool(probe.get("recv")) and probe["recv"]["rx"] == 200
    return probe["delivered"] == 200


class Receiver:
    """bmrecv in bmb-h1; the constructor returns once it has printed READY."""
    def __init__(self, duration, run_id):
        self.p = subprocess.Popen(PRIV + ["exec", "bmb-h1", RECV_CPU, os.path.join(TOOLS, "bmrecv"),
                                          "--if", "h1", "--duration", str(duration), "--run-id", str(run_id)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        t0 = time.time()
        while time.time() - t0 < 10:
            r, _, _ = select.select([self.p.stderr], [], [], 0.5)
            if r and "READY" in self.p.stderr.readline():
                return
            if self.p.poll() is not None:
                break
        raise RuntimeError("bmrecv did not report READY")

    def result(self, timeout):
        out, _ = self.p.communicate(timeout=timeout)
        return json.loads(out.strip().splitlines()[-1])


# ----------------------------------------------------------------------------------- switch
class Switch:
    def __init__(self, inst, log_dir):
        self.inst = inst
        b = inst["build"]
        if b.startswith(("docker:", "docker-grpc:")):          # binary as shipped in the official image
            kind, d = b.split(":", 1)
            root = os.path.join(BMB, "docker", d, "rootfs")
            self.binary = os.path.join(root, "usr/local/bin",
                                       "simple_switch_grpc" if kind == "docker-grpc" else "simple_switch")
            self.cmd = [os.path.join(root, "lib64/ld-linux-x86-64.so.2"), "--library-path",
                        ":".join(os.path.join(root, x) for x in
                                 # usr/lib last: simple_switch_grpc needs libavl.so.1 from there
                                 # (AMENDMENT-5); the dirs before it resolve as they did
                                 ("usr/local/lib", "usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu",
                                  "usr/lib")),
                        self.binary]
            self.expect_sha = None
        else:
            bi = build_info(b)
            self.binary = bi["binary"]
            self.cmd = [self.binary]
            self.expect_sha = bi["binary_sha256"]
        args = []
        for k in range(NPORTS):
            args += ["-i", f"{k}@sw{k}"]
        args += ["--thrift-port", THRIFT_PORT, "--device-id", "0"]
        # Runtime recording options. "tutorial" = the recording options p4lang/tutorials'
        # run_exercise.py + p4runtime_switch.py start every switch with: --log-console (stdout to a
        # file), --pcap <dir>, --nanolog ipc://... (the tutorials run simple_switch_grpc; the
        # docker-grpc build reproduces that target). "logoff" = -L off and no sink (AMENDMENT-12): the
        # default level is trace, so compiled-in macros format every record unless the level is lowered.
        rt = "console" if inst.get("hitcheck") else inst.get("runtime", "none")
        if rt not in ("none", "console", "pcap", "nanolog", "tutorial", "logoff"):
            raise ValueError(f"unknown runtime option {rt}")
        self.scratch = os.path.join(log_dir, f"{inst['id']}.scratch")
        os.makedirs(self.scratch, exist_ok=True)
        if rt in ("console", "tutorial"):
            args += ["--log-console"]
        if rt in ("pcap", "tutorial"):
            args += ["--pcap", self.scratch]
        if rt in ("nanolog", "tutorial"):
            self.ipc = f"/tmp/bmb-nanolog-{hashlib.sha1(inst['id'].encode()).hexdigest()[:10]}.ipc"
            if os.path.exists(self.ipc):
                os.remove(self.ipc)
            args += ["--nanolog", f"ipc://{self.ipc}"]
        if rt == "logoff":
            args += ["-L", "off"]
        args += inst.get("extra_args", [])
        self.program_json = inst.get("program_json") or os.path.join(P4DIR, inst["program"] + ".json")
        args.append(self.program_json)
        self.argv = self.cmd + args
        self.log_path = os.path.join(log_dir, f"{inst['id']}.stdout")
        self.proc = self.pid = self.ident = self.bulk = self.log = None
        self.entries = None                           # set by load_entries(): goes into the instance record
        if inst.get("entries_cli") and b.startswith(("docker:", "docker-grpc:")):
            raise ValueError("entries_cli needs a build tree under builds/ (not a docker rootfs)")
        self.entries_log = os.path.join(log_dir, f"{inst['id']}.entries.log")

    def load_entries(self):
        """Fill the program's tables from the instance's simple_switch_CLI commands file, through
        cli_load.sh in the switch's netns (its Thrift server listens on localhost there)."""
        path = self.inst["entries_cli"]
        with open(path) as f:
            n = sum(1 for line in f if line.strip() and not line.lstrip().startswith("#"))
        self.entries = {"entries_cli": path, "entries_sha256": sha256(path), "entries_commands": n,
                        "entries_transcript": self.entries_log}
        r = sh(PRIV + ["exec", "bmb-sw", HOUSE_CPU, CLI_LOAD, os.path.join(BMB, "builds", self.inst["build"]),
                       BMV2_SRC, THRIFT_PORT, path], timeout=120)
        with open(self.entries_log, "w") as f:
            f.write(r.stdout)
            if r.stderr:
                f.write("--- stderr\n" + r.stderr)
        if r.returncode != 0:
            last = (r.stdout.strip().splitlines() or r.stderr.strip().splitlines() or [""])[-1]
            raise RuntimeError(f"entries load failed (cli_load rc {r.returncode}): {last[:300]} "
                               f"[transcript {self.entries_log}]")

    def start(self):
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen(PRIV + ["exec", "bmb-sw", SW_CPUS] + self.argv,
                                     stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + 30
        while time.time() < deadline and self.pid is None:
            time.sleep(0.2)
            if self.proc.poll() is not None:
                self.log.flush()
                with open(self.log_path, errors="replace") as f:
                    tail = f.read()[-300:].strip()
                raise RuntimeError(f"switch wrapper exited (rc {self.proc.returncode}) before the switch appeared: {tail}")
            for pid in descendants(snapshot(), self.proc.pid):
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        argv = f.read().split(b"\0")
                except OSError:
                    continue
                if any(a.endswith((b"simple_switch", b"simple_switch_grpc")) for a in argv[:4]) \
                        and b"--thrift-port" in argv:
                    self.pid = pid
        if self.pid is None:
            raise RuntimeError("switch did not appear under our sudo")
        if self.inst.get("entries_cli"):
            self.load_entries()                      # before the readiness probe: empty tables may drop all
        deadline = time.time() + 30
        while True:                                  # ready = frames actually cross the switch
            if self.proc.poll() is not None:
                raise RuntimeError(f"switch exited during start (rc {self.proc.returncode})")
            if self.inst.get("own_frames"):          # the program may send frames of its own to h1
                rid = next_run_id()
                rcv = Receiver(4, rid)
                g = gen(64, 1000, 1, 0.02, 1, rid)
                drain(max_s=3)
                if rcv.result(timeout=30)["rx"] == g["accepted"] > 0:
                    break
            else:
                c0 = netdev("bmb-h1")["h1"]["rx_packets"]
                g = gen(64, 1000, 1, 0.02, 1, next_run_id())
                drain(max_s=3)
                if netdev("bmb-h1")["h1"]["rx_packets"] - c0 == g["accepted"] > 0:
                    break
            if time.time() > deadline:
                raise RuntimeError("no frame crossed the switch within 30 s of start")
        self.ident = {"pid": self.pid, "argv": self.argv, "binary": self.binary,
                      "binary_sha256_at_start": sha256(self.binary),
                      "program_sha256": sha256(self.program_json)}
        if self.expect_sha and self.ident["binary_sha256_at_start"] != self.expect_sha:
            raise RuntimeError(f"identity mismatch: file {self.ident['binary_sha256_at_start'][:12]} "
                               f"vs BUILD-INFO {self.expect_sha[:12]}")
        return self.ident

    def hitcheck(self):
        """Send 10 frames, then read bmv2's own log: every table with const entries must hit."""
        with open(self.log_path) as f:
            before = len(f.readlines())
        gen(64, 100, 1, 0.1, 1, next_run_id())
        drain(max_s=5)
        time.sleep(0.5)
        with open(self.log_path) as f:
            lines = f.readlines()[before:]
        prog = json.load(open(self.program_json))
        want = [t["name"] for p in prog["pipelines"] for t in p["tables"] if t.get("entries")]
        res = {t: {"hit": sum(1 for l in lines if f"Table '{t}': hit" in l),
                   "miss": sum(1 for l in lines if f"Table '{t}': miss" in l)} for t in want}
        # The log must show the frames at all (else an empty log would pass a program like `min`
        # that has no const-entry table); every const-entry table must then hit every frame.
        seen = sum(1 for l in lines if "Processing packet received on port" in l)
        ok = seen >= 10 and all(v["hit"] >= 10 and v["miss"] == 0 for v in res.values())
        detail = {"frames_in_log": seen, "tables": res}
        if self.inst.get("path_profile"):
            # E8 (AMENDMENT-9): every headline frame must match the declared profile
            with open(self.inst["path_profile"]) as f:
                profile = json.load(f)["tables"]
            added = set()
            if self.entries:
                with open(self.entries["entries_cli"]) as f:
                    added = {c[1] for c in (line.split() for line in f) if len(c) > 1 and c[0] == "table_add"}
            ok, prof = path_profile_check(lines, profile, added, set(want))
            detail.update({"path_profile": self.inst["path_profile"],
                           "path_profile_sha256": sha256(self.inst["path_profile"]), "profile": prof})
            return ok, detail
        if self.entries:
            # Tables filled from entries_cli: AMENDMENT-7 (E8) requires a hit on the path for every
            # frame, so a table that received table_add entries must hit every frame, as const-entry
            # tables do. Tables that only got a default action are reported, not required to hit.
            with open(self.entries["entries_cli"]) as f:
                cmds = [line.split() for line in f if len(line.split()) > 1]
            rt = sorted({c[1] for c in cmds if c[0] in ("table_add", "table_set_default")})
            added = {c[1] for c in cmds if c[0] == "table_add"}
            detail["entries_tables"] = {t: {"hit": sum(1 for l in lines if f"Table '{t}': hit" in l),
                                            "miss": sum(1 for l in lines if f"Table '{t}': miss" in l)}
                                        for t in rt}
            ok = ok and all(v["hit"] >= 10 and v["miss"] == 0
                            for t, v in detail["entries_tables"].items() if t in added)
        return ok, detail

    def stop(self):
        if self.ident:
            self.ident["binary_sha256_at_stop"] = sha256(self.binary)
        if self.pid:
            try:
                os.kill(self.pid, 15)
            except ProcessLookupError:
                pass
            for _ in range(50):
                if not os.path.exists(f"/proc/{self.pid}"):
                    break
                time.sleep(0.1)
            else:
                os.kill(self.pid, 9)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()                # sudo relays TERM to whatever it started
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        if self.log:
            self.log.close()
        # console logs and pcaps can reach GB per instance: keep sizes and a 1 MB head only
        self.bulk = {"stdout_bytes": os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0,
                     "scratch_bytes": sum(os.path.getsize(os.path.join(self.scratch, f)) for f in os.listdir(self.scratch))}
        if self.bulk["stdout_bytes"] > 1 << 20:
            with open(self.log_path, "rb") as f:
                head = f.read(1 << 20)
            with open(self.log_path, "wb") as f:
                f.write(head + b"\n[truncated by bench.py]\n")
        for f in os.listdir(self.scratch):
            os.remove(os.path.join(self.scratch, f))
        if getattr(self, "ipc", None) and os.path.exists(self.ipc):
            os.remove(self.ipc)


# ----------------------------------------------------------------------------------- trials
def foreign_and_idle(p0, p1, s0, s1, seconds, harness_root):
    tree = descendants(p1, harness_root) | descendants(p0, harness_root)
    foreign = []
    for pid, (ppid, j1, comm) in p1.items():
        if pid in tree or is_kthread(p1, pid):
            continue
        j0 = p0[pid][1] if pid in p0 else 0          # a process born during the trial counts from 0
        if j1 - j0 > 0:
            foreign.append((j1 - j0, comm, pid))
    # A process seen at p0 and reaped before p1 adds ALL its CPU, pre-trial part included, to its
    # parent's cutime: take the pre-trial part back out when that parent is counted above.
    counted = {pid for _, _, pid in foreign}
    before = sum(j0 for pid, (ppid, j0, _) in p0.items() if pid not in p1 and ppid in counted)
    foreign.sort(reverse=True)
    foreign_frac = max(0, sum(j for j, _, _ in foreign) - before) / 100.0 / seconds   # USER_HZ = 100
    idle_busy = {c: round((s1[c][0] - s0[c][0]) / max(1, s1[c][1] - s0[c][1]), 4) for c in IDLE_CPUS}
    return foreign_frac, foreign[:8], idle_busy


def trial(sw, kind, frame, pps, burst, duration, flows, with_recv, sample_s=0.5, pairs=1):
    if pairs not in (1, 2) or (pairs == 2 and with_recv):
        raise ValueError(f"pairs={pairs!r}, with_recv={with_recv}: two pairs run without a receiver (AMENDMENT-11)")
    run_id = next_run_id()
    rec = {"kind": kind, "frame": frame, "offered_pps": pps, "burst": burst, "duration": duration,
           "flows": flows, "run_id": run_id}
    if pairs == 2:
        rec["pairs"] = 2
    samples, samples2, stop = [], [], threading.Event()

    def sampler():
        t0 = time.time()
        while not stop.is_set():
            try:
                c = netdev("bmb-h1")["h1"]["rx_packets"]
            except Exception:
                c = None
            samples.append((round(time.time() - t0, 3), c, freq_sample(SW_CPUS)))
            if pairs == 2:
                t2 = round(time.time() - t0, 3)
                try:
                    c2 = netdev("bmb-h3")["h3"]["rx_packets"]
                except Exception:
                    c2 = None
                samples2.append((t2, c2))
            stop.wait(sample_s)

    g2box = {}

    def gen2():
        try:
            g2box["g"] = gen(frame, pps, burst, duration, flows, run_id, host=2, cpu=GEN2_CPU)
        except Exception as ex:
            g2box["error"] = ex

    drain(max_s=5, pairs=pairs)                      # nothing left over from the previous trial
    c0 = all_counters()
    p0, s0, t0j = snapshot(), stat_cpu(), thread_jiffies(sw.pid)
    tp0 = time.time()
    rcv = Receiver(duration + 3, run_id) if with_recv else None
    th = threading.Thread(target=sampler, daemon=True)
    th2 = threading.Thread(target=gen2, daemon=True) if pairs == 2 else None
    tstart = time.time()
    th.start()
    if th2:
        th2.start()
    try:
        g = gen(frame, pps, burst, duration, flows, run_id)
        if th2:
            th2.join(duration + 60)
        drain_s, drained = drain(pairs=pairs)
    finally:
        stop.set()
        th.join()
    if th2 and "g" not in g2box:
        raise g2box.get("error") or RuntimeError("second generator did not finish")
    elapsed = time.time() - tstart
    r = rcv.result(timeout=30) if rcv else None
    c1 = all_counters()
    p1, s1, t1j = snapshot(), stat_cpu(), thread_jiffies(sw.pid)
    window = time.time() - tp0

    d = lambda a, b: b - a
    delivered = d(c0["h1"]["rx_packets"], c1["h1"]["rx_packets"])
    steady = steady_rate([(t, c) for t, c, _ in samples], duration)
    # foreign jiffies are counted between the p0 and p1 snapshots, so they are divided by that
    # window (dividing by `elapsed`, which starts after the receiver is up and ends before its
    # tail, overstated G4 by ~20 % in receiver trials)
    foreign_frac, foreign_top, idle_busy = foreign_and_idle(p0, p1, s0, s1, window, os.getpid())
    flags = []
    if foreign_frac > FOREIGN_MAX or any(v > IDLE_BUSY_MAX for v in idle_busy.values()):
        flags.append("G4")
    if kind == "sat" and steady and g["accepted_pps"] < 3 * steady:
        flags.append("generator-bounded")
    if pps > 0 and (g.get("catchup_bursts", 0) > 0 or g["late_bursts"] > 0.01 * max(1, g["bursts"])):
        flags.append("schedule")
    if not drained:
        flags.append("no-drain")
    if g["accepted"] != d(c0["h0"]["tx_packets"], c1["h0"]["tx_packets"]):
        flags.append("G2:gen-vs-h0tx")
    if r and r.get("ring_drops", 0) > 0:
        flags.append("G3:recv-drops")
    if r and r["rx"] + r["foreign"] + r.get("stale", 0) + r.get("skipped", 0) \
            != r.get("ring_packets", -1) - r.get("ring_drops", 0):
        flags.append("G3:recv-accounting")
    if pairs == 2:
        g2 = g2box["g"]
        steady2 = steady_rate(samples2, duration)
        delivered2 = d(c0["h3"]["rx_packets"], c1["h3"]["rx_packets"])
        if kind == "sat" and steady2 and g2["accepted_pps"] < 3 * steady2 and "generator-bounded" not in flags:
            flags.append("generator-bounded")
        if pps > 0 and (g2.get("catchup_bursts", 0) > 0 or g2["late_bursts"] > 0.01 * max(1, g2["bursts"])) \
                and "schedule" not in flags:
            flags.append("schedule")
        if g2["accepted"] != d(c0["h2"]["tx_packets"], c1["h2"]["tx_packets"]):
            flags.append("G2:gen-vs-h2tx")
        rec["pair2"] = {"gen": g2, "delivered": delivered2,
                        "loss": 1 - delivered2 / g2["accepted"] if g2["accepted"] else None,
                        "switch_rx": d(c0["sw"]["sw2"]["rx_packets"], c1["sw"]["sw2"]["rx_packets"]),
                        "switch_tx": d(c0["sw"]["sw3"]["tx_packets"], c1["sw"]["sw3"]["tx_packets"]),
                        "delivered_pps_steady": steady2, "samples": samples2}
        rec["delivered_pps_steady_total"] = steady + steady2 if steady is not None and steady2 is not None else None
    stray = {f"h{k}": d(c0[f"h{k}"]["rx_packets"], c1[f"h{k}"]["rx_packets"]) for k in range(2, NPORTS)
             if not (pairs == 2 and k == 3)}        # with two pairs h3 is the second sink; h2 must still get nothing
    rec.update({
        "gen": g, "recv": r, "counters_before": c0, "counters_after": c1, "drain_s": drain_s,
        "delivered": delivered, "loss": 1 - delivered / g["accepted"] if g["accepted"] else None,
        "switch_rx": d(c0["sw"]["sw0"]["rx_packets"], c1["sw"]["sw0"]["rx_packets"]),
        "switch_tx": d(c0["sw"]["sw1"]["tx_packets"], c1["sw"]["sw1"]["tx_packets"]),
        "veth_rx_dropped_sw0": d(c0["sw"]["sw0"]["rx_dropped"], c1["sw"]["sw0"]["rx_dropped"]),
        "stray_rx": stray, "delivered_pps_steady": steady, "samples": samples,
        "cpu_busy_per_cpu": {c: round((s1[c][0] - s0[c][0]) / max(1, s1[c][1] - s0[c][1]), 4) for c in s1},
        "softirq_jiffies_per_cpu": {c: s1[c][2] - s0[c][2] for c in s1},
        "cpu_switch_threads": {tid: t1j.get(tid, 0) - t0j.get(tid, 0) for tid in t1j},
        "foreign_cpu_fraction": round(foreign_frac, 4), "g4_window_s": round(window, 3), "foreign_top": foreign_top, "idle_cpu_busy": idle_busy,
        "flags": flags,
    })
    return rec


def gated(fn, log, base):
    """Run a trial; on G4 or schedule failure, record it and rerun once (AMENDMENT-1)."""
    r = fn()
    if any(f in ("G4", "schedule") for f in r["flags"]):
        log({**base, **r, "attempt": 1, "superseded": True})
        r = fn()
        r["attempt"] = 2
    return r


def zero_loss_search(sw, frame, burst, duration, flows, lo, hi, threshold, log, base, resolution=0.01):
    steps = []

    def step(pps, role):
        r = gated(lambda: trial(sw, "zl-step", frame, pps, burst, duration, flows, with_recv=False), log, base)
        ok = r["loss"] is not None and r["loss"] <= threshold
        steps.append({"pps": pps, "loss": r["loss"], "ok": ok, "role": role, "flags": r["flags"]})
        log({**base, **r, "zl_role": role})
        return ok

    bracket = {"lo_passes": step(lo, "lo"), "hi_fails": not step(hi, "hi")}
    valid = bracket["lo_passes"] and bracket["hi_fails"]
    if valid:
        while hi - lo > resolution * hi:
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if step(mid, "bisect") else (lo, mid)
    return (lo if valid else None), bracket, steps


# ----------------------------------------------------------------------------------- main
def ordered_instances(plan):
    """Shuffle within blocks (block = replicate index), blocks in order: every cell appears once
    per block, so drift cannot line up with a factor. Trial order inside an instance is shuffled
    with a seed derived from the plan seed and the instance id."""
    seed = plan.get("shuffle_seed", 0)
    rng = random.Random(seed)
    blocks = {}
    for inst in plan["instances"]:
        blocks.setdefault(inst.get("block", 0), []).append(inst)
    out = []
    for b in sorted(blocks):
        insts = blocks[b][:]
        rng.shuffle(insts)
        out.extend(insts)
    for inst in out:
        if inst.get("shuffle_trials", True):
            random.Random(f"{seed}:{inst['id']}").shuffle(inst["trials"])
    return out


def main():
    plan_path, out_path = sys.argv[1], sys.argv[2]
    plan = json.load(open(plan_path))
    os.sched_setaffinity(0, {int(HOUSE_CPU)})
    log_dir = os.path.splitext(out_path)[0] + ".logs"
    os.makedirs(log_dir, exist_ok=True)
    out = open(out_path, "a", buffering=1)
    log = lambda rec: out.write(json.dumps(rec) + "\n")
    instances = ordered_instances(plan)
    plan_dir = os.path.dirname(os.path.abspath(plan_path))
    for inst in instances:                             # E8 instances: paths relative to the plan file
        for k in ("program_json", "entries_cli", "path_profile"):
            if inst.get(k) and not os.path.isabs(inst[k]):
                inst[k] = os.path.normpath(os.path.join(plan_dir, inst[k]))
    log({"record": "run-start", "plan": plan_path, "plan_sha256": sha256(plan_path),
         "harness_git": open(os.path.join(BMB, "harness", "HARNESS-GIT")).read().strip(),
         "host": os.uname().nodename, "started": time.time(),
         "priv": PRIV, "tools": TOOLS, "bmbench_root": BMB, "cli_load": CLI_LOAD,
         "no_turbo": open("/sys/devices/system/cpu/intel_pstate/no_turbo").read().strip(),
         "governor": open("/sys/devices/system/cpu/cpu4/cpufreq/scaling_governor").read().strip(),
         "host_nics": host_nics_off(), "order": [i["id"] for i in instances]})
    import arms                                        # iperf3 arm; imported late (arms imports bench)
    for inst in instances:
        sw = None
        base = {"record": "trial", "instance": inst["id"], "build": inst["build"], "program": inst["program"],
                "runtime": "console" if inst.get("hitcheck") else inst.get("runtime", "none"),
                "block": inst.get("block", 0)}
        try:
            GEN_EXTRA[:] = gen_extra(inst)             # before the switch starts: readiness uses them too
            sw = Switch(inst, log_dir)                 # reads BUILD-INFO: a missing build costs this instance only
            ident = sw.start()
            own = bool(inst.get("own_frames"))
            probe = trial(sw, "probe", 64, 1000, 1, 0.2, 1, with_recv=own)
            ok = probe_ok(probe, own)
            irec = {**base, "record": "instance", "ident": ident, "probe_ok": ok,
                    "probe": {k: probe[k] for k in ("delivered", "switch_rx", "switch_tx", "stray_rx", "flags", "recv")}}
            if GEN_EXTRA:
                irec["gen_extra"] = list(GEN_EXTRA)
            if inst.get("program_json"):
                irec["program_json"] = inst["program_json"]
            if sw.entries:
                irec.update(sw.entries)
            if inst.get("hitcheck"):
                irec["hitcheck_ok"], irec["hitcheck"] = sw.hitcheck()
            log(irec)
            if not ok:
                print(f"{inst['id']}: path probe FAILED -- instance skipped", flush=True)
                continue
            last_sat = {}
            for t in inst["trials"]:
                k = t["kind"]
                if k == "zl":
                    best, bracket, steps = zero_loss_search(sw, t["frame"], t.get("burst", 1), t.get("duration", 10),
                                                            t.get("flows", 1), t["lo"], t["hi"],
                                                            t.get("threshold", 0.0), log, base)
                    log({**base, "kind": "zl-result", "frame": t["frame"], "burst": t.get("burst", 1),
                         "threshold": t.get("threshold", 0.0), "zero_loss_pps": best, "bracket": bracket,
                         "steps": steps})
                    print(f"{inst['id']} zl f{t['frame']} b{t.get('burst', 1)}: {best} {bracket}", flush=True)
                elif k == "iperf3":
                    for rung in arms.iperf3_ladder(sw, t["frame"], t.get("flows", 1), t["ladder_pps"],
                                                   duration=t.get("duration", 10)):
                        log({**base, **rung})
                elif k == "iperf3-tcp":
                    r = gated(lambda: arms.iperf3_tcp(sw, duration=t.get("duration", 10)), log, base)
                    log({**base, **r})
                elif k in ("sat", "rate"):
                    pps = t.get("pps", 0)
                    if "pps_frac_of_sat" in t:       # E5: offered rate relative to THIS instance's own C
                        if not last_sat.get(t.get("flows", 1)):
                            raise RuntimeError("pps_frac_of_sat before any sat trial in this instance")
                        pps = round(t["pps_frac_of_sat"] * last_sat[t.get("flows", 1)])
                    r = gated(lambda: trial(sw, k, t["frame"], pps, t.get("burst", 32),
                                            t.get("duration", 10), t.get("flows", 1),
                                            with_recv=t.get("recv", False), pairs=t.get("pairs", 1)), log, base)
                    if "pps_frac_of_sat" in t:
                        r["pps_frac_of_sat"] = t["pps_frac_of_sat"]
                    if k == "sat" and r["delivered_pps_steady"] and t.get("pairs", 1) == 1:
                        last_sat[t.get("flows", 1)] = r["delivered_pps_steady"]
                    log({**base, **r})
                    p2 = (f" + pair2 {r['pair2']['delivered_pps_steady'] and round(r['pair2']['delivered_pps_steady'])}"
                          if "pair2" in r else "")
                    print(f"{inst['id']} {k} f{t['frame']} b{t.get('burst', 32)} n{t.get('flows', 1)}: "
                          f"steady {r['delivered_pps_steady'] and round(r['delivered_pps_steady'])}{p2} pps, "
                          f"loss {r['loss'] and round(r['loss'], 4)} {r['flags'] or ''}", flush=True)
                else:
                    raise ValueError(f"unknown trial kind {k!r}")
        except Exception as ex:
            log({**base, "record": "error", "error": repr(ex)})
            print(f"{inst['id']}: ERROR {ex!r}", flush=True)
        finally:
            GEN_EXTRA[:] = []
            if sw:
                sw.stop()
            if sw and sw.ident:
                log({**base, "record": "instance-end", "bulk_output": sw.bulk,
                     "binary_unchanged": sw.ident.get("binary_sha256_at_stop") == sw.ident["binary_sha256_at_start"],
                     "binary_sha256_at_stop": sw.ident.get("binary_sha256_at_stop")})
            time.sleep(1)
    log({"record": "run-end", "finished": time.time()})


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
