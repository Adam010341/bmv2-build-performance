// e8test.p4 -- the smallest program whose table is filled at run time (E8 harness check, not a
// measured program). One NON-const exact table on ipv4.dstAddr: an entry forwards port p to
// p ^ 1 (the rule every generated program uses), the default action drops. With no entries
// loaded nothing crosses the switch, so a run that forwards proves the entries were loaded.
// Entries: entries.txt (simple_switch_CLI commands); compiled by compile.sh next to this file.
#include <core.p4>
#include <v1model.p4>

header ethernet_t { bit<48> dstAddr; bit<48> srcAddr; bit<16> etherType; }
header ipv4_t {
    bit<4> version; bit<4> ihl; bit<8> diffserv; bit<16> totalLen; bit<16> identification;
    bit<3> flags; bit<13> fragOffset; bit<8> ttl; bit<8> protocol; bit<16> hdrChecksum;
    bit<32> srcAddr; bit<32> dstAddr;
}
header udp_t { bit<16> srcPort; bit<16> dstPort; bit<16> len; bit<16> checksum; }
struct headers_t { ethernet_t ethernet; ipv4_t ipv4; udp_t udp; }
struct meta_t { }

parser MyParser(packet_in pkt, out headers_t hdr, inout meta_t meta,
                inout standard_metadata_t sm) {
    state start { pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) { 0x0800: parse_ipv4; default: accept; } }
    state parse_ipv4 { pkt.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) { 17: parse_udp; default: accept; } }
    state parse_udp { pkt.extract(hdr.udp); transition accept; }
}
control MyVerifyChecksum(inout headers_t hdr, inout meta_t meta) { apply { } }
control MyIngress(inout headers_t hdr, inout meta_t meta, inout standard_metadata_t sm) {
    action drop() { mark_to_drop(sm); }
    action forward() { sm.egress_spec = sm.ingress_port ^ 9w1; }
    table ipv4_fwd {
        key = { hdr.ipv4.dstAddr : exact; }
        actions = { forward; drop; }
        default_action = drop();
        size = 1024;
    }
    apply {
        if (hdr.ipv4.isValid()) { ipv4_fwd.apply(); } else { drop(); }
    }
}
control MyEgress(inout headers_t hdr, inout meta_t meta, inout standard_metadata_t sm) { apply { } }
control MyComputeChecksum(inout headers_t hdr, inout meta_t meta) { apply { } }
control MyDeparser(packet_out pkt, in headers_t hdr) {
    apply { pkt.emit(hdr.ethernet); pkt.emit(hdr.ipv4); pkt.emit(hdr.udp); }
}
V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(), MyComputeChecksum(), MyDeparser()) main;
