#!/usr/bin/env bash
# Offline test of the frame format and the receiver's per-flow accounting. No privileges needed:
# bmgen writes frames to a pcap file, and both bmrecv and tcpdump read it back.
set -euo pipefail
cd "$(dirname "$0")/.."
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail=0
check() { if [[ "$2" == "$3" ]]; then echo "  ok   $1 = $2"; else echo "  FAIL $1: got '$2' want '$3'"; fail=1; fi; }
jget() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2],{},{'d':d}))" "$1" "$2"; }

for frame in 64 512 1518; do
  echo "frame $frame, 4 flows, 24 frames"
  bin/bmgen --frame "$frame" --flows 4 --dump "$T/f$frame.pcap" --dump-count 24 > "$T/g$frame.json"
  bin/bmrecv --pcap-in "$T/f$frame.pcap" > "$T/r$frame.json"
  check "buflen" "$(jget "$T/g$frame.json" "d['buflen']")" "$((frame - 4))"
  check "rx" "$(jget "$T/r$frame.json" "d['rx']")" 24
  check "foreign" "$(jget "$T/r$frame.json" "d['foreign']")" 0
  check "flows" "$(jget "$T/r$frame.json" "len(d['flows'])")" 4
  check "per-flow rx" "$(jget "$T/r$frame.json" "[f['rx'] for f in d['flows']]")" "[6, 6, 6, 6]"
  check "per-flow max_seq" "$(jget "$T/r$frame.json" "[f['max_seq'] for f in d['flows']]")" "[5, 5, 5, 5]"
  check "ooo+dup" "$(jget "$T/r$frame.json" "sum(f['ooo']+f['dup'] for f in d['flows'])")" 0
  if command -v tcpdump >/dev/null; then
    # independent decoder: IPv4 length and UDP ports must be what bm_build wrote
    line="$(tcpdump -nn -v -r "$T/f$frame.pcap" 2>/dev/null | head -2 | tr '\n' ' ')"
    check "tcpdump ip len" "$(grep -o 'length [0-9]*)' <<<"$line" | head -1 | tr -dc 0-9)" "$((frame - 4 - 14))"
    check "tcpdump ports" "$(grep -o '10.0.0.1.10000 > 10.0.1.1.5001' <<<"$line" | head -1)" "10.0.0.1.10000 > 10.0.1.1.5001"
    if grep -q 'bad cksum' <<<"$line"; then check "ip checksum" bad good; else check "ip checksum" good good; fi
  fi
  # run id: written by bmgen --run-id, filtered by bmrecv --run-id
  bin/bmgen --frame "$frame" --flows 4 --run-id 7 --dump "$T/r7_$frame.pcap" --dump-count 24 > "$T/g7_$frame.json"
  check "gen run_id" "$(jget "$T/g7_$frame.json" "d['run_id']")" 7
  check "gen buflen (run 7)" "$(jget "$T/g7_$frame.json" "d['buflen']")" "$((frame - 4))"
  # the run id must be in the frame bytes: UDP payload offset 16, big-endian (pcap hdr 24 + rec hdr 16 + 42)
  check "run id bytes @ payload+16" "$(python3 - "$T/r7_$frame.pcap" <<'PY'
import sys
b = open(sys.argv[1], 'rb').read()
p = 24 + 16 + 42
print(b[p + 16:p + 18].hex(), b[p:p + 2].hex(), b[p + 2:p + 4].hex())
PY
)" "0007 b2b2 0000"
  bin/bmrecv --pcap-in "$T/r7_$frame.pcap" --run-id 7 > "$T/rr7_$frame.json"
  check "run 7 rx" "$(jget "$T/rr7_$frame.json" "d['rx']")" 24
  check "run 7 stale" "$(jget "$T/rr7_$frame.json" "d['stale']")" 0
  check "run 7 foreign" "$(jget "$T/rr7_$frame.json" "d['foreign']")" 0
  check "run 7 filter" "$(jget "$T/rr7_$frame.json" "d['run_id_filter']")" 7
  check "run 7 per-flow rx" "$(jget "$T/rr7_$frame.json" "[f['rx'] for f in d['flows']]")" "[6, 6, 6, 6]"
  check "run 7 per-flow max_seq" "$(jget "$T/rr7_$frame.json" "[f['max_seq'] for f in d['flows']]")" "[5, 5, 5, 5]"
  bin/bmrecv --pcap-in "$T/r7_$frame.pcap" --run-id 8 > "$T/rr8_$frame.json"
  check "run 8 rx" "$(jget "$T/rr8_$frame.json" "d['rx']")" 0
  check "run 8 stale" "$(jget "$T/rr8_$frame.json" "d['stale']")" 24
  check "run 8 flows" "$(jget "$T/rr8_$frame.json" "len(d['flows'])")" 0
  check "run 8 lat_n" "$(jget "$T/rr8_$frame.json" "d['lat_n']")" 0
  bin/bmrecv --pcap-in "$T/r7_$frame.pcap" > "$T/rrall_$frame.json"
  check "no filter rx (run 7 file)" "$(jget "$T/rrall_$frame.json" "d['rx']")" 24
  check "no filter stale" "$(jget "$T/rrall_$frame.json" "d['stale']")" 0
  check "no filter run_id_filter" "$(jget "$T/rrall_$frame.json" "d['run_id_filter']")" None
