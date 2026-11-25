#!/usr/bin/env bash
# Recompute the knob evidence for every built variant and compare it with what matrix.tsv says
# the variant should be. Prints one line per build and exits non-zero on any disagreement.
#   logging macros ON  <=> trace format strings present
#   elogger ON         <=> EventLogger symbols present
#   debugger ON        <=> DebuggerNN (the real debugger class) present
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; BMB="${BMB:-$HOME/bmbench}"; bad=0
printf '%-20s %-8s %-8s %-8s %s\n' id trace elog dbg verdict
while IFS=$'\t' read -r id system cflags options why; do
    [[ "$id" == \#* || -z "$id" ]] && continue
    info="$BMB/builds/$id/BUILD-INFO"; [[ -f "$info" ]] || { printf '%-20s not built\n' "$id"; continue; }
    bin="$(sed -n 's/^binary=//p' "$info")"
    t=$(strings "$bin" | grep -c -E 'Processing packet received on port|Parser set: setting field')
    e=$(nm -DC "$bin" 2>/dev/null | grep -c 'EventLogger::')
    d=$(strings "$bin" | grep -c DebuggerNN)
    # expected values from the recipe (defaults: macros ON, elogger ON, debugger OFF)
    wl=1; we=1; wd=0
    [[ "$options" == *LOGGING_MACROS=OFF* || "$options" == *--disable-logging-macros* ]] && wl=0
    [[ "$options" == *ELOGGER=OFF* || "$options" == *--disable-elogger* ]] && we=0
    [[ "$options" == *DEBUGGER=ON* || "$options" == *--enable-debugger* ]] && wd=1
    ok=OK
    (( (t > 0) == wl )) || ok="BAD-trace"; (( (e > 0) == we )) || ok="$ok BAD-elog"; (( (d > 0) == wd )) || ok="$ok BAD-dbg"
    [[ "$ok" == OK ]] || bad=1
    printf '%-20s %-8s %-8s %-8s %s\n' "$id" "$t" "$e" "$d" "$ok"
done < "$HERE/matrix.tsv"
exit $bad
