/* bmgen -- paced raw-Ethernet UDP generator for the bmbench harness.
 *
 * Sends test frames (bmpkt.h layout) out of one interface through an AF_PACKET socket with
 * PACKET_QDISC_BYPASS, so nothing between this process and the veth reshapes the traffic.
 *
 * Pacing: the schedule is absolute. Burst k of B frames is due at t0 + k*B/pps; the sender
 * busy-waits until that instant and then hands all B frames to one sendmmsg(). B = 1 is smooth
 * pacing; larger B reproduces the bunching of timer-driven tools (iperf3 paces on a 1 ms timer).
 * When the sender falls behind it sends immediately and records the lag instead of skipping.
 * pps = 0 means "as fast as possible", in bursts of B.
 *
 * Output: one JSON object on stdout. `attempted` counts frames handed to the kernel; `send_errors`
 * counts frames it refused (e.g. ENOBUFS when the veth peer's backlog is full) and `accepted` is
 * the difference. per_flow_attempted / per_flow_accepted split the same two counts by flow (a
 * refusal is charged to the flow of the refused frame). Schedule fidelity is reported as
 * gap_us (burst period), late_bursts (more than half a gap late), catchup_bursts (more than a
 * whole gap late) and max_lag_us; a late burst is still sent at once, never skipped. Interface
 * counters, read by the orchestrator, remain the ground truth for what entered the switch.
 *
 * --run-id N (0..65535) is written into every frame; bmrecv --run-id N ignores other runs.
 *
 * Offline check without privileges: --dump FILE writes the first --dump-count frames to a pcap
 * file and exits, so the frame format can be verified with tcpdump/bmrecv --pcap-in.
 *
 */
#define _GNU_SOURCE
#include "bmpkt.h"

#include <arpa/inet.h>
#include <errno.h>
#include <getopt.h>
#include <linux/if_packet.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define MAX_BURST 256

static volatile sig_atomic_t stop_flag = 0;
static void on_signal(int s) { (void)s; stop_flag = 1; }

