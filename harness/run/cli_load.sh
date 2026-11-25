#!/bin/sh
# cli_load.sh BUILD_DIR SRC_DIR THRIFT_PORT COMMANDS_FILE
#
# Load table entries (simple_switch_CLI commands) into a running simple_switch over Thrift, for
# programs whose tables are filled at run time (E8, PREREG AMENDMENT-7). bench.py runs it through
# the root wrapper inside the switch's netns (the Thrift server listens on localhost there):
#   bmbench-priv exec bmb-sw <HOUSE_CPU> /home/adam/bmbench/harness/run/cli_load.sh ...
# The wrapper clears the environment, so PYTHONPATH is set here:
#   SRC_DIR/tools                                    runtime_CLI, bmpy_utils
#   first BUILD_DIR/**/gen-py holding bm_runtime     Thrift bindings of the standard API
#   first BUILD_DIR/**/gen-py holding sswitch_runtime  Thrift bindings of simple_switch
# (CMake and autotools trees put gen-py in different places, so both are searched for; an
# autotools build that was only installed has no gen-py, its bindings are under the prefix's
# site-packages, which is searched when no gen-py is found.)
#
# Blank lines and lines starting with '#' are skipped. Waits up to 30 s for the Thrift port, then
# feeds every command to SRC_DIR/targets/simple_switch/sswitch_CLI.py on stdin and prints the
# transcript. Exits 1 if the CLI fails, if any output line looks like an error, or if the CLI's
# "Entry has been added with handle" lines do not match the entry-adding commands sent one for one
# (table_add, table_indirect_add, table_indirect_add_with_group print it);
# exits 2 on bad arguments. The last line is always "cli_load: OK ..." or "cli_load: FAILED ...".
set -u
fail() { echo "cli_load: FAILED $1"; exit "${2:-1}"; }
[ $# -eq 4 ] || { echo "usage: cli_load.sh BUILD_DIR SRC_DIR THRIFT_PORT COMMANDS_FILE" >&2; fail "bad arguments" 2; }
BUILD=$1 SRC=$2 PORT=$3 CMDS=$4
case "$PORT" in ''|*[!0-9]*) fail "thrift port '$PORT' is not a number" 2 ;; esac
[ -d "$BUILD" ] || fail "no build directory $BUILD" 2
[ -r "$CMDS" ] && [ -f "$CMDS" ] || fail "cannot read commands file $CMDS" 2
CLI="$SRC/targets/simple_switch/sswitch_CLI.py"
[ -f "$CLI" ] || fail "no $CLI" 2
[ -f "$SRC/tools/runtime_CLI.py" ] || fail "no $SRC/tools/runtime_CLI.py" 2
PY=/usr/bin/python3
[ -x "$PY" ] || PY=python3

pkg() {  # first directory named $1 under a gen-py, else under a site-packages
    for d in gen-py site-packages; do
        p=$(find "$BUILD" -type d -path "*/$d/$1" 2>/dev/null | LC_ALL=C sort | head -n 1)
        [ -n "$p" ] && { echo "$p"; return; }
    done
}
BM=$(pkg bm_runtime)
SS=$(pkg sswitch_runtime)
[ -n "$BM" ] || fail "no bm_runtime bindings (gen-py or site-packages) under $BUILD"
[ -n "$SS" ] || fail "no sswitch_runtime bindings (gen-py or site-packages) under $BUILD"
PYTHONPATH="$SRC/tools:$(dirname "$BM"):$(dirname "$SS")"
export PYTHONPATH
echo "cli_load: build $BUILD, cli $CLI, port $PORT, commands $CMDS"
echo "cli_load: PYTHONPATH=$PYTHONPATH"

SKIP='^[[:space:]]*(#|$)'
N=$(grep -c -v -E "$SKIP" "$CMDS")
[ "$N" -gt 0 ] || fail "no commands in $CMDS"
NADD=$(grep -c -E '^[[:space:]]*table_(add|indirect_add|indirect_add_with_group)[[:space:]]' "$CMDS")

# the switch process exists before its Thrift server listens: wait for the port ("localhost",
# resolved as the CLI resolves its default --thrift-ip)
"$PY" -I -c '
import socket, sys, time
port, t0 = int(sys.argv[1]), time.time()
while True:
    try:
        socket.create_connection(("localhost", port), timeout=1).close()
        break
    except OSError as e:
        if time.time() - t0 > 30:
            sys.exit("thrift port %d not reachable after 30 s: %s" % (port, e))
        time.sleep(0.2)
' "$PORT" || fail "thrift port $PORT never answered"

OUT=$(grep -v -E "$SKIP" "$CMDS" | "$PY" "$CLI" --thrift-port "$PORT" 2>&1)
RC=$?
printf '%s\n' "$OUT"
[ "$RC" -eq 0 ] || fail "sswitch_CLI exited $RC"
BAD=$(printf '%s\n' "$OUT" | grep -n -E 'Error|Invalid|is not a valid|Exception|Traceback|Unknown syntax|Could not connect')
[ -z "$BAD" ] || { printf '%s\n' "$BAD" | sed 's/^/cli_load: error line /'; fail "error lines in the CLI output"; }
OKADD=$(printf '%s\n' "$OUT" | grep -c 'Entry has been added with handle')
[ "$OKADD" -eq "$NADD" ] || fail "$OKADD entries confirmed for $NADD entry-adding commands"
echo "cli_load: OK $N commands, $NADD entries added"
