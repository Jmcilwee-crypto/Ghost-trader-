# Ghost Trader AI

A **paper-trading only** research bot. It watches Polymarket's public trade
feed, scores each wallet's historical accuracy on resolved bets, and copies
wallets that have been right / fades wallets that have been wrong — all in a
simulated portfolio that starts at $1,000. Nothing here ever creates a real
order, signs a transaction, or touches a real wallet. It's a science
experiment in whether "follow/fade the whales" is a viable signal on
Polymarket, not a live trading system.

## How it works

- **Data source**: Polymarket's public Gamma API (market metadata), Data API
  (trade history), and CLOB API (order book / price history). No API key or
  wallet needed for any of this — it's all public read access.
- **Signal**: a trade counts as a "whale" bet once its USD notional crosses
  `strategy.whale_usd_threshold` in [config.yaml](config.yaml). We only act on
  binary (two-outcome, Yes/No-style) markets, since "fade" needs a
  well-defined opposite side.
- **Scoring**: [`src/scorecard.py`](src/scorecard.py) tracks each wallet's
  size-weighted win rate across markets that have *already resolved*. A
  wallet's next big bet is only acted on once it has at least
  `strategy.min_track_record` resolved bets behind it.
- **Decision**: [`src/strategy/whale_follow.py`](src/strategy/whale_follow.py)
  copies wallets with accuracy above `copy_above_accuracy`, fades wallets
  below `fade_below_accuracy`, and ignores everyone in between (no edge).
- **Sizing**: [`src/risk.py`](src/risk.py) caps any single bet at
  `risk.max_pct_per_trade` of current equity, scaled down further by how
  confident the signal is, and caps total exposure per market and per number
  of concurrent open positions.
- **Bookkeeping**: [`src/portfolio.py`](src/portfolio.py) is a plain paper
  ledger — cash, open positions, realized P&L, an equity curve, and
  win/loss stats. Positions settle at $1/share on a win and $0/share on a
  loss, same as real Polymarket outcome tokens.

## Setup

```bash
cd polymarket-ghost-trader
python3 -m pip install -r requirements.txt
```

First, confirm you can actually reach Polymarket's APIs from wherever you're
running this (see **Network note** below):

```bash
python3 -m src.polymarket_client selftest
```

## Backtest first

