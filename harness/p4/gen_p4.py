#!/usr/bin/env python3
"""Generate the P4_16 (v1model) program family used by the bmbench experiments.

Every program parses Ethernet/IPv4/UDP and forwards port 2k <-> 2k+1. What varies:

  min                 no table: parser, one assignment, deparser
  l3                  tutorial-style router: LPM on dstAddr (10.0.1.0/24 and 10.0.0.0/24, so both
                      bmgen and iperf3 traffic hit), TTL-1, IPv4 checksum update
  exact_K / lpm_K / ternary_K
                      K tables in sequence, each with ONE const entry that every test frame hits
  l3_reg              l3 + a crc32 hash of the 5-tuple + a register read-modify-write per packet
  flow_exact_N / flow_ternary_N
                      one table keyed on the UDP source port with N const entries, one per flow
                      (bmgen flow f uses source port 10000+f), so N flows hit N distinct entries

All tables have const entries, so a run needs no control plane at all.

Usage: gen_p4.py OUTDIR    (writes OUTDIR/<name>.p4 and OUTDIR/programs.tsv)
"""
import os
import sys

HEADER = r"""#include <core.p4>
#include <v1model.p4>

header ethernet_t { bit<48> dstAddr; bit<48> srcAddr; bit<16> etherType; }
header ipv4_t {
    bit<4> version; bit<4> ihl; bit<8> diffserv; bit<16> totalLen; bit<16> identification;
    bit<3> flags; bit<13> fragOffset; bit<8> ttl; bit<8> protocol; bit<16> hdrChecksum;
    bit<32> srcAddr; bit<32> dstAddr;
}
header udp_t { bit<16> srcPort; bit<16> dstPort; bit<16> len; bit<16> checksum; }
struct headers_t { ethernet_t ethernet; ipv4_t ipv4; udp_t udp; }
struct meta_t { bit<32> acc; bit<32> h; }

parser MyParser(packet_in pkt, out headers_t hdr, inout meta_t meta,
                inout standard_metadata_t sm) {
    state start { pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) { 0x0800: parse_ipv4; default: accept; } }
    state parse_ipv4 { pkt.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) { 17: parse_udp; default: accept; } }
    state parse_udp { pkt.extract(hdr.udp); transition accept; }
}
control MyVerifyChecksum(inout headers_t hdr, inout meta_t meta) { apply { } }
"""

TRAILER = r"""
control MyEgress(inout headers_t hdr, inout meta_t meta, inout standard_metadata_t sm) { apply { } }
control MyComputeChecksum(inout headers_t hdr, inout meta_t meta) {
    apply {
%CSUM%
    }
}
control MyDeparser(packet_out pkt, in headers_t hdr) {
    apply { pkt.emit(hdr.ethernet); pkt.emit(hdr.ipv4); pkt.emit(hdr.udp); }
}
V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(), MyComputeChecksum(), MyDeparser()) main;
"""

CSUM = r"""        update_checksum(hdr.ipv4.isValid(),
            { hdr.ipv4.version, hdr.ipv4.ihl, hdr.ipv4.diffserv, hdr.ipv4.totalLen,
              hdr.ipv4.identification, hdr.ipv4.flags, hdr.ipv4.fragOffset, hdr.ipv4.ttl,
              hdr.ipv4.protocol, hdr.ipv4.srcAddr, hdr.ipv4.dstAddr },
            hdr.ipv4.hdrChecksum, HashAlgorithm.csum16);"""

DST = "32w0x0a000101"   # 10.0.1.1, bmgen's default destination
HOSTNET = "32w0x0a000000"  # 10.0.0.0/24, the hosts' own subnet: iperf3 traffic (to 10.0.0.2) must hit
                           # the same LPM entry count as bmgen traffic, not fall through to a miss

FWD = "        sm.egress_spec = sm.ingress_port ^ 9w1;"


def table(name, kind, field, entries):
    """A table with const entries; every action adds its argument to meta.acc."""
    ents = "\n".join(f"            {e} : add({i + 1});" for i, e in enumerate(entries))
    return f"""    table {name} {{
        key = {{ {field} : {kind}; }}
        actions = {{ add; NoAction; }}
        const entries = {{
{ents}
        }}
        default_action = NoAction();
    }}"""


