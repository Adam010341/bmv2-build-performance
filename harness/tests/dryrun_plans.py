#!/usr/bin/env python3
"""dryrun_plans.py -- helpers of tests/test_dryrun.sh (stdlib only).

  dryrun_plans.py builds PLANS_DIR             print every build id the plans use, one per line
  dryrun_plans.py trim SRC.json OUT.json ID[:T1,T2..] ...
        copy SRC keeping only the named instances, in the order given; ID:0,4 also keeps only
        trials 0 and 4 of that instance (indices into its trial list as written in SRC). Nothing
        else changes: same seed, same trial specs, same durations.
  dryrun_plans.py faults OUT.json              the fault-injection plan (a build that does not exist,
                                               then a normal instance)
  dryrun_plans.py entries OUT.json PROGRAM_JSON GOOD_ENTRIES
                                               the entries-load fault plan: an instance whose
                                               commands file has a BAD line (written next to
                                               OUT.json), then one with GOOD_ENTRIES; both name
                                               their files relative to the plan
  dryrun_plans.py e8 OUTDIR                    the E8 lane (AMENDMENT-9): in OUTDIR a small program JSON with two
                                               keyed tables (ingress.fwd: expected hit, ingress.deny:
                                               expected miss), their entries_cli, two PATH.json files, and
                                               plans e8.json (all E8 fields set), e8noown.json (same, no
                                               own_frames) and e8badprofile.json (deny declared "hit")
  dryrun_plans.py scrub-g4 IN.jsonl OUT.jsonl  copy with the G4 flag removed (see test_dryrun.sh:
                                               a desktop is never quiet enough for G4, which would
                                               leave analyse.py with no cells and no capacities)
"""
import glob
import json
import os
import sys


E8_GEN = {"dst_ip": "10.0.7.9", "src_ip": "10.0.7.1", "tos": 2}     # the plan's "gen" fields
E8_TABLES = ("ingress.fwd", "ingress.deny")


def e8_files(d):
    """The E8 lane's inputs, all in directory d (plan-relative
    names, as in a real E8 plan). The program JSON has only what the fake switch and bench.py read:
    two tables with a match key and no const entries (so both must be filled at run time) and the
    actions their commands name. fwd gets a table_add for the generator's destination (a hit for
    every frame); deny gets one for an address bmgen never sends (a miss for every frame)."""
    os.makedirs(d, exist_ok=True)
    key = [{"match_type": "exact", "name": "hdr.ipv4.dstAddr", "target": ["ipv4", "dstAddr"], "mask": None}]
    prog = {"program": "e8lane.p4",
            "actions": [{"name": "ingress.forward", "id": 0, "runtime_data": [], "primitives": []},
                        {"name": "ingress.drop", "id": 1, "runtime_data": [], "primitives": []}],
            "pipelines": [{"name": "ingress", "id": 0, "tables": [
                {"name": t, "id": i, "key": key, "match_type": "exact", "type": "simple",
                 "actions": ["ingress.forward", "ingress.drop"]} for i, t in enumerate(E8_TABLES)]}]}
    json.dump(prog, open(os.path.join(d, "e8lane.json"), "w"), indent=1)
    with open(os.path.join(d, "entries-e8.txt"), "w") as f:
        f.write("# e8 lane: fwd matches the generator's destination, deny only an address it never sends\n"
                f"table_add ingress.fwd ingress.forward {E8_GEN['dst_ip']} =>\n"
                "table_add ingress.deny ingress.drop 10.66.66.66 =>\n")
    for name, deny in (("PATH-e8.json", "miss"), ("PATH-e8bad.json", "hit")):
        json.dump({"tables": {"ingress.fwd": "hit", "ingress.deny": deny}}, open(os.path.join(d, name), "w"), indent=1)

    def inst(iid, own, profile):
        i = {"id": iid, "build": "cm-default", "program": "e8lane", "program_json": "e8lane.json",
             "entries_cli": "entries-e8.txt", "path_profile": profile, "gen": dict(E8_GEN),
             "hitcheck": True, "block": 0,
             "trials": [{"kind": "sat", "frame": 64, "pps": 0, "burst": 32, "duration": 10, "recv": True}]}
        if own:
            i["own_frames"] = True
        return i

    for fn, name, i in (("e8.json", "E8-dryrun", inst("e8dry-ok", True, "PATH-e8.json")),
                        ("e8noown.json", "E8NOOWN-dryrun", inst("e8dry-noown", False, "PATH-e8.json")),
                        ("e8badprofile.json", "E8BADPROFILE-dryrun", inst("e8dry-badprofile", True, "PATH-e8bad.json"))):
        if name == "E8BADPROFILE-dryrun":
            i["trials"] = []          # the gate under test is the hitcheck; nothing needs measuring
        json.dump({"name": name, "shuffle_seed": 9, "instances": [i]}, open(os.path.join(d, fn), "w"), indent=1)