done
# a foreign frame (wrong magic) must be counted as foreign, not as ours
python3 - "$T/f64.pcap" "$T/bad.pcap" <<'PY'
import sys
b = bytearray(open(sys.argv[1], 'rb').read())
b[24 + 16 + 42] ^= 0xff          # first record's payload magic byte
open(sys.argv[2], 'wb').write(b)
PY
bin/bmrecv --pcap-in "$T/bad.pcap" > "$T/rb.json"
echo "corrupted magic"
check "foreign" "$(jget "$T/rb.json" "d['foreign']")" 1
check "rx" "$(jget "$T/rb.json" "d['rx']")" 23

# two runs in one capture: only the requested run is accounted, the other is stale
bin/bmgen --frame 64 --flows 4 --run-id 8 --dump "$T/r8.pcap" --dump-count 24 > /dev/null
{ cat "$T/r7_64.pcap"; tail -c +25 "$T/r8.pcap"; } > "$T/mixed.pcap"
bin/bmrecv --pcap-in "$T/mixed.pcap" --run-id 7 > "$T/rm.json"
echo "mixed capture, --run-id 7"
check "rx" "$(jget "$T/rm.json" "d['rx']")" 24
check "stale" "$(jget "$T/rm.json" "d['stale']")" 24
check "per-flow rx" "$(jget "$T/rm.json" "[f['rx'] for f in d['flows']]")" "[6, 6, 6, 6]"
check "lat_n" "$(jget "$T/rm.json" "d['lat_n']")" 24
bin/bmrecv --pcap-in "$T/mixed.pcap" > "$T/rm_all.json"
echo "mixed capture, no filter"
check "rx" "$(jget "$T/rm_all.json" "d['rx']")" 48
check "stale" "$(jget "$T/rm_all.json" "d['stale']")" 0

# --tos sets only the IPv4 TOS byte (and the checksum); the default stays 0 (AMENDMENT-9)
echo "tos"
bin/bmgen --frame 64 --flows 2 --tos 0x02 --dump "$T/tos2.pcap" --dump-count 4 > /dev/null
bin/bmgen --frame 64 --flows 2 --tos 0 --dump "$T/tos0.pcap" --dump-count 4 > /dev/null
bin/bmgen --frame 64 --flows 2 --dump "$T/tosd.pcap" --dump-count 4 > /dev/null
hdr() { python3 - "$1" <<'PY'
import sys
b = open(sys.argv[1], 'rb').read()
p = 24 + 16                       # first record: Ethernet + IPv4 + UDP + marker magic/flow
print(b[p:p + 46].hex())
PY
}
check "tos byte (--tos 0x02)" "$(hdr "$T/tos2.pcap" | cut -c31-32)" 02
check "tos byte (default)" "$(hdr "$T/tosd.pcap" | cut -c31-32)" 00
check "--tos 0 = default (headers)" "$(hdr "$T/tos0.pcap")" "$(hdr "$T/tosd.pcap")"
check "tos 2 differs only in tos + checksum" "$(python3 - "$(hdr "$T/tos2.pcap")" "$(hdr "$T/tosd.pcap")" <<'PY'
import sys
a, b = bytes.fromhex(sys.argv[1]), bytes.fromhex(sys.argv[2])
print([i for i in range(len(a)) if a[i] != b[i] and i not in (24, 25)])   # 24-25 = IPv4 checksum
PY
)" "[15]"
check "tos 2 bmrecv rx" "$(bin/bmrecv --pcap-in "$T/tos2.pcap" | python3 -c 'import json,sys; print(json.load(sys.stdin)["rx"])')" 4
if command -v tcpdump >/dev/null; then
  line="$(tcpdump -nn -v -r "$T/tos2.pcap" 2>/dev/null | head -1)"
  check "tcpdump tos" "$(grep -o 'tos 0x2' <<<"$line")" "tos 0x2"
  if grep -q 'bad cksum' <<<"$line"; then check "tos 2 ip checksum" bad good; else check "tos 2 ip checksum" good good; fi
fi
rc=0; bin/bmgen --frame 64 --tos 256 --dump "$T/x.pcap" >/dev/null 2>&1 || rc=$?
check "bmgen --tos 256 exit" "$rc" 2

# argument validation
echo "argument checks"
rc=0; bin/bmgen --frame 64 --run-id 65536 --dump "$T/x.pcap" >/dev/null 2>&1 || rc=$?
check "bmgen --run-id 65536 exit" "$rc" 2
rc=0; bin/bmrecv --pcap-in "$T/f64.pcap" --run-id -1 >/dev/null 2>&1 || rc=$?
check "bmrecv --run-id -1 exit" "$rc" 2
# bmnull needs privileges and two real interfaces, so only its build and usage are checked
[[ -x bin/bmnull ]] && check "bmnull built" yes yes || check "bmnull built" no yes
rc=0; bin/bmnull >"$T/null.out" 2>"$T/null.err" || rc=$?
check "bmnull no-args exit" "$([[ $rc -ne 0 ]] && echo nonzero || echo zero)" nonzero
check "bmnull usage message" "$(grep -c '^usage: bmnull' "$T/null.err")" 1
[[ $fail -eq 0 ]] && echo "ALL OK" || { echo "FAILED"; exit 1; }
