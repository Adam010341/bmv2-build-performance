#!/usr/bin/env python3
"""Draw the two figures in the README.

    python3 figures/make_figures.py

capacity-by-build.png: packets per second of the IPv4 router program (64-byte frames) for five
setups. Values are the medians of five runs each (results of experiments E1 and E1-RT).
reported-rates.png: the largest bmv2 packet rate reported by each census paper that gives one,
against what three builds reach on my machine (data/reported-pps.json).
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, NullFormatter  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
INK, MUTED, GRID, BAR, ACCENT = "#1f1f1f", "#6b6b6b", "#e4e4e1", "#a9b8c9", "#2a78d6"

SETUPS = [  # (label, kpps)
    ("P4 tutorial setup\n(-O0, logging macros, logging on)", 4.47),
    ("CMake default / Docker image\n(-O0, logging macros)", 5.92),
    ("autotools default\n(-O2, logging macros)", 24.94),
    ("-O0 without logging macros", 49.08),
    ("recommended build\n(-O3, no logging macros)", 216.26),
]


def style():
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.linewidth": 0.6,
                         "xtick.major.width": 0.6, "ytick.major.width": 0.6,
                         "axes.edgecolor": MUTED, "xtick.color": MUTED, "ytick.color": INK})


def capacity_by_build(out):
    fig, ax = plt.subplots(figsize=(7.2, 2.9), dpi=200)
    labels = [s[0] for s in SETUPS]
    vals = [s[1] for s in SETUPS]
    colors = [BAR] * (len(SETUPS) - 1) + [ACCENT]
    ax.barh(range(len(SETUPS)), vals, color=colors, height=0.62, zorder=3)
    for i, v in enumerate(vals):
        ax.text(v + 3, i, f"{v:.3g} kpps", va="center", ha="left", color=INK, fontsize=8.5)
    ax.text(216.26 + 3, 4 - 0.3, "48x the tutorial setup", va="center", ha="left", color=MUTED,
            fontsize=7.5)
    ax.set_yticks(range(len(SETUPS)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlim(0, 310)
    ax.set_xlabel("packets forwarded per second (thousands), IPv4 router, 64-byte frames", color=MUTED)
    ax.grid(axis="x", color=GRID, lw=0.6, zorder=0)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis="y", length=0)
    fig.tight_layout(pad=0.4)
    fig.savefig(out, facecolor="white")


def reported_rates(src, out):
    xs = sorted(json.load(open(src))["used"].values())
    n = len(xs)
    fig, ax = plt.subplots(figsize=(7.2, 2.4), dpi=200)
    ys = [(i + 1) / n for i in range(n)]
    ax.step([xs[0] / 1.5] + xs, [0] + ys, where="post", color=ACCENT, lw=1.6, zorder=3)
    for label, c, side, y in [("CMake default", 5920, "right", 0.78),
                              ("autotools default", 24943, "left", 0.08),
                              ("recommended build", 216258, "left", 0.08)]:
        ax.axvline(c, color=MUTED, lw=0.8, ls=(0, (3, 2)), zorder=2)
        ax.text(c * (1.12 if side == "left" else 1 / 1.12), y, label, color=MUTED, fontsize=7.5,
                ha=side, va="bottom")
    ax.set_xscale("log")
    ax.set_xlim(2, 2e6)
    ax.set_ylim(0, 1.02)
    ax.xaxis.set_major_locator(FixedLocator([10, 100, 1e3, 1e4, 1e5, 1e6]))
    ax.set_xticklabels(["10", "100", "1k", "10k", "100k", "1M"])
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_yticks([0, 0.5, 1])
    ax.set_yticklabels(["0", "0.5", "1"])
    ax.grid(axis="y", color=GRID, lw=0.6, zorder=0)
    ax.set_xlabel("largest bmv2 packet rate the paper reports (packets/s)", color=MUTED)
    ax.set_ylabel(f"share of the {n} papers", color=MUTED)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout(pad=0.4)
    fig.savefig(out, facecolor="white")


if __name__ == "__main__":
    style()
    capacity_by_build(os.path.join(HERE, "capacity-by-build.png"))
    reported_rates(os.path.join(HERE, "..", "data", "reported-pps.json"),
                   os.path.join(HERE, "reported-rates.png"))
