# Sovereign v2 — the pipeline that actually ran

This directory holds the version of Sovereign that has traded a paper account on a cron schedule since July 2026. The top of this repository is the original PL Genesis hackathon build (a ReAct agent with ERC-8004 identity, Storacha logs and Lit-gated signals). v2 is a different design, built afterwards and run for real.

**Status: paper trading only, and no demonstrated edge.** Read [Results](#results) before you point it at money.

## How it works

```
congress (06:00)   pull House/Senate STOCK Act filings, detect "herds" (2+ members, same ticker, same side)
scan (hourly)      score ~30 tickers on a 7-part composite, gate by sector, ask an LLM to write or veto a thesis
execute (06:50, 10:50)   buy high/medium composite names whose thesis says buy, sized by the risk engine
manage (every 30 min)    enforce synthetic stops and targets; reconcile against the broker
```

- **Composite** (`signal_aggregator.py`): congress 0.30, momentum 0.20, sector 0.15, options flow 0.12, sentiment 0.10, correlation break 0.08, earnings 0.05. Congressional signals decay from full weight at 30 days after filing to zero at 60.
- **Thesis** (`sovereign_pipeline.py`): the LLM writes a thesis for the top candidates or vetoes them. Backends: the Anthropic API with your key, or the Claude CLI with your own login (`SOVEREIGN_THESIS_BACKEND=auto|sdk|cli`). If no LLM is reachable, the pipeline holds instead of trading.
- **Risk** (`risk_engine.py`): 2% of equity at risk per trade, a stop at least 8% below entry (or 2x ATR if wider), six positions at most, a 50% sector cap, a correlation penalty, a 10% cash reserve, and a daily-loss circuit breaker.
- **Execution** (`sovereign_execute.py`): orders carry client order IDs, so a retried request can't fill twice. A position leaves the books only when the broker confirms the fill. A missing position is confirmed with the broker before it counts as sold. Failures alert and fail safe.

## Results

These are measured on the author's paper account, July to September 2026, and re-derived by independent reviewers.

- Sovereign lost **4.70%** from 1 July to 24 September while about 22% invested. QQQ held at the same daily exposure lost 0.75%.
- The names the composite rated highest **underperformed** the rest of the same day by 8.4% over the next 10 trading days. The composite mostly followed the previous week's winners. This comes from a single stretch of the sample, so treat it as one episode, not a law.
- Congressional buying was the only input with a positive relationship to later returns, and it was weak.
- The LLM veto had no measurable effect either way.
- The weekly congress backtest (`sovereign_backtest.py`) shows about +3.9% per trade over SPY, but only +1.5% over each stock's sector ETF, which is not statistically significant. It also tests the congress signal alone, not the full pipeline. Confirming an edge of that size needs 80 to 260 trades.

## Run it

```bash
cp .envrc.example .envrc        # your Alpaca paper keys; paper is the default
pip install alpaca-py anthropic psycopg2-binary requests pdfplumber
python3 sovereign_pipeline.py scan          # score and write theses; places no orders
python3 sovereign_execute.py status         # read-only view of positions vs tracked stops
python3 -m pytest tests/ -q                 # order-path tests against a fake broker
```

`sovereign_cron.sh` shows the full schedule. Live trading needs `ALPACA_PAPER=false` **and** `SOVEREIGN_LIVE_CONFIRM=yes`. Don't set either until your own evaluation says the strategy works.

## What we learned building it

- **A backtest that tests a different strategy will look great forever.** Ours re-ran the congress signal alone every week and showed a healthy edge over SPY, while the live composite lost money.
- **Retries are orders too.** HTTP clients resend on timeouts; a buy without a client order ID can fill twice.
- **An exit isn't done when the order is accepted.** Stocks halt, and orders don't fill.
- **Anchor to what you paid.** Entry prices taken from the opening ask sat above the real fill, which quietly tightened every stop.
- **Tests written by the author agree with the author.** The first fake broker in this repo matched the code, not Alpaca. The current tests were written independently against the real API's behaviour.

Built by CC (Coalition Code) with Thomas, Liberation Labs. MIT licensed.
