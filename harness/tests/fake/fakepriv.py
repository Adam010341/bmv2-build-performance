#!/usr/bin/env python3
"""fakepriv.py -- the state-changing subcommands of the fake bmbench-priv (topo-up, topo-down,
cpu-mode, perf-paranoid) and `ip -br link` for its selftest. Output and exit codes follow
harness/priv/bmbench-priv; nothing on the host is changed.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakenet  # noqa: E402

STATE = "/run/bmbench-cpu-state"      # the real wrapper's path, only quoted in messages


def die(msg):
    sys.stderr.write(f"bmbench-priv: {msg}\n")
    sys.exit(2)


def host_cpus():
    return sorted((os.path.basename(c) for c in glob.glob("/sys/devices/system/cpu/cpu[0-9]*")),
                  key=lambda c: int(c[3:]))


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "topo-up":
        n = args[0] if args else ""
        if not (len(n) == 1 and n in "2345678"):
            die("topo-up wants NPORTS in 2..8")
        n = int(n)
        with fakenet.txn() as s:
            if s["netns"]:
                die("bmb-* netns already exist; run topo-down first")
            s["netns"]["bmb-sw"] = {"lo": fakenet.new_ctr(), **{f"sw{k}": fakenet.new_ctr() for k in range(n)}}
            for k in range(n):
                s["netns"][f"bmb-h{k}"] = {"lo": fakenet.new_ctr(), f"h{k}": fakenet.new_ctr()}
        print(f"topo-up: sw0..sw{n - 1} in bmb-sw <-> h0..h{n - 1} in bmb-h0..bmb-h{n - 1}")
    elif cmd == "topo-down":
        with fakenet.txn() as s:
            names = sorted(s["netns"])                 # `ls /run/netns` order
            s["netns"] = {}
            s["socks"] = {}
        for ns in names:
            print(f"deleted {ns}")
    elif cmd == "cpu-mode":
        mode = args[0] if args else ""
        with fakenet.txn() as s:
            if mode == "show":
                cpus = host_cpus()
                if s.get("cpu"):
                    govs = {"performance": len(cpus)}
                    nt = "1"
                else:
                    govs = {}
                    for c in cpus:
                        g = read(f"/sys/devices/system/cpu/{c}/cpufreq/scaling_governor")
                        govs[g] = govs.get(g, 0) + 1
                    nt = read("/sys/devices/system/cpu/intel_pstate/no_turbo", "n/a")
                for g in sorted(govs):
                    print(f"governor: {govs[g]:7d} {g}")
                print(f"no_turbo: {nt}")
            elif mode == "bench":
                if not s.get("cpu"):
                    s["cpu"] = {"saved": True}
                print(f"cpu-mode bench: governor=performance, turbo off (previous state in {STATE})")
            elif mode == "restore":
                if not s.get("cpu"):
                    die(f"no saved state in {STATE}")
                s["cpu"] = None
                print("cpu-mode restored")
            else:
                die("cpu-mode bench|restore|show")
    elif cmd == "perf-paranoid":
        v = args[0] if args else ""
        if v not in ("2", "4"):
            die("perf-paranoid 2|4")
        print(f"kernel.perf_event_paranoid={v}")
    elif cmd == "ip-br-link":
        ns = os.environ.get("FAKE_NS", "")
        with fakenet.txn(write=False) as s:
            for ifc in s["netns"].get(ns, {}):
                print(f"{ifc:<16} {'UNKNOWN' if ifc == 'lo' else 'UP':<14} "
                      f"{'00:00:00:00:00:00' if ifc == 'lo' else '02:00:00:00:00:00'} <LOOPBACK,UP,LOWER_UP>")
    else:
        die(f"unknown subcommand '{cmd}'")


if __name__ == "__main__":
    main()
