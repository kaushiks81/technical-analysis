#!/usr/bin/env python3
"""Regression tests for Swing Algo 1 v3.3 (ChatGPT review findings).

Covers the v3.3 SELL-side robustness batch:
  #1  removed-from-watchlist positions are still evaluated for SELL/WATCH,
      but get no new BUY/ADD legs;
  #2  missing/stale fib data no longer freezes the trend-based exits;
  #3  a stale ("n/a") trend reading no longer overwrites the last valid
      trend in trend_watch (which permanently suppressed the flip exit);
  #5  a confirmation whose signal had no price is NOT consumed, so a
      later same-day run can retry it;
  #6  the 500-key consumed cap prunes oldest-first (date-first keys),
      and pre-v3.3 ticker-first keys are still honored.

Follow-up review round (folded into 3.3 — fixes based on code review,
no version bump):
  P1  a total fib outage (empty fib state) no longer aborts the intraday
      scan at the no_structure_date check — the structure date falls back
      to the trend state's date, so the fib-independent trend exits still
      run;
  P2  the 500-key consumed cap sorts by embedded day, so mixed
      legacy/date-first key sets still prune oldest-first (plain sorted()
      would retain legacy keys ahead of newer date-first keys).

Run: python3 test_v33_sell_robustness.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbs_scan as sbs

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          (" — " + str(detail) if detail and not cond else ""))


def write_json(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)


def make_trend(sym, trend, close, as_of):
    return {"trend": trend, "close": close, "as_of": as_of,
            "basis": "daily"}


def make_fib(as_of, confirmed=(), anchors=None):
    return {"as_of": as_of, "confirmed": list(confirmed),
            "anchors": anchors or {"high": {"date": "2026-09-01",
                                            "price": 200.0},
                                   "low": {"date": "2026-08-01",
                                           "price": 100.0}}}


def make_break(pct, direction, level, close, confirm_date):
    return {"pct": pct, "dir": direction, "type": "retracement",
            "level": level, "decisive": True, "dist_pct": 0.6,
            "touches": 3, "vol_ratio": 1.0, "vol_expansion": False,
            "close": close, "basis": "daily", "confirm_date": confirm_date,
            "confirm_time": None}


def make_position(price=100.0, top_level=50.0):
    return {"version": "3.3", "date": "2026-09-29", "price": price,
            "index": "SPY", "index_trend": "uptrend",
            "trigger": "fib 61.8% 150.00", "trigger_basis": "daily",
            "adds": [], "top_level": top_level,
            "failed_since": "2026-09-29"}


class Harness:
    """Redirects all of sbs_scan's file I/O into a temp dir."""

    def __init__(self, today):
        self.tmp = tempfile.mkdtemp(prefix="sbs33_")
        self.today = today
        sbs.WATCHLIST = os.path.join(self.tmp, "watchlist.json")
        sbs.TREND_STATE_PATH = os.path.join(self.tmp, "trend.json")
        sbs.FIB_STATE_PATH = os.path.join(self.tmp, "fib.json")
        sbs.STATE_PATH = os.path.join(self.tmp, "state.json")
        sbs.LEDGER_PATH = os.path.join(self.tmp, "ledger.jsonl")
        sbs.DRY_RUN = False

    def setup(self, watchlist, trend_tickers, fib_entries,
              positions=None, trend_watch=None, consumed=()):
        write_json(sbs.WATCHLIST, {"tickers": watchlist})
        write_json(sbs.TREND_STATE_PATH, {"tickers": trend_tickers})
        write_json(sbs.FIB_STATE_PATH, fib_entries)
        write_json(sbs.STATE_PATH, {
            "positions": positions or {},
            "consumed": list(consumed),
            "trend_watch": trend_watch or {},
            "level_anchors": {},
            "failed_since": {},
            "failed_levels": {},
        })

    def run(self):
        return sbs.scan("daily")

    def state(self):
        with open(sbs.STATE_PATH) as fh:
            return json.load(fh)

    def signals(self, out):
        return ([(s["ticker"], "BUY") for s in out.get("buys", [])] +
                [(s["ticker"], "ADD") for s in out.get("adds", [])] +
                [(s["ticker"], "SELL") for s in out.get("sells", [])])


