#!/usr/bin/env bash
# X13 run on testbed (PREREG AMENDMENT-13, the E8 extension): fresh topology, bench CPU mode, G10 against each
# program's declared path profile (plans/x13hit.json), and only if every program passes it the
# measured plan (plans/x13.json), written as e8hit.jsonl / e8.jsonl so report_e8.py reads them; cpu-mode is
# restored on every exit. G1/G9 ceilings are X12's pre-round (must have passed). Refuses to overwrite raw/x13/.
# Start: ssh -n testbed '(setsid nohup bash ~/bmbench/harness/run/x13.sh > /dev/null 2>&1 < /dev/null &)'
set -uo pipefail
BMB="$HOME/bmbench"; H="$BMB/harness"; OUT="$BMB/raw/x13"
PRIV="sudo -n /usr/local/sbin/bmbench-priv"
[[ -e "$OUT" ]] && { echo "$OUT exists -- refusing to overwrite"; exit 1; }
mkdir -p "$OUT"
st() { echo "$(date '+%F %T') $*" | tee -a "$OUT/STATUS"; }
trap '$PRIV cpu-mode restore >> "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode show >> "$OUT/cpu-mode.txt" 2>&1; st "cpu-mode restored; X13 END"' EXIT
cd "$BMB"
load=$(cut -d' ' -f1 /proc/loadavg)
if awk -v l="$load" 'BEGIN { exit !(l > 0.5) }'; then
    st "machine busy (loadavg $load) -- stopping; top: $(ps -eo pcpu,comm --sort=-pcpu | sed -n 2,4p | tr -s ' ' | tr '\n' ';')"
    exit 1
fi
chk="$BMB/raw/x12/preround/CHECK"
if [[ ! -f "$chk" ]] || grep -q FAIL "$chk"; then st "X12 pre-round missing or failed ($chk) -- stopping"; exit 1; fi
st "X13 start, harness $(cat "$H/HARNESS-GIT")"
$PRIV topo-down >/dev/null 2>&1; $PRIV topo-up 4 > "$OUT/topo.txt" 2>&1 || { st "topo-up FAILED"; exit 1; }
$PRIV cpu-mode bench > "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode show >> "$OUT/cpu-mode.txt" 2>&1
st "cpu-mode: $(tr '\n' ' ' < "$OUT/cpu-mode.txt")"
sleep 5
st "G10 (E8HIT)"
python3 -u "$H/run/bench.py" "$H/plans/x13hit.json" "$OUT/e8hit.jsonl" > "$OUT/e8hit.log" 2>&1; st "E8HIT exit=$?"
if ! python3 -I - "$OUT/e8hit.jsonl" > "$OUT/e8hit-CHECK" 2>&1 <<'PY'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1])]
inst = [r for r in recs if r.get("record") == "instance"]
errs = [r for r in recs if r.get("record") == "error"]
for r in inst:
    print(r["instance"], "probe_ok", r["probe_ok"], "hitcheck_ok", r.get("hitcheck_ok"))
for r in errs:
    print("ERROR", r["instance"], r["error"][:300])
ok = len(inst) == 1 and not errs and all(r["probe_ok"] and r.get("hitcheck_ok") for r in inst)
print("E8HIT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
PY
then st "E8HIT FAILED -- not measuring: $(tr '\n' ' ' < "$OUT/e8hit-CHECK")"; exit 1; fi
st "E8HIT: $(tail -1 "$OUT/e8hit-CHECK")"
st "E8 measured ($(python3 -c "import json;print(len(json.load(open('$H/plans/x13.json'))['instances']))") instances)"
python3 -u "$H/run/bench.py" "$H/plans/x13.json" "$OUT/e8.jsonl" > "$OUT/e8.log" 2>&1; st "E8 exit=$?"
