# swing-algo-1

The **combiner** — the last skill in the pipeline. It takes the
outputs of the skills that ran before it and turns them into swing
**BUY** / **ADD** / **SELL** signals. Built 2026-09-25.

**Algo version: 3.3** (2026-09-30: fixes based on code review —
SELL-side robustness: the scan covers the watchlist ∪ open positions so
a removed ticker's position can still exit, missing/stale fib data no
longer freezes the trend-based exits, and a stale trend reading no longer
poisons the flip-exit memory; two follow-up review findings folded into
3.3 with no version bump: a total fib outage no longer aborts the
intraday scan (the structure date falls back to the trend state's date),
and the consumed-confirmation cap prunes oldest-first across mixed key
formats; v3.2 fixed the pyramid-up ADD trigger selection to skip blocked
same/lower re-crosses; v3.1 made the ADD pyramid-up only;
v3.0 took AVWAP out of the algorithm entirely — entries fire only on
confirmed breaks above fib levels, and the level-based exit fires when
**two fib levels have been lost since the last entry**; every BUY and
every ADD wipes the failure slate. The trend-flip and
sector-index-downtrend backstop exits are unchanged).
The version tracks the algorithm —
the BUY/ADD/SELL conditions — not software changes. Bump the
`ALGO_VERSION` constant in `bin/sbs_scan.py` only when the conditions
change; every ledger row and the state file carry the version, so a
performance analysis can always attribute results to the right rule set.

**Implementation note (2026-09-26):** the combiner was refactored into a
pure consumer — it never looks at stock prices and does no calculations
of its own (no bars are read, no levels are derived, no breaks are
detected here). It reads the recommendations the upstream skills already
published in their state files and applies the user's conditions to the
freshly **confirmed** breaks. The conditions themselves are unchanged,
so the version stays 1.0.

## Pipeline position

Two schedules, both weekdays, Pacific time:

**Intraday sweeps (the trading day):** 8 runs, hourly 6:30 AM–1:30 PM —
one combined sequential sweep (`bin/intraday_sweep.py`, crons
`intraday-sweep-0630` … `intraday-sweep-1330`), stages in the user's
mandated order: 30-min fetch → trend (`--intraday`) → fibonacci
(`--intraday`) → vwap (`--intraday`) → this skill (`--mode intraday`).
Signal-only output: the sweep is silent unless a BUY/ADD/SELL fires or a
stage fails.

**Evening pipeline (after the close) — structure refresh only:**

1. 1:35 PM — `get-stock-data` fetches the day's bars into the shared cache
2. 2:00 PM — `trend-detector` re-baselines trends and 9/21 EMAs
   (`daily-trend-scan`)
3. 2:05 PM — `fibonacci` recomputes anchors/levels/ATR, redrawing on a new
   swing high/low (`daily-fib-scan`)
4. 2:10 PM — `vwap` recomputes AVWAP lines/anchors (`daily-vwap-scan`)
5. 2:20 PM — `algo-performance-evaluation` (`daily-perf-eval`)

No signals fire in the evening: breakouts/breakdowns are evaluated on
30-minute bars only, by the 8 intraday sweeps. The `--mode daily` path
in `bin/sbs_scan.py` is kept for manual/ad-hoc checks; nothing is
scheduled on it.

It is deliberately the last step before performance evaluation: every
condition it evaluates is built from information the earlier skills
produced that same day.

## Modes

- `--mode intraday` (the hourly sweeps): consumes the 2×30-min-bar
  confirmations (`basis="30min"`); the fib structure must be as of
  the latest completed daily bar (e.g. Friday for a Monday run). The
  consumed-confirmation keys include the basis, so a re-run can never
  double-fire.
- `--mode daily` (manual use only, not scheduled): consumes the daily
  2-day-rule confirmations (`basis="daily"`); the fib structure must
  be as of today.

## What it combines

The scan (`bin/sbs_scan.py`) reads the upstream skills' **published
outputs** — their state files — through small **adapter** functions, one
per skill. It performs no analysis itself:

- `adapter_trend()` (trend-detector) — reads `state.json`: the published
  9/21-EMA trend of each stock's **sector index** (the uptrend gate for
  buys) and of the stock itself (context in the report). A stale or
  missing reading comes back `"n/a"`, which fails the buy gate closed.
