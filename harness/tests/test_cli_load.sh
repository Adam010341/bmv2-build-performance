#!/usr/bin/env bash
# test_cli_load.sh -- the real harness/run/cli_load.sh against a stub bmv2 tree (no switch, no
# privileges; ~35 s, most of it the 30 s "Thrift never answers" case).
#
# The stub sswitch_CLI.py imports runtime_CLI, bm_runtime and sswitch_runtime (so PYTHONPATH must
# be right), connects to the Thrift port like bmpy_utils.thrift_connect (prints "Could not connect
# ..." and exits 1 if nothing listens), and answers commands in runtime_CLI's words. It treats a
# '#' line as an unknown command, as cmd.Cmd does, so cli_load must filter comments. Every case
# runs under the cleared environment the root wrapper hands out (env -i, five variables).
#
# What the dry run cannot show (its cli_load.sh is a fake): PYTHONPATH from a CMake and from an
# autotools-like layout, comment filtering, waiting for a late Thrift port, error lines, an
# unconfirmed table_add, a CLI crash or silent non-zero exit, no Thrift at all, empty file, missing gen-py, bad arguments.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CL="${CLI_LOAD:-$HERE/../run/cli_load.sh}"      # CLI_LOAD=... tests another copy (mutants)
T="$(mktemp -d "${TMPDIR:-/tmp}/clitest.XXXXXX")"
LPID=""
cleanup() { [[ -n "$LPID" ]] && kill "$LPID" 2>/dev/null; rm -rf "${T:?}"; }
trap cleanup EXIT
fail=0

# ---- stub bmv2 trees
S="$T/src"
mkdir -p "$S/tools" "$S/targets/simple_switch"
echo "# stub" > "$S/tools/runtime_CLI.py"
cat > "$S/targets/simple_switch/sswitch_CLI.py" <<'PY'
import socket, sys
import runtime_CLI, bm_runtime, sswitch_runtime      # noqa: F401 -- PYTHONPATH check
port = int(sys.argv[sys.argv.index("--thrift-port") + 1])
try:
    socket.create_connection(("127.0.0.1", port), timeout=1).close()
except OSError:
    print("Could not connect to thrift client on port %d" % port)
    print("Make sure the switch is running and that you have the right port")
    sys.exit(1)
print("Obtaining JSON from switch...\nDone\nControl utility for runtime P4 table manipulation")
h = 0
for line in sys.stdin:
    w = line.split()
    if not w:
        continue
    if w[0] == "table_add" and w[1] == "SILENT":
        print("RuntimeCmd: Adding entry to exact match table SILENT")      # and no confirmation
    elif w[0] in ("table_add", "table_indirect_add"):
        print("RuntimeCmd: Adding entry to exact match table %s\nmatch key:           EXACT-0a:00:01:01\n"
              "action:              %s\nruntime data:        \nEntry has been added with handle %d" % (w[1], w[2], h))
        h += 1
    elif w[0] == "table_set_default" and w[1] == "NOSUCH":
        print("RuntimeCmd: Error: Invalid table name (NOSUCH)")
    elif w[0] == "table_set_default":
        print("RuntimeCmd: Setting default action of %s\naction:              %s\nruntime data:        " % (w[1], w[2]))
    elif w[0] == "CRASH":
        raise RuntimeError("stub crash")
    elif w[0] == "EXIT3":
        sys.exit(3)                                                         # silent non-zero exit
    else:
        print("RuntimeCmd: *** Unknown syntax: %s" % line.strip())
print("RuntimeCmd: ")
PY
mkpkg() { mkdir -p "$1" && : > "$1/__init__.py"; }
CM="$T/builds/cm-x"; mkpkg "$CM/thrift_src/gen-py/bm_runtime"; mkpkg "$CM/targets/simple_switch/gen-py/sswitch_runtime"
AT="$T/builds/at-x"; mkpkg "$AT/a/b/gen-py/bm_runtime"; mkpkg "$AT/c/gen-py/sswitch_runtime"
# what `make install` of an autotools build leaves (testbed builds/at-docs-perf, seen 2026-10-23):
# the bindings live under the install prefix's site-packages, there is no gen-py at all
AI="$T/builds/at-inst"; mkpkg "$AI/inst/lib/python3.12/site-packages/bm_runtime"; mkpkg "$AI/inst/lib/python3.12/site-packages/sswitch_runtime"
mkdir -p "$AI/inst/include/bm/bm_runtime"                     # a header dir of the same name: not a package
NOGEN="$T/builds/none"; mkdir -p "$NOGEN"

