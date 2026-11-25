#!/usr/bin/env python3
"""report.py -- the registered analyses of PREREG v1 (+ AMENDMENT-1..6), one report per experiment.

    report.py RAW_DIR OUT_DIR

RAW_DIR holds e1.jsonl, e1rt.jsonl, e1rt-grpc.jsonl, e2.jsonl ... e5.jsonl and preround/ (G1
calibration g1-calibration.jsonl, G9 ceiling g9-null.jsonl). For each experiment it writes
OUT_DIR/<exp>.json (everything, full precision) and OUT_DIR/<exp>.md (short tables), plus
OUT_DIR/CHOICES.md: every place where the prereg left a choice open and the reading taken here.

Rules shared by every experiment (PREREG §2, A1.2, A1.4, AMENDMENT-5/6):
  * a trial's gate failures are analyse.gates() (bench.py's run-time flags + G8 over the steady
    window) plus G7 (instance probe) and G6 (binary changed) from the instance records;
  * the superseded first attempt of a rerun is dropped; a trial with an analyse.EXCLUDING flag is
    excluded from estimates and counted; every other failing trial is kept and counted;
  * a cell's value is the median over instances, 95 % bootstrap interval (10 000 resamples over
    instances); ratios bootstrap each cell independently (as analyse.py) unless stated;
  * a registered inequality holds if the whole interval is on the predicted side, fails if the
    whole interval is on the other side, else inconclusive (A1.4);
  * every capacity cell carries the G1 check (C <= 1/3 of the generator ceiling), the GF flag
    (generator-bounded -> lower bound), and the G9 check (C > 0.5 x bmnull ceiling -> possibly
    I/O-bound, with the ratio); G8 is shown as counted and with AMENDMENT-6's busy-CPU diagnostic.
Stdlib only.
"""
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyse  # noqa: E402
from analyse import EXCLUDING, B, boot_median, ci, gates  # noqa: E402

SW_CPUS = ("4", "6", "8", "10")
DEFAULT_LIKE, PERF = "cm-o0-L1-E1", "cm-o3-L0-E0"
FACT = {(o, l, e): f"cm-o{o}-L{l}-E{e}" for o in (0, 2, 3) for l in (1, 0) for e in (1, 0)}
# ladders exactly as make_plans.py writes them (rungs are matched by rank inside an instance)
E4_LADDER = (0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0, 1.2, 1.5)
E5D_LADDER = (0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0, 1.2)
E5_RUNGS = (0.3, 0.5, 0.7, 0.85)
E2_PROGS = ["min", "l3", "exact_1", "exact_2", "exact_4", "exact_8", "lpm_1", "lpm_4", "lpm_8",
            "ternary_1", "ternary_4", "ternary_8", "l3_reg"]
E2_KINDS = {"exact": (1, 2, 4, 8), "lpm": (1, 4, 8), "ternary": (1, 4, 8)}
E3_FRAMES = (64, 128, 256, 512, 1024, 1280, 1518)
E5C_SIMS = 2000
CLEAN = 0.005             # the 0.5 % criterion of E4.c / E5.c (v1)

CHOICES = [
    ("§2 G1 / A1.2", "G1 ceiling per frame = median accepted_pps of the preround B=32 calibration runs; a cell fails G1 if its median C > ceiling/3 and is then reported as '>= C, generator-bounded'."),
    ("§2 G1 (deviation in the data)", "§2 registers 5 x 10 s calibration runs per frame size; arms.py calibrate() runs reps=3, so the G1 ceilings here are medians of 3 runs (preround.json lists them)."),
    ("analyse.EXCLUDING (beyond the prereg)", "report.py excludes trials with any analyse.EXCLUDING flag: G4 after its rerun (§2/§4), G7 and G6 (instance gates), and also `no-drain` and `G2:gen-vs-h0tx`, which the prereg does not name as excluding (A1.1.4/A1.1.5 define them; A1.2 says failing trials are reported, not dropped). Occurrences of these two in the data are counted at the end of this file."),
    ("No measurement", "A trial whose capacity value is None (fewer than two steady-window samples) is never dropped silently: it is listed in the cell's no_value_trials and makes the cell a §4 failed cell; a value of 0 is kept as a measurement."),
    ("§4 failed cell", "A cell with any excluded trial (an excluding gate still failing after the one rerun, or no value) is shown as FAILED CELL (§4) with the failing instances; its estimate from the remaining instances is reported as secondary."),
    ("A1.2 G9", "G9 ceiling per frame = median delivered_pps_steady of the bmnull sat runs; a cell is flagged 'possibly I/O-bound' if its median C > 0.5 x ceiling; the ratio C/ceiling is reported for every capacity cell."),
    ("A1.2 GF", "A cell is a lower bound ('generator-bounded') if any included trial of it carries the run-time GF flag 'generator-bounded' (as analyse.py)."),
    ("A1.2 'reported, not dropped'", "Estimates use non-superseded trials without an analyse.EXCLUDING flag and whose instance passed G7 and G6; all other gate failures (G8, G3, schedule, generator-bounded) are kept and counted per cell."),
    ("AMENDMENT-6", "G8 diagnostic counts (trial, switch CPU) pairs of the reported (non-superseded) trials, split at 90 % busy, twice: raw (cpu_busy_per_cpu as recorded, over the snapshot window g4_window_s) and normalised to the generator window (busy x g4_window_s / duration); the counts over all attempts (incl. superseded), which is what AMENDMENT-6's numbers count, are given next to them."),
    ("AMENDMENT-6 sensitivity", "'Reported with the results': every verdict is recomputed with G8-failing trials treated as excluded (cells left empty give 'no estimate'), and each <exp>.md lists the verdicts that change."),
    ("§4 schedule sensitivity", "Every verdict is also recomputed with trials still flagged `schedule` after their one rerun treated as failed (§4: a second failure is a failed cell); for E4 a zero-loss search with such a step then has no Z. The registered verdicts keep these trials (A1.2: 'reported, not dropped'); the per-part `schedule` counts are shown in the E4.a and E5.c rows."),
    ("§3 E1 / A1.4", "Bootstrap: each cell resampled independently with random.Random('<EXP>|<cell key>') (10 000 draws, analyse.boot_median/ci); a ratio's interval pairs draw i of numerator and denominator."),
    ("A1.4 '≈'", "'≈' and '∈ [a, b]' are decided like the inequalities: holds if the interval lies inside [a, b], fails if it lies wholly outside, else inconclusive."),
    ("E1.b", "Evaluated at every (logging, elogger) setting x program {min, l3} x frame {64, 1518}: holds if all 16 hold, fails if any fails, else inconclusive."),
    ("E1.c / A1.4", "Relative macro cost at O = geometric mean over elogger levels of C(O,L0,E)/C(O,L1,E); E1.c = cost(O0)/cost(O3), predicted > 1; evaluated per program x frame and combined (all hold -> holds, any fails -> fails). Shown as labelled alternatives, not verdicts: (a) the pooled ratio of ratios (geometric mean of the four parts, bootstrapped jointly), (b) the A1.4 three-way reading of the parts (holds only if all hold, fails only if all fail, else inconclusive/mixed)."),
    ("E1.d", "Evaluated on l3 at 64 and 1518 B separately; each equivalence combined over the two frames; the Docker binary vs cm-default-dbg is reported with its interval, no verdict."),
    ("E1 model / A1.4", "Main effects O3/O0, O2/O0, O2/O3 (gm over L,E), L0/L1 (gm over O,E), E0/E1 (gm over O,L); two-way interactions are ratios of the knob effect between levels of the other knob (O x L, O x E for O pairs (0,3),(0,2),(2,3); L x E as E-effect at L1 / at L0), gm over the third knob."),
    ("E1-RT run order", "The docker-grpc cells come from e1rt-grpc.jsonl, a separate run after the main E1-RT run (AMENDMENT-5); their instances were not interleaved with the other builds', and the md says so with both runs' start/end times."),
    ("E1-RT.a", "Denominator C(cm-o3-L0-E0, none) is E1's cell (l3, 64 B), since E1-RT does not run that build; numerator is E1-RT's cm-o0-L1-E1 tutorial cell."),
    ("AMENDMENT-5", "E1-RT records of build docker-grpc:* are taken from e1rt-grpc.jsonl only; any record of that build in e1rt.jsonl is ignored."),
    ("E2 t_b", "Per-table time = OLS slope of 1e6/C (µs per packet) on K over the programs of that match kind only (exact K=1,2,4,8; lpm and ternary K=1,4,8), fitted to cell medians; `min` (K=0) is not a point; interval from the per-cell bootstrap draws."),
    ("E2 / A1.4", "Decided on the 95 % interval with an inconclusive outcome: RR build-dependent if the interval lies wholly outside [0.9, 1.11], not build-dependent if wholly inside, else inconclusive; claim-level divergence if the interval of OH_O0 - OH_O3 lies wholly beyond ±0.10 (it then also excludes 0), none if wholly inside [-0.10, 0.10], else inconclusive. The first report's binary/point readings are kept in the JSON and any verdict that differs is listed. Ranking flip for any pair of the 13 programs whose within-build ratio intervals both exclude 1 on opposite sides."),
    ("E2 t_b (also)", "t_b is also given with `min` as the K = 0 point, and, descriptively, the per-table cost from K = 0 to 4 and from 4 to 8 (cell medians), because a step at K = 8 makes a single slope a poor summary."),
    ("E3", "Fit 1/C(s) = a + b s by OLS over every included trial of a build (s = frame size in bytes incl. FCS); E3.a ratio = median C(1518)/median C(64); both bootstrapped by resampling E3 instances (each instance carries all 7 sizes), seed 'E3|<build>'."),
    ("E4 C", "C in E4.a/E4.b/E4.c is E1's cell C(build, l3, frame) (the C E4 was planned from); E4's per-instance values and E1's cell are bootstrapped independently."),
    ("E4 / A1.3.4", "A zero-loss search with a failed bracket has no Z; Z is estimated over valid brackets only; a cell where every bracket failed gets verdict 'no estimate (failed bracket)'. The bound a failed bracket implies if loss is monotone in rate (the assumption every bisection Z relies on) is stated only for the way it failed: 'Z < lo' when lo lost frames, 'Z >= hi' when hi lost none; the verdict each part would then get is shown as a labelled alternative reading. A search is excluded if any of its counted steps carries an excluding flag."),
    ("E4 descriptive", "Z_B/C at B = 1, 8, 32 and the delivered rate (h1 counter / 10 s) of iperf3 rungs offered above C are tabulated against the sat C, because values above 1 mean sat C understates the sustained rate (not registered)."),
    ("E4.c", "Highest rung = the largest offered rate among rungs with iperf3's own aggregate loss <= 0.5 % (not required to be contiguous); an instance with no such rung counts as 0; evaluated per frame for cm-o3-L0-E0 and combined (all hold -> holds); cm-o0-L1-E1 shown without verdict."),
    ("E5.a/b", "C(n) from E5's own sat trials (receiver on); ratio C(256)/C(1) per build x program, cells bootstrapped independently."),
    ("E5.c / A1.3.1", "Per rate trial (non-superseded, G3-clean, not excluded) with aggregate loss L = 1 - delivered/accepted > 0: worst flow = max over flows of (accepted_f - rx_f)/accepted_f; null = 2000 draws of the same max with lost_f ~ Binomial(accepted_f, L); inside if q2.5 <= observed <= q97.5 by fraction, OR the worst flow's lost-frame count equals the lost-frame count of the q2.5 or q97.5 draw (tie-aware in frames: bmgen's round-robin gives flows accepted counts that differ by 1, so equal frame counts can have unequal fractions)."),
    ("E5.c verdicts", "A cell holds if every lossy trial is inside, fails otherwise; a cell with no lossy trial is 'no estimate (no lossy trial: not tested)', never 'holds'; an n = 1 cell 'cannot fail' (its worst flow is the aggregate). The build-level E5.c pools the cells with n > 1 that were tested ('no estimate' if none). The share of outside trials expected under the null itself (1 - prod(1 - p_i), p_i from each trial's own simulation) is reported as description, not as a verdict."),
    ("E5.c v1 criterion", "Per instance and burst: largest rung whose worst flow loses <= 0.5 % (per-flow criterion) and largest rung whose aggregate loss <= 0.5 % (aggregate criterion), reported as aggregate offered pps and as fraction of the instance's own C; 0 if no rung qualifies."),
    ("E5 reading / A1.4", "Applied per build with the build-level E5.c; when E5.a fails for l3 and also holds for exactly one program, both applicable readings are listed; E5.a holding for both programs with E5.c not tested gives 'no registered reading: E5.c not tested'. Whether a per-flow decline exists at all (median per-flow clean rung lower at n = 16 or 256 than at n = 1, any program/burst) is reported next to the reading."),
    ("E5.d", "No decision rule is registered; the iperf3 rungs are tabulated and the per-flow 0.5 % criterion is applied descriptively (labelled as such)."),
    ("E5.d (deviation in the plan)", "The ladder that ran (make_plans.py e5(): 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0, 1.2 x C, divided over n flows) differs from the registered 'same per-flow ladder as E4' (0.1 ... 1.5 x C); the rungs are reported as run."),
    ("§5", "Estimation only, on E1 l3 cells: debugger = cm-default-dbg/cm-default; -march=native = cm-o3native-L0-E0/cm-o3-L0-E0; NDEBUG = cm-release-L0-E0/cm-o3-L0-E0; Docker = docker/cm-default-dbg and docker/cm-default; elogger = the E0/E1 main effect; per-thread CPU = switch threads' CPU (jiffies / 100 / g4_window_s) ranked per trial, median per rank."),
    ("§6", "Robustness checks need runs that are not in this campaign's raw dir; nothing is computed for §6."),
    ("G5, G10", "Build identity (G5) and table hits (G10) are pre-round checks (preround/CHECK); not recomputed here."),
]


