#!/usr/bin/env bash
# check_preround.py: a complete passing pre-round passes; each single defect fails it.
set -uo pipefail
C="${CHECK:-$(cd "$(dirname "$0")/.." && pwd)/run/check_preround.py}"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mk() {
    rm -rf "$T/d"; mkdir -p "$T/d"
    printf '%s\n' "x identity exit=0" "x reproducibility: 18 identical, 0 different" "x exec check ok" \
        "x G1 exit=0" "x G9 exit=0" "x G4 exit=0" "x G10 exit=0" "x smoke exit=0" "x PREROUND DONE" > "$T/d/STATUS"
    echo '{"record":"calibration","rate":1}' > "$T/d/g1-calibration.jsonl"
    echo '{"record":"null-forwarder","delivered_pps_steady":500000}' > "$T/d/g9-null.jsonl"
    for c in none sibling-busy-loop short-lived-stream none-again; do
        f='[]'; [[ $c == none* ]] || f='["G4"]'
        echo "{\"record\":\"g4-control\",\"condition\":\"$c\",\"flags\":$f}"; done > "$T/d/g4-controls.jsonl"
    echo '{"record":"instance","probe_ok":true,"hitcheck_ok":true}' > "$T/d/g10-hitcheck.jsonl"
    echo '{"record":"instance","probe_ok":true}' > "$T/d/smoke.jsonl"
}
bad=0
expect() {  # $1 = want exit (0 pass / 1 fail), $2 = label
    python3 "$C" "$T/d" > /dev/null; rc=$?
    if [[ $rc != "$1" ]]; then echo "FAIL: $2 (exit $rc, want $1)"; bad=1; fi
}
mk; expect 0 "complete pre-round"
mk; sed -i 's/exec check ok/exec check FAILED/' "$T/d/STATUS"; expect 1 "exec check failed"
mk; sed -i 's/18 identical, 0 different/17 identical, 1 different/' "$T/d/STATUS"; expect 1 "a build not reproduced"
mk; sed -i 's/"short-lived-stream","flags":\["G4"\]/"short-lived-stream","flags":[]/' "$T/d/g4-controls.jsonl"; expect 1 "G4 control not firing"
mk; echo '{"record":"instance","probe_ok":true,"hitcheck_ok":false}' >> "$T/d/g10-hitcheck.jsonl"; expect 1 "G10 miss"
mk; echo '{"record":"null-forwarder","delivered_pps_steady":0}' >> "$T/d/g9-null.jsonl"; expect 1 "empty G9"
[[ $bad == 0 ]] && echo "test_check_preround OK"
exit $bad
