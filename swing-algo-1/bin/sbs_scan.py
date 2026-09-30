#!/usr/bin/env python3
"""sbs_scan: swing buy/sell signal scan — the combiner (Swing Algo 1,
algorithm v3.1).

Pure consumer. This skill never looks at stock prices and does no
calculations of its own: no bars are read, no levels are derived, no
breaks are detected here. It reads the recommendations the earlier
skills already published in their state files and applies the user's
BUY / ADD / SELL criteria to the freshly CONFIRMED breaks:

  trend-detector -> state.json:            current trend per symbol
  fibonacci      -> data/fib_state.json:   confirmed fib level breaks

(The vwap skill still runs in the sweeps and reports to its TA chat,
but as of v3.0 the algorithm no longer consumes AVWAP crosses.)

Two modes:
  --mode daily: the evening run — consumes the daily 2-day-rule
    confirmations (basis="daily"); the fib/vwap structure must be as of
    today. Pipeline (weekdays, PT): 2:00 trend -> 2:05 fib -> 2:10 vwap ->
    2:15 this skill.
  --mode intraday: the hourly sweeps (6:30am-1:30pm PT weekdays) —
    consumes the 2x30-min-bar confirmations (basis="30min"); the fib/vwap
    structure must be as of the latest completed daily bar (e.g. Friday
    for a Monday run). The orchestrator bin/intraday_sweep.py runs
    fetch -> trend -> fib -> vwap -> this skill in sequence.

Conditions (the user's rules), evaluated on CONFIRMED breaks only:
  BUY  #1: (stock's index in UPTREND) AND (stock itself in UPTREND) AND
           (confirmed break ABOVE any fib level 23.6%..161.8%)
  SELL #1: (an open recommended BUY exists) AND
           (TWO fib levels have been lost since the last entry — a
            level is lost on a confirmed break BELOW it and restored on
            a confirmed break back ABOVE it; a single lost level is
            surfaced as a SELL WATCH, not a signal; every BUY and every
            ADD wipes the slate so only post-entry losses count) OR
           (the stock's intraday trend flips to DOWNTREND) OR
           (the stock's sector index is in DOWNTREND — sector-wide
            exit: when the index dumps, all bets are off)
  ADD:     (an open recommended BUY exists) AND the BUY #1 condition
           fires again — add to the position. v3.1: only when the break
           is above a fib level HIGHER than the highest level already
           bought (pyramid up; re-crossing the same/lower level adds
           nothing). SELL takes precedence when both fire. SELL exits the
           ENTIRE position (initial BUY + all ADD legs); P&L is reported
           vs the blended entry.

v1.1 change: "confirmed" now means the basis of the current mode —
2 consecutive 30-min closes beyond the level/line intraday, or the
2-day rule in the evening. Trigger basis is recorded on every position
and ledger row.

Safety: a ticker is evaluated only when the fib output carries
the expected structure date. A stale or missing scan contributes nothing
(no signal), never a guess. A stale trend reading fails the buy gate
closed (it can never read "uptrend"), while exits — which need no
trend — still work. Every confirmation acted on (or seen) is
remembered, so re-running the scan on the same day can never
double-fire a signal or double-append the ledger.

Every recommendation (BUY / ADD / SELL) plus the price at the time is
appended to output/sbs_recommendations.jsonl — an append-only ledger any
independent skill can read to analyze the algo's performance
(see output/SCHEMA.md).

State in data/sbs_state.json: open recommended-BUY positions (with ADD
legs), closed-signal history, and the set of confirmations already
consumed (re-run safety). The v1.0 per-ticker anchors/sides are
gone — the scans own that now; a v1.0 state file is migrated on load.

Usage:
    python3 bin/sbs_scan.py [--json] [--dry-run] [--mode daily|intraday]
    python3 bin/sbs_scan.py --mode intraday --advisory [--json]

--advisory: evaluate the same rules against Kaushik's personal holdings
(portfolio skill's data/kaushik.json) instead of the watchlist. Every
holding counts as an open position, so a fresh BUY condition surfaces as
an ADD recommendation and SELL conditions surface as SELL. Uses a
separate advisor state file (consumed confirmations, failed levels) and
never writes to the algo's state or ledger — the algo's own portfolio is
untouched. The sweeps run this as a best-effort stage and relay the
[ADVISOR] section to the "Kaushik's Portfolio" chat.
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.expanduser("~/workspace/cache/stock-data"))
import stock_cache as sc  # noqa: E402 — price formatting only; never reads bars

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Crypto-lane overrides: SWEEP_WATCHLIST points at an alternate watchlist
# JSON, SWEEP_STATE_SUFFIX (e.g. "_crypto") namespaces every state file the
# combiner touches so crypto and equity runs never share positions,
# consumed-confirmation sets, or upstream scan state. The ledger is shared
# (tickers never collide) with a "universe" tag per row.
_SUFFIX = os.environ.get("SWEEP_STATE_SUFFIX", "")
UNIVERSE = "crypto" if _SUFFIX else "equity"
STATE_PATH = os.path.join(SKILL_DIR, "data", "sbs_state%s.json" % _SUFFIX)
# Advisory state: same shape as the algo state (consumed confirmations,
# failed levels, trend watch) but fully independent — the advisor never
# touches the algo's positions, history, or ledger.
ADVISOR_STATE_PATH = os.path.join(
    SKILL_DIR, "data", "sbs_advisor_state%s.json" % _SUFFIX)
INDEX_MAP_PATH = os.path.join(SKILL_DIR, "data", "index_map.json")
LEDGER_PATH = os.path.join(SKILL_DIR, "output", "sbs_recommendations.jsonl")
WATCHLIST = os.path.expanduser(os.environ.get(
    "SWEEP_WATCHLIST", "~/workspace/cache/stock-data/watchlist.json"))
# Kaushik's personal holdings (portfolio skill). Cost basis is 0; the
# advisor only reads this file, never writes it.
HOLDINGS_PATH = os.path.expanduser(
    "~/workspace/skills/technical-analysis/portfolio/data/kaushik.json")

TA_DIR = os.path.expanduser("~/workspace/skills/technical-analysis")
TREND_STATE_PATH = os.path.join(TA_DIR, "trend-detector",
                                "state%s.json" % _SUFFIX)
FIB_STATE_PATH = os.path.join(TA_DIR, "fibonacci", "data",
                              "fib_state%s.json" % _SUFFIX)
# NOTE (v3.0): the algorithm no longer consumes the vwap skill's output,
# so its state file is not loaded here. The vwap scan still runs in the
# sweeps and reports to the Technical Analysis - VWAP chat.

# ALGO_VERSION tracks the algorithm — the BUY/ADD/SELL conditions — not
# software changes. Bumped 1.1 -> 1.2 (2026-09-28): BUY now also requires
# the stock itself to be in an uptrend (intraday reading) and accepts a
# confirmed break above ANY fib level (23.6%..161.8%), not just
# 23.6%/38.2%. SELL no longer fires on a single level break: it fires
# when TWO monitored levels have failed (a confirmed down-break fails a
# level, a confirmed up-break restores it; one failure = SELL WATCH) or
# when the stock's intraday trend flips to downtrend.
# Bumped 1.2 -> 2.0 (2026-09-28): SELL also fires when the stock's sector
# index is in DOWNTREND (sector-wide exit — index selling is arbitrary,
# so all bets are off). A new exit leg is a significant change to the
# algorithm, hence the major version bump. Index sideways does NOT exit;
# only a positive downtrend reading fires (fails closed on missing/stale
# index trend).
# Bumped 2.0 -> 3.0 (2026-09-29): AVWAP is out of the algorithm entirely.
# BUY/ADD fire only on confirmed breaks above fib levels (23.6%..161.8%);
# the level-based SELL fires when TWO fib levels have been lost since the
# last entry (BUY or ADD) — every entry wipes the failure slate, so only
# post-entry losses count, and pre-entry failures can never leak into a
# new position. The trend-flip and index-downtrend backstop exits are
# unchanged. Major bump: the entry/exit level set changed shape.
# Bumped 3.0 -> 3.1 (2026-09-30): ADD is pyramid-up only — it fires only
# on a confirmed break above a fib level HIGHER (by price) than the
# highest fib level already bought on this position. Re-crossing the
# same or a lower level on a later day no longer adds another leg, so
# oscillation around one line can't pile on legs (each such ADD also
# wiped the failure slate, resetting the exit memory). Minor bump: the
# ADD condition changed; the v3 family is unchanged.
ALGO_VERSION = "3.1"

# Set by --dry-run: suppresses ALL writes (state file + ledger).
DRY_RUN = False

# Set by --advisory: evaluate the algo's rules against Kaushik's personal
# holdings instead of the watchlist. Every holding counts as an open
# position, so a fresh BUY condition surfaces as ADD. Writes go to the
# advisor state file only — never the algo state, never the ledger.
ADVISORY = False

BUY_FIB_PCTS = (23.6, 38.2, 50.0, 61.8, 78.6, 100.0, 127.2, 161.8)  # v1.2: all monitored levels
HISTORY_CAP = 200


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def day_of(v):
    """Normalize an as-of stamp to YYYY-MM-DD (trend uses full ISO, scans use date)."""
    return str(v)[:10] if v else None


def expected_structure_date(fib_state):
    """The date the upstream scan says its daily structure is as of.

    Pure consumer: takes the modal `as_of` across the fib state entries
    (e.g. last Friday when run on a Monday morning). Returns None when
    the upstream state carries no usable date, which fails the run
    closed via the `no_structure_date` skip.
    """
    counts = {}
    for e in (fib_state or {}).values():
        d = day_of((e or {}).get("as_of"))
        if d:
            counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda d: counts[d])


def _active_state_path():
    return ADVISOR_STATE_PATH if ADVISORY else STATE_PATH


def holdings_for_universe():
    """Tickers in Kaushik's portfolio for this sweep's universe."""
    try:
        data = json.load(open(HOLDINGS_PATH))
    except Exception:
        return []
    return [h["ticker"] for h in data.get("holdings", [])
            if (h.get("universe") or UNIVERSE) == UNIVERSE]


def load_state():
    raw = load_json(_active_state_path())
    if "positions" not in raw and "tickers" in raw:
        # v1.0 shape: per-ticker anchors/sides/position -> keep positions only.
        positions = {}
        for t, e in raw["tickers"].items():
            p = (e or {}).get("position")
            if p:
                p.setdefault("adds", [])
                p.setdefault("version", "1.0")
                positions[t] = p
        return {"algo_version": "1.0", "positions": positions,
                "history": raw.get("history", []),
                "last_scan": raw.get("last_scan")}
    raw.setdefault("positions", {})
    raw.setdefault("history", [])
    return raw


def save_state(state):
    if DRY_RUN:
        return
    path = _active_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def load_index_map():
    m = load_json(INDEX_MAP_PATH)
    return m.get("map", {}), m.get("_default", "SPY")


# --------------------------------------------------------------------------
# Adapters: read-only views of the upstream skills' published outputs.
# No prices are read here; no calculations are done here.
# --------------------------------------------------------------------------

def adapter_trend(trend_state, sym, today, struct_day):
    """The trend-detector skill's published trend for a symbol.

    Freshness rule: the daily baseline (as_of) must be the expected
    structure date, and a 30-min intraday flip (basis="30min") only counts
    when it was made today. Returns {"trend", "close"}; trend is "n/a" when
    the skill has no fresh reading (fails the buy gate closed).
    """
    e = trend_state.get("tickers", {}).get(sym)
    if e is None:
        e = trend_state.get("indexes", {}).get(sym)
    if not e or day_of(e.get("as_of")) != struct_day:
        return {"trend": "n/a", "close": None}
    if e.get("basis") == "30min" and e.get("intraday_date") != today:
        return {"trend": "n/a", "close": None}
    return {"trend": e["trend"], "close": e["close"]}


def normalize_fib(rec):
    return {"kind": "fib", "pct": rec["pct"], "type": rec.get("type"),
            "level": rec["level"], "dir": rec["dir"],
            "decisive": rec["decisive"], "dist_pct": rec.get("dist_pct"),
            "touches": rec.get("touches"),
            "vol_ratio": rec.get("vol_ratio"),
            "vol_expansion": rec.get("vol_expansion"),
            "close": rec.get("close"),
            "basis": rec.get("basis", "daily"),
            # Carried through (not displayed) so an entry can stamp the
            # failure window at its own trigger's confirmation time.
            "confirm_date": rec.get("confirm_date"),
            "confirm_time": rec.get("confirm_time")}


def adapter_fib_breaks(fib_state, ticker, today, basis, consumed):
    """The fibonacci skill's breaks CONFIRMED on this day with this basis.

    basis="daily" matches the evening scan's 2-day confirmations;
    basis="30min" matches the intraday sweeps' 2-bar confirmations.
    Skips confirmations this skill already acted on (so a re-run never
    double-fires a signal).
    """
    recs = (fib_state.get(ticker) or {}).get("confirmed", [])
    out = []
    for r in recs:
        if r.get("confirm_date") != today:
            continue
        if r.get("basis", "daily") != basis:
            continue
        if already_consumed(consumed, ticker, "fib", r["pct"], today, basis,
                            r.get("dir")):
            continue
        out.append(normalize_fib(r))
    return out


def conf_key(ticker, kind, ident, day, basis, direction=None):
    # v1.2: the direction is part of the key — a down-break and a later
    # same-day up-reclaim of the same level are independent confirmations.
    # direction=None renders the legacy v1.1 key, still honored so
    # confirmations consumed before the upgrade stay consumed.
    if direction is None:
        return f"{ticker}|{kind}|{ident}|{day}|{basis}"
    return f"{ticker}|{kind}|{ident}|{direction}|{day}|{basis}"


def already_consumed(consumed, ticker, kind, ident, day, basis, direction):
    return (conf_key(ticker, kind, ident, day, basis, direction) in consumed
            or conf_key(ticker, kind, ident, day, basis) in consumed)


# --------------------------------------------------------------------------
# The user's conditions, applied to confirmed upstream recommendations
# --------------------------------------------------------------------------

def buy_condition(index_trend, stock_trend, fib_events):
    """The buy-side trigger (BUY #1). Returns the trigger event or None.

    v3.0: AVWAP is out — the trigger is a confirmed break above any
    monitored fib level (23.6%..161.8%), still gated on the sector index
    and the stock itself being in an uptrend. The same condition opens
    a fresh position (BUY) and, when a position is already open,
    recommends adding to it (ADD).
    """
    if index_trend != "uptrend":
        return None
    if stock_trend != "uptrend":
        return None
    return next((e for e in fib_events
                 if e["dir"] == "up" and e["pct"] in BUY_FIB_PCTS), None)


_FIB_TRIG_RE = re.compile(r"fib ([\d.]+)% ([\d.]+)")


def add_above_top(trig, top_level):
    """v3.1 pyramid-up guard for ADDs.

    An ADD fires only on a confirmed break above a fib level HIGHER (by
    price) than the highest fib level already bought on this position.
    Re-crossing the same or a lower level on a later day — oscillation
    around one line — produces no ADD. top_level=None (a legacy position
    whose entry levels predate this tracking, or an unparseable entry)
    fails open to the pre-3.1 behavior for that one ADD; the level is
    recorded from then on, so the guard applies to every later ADD.
    """
    if top_level is None:
        return True
    lvl = trig.get("level")
    return lvl is not None and lvl > top_level


def backfill_top_level(position):
    """One-time v3.1 migration: the highest fib level price among a legacy
    position's recorded entry triggers.

    Pre-3.1 entries stored only the human-readable trigger string
    ("fib 61.8% 607.58 above"); pre-3.0 AVWAP entries carry no fib level
    and are skipped. Returns None when nothing parseable exists — the
    guard then fails open for that position's next ADD.
    """
    prices = []
    texts = [position.get("trigger") or ""]
    texts += [a.get("trigger") or "" for a in position.get("adds", [])]
    for t in texts:
        m = _FIB_TRIG_RE.search(t)
        if m:
            prices.append(float(m.group(2)))
    return max(prices) if prices else None


def anchor_sig(fib_entry):
    """Signature of the fib anchors a ticker's levels are drawn from.

    When the scans redraw anchors the old levels are different prices, so
    a new signature resets that ticker's failed-level tracking.
    """
    a = (fib_entry or {}).get("anchors") or {}
    h, low = a.get("high") or {}, a.get("low") or {}
    return "%s@%s|%s@%s" % (h.get("date"), h.get("price"),
                            low.get("date"), low.get("price"))


def rec_stamp(rec):
    """Sortable confirmation timestamp of a fib break rec.

    30-min confirmations carry confirm_time ("2026-09-29T09:00", local
    PT); daily confirmations carry only confirm_date — published by the
    evening scan after the close, so they sort at end of day. Fixed-width
    ISO fields, so plain string comparison orders them.
    """
    t = (rec.get("confirm_time") or "")[:16]
    if t:
        return t
    d = rec.get("confirm_date") or ""
    return d + "T23:59" if d else ""


def backfill_failed_since(position, confirmed):
    """One-time migration: the failure-window start for a position that
    opened before failed_since tracking existed.

    The window starts at the LAST entry (BUY or ADD). Each entry's stamp
    is its entry-day confirmed UP break of the level named in its trigger
    string; the window start is the latest of those stamps. An entry that
    can't be matched (its rec aged out of the capped log, or a pre-3.0
    AVWAP trigger string) falls back to the start of its entry day.
    """
    def entry_stamp(trigger_text, entry_date):
        m = _FIB_TRIG_RE.search(trigger_text or "")
        if m:
            pct = float(m.group(1))
            stamps = [rec_stamp(r) for r in (confirmed or [])
                      if r.get("dir") == "up" and r.get("pct") == pct
                      and r.get("confirm_date") == entry_date]
            stamps = [s for s in stamps if s]
            if stamps:
                # The first signal of the day consumed the earliest
                # same-day confirmation; later dupes were consumed-skipped.
                return min(stamps)
        return entry_date + "T00:00" if entry_date else ""

    entries = [(position.get("trigger"), position.get("date"))]
    entries += [(a.get("trigger"), a.get("date"))
                for a in position.get("adds", [])]
    stamps = [s for s in (entry_stamp(t, d) for t, d in entries) if s]
    return max(stamps) if stamps else ""


def trend_down_trigger(stock_trend):
    """Synthetic trigger for the downtrend-flip SELL (no level broke)."""
    return {"kind": "trend", "dir": "down", "decisive": True,
            "dist_pct": None, "vol_ratio": None, "vol_expansion": None,
            "close": None, "basis": "30min", "stock_trend": stock_trend}


def index_down_trigger(index_trend, index_sym):
    """Synthetic trigger for the index-downtrend SELL (sector-wide exit)."""
    return {"kind": "trend", "dir": "down", "decisive": True,
            "dist_pct": None, "vol_ratio": None, "vol_expansion": None,
            "close": None, "basis": "30min", "index_sym": index_sym,
            "index_trend": index_trend}


def evaluate(ticker, position, index_trend, index_sym, stock_trend,
             prev_stock_trend, fib_events, failed_detail, top_level=None):
    """Apply the user's v3.0 buy/sell conditions.

    Returns (signal, trigger, reason). signal is BUY / ADD / SELL /
    WATCH / None. WATCH (one lost fib level, position open) is
    informational only — never a ledger row. With an open position SELL
    takes precedence: two lost fib levels exits the ENTIRE position, as
    does the stock's intraday trend flipping to downtrend, as does the
    sector index reading downtrend (sector-wide exit). Index sideways
    never exits; a missing/stale index trend ("n/a") fails closed.
    failed_detail carries only fib levels lost since the last entry
    (BUY or ADD wipes the slate). top_level is the v3.1 pyramid-up
    memory: the highest fib level price already bought on this position —
    an ADD fires only on a break above a HIGHER level; None disables the
    guard (legacy/unknown entries fail open).
    """
    if position is not None:
        if len(failed_detail) >= 2:
            # The most recently lost level is the trigger event.
            trig = list(failed_detail.values())[-1]
            return "SELL", trig, "two_failed_levels"
        if index_trend == "downtrend":
            # Sector-wide exit: when the index is in a downtrend, all
            # bets are off — exit the entire position.
            return "SELL", index_down_trigger(index_trend, index_sym), \
                "index_downtrend"
        if (prev_stock_trend not in (None, "n/a", "downtrend")
                and stock_trend == "downtrend"):
            return "SELL", trend_down_trigger(stock_trend), "trend_flip"
        trig = buy_condition(index_trend, stock_trend, fib_events)
        if trig and add_above_top(trig, top_level):
            return "ADD", trig, "buy_condition"
        if len(failed_detail) == 1:
            trig = list(failed_detail.values())[-1]
            return "WATCH", trig, "one_failed_level"
        return None, None, None
    trig = buy_condition(index_trend, stock_trend, fib_events)
    if trig:
        return "BUY", trig, "buy_condition"
    return None, None, None


def describe_trigger(trig):
    if trig["kind"] == "trend":
        if trig.get("index_sym"):
            return f"sector index {trig['index_sym']} in DOWNTREND"
        return "intraday trend flipped to downtrend"
    d = "above" if trig["dir"] == "up" else "below"
    if trig["kind"] == "fib":
        return f"fib {trig['pct']:.1f}% {sc.fmt_price(trig['level'])} {d}"
    label = "swing-high" if trig["line"] == "from_swing_high" else "swing-low"
    return f"AVWAP({label}) {sc.fmt_price(trig['avwap'])} {d}"


def fmt_failed_level(trig):
    """One-line label for a failed level (fib; the AVWAP branch is legacy
    for pre-3.0 rows — v3.0 tracks fib levels only)."""
    if trig["kind"] == "fib":
        return f"fib {trig['pct']:.1f}% {sc.fmt_price(trig['level'])}"
    label = "swing-high" if trig["line"] == "from_swing_high" else "swing-low"
    return f"AVWAP({label}) {sc.fmt_price(trig['avwap'])}"


def fmt_failed(failed_detail):
    return " + ".join(fmt_failed_level(e)
                      for e in failed_detail.values())


def fmt_trigger(trig):
    if trig["kind"] == "trend":
        if trig.get("index_sym"):
            return (f"sector index {trig['index_sym']} in DOWNTREND "
                    f"(sector-wide exit)")
        return "intraday trend flipped to DOWNTREND (backstop exit)"
    flag = "decisive" if trig["decisive"] else "marginal"
    if trig.get("vol_ratio") is not None:
        vol = (f"vol {trig['vol_ratio']}x EXPANSION" if trig.get("vol_expansion")
               else f"vol {trig['vol_ratio']}x")
    else:
        vol = "vol n/a"
    if trig["kind"] == "vwap":
        label = ("from swing high" if trig["line"] == "from_swing_high"
                 else "from swing low")
        return (f"break {trig['dir']} AVWAP {label} {sc.fmt_price(trig['avwap'])} "
                f"({flag}, {trig.get('crosses', '?')} crosses since anchor, {vol})")
    touch = (f"{trig['touches']} touches" if trig.get("touches") else "fresh level")
    return (f"break {trig['dir']} {trig['pct']:.1f}% fib {sc.fmt_price(trig['level'])} "
            f"({trig.get('type', '?')}, {flag}, {touch}, {vol})")


def ledger_row(date_iso, ticker, action, price, position, trig, itrend, strend,
               leg=None, extras=None):
    """One row for the append-only trade ledger.

    action is BUY / ADD / SELL. leg is the recommendation's leg number
    (BUY = 1, first ADD = 2, ...; None for SELL). legs_open is the number
    of open legs after this action (0 after a SELL).
    """
    row = {
        "algo_version": ALGO_VERSION,
        "universe": UNIVERSE,  # "equity" | "crypto"
        "date": date_iso,
        "ticker": ticker,
        "action": action,
        "price": sc.round_price(price),
        "sector_index": position.get("index") if position else None,
        "index_trend": itrend["trend"],
        "index_close": itrend["close"],
        "stock_trend": strend["trend"],
        "trigger": describe_trigger(trig),
        "trigger_kind": trig["kind"],  # "fib" | "trend"
        "trigger_dir": trig["dir"],    # "up" | "down"
        "trigger_basis": trig.get("basis", "daily"),  # "daily" | "30min"
        "decisive": trig["decisive"],
        "vol_ratio": trig.get("vol_ratio"),
        "vol_expansion": trig.get("vol_expansion"),
        "leg": leg,
        "legs_open": 0 if action == "SELL" else leg,
    }
    if extras:
        row.update(extras)
    return row


def append_ledger(row):
    """Append one recommendation row to the append-only trade ledger.

    The ledger lives in output/ (not data/): it is the durable record of
    every recommendation this algo ever made — BUY, ADD, SELL — together
    with the price at the time. Any independent skill can read this JSONL
    file and analyze the algo's performance without knowing anything about
    the internal state format. See output/SCHEMA.md.
    """
    if DRY_RUN:
        return
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "a") as fh:
        fh.write(json.dumps(row) + "\n")


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def scan(mode="daily"):
    """Run one evaluation pass.

    mode="daily": the evening run — consumes the daily 2-day-rule
    confirmations (basis="daily"); the fib structure must be as of
    today.
    mode="intraday": the hourly sweeps — consumes the 2x30-min-bar
    confirmations (basis="30min"); the fib structure must be as of
    the latest completed daily bar (e.g. Friday for a Monday run).
    """
    basis = "daily" if mode == "daily" else "30min"
    today = date.today().isoformat()
    # Wall-clock stamp for anchor-redraw window resets (local PT, same
    # clock the fib scan stamps its confirm_time values with).
    run_stamp = datetime.now().isoformat(timespec="minutes")
    if ADVISORY:
        tickers = holdings_for_universe()
    else:
        tickers = json.load(open(WATCHLIST))["tickers"]
    index_map, default_index = load_index_map()
    trend_state = load_json(TREND_STATE_PATH)
    fib_state = load_json(FIB_STATE_PATH)
    state = load_state()
    positions = state["positions"]
    if ADVISORY:
        # Every holding counts as an open position, so a fresh BUY
        # condition surfaces as ADD. Synthetic — never persisted.
        positions = {t: {"advisory": True, "version": ALGO_VERSION,
                         "adds": []} for t in tickers}
        # Advisory SELL episode memory: a SELL recommendation on a
        # personal holding must fire once per breakdown episode, not on
        # every sweep. (The algo's own SELLs can't re-fire — the SELL
        # deletes the position — but advisory positions are synthetic
        # and persist, so without this the same SELL repeats hourly.)
        # An episode ends when its condition clears: failures drop below
        # 2, or the index leaves its downtrend — then a future episode
        # may fire again. Never touched in non-advisory mode.
        sell_episodes = state.setdefault("advisor_sell_episodes", {})
        # v3.1: per-holding highest recommended-ADD fib level (pyramid-up
        # guard). Advisory positions are synthetic and rebuilt every run,
        # so this memory lives in the persisted advisor state, next to
        # the SELL episode memory.
        advisor_top_levels = state.setdefault("advisor_top_levels", {})
    else:
        sell_episodes = {}
        advisor_top_levels = {}
    history = state["history"]
    consumed = set(state.get("consumed", []))
    consumed_today = set()
    initialized = "algo_version" not in state
    for p in positions.values():
        p.setdefault("adds", [])
        p.setdefault("version", p.get("version", "1.0"))

    out = {"algo_version": ALGO_VERSION,
           "mode": f"{mode}-{basis}" + ("-advisory" if ADVISORY else ""),
           "as_of": today,
           "buys": [], "adds": [], "sells": [], "sell_watches": [],
           "open_positions": [],
           "skipped": [], "scanned": 0, "initialized": initialized}
    as_of_counts = {}

    # v3.0 SELL-side memory: per-ticker lost fib levels (level id ->
    # normalized trigger event of the break that lost it), the anchor
    # signature those levels were drawn from, and the last seen intraday
    # stock trend (for the downtrend-flip backstop exit).
    #
    # v3.1 fix (2026-09-30): per-ticker failure-window start. The old
    # code folded the entire published confirmed log into failed_levels
    # on every run, so pre-entry failures leaked back in one run after a
    # BUY/ADD wipe or an anchor redraw — a stale failure plus the first
    # genuine post-entry failure could fire an immediate two-level SELL.
    # Now only confirmations stamped AFTER the last entry (BUY/ADD) or
    # anchor redraw are folded in.
    failed_levels = state.get("failed_levels", {})
    level_anchors = state.get("level_anchors", {})
    trend_watch = state.get("trend_watch", {})
    failed_since = state.get("failed_since", {})
    if not ADVISORY:
        # One-time migration for positions opened before this tracking
        # existed. Advisory positions are synthetic (no entry event), so
        # they keep the full-history window until an advisory ADD sets it.
        for ticker, pos in positions.items():
            if ticker not in failed_since:
                f = fib_state.get(ticker) or {}
                failed_since[ticker] = backfill_failed_since(
                    pos, f.get("confirmed"))

    struct_day = (today if mode == "daily"
                  else expected_structure_date(fib_state))
    if not struct_day:
        out["skipped"].append({"ticker": "*",
                               "reason": "no_structure_date"})
        return out

    for ticker in sorted(tickers):
        f = fib_state.get(ticker)
        if not f:
            out["skipped"].append({"ticker": ticker, "reason": "no_scan_data"})
            continue
        fday = day_of(f.get("as_of"))
        if not fday or fday != struct_day:
            out["skipped"].append({"ticker": ticker,
                                   "reason": "stale_scan_data"})
            continue

        out["scanned"] += 1
        as_of_counts[fday] = as_of_counts.get(fday, 0) + 1

        # --- v3.1 failure tracking (fib levels only — AVWAP is out) ---
        # A confirmed DOWN break through a fib level loses it; a confirmed
        # UP break reclaims it. Only confirmations stamped AFTER the
        # failure-window start (last BUY/ADD, or last anchor redraw) are
        # folded in — pre-entry failures can never leak into a position.
        # Rebuilt from the published log every run, so re-runs and days
        # this skill missed stay safe. An anchor redraw moves the window
        # start to now: the levels are different prices, so nothing
        # confirmed before can fail the new ones. Legacy "vwap:*" keys
        # from pre-3.0 state are dropped by the rebuild (only fib:* keys
        # are ever added now).
        sig_now = anchor_sig(f)
        if level_anchors.get(ticker) not in (None, sig_now):
            failed_since[ticker] = run_stamp
        level_anchors[ticker] = sig_now
        since = failed_since.get(ticker) or ""
        fdet = {}
        for r in (f.get("confirmed") or []):
            if r.get("basis", "daily") != basis:
                continue
            if rec_stamp(r) <= since:
                continue
            lid = "fib:%.1f" % r["pct"]
            if r.get("dir") == "down":
                fdet.setdefault(lid, normalize_fib(r))
            elif r.get("dir") == "up":
                fdet.pop(lid, None)
        failed_levels[ticker] = fdet

        fib_events = adapter_fib_breaks(fib_state, ticker, today, basis,
                                        consumed)
        for e in fib_events:
            consumed_today.add(conf_key(ticker, "fib", e["pct"], today,
                                        basis, e["dir"]))
        index_sym = index_map.get(ticker, default_index)
        itrend = adapter_trend(trend_state, index_sym, today, struct_day)
        strend = adapter_trend(trend_state, ticker, today, struct_day)
        prev_stock_trend = trend_watch.get(ticker)
        trend_watch[ticker] = strend["trend"]

        if ADVISORY:
            # Expire a finished SELL episode so a future one may fire.
            ep = sell_episodes.get(ticker)
            if ep == "tfl" and len(fdet) < 2:
                del sell_episodes[ticker]
            elif ep and ep.startswith("idt|") and \
                    itrend["trend"] != "downtrend":
                del sell_episodes[ticker]

        position = positions.get(ticker)
        if ADVISORY:
            # Synthetic positions are rebuilt every run; the pyramid-up
            # memory lives in the persisted advisor state instead.
            top_level = advisor_top_levels.get(ticker)
        elif position is not None:
            if "top_level" not in position:
                # One-time v3.1 migration for positions opened earlier.
                position["top_level"] = backfill_top_level(position)
            top_level = position.get("top_level")
        else:
            top_level = None
        sig, trig, why = evaluate(ticker, position, itrend["trend"],
                                  index_sym, strend["trend"],
                                  prev_stock_trend,
                                  fib_events, fdet, top_level)
        if sig is None:
            continue
        if sig == "WATCH":
            # Informational only: one failed level on an open position.
            # Never a ledger row, never a signal the sweeps relay.
            out["sell_watches"].append({
                "ticker": ticker,
                "failed": fmt_failed(fdet),
                "stock_trend": strend["trend"],
                "trigger": fmt_trigger(trig),
            })
            continue
        price = trig.get("close") or strend["close"]
        if price is None:
            out["skipped"].append({"ticker": ticker, "reason": "no_price"})
            continue

        if sig == "BUY":
            positions[ticker] = {
                "version": ALGO_VERSION,
                "date": today, "price": sc.round_price(price),
                "index": index_sym, "index_trend": itrend["trend"],
                "trigger": describe_trigger(trig),
                "adds": [],
                # v3.1 pyramid-up memory: the fib level this entry broke
                # above. A later ADD fires only above a HIGHER level.
                "top_level": trig.get("level"),
            }
            # v3.1 fix: a fresh BUY restarts the failure window AT the
            # entry trigger's own confirmation stamp — only breaks
            # confirmed after this stamp can fail a level. (The per-run
            # rebuild above makes the old failed_levels[ticker] = {}
            # wipe redundant; the window start is the mechanism now.)
            failed_since[ticker] = rec_stamp(trig)
            out["buys"].append({
                "ticker": ticker, "price": sc.round_price(price),
                "index": index_sym, "index_trend": itrend["trend"],
                "index_close": itrend["close"],
                "stock_trend": strend["trend"],
                "trigger": fmt_trigger(trig),
            })
            history.append({"date": today, "ticker": ticker, "action": "BUY",
                            "price": sc.round_price(price),
                            "trigger": describe_trigger(trig)})
            append_ledger(ledger_row(today, ticker, "BUY", price, None,
                                     trig, itrend, strend, leg=1))
        elif sig == "ADD":
            # v3.1 fix: an ADD re-anchors the failure window at this
            # trigger's confirmation stamp — only breaks confirmed after
            # it can fail a level. Applies to the advisory window too:
            # the advisor state is separate from the algo's, and a
            # recommended ADD restarts its count the same way.
            failed_since[ticker] = rec_stamp(trig)
            if ADVISORY:
                # Advisory: a recommendation for his holding. No legs,
                # no ledger, no state mutation — just the signal. The
                # v3.1 pyramid-up level is recorded so the next
                # recommendation needs a HIGHER break.
                advisor_top_levels[ticker] = trig.get("level")
                out["adds"].append({
                    "ticker": ticker, "price": sc.round_price(price),
                    "index": index_sym, "index_trend": itrend["trend"],
                    "stock_trend": strend["trend"],
                    "trigger": fmt_trigger(trig),
                })
                continue
            # An open position exists and the buy-side condition fired
            # again: recommend adding to the position as a new leg.
            leg = len(position["adds"]) + 2
            position["adds"].append({
                "version": ALGO_VERSION, "date": today,
                "price": sc.round_price(price),
                "trigger": describe_trigger(trig), "leg": leg,
            })
            # v3.1: the guard allowed this ADD, so this level is now the
            # highest bought — future ADDs need to beat it.
            position["top_level"] = trig.get("level")
            out["adds"].append({
                "ticker": ticker, "price": sc.round_price(price),
                "index": index_sym, "index_trend": itrend["trend"],
                "stock_trend": strend["trend"],
                "trigger": fmt_trigger(trig),
                "leg": leg,
            })
            history.append({"date": today, "ticker": ticker, "action": "ADD",
                            "price": sc.round_price(price), "leg": leg,
                            "trigger": describe_trigger(trig)})
            append_ledger(ledger_row(today, ticker, "ADD", price, position,
                                     trig, itrend, strend, leg=leg))
        elif sig == "SELL":
            if ADVISORY:
                # Fire once per breakdown episode — never the same SELL
                # on consecutive sweeps. (trend_flip needs no memory:
                # prev==current after the first fire.)
                if why == "two_failed_levels":
                    ep_key = "tfl"
                elif why == "index_downtrend":
                    ep_key = "idt|" + index_sym
                else:
                    ep_key = None
                if ep_key is not None:
                    if sell_episodes.get(ticker) == ep_key:
                        continue  # already signaled this episode
                    sell_episodes[ticker] = ep_key
                # Advisory: a recommendation for his holding. No P&L
                # math (his cost basis is 0), no ledger, no state
                # mutation — just the signal.
                sell_trigger_text = fmt_trigger(trig)
                if why == "two_failed_levels":
                    sell_trigger_text = ("2 failed levels: " +
                                         fmt_failed(fdet))
                out["sells"].append({
                    "ticker": ticker, "price": sc.round_price(price),
                    "trigger": sell_trigger_text,
                    "stock_trend": strend["trend"],
                    "sell_reason": why,
                })
                continue
            # Exit the ENTIRE position — initial BUY plus every ADD leg.
            legs = [position["price"]] + \
                [a["price"] for a in position["adds"]]
            blended = sc.round_price(sum(legs) / len(legs))
            pnl_pct = round((price - blended) / blended * 100, 2)
            sell_trigger_text = fmt_trigger(trig)
            ledger_extras = {
                "entry_date": position["date"],
                "entry_price": position["price"],
                "n_adds": len(position["adds"]),
                "add_prices": [a["price"] for a in position["adds"]],
                "blended_entry": blended,
                "pnl_pct_vs_blended": pnl_pct,
                "legs_closed": len(legs),
                "sell_reason": why,  # "two_failed_levels" | "trend_flip" | "index_downtrend"
            }
            if why == "two_failed_levels":
                failed_txt = fmt_failed(fdet)
                sell_trigger_text = "2 failed levels: " + failed_txt
                ledger_extras["failed_levels"] = failed_txt
            out["sells"].append({
                "ticker": ticker, "price": sc.round_price(price),
                "entry_date": position["date"], "entry_price": position["price"],
                "n_adds": len(position["adds"]),
                "blended_entry": blended,
                "pnl_pct": pnl_pct,
                "legs_closed": len(legs),
                "trigger": sell_trigger_text,
                "stock_trend": strend["trend"],
            })
            history.append({
                "ticker": ticker, "signal": "BUY",
                "version": position["version"],
                "entry_date": position["date"],
                "entry_price": position["price"],
                "adds": position["adds"],
                "blended_entry": blended,
                "exit_date": today, "exit_price": sc.round_price(price),
                "exit_trigger": sell_trigger_text,
                "pnl_pct": pnl_pct,
            })
            append_ledger(ledger_row(
                today, ticker, "SELL", price, position, trig, itrend, strend,
                extras=ledger_extras))
            del positions[ticker]

    # Report still-open positions (current price from the trend skill's
    # published output — never read from the cache here).
    tickers_state = trend_state.get("tickers", {})
    if ADVISORY:
        for ticker in sorted(tickers):
            cur = (tickers_state.get(ticker) or {}).get("close")
            unr = {"ticker": ticker}
            if cur:
                unr["current"] = cur
            out["open_positions"].append(unr)
    else:
        for ticker in sorted(positions):
            pos = positions[ticker]
            cur = (tickers_state.get(ticker) or {}).get("close")
            prices = [pos["price"]] + [a["price"] for a in pos["adds"]]
            blended = sc.round_price(sum(prices) / len(prices))
            unr = {"ticker": ticker, "entry": pos["price"], "date": pos["date"],
                   "adds": len(pos["adds"]),
                   "add_prices": [a["price"] for a in pos["adds"]],
                   "blended_entry": blended}
            if cur:
                unr["current"] = cur
                unr["unrealized_pct"] = round((cur - blended) / blended * 100, 2)
            out["open_positions"].append(unr)

    state["positions"] = {} if ADVISORY else positions
    state["history"] = history[-HISTORY_CAP:]
    state["algo_version"] = ALGO_VERSION
    state["last_scan"] = date.today().isoformat()
    # v1.2 SELL-side memory (save_state suppresses the write on --dry-run).
    state["failed_levels"] = failed_levels
    state["level_anchors"] = level_anchors
    state["trend_watch"] = trend_watch
    # v3.1 fix: the per-ticker failure-window start (see above).
    state["failed_since"] = failed_since
    if not DRY_RUN:
        # Remember every confirmation seen today so a re-run never
        # double-fires a signal (whether or not it triggered one).
        consumed |= consumed_today
        state["consumed"] = sorted(consumed)[-500:]
    save_state(state)
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def report_advisory(out):
    """Compact advisory report: ADD/SELL recommendations on his holdings."""
    lines = [f"Swing Algo 1 v{out['algo_version']} advisory ({out['mode']}) — "
             f"{out['as_of']} · {out['scanned']} holdings"]
    if out["sells"]:
        lines.append(f"\nSELL ({len(out['sells'])})")
        for s in out["sells"]:
            lines.append(f"  {s['ticker']} @ {sc.fmt_price(s['price'])} — "
                         f"{s['trigger']}")
    if out["adds"]:
        lines.append(f"\nADD ({len(out['adds'])})")
        for a in out["adds"]:
            lines.append(f"  {a['ticker']} @ {sc.fmt_price(a['price'])} — "
                         f"{a['trigger']}")
    if not out["sells"] and not out["adds"]:
        lines.append("\nNo advisory signals.")
    return "\n".join(lines)


DIV = "—" * 32


def report(out):
    lines = [f"Swing Algo 1 v{out['algo_version']} ({out['mode']}) — "
             f"{out['as_of']} · {out['scanned']} tickers"]
    if out["initialized"]:
        lines.append("(inaugural run — no prior positions)")
    lines.append(DIV)
    if out["sells"]:
        lines.append(f"SELL — exit entire position ({len(out['sells'])})")
        for s in out["sells"]:
            adds = (f", {s['n_adds']} add(s), blended {sc.fmt_price(s['blended_entry'])}"
                    if s["n_adds"] else "")
            lines.append(
                f"  {s['ticker']} @ {sc.fmt_price(s['price'])} — "
                f"{s['trigger']} · entered {s['entry_date']} @ "
                f"{sc.fmt_price(s['entry_price'])}{adds} → {s['pnl_pct']:+.2f}% "
                f"(entire position closed)")
        lines.append(DIV)
    if out["buys"]:
        lines.append(f"BUY ({len(out['buys'])})")
        for b in out["buys"]:
            lines.append(
                f"  {b['ticker']} @ {sc.fmt_price(b['price'])} — {b['index']} in "
                f"{b['index_trend'].upper()} · {b['trigger']}")
        lines.append(DIV)
    if out["adds"]:
        lines.append(f"ADD ({len(out['adds'])})")
        for a in out["adds"]:
            lines.append(
                f"  {a['ticker']} @ {sc.fmt_price(a['price'])} — add to open position "
                f"(leg {a['leg']}) · {a['trigger']}")
        lines.append(DIV)
    if out["sell_watches"]:
        lines.append(f"SELL WATCH — first level failure ({len(out['sell_watches'])})")
        for w in out["sell_watches"]:
            lines.append(
                f"  {w['ticker']} — {w['failed']} failed; one more level failure "
                f"triggers SELL · {w['trigger']}")
        lines.append(DIV)
    if out["open_positions"]:
        lines.append(f"OPEN POSITIONS ({len(out['open_positions'])})")
        for p in out["open_positions"]:
            u = (f"{p['unrealized_pct']:+.2f}%" if p.get("unrealized_pct") is not None
                 else "n/a")
            cur = sc.fmt_price(p['current']) if p.get("current") is not None else "n/a"
            adds_tag = f" (+{p['adds']} adds)" if p.get("adds") else ""
            lines.append(
                f"  {p['ticker']}{adds_tag} — entered {p['date']} @ "
                f"{sc.fmt_price(p['entry'])}, now {cur} ({u}) · blended "
                f"{sc.fmt_price(p['blended_entry'])}")
        lines.append(DIV)
    if not out["buys"] and not out["adds"] and not out["sells"]:
        lines.append("No new swing signals today.")
    if out["skipped"]:
        names = ", ".join(f"{s['ticker']} ({s['reason']})"
                          for s in out["skipped"])
        lines.append(f"Skipped: {names}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute everything but do not write state or ledger")
    ap.add_argument("--mode", choices=("daily", "intraday"), default="daily",
                    help="daily: evening run on 2-day-rule confirmations; "
                         "intraday: hourly sweep on 2x30-min confirmations")
    ap.add_argument("--advisory", action="store_true",
                    help="evaluate the algo's rules against Kaushik's "
                         "personal holdings (portfolio/data/kaushik.json) "
                         "instead of the watchlist. Every holding counts as "
                         "an open position, so fresh BUY conditions surface "
                         "as ADD. Uses separate advisor state; never touches "
                         "the algo's state or ledger.")
    args = ap.parse_args()

    if args.dry_run:
        # Suppress all writes (state + ledger).
        global DRY_RUN
        DRY_RUN = True
    if args.advisory:
        global ADVISORY
        ADVISORY = True

    out = scan(mode=args.mode)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(report_advisory(out) if ADVISORY else report(out))


if __name__ == "__main__":
    main()
