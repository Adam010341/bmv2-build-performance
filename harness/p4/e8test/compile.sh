#!/usr/bin/env bash
# Compile e8test.p4 exactly as ../compile.sh compiles the generated programs (run on the laptop,
# which has p4c) and record the compiler identity next to the JSON.
set -euo pipefail
cd "$(dirname "$0")"
P4C="$(command -v p4c-bm2-ss)"
{ echo "p4c_path=$P4C"; echo "p4c_version=$($P4C --version 2>&1 | grep -m1 Version)"; echo "p4c_sha256=$(sha256sum "$P4C" | cut -d' ' -f1)"; } > COMPILER
"$P4C" --arch v1model --std p4-16 -o e8test.json e8test.p4
sha256sum e8test.p4 e8test.json entries.txt > SHA256SUMS
echo "compiled e8test.json"
