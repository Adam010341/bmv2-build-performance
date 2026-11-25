#!/usr/bin/env python3
"""AMENDMENT-14 (d): x15.py reads perf report's --stdio table and counts the fixed logging pattern.

    python3 -I harness/tests/test_x15.py [DIR_WITH_x15.py]
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "run"))
import x15  # noqa: E402

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + f"{name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    fails += not ok


# shape of `perf report --stdio --no-children -g none --sort symbol` (perf 6.x)
REPORT = """# To display the perf.data header info, please use --header/--header-only options.
#
# Total Lost Samples: 0
#
# Samples: 10K of event 'cycles:u'
# Event count (approx.): 123456789
#
# Overhead  Symbol
# ........  ..............................................
#
    31.20%  [.] fmt::v9::detail::vformat_to<char>
    12.50%  [.] spdlog::logger::log_it_
     9.75%  [.] bm::MatchUnitAbstract<bm::MatchUnitExact>::lookup
     4.00%  [.] bm::Logger::get
     2.25%  [k] 0xffffffff81000000
     0.30%  [.] std::_Hashtable<int, ...>::find
"""
TWO = REPORT.replace("event 'cycles:u'", "event 'cpu_atom/cycles/u'") + "\n" + \
    REPORT.replace("event 'cycles:u'", "event 'cpu_core/cycles/u'").replace("31.20%", "30.20%").replace("0.30%", "1.30%")
sec = x15.parse_sections(TWO)
check("two PMU sections kept apart", sorted(sec), ["cpu_atom/cycles/u", "cpu_core/cycles/u"])
check("primary is cpu_core", x15.primary(sec), "cpu_core/cycles/u")
check("parse_report takes the primary section only", x15.parse_report(TWO)[0], (30.20, "fmt::v9::detail::vformat_to<char>"))
check("no double counting over sections", x15.summarise(x15.parse_report(TWO))["total_pct"], 60.0)
rows = x15.parse_report(REPORT)
check("rows parsed, header skipped", len(rows), 6)
check("largest first", rows[0], (31.20, "fmt::v9::detail::vformat_to<char>"))
s = x15.summarise(rows)
check("log pattern share (fmt::, spdlog, Logger)", s["log_pattern_pct"], 47.7)
check("table lookup not counted as logging", any("lookup" in t["symbol"] for t in s["top"]), True)
check("total", s["total_pct"], 60.0)
check("top capped at 15", len(x15.summarise([(1.0, f"f{i}") for i in range(20)])["top"]), 15)
check("pattern is the amendment's", x15.LOG_PATTERN.pattern, r"spdlog|fmt::|Logger|logger|debug_log|BMLOG|log_msg")
check("builds are the amendment's", x15.BUILDS, ("cm-default", "cm-o3-L1-E1", "at-docs-perf"))
print("ALL OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
