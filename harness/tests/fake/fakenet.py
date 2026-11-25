"""fakenet -- shared state of the dry-run test doubles (fake bmbench-priv, bmgen, bmrecv, bmnull,
simple_switch, iperf3). Imported by every fake tool; never used by the real harness.


The "network" is one JSON file, $FAKE_STATE/state.json, changed only under an exclusive flock:
  netns   {ns: {iface: {16 counters as in /proc/net/dev}}}   -- created by `topo-up`
  socks   {id: {pid, ns, if, kind, port, inbox}}              -- who reads what
            kind "packet": an AF_PACKET/pcap reader bound to (ns, if): sees every frame on it,
                           received and (like a real packet socket) sent
            kind "udp"/"tcp": a host socket on (ns, port): sees frames addressed to that host+port
Frames move in *batches* (a count, not one object per frame), so a generator offering 10^6 pps
costs a few dicts per tick. A batch is {"n", "len", "proto", ...}:
  proto "bm"  : bmgen frames; "run", "nflows", "i0" (global index of the first frame in this run;
                frame i belongs to flow i % nflows with per-flow seq i // nflows)
  every batch: "ts", "t" -- when its first and last frame were due to be sent (ns, CLOCK_REALTIME)
  proto "udp" : iperf3 datagrams; "sport", "dport", "src", "dst", "s0" (iperf3 seq of the first)
  proto "tcp" : iperf3 control; "sport", "dport", "src", "dst", "flags", "data"
After every change the per-netns /proc/net/dev text is rendered to $FAKE_STATE/netdev/<ns>, which
the fake wrapper's `netdev` prints, in the kernel's exact format.
"""
import contextlib
import fcntl
import json
import os
import signal
import sys
import time

CTR = ["rx_bytes", "rx_packets", "rx_errs", "rx_drop", "rx_fifo", "rx_frame", "rx_compressed", "rx_multicast",
       "tx_bytes", "tx_packets", "tx_errs", "tx_drop", "tx_fifo", "tx_colls", "tx_carrier", "tx_compressed"]
HDR = ("Inter-|   Receive                                                |  Transmit\n"
       " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n")
FMT = "%6s:%8d %7d %4d %4d %4d %5d %10d %9d %8d %7d %4d %4d %4d %5d %7d %10d\n"   # net/core/net-procfs.c


def host_ip(ns):
    """bmb-hK owns 10.0.0.(K+1), as topo-up configures it."""
    if ns.startswith("bmb-h"):
        return f"10.0.0.{int(ns[5:]) + 1}"
    return None


def ns_of_ip(ip):
    if ip.startswith("10.0.0."):
        return f"bmb-h{int(ip.split('.')[3]) - 1}"
    return None


def peer(ns, ifc):
    """The other end of a veth pair: swK in bmb-sw <-> hK in bmb-hK."""
    if ns == "bmb-sw" and ifc.startswith("sw"):
        k = ifc[2:]
        return f"bmb-h{k}", f"h{k}"
    if ns.startswith("bmb-h") and ifc.startswith("h"):
        return "bmb-sw", f"sw{ifc[1:]}"
    return None


def state_dir():
    return os.environ["FAKE_STATE"]


