#!/usr/bin/env bash
# test_dryrun.sh -- run the harness's runtime path (bench.py, arms.py, analyse.py, make_plans.py,
# preround.sh) end to end against test doubles, with no root and no network namespaces.
#
#   bash harness/tests/test_dryrun.sh            # ~15 min; exit 0 = every check passed
#   DRYRUN_KEEP=1 ...                            # keep the temp dir (it is always kept on failure)
#   DRYRUN_LANES="smoke faults" ...              # run only these lanes (default: all)
#   DRYRUN_RUN_DIR=/path/to/copy-of-run ...      # use that bench.py/arms.py instead of harness/run's (mutation
#                                                #   checks: a deliberately broken copy must make a lane fail)
#
# What runs where: tests/fake/ holds a fake bmbench-priv (same CLI as harness/priv/bmbench-priv)
# and fake bmgen, bmrecv, bmnull, bmnic, simple_switch, iperf3 and docker-rootfs ld.so, all sharing one
# fake network per lane (fakenet.py). bench.py/arms.py reach the fakes through BMBENCH_PRIV,
# BMBENCH_TOOLS and BMBENCH_ROOT; preround.sh, which hard-codes `sudo -n
# /usr/local/sbin/bmbench-priv` and $HOME/bmbench, through a fake `sudo` first on PATH and a
# throwaway HOME. Durations, ladders and gates are the plans' own: only the set of instances (and,
# for E4, the trials of an instance) is cut, in trimmed copies of the plans.
#
# Fails on: any non-zero exit, any Python traceback, any `"record": "error"` that is not an
# expected fault, any hang (every step runs under `timeout`), and any check of
# tests/dryrun_check.py (counter-based gates that fire on the exact fake network, wrong record
# counts, capacities and zero-loss rates that do not match the fake switch, ...).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
H="$(cd "$HERE/.." && pwd)"
FAKE="$HERE/fake"
PRIV="$FAKE/bmbench-priv"
PY=/usr/bin/python3; [[ -x "$PY" ]] || PY="$(command -v python3)"   # testbed runs the system python
T="$(mktemp -d "${TMPDIR:-/tmp}/bmbdry.XXXXXX")"
ROOT="$T/r"                       # short: nanomsg ipc paths must fit in sun_path, as on testbed
OUT="$ROOT/raw/dry"
RUNDIR="${DRYRUN_RUN_DIR:-$H/run}"
LANES="${DRYRUN_LANES:-calibrate null smoke hit e1chain e1rt e3 faults dead iperfdead entries e8 e8noown e8badprofile r3 x12 x14 preround}"
T0=$(date +%s)
export BMBENCH_ROOT="$ROOT" BMBENCH_PRIV="$PRIV" BMBENCH_TOOLS="$ROOT/fake-bin" FAKE_ROOT="$ROOT"
export BMBENCH_CLI_LOAD="$ROOT/fake-bin/cli_load.sh"   # under the fake root, so the fake wrapper runs it

say() { printf '%s [%4ds] %s\n' "$(date +%T)" "$(( $(date +%s) - T0 ))" "$*"; }

stop_fakes() {   # every fake process leaves a pid file; stop those whose environment is ours
    local f pid
    for f in "$T"/state-*/pids/*; do
        [[ -e "$f" ]] || continue
        pid="${f##*/}"
        if tr '\0' '\n' 2>/dev/null < "/proc/$pid/environ" | grep -qx "FAKE_STATE=$T/state-.*"; then
            kill -TERM "$pid" 2>/dev/null
        fi
    done
    sleep 1
    for f in "$T"/state-*/pids/*; do
        [[ -e "$f" ]] || continue
        pid="${f##*/}"
        if tr '\0' '\n' 2>/dev/null < "/proc/$pid/environ" | grep -qx "FAKE_STATE=$T/state-.*"; then
            kill -KILL "$pid" 2>/dev/null
        fi
    done
    for f in $(find "$OUT" -name control-load.pid 2>/dev/null); do   # arms.py controls' CPU loads
        kill -TERM -- "-$(cat "$f")" 2>/dev/null
    done
}

finish() {
    local rc=$1
    stop_fakes
    if [[ $rc -eq 0 && -z "${DRYRUN_KEEP:-}" ]]; then rm -rf "$T"; else say "outputs kept in $T"; fi
    exit "$rc"
}
trap 'say "interrupted"; finish 130' INT TERM