# ---- a Thrift port stand-in: accepts and closes, optionally after a delay
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
listen() {  # $1 = delay in s
    python3 -c '
import socket, sys, time
time.sleep(float(sys.argv[2]))
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(16)
while True:
    s.accept()[0].close()
' "$PORT" "$1" &
    LPID=$!
}
unlisten() { [[ -n "$LPID" ]] && kill "$LPID" 2>/dev/null; wait "$LPID" 2>/dev/null; LPID=""; }

# ---- commands files
printf '# comment\n\ntable_add MyIngress.t MyIngress.fwd 10.0.1.1 =>\n  # indented comment\ntable_set_default MyIngress.t MyIngress.drop\ntable_indirect_add MyIngress.t2 10.0.1.3 => 0\n' > "$T/good.txt"
printf 'table_add MyIngress.t MyIngress.fwd 10.0.1.1 =>\ntable_set_default NOSUCH MyIngress.drop\n' > "$T/errline.txt"
printf 'table_add MyIngress.t MyIngress.fwd 10.0.1.1 =>\ntable_add SILENT MyIngress.fwd 10.0.1.2 =>\n' > "$T/silent.txt"
printf 'table_add MyIngress.t MyIngress.fwd 10.0.1.1 =>\nCRASH\n' > "$T/crash.txt"
printf 'table_add MyIngress.t MyIngress.fwd 10.0.1.1 =>\nEXIT3\n' > "$T/exit3.txt"
printf '# only a comment\n\n' > "$T/empty.txt"

case_() {  # NAME WANT_RC WANT_LAST_LINE_PREFIX ARGS...
    local name="$1" want_rc="$2" want="$3"; shift 3
    local out rc last
    out="$(env -i HOME="$HOME" USER="$(id -un)" LOGNAME="$(id -un)" LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin \
           "$CL" "$@" 2>&1)"; rc=$?
    last="$(printf '%s\n' "$out" | tail -n 1)"
    if [[ $rc -eq $want_rc && "$last" == "$want"* ]]; then
        echo "  ok   $name (rc $rc: $last)"
    else
        echo "  FAIL $name: rc $rc (want $want_rc), last line '$last' (want '$want...')"
        printf '%s\n' "$out" | sed 's/^/       | /'
        fail=1
    fi
}

listen 0; sleep 0.5
case_ "good, CMake layout, comments skipped" 0 "cli_load: OK 3 commands, 2 entries added" "$CM" "$S" "$PORT" "$T/good.txt"
case_ "good, autotools-like layout" 0 "cli_load: OK 3 commands" "$AT" "$S" "$PORT" "$T/good.txt"
case_ "good, autotools install layout (site-packages)" 0 "cli_load: OK 3 commands" "$AI" "$S" "$PORT" "$T/good.txt"
case_ "error line in the output" 1 "cli_load: FAILED error lines" "$CM" "$S" "$PORT" "$T/errline.txt"
case_ "table_add not confirmed" 1 "cli_load: FAILED 1 entries confirmed for 2" "$CM" "$S" "$PORT" "$T/silent.txt"
case_ "CLI crashes" 1 "cli_load: FAILED sswitch_CLI exited" "$CM" "$S" "$PORT" "$T/crash.txt"
case_ "CLI exits 3 without an error line" 1 "cli_load: FAILED sswitch_CLI exited 3" "$CM" "$S" "$PORT" "$T/exit3.txt"
case_ "no commands" 1 "cli_load: FAILED no commands" "$CM" "$S" "$PORT" "$T/empty.txt"
case_ "no bindings" 1 "cli_load: FAILED no bm_runtime bindings" "$NOGEN" "$S" "$PORT" "$T/good.txt"
case_ "bad arguments" 2 "cli_load: FAILED bad arguments" "$CM" "$S" "$PORT"
case_ "port not a number" 2 "cli_load: FAILED thrift port" "$CM" "$S" "90x" "$T/good.txt"
unlisten
listen 3
case_ "Thrift port comes up 3 s late" 0 "cli_load: OK 3 commands" "$CM" "$S" "$PORT" "$T/good.txt"
unlisten
case_ "Thrift never answers (30 s)" 1 "cli_load: FAILED thrift port $PORT never answered" "$CM" "$S" "$PORT" "$T/good.txt"

if [[ $fail -eq 0 ]]; then echo "cli_load tests OK"; else echo "cli_load tests FAILED"; fi
exit $fail