def _load(d):
    try:
        with open(os.path.join(d, "state.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"netns": {}, "socks": {}, "next_id": 1, "cpu": None}


def _save(d, s):
    tmp = os.path.join(d, f"state.json.{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, os.path.join(d, "state.json"))
    render(d, s)


def render(d, s):
    nd = os.path.join(d, "netdev")
    os.makedirs(nd, exist_ok=True)
    for ns, ifs in s["netns"].items():
        text = HDR + "".join(FMT % ((name,) + tuple(c[k] for k in CTR)) for name, c in ifs.items())
        tmp = os.path.join(nd, f".{ns}.{os.getpid()}")
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, os.path.join(nd, ns))
    for f in os.listdir(nd):
        if not f.startswith(".") and f not in s["netns"]:
            os.remove(os.path.join(nd, f))


@contextlib.contextmanager
def txn(write=True):
    d = state_dir()
    with open(os.path.join(d, "lock"), "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            s = _load(d)
            yield s
            if write:
                _save(d, s)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def new_ctr():
    return {k: 0 for k in CTR}


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ------------------------------------------------------------------------------ frame movement
def _sockets(s, ns, ifc=None, kind="packet", port=None):
    out = []
    for sid, k in list(s["socks"].items()):
        if not alive(k["pid"]):
            del s["socks"][sid]
            continue
        if k["ns"] == ns and k["kind"] == kind and (ifc is None or k["if"] == ifc) \
                and (port is None or k["port"] == port):
            out.append(k)
    return out


def transmit(s, ns, ifc, batch):
    """`batch` leaves (ns, ifc): tx there, rx on the veth peer, then delivery at the peer."""
    if batch["n"] <= 0 or ns not in s["netns"] or ifc not in s["netns"][ns]:
        return
    if "t" not in batch:
        batch = dict(batch, t=time.time_ns())
    c = s["netns"][ns][ifc]
    c["tx_packets"] += batch["n"]
    c["tx_bytes"] += batch["n"] * batch["len"]
    for k in _sockets(s, ns, ifc):                       # packet sockets see outgoing frames too
        k["inbox"].append({**batch, "outgoing": True})
    p = peer(ns, ifc)
    if not p or p[0] not in s["netns"]:
        return
    pc = s["netns"][p[0]][p[1]]
    pc["rx_packets"] += batch["n"]
    pc["rx_bytes"] += batch["n"] * batch["len"]
    deliver(s, p[0], p[1], dict(batch, t_rx=time.time_ns()))


def deliver(s, ns, ifc, batch):
    for k in _sockets(s, ns, ifc):
        k["inbox"].append(batch)
    # host stack: only frames addressed to this host's IP reach a socket; the rest (bmgen frames
    # to 10.0.1.1 with a foreign MAC) are dropped silently, as the real sink does
    if host_ip(ns) is None or batch.get("dst") != host_ip(ns) or batch["proto"] not in ("udp", "tcp"):
        return
    socks = _sockets(s, ns, None, batch["proto"], batch["dport"])
    if socks:
        socks[0]["inbox"].append(batch)
    elif batch["proto"] == "tcp" and batch.get("flags") == "SYN":
        transmit(s, ns, ifc, {"n": 1, "len": 54, "proto": "tcp", "flags": "RST", "src": batch["dst"],
                              "dst": batch["src"], "sport": batch["dport"], "dport": batch["sport"]})


def open_sock(s, ns, ifc, kind, port=None):
    sid = str(s["next_id"])
    s["next_id"] += 1
    s["socks"][sid] = {"pid": os.getpid(), "ns": ns, "if": ifc, "kind": kind, "port": port, "inbox": []}
    return sid


def pop(s, sid):
    k = s["socks"].get(sid)
    if not k:
        return []
    out, k["inbox"] = k["inbox"], []
    return out


def close_sock(sid):
    with txn() as s:
        s["socks"].pop(sid, None)


def has_iface(ns, ifc):
    with txn(write=False) as s:
        return ns in s["netns"] and ifc in s["netns"][ns]


# ------------------------------------------------------------------------------ process plumbing
def register_pid():
    """Leave a pid file so the test can find and stop every fake process it started."""
    d = os.path.join(state_dir(), "pids")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, str(os.getpid()))
    open(p, "w").close()
    with open(os.path.join(state_dir(), "started.log"), "a") as f:
        f.write(f"{os.getpid()} {os.path.basename(sys.argv[0])} {os.environ.get('FAKE_NS', '-')}\n")
    return p


def unregister_pid():
    try:
        os.remove(os.path.join(state_dir(), "pids", str(os.getpid())))
    except OSError:
        pass


class Stop:
    """SIGTERM/SIGINT set a flag, like the C tools' on_signal()."""
    def __init__(self):
        self.flag = False
        signal.signal(signal.SIGTERM, self._on)
        signal.signal(signal.SIGINT, self._on)

    def _on(self, *_):
        self.flag = True


class Bucket:
    """The forwarding capacity of a fake forwarder: pps frames/s plus a queue of `depth` frames.
    Capacity is booked on a timeline of 10 ms bins indexed by the frames' own send times (batch
    "ts" .. "t", ns), not by when this process gets to look at them, and a late batch can still
    use the capacity of the bins it belongs to. So neither a descheduled fake switch nor a sender
    that the host stalled (and that then hands over seconds of frames at once) turns into loss
    below capacity: a real switch and a real sender keep running while our Python fakes wait.
    A batch's frames are spread evenly over its bins; what a bin cannot take waits in the queue
    (spills into the next bins) and is dropped once the queue holds more than `depth`."""
    BIN = 0.01

    def __init__(self, pps, depth):
        self.cap, self.depth, self.used = pps * self.BIN, depth, {}

    def take(self, n, ts, te):
        b0 = int(ts / 1e9 / self.BIN)
        b1 = max(b0, int(max(ts, te) / 1e9 / self.BIN))
        per = n / (b1 - b0 + 1)
        carry = dropped = 0.0
        b = b0
        last = b1 + int(self.depth / self.cap)          # a full queue delays a frame by depth/pps at most
        while b <= b1 or (carry > 1e-9 and b <= last):
            demand = carry + (per if b <= b1 else 0.0)
            used = self.used.get(b, 0.0)
            s = min(demand, self.cap - used)
            self.used[b] = used + s
            carry = demand - s
            if carry > self.depth:
                dropped += carry - self.depth
                carry = self.depth
            b += 1
        dropped += carry
        if len(self.used) > 20000:                       # keep about the last minute
            for k in [k for k in self.used if k < b - 6000]:
                del self.used[k]
        return n - min(n, int(round(dropped)))


_turn = [0]


def forward(s, sid_by_port, bucket, out_port_of, on_forward=None, drop_proto=None):
    """Move what arrived on each port to its output port, through `bucket`. Frames the bucket
    refuses are lost inside the forwarder (as in a full pcap buffer): no netdev counter shows them.
    Ports take turns going first, so two loaded ports share the bucket (R3, two port pairs), as
    bmv2's one ingress thread serves every port."""
    items = list(sid_by_port.items())
    _turn[0] = (_turn[0] + 1) % max(1, len(items))
    for port, sid in items[_turn[0]:] + items[:_turn[0]]:
        for b in pop(s, sid):
            if b.get("outgoing") or b["proto"] == drop_proto:
                continue
            k = bucket.take(b["n"], b.get("ts", b["t"]), b["t"])
            if k <= 0:
                continue
            fb = dict(b, n=k)
            fb.pop("t_rx", None)
            if on_forward:
                on_forward(port, fb)
            transmit(s, "bmb-sw", f"sw{out_port_of(port)}", fb)


DEFAULTS = {
    "switch_pps": 100000,        # fake bmv2 forwarding capacity (frames/s, all ports together)
    "switch_depth": 10000,       # frames it can absorb in a burst (0.1 s at switch_pps): larger than
                                 # any batch a fake sender emits, so a rate below capacity is lossless
    "switch_startup_s": 0.6,     # time before its ports are open (frames sent earlier are lost)
    "null_pps": 400000,          # fake bmnull capacity
    "gen_max_pps": 1000000,      # fake bmgen rate at --pps 0
    "gen_max_pps_if": {},        # per-interface override of gen_max_pps (R3: the second pair's generator
                                 # on h2 sends a different count, so a G2 against the wrong counter shows)
    "log_frames_per_s": 3000,    # console-log volume cap of the fake switch (per-frame lines)
    "tcp_syn_timeout_s": 127,    # Linux default (tcp_syn_retries = 6)
    "miss_table": None,          # a table name the fake switch reports as "miss" (G10 negative test)
    "miss_tables": [],           # several tables reported as "miss" (E8: a deny list that never matches)
    "own_frames_pps": 0,         # the fake switch also emits frames of its own out of port 1 at this rate
                                 # while it forwards (E8: h1's interface counter counts them, bmrecv `foreign`)
    "recirc_copy": False,        # log an N.2 copy (inverted table outcomes) of every logged frame (E8: G10 ignores it)
    "dead_switch": False,        # the fake switch starts but never forwards
    "drop_proto": None,          # the fake switch drops every frame of this proto ("tcp": iperf3 cannot connect)
    "tcp_fake_bps": 20e6,        # fake iperf3 TCP bulk rate (AMENDMENT-14 (c) dry run)
}


def config():
    """Knobs of the fake network, from $FAKE_STATE/config.json (the wrapper clears the
    environment of what it runs, exactly as the real one does, so knobs cannot travel as env)."""
    try:
        with open(os.path.join(state_dir(), "config.json")) as f:
            return {**DEFAULTS, **json.load(f)}
    except FileNotFoundError:
        return dict(DEFAULTS)


def die(tool, msg, code=1):
    sys.stderr.write(f"{tool}: {msg}\n")
    sys.stderr.flush()
    sys.exit(code)
