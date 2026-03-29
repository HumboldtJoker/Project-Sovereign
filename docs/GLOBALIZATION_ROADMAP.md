# Globalization Roadmap — From Amerocentric to Worldwide

## Current US-Only Dependencies

| Component | US Source | Limitation |
|-----------|----------|------------|
| Macro data | FRED (St. Louis Fed) | US indicators only |
| Exchange | Alpaca | US equities only |
| Political signals | Congressional STOCK Act | US Congress only |
| News sentiment | Yahoo Finance | Already global (no change needed) |
| Technical analysis | yfinance | Already global (no change needed) |
| Currency | USD-denominated | Single currency |

## Tier 1: Free Data Swaps (Zero Cost)

### World Bank API (replaces FRED for international macro)
- **URL**: `api.worldbank.org/v2`
- **No API key needed** — completely free, no auth
- **29,000+ indicators** for 200+ countries
- Key indicators:
  - `NY.GDP.MKTP.KD.ZG` — GDP growth rate
  - `FP.CPI.TOTL.ZG` — Inflation (CPI)
  - `FR.INR.RINR` — Real interest rate
  - `SL.UEM.TOTL.ZS` — Unemployment rate
  - `BN.CAB.XOKA.GD.ZS` — Current account balance
- **Python**: `pip install wbgapi` (official client)
- **Implementation**: Add `analysis/macro_global.py` alongside existing `macro.py`

### ECB Statistical Data Warehouse (Europe)
- **URL**: `sdw-wsrest.ecb.europa.eu`
- **Free, no auth**
- Euro area interest rates, money supply, exchange rates
- Complements World Bank with higher-frequency European data

### IMF Data API
- **URL**: `dataservices.imf.org/REST/SDMX_JSON.svc`
- **Free, no auth**
- World Economic Outlook, Global Financial Stability data
- Cross-country comparable macro indicators

### Bank of Japan API
- **Free, English interface available**
- Japanese macro indicators, BOJ policy rate

## Tier 2: Exchange Access ($0-10/month)

### yfinance (Already Global — No Change Needed)
yfinance already supports international tickers:
- Tokyo: `7203.T` (Toyota)
- London: `SHEL.L` (Shell)
- Frankfurt: `SAP.DE` (SAP)
- Hong Kong: `0700.HK` (Tencent)
- São Paulo: `PETR4.SA` (Petrobras)

Our `analysis/technical.py` works globally with zero changes.

### Trading (Broker Alternatives)

| Broker | Markets | API | Cost | Notes |
|--------|---------|-----|------|-------|
| **Alpaca** (current) | US only | REST | Free | Paper + live |
| **Interactive Brokers** | 150+ markets, 33 countries | REST/FIX | $0-10/mo | Most comprehensive |
| **Trading212** | EU/UK markets | REST | Free | EU-focused |
| **eToro API** | Global | REST | Varies | Social trading angle |
| **iTick** | US, HK, Japan, Singapore, AU | WebSocket | Free tier | Real-time <50ms |
| **Twelve Data** | 80+ exchanges, 50 countries | REST | Free tier (800 req/day) | Best free coverage |

**Recommendation**: Interactive Brokers for serious global trading. Twelve Data for data-only (analysis without execution).

## Tier 3: Political Signal Equivalents

This is the hardest part to globalize — each country has different disclosure laws.

### Available Sources

| Country/Region | Source | Format | API? |
|----------------|--------|--------|------|
| **US** | STOCK Act disclosures | RapidAPI | Yes (current) |
| **EU** | EU Integrity Watch | Web scraping | Partial (integritywatch.eu) |
| **UK** | Register of Members' Interests | Published PDFs | No API — needs scraping |
| **Japan** | Asset disclosure (annual) | PDF filings | No API |
| **Australia** | Register of Interests | Published online | No API |
| **Canada** | Conflict of Interest reports | Published online | No API |

**Honest assessment**: The US is uniquely transparent about congressional trading (thanks to STOCK Act). Most countries publish financial interests but not individual stock trades with dates and amounts. The "congressional confluence" signal that's our edge doesn't have a direct equivalent elsewhere.

**Adaptation strategy**: In non-US markets, replace congressional signals with:
- Insider trading disclosures (most exchanges require these)
- Institutional ownership changes (13F equivalent filings)
- Central bank communication analysis (policy signals)

## Tier 4: Multi-Currency Support

### Implementation
```python
# Already possible via yfinance
import yfinance as yf

# Forex pairs
usd_eur = yf.Ticker("USDEUR=X")
usd_jpy = yf.Ticker("USDJPY=X")

# Currency-adjusted portfolio
# When buying Toyota (7203.T), track JPY/USD exposure
```

