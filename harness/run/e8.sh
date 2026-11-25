#!/usr/bin/env bash
# E8 run on testbed (PREREG AMENDMENT-7/9): fresh topology, bench CPU mode, G10 against each
# program's declared path profile (plans/e8hit.json), and only if every program passes it the
# measured plan (plans/e8.json); cpu-mode is restored on every exit. G1/G9 ceilings are the
# 2026-10-21 pre-round's (E8 rates stay far below them). Refuses to overwrite raw/e8/.
# Start: ssh -n testbed '(setsid nohup bash ~/bmbench/harness/run/e8.sh > /dev/null 2>&1 < /dev/null &)'
set -uo pipefail
BMB="$HOME/bmbench"; H="$BMB/harness"; OUT="$BMB/raw/e8"
PRIV="sudo -n /usr/local/sbin/bmbench-priv"
[[ -e "$OUT" ]] && { echo "$OUT exists -- refusing to overwrite"; exit 1; }
mkdir -p "$OUT"
st() { echo "$(date '+%F %T') $*" | tee -a "$OUT/STATUS"; }
trap '$PRIV cpu-mode restore >> "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode show >> "$OUT/cpu-mode.txt" 2>&1; st "cpu-mode restored; E8 END"' EXIT
cd "$BMB"
st "E8 start, harness $(cat "$H/HARNESS-GIT")"
$PRIV topo-down >/dev/null 2>&1; $PRIV topo-up 4 > "$OUT/topo.txt" 2>&1 || { st "topo-up FAILED"; exit 1; }
$PRIV cpu-mode bench > "$OUT/cpu-mode.txt" 2>&1; $PRIV cpu-mode show >> "$OUT/cpu-mode.txt" 2>&1
st "cpu-mode: $(tr '\n' ' ' < "$OUT/cpu-mode.txt")"
sleep 5
st "G10 (E8HIT)"
python3 -u "$H/run/bench.py" "$H/plans/e8hit.json" "$OUT/e8hit.jsonl" > "$OUT/e8hit.log" 2>&1; st "E8HIT exit=$?"
if ! python3 -I - "$OUT/e8hit.jsonl" > "$OUT/e8hit-CHECK" 2>&1 <<'PY'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1])]
inst = [r for r in recs if r.get("record") == "instance"]
errs = [r for r in recs if r.get("record") == "error"]
for r in inst:
    print(r["instance"], "probe_ok", r["probe_ok"], "hitcheck_ok", r.get("hitcheck_ok"))
for r in errs:
    print("ERROR", r["instance"], r["error"][:300])
ok = len(inst) == 2 and not errs and all(r["probe_ok"] and r.get("hitcheck_ok") for r in inst)
print("E8HIT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
PY
then st "E8HIT FAILED -- not measuring: $(tr '\n' ' ' < "$OUT/e8hit-CHECK")"; exit 1; fi
st "E8HIT: $(tail -1 "$OUT/e8hit-CHECK")"
st "E8 measured ($(python3 -c "import json;print(len(json.load(open('$H/plans/e8.json'))['instances']))") instances)"
python3 -u "$H/run/bench.py" "$H/plans/e8.json" "$OUT/e8.jsonl" > "$OUT/e8.log" 2>&1; st "E8 exit=$?"
