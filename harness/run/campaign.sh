#!/usr/bin/env bash
# The whole measurement campaign, unattended (Adam 2026-10-20: "pre-round passed => run on"):
#   pre-round -> objective pre-round checks -> E1, E1-RT, E2, E3 -> E1 capacities ->
#   E4, E5 plans from them -> E4, E5 -> CPU mode restored.
# Stops at the first failed check; the CPU mode is restored on every exit path.
# Progress: ~/bmbench/logs/campaign.log. Run under setsid on testbed.
set -uo pipefail
BMB="$HOME/bmbench"; H="$BMB/harness"; R="$BMB/raw"; L="$BMB/logs"
PRIV="sudo -n /usr/local/sbin/bmbench-priv"
mkdir -p "$R" "$L"
say() { echo "$(date '+%F %T') $*" | tee -a "$L/campaign.log"; }
trap '$PRIV cpu-mode restore >> "$L/campaign.log" 2>&1; say "cpu-mode restored; campaign exits"' EXIT

say "campaign start, harness $(cat "$H/HARNESS-GIT")"
[[ -d "$R/preround" ]] && mv "$R/preround" "$R/preround.before-$(date +%Y%m%d-%H%M%S)"
bash "$H/run/preround.sh" > "$L/preround.log" 2>&1
say "pre-round: $(tail -1 "$R/preround/STATUS")"
if ! python3 "$H/run/check_preround.py" "$R/preround" > "$R/preround/CHECK" 2>&1; then
    say "pre-round checks FAILED -- stopping; see raw/preround/CHECK"; exit 1
fi
say "pre-round checks passed"

for x in e1 e1rt e2 e3; do
    [[ -e "$R/$x.jsonl" ]] && { say "$x: raw/$x.jsonl exists, not overwriting -- stopping"; exit 1; }
    say "$x start ($(python3 -c "import json;print(len(json.load(open('$H/plans/$x.json'))['instances']))" ) instances)"
    python3 -u "$H/run/bench.py" "$H/plans/$x.json" "$R/$x.jsonl" > "$L/$x.log" 2>&1
    say "$x exit=$? errors=$(grep -c '"record": "error"' "$R/$x.jsonl")"
done

python3 "$H/run/analyse.py" "$R/e1.jsonl" --caps-out "$R/e1-caps.json" --json "$R/e1-analysis.json" > "$R/e1-analysis.txt" 2>&1 \
    || { say "E1 analysis failed -- stopping before E4/E5"; exit 1; }
python3 "$H/run/make_plans.py" "$R/plans-from-e1" --capacity "$R/e1-caps.json" > "$L/make_plans.log" 2>&1 \
    || { say "E4/E5 planning failed: $(tail -1 "$L/make_plans.log")"; exit 1; }
for x in e4 e5; do
    [[ -e "$R/$x.jsonl" ]] && { say "$x: raw/$x.jsonl exists, not overwriting -- stopping"; exit 1; }
    say "$x start"
    python3 -u "$H/run/bench.py" "$R/plans-from-e1/$x.json" "$R/$x.jsonl" > "$L/$x.log" 2>&1
    say "$x exit=$? errors=$(grep -c '"record": "error"' "$R/$x.jsonl")"
done
say "CAMPAIGN DONE"
