/* bmrecv -- per-flow receiver for the bmbench harness.
 *
 * Reads frames from one interface through a TPACKET_V3 ring and keeps, per flow: frames
 * received, highest sequence number seen, out-of-order arrivals, duplicates; and a latency
 * histogram (kernel RX timestamp minus the generator's CLOCK_REALTIME stamp, same host).
 *
 * Loss per flow is computed by the orchestrator from bmgen's per-flow counts and ours; this
 * program also reports its OWN drops (PACKET_STATISTICS tp_drops), so a receiver that could not
 * keep up is visible instead of looking like switch loss.
 *
 * --run-id N: only frames carrying run id N are accounted; frames of ours with another run id
 * (left over from an earlier run) are counted in `stale` and kept out of the per-flow and latency
 * statistics. Without it every run is accepted.
 *
 * Handshake: once the socket is bound and the ring mapped, "READY\n" is written to stderr so the
 * orchestrator knows it may start the generator. On duration expiry or SIGINT/SIGTERM the ring is
 * drained before reporting: blocks are consumed until a 50 ms poll (the ring retires partly
 * filled blocks after 10 ms) finds none ready.
 *
 * Offline mode, no privileges: --pcap-in FILE parses a pcap file (e.g. from bmgen --dump) with
 * the same accounting code, which is what the unit test exercises.
 *
 */
#define _GNU_SOURCE
#include "bmpkt.h"

#include <arpa/inet.h>
#include <getopt.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define NBUCKET 64      /* latency buckets: bucket i holds [2^(i/2), 2^((i+1)/2)) ns, roughly */

struct flowstat { uint64_t rx, ooo, dup; int64_t max_seq; };

static volatile sig_atomic_t stop_flag = 0;
static void on_signal(int s) { (void)s; stop_flag = 1; }

static struct flowstat fs[BM_MAX_FLOWS];
static int run_filter = -1;      /* -1: accept every run id */
static uint64_t hist[NBUCKET], foreign = 0, stale = 0, rx_total = 0, lat_n = 0, lat_neg = 0;
static uint64_t skipped = 0;   /* ring frames too short to parse or marked TP_STATUS_COPY */
static double lat_sum = 0;

static int bucket(uint64_t ns) {
    if (ns == 0) return 0;
    int b = (int)(2.0 * __builtin_log2((double)ns));   /* half-octave buckets */
    return b < 0 ? 0 : (b >= NBUCKET ? NBUCKET - 1 : b);
}

static void account(const uint8_t *p, int len, uint64_t rx_ns) {
    uint16_t flow, run; uint32_t seq; uint64_t tx;
    if (!bm_parse(p, len, &flow, &seq, &tx, &run) || flow >= BM_MAX_FLOWS) { foreign++; return; }
    if (run_filter >= 0 && run != run_filter) { stale++; return; }
    struct flowstat *f = &fs[flow];
    rx_total++;
    if (f->rx == 0) f->max_seq = -1;
    if ((int64_t)seq > f->max_seq) f->max_seq = seq;
    else if ((int64_t)seq == f->max_seq) f->dup++;
    else f->ooo++;
    f->rx++;
    if (rx_ns >= tx) { uint64_t d = rx_ns - tx; hist[bucket(d)]++; lat_sum += (double)d; lat_n++; }
    else lat_neg++;
}

static double pct(double q) {   /* bucket upper edge of the q-quantile */
    uint64_t tot = 0, acc = 0;
    for (int i = 0; i < NBUCKET; i++) tot += hist[i];
    if (!tot) return -1;
    for (int i = 0; i < NBUCKET; i++) {
        acc += hist[i];
        if ((double)acc >= q * (double)tot) return __builtin_pow(2.0, (i + 1) / 2.0);
    }
    return -1;
}