def main():
    cmd = sys.argv[1]
    if cmd == "builds":
        ids = set()
        for p in glob.glob(os.path.join(sys.argv[2], "*.json")):
            for inst in json.load(open(p))["instances"]:
                ids.add(inst["build"])
        print("\n".join(sorted(ids)))
    elif cmd == "trim":
        src, out = sys.argv[2], sys.argv[3]
        plan = json.load(open(src))
        by_id = {i["id"]: i for i in plan["instances"]}
        keep = []
        for spec in sys.argv[4:]:
            iid, _, tr = spec.partition(":")
            if iid not in by_id:
                sys.exit(f"{src}: no instance {iid}")
            inst = dict(by_id[iid])
            if tr:
                inst["trials"] = [inst["trials"][int(k)] for k in tr.split(",")]
            keep.append(inst)
        plan["instances"] = keep
        plan["name"] = plan["name"] + "-dryrun"
        json.dump(plan, open(out, "w"), indent=1)
    elif cmd == "faults":
        sat = {"kind": "sat", "frame": 64, "pps": 0, "burst": 32, "duration": 10}
        plan = {"name": "FAULTS-dryrun", "shuffle_seed": 7, "instances": [
            {"id": "fault-missing-build", "build": "cm-not-built", "program": "l3", "trials": [sat], "block": 0},
            {"id": "fault-after", "build": "cm-o3-L0-E0", "program": "l3", "block": 1,
             "trials": [{"kind": "rate", "frame": 64, "pps": 20000, "burst": 1, "duration": 10, "recv": True}]}]}
        json.dump(plan, open(sys.argv[2], "w"), indent=1)
    elif cmd == "entries":
        out, prog, good = sys.argv[2], os.path.abspath(sys.argv[3]), sys.argv[4]
        d = os.path.dirname(os.path.abspath(out))
        lines = open(good).read().splitlines()
        with open(os.path.join(d, "entries-good.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        with open(os.path.join(d, "entries-bad.txt"), "w") as f:      # a good command first, then BAD
            f.write("\n".join(lines + ["BAD this line is not a simple_switch_CLI command"]) + "\n")
        plan = {"name": "ENTRIES-dryrun", "shuffle_seed": 8, "instances": [
            {"id": "e8dry-bad", "build": "cm-default", "program": "e8test", "program_json": prog,
             "entries_cli": "entries-bad.txt", "block": 0,
             "trials": [{"kind": "sat", "frame": 64, "pps": 0, "burst": 32, "duration": 10}]},
            {"id": "e8dry-after", "build": "cm-default", "program": "e8test", "program_json": prog,
             "entries_cli": "entries-good.txt", "block": 1,
             "trials": [{"kind": "rate", "frame": 64, "pps": 20000, "burst": 1, "duration": 10, "recv": True}]}]}
        json.dump(plan, open(out, "w"), indent=1)
    elif cmd == "e8":
        e8_files(sys.argv[2])
    elif cmd == "scrub-g4":
        with open(sys.argv[2]) as f, open(sys.argv[3], "w") as g:
            for line in f:
                r = json.loads(line)
                if "flags" in r:
                    r["flags"] = [x for x in r["flags"] if x != "G4"]
                g.write(json.dumps(r) + "\n")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
