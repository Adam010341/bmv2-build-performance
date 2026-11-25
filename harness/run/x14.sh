#!/usr/bin/env bash
# AMENDMENT-14 (c) on testbed (exploratory, post-data): the X14 plan (one iperf3 TCP stream per
# instance) in bench mode, with X12's pre-round as its ceilings (raw/x12/preround must have passed).
# cpu-mode is restored on every exit. Refuses to overwrite, and refuses to start while the machine is
# busy (another project's jobs share testbed).
# Start: ssh -n testbed '(setsid nohup bash ~/bmbench/harness/run/x14.sh > /dev/null 2>&1 < /dev/null &)'
set -uo pipefail
BMB="$HOME/bmbench"; H="$BMB/harness"; OUT="$BMB/raw/x14"
PRIV="sudo -n /usr/local/sbin/bmbench-priv"
mkdir -p "$OUT"
st() { echo "$(date '+%F %T') $*" | tee -a "$OUT/STATUS"; }
trap '$PRIV cpu-mode restore >> "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode show >> "$OUT/cpu-mode.txt" 2>&1; st "cpu-mode: $($PRIV cpu-mode show | tr "\n" " "); X14 END"' EXIT
cd "$BMB"

st "x14 start, harness $(cat "$H/HARNESS-GIT")"
load=$(cut -d' ' -f1 /proc/loadavg)
if awk -v l="$load" 'BEGIN { exit !(l > 0.5) }'; then
    st "machine busy (loadavg $load) -- stopping; top: $(ps -eo pcpu,comm --sort=-pcpu | sed -n 2,4p | tr -s ' ' | tr '\n' ';')"
    exit 1
fi
[[ -e "$OUT/x14.jsonl" ]] && { st "x14.jsonl exists, not overwriting -- stopping"; exit 1; }
chk="$BMB/raw/x12/preround/CHECK"
if [[ ! -f "$chk" ]] || grep -q FAIL "$chk"; then st "X12 pre-round missing or failed ($chk) -- stopping"; exit 1; fi
for b in cm-default at-docs-perf at-tutorial; do
    [[ -f "builds/$b/BUILD-INFO" ]] || { st "build $b missing -- stopping"; exit 1; }
done
st "X12 pre-round passed; builds present"
if [[ ! -e /run/netns/bmb-sw ]]; then
    $PRIV topo-up 4 > "$OUT/topo.txt" 2>&1 || { st "topo-up FAILED"; exit 1; }
    st "topology was down: topo-up 4"
fi

$PRIV cpu-mode bench >> "$OUT/cpu-mode.txt" 2>&1; sleep 5
now=$($PRIV cpu-mode show | tr '\n' ' ')
st "mode bench: $now"
[[ $now == *"12 performance"*"no_turbo: 1"* ]] || { st "bench mode not set -- stopping"; exit 1; }
st "x14 plan start ($(python3 -c "import json;print(len(json.load(open('$H/plans/x14.json'))['instances']))") instances)"
python3 -u "$H/run/bench.py" "$H/plans/x14.json" "$OUT/x14.jsonl" > "$OUT/x14.log" 2>&1
st "x14 exit=$? errors=$(grep -c '"record": "error"' "$OUT/x14.jsonl")"
st "X14 DONE"
