#!/usr/bin/env bash
# Copy the harness and the compiled P4 programs to testbed and build the tools there.
set -euo pipefail
cd "$(dirname "$0")"
HOST="${1:-testbed}"
GIT="$(git rev-parse --short HEAD)$(git diff --quiet -- . || echo -dirty)"
ssh -o BatchMode=yes "$HOST" 'mkdir -p ~/bmbench/harness ~/bmbench/p4'
rsync -a --delete --exclude bin/ --exclude p4/build/ ./ "$HOST:bmbench/harness/"
rsync -a --delete p4/build/ "$HOST:bmbench/p4/"
ssh -o BatchMode=yes "$HOST" "cd ~/bmbench/harness && echo '$GIT' > HARNESS-GIT && make -s && bash tests/test_offline.sh | tail -1"
