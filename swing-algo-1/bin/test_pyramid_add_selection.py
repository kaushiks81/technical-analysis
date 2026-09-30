#!/usr/bin/env python3
"""Isolated regression test for the v3.1 pyramid-up ADD selection fix.

Defect (found in code review 2026-09-30): buy_condition() selected only
the FIRST eligible up-break via next(), and evaluate() then applied the
top_level guard to that single event. With an ordered event list
[same/lower event, higher event], the blocked lower event suppressed the
valid higher ADD — the higher event was never considered.

Fix: buy_condition() now takes top_level and, for the ADD path, selects
the first event ABOVE top_level, skipping blocked same/lower re-crosses.

This test is isolated: it imports only buy_condition/evaluate from
sbs_scan and uses synthetic events. It does not touch state, cache,
or the ledger.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.expanduser("~"),
                               "workspace/skills/technical-analysis/swing-algo-1/bin"))
sys.path.insert(0, os.path.join(os.path.expanduser("~"),
                               "workspace/cache/stock-data"))

import sbs_scan as sbs


def ev(pct, level):
    """A normalized confirmed UP break of a fib level."""
    return {"kind": "fib", "pct": pct, "type": "retracement",
            "level": level, "dir": "up", "decisive": False,
            "dist_pct": 0.1, "touches": 3, "vol_ratio": 1.0,
            "vol_expansion": False, "close": level * 1.001,
            "basis": "30min", "confirm_date": "2026-09-30",
            "confirm_time": "2026-09-30T09:00"}


def position_with_top(top):
    return {"version": "3.1", "date": "2026-09-29", "price": 90.0,
            "index": "SMH", "index_trend": "uptrend",
            "trigger": "fib 50.0% 90.00 above",
            "adds": [], "top_level": top}


passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


print("1. Defect scenario: [lower blocked, higher valid] -> ADD fires on higher")
pos = position_with_top(100.0)          # bought 50% @ 100
events = [ev(38.2, 90.0), ev(61.8, 110.0)]  # 38.2 re-cross (blocked), 61.8 new high
sig, trig, why = sbs.evaluate("TEST", pos, "uptrend", "SMH", "uptrend",
                              "uptrend", events, {}, 100.0)
check("signal is ADD", sig == "ADD")
check("trigger is the 61.8% event", trig is not None and trig["pct"] == 61.8)
check("reason is buy_condition", why == "buy_condition")

print("2. Single lower event still blocked (no ADD, no crash)")
sig, trig, why = sbs.evaluate("TEST", pos, "uptrend", "SMH", "uptrend",
                              "uptrend", [ev(38.2, 90.0)], {}, 100.0)
check("signal is None", sig is None)

print("3. Single higher event fires ADD")
sig, trig, why = sbs.evaluate("TEST", pos, "uptrend", "SMH", "uptrend",
                              "uptrend", [ev(61.8, 110.0)], {}, 100.0)
check("signal is ADD", sig == "ADD")
check("trigger is 61.8%", trig is not None and trig["pct"] == 61.8)

print("4. Reversed order [higher, lower] -> ADD on higher (first valid)")
events = [ev(61.8, 110.0), ev(38.2, 90.0)]
sig, trig, why = sbs.evaluate("TEST", pos, "uptrend", "SMH", "uptrend",
                              "uptrend", events, {}, 100.0)
check("signal is ADD", sig == "ADD")
check("trigger is 61.8%", trig is not None and trig["pct"] == 61.8)

print("5. BUY path (no position) still picks first eligible event")
sig, trig, why = sbs.evaluate("TEST", None, "uptrend", "SMH", "uptrend",
                              None, [ev(38.2, 90.0), ev(61.8, 110.0)],
                              {}, None)
check("signal is BUY", sig == "BUY")
check("trigger is 38.2% (first eligible)", trig is not None and trig["pct"] == 38.2)

print("6. top_level=None fails open (legacy): lower event fires ADD")
pos_none = position_with_top(None)
sig, trig, why = sbs.evaluate("TEST", pos_none, "uptrend", "SMH", "uptrend",
                              "uptrend", [ev(38.2, 90.0)], {}, None)
check("signal is ADD", sig == "ADD")

print("7. buy_condition direct: gates still apply")
check("index not uptrend -> None",
      sbs.buy_condition("sideways", "uptrend", [ev(61.8, 110.0)], 100.0) is None)
check("stock not uptrend -> None",
      sbs.buy_condition("uptrend", "sideways", [ev(61.8, 110.0)], 100.0) is None)
check("no events -> None",
      sbs.buy_condition("uptrend", "uptrend", [], 100.0) is None)
check("down-break ignored",
      sbs.buy_condition("uptrend", "uptrend",
                        [{"kind": "fib", "pct": 61.8, "level": 110.0,
                          "dir": "down"}], 100.0) is None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
