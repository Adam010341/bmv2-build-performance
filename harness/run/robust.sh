#!/usr/bin/env bash
# The §6 robustness checks on testbed (PREREG AMENDMENT-11): a fresh pre-round into raw/robust/preround
# (stops if its checks fail), then R1 (overload), R2 (trial length) and R4 (default CPU mode, the two
# modes alternating per replicate: bench first for even replicates). R3 (two port pairs) is run by
# robust.sh r3 once its harness code is in. cpu-mode is restored on every exit. Refuses to overwrite.
# Start: ssh -n testbed '(setsid nohup bash ~/bmbench/harness/run/robust.sh > /dev/null 2>&1 < /dev/null &)'
set -uo pipefail
BMB="$HOME/bmbench"; H="$BMB/harness"; OUT="$BMB/raw/robust"
PRIV="sudo -n /usr/local/sbin/bmbench-priv"
WHAT="${1:-main}"                                   # main | r3
mkdir -p "$OUT"
st() { echo "$(date '+%F %T') $*" | tee -a "$OUT/STATUS"; }
trap '$PRIV cpu-mode restore >> "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode show >> "$OUT/cpu-mode.txt" 2>&1; st "cpu-mode: $($PRIV cpu-mode show | tr "\n" " "); ROBUST $WHAT END"' EXIT
cd "$BMB"

# mode bench|default: switch only when needed (restore without a saved state fails), then verify
mode() {
    local turbo; turbo=$($PRIV cpu-mode show | sed -n 's/^no_turbo: //p')
    if [[ $1 == bench && $turbo != 1 ]]; then $PRIV cpu-mode bench >> "$OUT/cpu-mode.txt" 2>&1; sleep 5; fi
    if [[ $1 == default && $turbo == 1 ]]; then $PRIV cpu-mode restore >> "$OUT/cpu-mode.txt" 2>&1; sleep 5; fi
    local now; now=$($PRIV cpu-mode show | tr '\n' ' ')
    st "mode $1: $now"
    if [[ $1 == bench ]]; then [[ $now == *"12 performance"*"no_turbo: 1"* ]]; else [[ $now == *"no_turbo: 0"* ]]; fi
}

run() {                                             # run PLAN: plans/<PLAN>.json -> raw/robust/<PLAN>.jsonl (make_plans names: r1 r2 r3 r4benchb0 ...)
    [[ -e "$OUT/$1.jsonl" ]] && { st "$1: exists, not overwriting -- stopping"; exit 1; }
    st "$1 start ($(python3 -c "import json;print(len(json.load(open('$H/plans/$1.json'))['instances']))") instances)"
    python3 -u "$H/run/bench.py" "$H/plans/$1.json" "$OUT/$1.jsonl" > "$OUT/$1.log" 2>&1
    st "$1 exit=$? errors=$(grep -c '"record": "error"' "$OUT/$1.jsonl")"
}

st "robust $WHAT start, harness $(cat "$H/HARNESS-GIT")"
if [[ $WHAT == r3 ]]; then
    $PRIV topo-down >/dev/null 2>&1; $PRIV topo-up 4 > "$OUT/topo-r3.txt" 2>&1 || { st "topo-up FAILED"; exit 1; }
    mode bench || { st "bench mode not set -- stopping"; exit 1; }
    run r3
    exit 0
fi

[[ -e "$OUT/preround" ]] && { st "preround exists -- stopping"; exit 1; }
PREROUND_OUT="$OUT/preround" bash "$H/run/preround.sh" > "$OUT/preround.log" 2>&1
st "pre-round: $(tail -1 "$OUT/preround/STATUS")"
if ! python3 "$H/run/check_preround.py" "$OUT/preround" > "$OUT/preround/CHECK" 2>&1; then
    st "pre-round checks FAILED -- stopping; see preround/CHECK"; exit 1
fi
st "pre-round checks passed"

mode bench || { st "bench mode not set -- stopping"; exit 1; }
run r1
run r2
for r in 0 1 2 3 4; do
    if (( r % 2 == 0 )); then order="bench default"; else order="default bench"; fi
    for m in $order; do
        mode "$m" || { st "mode $m not set -- stopping"; exit 1; }
        run "r4${m}b$r"
    done
done
st "ROBUST MAIN DONE"
