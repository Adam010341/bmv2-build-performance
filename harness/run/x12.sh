#!/usr/bin/env bash
# AMENDMENT-12 on testbed (exploratory, post-data): build at-tutorial twice (builds.v0 + builds, so the
# pre-round's reproducibility check covers it), a fresh pre-round into raw/x12/preround (stops if its
# checks fail), then the X12 plan in bench mode. cpu-mode is restored on every exit. Refuses to
# overwrite, and refuses to start while the machine is busy (another project's jobs share testbed).
# Start: ssh -n testbed '(setsid nohup bash ~/bmbench/harness/run/x12.sh > /dev/null 2>&1 < /dev/null &)'
set -uo pipefail
BMB="$HOME/bmbench"; H="$BMB/harness"; OUT="$BMB/raw/x12"
PRIV="sudo -n /usr/local/sbin/bmbench-priv"
mkdir -p "$OUT"
st() { echo "$(date '+%F %T') $*" | tee -a "$OUT/STATUS"; }
trap '$PRIV cpu-mode restore >> "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode show >> "$OUT/cpu-mode.txt" 2>&1; st "cpu-mode: $($PRIV cpu-mode show | tr "\n" " "); X12 END"' EXIT
cd "$BMB"

st "x12 start, harness $(cat "$H/HARNESS-GIT")"
load=$(cut -d' ' -f1 /proc/loadavg)
if awk -v l="$load" 'BEGIN { exit !(l > 0.5) }'; then
    st "machine busy (loadavg $load) -- stopping; top: $(ps -eo pcpu,comm --sort=-pcpu | sed -n 2,4p | tr -s ' ' | tr '\n' ';')"
    exit 1
fi
[[ -e "$OUT/x12.jsonl" ]] && { st "x12.jsonl exists, not overwriting -- stopping"; exit 1; }

if [[ ! -f builds/at-tutorial/BUILD-INFO ]]; then
    if [[ ! -f builds.v0/at-tutorial/BUILD-INFO ]]; then
        bash "$H/builds/build_matrix.sh" at-tutorial > "$OUT/build-v0.log" 2>&1
        [[ -f builds/at-tutorial/BUILD-INFO ]] || { st "at-tutorial build FAILED (build-v0.log)"; exit 1; }
        mv builds/at-tutorial builds.v0/at-tutorial
    fi
    bash "$H/builds/build_matrix.sh" at-tutorial > "$OUT/build.log" 2>&1
    [[ -f builds/at-tutorial/BUILD-INFO ]] || { st "at-tutorial rebuild FAILED (build.log)"; exit 1; }
fi
recap="$(tr '\n' ' ' < builds/at-tutorial/configure-recap.txt)"
st "at-tutorial: sha $(sed -n 's/^binary_sha256=//p' builds/at-tutorial/BUILD-INFO | cut -c1-12), v0 $(sed -n 's/^binary_sha256=//p' builds.v0/at-tutorial/BUILD-INFO | cut -c1-12); recap: $recap"
if [[ "$recap" != *"CXXFLAGS='-O0 -g'"* && "$recap" != *"CXXFLAGS=-O0 -g"* ]]; then
    st "at-tutorial recap does not show CXXFLAGS=-O0 -g -- stopping"; exit 1
fi

[[ -e "$OUT/preround" ]] && { st "preround exists -- stopping"; exit 1; }
PREROUND_OUT="$OUT/preround" bash "$H/run/preround.sh" > "$OUT/preround.log" 2>&1
st "pre-round: $(tail -1 "$OUT/preround/STATUS")"
if ! python3 "$H/run/check_preround.py" "$OUT/preround" > "$OUT/preround/CHECK" 2>&1; then
    st "pre-round checks FAILED -- stopping; see preround/CHECK"; exit 1
fi
st "pre-round checks passed"

$PRIV cpu-mode bench >> "$OUT/cpu-mode.txt" 2>&1; sleep 5
now=$($PRIV cpu-mode show | tr '\n' ' ')
st "mode bench: $now"
[[ $now == *"12 performance"*"no_turbo: 1"* ]] || { st "bench mode not set -- stopping"; exit 1; }
st "x12 plan start ($(python3 -c "import json;print(len(json.load(open('$H/plans/x12.json'))['instances']))") instances)"
python3 -u "$H/run/bench.py" "$H/plans/x12.json" "$OUT/x12.jsonl" > "$OUT/x12.log" 2>&1
st "x12 exit=$? errors=$(grep -c '"record": "error"' "$OUT/x12.jsonl")"
st "X12 DONE"