# --------------------------------------------------------------------------- throwaway root
say "building the throwaway bmbench root in $ROOT"
mkdir -p "$ROOT"/{builds,builds.v0,p4,raw,docker,fake-bin,harness} "$OUT" "$T/plans"
cp -a "$H"/. "$ROOT/harness/"
rm -rf "$ROOT/harness/bin"
echo "dryrun-$(git -C "$H" rev-parse --short HEAD 2>/dev/null || echo nogit)" > "$ROOT/harness/HARNESS-GIT"
if ls "$H"/p4/build/*.json > /dev/null 2>&1; then
    cp "$H"/p4/build/*.json "$ROOT/p4/"
else
    say "FAIL: no compiled P4 programs in harness/p4/build (run harness/p4/compile.sh)"; finish 1
fi
cp "$FAKE/bmgen" "$FAKE/bmrecv" "$FAKE/bmnull" "$FAKE/bmnic" "$FAKE/cli_load.sh" "$ROOT/fake-bin/"
# every build of matrix.tsv, plus whatever docker images the plans name
n=0
while IFS=$'\t' read -r id system _; do
    [[ "$id" == \#* || -z "$id" ]] && continue
    d="$ROOT/builds/$id"
    if [[ "$system" == cmake ]]; then bin="$d/targets/simple_switch/simple_switch"; else bin="$d/inst/bin/simple_switch"; fi
    mkdir -p "$(dirname "$bin")"
    { cat "$FAKE/simple_switch"; echo "# fake build $id"; } > "$bin"; chmod +x "$bin"
    { echo "id=$id"; echo "bmv2_commit=0000000000000000000000000000000000000000"; echo "system=$system"
      echo "binary=$bin"; echo "binary_sha256=$(sha256sum "$bin" | cut -d' ' -f1)"
      echo "version_string=fake"; echo "foreign_bmv2_libs=none"
      echo "ldd: 	libbmall.so.0 => $d/src/bm_sim/.libs/libbmall.so.0 (0x00007f0000000000)"
      echo "evidence_trace_strings=0"; } > "$d/BUILD-INFO"
    if (( n++ % 2 == 0 )); then mkdir -p "$ROOT/builds.v0/$id"; cp "$d/BUILD-INFO" "$ROOT/builds.v0/$id/"; fi
done < "$H/builds/matrix.tsv"
for b in $("$PY" "$HERE/dryrun_plans.py" builds "$H/plans" | grep '^docker'); do
    r="$ROOT/docker/${b#*:}/rootfs"
    [[ -d "$r" ]] && continue
    mkdir -p "$r/usr/local/bin" "$r/usr/local/lib" "$r/usr/lib/x86_64-linux-gnu" "$r/lib64"
    ln -s usr/lib "$r/lib"                                                 # merged /usr, re-rooted
    cp "$FAKE/simple_switch" "$r/usr/local/bin/simple_switch"
    cp "$FAKE/simple_switch" "$r/usr/local/bin/simple_switch_grpc"
    cp "$FAKE/ld-linux-x86-64.so.2" "$r/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
    ln -s ../usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 "$r/lib64/ld-linux-x86-64.so.2"
done

# --------------------------------------------------------------------------- trimmed plans
P="$T/plans"; DP="$HERE/dryrun_plans.py"; HP="$H/plans"
"$PY" "$DP" trim "$HP/hitcheck.json" "$P/hitcheck.json" hit-l3 hit-flow_ternary_256 hit-lpm_8 hit-min || finish 1
"$PY" "$DP" trim "$HP/e1.json" "$P/e1.json" e1-cm-o0-L1-E1-min-r0 e1-cm-o0-L1-E1-l3-r0 e1-cm-o3-L0-E0-l3-r0 \
    e1x-at-default-l3-r0 e1x-docker_latest-b2ca70c6b04e-l3-r0 || finish 1
"$PY" "$DP" trim "$HP/e1rt.json" "$P/e1rt.json" e1rt-cm-o0-L1-E1-none-r0 e1rt-cm-o0-L1-E1-console-r0 \
    e1rt-cm-o3-L1-E1-pcap-r0 e1rt-cm-o0-L1-E1-nanolog-r0 e1rt-docker-grpc_latest-b2ca70c6b04e-tutorial-r0 || finish 1
"$PY" "$DP" trim "$HP/e2.json" "$P/e2.json" e2-cm-o0-L1-E1-min-r0 e2-cm-o0-L1-E1-min-r1 \
    e2-cm-o3-L0-E0-ternary_8-r0 e2-cm-o0-L1-E1-l3_reg-r0 || finish 1
"$PY" "$DP" trim "$HP/e3.json" "$P/e3.json" e3-cm-o0-L1-E1-l3-r0 e3-cm-o0-L1-E1-l3-r1 || finish 1
"$PY" "$DP" faults "$P/faults.json"
"$PY" "$DP" trim "$HP/smoke.json" "$P/dead.json" smoke-cm-o3-L0-E0 || finish 1
"$PY" "$DP" trim "$HP/smoke.json" "$P/iperfdead.json" smoke-cm-o3-L0-E0:4 || finish 1
"$PY" "$DP" entries "$P/entries.json" "$H/p4/e8test/e8test.json" "$H/p4/e8test/entries.txt" || finish 1
"$PY" "$DP" e8 "$P" || finish 1
"$PY" "$DP" trim "$HP/r3.json" "$P/r3.json" r3-cm-o0-L1-E1-l3-r0 r3-cm-o3-L0-E0-l3-r0 || finish 1
"$PY" "$DP" trim "$HP/x12.json" "$P/x12.json" x12-cm-o0-L1-E1-logoff-r0 x12-at-tutorial-tutorial-r0 \
    x12-cm-o3-L1-E1-none-r0 || finish 1
"$PY" "$DP" trim "$HP/x14.json" "$P/x14.json" x14-cm-default-none-r0 x14-at-docs-perf-none-r0 \
    x14-at-tutorial-tutorial-r0 || finish 1

# --------------------------------------------------------------------------- lanes
# A lane = one fake network (its own state dir, its own topo-up), so lanes run side by side.
lane_env() {   # $1 = lane name; sets FAKE_STATE and writes the lane's config
    export FAKE_STATE="$T/state-$1"
    mkdir -p "$FAKE_STATE"
    local c="${2:-}"; [[ -n "$c" ]] || c='{}'
    echo "$c" > "$FAKE_STATE/config.json"
}
topo() { "$PRIV" topo-down > /dev/null && "$PRIV" topo-up 4 > /dev/null; }
lane_pids() {  # fake processes of this lane that are still alive
    local f pid
    for f in "$FAKE_STATE"/pids/*; do
        [[ -e "$f" ]] || continue
        pid="${f##*/}"
        tr '\0' '\n' 2>/dev/null < "/proc/$pid/environ" | grep -qx "FAKE_STATE=$FAKE_STATE" && echo "$pid"
    done
}
leftovers() {  # what a harness step started must be gone once it has exited (20 s grace for receivers)
    local i p left=""
    for i in $(seq 20); do left="$(lane_pids)"; [[ -z "$left" ]] && return 0; sleep 1; done
    for p in $left; do
        echo "LANE-FAIL left running after '$1': pid $p $(tr '\0' ' ' 2>/dev/null < "/proc/$p/cmdline" | cut -c1-160)"
        kill -TERM "$p" 2>/dev/null
    done
}
run() {        # $1 = timeout in s, rest = command; logs the command and its exit code
    local to=$1 f; shift
    echo "+ $*"
    timeout -k 20 "$to" "$@"; local rc=$?
    echo "+ exit $rc"
    [[ $rc -eq 0 ]] || echo "LANE-FAIL exit $rc: $*"
    leftovers "${*##*/}"
    for f in $(find "$OUT" -name control-load.pid 2>/dev/null); do
        if kill -0 -- "-$(cat "$f")" 2>/dev/null; then
            echo "LANE-FAIL control load (process group $(cat "$f")) still running"; kill -TERM -- "-$(cat "$f")"
        fi
    done
    return $rc
}
bench() { run "$1" "$PY" -u "$RUNDIR/bench.py" "$2" "$3"; }
check() { "$PY" "$HERE/dryrun_check.py" "$@" || echo "LANE-FAIL check: $*"; }

