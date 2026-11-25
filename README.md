# How bmv2 Performance Depends on Build and Runtime Options

bmv2, the reference P4 software switch, is the data plane on which hundreds of papers measure the
throughput, latency and loss of their designs. These figures depend on how bmv2 was compiled and
started, yet the P4 tutorials, bmv2's CMake default and the official Docker image all build it
without optimisation and with logging macros, while its documentation recommends an optimised build
without them, and papers almost never mention these configurations. In this project, we measure
how much the build and start-up options change bmv2's forwarding capacity, and we examine how 253
papers that report bmv2 figures describe their setup.
A short paper describing this work is currently under review.

## Findings

**Build configuration.** On our machine, bmv2 with the recommended build forwards 216 kpps with an
IPv4 router program and 64-byte frames, 48 times as many frames as bmv2 built and started as in the
P4 tutorials (4.47 kpps). bmv2's CMake default and the official Docker image forward 5.9 kpps. Most
of this gap comes from the logging macros: removing them makes bmv2 8.3x faster at `-O0`, whereas
going from `-O0` to `-O2` without the macros makes it only 4.4x faster. Although nothing is logged,
at least half of the CPU cycles that bmv2 spends outside the kernel in the CMake default go to
formatting log messages.

![Capacity of the router program for five setups](figures/capacity-by-build.png)

**Comparisons between programs.** The build also changes comparisons between P4 programs. In 18 of
the 78 pairs of the 13 programs that we tested, the program that is faster with one build is slower
with the other. Programs from published papers gain as well: three of them forward 36, 47 and 5.4
times as many frames with the recommended build as with the CMake default.

**How papers report bmv2 performance.** None of the 253 papers reports its full build configuration
in the text, and for 200 of them neither the text nor a linked repository mentions the build. Only
60 papers report packets per second, and only 6 show by measurement that the traffic generator or
the receiving host was not the bottleneck. The figure below plots the largest bmv2 rate in the 35
of these 60 papers whose figures we could convert. In 22 of them, this rate is below what even
bmv2's CMake default forwards on our machine. A reader therefore cannot tell from such a figure
whether bmv2, the link or the traffic source was the bottleneck.

![Largest bmv2 rate reported by 35 papers](figures/reported-rates.png)

## Reporting Checklist

Based on our census and our measurements, a bmv2 figure can be interpreted only if the paper also
reports the following six setup details:

1. **Build.** The optimisation level and whether the logging macros and the event logger were
   compiled in, best given as the full `configure` or CMake command line.
2. **Version and installation.** The bmv2 release or commit and how bmv2 was installed (tutorial
   VM, image tag, package, source).
3. **Run time.** The target (`simple_switch` or `simple_switch_grpc`) and the recording options
   (`--log-console`, `--log-file`, `-L`, `--pcap`, `--nanolog`).
4. **Traffic.** Packets per second with the frame size, the burstiness of the traffic, and where
   loss is counted.
5. **Instrument.** A measurement showing that the generator and the receiving host can carry more
   than the reported rate.
6. **Host.** CPU model, frequency control, and any VM or container.

## Measurement Setup

We run all measurements on one machine (Intel Core i5-12400F, Ubuntu 24.04) with the performance
governor and turbo disabled. We pin the switch, the generator and the receiver to separate physical
cores, and, as in Mininet, we run hosts and switch in separate network namespaces connected by veth
pairs.

We wrote a traffic generator and a receiver in C that use Linux raw packet sockets (`AF_PACKET`).
In each trial we verify that the instrument was not the limit: the generator must be able to send
at least three times the measured capacity, the frame counts of the generator and the receiver must
match the kernel's interface counters, and no process outside the experiment may use more than 5 %
of a CPU. A minimal forwarder that opens the ports through libpcap as bmv2 does reaches about four
times the rate of the fastest bmv2 build, so bmv2's own processing is the bottleneck.

We build bmv2 in all 12 combinations of optimisation level (`-O0`/`-O2`/`-O3`), logging macros
on/off and event logger on/off. Before running any trial, we registered the experiments, the
expected outcomes and the rules for judging them. Each figure is the median of five switch
instances, with a 95 % bootstrap confidence interval.

Experiment binaries never run as root. A small wrapper (`harness/priv/bmbench-priv`) is the only
program allowed through `sudo`; it sets up the namespaces and CPU mode, and runs each tool as a
normal user holding only `CAP_NET_RAW` and `CAP_NET_ADMIN`.

## Repository Layout

```
harness/
  src/      traffic generator (bmgen), receiver (bmrecv), minimal libpcap forwarder (bmnull),
            and bmnic, which turns off NIC offloads on the host veths as the P4 tutorials do
  run/      experiment orchestration (bench.py), plan generation, analysis and reports
  plans/    the experiment plans (JSON) that bench.py runs
  builds/   scripts that build the 12 bmv2 configurations and fetch the Docker image
  p4/       generator of the P4 test programs
  priv/     the root wrapper and the one-time machine setup
  tests/    offline tests, with fake binaries in place of bmv2, sudo and the NIC tools
data/
  census-253.csv     the 253 papers with the 14 setup fields we recorded for each
  reported-pps.json  the packet rates in the second figure
figures/             the figures above and the script that draws them
```

The three programs from published papers are not included; they can be obtained from their
authors' repositories.

## Running the Tests

The tests need Linux, `gcc`, `libpcap-dev` and Python 3, and no root.

```bash
cd harness
make test
python3 -m unittest discover -s tests -p 'test_*.py'
```

A measurement run needs a dedicated machine, the bmv2 builds from `harness/builds`, and the
one-time setup in `harness/priv/setup_testbed.sh`.
