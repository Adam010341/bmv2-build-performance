#!/usr/bin/env python3
"""G4 must see CPU of foreign processes that are born and reaped between the two snapshots, and must
not charge a process's pre-trial CPU to the trial when it is reaped during it (AMENDMENT-4).

Runs on any Linux box, no privileges: only the test's own detached process trees are looked at,
so the machine's other load does not matter.
Usage: python3 tests/test_g4_reaped.py [DIR_WITH_bench.py]   (default: ../run)
"""
import os
import signal
import subprocess
import sys
import tempfile
import time

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "run"))
import bench  # noqa: E402

IDLE = {c: (0, 1) for c in bench.IDLE_CPUS}


def detached(script):
    """Start `script` in its own session (outside this process tree); return its leader pid."""
    pf = tempfile.mktemp()
    subprocess.run(["setsid", "-f", "sh", "-c", "echo $$ > %s; %s" % (pf, script)], check=True)
    for _ in range(100):
        if os.path.exists(pf) and open(pf).read().strip():
            break
        time.sleep(0.01)
    pid = int(open(pf).read())
    os.remove(pf)
    return pid


def foreign(leader, p0, p1, seconds):
    """foreign_and_idle restricted to `leader` and its descendants (at either snapshot)."""
    keep = {leader} | bench.descendants(p0, leader) | bench.descendants(p1, leader)
    q0 = {k: v for k, v in p0.items() if k in keep}
    q1 = {k: v for k, v in p1.items() if k in keep}
    return bench.foreign_and_idle(q0, q1, IDLE, IDLE, seconds, -1)[0]


fails = 0

# 1. the positive control: a stream of 50 ms busy processes, none alive long enough to be sampled
lead = detached("while :; do timeout 0.05 sh -c 'while :; do :; done'; done")
time.sleep(0.5)
p0, t0 = bench.snapshot(), time.time()
time.sleep(3)
p1, t1 = bench.snapshot(), time.time()
os.killpg(lead, signal.SIGTERM)
f = foreign(lead, p0, p1, t1 - t0)
ok = f > 0.5
fails += not ok
print(f"{'PASS' if ok else 'FAIL'} short-lived stream seen: foreign {f:.2f} CPU (want > 0.5)")

# 2. a child that burned 2 s of CPU BEFORE p0 and is reaped during the window adds nothing
lead = detached("(timeout 2 sh -c 'while :; do :; done'; sleep 2.5); sleep 30")
time.sleep(2.6)                       # the burn is over; the child sleeps until ~t=4.5
p0, t0 = bench.snapshot(), time.time()
time.sleep(3)                         # the child exits and is reaped inside this window
p1, t1 = bench.snapshot(), time.time()
os.killpg(lead, signal.SIGTERM)
f = foreign(lead, p0, p1, t1 - t0)
ok = f < 0.05
fails += not ok
print(f"{'PASS' if ok else 'FAIL'} pre-trial CPU of a child reaped in the window not charged: "
      f"foreign {f:.2f} CPU (want < 0.05)")

print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
