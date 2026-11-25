# How much does the bmv2 build matter?

bmv2 is the reference software switch for P4, and hundreds of research papers report throughput,
latency or loss measured on it. Those figures depend heavily on how bmv2 was compiled and started,
but papers almost never say.

This project does two things:

1. **Measures** how much the build and start-up options change bmv2's forwarding rate, on one
   carefully controlled machine.
2. **Surveys** 253 papers that report bmv2 performance and records which setup details each one
   states.

The full write-up is a short paper that is currently under review. This repo holds the
measurement tooling, the survey data and a summary of the results.

## Main findings

![Capacity by build](figures/capacity-by-build.png)

- **The build changes the packet rate by up to 48x.** With an IPv4 router program and 64-byte
  frames, bmv2 built and started the way the P4 tutorials do it forwards 4.5k packets/s. The build
  that bmv2's own performance docs recommend forwards 216k packets/s. bmv2's CMake default and the
  official Docker image both land at the slow end (5.9k).
- **Logging macros cost the most, even when nothing is logged.** Compiling them out makes bmv2
  about 8x faster. Going from `-O0` to `-O2` gives only about 4x. In the default build, at least
  half of bmv2's user-space CPU time goes to formatting log messages.
- **The build can flip comparisons between P4 programs.** I measured 13 programs. In 18 of the
  78 pairs, the program that is faster with one build is slower with the other. So "my program
  adds X% overhead" depends on a build the paper usually does not name.
- **The same holds for programs from published papers.** Three programs taken from other papers'
  repositories ran 36x, 47x and 5.4x faster with the recommended build than with the CMake
  default.
- **Papers rarely report the build.** None of the 253 papers gives its full build configuration.
  For 200 of them, neither the paper nor its linked repository says anything about the build.
  Only 6 show that their traffic generator was not the bottleneck.

![Reported rates](figures/reported-rates.png)

The figure above takes the 35 papers whose reported packet rate I could convert. In 22 of them,
the highest rate is below what even the slowest default build forwards on my machine. That
doesn't mean those papers used a slow build, but a reader has no way to tell.

## What I suggest papers report

1. **Build:** optimisation level, and whether logging macros and the event logger are compiled
   in (ideally the exact `configure`/CMake command).
2. **Version and install method:** release or commit, and tutorial VM, Docker image, package or
   source.
3. **Run time:** `simple_switch` or `simple_switch_grpc`, plus the logging and pcap options.
4. **Traffic:** packets per second with the frame size, burstiness, and where loss is counted.
5. **Instrument:** evidence that the traffic generator and receiver were not the bottleneck.
6. **Host:** CPU model, frequency settings, and any VM or container.

## How the measurements work

- **Hardware and isolation:** everything runs on one machine (Intel i5-12400F, Ubuntu 24.04)
  with CPU frequency fixed and turbo off. The switch, the generator and the receiver are pinned
  to separate physical cores. Hosts and switch live in separate network namespaces joined by
  veth pairs, as in Mininet.
- **Traffic tools:** I wrote my own traffic generator and receiver in C, using raw packet
  sockets (`AF_PACKET`). Every trial is checked automatically:
  - the generator must be able to send at least 3x the measured rate;
  - the tools' frame counts must match the kernel's interface counters;
  - no unrelated process may use more than 5% of a CPU.
- **Checking the packet path itself:** a minimal libpcap forwarder runs on the same path. The
  fastest bmv2 build reaches only 24% of its rate, so bmv2 is the bottleneck, not the veth
  pairs or libpcap.
- **Pre-registration and statistics:** before running anything, I wrote down the experiments,
  the expected outcomes and the rules for judging them. Each figure is the median of 5
  independent switch instances, with bootstrap 95% confidence intervals.
- **Privileges:** the harness never runs experiments as root. A small wrapper
  (`harness/priv/bmbench-priv`) is the only thing allowed through `sudo`. It sets up the
  namespaces and CPU mode, then drops to a normal user with only `CAP_NET_RAW` and
  `CAP_NET_ADMIN`.

## Repository layout

```
harness/
  src/      C tools: traffic generator (bmgen), receiver (bmrecv), minimal libpcap forwarder
            (bmnull), and bmnic, which turns off NIC offloads on the host veths like the P4 tutorials
  run/      Python orchestration (bench.py), plan generation, analysis and reports
  plans/    the experiment plans (JSON) that bench.py executes
  builds/   scripts that build the 12 bmv2 configurations and fetch the Docker image
  p4/       generator for the P4 test programs
  priv/     the root wrapper and one-time machine setup
  tests/    offline tests; fake binaries stand in for bmv2, sudo and the NIC tools
data/
  census-253.csv     the 253 surveyed papers with their 14 setup fields
  reported-pps.json  the packet rates used in the second figure
figures/             the README figures and the script that draws them
```

The three programs from published papers are not included here. They belong to their authors and
can be fetched from the original repositories.

## Running it

The tests run on any Linux machine with `gcc`, `libpcap-dev` and Python 3. No root is needed.

```bash
cd harness
make test                                              # builds the C tools, checks the frame format
python3 -m unittest discover -s tests -p 'test_*.py'   # Python tests
```

A real measurement run needs a dedicated machine, the bmv2 builds from `harness/builds`, and the
one-time setup in `harness/priv/setup_testbed.sh`.