lane_calibrate() {
    lane_env calibrate; topo
    run 900 "$PY" -u "$H/run/arms.py" calibrate "$OUT/g1-calibration.jsonl"
    check calibrate "$OUT/g1-calibration.jsonl" "$PRIV"
}
lane_null() {
    lane_env null; topo
    run 600 "$PY" -u "$H/run/arms.py" null "$OUT/g9-null.jsonl"
    check null "$OUT/g9-null.jsonl" "$FAKE_STATE" "$PRIV"
    run 300 "$PY" -u "$H/run/arms.py" controls cm-o3-L0-E0 "$OUT/g4-controls.jsonl"
    check controls "$OUT/g4-controls.jsonl" "$PRIV"
}
lane_smoke() {
    lane_env smoke; topo
    bench 1500 "$HP/smoke.json" "$OUT/smoke.jsonl"
    check bench "$HP/smoke.json" "$OUT/smoke.jsonl" "$FAKE_STATE" "$PRIV"
}
lane_hit() {
    lane_env hit; topo
    bench 300 "$P/hitcheck.json" "$OUT/g10-hitcheck.jsonl"
    check bench "$P/hitcheck.json" "$OUT/g10-hitcheck.jsonl" "$FAKE_STATE" "$PRIV"
    bench 600 "$P/e2.json" "$OUT/e2.jsonl"
    check bench "$P/e2.json" "$OUT/e2.jsonl" "$FAKE_STATE" "$PRIV"
}
lane_e1rt() {
    lane_env e1rt; topo
    bench 600 "$P/e1rt.json" "$OUT/e1rt.jsonl"
    check bench "$P/e1rt.json" "$OUT/e1rt.jsonl" "$FAKE_STATE" "$PRIV"
}
lane_x12() {      # AMENDMENT-12: -L off reaches the switch, and the at-tutorial build runs
    lane_env x12; topo
    bench 600 "$P/x12.json" "$OUT/x12.jsonl"
    check bench "$P/x12.json" "$OUT/x12.jsonl" "$FAKE_STATE" "$PRIV"
}
lane_x14() {      # AMENDMENT-14 (c): one iperf3 TCP stream per instance through the switch
    lane_env x14; topo
    bench 600 "$P/x14.json" "$OUT/x14.jsonl"
    check bench "$P/x14.json" "$OUT/x14.jsonl" "$FAKE_STATE" "$PRIV"
}
lane_e3() {
    lane_env e3; topo
    bench 900 "$P/e3.json" "$OUT/e3.jsonl"
    check bench "$P/e3.json" "$OUT/e3.jsonl" "$FAKE_STATE" "$PRIV"
}
lane_faults() {   # a build that was never built must cost its instance only, not the run
    lane_env faults; topo
    bench 300 "$P/faults.json" "$OUT/faults.jsonl"
    check bench "$P/faults.json" "$OUT/faults.jsonl" "$FAKE_STATE" "$PRIV" \
        --expect-error "fault-missing-build:FileNotFoundError"
}
lane_dead() {     # a switch that starts but never forwards: error record, clean stop, no hang
    lane_env dead '{"dead_switch": true}'; topo
    bench 300 "$P/dead.json" "$OUT/dead.jsonl"
    check bench "$P/dead.json" "$OUT/dead.jsonl" "$FAKE_STATE" "$PRIV" \
        --expect-error "smoke-cm-o3-L0-E0:no frame crossed the switch"
}
lane_iperfdead() {  # iperf3 cannot connect through the switch: error record, and nothing left behind
    lane_env iperfdead '{"drop_proto": "tcp"}'; topo
    bench 300 "$P/iperfdead.json" "$OUT/iperfdead.jsonl"
    check bench "$P/iperfdead.json" "$OUT/iperfdead.jsonl" "$FAKE_STATE" "$PRIV" \
        --expect-error "smoke-cm-o3-L0-E0:TimeoutExpired"
}
lane_entries() {  # E8: tables filled at run time from a simple_switch_CLI commands file (cli_load.sh)
    lane_env entries; topo
    # (a) the real e8test plan: entries loaded before the readiness probe (the fake switch, like
    # e8test.p4, drops everything until its table is filled), sat trial and hitcheck run, the
    # instance records carry the file's path, sha256 and command count
    bench 300 "$HP/e8test.json" "$OUT/e8test.jsonl"
    check bench "$HP/e8test.json" "$OUT/e8test.jsonl" "$FAKE_STATE" "$PRIV"
    # (b) a commands file with a BAD line: error record, nothing measured, switch stopped, and
    # the next instance still runs
    bench 300 "$P/entries.json" "$OUT/entries.jsonl"
    check bench "$P/entries.json" "$OUT/entries.jsonl" "$FAKE_STATE" "$PRIV" \
        --expect-error "e8dry-bad:entries load failed"
}
# E8 (AMENDMENT-9) through the fakes: per-instance generator fields, G10 path profile, own-frames probe.
# The fake switch has two keyed tables filled from entries_cli (ingress.fwd hit, ingress.deny miss by
# config), logs a recirculated N.2 copy of every frame with the opposite outcomes, and sends frames of
# its own to h1 (so h1's interface counter is not ours).
E8_CFG='{"miss_tables": ["ingress.deny"], "own_frames_pps": 1000, "recirc_copy": true}'
lane_e8() {       # all three E8 fields set: probe, hitcheck and the sat trial pass; foreign frames are seen
    lane_env e8 "$E8_CFG"; topo
    bench 300 "$P/e8.json" "$OUT/e8.jsonl"
    check bench "$P/e8.json" "$OUT/e8.jsonl" "$FAKE_STATE" "$PRIV"
}
lane_e8noown() {  # the same instance without own_frames: the probe counts h1's interface, fails, instance skipped
    lane_env e8noown "$E8_CFG"; topo
    bench 300 "$P/e8noown.json" "$OUT/e8noown.jsonl"
    check bench "$P/e8noown.json" "$OUT/e8noown.jsonl" "$FAKE_STATE" "$PRIV" --expect-probe-fail e8dry-noown
}
lane_e8badprofile() {  # a profile that declares ingress.deny "hit": G10 fails, the instance record says so
    lane_env e8badprofile "$E8_CFG"; topo
    bench 300 "$P/e8badprofile.json" "$OUT/e8badprofile.jsonl"
    check bench "$P/e8badprofile.json" "$OUT/e8badprofile.jsonl" "$FAKE_STATE" "$PRIV" --expect-hitcheck-fail e8dry-badprofile
}
lane_r3() {       # R3 (AMENDMENT-11): one pair, then h0->h1 and h2->h3 at once, sharing the switch
    lane_env r3 '{"gen_max_pps_if": {"h2": 800000}}'; topo
    bench 300 "$P/r3.json" "$OUT/r3.jsonl"
    check bench "$P/r3.json" "$OUT/r3.jsonl" "$FAKE_STATE" "$PRIV"
}
lane_e1chain() {  # E1 -> analyse --caps-out -> make_plans --capacity -> E4 and E5 (each in its own lane)
    lane_env e1chain; topo
    bench 900 "$P/e1.json" "$OUT/e1.jsonl"
    check bench "$P/e1.json" "$OUT/e1.jsonl" "$FAKE_STATE" "$PRIV"
    run 300 "$PY" "$H/run/analyse.py" "$OUT/e1.jsonl" --caps-out "$OUT/caps-raw.json" --json "$OUT/e1-analysis-raw.json"
    # This host is not quiet: G4 (foreign CPU > 5 %) flags every trial, and analyse.py rightly
    # excludes them all. The capacities for E4/E5 are therefore taken from a copy without G4.
    "$PY" "$DP" scrub-g4 "$OUT/e1.jsonl" "$OUT/e1-nog4.jsonl"
    run 300 "$PY" "$H/run/analyse.py" "$OUT/e1-nog4.jsonl" --caps-out "$OUT/caps.json" --json "$OUT/e1-analysis.json"
    check caps "$OUT/caps.json" "cm-o0-L1-E1|l3|64" "cm-o0-L1-E1|l3|1518" "cm-o3-L0-E0|l3|64" "cm-o3-L0-E0|l3|1518"
    run 120 "$PY" "$H/run/make_plans.py" "$T/plans-gen" --capacity "$OUT/caps.json" || return
    "$PY" "$DP" trim "$T/plans-gen/e4.json" "$P/e4a.json" "e4-cm-o0-L1-E1-l3-r0:0,4"
    "$PY" "$DP" trim "$T/plans-gen/e4.json" "$P/e4b.json" "e4-cm-o0-L1-E1-l3-r1:8,9"
    "$PY" "$DP" trim "$T/plans-gen/e5.json" "$P/e5a.json" e5-cm-o0-L1-E1-l3-n1-r0
    "$PY" "$DP" trim "$T/plans-gen/e5.json" "$P/e5b.json" e5-cm-o0-L1-E1-l3-n1-r1
    "$PY" "$DP" trim "$T/plans-gen/e5.json" "$P/e5c.json" e5-cm-o3-L0-E0-flow_exact_256-n256-r0
    "$PY" "$DP" trim "$T/plans-gen/e5.json" "$P/e5d.json" e5d-cm-o3-L0-E0-l3-n16-r0
    local sub
    for sub in e4a e4b e5a e5b e5c e5d; do
        ( lane_env "$sub"; topo
          bench 1200 "$P/$sub.json" "$OUT/$sub.jsonl"
          check bench "$P/$sub.json" "$OUT/$sub.jsonl" "$FAKE_STATE" "$PRIV" ) > "$T/lane-$sub.log" 2>&1 &
    done
    wait
    for sub in e4a e4b e5a e5b e5c e5d; do echo "== sub-lane $sub"; cat "$T/lane-$sub.log"; done
}
lane_preround() {  # the real preround.sh, through the fake sudo, until it needs real capabilities
    lane_env preround
    mkdir -p "$T/home"; ln -sfn "$ROOT" "$T/home/bmbench"
    echo "+ preround.sh (HOME=$T/home, fake sudo on PATH)"
    HOME="$T/home" PATH="$FAKE/pathshim:$PATH" timeout -k 20 300 bash "$ROOT/harness/run/preround.sh"
    local rc=$?
    echo "+ exit $rc"
    local S="$ROOT/raw/preround/STATUS"
    cat "$S"
    # It must get through identity, reproducibility, topology, selftest and the exec of a tool,
    # and stop exactly where an unprivileged run must: the tool holds no capabilities.
    local want
    for want in "identity recheck" "identity exit=" "reproducibility: " "exec check"; do
        grep -q "$want" "$S" || echo "LANE-FAIL preround: no STATUS line '$want'"
    done
    grep -q "exec check FAILED (selftest=0 exec=0): uid=[0-9]*($(id -un))" "$S" \
        || echo "LANE-FAIL preround: the exec check did not fail for the expected reason only (capabilities)"
    grep -q "^bmbench-priv version 2$" "$ROOT/raw/preround/selftest.txt" || echo "LANE-FAIL preround: selftest output"
    [[ $rc -eq 1 ]] || echo "LANE-FAIL preround: exit $rc, expected 1 (aborted at the capability check)"
    "$PRIV" topo-down > /dev/null
}

