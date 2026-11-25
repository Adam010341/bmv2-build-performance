/* bmnull -- single-threaded libpcap "null switch" for the bmbench harness.
 *
 * Copies every packet that arrives on --in to --out, unchanged, through the same libpcap calls
 * bmv2 uses for its ports (pcap_create + snaplen 65535 + promisc + immediate mode, pcap_dispatch
 * on the way in, pcap_inject on the way out). It does no table lookup and no parsing, so the
 * throughput it reaches is the I/O ceiling of that path on this host: whatever bmv2 does on top
 * can only be slower.
 *
 * Loop: wait up to 100 ms for --in to become readable (poll on pcap_get_selectable_fd), then
 * pcap_dispatch() whatever is there and inject each packet. The capture handle is non-blocking.
 * On --duration expiry or SIGINT/SIGTERM one JSON line is printed, with the kernel's own counters
 * for --in (pcap_stats) so drops inside the capture path are visible.
 *
 * Needs CAP_NET_RAW (like bmv2 itself); there is no offline mode.
 *
 */
#define _GNU_SOURCE
#include <getopt.h>
#include <pcap/pcap.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

static volatile sig_atomic_t stop_flag = 0;
static void on_signal(int s) { (void)s; stop_flag = 1; }

static uint64_t received = 0, injected = 0, inject_errors = 0;
static pcap_t *out_h;

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void usage(void) {
    fprintf(stderr, "usage: bmnull --in IFACE --out IFACE [--duration S]\n");
    exit(2);
}

/* Open an interface the way bmv2 does. */
static pcap_t *open_port(const char *ifname) {
    char err[PCAP_ERRBUF_SIZE];
    pcap_t *h = pcap_create(ifname, err);
    if (!h) { fprintf(stderr, "pcap_create(%s): %s\n", ifname, err); exit(1); }
    if (pcap_set_snaplen(h, 65535) || pcap_set_promisc(h, 1) || pcap_set_immediate_mode(h, 1)) {
        fprintf(stderr, "pcap_set_*(%s): %s\n", ifname, pcap_geterr(h));
        exit(1);
    }
    int r = pcap_activate(h);
    if (r < 0) { fprintf(stderr, "pcap_activate(%s): %s\n", ifname, pcap_geterr(h)); exit(1); }
    if (r > 0) fprintf(stderr, "pcap_activate(%s) warning: %s\n", ifname, pcap_geterr(h));
    return h;
}

static void on_packet(u_char *user, const struct pcap_pkthdr *hdr, const u_char *bytes) {
    (void)user;
    received++;
    if (pcap_inject(out_h, bytes, hdr->caplen) < 0) inject_errors++;
    else injected++;
}

int main(int argc, char **argv) {
    const char *in = NULL, *out = NULL;
    double duration = 10;
    static struct option opts[] = {{"in", 1, 0, 'i'}, {"out", 1, 0, 'o'}, {"duration", 1, 0, 'd'}, {0, 0, 0, 0}};
    int o;
    while ((o = getopt_long(argc, argv, "", opts, NULL)) != -1) {
        if (o == 'i') in = optarg;
        else if (o == 'o') out = optarg;
        else if (o == 'd') duration = atof(optarg);
        else usage();
    }
    if (!in || !out || duration <= 0) usage();

    pcap_t *in_h = open_port(in);
    out_h = open_port(out);
    char err[PCAP_ERRBUF_SIZE];
    if (pcap_setnonblock(in_h, 1, err) < 0) { fprintf(stderr, "pcap_setnonblock: %s\n", err); return 1; }
    struct pollfd pfd = { pcap_get_selectable_fd(in_h), POLLIN, 0 };
    if (pfd.fd < 0) { fprintf(stderr, "%s has no selectable fd\n", in); return 1; }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    const uint64_t t0 = now_ns(), t_end = t0 + (uint64_t)(duration * 1e9);
    while (!stop_flag && now_ns() < t_end) {
        poll(&pfd, 1, 100);
        if (pcap_dispatch(in_h, -1, on_packet, NULL) == PCAP_ERROR) {
            fprintf(stderr, "pcap_dispatch: %s\n", pcap_geterr(in_h));
            break;
        }
    }
    const double secs = (double)(now_ns() - t0) / 1e9;

    struct pcap_stat ps = {0};
    if (pcap_stats(in_h, &ps) < 0) fprintf(stderr, "pcap_stats: %s\n", pcap_geterr(in_h));
    printf("{\"tool\":\"bmnull\",\"in\":\"%s\",\"out\":\"%s\",\"duration_s\":%.6f,\"received\":%llu,"
           "\"injected\":%llu,\"inject_errors\":%llu,\"pcap_ps_recv\":%u,\"pcap_ps_drop\":%u,"
           "\"pcap_ps_ifdrop\":%u}\n",
           in, out, secs, (unsigned long long)received, (unsigned long long)injected,
           (unsigned long long)inject_errors, ps.ps_recv, ps.ps_drop, ps.ps_ifdrop);
    pcap_close(in_h);
    pcap_close(out_h);
    return 0;
}
