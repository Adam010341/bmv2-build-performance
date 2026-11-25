#!/bin/sh
# Run through `bmbench-priv exec` by preround.sh: shows the identity and capabilities a tool gets.
id
grep -E '^Cap(Prm|Eff|Amb)' /proc/self/status