TODAY = "2026-09-30"
YDAY = "2026-09-29"

# ---------------------------------------------------------------- #1
h = Harness(TODAY)
h.setup(
    watchlist=["AAA"],
    trend_tickers={
        "AAA": make_trend("AAA", "uptrend", 110.0, TODAY),
        "ZZ9": make_trend("ZZ9", "uptrend", 120.0, TODAY),
        "SPY": make_trend("SPY", "uptrend", 500.0, TODAY),
    },
    fib_entries={
        # ZZ9 was REMOVED from the watchlist but still has an open
        # position. A fresh confirmed up-break above its top_level
        # would be an ADD under the old code.
        "ZZ9": make_fib(TODAY, [make_break(61.8, "up", 60.0, 121.0,
                                           TODAY)]),
    },
    positions={"ZZ9": make_position(top_level=50.0)},
)
out = h.run()
sigs = h.signals(out)
check("#1 removed ticker gets no ADD",
      not any(t == "ZZ9" and s == "ADD" for t, s in sigs), sigs)
check("#1 removed ticker still in state (not stranded silently)",
      "ZZ9" in h.state()["positions"])

# Now flip ZZ9's trend to downtrend: the exit must still fire.
h.setup(
    watchlist=["AAA"],
    trend_tickers={
        "AAA": make_trend("AAA", "uptrend", 110.0, TODAY),
        "ZZ9": make_trend("ZZ9", "downtrend", 118.0, TODAY),
        "SPY": make_trend("SPY", "uptrend", 500.0, TODAY),
    },
    fib_entries={"ZZ9": make_fib(TODAY)},
    positions={"ZZ9": make_position(top_level=50.0)},
    trend_watch={"ZZ9": "uptrend"},
)
out = h.run()
sigs = h.signals(out)
check("#1 removed ticker still exits on trend flip",
      ("ZZ9", "SELL") in sigs, sigs)
check("#1 SELL reason is trend_flip",
      any(s.get("ticker") == "ZZ9" and "DOWNTREND" in s.get("trigger", "")
          for s in out.get("sells", [])), out.get("sells"))
check("#1 position closed after SELL",
      "ZZ9" not in h.state()["positions"])

# ---------------------------------------------------------------- #2
h = Harness(TODAY)
h.setup(
    watchlist=["BBB", "CCC"],
    trend_tickers={
        # BBB: NO fib entry at all (fib scan outage for this ticker).
        "BBB": make_trend("BBB", "downtrend", 90.0, TODAY),
        # CCC: fib entry present but stale.
        "CCC": make_trend("CCC", "downtrend", 91.0, TODAY),
        "SPY": make_trend("SPY", "uptrend", 500.0, TODAY),
    },
    fib_entries={"CCC": make_fib(YDAY)},
    positions={"BBB": make_position(), "CCC": make_position()},
    trend_watch={"BBB": "uptrend", "CCC": "uptrend"},
)
out = h.run()
sigs = h.signals(out)
check("#2 trend-flip SELL fires with no fib data",
      ("BBB", "SELL") in sigs, sigs)
check("#2 trend-flip SELL fires with stale fib data",
      ("CCC", "SELL") in sigs, sigs)

# Index-downtrend exit with no fib data at all.
h = Harness(TODAY)
h.setup(
    watchlist=["BBB"],
    trend_tickers={
        "BBB": make_trend("BBB", "uptrend", 90.0, TODAY),
        "SPY": make_trend("SPY", "downtrend", 490.0, TODAY),
    },
    fib_entries={},
    positions={"BBB": make_position()},
)
out = h.run()
sigs = h.signals(out)
check("#2 index-downtrend SELL fires with no fib data",
      ("BBB", "SELL") in sigs, sigs)
check("#2 SELL reason is index_downtrend",
      any(s.get("ticker") == "BBB" and "sector index" in s.get("trigger", "")
          for s in out.get("sells", [])), out.get("sells"))