- `adapter_fib_breaks()` (fibonacci) — reads `data/fib_state.json`: the
  fib breaks the scan **confirmed** that day (2-day rule already applied),
  with level, decisiveness and volume as the scan reported them.

(The vwap skill still runs in the sweeps and reports to the Technical
Analysis - VWAP chat, but since v3.0 the algorithm no longer consumes
AVWAP crosses.)

A ticker is evaluated only when the fib output carries the expected
as-of day; a stale or missing scan contributes nothing (never a guess).
Every confirmation seen is remembered in the state file, so re-running
the scan on the same day can never double-fire a signal.

## The conditions (v3.0, 2026-09-29)

**BUY #1:** the stock's sector index is in an **uptrend**, AND the stock
itself is in an **uptrend** (intraday reading), AND a **confirmed break
above any fib level** 23.6%–161.8%. (AVWAP crosses no longer trigger
entries.) The sector index comes from `data/index_map.json` (semis
→ SMH, software → IGV, broad tech → QQQ, everything else → SPY by
default; crypto → BTC-USD; the mapping is a judgment call and is meant
to be edited).

**SELL #1:** a recommended BUY is currently open for the stock, AND
either:

- **two fib levels have been lost since the last entry** — a level is
  lost on a confirmed break **below** it and restored on a confirmed
  break back **above** it (tracked per ticker in the state file; anchor
  redraws reset it, and every BUY and every ADD wipes the slate, so
  only post-entry losses count).
  The first lost level is surfaced as a **SELL WATCH** (informational
  only, never a ledger row); the second lost level exits the position;
  or
- the stock's intraday trend **flips to downtrend** (backstop for slow
  bleeds that never break a second level); or
- the stock's **sector index is in downtrend** (sector-wide exit: index
  selling is arbitrary, so all bets are off — exits the entire
  position; index *sideways* does not exit, and a missing/stale index
  trend fails closed).

**ADD:** a recommended BUY is already open for the stock, AND the **BUY
#1** condition fires again (index and stock still in uptrend + confirmed
break above). The recommendation is to **add to the position** — no
second independent position is opened; the new entry is recorded as
another leg on the same position. v3.1: the ADD is **pyramid-up only**
— it fires only when the break is above a fib level **higher** than the
highest level already bought on the position (tracked per position in
the state file; legacy positions are backfilled once from their entry
trigger strings). Re-crossing the same or a lower level on a later day
— oscillation around one line — adds nothing.

**Exit = everything:** a SELL closes the **entire** position at once —
the initial BUY leg plus every ADD leg. P&L is reported against the
blended (equal-weighted average) entry price of all legs.

Notes on the logic:

- "Confirmed" means the scan's own rule for the run's basis: 2x30-min
  bars intraday (`basis="30min"`), 2-day rule in the evening
  (`basis="daily"`). The combiner never acts on a first-bar/first-day
  cross.
- Anchor redraws reset a ticker's lost-level tracking: the levels are
  different prices now, so old losses no longer count.
- So does every entry: a BUY or an ADD wipes the ticker's lost levels —
  the exit needs two fib levels lost *since the last entry*. (An ADD
  therefore ratchets the stop: add at 50%, and losing 50% + 38.2% exits.)
