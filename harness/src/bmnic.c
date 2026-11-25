/* bmnic -- turn off rx/tx checksum and scatter-gather offload on one host veth, as the p4lang
 * tutorials do for every Mininet host (utils/p4_mininet.py, P4Host.config: ethtool --offload
 * eth0 rx|tx|sg off).
 *
 * Why: with TX checksum offload a frame the kernel stack sends (iperf3, TCP or UDP) leaves the
 * host veth with only a partial L4 checksum; bmv2 reads it with pcap, forwards the bytes as they
 * are, and the receiving host drops it as corrupt. bmgen/bmrecv use raw sockets and are unaffected.
 *
 * Usage: bmnic IFNAME  (run inside the host's netns with cap_net_admin, i.e. through
 * `bmbench-priv exec`). Prints one JSON line with the read-back state; exit 0 if rx/tx checksum
 * and sg all read back off, 1 if any is still on, 2 on a usage or ioctl error.
 */
#include <errno.h>
#include <linux/ethtool.h>
#include <linux/sockios.h>
#include <net/if.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

static int fd;
static const char *ifname;

static int eth(unsigned cmd, unsigned *data)
{
    struct ethtool_value ev = { .cmd = cmd, .data = data ? *data : 0 };
    struct ifreq ifr;
    memset(&ifr, 0, sizeof ifr);
    strncpy(ifr.ifr_name, ifname, IFNAMSIZ - 1);
    ifr.ifr_data = (void *)&ev;
    if (ioctl(fd, SIOCETHTOOL, &ifr) < 0) {
        printf("{\"tool\": \"bmnic\", \"if\": \"%s\", \"error\": \"ethtool cmd 0x%x: %s\"}\n",
               ifname, cmd, strerror(errno));
        return -1;
    }
    if (data)
        *data = ev.data;
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 2 || strlen(argv[1]) >= IFNAMSIZ) {
        fprintf(stderr, "usage: bmnic IFNAME\n");
        return 2;
    }
    ifname = argv[1];
    fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        perror("socket");
        return 2;
    }
    unsigned zero = 0;
    const unsigned set[] = { ETHTOOL_SRXCSUM, ETHTOOL_STXCSUM, ETHTOOL_SSG };
    for (size_t i = 0; i < sizeof set / sizeof set[0]; i++) {
        zero = 0;
        if (eth(set[i], &zero) < 0)
            return 2;
    }
    unsigned rx = 1, tx = 1, sg = 1, tso = 1;
    if (eth(ETHTOOL_GRXCSUM, &rx) < 0 || eth(ETHTOOL_GTXCSUM, &tx) < 0 ||
        eth(ETHTOOL_GSG, &sg) < 0 || eth(ETHTOOL_GTSO, &tso) < 0)
        return 2;
    printf("{\"tool\": \"bmnic\", \"if\": \"%s\", \"rx_csum\": %u, \"tx_csum\": %u, \"sg\": %u, "
           "\"tso\": %u}\n", ifname, rx, tx, sg, tso);
    return (rx || tx || sg) ? 1 : 0;
}