### Regime Detection by Region
Each region needs its own macro regime detector:
- **US**: FRED (VIX, yield curve, credit spreads) — current
- **Europe**: ECB rates, Euro Stoxx 50 volatility (V2X), Bund yields
- **Asia**: BOJ rate, Nikkei VI, China PMI
- **Emerging Markets**: Dollar strength index (DXY), commodity prices, sovereign spreads

## Architecture: How It Fits

```
core/config.py
  + REGION = os.getenv("REGION", "US")  # US, EU, UK, JP, CN, GLOBAL

analysis/
  macro.py          → US (FRED) — unchanged
  macro_global.py   → NEW: World Bank + ECB + IMF
  macro_factory.py  → NEW: returns right macro agent per region

execution/
  order_executor.py → Add broker abstraction layer
  brokers/
    alpaca.py       → US (current code, extracted)
    ibkr.py         → NEW: Interactive Brokers
    paper.py        → Local sim (current, unchanged)

integrations/
  political/
    congressional.py  → US (current)
    eu_integrity.py   → NEW: EU Integrity Watch scraper
    uk_register.py    → NEW: UK Parliament register parser
    insider_filings.py → NEW: Generic exchange insider disclosures
```

## Cost Summary

| Tier | What | Cost |
|------|------|------|
| 1 | World Bank + ECB + IMF macro data | **$0** |
| 2a | yfinance international tickers | **$0** (already works) |
| 2b | Twelve Data (80+ exchanges) | **$0** (free tier) |
| 2c | Interactive Brokers (execution) | **$0-10/mo** |
| 3 | Political signal scraping | **$0** (engineering time) |
| 4 | Forex data via yfinance | **$0** |
| **Total** | **Global market intelligence** | **$0-10/mo** |

## Implementation Priority

### Phase 1: Data Layer (1 week)
- [ ] Add `macro_global.py` with World Bank API
- [ ] Test yfinance with international tickers (already works)
- [ ] Add forex overlay to portfolio manager
- [ ] Add region config to `core/config.py`

### Phase 2: Regime Detection (1 week)
- [ ] European regime detector (V2X, Bund yields, ECB rate)
- [ ] Asian regime detector (Nikkei VI, BOJ rate)
- [ ] Emerging markets regime detector (DXY, commodity prices)
- [ ] Regime factory that returns right detector per region

### Phase 3: Political Signals (2 weeks)
- [ ] EU Integrity Watch scraper
- [ ] UK Register of Interests parser
- [ ] Generic insider filing parser (exchange-agnostic)
- [ ] Political signal factory per region

### Phase 4: Broker Abstraction (1 week)
- [ ] Extract Alpaca code into `brokers/alpaca.py`
- [ ] Add IBKR integration for global execution
- [ ] Broker factory based on region config

### Phase 5: Knowledge Graph (1 week)
- [ ] Seed international investor wisdom (not just US-centric)
  - Masayoshi Son, Li Ka-shing, Jim Rogers on Asian markets
  - Sector rotation patterns differ by region
- [ ] International crisis events in KG backfill
- [ ] Currency regime as a dimension in regime history

## The Vision

```bash
# US user (current)
REGION=US python main.py --autonomous

# European user
REGION=EU python main.py --autonomous
# → ECB macro data, Euro Stoxx analysis, EU political signals

# Global multi-market
REGION=GLOBAL python main.py --autonomous
# → Cross-region analysis, forex-aware, best opportunities worldwide
```

Same agent. Same safety system. Same narrator explaining in plain language.
Just different data sources plugged in per region.

**"Bloomberg costs $24K and works globally. We cost $0-10/month and should too."**

---

Sources:
- [World Bank Open Data](https://data.worldbank.org/)
- [World Bank API Documentation](https://datahelpdesk.worldbank.org/knowledgebase/articles/889392-about-the-indicators-api-documentation)
- [EU Integrity Watch](https://www.integritywatch.eu/)
- [UK Register of Members' Interests](https://members.parliament.uk/members/commons/interests/publications)
- [Twelve Data — 80+ Exchanges](https://medium.com/@trading.dude/beyond-yfinance-comparing-the-best-financial-data-apis-for-traders-and-developers-06a3b8bc07e2)
- [iTick Global Market Data](https://signals.coincodecap.com/best-free-stock-market-data-apis)
- [Best API Trading Platforms 2026](https://www.lambdafin.com/articles/best-api-trading-platforms-financial-markets)
- [IMF Data Services](https://www.imf.org/en/data)
