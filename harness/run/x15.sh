#!/usr/bin/env bash
# AMENDMENT-14 (d) on testbed (exploratory, post-data): one profiled sat trial per build (x15.py),
# in bench mode, with perf_event_paranoid 2 for the run. cpu-mode and paranoid 4 are restored on every
# exit. Refuses to overwrite, and refuses to start while the machine is busy.
# Start: ssh -n testbed '(setsid nohup bash ~/bmbench/harness/run/x15.sh > /dev/null 2>&1 < /dev/null &)'
set -uo pipefail
BMB="$HOME/bmbench"; H="$BMB/harness"; OUT="$BMB/raw/x15"
PRIV="sudo -n /usr/local/sbin/bmbench-priv"
mkdir -p "$OUT"
st() { echo "$(date '+%F %T') $*" | tee -a "$OUT/STATUS"; }
trap '$PRIV perf-paranoid 4 >> "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode restore >> "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode show >> "$OUT/cpu-mode.txt" 2>&1; st "cpu-mode: $($PRIV cpu-mode show | tr "\n" " ") paranoid $(cat /proc/sys/kernel/perf_event_paranoid); X15 END"' EXIT
cd "$BMB"

st "x15 start, harness $(cat "$H/HARNESS-GIT")"
load=$(cut -d' ' -f1 /proc/loadavg)
if awk -v l="$load" 'BEGIN { exit !(l > 0.5) }'; then
    st "machine busy (loadavg $load) -- stopping; top: $(ps -eo pcpu,comm --sort=-pcpu | sed -n 2,4p | tr -s ' ' | tr '\n' ';')"
    exit 1
fi
[[ -e "$OUT/x15.jsonl" ]] && { st "x15.jsonl exists, not overwriting -- stopping"; exit 1; }
for b in cm-default cm-o3-L1-E1 at-docs-perf; do
    [[ -f "builds/$b/BUILD-INFO" ]] || { st "build $b missing -- stopping"; exit 1; }
done
real=""
for p in "/usr/lib/linux-tools/$(uname -r)/perf" "/usr/lib/linux-tools-$(uname -r)/perf"; do
    [[ -x "$p" ]] && { real="$p"; break; }
done
[[ -n "$real" ]] || { st "no perf binary for kernel $(uname -r) -- stopping"; exit 1; }
mkdir -p "$BMB/local/bin"
cp "$real" "$BMB/local/bin/perf"
st "perf: $real -> local/bin/perf ($("$BMB/local/bin/perf" --version), sha $(sha256sum "$BMB/local/bin/perf" | cut -c1-12))"
if [[ ! -e /run/netns/bmb-sw ]]; then
    $PRIV topo-up 4 > "$OUT/topo.txt" 2>&1 || { st "topo-up FAILED"; exit 1; }
    st "topology was down: topo-up 4"
fi

$PRIV cpu-mode bench >> "$OUT/cpu-mode.txt" 2>&1; sleep 5
now=$($PRIV cpu-mode show | tr '\n' ' ')
st "mode bench: $now"
[[ $now == *"12 performance"*"no_turbo: 1"* ]] || { st "bench mode not set -- stopping"; exit 1; }
$PRIV perf-paranoid 2 >> "$OUT/cpu-mode.txt" 2>&1
st "perf_event_paranoid $(cat /proc/sys/kernel/perf_event_paranoid)"
python3 -u "$H/run/x15.py" "$OUT" > "$OUT/x15.log" 2>&1
st "x15 exit=$? errors=$(grep -c '"record": "error"' "$OUT/x15.jsonl")"
st "X15 DONE"