static void report(const char *src, double secs, long long tp_packets, long long tp_drops) {
    int maxflow = -1;
    for (int i = 0; i < BM_MAX_FLOWS; i++) if (fs[i].rx) maxflow = i;
    printf("{\"tool\":\"bmrecv\",\"source\":\"%s\",\"duration_s\":%.6f,\"rx\":%llu,\"foreign\":%llu,\"stale\":%llu,\"skipped\":%llu,",
           src, secs, (unsigned long long)rx_total, (unsigned long long)foreign, (unsigned long long)stale,
           (unsigned long long)skipped);
    if (run_filter >= 0) printf("\"run_id_filter\":%d,", run_filter);
    else printf("\"run_id_filter\":null,");
    printf("\"ring_packets\":%lld,\"ring_drops\":%lld,\"lat_n\":%llu,\"lat_negative\":%llu,"
           "\"lat_mean_us\":%.2f,\"lat_p50_us_le\":%.2f,\"lat_p99_us_le\":%.2f,\"lat_p999_us_le\":%.2f,\"flows\":[",
           tp_packets, tp_drops,
           (unsigned long long)lat_n, (unsigned long long)lat_neg, lat_n ? lat_sum / (double)lat_n / 1e3 : -1,
           pct(0.5) / 1e3, pct(0.99) / 1e3, pct(0.999) / 1e3);
    for (int i = 0; i <= maxflow; i++)
        printf("%s{\"flow\":%d,\"rx\":%llu,\"max_seq\":%lld,\"ooo\":%llu,\"dup\":%llu}", i ? "," : "", i,
               (unsigned long long)fs[i].rx, (long long)(fs[i].rx ? fs[i].max_seq : -1),
               (unsigned long long)fs[i].ooo, (unsigned long long)fs[i].dup);
    printf("],\"lat_hist_halfoctave_ns\":[");
    for (int i = 0; i < NBUCKET; i++) printf("%s%llu", i ? "," : "", (unsigned long long)hist[i]);
    printf("]}\n");
}

/* Account every frame of the block at *blk and hand it back to the kernel. Returns 0 if the
   block is not ready (status not TP_STATUS_USER), 1 if one was consumed. */
static int consume_block(uint8_t *ring, unsigned *blk, const struct tpacket_req3 *req) {
    struct tpacket_block_desc *bd = (struct tpacket_block_desc *)(ring + (size_t)*blk * req->tp_block_size);
    if (!(bd->hdr.bh1.block_status & TP_STATUS_USER)) return 0;
    struct tpacket3_hdr *h = (struct tpacket3_hdr *)((uint8_t *)bd + bd->hdr.bh1.offset_to_first_pkt);
    for (unsigned i = 0; i < bd->hdr.bh1.num_pkts; i++) {
        if (h->tp_snaplen >= (unsigned)BM_HDR_LEN && !(h->tp_status & TP_STATUS_COPY)) {
            uint64_t rx_ns = (uint64_t)h->tp_sec * 1000000000ull + h->tp_nsec;
            account((uint8_t *)h + h->tp_mac, (int)h->tp_snaplen, rx_ns);
        } else {
            skipped++;
        }
        h = (struct tpacket3_hdr *)((uint8_t *)h + h->tp_next_offset);
    }
    __sync_synchronize();
    bd->hdr.bh1.block_status = TP_STATUS_KERNEL;
    *blk = (*blk + 1) % req->tp_block_nr;
    return 1;
}

static int run_pcap(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); return 1; }
    uint32_t gh[6];
    if (fread(gh, 4, 6, f) != 6 || gh[0] != 0xa1b2c3d4) { fprintf(stderr, "not a little-endian usec pcap\n"); return 1; }
    uint32_t rh[4];
    static uint8_t buf[65536];
    while (fread(rh, 4, 4, f) == 4) {
        if (rh[2] > sizeof buf || fread(buf, 1, rh[2], f) != rh[2]) break;
        uint16_t fl, rn; uint32_t sq; uint64_t tx;
        /* offline: pretend the frame arrived 1 us after it was stamped */
        uint64_t rx = bm_parse(buf, (int)rh[2], &fl, &sq, &tx, &rn) ? tx + 1000 : 0;
        account(buf, (int)rh[2], rx);
    }
    fclose(f);
    report(path, 0, -1, -1);
    return 0;
}