- Implementation of the wipe (fixed 2026-09-30, still v3.1 — the
  conditions didn't change, only the bookkeeping): the state file keeps
  a per-ticker `failed_since` stamp. Every BUY/ADD sets it to its own
  trigger's confirmation time, every anchor redraw sets it to the run
  time, and the per-run rebuild of lost levels folds in only
  confirmations stamped *after* it. Before this fix the code folded the
  whole published log every run, so pre-entry failures leaked back in
  one run after each wipe/redraw — a stale failure plus the first
  genuine post-entry failure could fire an immediate two-level SELL.
  Positions opened before the fix were backfilled once from their entry
  trigger strings (latest of BUY/ADD triggers).
- SELL takes precedence: with an open position, the sell conditions are
  checked first; otherwise the buy-side condition produces an ADD rather
  than a second BUY.
- Signals fire once, on the confirmation day. The consumed-confirmation
  set in the state file guarantees a re-run never repeats a signal; the
  key includes the break direction, so a down-break and a later same-day
  up-reclaim of one level are independent confirmations.

## Output ledger

`output/sbs_recommendations.jsonl` — an **append-only ledger** of every
recommendation the algo ever makes: one JSON object per line with
`algo_version`, `date`, `ticker`, `action` (`BUY` / `ADD` / `SELL`),
`price` at the time, sector-index context, trigger detail, and (for
SELLs) entry/add prices, blended entry and P&L. Full field documentation
in `output/SCHEMA.md`. Any independent skill can read this file to
analyze the algo's performance without knowing the internal state format.
The ledger is never edited — rows from older versions stay as-is. The
six inaugural BUYs of 2026-09-25 were backfilled on 2026-09-26 (flagged
with `note`) because the ledger did not exist when they fired.

## State

`data/sbs_state.json` — the open recommended-BUY positions (`version`,
`date`, `price`, `index`, `trigger`, `adds` legs), a capped history of
closed signals with P&L, the set of confirmations already consumed
(re-run safety), the per-ticker lost fib levels (`failed_levels`: level
id → the break that lost it; a confirmed up-break reclaims), the anchor
signature each ticker's losses were drawn from (`level_anchors`;
redraws reset losses), and the last seen intraday stock trend per
ticker (`trend_watch`, for the downtrend-flip exit). Carries
`algo_version`. Legacy `vwap:*` keys in `failed_levels` are purged on
read (v3.0 no longer tracks AVWAP). A v1.0 state file (per-ticker
anchors/sides) is migrated on load: positions carry over, the rest is
dropped.
`data/index_map.json` — ticker → sector index.

## Usage

```bash
# Daily scheduled scan (posts to the "Technical Analysis - Swing Buy Sell Signal" chat)
python3 bin/sbs_scan.py

# Machine-readable output
python3 bin/sbs_scan.py --json

# Trial run without touching state
python3 bin/sbs_scan.py --dry-run
```

Read-only against the upstream skills' published outputs — this skill
reads no price bars at all and never fetches. Reports to the
**"Technical Analysis - Swing Algo 1"** side chat. SCHEDULED: the eight
hourly intraday sweeps 6:30 AM–1:30 PM PT (`intraday-sweep-*`, `--mode
intraday`, signal-only output) — the sole signal source. The sweep
script also prints `[STOCKS-TREND]`, `[STOCKS-FIB]`, and `[STOCKS-VWAP]`
sections whenever that hour's 30-min bars produce intraday trend flips,
fib level breaks, or AVWAP crosses (confirmed or watch; no section when
quiet — no heartbeat for stocks). The sweep worker relays any printed
sections under a `RELAY:` heading, and the agent in this chat fans them
out: `[STOCKS-TREND]` → "Technical Analysis - Trend Detection",
`[STOCKS-FIB]` → "Technical Analysis - Fibonacci", `[STOCKS-VWAP]` →
"Technical Analysis - VWAP" (his 2026-09-28 instruction). The evening
pipeline (1:35/2:00/2:05/2:10 PM) refreshes daily structure only; no
evening sbs run (breakouts/breakdowns are evaluated on 30-min bars
only).

## Adding a new upstream skill

The user expects more skills to be inserted before this one over time.
To wire a new one in:

1. The new skill must **publish its recommendations** in its own state
   file (the way the fib/vwap scans persist their `confirmed` log and
   the trend scan persists its trends) — this skill never recomputes
   another skill's output.
2. Write `adapter_<name>()` in `bin/sbs_scan.py` reading that state file
   and returning the skill's confirmed events for a ticker on the scan
   day (same event-dict shape as the fib/vwap adapters: `kind`, `dir`,
   `decisive`, `dist_pct`, `vol_ratio`, `vol_expansion`, `close`).
3. Extend `evaluate()` with the new condition and document it here.

Rule-based signal analysis, not financial advice.
