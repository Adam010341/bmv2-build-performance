#!/usr/bin/env bash
# Compile every generated program with p4c-bm2-ss (run on the laptop, which has p4c).
# Records the compiler identity next to the JSON, so a run can name what produced its program.
set -euo pipefail
cd "$(dirname "$0")"
python3 gen_p4.py build
P4C="$(command -v p4c-bm2-ss)"
{ echo "p4c_path=$P4C"; echo "p4c_version=$($P4C --version 2>&1 | grep -m1 Version)"; echo "p4c_sha256=$(sha256sum "$P4C" | cut -d' ' -f1)"; } > build/COMPILER
for f in build/*.p4; do
  "$P4C" --arch v1model --std p4-16 -o "${f%.p4}.json" "$f"
done
sha256sum build/*.json > build/SHA256SUMS
echo "compiled $(ls build/*.json | wc -l) programs"
