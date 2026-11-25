#!/usr/bin/env python3
"""E8 gates as AMENDMENT-9 defines them, on synthetic bmv2 logs in the real log-line format.

- G10 checks a declared path profile per headline frame: bmv2 numbers every packet "N.k"; a frame
  from a port is N.0, clones and recirculated copies are N.1, N.2, ... (seen in a real simple_switch
  log of C0272: "[0.2] ... Processing packet received on port 0"), so only N.0 frames received on
  the generator port count, and only their own lines.
- The generator gets per-program addresses and the TOS byte from the plan.
- An own-frames probe passes on the receiver's count of our frames, not on h1's interface counter.
No privileges, no switch. Usage: python3 tests/test_e8_gates.py [DIR_WITH_bench.py]  (default ../run)
"""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "run"))
import bench  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    if got == want:
        print(f"  ok   {name} = {got!r}")
    else:
        print(f"  FAIL {name}: got {got!r} want {want!r}")
        fails += 1


def line(pid, msg, level="D"):
    return f"[19:35:54.033] [bmv2] [{level}] [thread 534690] [{pid}] [cxt 0] {msg}\n"


def frame(n, tables, port=0, copy=0):
    """Log lines of one packet: `tables` = [(name, 'hit'|'miss'), ...] in pipeline order."""
    pid = f"{n}.{copy}"
    out = [line(pid, f"Processing packet received on port {port}")]
    for t, hm in tables:
        out.append(line(pid, f"Applying table '{t}'", "T"))
        out.append(line(pid, f"Table '{t}': hit with handle 4" if hm == "hit" else f"Table '{t}': miss"))
    return out


# C0303-like: three tables hit, the deny list misses by design, the knock table is never applied
PROFILE = {"ingress.ipv4_lpm": "hit", "ingress.ticket_deny_table": "miss", "ingress.forward": "hit",
           "egress.send_frame": "hit", "ingress.host_need_processing_0.update_state_table": "not_applied"}
ADDED = {"ingress.ipv4_lpm", "ingress.ticket_deny_table", "ingress.forward", "egress.send_frame",
         "ingress.host_need_processing_0.update_state_table"}
PATH = [("ingress.ipv4_lpm", "hit"), ("ingress.ticket_deny_table", "miss"), ("ingress.forward", "hit"),
        ("egress.send_frame", "hit")]


def run(lines, profile=PROFILE, added=ADDED, const=frozenset()):
    ok, detail = bench.path_profile_check(lines, profile, set(added), set(const))
    return ok, detail


good = [l for n in range(10) for l in frame(n, PATH)]
print("declared profile, 10 frames on the path")
ok, d = run(good)
check("ok", ok, True)
check("frames", d["frames"], 10)
check("deny table misses counted", d["tables"]["ingress.ticket_deny_table"]["miss"], 10)

print("deny table hits one frame (wrong path)")
bad = list(good) + frame(10, [("ingress.ipv4_lpm", "hit"), ("ingress.ticket_deny_table", "hit"),
                              ("ingress.forward", "hit"), ("egress.send_frame", "hit")])
check("ok", run(bad)[0], False)

print("a hit table misses one frame")
bad = list(good) + frame(10, [("ingress.ipv4_lpm", "miss"), ("ingress.ticket_deny_table", "miss")])
check("ok", run(bad)[0], False)

print("a not_applied table is applied to one frame")
bad = list(good) + frame(10, PATH + [("ingress.host_need_processing_0.update_state_table", "hit")])
check("ok", run(bad)[0], False)

print("a table applied twice to one frame, once hit and once miss")
bad = list(good) + frame(10, PATH + [("ingress.ipv4_lpm", "miss")])
check("ok", run(bad)[0], False)

print("a table with table_add entries is missing from the profile")
p = {k: v for k, v in PROFILE.items() if k != "ingress.ticket_deny_table"}
ok, d = run(good, profile=p)
check("ok", ok, False)
check("missing", d["missing_from_profile"], ["ingress.ticket_deny_table"])

print("fewer than 10 frames in the log")
check("ok", run(good[:-len(frame(0, PATH))])[0], False)

print("recirculated copies (N.2) and clones (N.1) received on port 0 are not headline frames")
noise = [l for n in range(10) for l in frame(n, [("ingress.ipv4_lpm", "miss")], copy=2)]
noise += [l for n in range(10) for l in frame(n, [("egress.send_frame", "miss")], copy=1)]
mixed = [x for pair in zip(good, noise + [""] * (len(good) - len(noise))) for x in pair if x]
ok, d = run(mixed)
check("ok", ok, True)
check("frames", d["frames"], 10)

print("frames from another port are not headline frames")
other = [l for n in range(20, 25) for l in frame(n, [("ingress.ipv4_lpm", "miss")], port=1)]
check("ok", run(good + other)[0], True)

print("const-entry tables not in the profile must hit every frame (the E1 rule)")
c_bad = [l for n in range(10) for l in frame(n, PATH + [("ingress.const_t", "miss")])]
check("ok (const misses)", run(c_bad, const={"ingress.const_t"})[0], False)
c_good = [l for n in range(10) for l in frame(n, PATH + [("ingress.const_t", "hit")])]
check("ok (const hits)", run(c_good, const={"ingress.const_t"})[0], True)

print("unknown expectation in the profile")
try:
    run(good, profile={**PROFILE, "ingress.forward": "maybe"})
    check("raises", False, True)
except ValueError:
    check("raises", True, True)

print("generator fields from the plan")
check("none", bench.gen_extra({}), [])
check("addresses + tos", bench.gen_extra({"gen": {"dst_ip": "10.0.3.10", "src_ip": "10.0.2.10", "tos": 2}}),
      ["--dst-ip", "10.0.3.10", "--src-ip", "10.0.2.10", "--tos", "2"])
for badgen in ({"gen": {"dst_mac": "02:00:00:00:00:01"}}, {"gen": {"tos": 256}}, {"gen": {"dst_ip": "10.0.3"}}):
    try:
        bench.gen_extra(badgen)
        check(f"rejects {badgen}", False, True)
    except ValueError:
        check(f"rejects {badgen}", True, True)
bench.GEN_EXTRA[:] = ["--dst-ip", "10.0.3.10"]
calls = []
real = bench.run_tool
bench.run_tool = lambda ns, cpu, tool, args, timeout: calls.append(args) or {}
try:
    bench.gen(64, 0, 32, 1, 1, 5)
finally:
    bench.run_tool = real
    bench.GEN_EXTRA[:] = []
check("gen() passes the instance's fields", calls[0][-2:], ["--dst-ip", "10.0.3.10"])

print("probe rule")
pr = {"gen": {"accepted": 200}, "delivered": 205, "stray_rx": {"h2": 0, "h3": 0},
      "recv": {"rx": 200, "foreign": 5}}
check("counter rule: extra frames fail", bench.probe_ok(pr, own_frames=False), False)
check("own frames: receiver count passes", bench.probe_ok(pr, own_frames=True), True)
check("own frames: one of ours lost", bench.probe_ok({**pr, "recv": {"rx": 199, "foreign": 5}}, True), False)
check("own frames: stray rx fails", bench.probe_ok({**pr, "stray_rx": {"h2": 1, "h3": 0}}, True), False)
check("own frames: no receiver fails", bench.probe_ok({**pr, "recv": None}, True), False)
check("counter rule: exact passes", bench.probe_ok({**pr, "delivered": 200, "recv": None}, False), True)

print("ALL OK" if not fails else f"FAILED ({fails})")
sys.exit(1 if fails else 0)
