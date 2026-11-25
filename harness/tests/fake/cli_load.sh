#!/bin/sh
# fake cli_load.sh -- test double of harness/run/cli_load.sh for the dry run.
#
# Same arguments, exit codes and last line ("cli_load: OK ..." / "cli_load: FAILED ...") as the
# real script. Instead of sswitch_CLI.py over Thrift it talks to the fake simple_switch through
# $FAKE_STATE/thrift/<port>.json (present while a fake switch with that --thrift-port is up; the
# real script waits up to 30 s for the port, so does this one). It checks:
#   * BUILD_DIR exists, THRIFT_PORT is a number, COMMANDS_FILE is readable
#   * the file has at least one command (blank lines and '#' lines are skipped, as in the real one)
#   * every command is a known simple_switch_CLI command; a line starting with BAD is an error
#   * table_add / table_set_default name a table of the running program, and an action it has
# On success it writes the tables it filled to <port>.loaded (the fake switch starts forwarding
# through them) and prints a transcript in runtime_CLI's words; on any error it loads nothing.
# SRC_DIR is not used: the dry run has no bmv2 source tree.
set -u
fail() { echo "cli_load: FAILED $1"; exit "${2:-1}"; }
[ $# -eq 4 ] || { echo "usage: cli_load.sh BUILD_DIR SRC_DIR THRIFT_PORT COMMANDS_FILE" >&2; fail "bad arguments" 2; }
BUILD=$1 PORT=$3 CMDS=$4
case "$PORT" in ''|*[!0-9]*) fail "thrift port '$PORT' is not a number" 2 ;; esac
[ -d "$BUILD" ] || fail "no build directory $BUILD" 2
[ -r "$CMDS" ] && [ -f "$CMDS" ] || fail "cannot read commands file $CMDS" 2
[ -n "${FAKE_STATE:-}" ] || fail "FAKE_STATE not set (fake cli_load runs only under the fake wrapper)" 2
exec /usr/bin/python3 -I - "$FAKE_STATE/thrift/$PORT" "$CMDS" <<'EOF'
import json, os, sys, time
base, cmds = sys.argv[1], sys.argv[2]
KNOWN = {"table_add", "table_set_default", "table_reset_default", "table_clear", "table_delete",
         "table_modify", "table_set_timeout", "act_prof_create_member", "act_prof_add_member_to_group",
         "act_prof_create_group", "table_indirect_add", "table_indirect_add_with_group",
         "table_indirect_set_default", "mirroring_add", "mirroring_add_mc", "mirroring_delete",
         "mc_mgrp_create", "mc_node_create", "mc_node_associate", "mc_node_dissociate",
         "mc_mgrp_destroy", "mc_node_destroy", "mc_node_update", "mc_set_lag_membership",
         "register_write", "register_reset", "counter_reset", "meter_set_rates",
         "meter_array_set_rates", "set_queue_depth", "set_queue_rate", "reset_state"}
def fail(msg):
    print(msg)
    print(f"cli_load: FAILED {msg}")
    sys.exit(1)
t0 = time.time()
while not os.path.exists(base + ".json"):
    if time.time() - t0 > 30:
        fail(f"thrift port {os.path.basename(base)} never answered")
    time.sleep(0.2)
prog = json.load(open(base + ".json"))
lines = [l.strip() for l in open(cmds) if l.strip() and not l.lstrip().startswith("#")]
if not lines:
    fail(f"no commands in {cmds}")
print("Obtaining JSON from switch...\nDone\nControl utility for runtime P4 table manipulation")
filled, added = set(), 0
for l in lines:
    w = l.split()
    if l.startswith("BAD"):
        fail(f"RuntimeCmd: *** Unknown syntax: {l}")
    if w[0] not in KNOWN:
        fail(f"RuntimeCmd: *** Unknown syntax: {l}")
    if w[0] in ("table_add", "table_set_default"):
        if len(w) < 3:
            fail("RuntimeCmd: Error: Insufficient number of args")
        if w[1] not in prog["tables"]:
            fail(f"RuntimeCmd: Error: Invalid table name ({w[1]})")
        if w[2] not in prog["actions"]:
            fail(f"RuntimeCmd: Error: Table {w[1]} has no action {w[2]}")
        filled.add(w[1])
    if w[0] in ("table_indirect_add", "table_indirect_add_with_group", "table_indirect_set_default"):
        if len(w) < 2 or w[1] not in prog["tables"]:
            fail(f"RuntimeCmd: Error: Invalid table name ({w[1] if len(w) > 1 else ''})")
        filled.add(w[1])
    if w[0] in ("table_add", "table_indirect_add", "table_indirect_add_with_group"):
        print(f"RuntimeCmd: Adding entry to match table {w[1]}\nEntry has been added with handle {added}")
        added += 1
    else:
        print(f"RuntimeCmd: {w[0]} (fake: accepted)")
print("RuntimeCmd: ")
with open(base + ".loaded.tmp", "w") as f:
    json.dump(sorted(filled), f)
os.replace(base + ".loaded.tmp", base + ".loaded")
print(f"cli_load: OK {len(lines)} commands, {added} entries added")
EOF