int main(int argc, char **argv) {
    const char *ifname = NULL, *pcap_in = NULL;
    double duration = 0;
    static struct option opts[] = {{"if", 1, 0, 'i'}, {"duration", 1, 0, 'd'}, {"pcap-in", 1, 0, 'p'},
                                   {"run-id", 1, 0, 'u'}, {0, 0, 0, 0}};
    const char *usage = "usage: bmrecv --if IFACE [--duration S] [--run-id N] | --pcap-in FILE [--run-id N]\n";
    int o;
    while ((o = getopt_long(argc, argv, "", opts, NULL)) != -1) {
        if (o == 'i') ifname = optarg;
        else if (o == 'd') duration = atof(optarg);
        else if (o == 'p') pcap_in = optarg;
        else if (o == 'u') {
            char *e; long v = strtol(optarg, &e, 10);
            if (*optarg == '\0' || *e || v < 0 || v > 65535) { fprintf(stderr, "%s", usage); return 2; }
            run_filter = (int)v;
        }
        else { fprintf(stderr, "%s", usage); return 2; }
    }
    if (pcap_in) return run_pcap(pcap_in);
    if (!ifname) { fprintf(stderr, "%s", usage); return 2; }

    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd < 0) { perror("socket(AF_PACKET) -- needs CAP_NET_RAW"); return 1; }
    int ver = TPACKET_V3;
    if (setsockopt(fd, SOL_PACKET, PACKET_VERSION, &ver, sizeof ver) < 0) { perror("PACKET_VERSION"); return 1; }
    struct tpacket_req3 req = {0};
    req.tp_block_size = 1 << 20;
    req.tp_block_nr = 64;
    req.tp_frame_size = 2048;
    req.tp_frame_nr = (req.tp_block_size * req.tp_block_nr) / req.tp_frame_size;
    req.tp_retire_blk_tov = 10;   /* ms: a partly filled block is handed over after 10 ms */
    if (setsockopt(fd, SOL_PACKET, PACKET_RX_RING, &req, sizeof req) < 0) { perror("PACKET_RX_RING"); return 1; }
    size_t ringlen = (size_t)req.tp_block_size * req.tp_block_nr;
    uint8_t *ring = mmap(NULL, ringlen, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_LOCKED, fd, 0);
    if (ring == MAP_FAILED) ring = mmap(NULL, ringlen, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (ring == MAP_FAILED) { perror("mmap"); return 1; }
    struct sockaddr_ll sll = {0};
    sll.sll_family = AF_PACKET;
    sll.sll_protocol = htons(ETH_P_ALL);
    sll.sll_ifindex = (int)if_nametoindex(ifname);
    if (!sll.sll_ifindex) { fprintf(stderr, "no interface %s\n", ifname); return 1; }
    if (bind(fd, (struct sockaddr *)&sll, sizeof sll) < 0) { perror("bind"); return 1; }
    /* Reset the kernel's counters so tp_drops covers only this run. */
    struct tpacket_stats_v3 st;
    socklen_t sl = sizeof st;
    getsockopt(fd, SOL_PACKET, PACKET_STATISTICS, &st, &sl);

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    struct timespec ts0, ts1;
    clock_gettime(CLOCK_MONOTONIC, &ts0);
    unsigned blk = 0;
    struct pollfd pfd = { fd, POLLIN | POLLERR, 0 };
    long long tp_packets = 0, tp_drops = 0;
    fprintf(stderr, "READY\n");      /* bound and mapped: the orchestrator may start the generator */
    fflush(stderr);
    while (!stop_flag) {
        if (duration > 0) {
            clock_gettime(CLOCK_MONOTONIC, &ts1);
            if ((double)(ts1.tv_sec - ts0.tv_sec) + (double)(ts1.tv_nsec - ts0.tv_nsec) / 1e9 >= duration) break;
        }
        if (!consume_block(ring, &blk, &req)) poll(&pfd, 1, 100);
    }
    clock_gettime(CLOCK_MONOTONIC, &ts1);
    /* Final drain: frames still sitting in the ring (or in a block the kernel has not retired yet)
       belong to the run. Stop only when a poll covering the 10 ms retire timeout finds nothing. */
    for (;;) {
        if (consume_block(ring, &blk, &req)) continue;
        poll(&pfd, 1, 50);
        if (!consume_block(ring, &blk, &req)) break;
    }
    sl = sizeof st;
    if (getsockopt(fd, SOL_PACKET, PACKET_STATISTICS, &st, &sl) == 0) { tp_packets = st.tp_packets; tp_drops = st.tp_drops; }
    report(ifname, (double)(ts1.tv_sec - ts0.tv_sec) + (double)(ts1.tv_nsec - ts0.tv_nsec) / 1e9, tp_packets, tp_drops);
    return 0;
}