# ------------------------------------------------------------------------------------ basics
def load(path):
    return analyse.load([path]) if os.path.exists(path) else []


def kstr(key):
    return "|".join(map(str, key))


def med(xs):
    return statistics.median(xs) if xs else None


def v_above(iv, thr, strict=True):
    """predicted: value > thr (strict) or >= thr."""
    if iv is None:
        return "no estimate"
    lo, hi = iv
    if (lo > thr) if strict else (lo >= thr):
        return "holds"
    if (hi <= thr) if strict else (hi < thr):
        return "fails"
    return "inconclusive"


def v_below(iv, thr):
    """predicted: value < thr."""
    if iv is None:
        return "no estimate"
    lo, hi = iv
    if hi < thr:
        return "holds"
    if lo >= thr:
        return "fails"
    return "inconclusive"


def v_within(iv, a, b):
    """predicted: value in [a, b]."""
    if iv is None:
        return "no estimate"
    lo, hi = iv
    if a <= lo and hi <= b:
        return "holds"
    if hi < a or lo > b:
        return "fails"
    return "inconclusive"


def combine(vs):
    vs = list(vs)
    if not vs or all(v == "no estimate" for v in vs):
        return "no estimate"
    if any(v == "fails" for v in vs):
        return "fails"
    if all(v == "holds" for v in vs):
        return "holds"
    return "inconclusive"