for l in $LANES; do declare -F "lane_$l" > /dev/null || { say "FAIL: no lane '$l'"; finish 1; }; done
say "lanes: $LANES (python: $PY)"
for l in $LANES; do
    ( "lane_$l" ) > "$T/lane-$l.log" 2>&1 &
done
wait
stop_fakes

# --------------------------------------------------------------------------- verdict
fail=0
for l in $LANES; do
    lf="$T/lane-$l.log"
    if grep -q "LANE-FAIL" "$lf"; then
        say "lane $l: FAILED"; grep -h "LANE-FAIL\|^FAIL" "$lf" | head -20 | sed 's/^/    /'; fail=1
    else
        say "lane $l: ok"
    fi
done
if grep -l "Traceback (most recent call last)" "$T"/lane-*.log > /dev/null 2>&1; then
    say "Python traceback in: $(grep -l 'Traceback (most recent call last)' "$T"/lane-*.log | xargs -n1 basename | tr '\n' ' ')"
    fail=1
fi
for f in "$OUT"/*.jsonl; do
    [[ "$f" == */faults.jsonl || "$f" == */dead.jsonl || "$f" == */iperfdead.jsonl || "$f" == */entries.jsonl ]] \
        && continue   # expected errors, checked above
    if grep -q '"record": "error"' "$f"; then say "error record in $(basename "$f")"; fail=1; fi
done
grep -h '^NOTE' "$T"/lane-*.log | sort -u | sed 's/^/    /'
if [[ $fail -eq 0 ]]; then say "DRY RUN OK ($(( $(date +%s) - T0 )) s)"; else say "DRY RUN FAILED"; fi
finish $fail