def program(tables, l3=False, reg=False):
    decls, applies = [], []
    decls.append("    action add(bit<32> v) { meta.acc = meta.acc + v; }")
    if reg:
        decls.append("    register<bit<32>>(1024) r;")
    for t in tables:
        decls.append(t[0])
        applies.append(f"        {t[1]}.apply();")
    body = []
    if l3:
        body.append("        if (hdr.ipv4.isValid()) { hdr.ipv4.ttl = hdr.ipv4.ttl - 1; }")
    if reg:
        body.append("""        hash(meta.h, HashAlgorithm.crc32, 32w0,
             { hdr.ipv4.srcAddr, hdr.ipv4.dstAddr, hdr.ipv4.protocol, hdr.udp.srcPort, hdr.udp.dstPort },
             32w1024);
        bit<32> v; r.read(v, meta.h); r.write(meta.h, v + 1);""")
    ingress = ("control MyIngress(inout headers_t hdr, inout meta_t meta, inout standard_metadata_t sm) {\n"
               + "\n".join(decls) + "\n    apply {\n" + "\n".join(applies + body) + "\n" + FWD + "\n    }\n}\n")
    return HEADER + ingress + TRAILER.replace("%CSUM%", CSUM if l3 else "")


def lpm_entry(value, plen):
    # NOT "value / plen": inside a P4_16 const entry that is integer division (0x0a000101 / 24 =
    # 0x6aaab5). An LPM key is written value &&& mask; p4c turns a prefix mask into a prefix length.
    mask = ((1 << 32) - 1) ^ ((1 << (32 - plen)) - 1)
    net = int(value.split("0x")[1], 16) & mask          # the network address, host bits cleared
    return f"32w0x{net:08x} &&& 32w0x{mask:08x}"


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "build"
    os.makedirs(out, exist_ok=True)
    progs = {}
    progs["min"] = (program([]), "no table")
    routes = [lpm_entry(DST, 24), lpm_entry(HOSTNET, 24)]
    progs["l3"] = (program([(table("route", "lpm", "hdr.ipv4.dstAddr", routes), "route")], l3=True),
                   "LPM route + TTL-1 + IPv4 checksum update")
    for k in (1, 2, 4, 8):
        progs[f"exact_{k}"] = (program([(table(f"t{i}", "exact", "hdr.ipv4.dstAddr", [DST]), f"t{i}") for i in range(k)]),
                               f"{k} exact table(s), 1 entry each, all hit")
        progs[f"lpm_{k}"] = (program([(table(f"t{i}", "lpm", "hdr.ipv4.dstAddr", [lpm_entry(DST, 24)]), f"t{i}") for i in range(k)]),
                             f"{k} lpm table(s), 1 entry each, all hit")
        progs[f"ternary_{k}"] = (program([(table(f"t{i}", "ternary", "hdr.ipv4.dstAddr", [f"{DST} &&& 32w0xffffff00"]), f"t{i}") for i in range(k)]),
                                 f"{k} ternary table(s), 1 entry each, all hit")
    progs["l3_reg"] = (program([(table("route", "lpm", "hdr.ipv4.dstAddr", routes), "route")], l3=True, reg=True),
                       "l3 + crc32 hash of 5-tuple + register read-modify-write")
    for n in (1, 16, 256):
        ents_e = [f"16w{10000 + f}" for f in range(n)]
        ents_t = [f"16w{10000 + f} &&& 16w0xffff" for f in range(n)]
        progs[f"flow_exact_{n}"] = (program([(table("flow", "exact", "hdr.udp.srcPort", ents_e), "flow")]),
                                    f"exact on UDP sport, {n} entries (one per flow)")
        progs[f"flow_ternary_{n}"] = (program([(table("flow", "ternary", "hdr.udp.srcPort", ents_t), "flow")]),
                                      f"ternary on UDP sport, {n} entries (one per flow)")
    with open(os.path.join(out, "programs.tsv"), "w") as idx:
        idx.write("name\tdescription\n")
        for name, (src, desc) in progs.items():
            with open(os.path.join(out, f"{name}.p4"), "w") as f:
                f.write(src)
            idx.write(f"{name}\t{desc}\n")
    print(f"wrote {len(progs)} programs to {out}")


if __name__ == "__main__":
    main()