static uint64_t now_ns(clockid_t c) {
    struct timespec ts;
    clock_gettime(c, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static int parse_mac(const char *s, uint8_t m[6]) {
    unsigned v[6];
    if (sscanf(s, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) return -1;
    for (int i = 0; i < 6; i++) { if (v[i] > 255) return -1; m[i] = (uint8_t)v[i]; }
    return 0;
}

static void usage(void) {
    fprintf(stderr,
        "usage: bmgen --if IFACE --frame BYTES [--pps N] [--burst B] [--duration S] [--flows N]\n"
        "             [--run-id N] [--dst-mac MAC] [--src-mac MAC] [--src-ip A.B.C.D] [--dst-ip A.B.C.D] [--tos BYTE]\n"
        "             [--dump FILE --dump-count N]\n");
    exit(2);
}

static void write_pcap(const char *path, uint8_t (*bufs)[BM_MAX_FRAME], int n, int buflen) {
    FILE *f = fopen(path, "wb");
    if (!f) { perror("dump"); exit(1); }
    uint32_t gh[6] = { 0xa1b2c3d4, 0x00040002, 0, 0, 65535, 1 };   /* v2.4, Ethernet */
    fwrite(gh, 4, 6, f);
    for (int i = 0; i < n; i++) {
        uint32_t rh[4] = { (uint32_t)i, 0, (uint32_t)buflen, (uint32_t)buflen };
        fwrite(rh, 4, 4, f);
        fwrite(bufs[i], 1, (size_t)buflen, f);
    }
    fclose(f);
}

int main(int argc, char **argv) {
    const char *ifname = NULL, *dump = NULL;
    int frame = 0, burst = 1, flows = 1, dump_count = 8, run_id = 0, tos = 0;
    double pps = 0, duration = 10;
    uint8_t dmac[6] = {0x02, 0, 0, 0, 0x01, 0x01}, smac[6] = {0x02, 0, 0, 0, 0x01, 0x00};
    uint32_t sip = 0x0a000001, dip = 0x0a000101;          /* 10.0.0.1 -> 10.0.1.1 */

    static struct option opts[] = {
        {"if", 1, 0, 'i'}, {"frame", 1, 0, 'f'}, {"pps", 1, 0, 'r'}, {"burst", 1, 0, 'b'},
        {"duration", 1, 0, 'd'}, {"flows", 1, 0, 'n'}, {"dst-mac", 1, 0, 'D'}, {"src-mac", 1, 0, 'S'},
        {"src-ip", 1, 0, 's'}, {"dst-ip", 1, 0, 't'}, {"dump", 1, 0, 'p'}, {"dump-count", 1, 0, 'c'},
        {"run-id", 1, 0, 'u'}, {"tos", 1, 0, 'T'},
        {0, 0, 0, 0}};
    int o;
    while ((o = getopt_long(argc, argv, "", opts, NULL)) != -1) {
        struct in_addr a;
        switch (o) {
        case 'i': ifname = optarg; break;
        case 'f': frame = atoi(optarg); break;
        case 'r': pps = atof(optarg); break;
        case 'b': burst = atoi(optarg); break;
        case 'd': duration = atof(optarg); break;
        case 'n': flows = atoi(optarg); break;
        case 'D': if (parse_mac(optarg, dmac)) usage(); break;
        case 'S': if (parse_mac(optarg, smac)) usage(); break;
        case 's': if (!inet_aton(optarg, &a)) usage(); sip = ntohl(a.s_addr); break;
        case 't': if (!inet_aton(optarg, &a)) usage(); dip = ntohl(a.s_addr); break;
        case 'p': dump = optarg; break;
        case 'c': dump_count = atoi(optarg); break;
        case 'u': { char *e; long v = strtol(optarg, &e, 10);
                    if (*optarg == '\0' || *e || v < 0 || v > 65535) usage();
                    run_id = (int)v; break; }
        case 'T': { char *e; long v = strtol(optarg, &e, 0);
                    if (*optarg == '\0' || *e || v < 0 || v > 255) usage();
                    tos = (int)v; break; }
        default: usage();
        }
    }
    if (frame < BM_MIN_FRAME || frame > BM_MAX_FRAME) { fprintf(stderr, "frame must be %d..%d\n", BM_MIN_FRAME, BM_MAX_FRAME); return 2; }
    if (burst < 1 || burst > MAX_BURST || flows < 1 || flows > BM_MAX_FLOWS || pps < 0 || duration <= 0) usage();
    int buflen = frame - BM_FCS_LEN;

    /* One template per flow; the per-packet work is only the seq/timestamp stamp. */
    uint8_t (*tmpl)[BM_MAX_FRAME] = calloc((size_t)flows, BM_MAX_FRAME);
    uint8_t (*bufs)[BM_MAX_FRAME] = calloc(MAX_BURST, BM_MAX_FRAME);
    uint32_t *seq = calloc((size_t)flows, sizeof *seq);
    if (!tmpl || !bufs || !seq) { perror("calloc"); return 1; }
    for (int fl = 0; fl < flows; fl++) bm_build(tmpl[fl], buflen, dmac, smac, sip, dip, (uint16_t)fl, (uint16_t)run_id, (uint8_t)tos);

    if (dump) {
        int n = dump_count > MAX_BURST ? MAX_BURST : dump_count;
        for (int i = 0; i < n; i++) {
            int fl = i % flows;
            memcpy(bufs[i], tmpl[fl], (size_t)buflen);
            bm_stamp(bufs[i], seq[fl]++, now_ns(CLOCK_REALTIME));
        }
        write_pcap(dump, bufs, n, buflen);
        printf("{\"dump\":\"%s\",\"frames\":%d,\"buflen\":%d,\"run_id\":%d}\n", dump, n, buflen, run_id);
        return 0;
    }
    if (!ifname) usage();

    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd < 0) { perror("socket(AF_PACKET) -- needs CAP_NET_RAW"); return 1; }
    int one = 1;
    if (setsockopt(fd, SOL_PACKET, PACKET_QDISC_BYPASS, &one, sizeof one) < 0) perror("PACKET_QDISC_BYPASS");
    struct sockaddr_ll sll = {0};
    sll.sll_family = AF_PACKET;
    sll.sll_protocol = htons(ETH_P_ALL);
    sll.sll_ifindex = (int)if_nametoindex(ifname);
    if (!sll.sll_ifindex) { fprintf(stderr, "no interface %s\n", ifname); return 1; }
    if (bind(fd, (struct sockaddr *)&sll, sizeof sll) < 0) { perror("bind"); return 1; }
    /* We never read from this socket; keep its receive queue from holding frames. */
    int zero = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &zero, sizeof zero);

    struct mmsghdr msgs[MAX_BURST];
    struct iovec iov[MAX_BURST];
    memset(msgs, 0, sizeof msgs);
    for (int i = 0; i < MAX_BURST; i++) {
        iov[i].iov_base = bufs[i];
        iov[i].iov_len = (size_t)buflen;
        msgs[i].msg_hdr.msg_iov = &iov[i];
        msgs[i].msg_hdr.msg_iovlen = 1;
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    uint64_t *per_flow_sent = calloc((size_t)flows, sizeof *per_flow_sent);
    uint64_t *per_flow_refused = calloc((size_t)flows, sizeof *per_flow_refused);
    if (!per_flow_sent || !per_flow_refused) { perror("calloc"); return 1; }
    uint64_t sent = 0, errors = 0, bursts = 0, late_bursts = 0, catchup_bursts = 0, max_lag_ns = 0;
    int last_errno = 0, fl = 0;
    const double gap_ns = pps > 0 ? (double)burst * 1e9 / pps : 0;
    struct rusage ru0, ru1;
    getrusage(RUSAGE_SELF, &ru0);
    const uint64_t t0 = now_ns(CLOCK_MONOTONIC);
    const uint64_t t_end = t0 + (uint64_t)(duration * 1e9);
    uint64_t t = t0;

    while (!stop_flag) {
        if (pps > 0) {
            uint64_t due = t0 + (uint64_t)((double)bursts * gap_ns);
            if (due >= t_end) break;
            while ((t = now_ns(CLOCK_MONOTONIC)) < due) { /* busy-wait: sub-microsecond schedule */ }
            if (t - due > max_lag_ns) max_lag_ns = t - due;
            if (t - due > (uint64_t)(gap_ns / 2)) late_bursts++;
            if (t - due > (uint64_t)gap_ns) catchup_bursts++;   /* more than one whole gap behind */
        } else if ((t = now_ns(CLOCK_MONOTONIC)) >= t_end) {
            break;
        }
        int first_flow = fl;
        uint64_t stamp = now_ns(CLOCK_REALTIME);
        for (int i = 0; i < burst; i++) {
            memcpy(bufs[i], tmpl[fl], (size_t)buflen);
            bm_stamp(bufs[i], seq[fl]++, stamp);
            if (++fl == flows) fl = 0;
        }
        int done = 0;
        while (done < burst) {
            int r = sendmmsg(fd, msgs + done, (unsigned)(burst - done), 0);
            if (r > 0) { done += r; continue; }
            last_errno = errno;
            errors++;                  /* the frame at `done` was refused: count it and move on */
            per_flow_refused[(first_flow + done) % flows]++;
            done++;
        }
        /* attribute attempted frames to flows (refusals were charged above); refused frames keep
           their seq so the receiver counts them as lost, which is what they are from the switch's
           point of view */
        int f2 = first_flow;
        for (int i = 0; i < burst; i++) { per_flow_sent[f2]++; if (++f2 == flows) f2 = 0; }
        sent += (uint64_t)burst;
        bursts++;
    }
    const uint64_t t_stop = now_ns(CLOCK_MONOTONIC);
    getrusage(RUSAGE_SELF, &ru1);
    double secs = (double)(t_stop - t0) / 1e9;
    double cpu = (double)(ru1.ru_utime.tv_sec - ru0.ru_utime.tv_sec + ru1.ru_stime.tv_sec - ru0.ru_stime.tv_sec)
               + (double)(ru1.ru_utime.tv_usec - ru0.ru_utime.tv_usec + ru1.ru_stime.tv_usec - ru0.ru_stime.tv_usec) / 1e6;

    printf("{\"tool\":\"bmgen\",\"if\":\"%s\",\"frame\":%d,\"buflen\":%d,\"target_pps\":%.1f,\"burst\":%d,"
           "\"flows\":%d,\"duration_s\":%.6f,\"attempted\":%llu,\"send_errors\":%llu,\"last_errno\":%d,"
           "\"accepted\":%llu,\"accepted_pps\":%.1f,\"bursts\":%llu,\"gap_us\":%.3f,\"late_bursts\":%llu,\"catchup_bursts\":%llu,"
           "\"max_lag_us\":%.1f,\"run_id\":%d,\"cpu_s\":%.3f,\"per_flow_attempted\":[",
           ifname, frame, buflen, pps, burst, flows, secs, (unsigned long long)sent,
           (unsigned long long)errors, last_errno, (unsigned long long)(sent - errors),
           (double)(sent - errors) / secs, (unsigned long long)bursts, gap_ns / 1e3,
           (unsigned long long)late_bursts, (unsigned long long)catchup_bursts,
           (double)max_lag_ns / 1e3, run_id, cpu);
    for (int i = 0; i < flows; i++) printf("%s%llu", i ? "," : "", (unsigned long long)per_flow_sent[i]);
    printf("],\"per_flow_accepted\":[");
    for (int i = 0; i < flows; i++)
        printf("%s%llu", i ? "," : "", (unsigned long long)(per_flow_sent[i] - per_flow_refused[i]));
    printf("]}\n");
    return 0;
}