def ols(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def f4(x):
    return "—" if x is None else f"{x:.4f}"


def f0(x):
    return "—" if x is None else f"{x:.0f}"


def fci(iv, fmt=f4):
    return "—" if iv is None else f"[{fmt(iv[0])}, {fmt(iv[1])}]"


def md_table(headers, rows):
    out = ["| " + " | ".join(h.replace("|", "\\|") for h in headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |" for row in rows]
    return "\n".join(out)


# ------------------------------------------------------------------------------ pre-round
def preround(raw):
    g1 = defaultdict(list)
    for r in load(os.path.join(raw, "preround", "g1-calibration.jsonl")):
        if r.get("record") == "calibration":
            g1[(r["frame"], r["burst"])].append(r["gen"]["accepted_pps"])
    g9 = defaultdict(list)
    g9flags = defaultdict(Counter)
    for r in load(os.path.join(raw, "preround", "g9-null.jsonl")):
        if r.get("record") == "null-forwarder" and r.get("delivered_pps_steady"):
            g9[r["frame"]].append(r["delivered_pps_steady"])
            for f in r.get("flags", []):
                g9flags[r["frame"]][f] += 1
    return {"g1": {f: med(v) for (f, b), v in g1.items() if b == 32},
            "g1_detail": {f"{f}|B{b}": {"n": len(v), "median_accepted_pps": med(v), "values": v} for (f, b), v in sorted(g1.items())},
            "g9": {f: med(v) for f, v in g9.items()},
            "g9_detail": {str(f): {"n": len(v), "median_pps": med(v), "values": v, "flags": dict(g9flags[f])} for f, v in sorted(g9.items())}}


# ------------------------------------------------------------------------- trial bookkeeping
class Exp:
    """One experiment's records: gate evaluation, exclusions, gate summary, bootstrap store."""
    BOOTS = {}     # (exp, key) -> bootstrap medians; shared so ratios can cross experiments
    EXTRA = ()     # sensitivity runs only: further flag prefixes treated as excluding ("G8", "schedule")

    def __init__(self, name, recs, pre):
        self.name, self.recs, self.pre = name, recs, pre
        self.inst_ok, self.inst_end, self.errors = {}, {}, []
        for r in recs:
            if r.get("record") == "instance":
                self.inst_ok[r["instance"]] = r.get("probe_ok")
            elif r.get("record") == "instance-end":
                self.inst_end[r["instance"]] = r.get("binary_unchanged")
            elif r.get("record") == "error":
                self.errors.append({"instance": r.get("instance"), "error": r.get("error")})
        self.trials = [r for r in recs if r.get("record") == "trial"]
        self.sup = {self.tkey(r): r.get("flags", []) for r in self.trials if r.get("superseded")}

    @staticmethod
    def tkey(r):
        return (r["instance"], r.get("kind"), r.get("frame"), r.get("flows"), r.get("burst"), r.get("pps_frac_of_sat"),
                r.get("offered_pps") if r.get("kind") == "zl-step" else None)

    def fails(self, r):
        f = gates(r) if "flags" in r or "samples" in r else []
        if r.get("kind") == "iperf3-rung" and r.get("drained") is False:
            f.append("no-drain")
        if self.inst_ok.get(r["instance"]) is False:
            f.append("G7:probe")
        if self.inst_end.get(r["instance"]) is False:
            f.append("G6:binary-changed")
        return f

    @staticmethod
    def excluded(fails):
        return any(x.startswith(EXCLUDING + Exp.EXTRA) for x in fails)

    def counted(self, kinds):
        """Non-superseded trial records of these kinds, each with its gate failures."""
        return [(r, self.fails(r)) for r in self.trials if r.get("kind") in kinds and not r.get("superseded")]

    def gate_summary(self):
        cnt, sup = Counter(), Counter()
        n = n_exc = 0
        for r in self.trials:
            if r.get("kind") in ("zl-result",):
                continue
            fl = self.fails(r)
            norm = sorted({x.split(":")[0] if x.startswith("G8") else x for x in fl})
            if r.get("superseded"):
                for x in norm:
                    sup[x] += 1
                continue
            n += 1
            n_exc += self.excluded(fl)
            for x in norm:
                cnt[x] += 1
        return {"trials_counted": n, "trials_excluded": n_exc,
                "superseded_first_attempts": sum(1 for r in self.trials if r.get("superseded")),
                "superseded_flags": dict(sup), "failures_by_gate_in_counted_trials": dict(cnt),
                "instances": len(self.inst_ok), "probe_failed_G7": sum(1 for v in self.inst_ok.values() if v is False),
                "binary_changed_G6": sum(1 for v in self.inst_end.values() if v is False),
                "error_records": self.errors,
                "g8_diagnostic": self.g8_diag(False), "g8_diagnostic_all_attempts": self.g8_diag(True)}

    def g8_diag(self, all_attempts):
        """AMENDMENT-6 split of (trial, switch CPU) pairs by busy fraction. `raw` uses cpu_busy_per_cpu as
        recorded (busy over the whole snapshot window, g4_window_s); `normalised` rescales it to the
        generator window (busy x g4_window_s / duration), so trials whose window also spans the receiver
        start and the drain (E5) are not diluted."""
        split = {m: {"ge": [0, 0], "lt": [0, 0]} for m in ("raw", "normalised")}
        trials_g8 = 0
        for r in self.trials:
            if "samples" not in r or (r.get("superseded") and not all_attempts):
                continue
            g = gates(r)
            trials_g8 += any(x.startswith("G8") for x in g)
            scale = (r["g4_window_s"] / r["duration"]) if r.get("g4_window_s") and r.get("duration") else None
            for c in SW_CPUS:
                b = r.get("cpu_busy_per_cpu", {}).get(c)
                if b is None:
                    continue
                for m, x in (("raw", b), ("normalised", b * scale if scale else None)):
                    if x is None:
                        continue
                    t = split[m]["ge" if x >= 0.9 else "lt"]
                    t[0] += f"G8:cpu{c}" in g
                    t[1] += 1
        out = {"trials_failing_G8_counted": trials_g8}
        for m in ("raw", "normalised"):
            sfx = "" if m == "raw" else "_normalised"
            out["cpu_ge_90pct_busy" + sfx] = {"fail": split[m]["ge"][0], "of": split[m]["ge"][1]}
            out["cpu_lt_90pct_busy" + sfx] = {"fail": split[m]["lt"][0], "of": split[m]["lt"][1]}
        return out

    # ---- cells
    def cells(self, kinds, keyfn, value=lambda r: r.get("delivered_pps_steady")):
        out = {}
        for r, fl in self.counted(kinds):
            key = keyfn(r)
            if key is None:
                continue
            c = out.setdefault(key, {"values": [], "instances": [], "trials": 0, "excluded": [], "no_value": [],
                                     "flags": Counter(), "g8_trials": 0, "generator_bounded": False})
            c["trials"] += 1
            for x in sorted({x.split(":")[0] if x.startswith("G8") else x for x in fl}):
                c["flags"][x] += 1
            c["g8_trials"] += any(x.startswith("G8") for x in fl)
            if self.excluded(fl):
                c["excluded"].append({"instance": r["instance"], "failed": fl,
                                      "first_attempt_failed": self.sup.get(self.tkey(r))})
                continue
            if "generator-bounded" in fl:
                c["generator_bounded"] = True
            v = value(r)
            if v is None:                         # no measurement: counted and listed, never silently lost
                c["no_value"].append(r["instance"])
                continue
            c["values"].append(v)
            c["instances"].append(r["instance"])
        return out

    def summarise(self, key, c, frame=None, capacity=True):
        xs = c["values"]
        s = {"n": len(xs), "median": med(xs), "ci95": None, "values": xs, "instances": c["instances"],
             "trials": c["trials"], "excluded_trials": c["excluded"], "no_value_trials": c["no_value"],
             "gate_failures": dict(c["flags"]), "g8_trials": c["g8_trials"],
             # §4: a trial that still fails a gate after its one rerun (or carries any other excluding
             # flag) makes the cell a failed cell; the estimate from the remaining instances is secondary
             "failed_cell_s4": [{"instance": x["instance"], "failed": [f for f in x["failed"] if f.startswith(EXCLUDING + Exp.EXTRA)],
                                 "first_attempt_failed": x.get("first_attempt_failed")}
                                for x in c["excluded"]] + [{"instance": i, "failed": ["no value"]} for i in c["no_value"]]}
        if xs:
            bs = boot_median(xs, random.Random(f"{self.name}|{kstr(key)}"))
            Exp.BOOTS[(self.name, key)] = bs
            s["ci95"] = list(ci(bs))
        if capacity and frame is not None and xs:
            g1, g9 = self.pre["g1"].get(frame), self.pre["g9"].get(frame)
            s["GF_generator_bounded"] = c["generator_bounded"]
            s["G1_ceiling_pps"] = g1
            s["G1_ok"] = (s["median"] <= g1 / 3) if g1 else None
            s["G9_ceiling_pps"] = g9
            s["G9_ratio"] = s["median"] / g9 if g9 else None
            s["G9_possibly_io_bound"] = (s["median"] > 0.5 * g9) if g9 else None
            s["lower_bound"] = bool(c["generator_bounded"] or s["G1_ok"] is False)
        return s


def ratio(ka, kb, ea, eb=None):
    """(point, ci) of median(A)/median(B) from the stored bootstrap draws, or None."""
    eb = eb or ea
    A, Bb = Exp.BOOTS.get((ea, ka)), Exp.BOOTS.get((eb, kb))
    if A is None or Bb is None or POINT[(eb, kb)] == 0 or 0 in Bb:
        return None
    return {"ratio": POINT[(ea, ka)] / POINT[(eb, kb)], "ci95": list(ci([x / y for x, y in zip(A, Bb)]))}


POINT = {}
PRE = {}


def draw(exp, key, i):
    return POINT[(exp, key)] if i is None else Exp.BOOTS[(exp, key)][i]


def have(exp, *keys):
    return all((exp, k) in Exp.BOOTS for k in keys)


def boot_stat(fn):
    """point = fn(None), ci over fn(i) for i in draws."""
    return {"value": fn(None), "ci95": list(ci([fn(i) for i in range(B)]))}


def cap_cells(ex, kinds, keyfn, framefn):
    cells = {}
    for key, c in sorted(ex.cells(kinds, keyfn).items(), key=lambda kv: kstr(kv[0])):
        s = ex.summarise(key, c, frame=framefn(key))
        if s["median"] is not None:
            POINT[(ex.name, key)] = s["median"]
        cells[key] = s
    return cells


def cell_rows(cells, label):
    rows = []
    for key, s in cells.items():
        note = []
        if s.get("lower_bound"):
            note.append("≥ (generator-bounded)" if s.get("GF_generator_bounded") else "≥ (G1)")
        if s.get("G9_possibly_io_bound"):
            note.append(f"possibly I/O-bound ({s['G9_ratio']:.3f} of bmnull)")
        if s.get("failed_cell_s4"):
            note.append("**FAILED CELL (§4)**: " + "; ".join(
                f"{x['instance']} {','.join(x['failed'])}"
                + (" on both attempts (rerun also failed: contaminated)" if x.get("first_attempt_failed") else "")
                for x in s["failed_cell_s4"]) + f"; the n={s['n']} estimate is secondary")
        gf = ", ".join(f"{k} {v}" for k, v in sorted(s["gate_failures"].items())) or "—"
        rows.append(label(key) + [s["n"], f0(s["median"]), fci(s["ci95"], f0), f4(s.get("G9_ratio")),
                                  gf, len(s["excluded_trials"]), "; ".join(note) or ""])
    return rows


CELL_HDR = ["n", "C median (pps)", "95% CI", "C/G9", "gate failures (trials)", "excluded", "note"]


def io_md(cells, pre):
    """One line: the G1 and G9 ceilings and how the capacity cells sit against them."""
    cs = [c for c in cells.values() if c.get("G9_ratio") is not None]
    if not cs:
        return ""
    g9flag = [c for c in cs if c["G9_possibly_io_bound"]]
    g1fail = [c for c in cs if c["G1_ok"] is False]
    gf = [c for c in cs if c["GF_generator_bounded"]]
    g9gb = sum(v["flags"].get("generator-bounded", 0) for v in pre["g9_detail"].values())
    g9n = sum(v["n"] for v in pre["g9_detail"].values())
    return (f"I/O checks: G1 ceiling (B=32, median of {', '.join(str(v['n']) for k, v in pre['g1_detail'].items() if k.endswith('B32'))} runs per frame) "
            + ", ".join(f"{f} B {c:.0f}" for f, c in sorted(pre["g1"].items()))
            + f" pps; cells failing G1 (C > ceiling/3): {len(g1fail)}; cells with a GF generator-bounded trial: {len(gf)}. "
            + "G9 bmnull ceiling " + ", ".join(f"{f} B {c:.0f}" for f, c in sorted(pre["g9"].items()))
            + f" pps ({g9gb} of {g9n} bmnull runs themselves carry GF 'generator-bounded', so the ceiling is a lower bound); "
            + f"cells above 0.5 x ceiling: {len(g9flag)}; largest C/ceiling {max(c['G9_ratio'] for c in cs):.4f}.")


def gate_md(gs):
    d = gs["g8_diagnostic"]
    lines = [f"Trials counted {gs['trials_counted']}, excluded {gs['trials_excluded']}, superseded first attempts "
             f"{gs['superseded_first_attempts']} {gs['superseded_flags'] or ''}; instances {gs['instances']}, G7 probe "
             f"failed {gs['probe_failed_G7']}, G6 binary changed {gs['binary_changed_G6']}, error records "
             f"{len(gs['error_records'])}.",
             f"Gate failures in counted trials: {gs['failures_by_gate_in_counted_trials'] or 'none'}.",
             f"G8 as counted: {d['trials_failing_G8_counted']} trial(s) fail.",
             "AMENDMENT-6 busy-CPU diagnostic (post hoc), (trial, switch CPU) pairs that fail G8 / pairs:"]
    for m, sfx, what in (("raw", "", "busy over the snapshot window as recorded"),
                         ("normalised", "_normalised", "busy × g4_window_s / duration, i.e. over the generator window")):
        a = gs["g8_diagnostic_all_attempts"]
        lines.append(f"- {m} ({what}): CPU ≥ 90 % busy {d['cpu_ge_90pct_busy' + sfx]['fail']}/{d['cpu_ge_90pct_busy' + sfx]['of']}, "
                     f"CPU < 90 % busy {d['cpu_lt_90pct_busy' + sfx]['fail']}/{d['cpu_lt_90pct_busy' + sfx]['of']} "
                     f"(all attempts incl. superseded: {a['cpu_ge_90pct_busy' + sfx]['fail']}/{a['cpu_ge_90pct_busy' + sfx]['of']}, "
                     f"{a['cpu_lt_90pct_busy' + sfx]['fail']}/{a['cpu_lt_90pct_busy' + sfx]['of']}).")
    n_raw, n_norm = d["cpu_ge_90pct_busy"]["of"], d["cpu_ge_90pct_busy_normalised"]["of"]
    lines.append(f"- This experiment has {n_raw} (trial, CPU) pairs ≥ 90 % busy as recorded and {n_norm} after normalising to the "
                 "generator window" + (" — the recorded split understates busy CPUs here." if n_norm > n_raw else "."))
    return "\n".join(lines)


def utc(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else None


def jkeys(cells):
    return {kstr(k): v for k, v in cells.items()}


# ------------------------------------------------------------------------------------- E1
def e1(raw, pre):
    ex = Exp("E1", load(os.path.join(raw, "e1.jsonl")), pre)
    key = lambda r: (r["build"], r["program"], r.get("runtime", "none"), r["frame"], r.get("flows", 1)) if r["kind"] == "sat" else None
    cells = cap_cells(ex, ("sat",), key, lambda k: k[3])
    K = lambda b, p, f: (b, p, "none", f, 1)
    out = {"experiment": "E1", "gates": ex.gate_summary(), "cells": jkeys(cells), "verdicts": {}, "ratios": {}}
    V = out["verdicts"]
    # E1.a (A1.4): > 2 on l3, 64 B
    r = ratio(K(PERF, "l3", 64), K("cm-o0-L0-E0", "l3", 64), "E1")
    V["E1.a"] = {"claim": "C(-O3,L0,E0)/C(-O0,L0,E0) > 2 on l3, 64 B", **(r or {}),
                 "verdict": v_above(r and r["ci95"], 2.0)}
    # E1.b: O2 ≈ O3 at every logging/elogger setting
    parts = {}
    for (l, e) in ((1, 1), (1, 0), (0, 1), (0, 0)):
        for p in ("min", "l3"):
            for f in (64, 1518):
                r = ratio(K(FACT[(2, l, e)], p, f), K(FACT[(3, l, e)], p, f), "E1")
                parts[f"L{l}E{e}|{p}|{f}"] = {**(r or {}), "verdict": v_within(r and r["ci95"], 0.9, 1.1)}
    V["E1.b"] = {"claim": "C(-O2)/C(-O3) in [0.9, 1.1] at every logging/elogger setting", "parts": parts,
                 "verdict": combine(p["verdict"] for p in parts.values())}
    # factorial model (A1.4) and E1.c
    def gm(pairs, p, f, i):
        return math.exp(sum(math.log(draw("E1", K(FACT[a], p, f), i) / draw("E1", K(FACT[b], p, f), i))
                            for a, b in pairs) / len(pairs))
    main = {"O3/O0": [((3, l, e), (0, l, e)) for l in (1, 0) for e in (1, 0)],
            "O2/O0": [((2, l, e), (0, l, e)) for l in (1, 0) for e in (1, 0)],
            "O2/O3": [((2, l, e), (3, l, e)) for l in (1, 0) for e in (1, 0)],
            "L0/L1": [((o, 0, e), (o, 1, e)) for o in (0, 2, 3) for e in (1, 0)],
            "E0/E1": [((o, l, 0), (o, l, 1)) for o in (0, 2, 3) for l in (1, 0)]}
    Leff = lambda o: [((o, 0, e), (o, 1, e)) for e in (1, 0)]
    Eeff_o = lambda o: [((o, l, 0), (o, l, 1)) for l in (1, 0)]
    Eeff_l = lambda l: [((o, l, 0), (o, l, 1)) for o in (0, 2, 3)]
    inter = {f"OxL (L0/L1 @O{a}) / (L0/L1 @O{b})": (Leff(a), Leff(b)) for a, b in ((0, 3), (0, 2), (2, 3))}
    inter.update({f"OxE (E0/E1 @O{a}) / (E0/E1 @O{b})": (Eeff_o(a), Eeff_o(b)) for a, b in ((0, 3), (0, 2), (2, 3))})
    inter["LxE (E0/E1 @L1) / (E0/E1 @L0)"] = (Eeff_l(1), Eeff_l(0))
    model, e1c = {}, {}
    for p in ("min", "l3"):
        for f in (64, 1518):
            if not have("E1", *[K(b, p, f) for b in FACT.values()]):
                e1c[f"{p}|{f}"] = {"verdict": "no estimate"}
                continue
            for name, pairs in main.items():
                model[f"{p}|{f}|main {name}"] = boot_stat(lambda i: gm(pairs, p, f, i))
            for name, (pa, pb) in inter.items():
                model[f"{p}|{f}|interaction {name}"] = boot_stat(lambda i: gm(pa, p, f, i) / gm(pb, p, f, i))
            x = model[f"{p}|{f}|interaction OxL (L0/L1 @O0) / (L0/L1 @O3)"]
            e1c[f"{p}|{f}"] = {**x, "verdict": v_above(x["ci95"], 1.0)}
    out["model"] = model
    V["E1.c"] = {"claim": "relative cost of logging macros larger at -O0 than at -O3: [C(O0,L0)/C(O0,L1)] / [C(O3,L0)/C(O3,L1)] > 1",
                 "parts": e1c, "verdict": combine(p["verdict"] for p in e1c.values()), "alternative_readings": {}}
    pf = [(p, f) for p in ("min", "l3") for f in (64, 1518) if "ci95" in e1c.get(f"{p}|{f}", {})]
    if pf:
        ror = lambda p, f, i: gm(Leff(0), p, f, i) / gm(Leff(3), p, f, i)
        pooled = boot_stat(lambda i: math.exp(sum(math.log(ror(p, f, i)) for p, f in pf) / len(pf)))
        V["E1.c"]["alternative_readings"]["pooled ratio of ratios (geometric mean of the parts; NOT the registered combination)"] = {
            **pooled, "parts": [f"{p}|{f}" for p, f in pf], "verdict": v_above(pooled["ci95"], 1.0)}
        vs = [p["verdict"] for p in e1c.values()]
        three = "holds" if all(v == "holds" for v in vs) else "fails" if all(v == "fails" for v in vs) else \
            ("inconclusive (mixed parts: " + ", ".join(f"{k} {p['verdict']}" for k, p in e1c.items()) + ")")
        V["E1.c"]["alternative_readings"]["A1.4 three-way over the parts (holds only if all hold, fails only if all fail; NOT the registered combination)"] = {
            "verdict": three}
    # E1.d recipe equivalences (l3)
    docker = sorted({k[0] for k in cells if k[0].startswith("docker:")})
    eqs = [("cm-default", DEFAULT_LIKE), ("cm-default-dbg", "cm-default"), ("at-docs-perf", PERF),
           ("at-default", "cm-o2-L1-E1")]
    parts = {}
    for a, b in eqs:
        sub = {}
        for f in (64, 1518):
            r = ratio(K(a, "l3", f), K(b, "l3", f), "E1")
            sub[str(f)] = {**(r or {}), "verdict": v_within(r and r["ci95"], 0.9, 1.1)}
        parts[f"{a} ≈ {b}"] = {"frames": sub, "verdict": combine(s["verdict"] for s in sub.values())}
    for d in docker:
        parts[f"{d} vs cm-default-dbg (no claim)"] = {"frames": {str(f): ratio(K(d, "l3", f), K("cm-default-dbg", "l3", f), "E1")
                                                                for f in (64, 1518)}, "verdict": "estimation only"}
    V["E1.d"] = {"claim": "recipe equivalences, each ratio in [0.9, 1.1]", "parts": parts,
                 "verdict": combine(p["verdict"] for k, p in parts.items() if "no claim" not in k)}
    # §5 estimation (E1 data)
    est = {}
    for name, a, b in [("debugger: cm-default-dbg / cm-default", "cm-default-dbg", "cm-default"),
                       ("-march=native: cm-o3native-L0-E0 / cm-o3-L0-E0", "cm-o3native-L0-E0", PERF),
                       ("NDEBUG: cm-release-L0-E0 / cm-o3-L0-E0", "cm-release-L0-E0", PERF)] + \
                      [(f"Docker: {d} / {b2}", d, b2) for d in docker for b2 in ("cm-default-dbg", "cm-default")]:
        for f in (64, 1518):
            est[f"{name} | l3 | {f}"] = ratio(K(a, "l3", f), K(b, "l3", f), "E1")
    for p in ("min", "l3"):
        for f in (64, 1518):
            if f"{p}|{f}|main E0/E1" in model:
                est[f"elogger (main effect E0/E1) | {p} | {f}"] = model[f"{p}|{f}|main E0/E1"]
    out["section5_estimation"] = est
    out["section5_per_thread_cpu"] = per_thread(ex, key)
    return out, ex, cells


def per_thread(ex, keyfn):
    """§5: CPU of each switch thread (CPU-seconds per second of the snapshot window), ranked."""
    by = defaultdict(list)
    for r, fl in ex.counted(("sat",)):
        if ex.excluded(fl) or not r.get("cpu_switch_threads") or not r.get("g4_window_s"):
            continue
        u = sorted((j / 100.0 / r["g4_window_s"] for j in r["cpu_switch_threads"].values()), reverse=True)
        by[keyfn(r)].append(u)
    out = {}
    for k, us in sorted(by.items(), key=lambda kv: kstr(kv[0])):
        ranks = max(len(u) for u in us)
        out[kstr(k)] = {"n": len(us), "threads": ranks,
                        "median_cpu_by_rank": [med([u[i] if i < len(u) else 0.0 for u in us]) for i in range(min(ranks, 6))],
                        "median_total_cpu": med([sum(u) for u in us]),
                        "median_threads_above_0.05_cpu": med([sum(1 for x in u if x > 0.05) for u in us])}
    return out


def e1_md(o, cells):
    lab = lambda k: [k[0], k[1], k[3]]
    V = o["verdicts"]
    s = ["# E1 — build knobs", "", gate_md(o["gates"]), "", io_md(cells, PRE), "", "## Cells (runtime none, 1 flow)", "",
         md_table(["build", "program", "frame"] + CELL_HDR, cell_rows(cells, lab)), "", "## Registered decision rules", ""]
    rows = [["E1.a", V["E1.a"]["claim"], f4(V["E1.a"].get("ratio")), fci(V["E1.a"].get("ci95")), V["E1.a"]["verdict"]]]
    for name in ("E1.b", "E1.c"):
        for k, p in V[name]["parts"].items():
            rows.append([name, k, f4(p.get("ratio", p.get("value"))), fci(p.get("ci95")), p["verdict"]])
        rows.append([name, "**combined (registered: every part must hold)**", "", "", f"**{V[name]['verdict']}**"])
        for k, a in V[name].get("alternative_readings", {}).items():
            rows.append([name, f"alternative reading: {k}", f4(a.get("value")), fci(a.get("ci95")), a["verdict"]])
    for k, p in V["E1.d"]["parts"].items():
        for f, x in p["frames"].items():
            rows.append(["E1.d", f"{k} @ {f} B", f4(x and x.get("ratio")), fci(x and x.get("ci95")),
                         x.get("verdict", "estimation only") if x else "no estimate"])
    rows.append(["E1.d", "**combined**", "", "", f"**{V['E1.d']['verdict']}**"])
    s += [md_table(["rule", "comparison", "ratio", "95% CI", "verdict"], rows), "",
          "## Factorial model (A1.4; estimation)", "",
          md_table(["program|frame|effect", "ratio", "95% CI"],
                   [[k, f4(v["value"]), fci(v["ci95"])] for k, v in o["model"].items()]), "",
          "## §5 estimation only", "",
          md_table(["quantity", "ratio", "95% CI"],
                   [[k, f4(v and v.get("ratio", v.get("value"))), fci(v and v.get("ci95"))] for k, v in o["section5_estimation"].items()]),
          "", "Per-thread CPU split (§5, estimation): median CPU of the busiest switch threads, l3 64 B cells:", "",
          md_table(["cell", "n", "top-thread CPU by rank", "total"],
                   [[k, v["n"], ", ".join(f"{x:.3f}" for x in v["median_cpu_by_rank"]), f"{v['median_total_cpu']:.3f}"]
                    for k, v in o["section5_per_thread_cpu"].items() if k.split("|")[1] == "l3" and k.split("|")[3] == "64"])]
    return "\n".join(s) + "\n"


# ----------------------------------------------------------------------------------- E1-RT
def e1rt(raw, pre):
    allrt = load(os.path.join(raw, "e1rt.jsonl"))
    recs = [r for r in allrt if not str(r.get("build", "")).startswith("docker-grpc:")]
    ignored = Counter(r.get("record") for r in allrt if str(r.get("build", "")).startswith("docker-grpc:"))
    grpc = load(os.path.join(raw, "e1rt-grpc.jsonl"))
    recs += grpc
    runs = {}
    for name, rs in (("e1rt.jsonl", allrt), ("e1rt-grpc.jsonl", grpc)):
        st = [r for r in rs if r.get("record") == "run-start"]
        en = [r for r in rs if r.get("record") == "run-end"]
        runs[name] = {"started_utc": utc(st[0].get("started")) if st else None,
                      "finished_utc": utc(en[-1].get("finished")) if en else None,
                      "builds": sorted({r["build"] for r in rs if r.get("record") == "trial"})}
    ex = Exp("E1-RT", recs, pre)
    key = lambda r: (r["build"], r["program"], r.get("runtime", "none"), r["frame"], r.get("flows", 1)) if r["kind"] == "sat" else None
    cells = cap_cells(ex, ("sat",), key, lambda k: k[3])
    out = {"experiment": "E1-RT", "gates": ex.gate_summary(), "cells": jkeys(cells), "verdicts": {}, "exploratory": {},
           "amendment5_ignored_from_e1rt": sum(ignored.values()), "amendment5_ignored_by_record": dict(ignored),
           "runs": runs}
    num, den = (DEFAULT_LIKE, "l3", "tutorial", 64, 1), (PERF, "l3", "none", 64, 1)
    r = ratio(num, den, "E1-RT", "E1")
    out["verdicts"]["E1-RT.a"] = {"claim": "C(cm-o0-L1-E1, tutorial) / C(cm-o3-L0-E0, none) < 0.1 (l3, 64 B; denominator from E1)",
                                  **(r or {}), "verdict": v_below(r and r["ci95"], 0.1)}
    for k in cells:
        if k[2] != "none" and (ex.name, (k[0], k[1], "none", k[3], k[4])) in Exp.BOOTS:
            out["exploratory"][f"{k[0]} {k[2]} / {k[0]} none"] = ratio(k, (k[0], k[1], "none", k[3], k[4]), "E1-RT")
        if k[0].startswith("docker-grpc:") and k[2] == "tutorial":
            out["exploratory"][f"{k[0]} tutorial / E1 {PERF} none"] = ratio(k, den, "E1-RT", "E1")
    # The paper's headline factor: the documentation's own configuration (E1) over the tutorial setup.
    out["exploratory"][f"E1 at-docs-perf none / {DEFAULT_LIKE} tutorial (headline factor)"] = \
        ratio(("at-docs-perf", "l3", "none", 64, 1), num, "E1", "E1-RT")
    return out, ex, cells


def e1rt_md(o, cells):
    v = o["verdicts"]["E1-RT.a"]
    s = ["# E1-RT — runtime recording options", "", gate_md(o["gates"]), "", io_md(cells, PRE), "",
         f"Error records: {len(o['gates']['error_records'])} among the records analysed; "
         f"{o['amendment5_ignored_by_record'].get('error', 0)} error record(s) (and "
         f"{o['amendment5_ignored_from_e1rt'] - o['amendment5_ignored_by_record'].get('error', 0)} other record(s)) of build "
         "docker-grpc:* in e1rt.jsonl are ignored per AMENDMENT-5 — no trial of those instances ran there; results for that "
         "build come from e1rt-grpc.jsonl only.", "",
         "Run timing: " + "; ".join(f"{k}: {v['started_utc']} → {v['finished_utc']} UTC, builds {', '.join(v['builds'])}"
                                    for k, v in o["runs"].items())
         + ". The docker-grpc cells come from that later, separate run: they were not interleaved with the other builds' "
         "instances, so drift between the runs is not randomised against that build.", "",
         "## Cells (l3, 64 B)", "", md_table(["build", "runtime"] + CELL_HDR, cell_rows(cells, lambda k: [k[0], k[2]])), "",
         "## Registered decision rule", "",
         md_table(["rule", "claim", "ratio", "95% CI", "verdict"],
                  [["E1-RT.a", v["claim"], f4(v.get("ratio")), fci(v.get("ci95")), v["verdict"]]]), "",
         "## Exploratory (not registered): ratios to the same build's `none`", "",
         md_table(["ratio", "point", "95% CI"], [[k, f4(x and x["ratio"]), fci(x and x["ci95"])] for k, x in o["exploratory"].items()])]
    return "\n".join(s) + "\n"


# -------------------------------------------------------------------------------------- E2
def e2(raw, pre):
    ex = Exp("E2", load(os.path.join(raw, "e2.jsonl")), pre)
    key = lambda r: (r["build"], r["program"], r.get("runtime", "none"), r["frame"], r.get("flows", 1)) if r["kind"] == "sat" else None
    cells = cap_cells(ex, ("sat",), key, lambda k: k[3])
    K = lambda b, p: (b, p, "none", 64, 1)
    out = {"experiment": "E2", "gates": ex.gate_summary(), "cells": jkeys(cells)}
    oh, rr, div = {}, {}, {}
    for p in E2_PROGS[1:]:
        for b in (DEFAULT_LIKE, PERF):
            if have("E2", K(b, "min"), K(b, p)):
                oh[f"{b}|{p}"] = boot_stat(lambda i: draw("E2", K(b, "min"), i) / draw("E2", K(b, p), i) - 1)
        if not have("E2", *[K(b, q) for b in (DEFAULT_LIKE, PERF) for q in ("min", p)]):
            rr[p] = {"verdict": "no estimate"}
            div[p] = {"verdict": "no estimate"}
            continue
        rel = lambda b, i: draw("E2", K(b, p), i) / draw("E2", K(b, "min"), i)
        x = boot_stat(lambda i: rel(DEFAULT_LIKE, i) / rel(PERF, i))
        lo, hi = x["ci95"]
        # A1.4: decided on the interval -- outside [0.9, 1.11] entirely, inside entirely, or neither
        rr[p] = {**x, "verdict": "build-dependent" if (hi < 0.9 or lo > 1.11) else
                 "not build-dependent" if (0.9 <= lo and hi <= 1.11) else "inconclusive",
                 "binary_reading_of_first_report": "build-dependent" if (hi < 0.9 or lo > 1.11) else "not build-dependent"}
        d = boot_stat(lambda i: (draw("E2", K(DEFAULT_LIKE, "min"), i) / draw("E2", K(DEFAULT_LIKE, p), i))
                      - (draw("E2", K(PERF, "min"), i) / draw("E2", K(PERF, p), i)))
        lo, hi = d["ci95"]
        # A1.4: |difference| > 10 points decided on the interval (which then also excludes 0)
        div[p] = {"OH_O0_minus_OH_O3": d["value"], "ci95": d["ci95"],
                  "verdict": "claim-level divergence" if (lo > 0.10 or hi < -0.10) else
                  "no claim-level divergence" if (-0.10 <= lo and hi <= 0.10) else "inconclusive",
                  "point_reading_of_first_report": "claim-level divergence" if (abs(d["value"]) > 0.10 and (lo > 0 or hi < 0))
                  else "no claim-level divergence"}
    flips, pairs = [], 0
    for i, p in enumerate(E2_PROGS):
        for q in E2_PROGS[i + 1:]:
            if not have("E2", *[K(b, x) for b in (DEFAULT_LIKE, PERF) for x in (p, q)]):
                continue
            pairs += 1
            r0, r3 = ratio(K(DEFAULT_LIKE, p), K(DEFAULT_LIKE, q), "E2"), ratio(K(PERF, p), K(PERF, q), "E2")
            up = lambda r: r["ci95"][0] > 1
            dn = lambda r: r["ci95"][1] < 1
            if (up(r0) and dn(r3)) or (dn(r0) and up(r3)):
                flips.append({"pair": f"{p} vs {q}", "O0_ratio": r0, "O3_ratio": r3})
    slopes, slopes_min, shape = {}, {}, {}
    for kind, Ks in E2_KINDS.items():
        for b in (DEFAULT_LIKE, PERF):
            keys = [K(b, f"{kind}_{k}") for k in Ks]
            if not have("E2", *keys):
                continue
            fit = lambda i: ols(list(Ks), [1e6 / draw("E2", k, i) for k in keys])[1]
            slopes[f"{b}|{kind}"] = {"unit": "µs per table", **boot_stat(fit)}
            if have("E2", K(b, "min")):
                kk = [K(b, "min")] + keys
                fit0 = lambda i: ols([0] + list(Ks), [1e6 / draw("E2", k, i) for k in kk])[1]
                slopes_min[f"{b}|{kind}"] = {"unit": "µs per table", **boot_stat(fit0)}
                us = {0: 1e6 / POINT[("E2", K(b, "min"))], **{k: 1e6 / POINT[("E2", key)] for k, key in zip(Ks, keys)}}
                shape[f"{b}|{kind}"] = {"us_per_packet_by_K": us,
                                        "per_table_K0_to_K4": (us[4] - us[0]) / 4,
                                        "per_table_K4_to_K8": (us[8] - us[4]) / 4}
    out["OH"] = oh
    out["verdicts"] = {"build-dependence (RR, A1.4)": rr, "claim-level divergence": div,
                       "ranking flips": {"pairs_checked": pairs, "flips": flips,
                                         "verdict": "ranking flip" if flips else "no ranking flip"}}
    out["per_table_time"] = slopes
    out["per_table_time_with_min_as_K0"] = slopes_min
    out["per_table_time_shape_descriptive"] = shape
    out["first_report_verdicts_changed"] = {f"{name}|{p}": [x[old], x["verdict"]]
                                            for name, d_, old in ((("RR", rr, "binary_reading_of_first_report")),
                                                                  ("divergence", div, "point_reading_of_first_report"))
                                            for p, x in d_.items() if old in x and x[old] != x["verdict"]}
    return out, ex, cells


def e2_md(o, cells):
    V = o["verdicts"]
    rows = []
    for p in E2_PROGS[1:]:
        r, d = V["build-dependence (RR, A1.4)"].get(p), V["claim-level divergence"].get(p)
        o0, o3 = o["OH"].get(f"{DEFAULT_LIKE}|{p}"), o["OH"].get(f"{PERF}|{p}")
        rows.append([p, f"{f4(o0 and o0['value'])} {fci(o0 and o0['ci95'])}", f"{f4(o3 and o3['value'])} {fci(o3 and o3['ci95'])}",
                     f"{f4((r or {}).get('value'))} {fci((r or {}).get('ci95'))}", (r or {}).get("verdict"),
                     f"{f4((d or {}).get('OH_O0_minus_OH_O3'))} {fci((d or {}).get('ci95'))}", (d or {}).get("verdict")])
    fl = V["ranking flips"]
    s = ["# E2 — does the build change what a P4 program costs?", "", gate_md(o["gates"]), "", io_md(cells, PRE), "",
         "## Cells (64 B)", "", md_table(["build", "program"] + CELL_HDR, cell_rows(cells, lambda k: [k[0], k[1]])), "",
         "## Registered decision rules (A1.4)", "",
         md_table(["program", "OH_O0 [CI]", "OH_O3 [CI]", "RR [CI]", "build-dependence", "OH_O0 − OH_O3 [CI]", "divergence"], rows), "",
         f"Ranking flips: {fl['verdict']} ({len(fl['flips'])} of {fl['pairs_checked']} pairs)"
         + "".join(f"\n- {x['pair']}: O0 {f4(x['O0_ratio']['ratio'])} {fci(x['O0_ratio']['ci95'])}, O3 {f4(x['O3_ratio']['ratio'])} {fci(x['O3_ratio']['ci95'])}"
                   for x in fl["flips"]), "",
         "RR and the divergence are decided on the 95 % interval with an inconclusive outcome (A1.4). Verdicts that differ "
         "from the first report's binary/point reading: "
         + (", ".join(f"{k}: {a} → {b}" for k, (a, b) in o["first_report_verdicts_changed"].items()) or "none") + ".", "",
         "## Per-table time t_b (slope of 1/C on K)", "",
         md_table(["build|kind", "µs/table, K ≥ 1 only (registered reading)", "95% CI", "µs/table, with min as K = 0", "95% CI",
                   "descriptive: µs/table K0→K4", "K4→K8"],
                  [[k, f4(v["value"]), fci(v["ci95"]), f4(o["per_table_time_with_min_as_K0"].get(k, {}).get("value")),
                    fci(o["per_table_time_with_min_as_K0"].get(k, {}).get("ci95")),
                    f4(o["per_table_time_shape_descriptive"].get(k, {}).get("per_table_K0_to_K4")),
                    f4(o["per_table_time_shape_descriptive"].get(k, {}).get("per_table_K4_to_K8"))]
                   for k, v in o["per_table_time"].items()]), "",
         "Descriptive: where the per-table cost from K = 0 to 4 and from 4 to 8 (last two columns) differ, a single slope in K "
         "is a poor summary of the cost."]
    return "\n".join(s) + "\n"


# -------------------------------------------------------------------------------------- E3
def e3(raw, pre):
    ex = Exp("E3", load(os.path.join(raw, "e3.jsonl")), pre)
    key = lambda r: (r["build"], r["program"], r.get("runtime", "none"), r["frame"], r.get("flows", 1)) if r["kind"] == "sat" else None
    cells = cap_cells(ex, ("sat",), key, lambda k: k[3])
    out = {"experiment": "E3", "gates": ex.gate_summary(), "cells": jkeys(cells), "fit": {}, "verdicts": {}}
    by_inst = defaultdict(lambda: defaultdict(dict))
    for r, fl in ex.counted(("sat",)):
        if not ex.excluded(fl) and r.get("delivered_pps_steady"):
            by_inst[r["build"]][r["instance"]][r["frame"]] = r["delivered_pps_steady"]
    parts = {}
    for b in sorted(by_inst):
        insts = sorted(by_inst[b])

        def stats(sample):
            pts = [(f, c) for i in sample for f, c in by_inst[b][i].items()]
            a, sl = ols([p[0] for p in pts], [1e6 / p[1] for p in pts])
            c64 = [by_inst[b][i][64] for i in sample if 64 in by_inst[b][i]]
            c1518 = [by_inst[b][i][1518] for i in sample if 1518 in by_inst[b][i]]
            share = (sl * 1518 / (a + sl * 1518)) if sl is not None else None
            return a, sl, share, (med(c1518) / med(c64)) if c64 and c1518 else None
        pt = stats(insts)
        rng = random.Random(f"E3|{b}")
        draws = [stats(rng.choices(insts, k=len(insts))) for _ in range(B)]
        civ = lambda j: list(ci([d[j] for d in draws if d[j] is not None])) if any(d[j] is not None for d in draws) else None
        out["fit"][b] = {"a_us": pt[0], "a_ci95": civ(0), "b_ns_per_byte": pt[1] * 1000 if pt[1] is not None else None,
                         "b_ci95": [x * 1000 for x in civ(1)] if civ(1) else None,
                         "byte_share_1518": pt[2], "byte_share_ci95": civ(2), "instances": len(insts)}
        parts[b] = {"ratio": pt[3], "ci95": civ(3), "verdict": v_above(civ(3), 0.8, strict=False)}
    out["verdicts"]["E3.a"] = {"claim": "C(1518)/C(64) >= 0.8 for both builds", "parts": parts,
                               "verdict": combine(p["verdict"] for p in parts.values()) if len(parts) == 2 else
                               combine(list(p["verdict"] for p in parts.values()) + ["no estimate"])}
    return out, ex, cells


def e3_md(o, cells):
    V = o["verdicts"]["E3.a"]
    s = ["# E3 — frame size", "", gate_md(o["gates"]), "", io_md(cells, PRE), "", "## Cells (l3)", "",
         md_table(["build", "frame"] + CELL_HDR, cell_rows(cells, lambda k: [k[0], k[3]])), "",
         "## Fit 1/C(s) = a + b·s (per build, instance bootstrap)", "",
         md_table(["build", "a (µs)", "95% CI", "b (ns/byte)", "95% CI", "byte share at 1518", "95% CI"],
                  [[b, f4(x["a_us"]), fci(x["a_ci95"]), f4(x["b_ns_per_byte"]), fci(x["b_ci95"]), f4(x["byte_share_1518"]),
                    fci(x["byte_share_ci95"])] for b, x in o["fit"].items()]), "",
         "## Registered decision rule", "",
         md_table(["rule", "build", "C(1518)/C(64)", "95% CI", "verdict"],
                  [["E3.a", b, f4(p["ratio"]), fci(p["ci95"]), p["verdict"]] for b, p in V["parts"].items()]
                  + [["E3.a", "**combined**", "", "", f"**{V['verdict']}**"]])]
    return "\n".join(s) + "\n"


# -------------------------------------------------------------------------------------- E4
def e4(raw, pre):
    ex = Exp("E4", load(os.path.join(raw, "e4.jsonl")), pre)
    out = {"experiment": "E4", "gates": ex.gate_summary(), "zero_loss": {}, "iperf3": {}, "verdicts": {}}
    step_fl = defaultdict(list)
    for r, fl in ex.counted(("zl-step",)):
        step_fl[(r["instance"], r["frame"], r.get("burst"))].append(fl)
    zl = defaultdict(lambda: {"values": [], "instances": [], "failed_bracket": [], "excluded": [], "lo_pps": [],
                              "hi_pps": [], "step_flags": Counter(), "sched_searches": 0, "searches": 0})
    for r in ex.trials:
        if r.get("kind") != "zl-result" or r.get("superseded"):
            continue
        k = (r["build"], r["frame"], r["burst"])
        c = zl[k]
        sf = step_fl.get((r["instance"], r["frame"], r["burst"]), [])
        c["searches"] += 1
        c["sched_searches"] += any("schedule" in fl for fl in sf)
        for fl in sf:
            for x in sorted({x.split(":")[0] if x.startswith("G8") else x for x in fl}):
                c["step_flags"][x] += 1
        inst_bad = ex.inst_ok.get(r["instance"]) is False or ex.inst_end.get(r["instance"]) is False
        if inst_bad or any(ex.excluded(fl) for fl in sf):
            c["excluded"].append(r["instance"])
            continue
        for role in ("lo", "hi"):
            st = [s for s in r.get("steps", []) if s.get("role") == role]
            if st:
                c[role + "_pps"].append(st[0]["pps"])
        if r.get("zero_loss_pps") is None:
            c["failed_bracket"].append({"instance": r["instance"], "bracket": r.get("bracket")})
        else:
            c["values"].append(r["zero_loss_pps"])
            c["instances"].append(r["instance"])
    for k in sorted(zl):
        c = zl[k]
        s = {"n_valid": len(c["values"]), "n_failed_bracket": len(c["failed_bracket"]), "n_excluded": len(c["excluded"]),
             "median": med(c["values"]), "ci95": None, "values": c["values"], "failed_bracket": c["failed_bracket"],
             "step_gate_failures": dict(c["step_flags"]), "searches": c["searches"],
             "searches_with_a_schedule_flagged_step": c["sched_searches"],
             "failed_bracket_lo_fails": sum(1 for x in c["failed_bracket"] if not (x["bracket"] or {}).get("lo_passes", True)),
             "failed_bracket_hi_passes": sum(1 for x in c["failed_bracket"] if not (x["bracket"] or {}).get("hi_fails", True))}
        if c["values"]:
            bs = boot_median(c["values"], random.Random(f"E4|{kstr(k)}"))
            Exp.BOOTS[("E4", ("Z",) + k)] = bs
            POINT[("E4", ("Z",) + k)] = s["median"]
            s["ci95"] = list(ci(bs))
        Ck = (k[0], "l3", "none", k[1], 1)
        if ("E1", Ck) in POINT:
            s["C_E1"] = POINT[("E1", Ck)]
            s["Z_over_C"] = ratio(("Z",) + k, Ck, "E4", "E1") if c["values"] else None
            for role in ("lo", "hi"):
                if c[role + "_pps"]:
                    s[role + "_over_C"] = med(c[role + "_pps"]) / POINT[("E1", Ck)]
        out["zero_loss"][kstr(k)] = s
    V = out["verdicts"]
    for name, burst, thr, fn, claim in (("E4.a", 1, 0.85, lambda iv: v_above(iv, 0.85, strict=False), "Z_1 >= 0.85 C"),
                                        ("E4.b", 128, 0.7, lambda iv: v_below(iv, 0.7), "Z_128 < 0.7 C")):
        parts = {}
        for b in (DEFAULT_LIKE, PERF):
            for f in (64, 1518):
                s = out["zero_loss"].get(kstr((b, f, burst)))
                if s is None:
                    continue
                zc = s.get("Z_over_C")
                sched = {"schedule_flagged_steps": s["step_gate_failures"].get("schedule", 0),
                         "searches_with_a_schedule_flagged_step": f"{s['searches_with_a_schedule_flagged_step']}/{s['searches']}"}
                if zc:
                    parts[f"{b}|{f}"] = {**zc, "verdict": fn(zc["ci95"]), "n_valid": s["n_valid"], "n_failed_bracket": s["n_failed_bracket"], **sched}
                else:
                    nall = s["n_failed_bracket"] + s["n_valid"] + s["n_excluded"]
                    # what a failed bracket bounds, if loss is monotone in rate: lo lost frames -> Z < lo;
                    # hi lost none -> Z >= hi. Only said when every search of the cell failed the same way.
                    bound, alt = None, None
                    if s["n_failed_bracket"] and not s["n_valid"]:
                        if s["failed_bracket_lo_fails"] == s["n_failed_bracket"] and s.get("lo_over_C") is not None:
                            bound = f"Z < lo = {s['lo_over_C']:.4f} C"
                            alt = fn([0.0, s["lo_over_C"]]) if name == "E4.b" else v_above([0.0, s["lo_over_C"]], thr, strict=False)
                        elif s["failed_bracket_hi_passes"] == s["n_failed_bracket"] and s.get("hi_over_C") is not None:
                            bound = f"Z >= hi = {s['hi_over_C']:.4f} C"
                    parts[f"{b}|{f}"] = {"verdict": f"no estimate (failed bracket {s['n_failed_bracket']}/{nall})"
                                         if s["n_failed_bracket"] else "no estimate",
                                         "n_valid": s["n_valid"], "n_failed_bracket": s["n_failed_bracket"],
                                         "failed_bracket_lo_fails": s["failed_bracket_lo_fails"],
                                         "failed_bracket_hi_passes": s["failed_bracket_hi_passes"],
                                         "bound_if_loss_monotone": bound, "verdict_if_loss_monotone": alt, **sched}
        V[name] = {"claim": claim, "parts": parts, "verdict": combine(p["verdict"] if p["verdict"] in ("holds", "fails", "inconclusive")
                                                                     else "no estimate" for p in parts.values())}
        alts = [p.get("verdict_if_loss_monotone") for p in parts.values() if not p["verdict"] in ("holds", "fails", "inconclusive")]
        if alts and all(alts):
            V[name]["alternative_readings"] = {
                "if loss is monotone in rate (the assumption every bisection Z relies on)": {
                    "statement": "; ".join(f"{k}: {p['bound_if_loss_monotone']}" for k, p in parts.items() if p.get("bound_if_loss_monotone"))
                                 + f" — so each such part would get '{"/".join(sorted(set(alts)))}' (inference from the bracket, not the registered verdict)",
                    "verdict": combine([p["verdict"] if p["verdict"] in ("holds", "fails", "inconclusive") else p["verdict_if_loss_monotone"]
                                        for p in parts.values()])}}
    # iperf3 arm
    rungs = defaultdict(list)
    for r, fl in ex.counted(("iperf3-rung",)):
        rungs[(r["instance"], r["frame"])].append((r, fl))
    table = defaultdict(lambda: defaultdict(list))
    best = defaultdict(list)
    for (inst, f), rs in sorted(rungs.items()):
        rs.sort(key=lambda x: x[0]["offered_pps"])
        fr = E4_LADDER if len(rs) == len(E4_LADDER) else [None] * len(rs)
        b = rs[0][0]["build"]
        clean = 0.0
        usable = True
        for x, (r, fl) in zip(fr, rs):
            if ex.excluded(fl):
                usable = False
                continue
            loss = r.get("iperf3_aggregate_loss_pct")
            t = table[(b, f, x)]
            t["iperf3_loss_pct"].append(loss)
            t["iperf3_received_bps"].append(sum(p["received_bps"] for p in r["iperf3_per_flow"]) if loss is not None else None)
            t["counter_loss"].append(1 - r["delivered"] / r["h0_tx"] if r.get("h0_tx") else None)
            t["offered_pps"].append(r["offered_pps"])
            t["delivered_pps"].append(r["delivered"] / r["duration"] if r.get("duration") and r.get("delivered") is not None else None)
            if loss is not None and loss <= 100 * CLEAN:
                clean = max(clean, r["offered_pps"])
        if usable:
            best[(b, f)].append(clean)
    for (b, f, x), t in sorted(table.items(), key=lambda kv: kstr(kv[0])):
        out["iperf3"][f"{b}|{f}|{x}"] = {k: {"median": med([v for v in vs if v is not None]), "values": vs} for k, vs in t.items()}
    parts = {}
    for (b, f), hs in sorted(best.items()):
        Ck = (b, "l3", "none", f, 1)
        bs = boot_median(hs, random.Random(f"E4|iperf3-highest|{b}|{f}"))
        Exp.BOOTS[("E4", ("H", b, f))] = bs
        POINT[("E4", ("H", b, f))] = med(hs)
        r = ratio(("H", b, f), Ck, "E4", "E1")
        parts[f"{b}|{f}"] = {"highest_clean_pps": hs, **(r or {}),
                             "verdict": v_below(r and r["ci95"], 0.8) if b == PERF else "no verdict registered"}
    V["E4.c"] = {"claim": "highest iperf3 rung with <= 0.5 % loss < 0.8 C for cm-o3-L0-E0", "parts": parts,
                 "verdict": combine(p["verdict"] for k, p in parts.items() if k.startswith(PERF + "|"))}
    # descriptive: rates the switch sustained above the sat C it was planned from
    desc = {}
    for b in (DEFAULT_LIKE, PERF):
        for f in (64, 1518):
            Ck = ("E1", (b, "l3", "none", f, 1))
            if Ck not in POINT:
                continue
            zc = {B_: (out["zero_loss"].get(kstr((b, f, B_))) or {}).get("Z_over_C") for B_ in (1, 8, 32)}
            above = [v for (bb, ff, x), t in table.items() if bb == b and ff == f and x is not None and x > 1.0
                     for v in t["delivered_pps"] if v is not None]
            desc[f"{b}|{f}"] = {"C_E1": POINT[Ck], "Z_over_C_B1_8_32": {str(k): v and v["ratio"] for k, v in zc.items()},
                                "iperf3_delivered_pps_on_rungs_above_C": [min(above), max(above)] if above else None,
                                "iperf3_delivered_over_C": [min(above) / POINT[Ck], max(above) / POINT[Ck]] if above else None}
    out["descriptive_rates_above_sat_C"] = desc
    return out, ex, None


def e4_md(o, cells=None):
    V = o["verdicts"]
    zrows = [[*k.split("|"), s["n_valid"], s["n_failed_bracket"], f0(s["median"]), fci(s["ci95"], f0),
              f4(s.get("Z_over_C") and s["Z_over_C"]["ratio"]), fci(s.get("Z_over_C") and s["Z_over_C"]["ci95"]),
              ", ".join(f"{a} {n}" for a, n in sorted(s["step_gate_failures"].items())) or "—"]
             for k, s in o["zero_loss"].items()]
    vrows = []
    for name in ("E4.a", "E4.b", "E4.c"):
        for k, p in V[name]["parts"].items():
            extra = []
            if p.get("bound_if_loss_monotone"):
                extra.append(f"if loss is monotone in rate: {p['bound_if_loss_monotone']}, so this part would get '{p["verdict_if_loss_monotone"]}'"
                             if p.get("verdict_if_loss_monotone") else f"if loss is monotone in rate: {p['bound_if_loss_monotone']}")
            if p.get("schedule_flagged_steps"):
                extra.append(f"GF: {p['schedule_flagged_steps']} counted steps still flagged `schedule` after their rerun, in "
                             f"{p['searches_with_a_schedule_flagged_step']} searches")
            vrows.append([name, k, f4(p.get("ratio")), fci(p.get("ci95")), p["verdict"] + "".join(f"; {x}" for x in extra)])
        vrows.append([name, "**combined**", "", "", f"**{V[name]['verdict']}**"])
        for k, a in V[name].get("alternative_readings", {}).items():
            vrows.append([name, f"alternative reading: {k}", "", "", f"{a['verdict']} — {a['statement']}"])
    irows = [[*k.split("|"), f0(t["offered_pps"]["median"]), f4(t["iperf3_loss_pct"]["median"]),
              f0(t["iperf3_received_bps"]["median"]), f4(t["counter_loss"]["median"]), f0(t["delivered_pps"]["median"])]
             for k, t in o["iperf3"].items()]
    drows = [[k, f0(d["C_E1"]), ", ".join(f"B{b} {f4(v)}" for b, v in d["Z_over_C_B1_8_32"].items()),
              fci(d["iperf3_delivered_pps_on_rungs_above_C"], f0), fci(d["iperf3_delivered_over_C"])]
             for k, d in o["descriptive_rates_above_sat_C"].items()]
    s = ["# E4 — what the instrument does to the number", "", gate_md(o["gates"]), "",
         "## Zero-loss throughput Z_B (threshold 0 frames)", "",
         md_table(["build", "frame", "B", "n valid", "failed bracket", "Z median", "95% CI", "Z/C", "95% CI", "step gate failures"], zrows), "",
         "## Registered decision rules", "", md_table(["rule", "cell", "ratio to C", "95% CI", "verdict"], vrows), "",
         "## iperf3 arm (medians over instances; loss in %, counter loss = 1 − delivered/h0_tx)", "",
         md_table(["build", "frame", "rung ×C", "offered pps", "iperf3 loss %", "iperf3 received bps", "counter loss",
                   "delivered pps (h1 counter / 10 s)"], irows), "",
         "## Descriptive: rates sustained above the sat C (not registered)", "",
         "Zero-loss throughput above 1.0 C, or iperf3 rungs above C that deliver more than C, mean the sat C of that cell "
         "understates the rate the switch sustains over a 10 s trial (loss is counted after the drain, so a backlog that "
         "drains after the trial counts as delivered).", "",
         md_table(["build|frame", "sat C (E1)", "Z_B / C", "iperf3 delivered pps, rungs > C (min, max)", "÷ C"], drows)]
    return "\n".join(s) + "\n"


# -------------------------------------------------------------------------------------- E5
def worst_flow(r):
    acc = r["gen"]["per_flow_accepted"]
    rx = {f["flow"]: f["rx"] for f in r["recv"]["flows"]}
    return acc, [rx.get(i, 0) for i in range(len(acc))]


def e5c_trial(r, rng):
    """One rate trial against the binomial max-of-n null (A1.3.1). The statistic is the worst flow's loss
    fraction; the comparison is tie-aware in frames: bmgen's round-robin leaves flows whose accepted counts
    differ by 1, so a worst flow that lost exactly the frame count of a null quantile can have a slightly
    larger FRACTION than that quantile (same k, smaller a) -- such an observation counts as inside."""
    acc, rx = worst_flow(r)
    L = r["loss"]
    lost = [a - x for a, x in zip(acc, rx)]
    # (a - x) / a, never 1 - x / a: the null's values are k / a, and the two spellings of the same
    # fraction can differ in the last bit, which would put an observation on the null's edge outside it
    obs, k_obs = max((k / a, k) for a, k in zip(acc, lost) if a)
    sims = []
    for _ in range(E5C_SIMS):
        best = (-1.0, 0)
        for a in acc:
            if a:
                k = rng.binomialvariate(a, L)
                if k / a > best[0]:
                    best = (k / a, k)
        sims.append(best)
    sims.sort()
    lo, hi = sims[int(0.025 * E5C_SIMS)], sims[int(0.975 * E5C_SIMS) - 1]
    inside = lambda f, k: lo[0] <= f <= hi[0] or k in (lo[1], hi[1])
    within = inside(obs, k_obs)
    return {"instance": r["instance"], "aggregate_loss": L, "worst_flow_loss": obs, "worst_flow_lost_frames": k_obs,
            "null_central95": [lo[0], hi[0]], "null_central95_frames": [lo[1], hi[1]],
            "within": within, "within_by_fraction_only": lo[0] <= obs <= hi[0],
            "side": "within" if within else ("above" if obs > hi[0] else "below"),
            "null_p_outside": sum(1 for f, k in sims if not inside(f, k)) / E5C_SIMS}


def e5(raw, pre):
    ex = Exp("E5", load(os.path.join(raw, "e5.jsonl")), pre)
    key = lambda r: (r["build"], r["program"], r.get("flows", 1)) if r["kind"] == "sat" else None
    cells = cap_cells(ex, ("sat",), key, lambda k: 64)
    out = {"experiment": "E5", "gates": ex.gate_summary(), "cells": jkeys(cells), "verdicts": {}}
    V = out["verdicts"]
    for name, progs, fn, claim in (("E5.a", ("l3", "flow_exact_256"), lambda iv: v_within(iv, 0.9, 1.1), "C(256)/C(1) in [0.9, 1.1]"),
                                   ("E5.b", ("flow_ternary_256",), lambda iv: v_below(iv, 0.8), "C(256)/C(1) < 0.8")):
        parts = {}
        for b in (DEFAULT_LIKE, PERF):
            for p in progs:
                r = ratio((b, p, 256), (b, p, 1), "E5")
                parts[f"{b}|{p}"] = {**(r or {}), "verdict": fn(r and r["ci95"])}
        V[name] = {"claim": claim, "parts": parts}
    # E5.c (A1.3.1)
    rate = defaultdict(list)
    for r, fl in ex.counted(("rate",)):
        if ex.excluded(fl) or any(x.startswith("G3") for x in fl):
            continue
        rate[(r["build"], r["program"], r["flows"])].append((r, fl))
    e5c, v1 = {}, {}
    for k in sorted(rate):
        res = []
        for r, fl in rate[k]:
            if r.get("loss") and r["loss"] > 0:
                x = e5c_trial(r, random.Random(f"E5c|{r['instance']}|{r.get('pps_frac_of_sat')}|{r['burst']}"))
                x.update({"rung": r.get("pps_frac_of_sat"), "burst": r["burst"], "schedule_flag": "schedule" in fl})
                res.append(x)
        outside = [x for x in res if not x["within"]]
        if k[2] == 1:
            verdict = "cannot fail (n = 1: the worst flow is the aggregate)"
        elif not res:
            verdict = "no estimate (no lossy trial: not tested)"
        else:
            verdict = "holds" if not outside else "fails"
        e5c[kstr(k)] = {"lossy_trials": len(res), "outside": len(outside), "trials": res, "verdict": verdict,
                        "lossy_trials_with_schedule_flag": sum(1 for x in res if x["schedule_flag"]),
                        "trials_with_schedule_flag": sum(1 for _, fl in rate[k] if "schedule" in fl)}
        # v1 criterion next to the aggregate criterion, per instance x burst
        per = defaultdict(lambda: {"perflow": (0.0, 0.0), "aggregate": (0.0, 0.0)})
        for r, fl in rate[k]:
            acc, rx = worst_flow(r)
            worst = max((a - x) / a for a, x in zip(acc, rx) if a)
            d = per[(r["instance"], r["burst"])]
            fr = r.get("pps_frac_of_sat")
            if worst <= CLEAN and fr > d["perflow"][0]:
                d["perflow"] = (fr, r["offered_pps"])
            if r["loss"] <= CLEAN and fr > d["aggregate"][0]:
                d["aggregate"] = (fr, r["offered_pps"])
        for burst in sorted({bb for _, bb in per}):
            rows = [v for (i, bb), v in per.items() if bb == burst]
            v1[f"{kstr(k)}|B{burst}"] = {
                crit: {"median_frac": med([x[crit][0] for x in rows]), "median_pps": med([x[crit][1] for x in rows]),
                       "fracs": [x[crit][0] for x in rows]} for crit in ("perflow", "aggregate")}
    tested = {b: {k: v for k, v in e5c.items() if k.startswith(b + "|") and v["verdict"] in ("holds", "fails")}
              for b in (DEFAULT_LIKE, PERF)}
    builds, desc = {}, {}
    for b, cs in tested.items():
        builds[b] = ("fails" if any(v["verdict"] == "fails" for v in cs.values()) else "holds") if cs else \
            "no estimate (no cell with n > 1 had a lossy trial: not tested)"
        ts = [t for v in cs.values() for t in v["trials"]]
        split = Counter(f"B{t['burst']} {t['side']}" for t in ts if not t["within"])
        p_any = 1 - math.prod(1 - t["null_p_outside"] for t in ts) if ts else None
        desc[b] = {"tested_trials": len(ts), "outside": sum(1 for t in ts if not t["within"]),
                   "outside_by_burst_and_side": dict(sorted(split.items())),
                   "tie_rescued_trials (outside by fraction only, inside in frames)": sum(1 for t in ts if t["within"] and not t["within_by_fraction_only"]),
                   "P(at least one outside | null), per-trial rule, simulated discreteness": p_any,
                   "P(at least one outside | null), nominal 5 % per trial": (1 - 0.95 ** len(ts)) if ts else None,
                   "lossy_trials_with_schedule_flag": sum(1 for t in ts if t["schedule_flag"])}
    V["E5.c"] = {"claim": "observed worst-flow loss within the central 95 % of the binomial max-of-n null at every rung with aggregate loss > 0",
                 "cells": {k: {kk: vv for kk, vv in v.items() if kk != "trials"} for k, v in e5c.items()},
                 "builds": builds, "description_not_a_verdict": desc}
    out["e5c_trials"] = {k: v["trials"] for k, v in e5c.items()}
    out["v1_criterion"] = v1
    # is there a per-flow decline at all? (the registered reading speaks of one)
    decline = {}
    for b in (DEFAULT_LIKE, PERF):
        rows = {}
        for kk, v in v1.items():
            bb, prog, n, burst = kk.split("|")
            if bb == b:
                rows.setdefault((prog, burst), {})[int(n)] = v["perflow"]["median_frac"]
        decline[b] = {"per_flow_rung_by_n": {f"{p}|{bu}": d for (p, bu), d in sorted(rows.items())},
                      "decline_observed": any(d.get(1) is not None and any(d.get(n) is not None and d[n] < d[1] for n in (16, 256))
                                              for d in rows.values())}
    out["per_flow_decline"] = decline
    readings = {}
    for b in (DEFAULT_LIKE, PERF):
        a_l3 = V["E5.a"]["parts"].get(f"{b}|l3", {}).get("verdict")
        a_fe = V["E5.a"]["parts"].get(f"{b}|flow_exact_256", {}).get("verdict")
        c = V["E5.c"]["builds"].get(b)
        rd = []
        if a_l3 == "fails":
            rd.append("the decline is a bmv2 property (E5.a fails for l3); mechanism left open")
        if (a_l3 == "holds") != (a_fe == "holds") or "inconclusive" in (a_l3, a_fe):
            rd.append("E5.a holds for one program and not the other, or is inconclusive: reported per program, no combined verdict")
        if a_l3 == "holds" and a_fe == "holds":
            if c == "holds":
                rd.append("a per-flow decline is a property of the criterion and the instrument, not of bmv2"
                          + ("" if decline[b]["decline_observed"] else " (no per-flow decline is observed in this build)"))
            elif str(c).startswith("no estimate"):
                rd.append("no registered reading: E5.c not tested")
            else:
                rd.append(f"no registered reading covers E5.a(l3)={a_l3}, E5.a(flow_exact_256)={a_fe}, E5.c={c}")
        if not rd:
            rd.append(f"no registered reading covers E5.a(l3)={a_l3}, E5.a(flow_exact_256)={a_fe}, E5.c={c}")
        readings[b] = rd
    V["E5 reading (A1.4)"] = readings
    # E5.d: iperf3 multi-flow arm (no decision rule registered)
    rungs = defaultdict(list)
    for r, fl in ex.counted(("iperf3-rung",)):
        rungs[r["instance"]].append((r, fl))
    tab = defaultdict(lambda: defaultdict(list))
    hi = defaultdict(list)
    for inst, rs in sorted(rungs.items()):
        rs.sort(key=lambda x: x[0]["offered_pps"])
        fr = E5D_LADDER if len(rs) == len(E5D_LADDER) else [None] * len(rs)
        n = rs[0][0]["flows"]
        best = 0.0
        for x, (r, fl) in zip(fr, rs):
            t = tab[(r["build"], n, x)]
            t["per_flow_offered_pps"].append(r["offered_pps"])
            t["iperf3_worst_flow_loss_pct"].append(r.get("iperf3_worst_flow_loss_pct"))
            t["iperf3_aggregate_loss_pct"].append(r.get("iperf3_aggregate_loss_pct"))
            t["counter_loss"].append(1 - r["delivered"] / r["h0_tx"] if r.get("h0_tx") else None)
            w = r.get("iperf3_worst_flow_loss_pct")
            if not ex.excluded(fl) and w is not None and w <= 100 * CLEAN and x is not None:
                best = max(best, x)
        hi[(rs[0][0]["build"], n)].append(best)
    out["E5.d"] = {"note": "no decision rule registered; per-flow 0.5 % criterion applied descriptively",
                   "ladder_note": "the ladder that ran (make_plans.py e5(): 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 1.0, 1.2 x C, "
                                  "split over n flows) differs from the registered 'same per-flow ladder as E4' (0.1 ... 1.5 x C)",
                   "rungs": {f"{b}|n{n}|{x}": {k: {"median": med([v for v in vs if v is not None]), "values": vs} for k, vs in t.items()}
                             for (b, n, x), t in sorted(tab.items(), key=lambda kv: kstr(kv[0]))},
                   "highest_clean_rung_fraction_of_C": {f"{b}|n{n}": {"median": med(v), "values": v} for (b, n), v in sorted(hi.items())}}
    return out, ex, cells


def e5_md(o, cells):
    V = o["verdicts"]
    vrows = []
    for name in ("E5.a", "E5.b"):
        for k, p in V[name]["parts"].items():
            vrows.append([name, k, f4(p.get("ratio")), fci(p.get("ci95")), p["verdict"]])
    crow = [[k, v["lossy_trials"], v["outside"], v["verdict"],
             f"{v['trials_with_schedule_flag']} ({v['lossy_trials_with_schedule_flag']} lossy)"] for k, v in V["E5.c"]["cells"].items()]
    dsc = V["E5.c"]["description_not_a_verdict"]
    drows = []
    for b, x in dsc.items():
        drows.append([b, x["tested_trials"], x["outside"], ", ".join(f"{k} {v}" for k, v in x["outside_by_burst_and_side"].items()) or "—",
                      x["tie_rescued_trials (outside by fraction only, inside in frames)"],
                      f4(x["P(at least one outside | null), per-trial rule, simulated discreteness"]),
                      f4(x["P(at least one outside | null), nominal 5 % per trial"]), x["lossy_trials_with_schedule_flag"]])
    v1rows = [[k, f4(v["perflow"]["median_frac"]), f0(v["perflow"]["median_pps"]), f4(v["aggregate"]["median_frac"]),
               f0(v["aggregate"]["median_pps"])] for k, v in o["v1_criterion"].items()]
    d = o["E5.d"]
    irows = [[k, f0(t["per_flow_offered_pps"]["median"]), f4(t["iperf3_worst_flow_loss_pct"]["median"]),
              f4(t["iperf3_aggregate_loss_pct"]["median"]), f4(t["counter_loss"]["median"])] for k, t in d["rungs"].items()]
    s = ["# E5 — flow count", "", gate_md(o["gates"]), "", io_md(cells, PRE), "", "## Capacity cells (sat, receiver on, 64 B)", "",
         md_table(["build", "program", "flows"] + CELL_HDR, cell_rows(cells, lambda k: list(k))), "",
         "## Registered decision rules", "", md_table(["rule", "cell", "C(256)/C(1)", "95% CI", "verdict"], vrows), "",
         "### E5.c (A1.3.1): worst-flow loss vs binomial max-of-n null (tie-aware in frames)", "",
         md_table(["build|program|flows", "trials with loss > 0", "outside central 95 %", "E5.c",
                   "trials still flagged `schedule` (GF) after rerun"], crow), "",
         "E5.c per build (pooled over cells with n > 1 that had a lossy trial): "
         + "; ".join(f"**{b}** {v}" for b, v in V["E5.c"]["builds"].items()), "",
         "Description, not a verdict: outside trials by burst and side, and how likely the per-trial rule is to fail "
         "somewhere under the null itself (1 − Π(1 − p_i), p_i = the null's own outside rate for trial i).", "",
         md_table(["build", "tested trials", "outside", "outside by burst / side", "inside only by the frame-count tie",
                   "P(≥ 1 outside | null)", "nominal 1 − 0.95^N", "tested trials flagged `schedule`"], drows), "",
         "E5 reading (A1.4): " + "; ".join(f"**{b}**: " + " / ".join(r) for b, r in V["E5 reading (A1.4)"].items()), "",
         "Per-flow decline (median per-flow clean rung at n = 1 / 16 / 256): "
         + "; ".join(f"**{b}** decline observed: {x['decline_observed']}" for b, x in o["per_flow_decline"].items()), "",
         "### v1 criterion (largest rung, worst flow ≤ 0.5 %) next to the aggregate criterion (aggregate loss ≤ 0.5 %)", "",
         "Medians over instances; rung as fraction of the instance's own C; 0 = no rung qualifies.", "",
         md_table(["build|program|flows|burst", "per-flow: rung", "per-flow: pps", "aggregate: rung", "aggregate: pps"], v1rows), "",
         "## E5.d iperf3 multi-flow arm (no decision rule registered)", "",
         f"Deviation: {d['ladder_note']}.", "",
         md_table(["build|flows|rung ×C", "per-flow offered pps", "worst-flow loss %", "aggregate loss %", "counter loss"], irows), "",
         "Highest rung with worst flow ≤ 0.5 % (descriptive): "
         + "; ".join(f"{k}: median {f4(v['median'])} {v['values']}" for k, v in d["highest_clean_rung_fraction_of_C"].items())]
    return "\n".join(s) + "\n"


# -------------------------------------------------------------------------------------- main
def dump(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, default=lambda x: list(x) if isinstance(x, tuple) else str(x))
        f.write("\n")


EXPS = [("e1", e1, e1_md), ("e1rt", e1rt, e1rt_md), ("e2", e2, e2_md), ("e3", e3, e3_md), ("e4", e4, e4_md), ("e5", e5, e5_md)]
SENS = {"g8_clean": (("G8",), "trials failing G8 removed (AMENDMENT-6: 'reported with the results')",
                     "Verdicts that depend on keeping G8-failing trials"),
        "schedule_as_failed": (("schedule",), "trials still flagged `schedule` after their one rerun treated as failed (§4)",
                               "Verdicts that depend on keeping twice-failed `schedule` trials")}


def run_all(raw, pre):
    Exp.BOOTS.clear()
    POINT.clear()
    return {name: fn(raw, pre) for name, fn, _ in EXPS}


def verdicts_of(o):
    """Every verdict string in an experiment's `verdicts`, by path (registered rules and alternative readings)."""
    out = {}

    def walk(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                p = f"{path}/{k}" if path else k
                if k == "verdict" and isinstance(v, str):
                    out[path] = v
                elif k == "builds" and isinstance(v, dict):
                    for b, y in v.items():
                        out[f"{p}/{b}"] = y
                elif k == "E5 reading (A1.4)":
                    for b, y in v.items():
                        out[f"{p}/{b}"] = " / ".join(y)
                else:
                    walk(v, p)
    walk(o.get("verdicts", {}), "")
    return out


def sens_md(o):
    lines = ["## Sensitivity (not the registered verdicts)", ""]
    for mode, (_, what, head) in SENS.items():
        x = o["sensitivity"][mode]
        ch = x["changed"]
        lines.append(f"**{head}** ({what}; {x['verdicts_compared']} verdicts recomputed): "
                     + ("none." if not ch else ""))
        lines += [f"- {k}: {a} → {b}" for k, (a, b) in ch.items()]
        lines.append("")
    return "\n".join(lines)


def main(argv):
    raw, out = argv[0], argv[1]
    os.makedirs(out, exist_ok=True)
    pre = preround(raw)
    PRE.clear()
    PRE.update(pre)
    alt = {}
    for mode, (extra, _, _) in SENS.items():
        Exp.EXTRA = extra
        try:
            alt[mode] = {k: verdicts_of(v[0]) for k, v in run_all(raw, pre).items()}
        finally:
            Exp.EXTRA = ()
    primary = run_all(raw, pre)              # last, so nothing of the sensitivity runs is left in the stores
    dump(os.path.join(out, "preround.json"), {k: v for k, v in pre.items() if k.endswith("detail")})
    for name, _, mdfn in EXPS:
        o, _, cells = primary[name]
        pv = verdicts_of(o)
        same = lambda a, b: a == b or (a.startswith("no estimate") and b.startswith("no estimate"))
        o["sensitivity"] = {mode: {"description": SENS[mode][1], "verdicts_compared": len(pv),
                                   "changed": {k: [v, alt[mode][name].get(k, "no estimate")]
                                               for k, v in pv.items() if not same(v, alt[mode][name].get(k, "no estimate"))}}
                            for mode in SENS}
        dump(os.path.join(out, f"{name}.json"), o)
        with open(os.path.join(out, f"{name}.md"), "w") as f:
            f.write(mdfn(o, cells) + "\n" + sens_md(o) + "\n")
    with open(os.path.join(out, "CHOICES.md"), "w") as f:
        f.write("# Interpretation choices made by harness/run/report.py\n\n"
                "One line each: the prereg passage interpreted, then the reading implemented.\n\n")
        for where, what in CHOICES:
            f.write(f"- **{where}** — {what}\n")
        occ = Counter()
        for name, _, _ in EXPS:
            g = primary[name][0]["gates"]["failures_by_gate_in_counted_trials"]
            for flag in ("no-drain", "G2:gen-vs-h0tx"):
                occ[flag] += g.get(flag, 0)
        f.write(f"\nOccurrences in the counted trials of this data set: no-drain {occ['no-drain']}, "
                f"G2:gen-vs-h0tx {occ['G2:gen-vs-h0tx']}.\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: report.py RAW_DIR OUT_DIR")
    main(sys.argv[1:])
