#!/bin/bash
# One-time root setup for the bmbench harness on testbed. Adam runs this ONCE, by hand:
#
#     sudo bash ~/bmbench-setup/setup_testbed.sh
#
# What it does, and nothing more:
#   1. apt-installs the build dependencies of bmv2 (CMake build, Thrift + nanomsg so that the
#      logging/elogger build knobs are real), iperf3 and trafgen (comparison generators), perf.
#   2. installs bmbench-priv (reviewed beside this file) as root:root 0755 in /usr/local/sbin.
#   3. adds ONE sudoers rule: adam may run /usr/local/sbin/bmbench-priv without a password.
#      The rule is checked with `visudo -cf` before it is copied into place.
#   4. creates /home/adam/bmbench owned by adam.
# It does not touch networking, CPU settings, or any existing sudoers file.
#
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_USER=adam

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
grep -q 'i5-12400F' /proc/cpuinfo || { echo "this is not testbed (no i5-12400F) -- refusing"; exit 1; }
[[ -f "$HERE/bmbench-priv" ]] || { echo "bmbench-priv not found next to this script"; exit 1; }

echo "== 1/4 apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    build-essential cmake ninja-build pkg-config autoconf automake libtool libtool-bin \
    libboost-dev libboost-program-options-dev libboost-system-dev libboost-filesystem-dev \
    libboost-thread-dev libgmp-dev libpcap-dev libjsoncpp-dev libxxhash-dev \
    libnanomsg-dev libssl-dev libevent-dev \
    libthrift-dev thrift-compiler python3-thrift python3-six \
    iperf3 netsniff-ng ethtool jq \
    linux-tools-common "linux-tools-$(uname -r)"

echo "== 2/4 bmbench-priv -> /usr/local/sbin"
install -o root -g root -m 0755 "$HERE/bmbench-priv" /usr/local/sbin/bmbench-priv
echo "   sha256 $(sha256sum /usr/local/sbin/bmbench-priv | cut -d' ' -f1)"

echo "== 3/4 sudoers rule"
tmp="$(mktemp)"
echo "$RUN_USER ALL=(root) NOPASSWD: /usr/local/sbin/bmbench-priv" > "$tmp"
visudo -cf "$tmp"
install -o root -g root -m 0440 "$tmp" /etc/sudoers.d/bmbench
rm -f "$tmp"
visudo -c >/dev/null && echo "   /etc/sudoers.d/bmbench installed; full sudoers still parses"

echo "== 4/4 /home/$RUN_USER/bmbench"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0755 "/home/$RUN_USER/bmbench"

echo
echo "DONE. Versions for the record:"
for p in libthrift-dev libnanomsg-dev libpcap-dev libboost-dev iperf3 netsniff-ng cmake g++; do
    printf '   %-16s %s\n' "$p" "$(dpkg-query -W -f='${Version}' "$p" 2>/dev/null || echo missing)"
done
echo "Next (agent, no password): sudo -n /usr/local/sbin/bmbench-priv selftest"