# ---------------------------------------------------------------- #3
h = Harness(TODAY)
h.setup(
    watchlist=["DDD"],
    trend_tickers={
        # Stale trend read -> adapter returns "n/a".
        "DDD": make_trend("DDD", "uptrend", 95.0, YDAY),
        "SPY": make_trend("SPY", "uptrend", 500.0, TODAY),
    },
    fib_entries={"DDD": make_fib(TODAY)},
    positions={"DDD": make_position()},
    trend_watch={"DDD": "uptrend"},
)
h.run()
tw = h.state()["trend_watch"].get("DDD")
check("#3 stale read does not overwrite last valid trend",
      tw == "uptrend", tw)

# Next valid read is downtrend -> the flip exit must fire.
h.setup(
    watchlist=["DDD"],
    trend_tickers={
        "DDD": make_trend("DDD", "downtrend", 93.0, TODAY),
        "SPY": make_trend("SPY", "uptrend", 500.0, TODAY),
    },
    fib_entries={"DDD": make_fib(TODAY)},
    positions={"DDD": make_position()},
    trend_watch={"DDD": "uptrend"},  # as the fixed run 1 left it
)
out = h.run()
sigs = h.signals(out)
check("#3 downtrend after n/a still exits (flip observed)",
      ("DDD", "SELL") in sigs, sigs)

# ---------------------------------------------------------------- #5
h = Harness(TODAY)
h.setup(
    watchlist=["EEE"],
    trend_tickers={
        # Trend close unavailable -> trigger has no price anywhere.
        "EEE": make_trend("EEE", "uptrend", None, TODAY),
        "SPY": make_trend("SPY", "uptrend", 500.0, TODAY),
    },
    fib_entries={"EEE": make_fib(
        TODAY, [make_break(61.8, "up", 150.0, None, TODAY)])},
)
out = h.run()
skipped = [s for s in out.get("skipped", [])
           if s.get("ticker") == "EEE" and s.get("reason") == "no_price"]
check("#5 priceless signal is skipped, not fired", len(skipped) == 1,
      out.get("skipped"))
consumed = h.state()["consumed"]
check("#5 priceless confirmation is NOT consumed",
      not any("|EEE|fib|" in k for k in consumed), consumed)

# Prices arrive later the same day -> the retry must fire the BUY.
h.setup(
    watchlist=["EEE"],
    trend_tickers={
        "EEE": make_trend("EEE", "uptrend", 155.0, TODAY),
        "SPY": make_trend("SPY", "uptrend", 500.0, TODAY),
    },
    fib_entries={"EEE": make_fib(
        TODAY, [make_break(61.8, "up", 150.0, 155.0, TODAY)])},
    consumed=tuple(consumed),  # carry the (unconsumed) state forward
)
out = h.run()
sigs = h.signals(out)
check("#5 retry after price arrives fires BUY",
      ("EEE", "BUY") in sigs, sigs)

# ---------------------------------------------------------------- #6
old_key = "AAA|fib|61.8|up|2026-09-30|daily"          # pre-v3.3 format
old_legacy = "AAA|fib|61.8|2026-09-30|daily"          # pre-v1.2 format
check("#6 honors pre-v3.3 ticker-first keys",
      sbs.already_consumed({old_key}, "AAA", "fib", 61.8,
                           "2026-09-30", "daily", "up"))
check("#6 honors pre-v1.2 directionless keys",
      sbs.already_consumed({old_legacy}, "AAA", "fib", 61.8,
                           "2026-09-30", "daily", None))
new_key = sbs.conf_key("AAA", "fib", 61.8, "2026-09-30", "daily", "up")
check("#6 new keys are date-first",
      new_key.startswith("2026-09-30|"), new_key)
# Date-first => the cap's sort key orders by embedded day: the cap keeps
# the newest.
k_old = sbs.conf_key("ZZZ", "fib", 61.8, "2026-09-28", "daily", "up")
k_new = sbs.conf_key("AAA", "fib", 61.8, "2026-09-30", "daily", "up")
check("#6 older date sorts before newer regardless of ticker",
      sbs._consumed_sort_key(k_old) < sbs._consumed_sort_key(k_new),
      (k_old, k_new))
