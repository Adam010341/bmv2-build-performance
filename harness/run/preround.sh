#!/usr/bin/env bash
# Pre-round sequence (PREREG v1 + AMENDMENT-1), run on testbed after the build matrix:
#   identity recheck + reproducibility vs builds.v0, bench CPU mode, G1, G9, G4 controls, G10, smoke.
# Each step logs to ~/bmbench/raw/preround/; the step list and its outcome go to STATUS.
set -uo pipefail
BMB="$HOME/bmbench"; H="$BMB/harness"; OUT="${PREROUND_OUT:-$BMB/raw/preround}"; mkdir -p "$OUT"   # robust.sh sets PREROUND_OUT
PRIV="sudo -n /usr/local/sbin/bmbench-priv"
st() { echo "$(date '+%F %T') $*" | tee -a "$OUT/STATUS"; }
cd "$BMB"

st "identity recheck"
bash "$H/builds/recheck_identity.sh" > "$OUT/identity.txt" 2>&1; st "identity exit=$?"
for d in builds/*/; do
    id=$(basename "$d"); new=$(sed -n 's/^binary_sha256=//p' "$d/BUILD-INFO" 2>/dev/null)
    old=$(sed -n 's/^binary_sha256=//p' "builds.v0/$id/BUILD-INFO" 2>/dev/null)
    echo "$id new=${new:0:12} old=${old:0:12} $([[ -n "$old" && "$new" == "$old" ]] && echo identical || echo DIFFERENT)"
done > "$OUT/reproducibility.txt"
st "reproducibility: $(grep -c identical "$OUT/reproducibility.txt") identical, $(grep -c DIFFERENT "$OUT/reproducibility.txt") different"

$PRIV topo-down >/dev/null 2>&1; $PRIV topo-up 4 > "$OUT/topo.txt" 2>&1 || { st "topo-up FAILED"; exit 1; }
# Fail fast if the wrapper cannot start a tool: in the first pre-round (10-20, wrapper v1) every
# exec failed and G10 alone spent 11 minutes timing out on switches that never started.
st "exec check"
$PRIV selftest > "$OUT/selftest.txt" 2>&1; rc_self=$?
$PRIV exec bmb-h0 0 "$H/run/execcheck.sh" > "$OUT/execcheck.txt" 2>&1; rc_exec=$?
if [[ $rc_self -ne 0 || $rc_exec -ne 0 ]] || ! grep -q '^bmbench-priv version 2$' "$OUT/selftest.txt" \
    || ! grep -q '^uid=[0-9]*(adam)' "$OUT/execcheck.txt" \
    || ! grep -qP '^CapEff:\t0000000000003000$' "$OUT/execcheck.txt" \
    || ! grep -qP '^CapAmb:\t0000000000003000$' "$OUT/execcheck.txt"; then
    st "exec check FAILED (selftest=$rc_self exec=$rc_exec): $(head -c 300 "$OUT/execcheck.txt" | tr '\n' ' ')"
    st "PREROUND ABORTED"; exit 1
fi
st "exec check ok"
$PRIV cpu-mode bench > "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode show >> "$OUT/cpu-mode.txt" 2>&1
st "cpu-mode: $(tr '\n' ' ' < "$OUT/cpu-mode.txt")"
sleep 5

st "G1 calibration"; python3 -u "$H/run/arms.py" calibrate "$OUT/g1-calibration.jsonl" > "$OUT/g1.log" 2>&1; st "G1 exit=$?"
st "G9 null forwarder"; python3 -u "$H/run/arms.py" null "$OUT/g9-null.jsonl" > "$OUT/g9.log" 2>&1; st "G9 exit=$?"
st "G4 controls"; python3 -u "$H/run/arms.py" controls cm-o3-L0-E0 "$OUT/g4-controls.jsonl" > "$OUT/g4.log" 2>&1; st "G4 exit=$?"
st "G10 hitcheck"; python3 -u "$H/run/bench.py" "$H/plans/hitcheck.json" "$OUT/g10-hitcheck.jsonl" > "$OUT/g10.log" 2>&1; st "G10 exit=$?"
st "smoke"; python3 -u "$H/run/bench.py" "$H/plans/smoke.json" "$OUT/smoke.jsonl" > "$OUT/smoke.log" 2>&1; st "smoke exit=$?"
st "PREROUND DONE"
