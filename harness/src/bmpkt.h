/* bmpkt.h -- the one test-packet layout shared by bmgen (sender) and bmrecv (receiver).
 *
 * Frame sizes follow RFC 2544: a "64 B frame" includes the 4-byte FCS, which a veth never
 * carries, so the buffer handed to the kernel is (frame - 4) bytes. 64 B -> 60 B buffer:
 * 14 Ethernet + 20 IPv4 + 8 UDP + 18 payload. The marker below is 18 bytes so it fits exactly
 * (checked at compile time below).
 *
 * The marker carries a 16-bit run id so frames left over from an earlier run can be told apart
 * from the current one (bmrecv --run-id counts the others as `stale`).
 *
 * Flows differ only in the UDP source port (BM_SPORT_BASE + flow), so every flow takes the same
 * IP addresses -- the P4 programs decide whether that hits one table entry or many.
 *
 */
#ifndef BMPKT_H
#define BMPKT_H

#include <stdint.h>
#include <string.h>

#define BM_MAGIC        0xB2B2u
#define BM_ETH_LEN      14
#define BM_IP_LEN       20
#define BM_UDP_LEN      8
#define BM_HDR_LEN      (BM_ETH_LEN + BM_IP_LEN + BM_UDP_LEN)    /* 42 */
#define BM_MARK_LEN     18
#define BM_FCS_LEN      4
#define BM_MIN_FRAME    64
#define BM_MAX_FRAME    1518
#define BM_SPORT_BASE   10000
#define BM_DPORT        5001
#define BM_MAX_FLOWS    4096

/* Marker at the start of the UDP payload, network byte order on the wire:
 * magic u16 @0, flow u16 @2, seq u32 @4, tx_ns u64 @8, run u16 @16. */
struct bm_mark {
    uint16_t magic;
    uint16_t flow;
    uint32_t seq;      /* per-flow sequence number, starts at 0 */
    uint64_t tx_ns;    /* CLOCK_REALTIME at send, ns -- comparable with TPACKET timestamps */
    uint16_t run;      /* run id, set per bmgen invocation (--run-id) */
} __attribute__((packed));

/* The marker must fit in the smallest RFC 2544 frame: 64 B minus FCS = 60 B buffer. */
_Static_assert(sizeof(struct bm_mark) == BM_MARK_LEN, "bm_mark size != BM_MARK_LEN");
_Static_assert(BM_HDR_LEN + BM_MARK_LEN <= BM_MIN_FRAME - BM_FCS_LEN, "marker does not fit a 64 B frame");

static inline uint16_t bm_ip_csum(const uint8_t *ip) {
    uint32_t s = 0;
    for (int i = 0; i < BM_IP_LEN; i += 2) s += (uint32_t)(ip[i] << 8 | ip[i + 1]);
    while (s >> 16) s = (s & 0xffff) + (s >> 16);
    return (uint16_t)~s;
}

static inline uint64_t bm_hton64(uint64_t v) {
    return ((uint64_t)__builtin_bswap32((uint32_t)v) << 32) | __builtin_bswap32((uint32_t)(v >> 32));
}

/* Build the fixed part of a frame of `buflen` bytes (frame size minus FCS) for `flow` and run id `run`;
 * `tos` is the IPv4 TOS byte (0 unless a program's path needs, e.g., ECT). */
static inline void bm_build(uint8_t *b, int buflen, const uint8_t dmac[6], const uint8_t smac[6],
                            uint32_t sip, uint32_t dip, uint16_t flow, uint16_t run, uint8_t tos) {
    memset(b, 0, (size_t)buflen);
    memcpy(b, dmac, 6);
    memcpy(b + 6, smac, 6);
    b[12] = 0x08; b[13] = 0x00;
    uint8_t *ip = b + BM_ETH_LEN;
    int iplen = buflen - BM_ETH_LEN;
    ip[0] = 0x45; ip[1] = tos;
    ip[2] = (uint8_t)(iplen >> 8); ip[3] = (uint8_t)iplen;
    ip[4] = 0; ip[5] = 0; ip[6] = 0x40; ip[7] = 0;     /* id 0, DF */
    ip[8] = 64; ip[9] = 17;                             /* TTL, UDP */
    ip[12] = (uint8_t)(sip >> 24); ip[13] = (uint8_t)(sip >> 16); ip[14] = (uint8_t)(sip >> 8); ip[15] = (uint8_t)sip;
    ip[16] = (uint8_t)(dip >> 24); ip[17] = (uint8_t)(dip >> 16); ip[18] = (uint8_t)(dip >> 8); ip[19] = (uint8_t)dip;
    uint16_t c = bm_ip_csum(ip);
    ip[10] = (uint8_t)(c >> 8); ip[11] = (uint8_t)c;
    uint8_t *udp = ip + BM_IP_LEN;
    uint16_t sport = (uint16_t)(BM_SPORT_BASE + flow), ulen = (uint16_t)(iplen - BM_IP_LEN);
    udp[0] = (uint8_t)(sport >> 8); udp[1] = (uint8_t)sport;
    udp[2] = (uint8_t)(BM_DPORT >> 8); udp[3] = (uint8_t)BM_DPORT;
    udp[4] = (uint8_t)(ulen >> 8); udp[5] = (uint8_t)ulen;
    udp[6] = 0; udp[7] = 0;                             /* UDP checksum 0 = not computed (IPv4) */
    struct bm_mark m = { __builtin_bswap16(BM_MAGIC), __builtin_bswap16(flow), 0, 0, __builtin_bswap16(run) };
    memcpy(b + BM_HDR_LEN, &m, sizeof m);
}

static inline void bm_stamp(uint8_t *b, uint32_t seq, uint64_t tx_ns) {
    uint32_t s = __builtin_bswap32(seq);
    uint64_t t = bm_hton64(tx_ns);
    memcpy(b + BM_HDR_LEN + 4, &s, 4);
    memcpy(b + BM_HDR_LEN + 8, &t, 8);
}

/* Parse a received frame. Returns 1 and fills flow/seq/tx_ns/run if it is one of ours. */
static inline int bm_parse(const uint8_t *b, int len, uint16_t *flow, uint32_t *seq, uint64_t *tx_ns,
                           uint16_t *run) {
    if (len < BM_HDR_LEN + BM_MARK_LEN || b[12] != 0x08 || b[13] != 0x00 || b[BM_ETH_LEN + 9] != 17) return 0;
    struct bm_mark m;
    memcpy(&m, b + BM_HDR_LEN, sizeof m);
    if (__builtin_bswap16(m.magic) != BM_MAGIC) return 0;
    *flow = __builtin_bswap16(m.flow);
    *seq = __builtin_bswap32(m.seq);
    *tx_ns = bm_hton64(m.tx_ns);
    *run = __builtin_bswap16(m.run);
    return 1;
}

#endif