bag = {sbs.conf_key("T%03d" % i, "fib", 61.8, "2026-09-28", "daily",
                    "up") for i in range(300)}
bag |= {sbs.conf_key("T%03d" % i, "fib", 61.8, "2026-09-30", "daily",
                     "up") for i in range(300)}
kept = set(sorted(bag, key=sbs._consumed_sort_key)[-500:])
evicted = bag - kept
check("#6 500-key cap evicts oldest-first",
      len(kept) == 500 and
      max(sbs._consumed_sort_key(k)[0] for k in evicted) <=
      min(sbs._consumed_sort_key(k)[0] for k in kept) and
      all(k.startswith("2026-09-28|") for k in evicted),
      (len(kept), len(evicted)))

# ---------------------------------------------------------------- P1 (follow-up)
# A TOTAL fib outage (empty fib state) in intraday mode used to abort the
# whole scan at the no_structure_date check, freezing even the
# fib-independent trend exits. The structure date now falls back to the
# trend state's date, so the exits still run.
h = Harness(TODAY)
h.setup(
    watchlist=["FFF"],
    trend_tickers={
        "FFF": make_trend("FFF", "downtrend", 90.0, TODAY),
        "SPY": make_trend("SPY", "uptrend", 500.0, TODAY),
    },
    fib_entries={},  # total fib outage: empty state
    positions={"FFF": make_position()},
    trend_watch={"FFF": "uptrend"},
)
out = sbs.scan("intraday")
sigs = h.signals(out)
check("P1 intraday scan survives empty fib state",
      out.get("scanned", 0) > 0 and
      not any(s.get("reason") == "no_structure_date"
              for s in out.get("skipped", [])),
      out.get("skipped"))
check("P1 trend-flip SELL fires with empty fib state (intraday)",
      ("FFF", "SELL") in sigs, sigs)
check("P1 SELL reason is trend_flip",
      any(s.get("ticker") == "FFF" and "DOWNTREND" in s.get("trigger", "")
          for s in out.get("sells", [])), out.get("sells"))

# Index-downtrend exit with empty fib state, intraday mode.
h = Harness(TODAY)
h.setup(
    watchlist=["GGG"],
    trend_tickers={
        "GGG": make_trend("GGG", "uptrend", 90.0, TODAY),
        "SPY": make_trend("SPY", "downtrend", 490.0, TODAY),
    },
    fib_entries={},
    positions={"GGG": make_position()},
)
out = sbs.scan("intraday")
sigs = h.signals(out)
check("P1 index-downtrend SELL fires with empty fib state (intraday)",
      ("GGG", "SELL") in sigs, sigs)

# ---------------------------------------------------------------- P2 (follow-up)
# Mixed key formats: plain sorted() retains ticker-first legacy keys ahead
# of newer date-first keys (digits < letters), evicting today's
# confirmations first. The reviewer's reproduction — 500 legacy keys plus
# one fresh date-first key — must keep the fresh key and evict oldest.
bag = {"L%03d|fib|61.8|up|2026-09-28|daily" % i for i in range(500)}
fresh = sbs.conf_key("AAA", "fib", 61.8, "2026-09-30", "daily", "up")
bag.add(fresh)
kept = set(sorted(bag, key=sbs._consumed_sort_key)[-500:])
evicted = bag - kept
check("P2 mixed-format cap keeps today's key",
      fresh in kept and len(kept) == 500, (len(kept), len(evicted)))
check("P2 mixed-format cap evicts oldest-first",
      len(evicted) == 1 and
      all(sbs._consumed_sort_key(k)[0] == "2026-09-28" for k in evicted),
      list(evicted)[:3])
# Sanity: the OLD code path would have failed this (plain sorted keeps
# the legacy keys, evicting the fresh one).
old_kept = set(sorted(bag)[-500:])
check("P2 old plain-sorted cap would evict the fresh key",
      fresh not in old_kept, "old path kept fresh key?!")

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