Replays the strategy over recently-closed markets (pulls each market's full
trade history, resolves wallets' track records in true chronological order
so there's no lookahead bias) and reports what would have happened:

```bash
python3 -m src.backtest
```

Output goes to `data/backtest_report.json` (final equity, return %, win
rate, max drawdown, whether the $10k target was hit) and
`data/backtest_equity_curve.csv`. This is a rough backtest — no fee/slippage
modeling, partial market/trade coverage due to API pagination — treat it as
a plausibility check on the strategy, not a guarantee.

Tune `strategy.*` and `risk.*` in [config.yaml](config.yaml) and re-run until
the backtest looks reasonable before touching live mode.

## Then go live (still paper-only)

```bash
python3 -m src.live
```

This polls the live trade feed every `live.poll_interval_seconds`, applies
the same strategy, and simulates fills. It persists state to
`data/live_state.json` after every cycle (safe to Ctrl-C and resume) and logs
every decision to `data/live_log.jsonl`. It stops itself automatically once
equity hits `bankroll.target_usd` ($10,000) or falls to `bankroll.ruin_usd`.

Run it somewhere long-lived (`tmux`, `nohup python3 -m src.live &`, etc.) if
you want it to keep monitoring unattended.

## Value betting from outside research (optional)

The whale bots ask "is this trader usually right?". The **value** bots ask a
more direct question: "is this priced wrong?" A share costs `q` and pays $1,
so the market is implicitly claiming the probability is `q`. If sportsbooks
say it's really `p`, the gap `p - q` is expected profit.

Bookmaker lines are used as the reference because they're about the best
calibrated public probabilities that exist — thousands of events priced with
real money and corrected instantly when wrong. Two details matter:

- **Vig is removed.** Book odds deliberately sum past 100%; that overround is
  their margin. Raw implied probabilities are all biased high, so they're
  normalised back to 1 before use.
- **Stakes use fractional Kelly.** For a $1-payout contract Kelly reduces to
  `(p - q) / (1 - q)`. Full Kelly assumes `p` is exactly right — it's an
  estimate, not truth — so only a fraction is staked, then the normal risk
  limits apply on top.

To switch it on, get a free key (500 requests/month) from
[the-odds-api.com](https://the-odds-api.com) and export it:

```bash
export ODDS_API_KEY=your_key_here
python3 -m src.experiment
```

The key is read from the environment, never from `config.yaml`, so it can't
be committed by accident. Without a key the bot logs that research is off and
runs the 20 whale bots exactly as before — nothing breaks.

With it enabled you get 4 extra bots (`value-edge5/10/15` and a bolder-staking
variant) racing the whale bots on the same data, so the experiment answers
whether research actually beats copying traders. One lookup is shared across
every bot per trade, so adding them doesn't multiply API calls. Only
head-to-head markets are priced — totals, spreads and halftime lines are
skipped, because who-wins odds can't answer those questions.

## Spot experiments (crypto, commodities, forex, stocks)

The same side-by-side idea applied to continuously-priced assets. One engine
drives every asset class — once prices arrive as bars, a momentum bot doesn't
care whether it's looking at Bitcoin, gold or the euro:

```bash
python3 -m src.spot_experiment --market crypto        # BTC ETH SOL BNB XRP
python3 -m src.spot_experiment --market commodities   # tokenised gold
python3 -m src.spot_experiment --market forex         # EUR GBP JPY AUD CHF
python3 -m src.spot_experiment --market stocks        # needs a key, see below
```

Each class keeps its own isolated state, history and archive, so they can run
simultaneously without touching each other. Add a new one by adding a block
under `spot.classes` in [config.yaml](config.yaml) — it appears in the
dashboard automatically.

| Class | Source | Key needed? | Notes |
|---|---|---|---|
| crypto | Binance → Coinbase fallback | no | 24/7, hourly bars |
| commodities | Binance (PAXG/XAUT) | no | tokenised gold, tracks spot |
| forex | Frankfurter (ECB rates) | no | **daily closes only**, weekdays |
| stocks | Alpha Vantage | optional | runs on the shared demo key; a free key unlocks all tickers |

**Stocks work without a key, but only just.** Yahoo Finance returns HTTP 429
to programmatic clients and Stooq serves a JavaScript bot-challenge, so there
is no good keyless equity feed. Alpha Vantage's public `demo` key is the
exception — it serves **IBM and MSFT** (and rejects everything else), which is
enough to run the experiment out of the box. Note the demo key only answers
their exact documented query, so `outputsize` is only sent when a real key is
present.

For any other ticker, get a free key at
[alphavantage.co](https://www.alphavantage.co/support/#api-key) and add it to
`.env` next to the odds key:

```
ALPHAVANTAGE_API_KEY=your_key_here
```

Then widen `spot.classes.stocks.symbols` in [config.yaml](config.yaml) to
whatever you want.

**The free tier is tight: 1 request per minute and ~25 per day.** Two things
keep the bot inside it:

- **One symbol refreshed per cycle.** Requests are spaced at least 65s apart;
  any symbol asked for sooner keeps serving cache rather than spending a call
  that would come back throttled. Five tickers warm up over ~25 minutes, then
  the 6-hour cache holds steady-state usage near 20 calls/day.
- **Throttles and bad tickers are told apart.** Alpha Vantage answers both
  with HTTP 200 and a prose message. A throttle is temporary and the symbol
  stays retryable; an invalid ticker or demo-key rejection disables that
  symbol so it stops burning quota. Getting this backwards would silently drop
  a stock for the rest of the run, so it's covered by tests.

**A caveat on forex:** ECB publishes one reference rate per weekday, so there
are no intraday bars and open/high/low are filled with the close. Only
indicators that read closing prices mean anything there.

11 bots, each starting at $1,000, all trading the same live candles:

| Family | Idea |
|---|---|
| `momentum-5/20`, `10/50`, `20/100` | buy strength: fast average crosses above slow |
| `meanrev-25/30/35` | buy weakness: RSI says oversold |
| `breakout-20/50` | buy new highs above the recent range |
| `vol-momentum` | momentum, sized down on volatile assets |
| `momentum-tp` | momentum with a take-profit |
| `buy-and-hold` | **the control** — every other bot has to beat simply owning it |

Momentum and mean reversion are deliberate opposites, so in any given market
one of them is wrong — that contrast is the point.

**Why this needed a separate engine.** Polymarket positions are binary
contracts: bought between 0 and 1, settling at exactly $1 or $0. Crypto has no
settlement — a position ends only when a strategy *sells* it. So spot bots
mark open positions to the live price every cycle (unrealized P&L is real),
and pay a fee on both entry and exit. At 0.1% each way a round trip costs
0.2%, which is frequently the entire edge of a fast strategy — you can watch
the mean-reversion bots sit slightly below $1,000 on fee drag alone before
anything has resolved.

Data comes from Binance with Coinbase as an automatic fallback (Binance blocks
some regions). No API key needed for either.

## Running it without leaving a machine on (GitHub Actions)

No card, no server, free. `.github/workflows/trade.yml` wakes roughly every 15
minutes, runs **one cycle** of each experiment, and commits the updated state
back to the repo — so the repo itself is the storage, and every cycle is
versioned automatically.

```bash
python3 -m src.spot_experiment --market crypto --once   # what a scheduled run does
python3 -m src.experiment --once
```

Setup:

1. Push this project to a GitHub repo.
2. Settings → Secrets and variables → Actions → add `ODDS_API_KEY` and
   `ALPHAVANTAGE_API_KEY`. **Do not commit `.env`.**
3. Settings → Actions → General → Workflow permissions → **Read and write**
   (the run has to commit state back).
4. Actions tab → `ghost-trader` → *Run workflow* to test it immediately rather
   than waiting for the schedule.

Things worth knowing before relying on it:

- **The cadence is approximate.** Five minutes is GitHub's floor and scheduled
  runs are often delayed under load, so treat it as "roughly every 15 minutes".
- **Polymarket still loses trades.** It reads a live feed, so whale trades
  between runs are missed permanently. The four spot experiments re-fetch
  history each cycle and lose nothing — they suit this model far better.
- **A public repo makes your results public.** That's fine for paper trading,
  but the API keys must live in Actions secrets, never in the repo.
- **Scheduled workflows switch off after 60 days of repo inactivity.** These
  runs commit state, which counts as activity and keeps them alive.
- `--once` deliberately skips the per-run state archive, since git history
  already versions every cycle; otherwise you'd get a new archive file every
  15 minutes.

**The strongest setup if you have a spare machine:** run Polymarket on it (a
continuous live feed on a residential IP — which datacenters often can't get
anyway) and let Actions handle the four spot experiments.

## Running it 24/7 on a server

A laptop that sleeps loses data. The four spot experiments survive that fine —
they re-fetch 150 bars of history every cycle, so an outage only delays their
decisions. **Polymarket does not**: it reads a live trade feed, so whale trades
that happen while the machine is off are gone permanently.

```bash
# on the server, AFTER cloning/copying the project:
python3 deploy/preflight.py     # do this FIRST
bash deploy/install.sh
```

**Run the preflight before anything else.** Polymarket and Binance routinely
refuse datacenter IP ranges even though they work from a home connection, so a
cloud box can hit exactly the same wall as a filtered campus network. The
preflight checks every data source and tells you which experiments would
actually work from that machine, before you spend time deploying.

`install.sh` creates a virtualenv and installs six systemd services — one per
experiment plus the dashboard. systemd rather than `nohup`/`tmux` because it
restarts a crashed bot by itself and brings everything back after a reboot,
which is the whole reason for leaving the laptop behind.

```bash
systemctl status 'ghost-*' --no-pager      # all six at a glance
journalctl -u ghost-polymarket -f          # follow one
sudo systemctl stop ghost-crypto           # stop one
```

The dashboard stays bound to localhost. Reach it over an SSH tunnel rather
than opening a port:

```bash
ssh -N -L 8765:localhost:8765 user@<server-ip>
```

then open `http://localhost:8765` as usual. Remember to copy `.env` across —
it is gitignored, so it will not come with a `git clone`.

## The dashboard

```bash
python3 -m src.dashboard        # then open http://localhost:8765
```

Note the `http://` — the server is plain HTTP, so browsers that silently
upgrade to `https://` will fail to connect.

Switch between the **Polymarket** and **Crypto** experiments with the buttons
next to the title — each keeps its own selection, sort and filters, and the
columns change to suit the instrument (spot shows unrealized P&L and fees
where prediction markets show minimum bet size and average entry).

**Light / dark** toggle sits next to the market buttons. It follows your OS
preference on first load, then remembers whatever you pick. Light mode keeps
the same hairline structure but swaps the neon accents for deep ink-green and
ink-red — neon on white is unreadable — and drops the glows, which read as
blur on a light field.

**Keyboard:** `↑`/`↓` walk the bot roster, `↵` opens the selected bot's detail,
`esc` clears the filter (or backs out to the leaderboard), `1`–`5` jump between
markets. Typing in the search box is left alone.

**The backdrop** is a hand-authored cybersigilism sigil — chrome-gradient
thorn forms — drawn as inline SVG rather than bitmaps, so it scales to any
display, adds no weight, needs no network, and recolours itself for light
mode. It sits behind everything with `pointer-events: none` so it can never
cost a click, and panel surfaces are only slightly translucent so the artwork
reads without ever taking contrast from the data. It holds still for anyone
whose OS asks for reduced motion.

It's interactive:

- **Sort** by any column (click a header; click again to reverse)
- **Filter** by name with the search box, or by style with the chips
- **Click any bot** to open a detail drawer with its full stats, the exact
  settings that make it different, an equity chart, and tabs for its active
  bets / finished bets / a breakdown by why each bet was opened
- **Sparklines** on every row show that bot's equity trend at a glance
- Charts are hand-rolled inline SVG, so the page works with no internet

## Statistics across all variants

```bash
python3 -m src.stats                # current run
python3 -m src.stats --all-runs     # current run + every archived past run
python3 -m src.stats --json         # machine-readable, for your own analysis
```

Per variant: return, win/loss record, average win vs average loss, profit
factor, expectancy per dollar staked, average entry price, max drawdown, and
a breakdown of finished bets by why they were opened (copy / fade / test bet).

It also groups results by each tunable axis (strategy style, minimum bet
size) and reports correlations between those settings and returns — which is
the actual point of running 20 variants. Correlations need *resolved* markets
to mean anything; until bets finish they'll read `n/a`.

### Nothing is ever thrown away

- On startup the experiment snapshots the previous run into `data/archive/`
  before writing anything, so a strategy change can always be compared
  against what it replaced.
- Every cycle appends each variant's equity to `data/experiment_history.jsonl`,
  so you get a time series rather than only the latest snapshot.
- `--all-runs` reads every archived run, and `=== same variant across runs ===`
  lines up each variant's result run-over-run.

## Monitoring it

**Dashboard (recommended)** — a visual, browser-based view:

```bash
python3 -m src.dashboard
```

Then open **http://127.0.0.1:8765** in a browser. It shows:
- **Total wallet value**, cash on hand, profit/loss, and a progress bar toward
  the $10k goal.
- **Active trades** — what it's holding right now and why.
- **Potential trades flagged** — every whale-sized trade the bot has looked
  at recently, whether it acted or not, each with an **Importance** rating
  (how big/notable the trade is) and a **Possibility** rating (how likely the
  bot thinks the bet pays off, based on that trader's track record), plus a
  plain-English reason.
- **Profit & loss history** — every finished bet, won or lost, with the
  dollar result.

It auto-refreshes every few seconds (`dashboard.refresh_seconds` in
config.yaml) and is entirely local and read-only — nothing leaves your
machine, and it's safe to leave open in a tab while `python3 -m src.live`
keeps running in a terminal.

**Other ways to check in:**

- **Foreground terminal**: just watch `python3 -m src.live` — it prints
  equity/cash/open positions every poll cycle, plus a line for every trade
  opened or resolved.
- **Plain-English CLI summary**: `python3 -m src.status` (add `--technical`
  for the dense numbers-first version).
- **Raw log**: `tail -f data/live_log.jsonl` for opened/resolved trades, or
  `tail -f data/flagged_log.jsonl` for every whale trade considered
  (this is what feeds the dashboard's "flagged" panel).
- **Raw state**: `data/live_state.json` is the full portfolio snapshot,
  rewritten every cycle — useful if you want to script something against it.

## Network note

`*.polymarket.com` was unreachable from the sandboxed environment this was
built in (TLS connections were reset/timed out even though other sites
worked fine) — so the API client and both runners are written directly
against Polymarket's documented endpoint shapes but haven't been exercised
against live traffic. Everything that *doesn't* need the network (portfolio
math, scorecard accuracy weighting, strategy decisions, risk sizing) is
covered by `tests/` and passes. Run the `selftest` command above first on
your own machine — if field names have drifted from what's implemented in
[`src/polymarket_client.py`](src/polymarket_client.py) or
[`src/market_utils.py`](src/market_utils.py), that's the first place to look.

## Project layout

```
src/
  polymarket_client.py   Gamma/Data/CLOB API wrappers
  market_utils.py        parsing helpers for Gamma's JSON-encoded-string fields
  portfolio.py           paper trading ledger
  scorecard.py           per-wallet track record, no-lookahead-bias design
  risk.py                position sizing / exposure limits
  strategy/
    base.py              TradeEvent / Signal / Evaluation data types
    whale_follow.py       copy/fade decision logic (evaluates every whale trade, acted or not)
  backtest.py             historical replay + report
  live.py                  live polling loop
  status.py                plain-English (or --technical) CLI snapshot
  dashboard_data.py        turns on-disk state into one dict for the dashboard
  dashboard.py              local web server (stdlib only, no new dependency)
web/
  index.html                dashboard's single-page UI (vanilla HTML/CSS/JS)
tests/                     unit tests for everything network-independent
config.yaml                all tunable knobs
```

## Honest limitations

- Copy/fade-the-whale is one hypothesis among many; nothing here claims it
  has edge until you've actually looked at a backtest report.
- Wash trading, market-making bots, and self-dealing wallets aren't filtered
  out — a "whale" signal can be noise from a market maker rather than an
  informed bettor. Worth adding a wallet-activity-count filter if you see
  this dominating.
- Binary markets only. Multi-outcome markets are skipped entirely.
- The trader scorecard is a size-weighted win rate (Brier-score-flavored),
  not a proper calibration curve — a wallet that makes one huge correct bet
  looks identical to one that makes many smaller correct bets.
- No fees/slippage modeled in the backtest; real fills would be worse than
  the simulated ones.
