#!/usr/bin/env python3
"""Check that every const entry in the compiled programs matches what the test traffic carries.
bmgen sends 10.0.0.1 -> 10.0.1.1, UDP sport 10000+flow; iperf3 sends 10.0.0.1 -> 10.0.0.2.
Exits non-zero if any table entry could not match that traffic as intended.
"""
import glob, json, os, sys
bad = 0
for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "build", "*.json"))):
    name = os.path.basename(path)[:-5]
    for p in json.load(open(path))["pipelines"]:
        for t in p["tables"]:
            for e in t.get("entries", []):
                k = e["match_key"][0]
                v = int(k["key"], 16)
                if k["match_type"] == "lpm":
                    plen = k["prefix_length"]
                    net = lambda a: a >> (32 - plen) << (32 - plen)
                    ok = plen == 24 and v in (0x0a000100, 0x0a000000) and net(v) == v
                elif k["match_type"] == "ternary":
                    m = int(k["mask"], 16)
                    ok = (v & m) in (0x0a000100,) or (m == 0xffff and 10000 <= v < 10000 + 4096)
                else:
                    ok = v == 0x0a000101 or 10000 <= v < 10000 + 4096
                if not ok:
                    bad += 1
                    print(f"BAD {name} {t['name']} {k}")
print("entries OK" if not bad else f"{bad} bad entries"); sys.exit(1 if bad else 0)
