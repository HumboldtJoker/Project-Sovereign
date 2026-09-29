"""Congressional Trading Scraper — STOCK Act disclosure ingestion.

Pulls periodic transaction reports (PTRs) from:
- House Clerk financial disclosure ZIP (official, daily rebuild)
- Senate eFD search (CSRF + DataTables scrape)

Extracts transactions via regex (structured fields) + Claude (fallback for messy PDFs).
Detects herd signals (multiple members, same ticker, same direction, within N days).

Feeds signals into Sovereign pipeline thesis generation.

Usage:
    python3 congress_scraper.py pull          # Pull latest filings
    python3 congress_scraper.py herd          # Detect herd signals
    python3 congress_scraper.py signal NVDA   # Check congressional signal for ticker
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("congress")

# Key comes from the environment only (see sovereign_config) — no fallbacks.

DATA_DIR = Path(__file__).parent / "congress_data"
CACHE_DIR = DATA_DIR / "cache"
DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

TRANSACTIONS_FILE = DATA_DIR / "transactions.json"
HERD_FILE = DATA_DIR / "herd_signals.json"

AMOUNT_RANGES = {
    "$1,001 - $15,000": (1001, 15000),
    "$15,001 - $50,000": (15001, 50000),
    "$50,001 - $100,000": (50001, 100000),
    "$100,001 - $250,000": (100001, 250000),
    "$250,001 - $500,000": (250001, 500000),
    "$500,001 - $1,000,000": (500001, 1000000),
    "$1,000,001 - $5,000,000": (1000001, 5000000),
    "$5,000,001 - $25,000,000": (5000001, 25000000),
    "$25,000,001 - $50,000,000": (25000001, 50000000),
    "Over $50,000,000": (50000001, 100000000),
}


@dataclass
class Transaction:
    member: str
    chamber: str  # "house" | "senate"
    state: str
    ticker: str
    asset_name: str
    direction: str  # "buy" | "sell"
    amount_range: str
    amount_low: int = 0
    amount_high: int = 0
    transaction_date: str = ""
    filing_date: str = ""
    doc_id: str = ""
    owner: str = ""  # SP=spouse, JT=joint, DC=dependent child


@dataclass
class HerdSignal:
    ticker: str
    direction: str
    members: list = field(default_factory=list)
    total_low: int = 0
    total_high: int = 0
    date_range: str = ""
    conviction: str = "low"  # low/medium/high based on member count + amount
    latest_filing: str = ""  # ISO date the newest leg became public (decay clock)


# ---------------------------------------------------------------------------
# House Clerk Scraper
# ---------------------------------------------------------------------------

class HouseClerkScraper:
    """Download and parse House financial disclosures from the Clerk's office."""

    BASE_URL = "https://disclosures-clerk.house.gov"
    ZIP_URL = "{base}/public_disc/financial-pdfs/{year}FD.zip"
    PTR_PDF_URL = "{base}/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "CongressTracker/1.0 (civic research)"

    def pull_index(self, year: int = None) -> list[dict]:
        """Download the annual ZIP and parse the XML index for PTR filings."""
        year = year or datetime.now().year
        url = self.ZIP_URL.format(base=self.BASE_URL, year=year)

        log.info("Downloading House Clerk ZIP for %d...", year)
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()

        filings = []
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_name = f"{year}FD.xml"
            if xml_name not in zf.namelist():
                log.error("XML not found in ZIP")
                return []

            tree = ET.parse(zf.open(xml_name))
            root = tree.getroot()

            for member in root:
                filing_type = member.findtext("FilingType", "")
                if filing_type != "P":
                    continue

                prefix = member.findtext("Prefix", "").strip()
                first = member.findtext("First", "").strip()
                last = member.findtext("Last", "").strip()
                suffix = member.findtext("Suffix", "").strip()

                name_parts = [p for p in [prefix, first, last, suffix] if p]
                name = " ".join(name_parts)

                filings.append({
                    "name": name,
                    "first": first,
                    "last": last,
                    "state_district": member.findtext("StateDst", ""),
                    "filing_date": member.findtext("FilingDate", ""),
                    "doc_id": member.findtext("DocID", ""),
                    "year": year,
                })

        log.info("Found %d House PTR filings for %d", len(filings), year)
        return filings

    def download_ptr(self, doc_id: str, year: int = None) -> str:
        """Download a PTR PDF and extract text."""
        year = year or datetime.now().year
        cache_path = CACHE_DIR / f"house_{doc_id}.txt"

        if cache_path.exists():
            return cache_path.read_text()

        url = self.PTR_PDF_URL.format(base=self.BASE_URL, year=year, doc_id=doc_id)
        log.info("Downloading PTR %s...", doc_id)

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            log.warning("Failed to download PTR %s: %s", doc_id, e)
            return ""

        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                text = "\n".join(
                    page.extract_text() or "" for page in pdf.pages
                )
        except Exception as e:
            log.warning("PDF extraction failed for %s: %s", doc_id, e)
            return ""

        cache_path.write_text(text)
        return text


# ---------------------------------------------------------------------------
# Senate eFD Scraper
# ---------------------------------------------------------------------------

class SenateEFDScraper:
    """Scrape Senate periodic transaction reports from efdsearch.senate.gov."""

    SEARCH_URL = "https://efdsearch.senate.gov/search/"
    HOME_URL = "https://efdsearch.senate.gov/search/home/"
    REPORT_URL = "https://efdsearch.senate.gov/search/report/data/"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "CongressTracker/1.0 (civic research)"

    def _init_session(self):
        """Get CSRF token and accept the agreement."""
        resp = self.session.get(self.HOME_URL, timeout=15)
        csrf = self.session.cookies.get("csrftoken", "")
        if not csrf:
            match = re.search(r'csrfmiddlewaretoken.*?value="([^"]+)"', resp.text)
            if match:
                csrf = match.group(1)

        self.session.post(
            self.SEARCH_URL,
            data={
                "csrfmiddlewaretoken": csrf,
                "prohibition_agreement": "1",
            },
            headers={"Referer": self.HOME_URL},
            timeout=15,
        )
        return csrf

    def pull_recent(self, days: int = 30) -> list[dict]:
        """Pull recent Senate PTR filings."""
        csrf = self._init_session()

        since = (datetime.now() - timedelta(days=days)).strftime("%m/%d/%Y")

        resp = self.session.post(
            self.REPORT_URL,
            data={
                "start": "0",
                "length": "100",
                "report_types": "[11]",
                "filer_types": "[]",
                "submitted_start_date": since,
                "submitted_end_date": "",
                "candidate_state": "",
                "senator_state": "",
                "office_id": "",
                "first_name": "",
                "last_name": "",
                "csrfmiddlewaretoken": csrf,
            },
            headers={
                "Referer": self.SEARCH_URL,
                "X-CSRFToken": csrf,
            },
            timeout=30,
        )

        try:
            data = resp.json()
        except Exception as e:
            log.error("Senate eFD response parse failed: %s", e)
            return []

        filings = []
        for row in data.get("data", []):
            if len(row) < 5:
                continue
            name_html = row[0]
            name_match = re.search(r">([^<]+)<", name_html)
            name = name_match.group(1).strip() if name_match else row[0]

            link_match = re.search(r'href="([^"]+)"', row[3])
            link = link_match.group(1) if link_match else ""

            filings.append({
                "name": name,
                "office": row[1] if len(row) > 1 else "",
                "filing_date": row[4] if len(row) > 4 else "",
                "link": link,
            })

        log.info("Found %d Senate PTR filings in last %d days", len(filings), days)
        return filings

    def download_ptr(self, filing: dict) -> str:
        """Download a Senate PTR PDF and extract text."""
        link = filing.get("link", "")
        if not link:
            return ""

        if link.startswith("/"):
            link = f"https://efdsearch.senate.gov{link}"

        cache_key = re.sub(r'[^\w]', '_', link[-40:])
        cache_path = CACHE_DIR / f"senate_{cache_key}.txt"

        if cache_path.exists():
            return cache_path.read_text()

        try:
            resp = self.session.get(link, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            log.warning("Failed to download Senate PTR: %s", e)
            return ""

        if resp.headers.get("content-type", "").startswith("application/pdf"):
            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                    text = "\n".join(
                        page.extract_text() or "" for page in pdf.pages
                    )
            except Exception as e:
                log.warning("Senate PDF extraction failed: %s", e)
                return ""
        else:
            text = resp.text

        cache_path.write_text(text)
        return text


# ---------------------------------------------------------------------------
# Transaction Extraction (regex + Claude fallback)
# ---------------------------------------------------------------------------

TICKER_RE = re.compile(r'\(([A-Z]{1,5})\)')
DIRECTION_MAP = {"P": "buy", "S": "sell", "S (Full)": "sell", "S (Partial)": "sell"}

TRANSACTION_LINE_RE = re.compile(
    r'(?:SP|JT|DC|CS)?\s+'
    r'(.+?)\s+'
    r'([PS])\s+'
    r'(\d{2}/\d{2}/\d{4})\s+'
    r'(\d{2}/\d{2}/\d{4})\s+'
    r'(\$[\d,]+ ?-\s*\$[\d,]+|\$[\d,]+)',
    re.IGNORECASE
)


def extract_transactions_regex(text: str, member: str, chamber: str,
                                state: str, doc_id: str, filing_date: str) -> list[Transaction]:
    """Extract transactions from PTR text using regex."""
    transactions = []

    tickers_found = TICKER_RE.findall(text)
    if not tickers_found:
        return transactions

    lines = text.split("\n")
    current_owner = ""

    for i, line in enumerate(lines):
        ticker_match = TICKER_RE.search(line)
        if not ticker_match:
            continue

        ticker = ticker_match.group(1)

        owner_match = re.match(r'^(SP|JT|DC|CS)\s', line)
        if owner_match:
            current_owner = owner_match.group(1)

        direction = None
        if re.search(r'\bP\b', line[ticker_match.end():ticker_match.end() + 20]):
            direction = "buy"
        elif re.search(r'\bS\b', line[ticker_match.end():ticker_match.end() + 20]):
            direction = "sell"

        if not direction:
            context = line + (lines[i + 1] if i + 1 < len(lines) else "")
            if " S " in context or "\tS\t" in context or " S\n" in context:
                direction = "sell"
            elif " P " in context or "\tP\t" in context or " P\n" in context:
                direction = "buy"

        if not direction:
            continue

        amount_match = re.search(r'\$[\d,]+\s*-\s*\$[\d,]+', line + " " + (lines[i + 1] if i + 1 < len(lines) else ""))
        amount_range = amount_match.group(0) if amount_match else "unknown"

        amount_low, amount_high = 0, 0
        for range_str, (low, high) in AMOUNT_RANGES.items():
            if range_str.replace(" ", "") in amount_range.replace(" ", ""):
                amount_low, amount_high = low, high
                break

        date_match = re.search(r'(\d{2}/\d{2}/\d{4})', line[ticker_match.end():])
        tx_date = date_match.group(1) if date_match else ""

        asset_name = line[:ticker_match.start()].strip()
        asset_name = re.sub(r'^(SP|JT|DC|CS)\s+', '', asset_name)

        transactions.append(Transaction(
            member=member,
            chamber=chamber,
            state=state,
            ticker=ticker,
            asset_name=asset_name.strip(),
            direction=direction,
            amount_range=amount_range,
            amount_low=amount_low,
            amount_high=amount_high,
            transaction_date=tx_date,
            filing_date=filing_date,
            doc_id=doc_id,
            owner=current_owner,
        ))

    return transactions


def extract_transactions_claude(text: str, member: str, chamber: str,
                                 state: str, doc_id: str, filing_date: str) -> list[Transaction]:
    """Use Claude to extract transactions from a PTR that regex couldn't parse."""
    import anthropic
    from sovereign_config import EXTRACT_MODEL, anthropic_key

    client = anthropic.Anthropic(api_key=anthropic_key())

    prompt = f"""Extract all stock/security transactions from this congressional financial disclosure.

DOCUMENT TEXT:
{text[:6000]}

Return a JSON array of transactions. Each transaction:
{{
    "ticker": "stock ticker symbol (e.g., AAPL) or empty string if not a stock",
    "asset_name": "full asset name",
    "direction": "buy" or "sell",
    "amount_range": "e.g., $15,001 - $50,000",
    "transaction_date": "MM/DD/YYYY",
    "owner": "SP for spouse, JT for joint, DC for dependent child, empty for member"
}}

Only include transactions with identifiable ticker symbols. Skip bonds, mutual funds
without tickers, and non-equity assets. Return [] if no stock transactions found."""

    try:
        resp = client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text_resp = resp.content[0].text.strip()

        start = text_resp.index("[")
        end = text_resp.rindex("]") + 1
        data = json.loads(text_resp[start:end])
    except Exception as e:
        log.error("Claude extraction failed for %s: %s", doc_id, e)
        return []

    transactions = []
    for item in data:
        ticker = item.get("ticker", "").strip().upper()
        if not ticker or len(ticker) > 5:
            continue

        amount_range = item.get("amount_range", "unknown")
        amount_low, amount_high = 0, 0
        for range_str, (low, high) in AMOUNT_RANGES.items():
            if range_str.replace(" ", "") in amount_range.replace(" ", ""):
                amount_low, amount_high = low, high
                break

        transactions.append(Transaction(
            member=member,
            chamber=chamber,
            state=state,
            ticker=ticker,
            asset_name=item.get("asset_name", ""),
            direction=item.get("direction", "unknown"),
            amount_range=amount_range,
            amount_low=amount_low,
            amount_high=amount_high,
            transaction_date=item.get("transaction_date", ""),
            filing_date=filing_date,
            doc_id=doc_id,
            owner=item.get("owner", ""),
        ))

    return transactions


# ---------------------------------------------------------------------------
# Herd Detection
# ---------------------------------------------------------------------------

def _valid_recent_tx(tx: Transaction, max_age_days: int = 120) -> bool:
    """Filter OCR garbage (future/ancient dates) and stale trades."""
    for raw in (tx.transaction_date, tx.filing_date):
        try:
            d = datetime.strptime(raw, "%m/%d/%Y")
        except (ValueError, TypeError):
            continue
        if d > datetime.now():
            return False  # future-dated = extraction error
        return (datetime.now() - d).days <= max_age_days
    return True  # no parseable date — keep, dated by filing batch


def detect_herds(transactions: list[Transaction], window_days: int = 30,
                 min_members: int = 2, member_weights: dict = None) -> list[HerdSignal]:
    """Find tickers where multiple members trade in the same direction.

    member_weights: optional {member_lower: weight} from member_scoring —
    proven performers count for more than noise traders. weight 1.0 = average.
    """
    member_weights = member_weights or {}
    by_ticker_direction = defaultdict(list)

    for tx in transactions:
        if not tx.ticker or tx.direction not in ("buy", "sell"):
            continue
        if not _valid_recent_tx(tx):
            continue
        key = (tx.ticker, tx.direction)
        by_ticker_direction[key].append(tx)

    signals = []
    for (ticker, direction), txs in by_ticker_direction.items():
        unique_members = {}
        for tx in txs:
            name_key = tx.member.lower().strip()
            if name_key not in unique_members:
                unique_members[name_key] = tx

        if len(unique_members) < min_members:
            continue

        member_txs = list(unique_members.values())
        dates = []
        for tx in member_txs:
            try:
                dates.append(datetime.strptime(tx.transaction_date, "%m/%d/%Y"))
            except (ValueError, TypeError):
                try:
                    dates.append(datetime.strptime(tx.filing_date, "%m/%d/%Y"))
                except (ValueError, TypeError):
                    continue

        if len(dates) >= 2:
            date_span = (max(dates) - min(dates)).days
            if date_span > window_days:
                continue
            date_range = f"{min(dates).strftime('%Y-%m-%d')} to {max(dates).strftime('%Y-%m-%d')}"
        else:
            date_range = "unknown"

        # Every leg of every herd member, not only each member's first (oldest)
        # leg: a member's fresh filing must restart the decay clock.
        members_in = set(unique_members)
        filed = []
        for tx in txs:
            if tx.member.lower().strip() not in members_in:
                continue
            try:
                filed.append(datetime.strptime(tx.filing_date, "%m/%d/%Y"))
            except (ValueError, TypeError):
                continue
        latest_filing = max(filed).strftime("%Y-%m-%d") if filed else ""

        total_low = sum(tx.amount_low for tx in member_txs)
        total_high = sum(tx.amount_high for tx in member_txs)

        # Track-record-weighted member count: 2 proven performers (weight ~2each)
        # outrank 4 noise traders (weight ~0.5 each).
        weighted_n = sum(member_weights.get(k, 1.0) for k in unique_members)
        if weighted_n >= 4 or total_high >= 500000:
            conviction = "high"
        elif weighted_n >= 3 or total_high >= 100000:
            conviction = "medium"
        else:
            conviction = "low"

        signals.append(HerdSignal(
            ticker=ticker,
            direction=direction,
            members=[{
                "name": tx.member,
                "chamber": tx.chamber,
                "state": tx.state,
                "amount_range": tx.amount_range,
                "date": tx.transaction_date or tx.filing_date,
            } for tx in member_txs],
            total_low=total_low,
            total_high=total_high,
            date_range=date_range,
            conviction=conviction,
            latest_filing=latest_filing,
        ))

    signals.sort(key=lambda s: len(s.members), reverse=True)
    return signals


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def load_existing_transactions() -> list[Transaction]:
    """Load previously saved transactions."""
    if not TRANSACTIONS_FILE.exists():
        return []
    try:
        data = json.loads(TRANSACTIONS_FILE.read_text())
        return [Transaction(**tx) for tx in data]
    except Exception:
        return []


def save_transactions(transactions: list[Transaction]):
    """Save transactions to disk, deduplicating."""
    seen = set()
    unique = []
    for tx in transactions:
        key = (tx.member.lower(), tx.ticker, tx.direction, tx.transaction_date, tx.doc_id)
        if key not in seen:
            seen.add(key)
            unique.append(tx)

    TRANSACTIONS_FILE.write_text(json.dumps(
        [vars(tx) for tx in unique], indent=2))
    log.info("Saved %d unique transactions", len(unique))


def pull_house(max_pdfs: int = 50) -> list[Transaction]:
    """Pull and parse recent House PTR filings."""
    scraper = HouseClerkScraper()
    filings = scraper.pull_index()

    existing = load_existing_transactions()
    existing_docs = {tx.doc_id for tx in existing}

    new_filings = [f for f in filings if f["doc_id"] not in existing_docs]
    log.info("%d new House filings to process (of %d total)", len(new_filings), len(filings))

    if len(new_filings) > max_pdfs:
        new_filings.sort(key=lambda f: f.get("filing_date", ""), reverse=True)
        new_filings = new_filings[:max_pdfs]
        log.info("Capped to %d most recent filings", max_pdfs)

    all_transactions = list(existing)

    for filing in new_filings:
        text = scraper.download_ptr(filing["doc_id"], filing["year"])
        if not text:
            continue

        state = filing.get("state_district", "")[:2]
        txs = extract_transactions_regex(
            text, filing["name"], "house", state,
            filing["doc_id"], filing.get("filing_date", ""))

        if not txs and TICKER_RE.search(text):
            log.info("Regex found 0 transactions but tickers present in %s, trying Claude...",
                     filing["doc_id"])
            txs = extract_transactions_claude(
                text, filing["name"], "house", state,
                filing["doc_id"], filing.get("filing_date", ""))

        if txs:
            log.info("  %s: %d transactions (%s)", filing["name"], len(txs),
                     ", ".join(set(tx.ticker for tx in txs)))
        all_transactions.extend(txs)

    save_transactions(all_transactions)
    return all_transactions


def pull_senate() -> list[Transaction]:
    """Pull and parse recent Senate PTR filings."""
    scraper = SenateEFDScraper()

    try:
        filings = scraper.pull_recent(days=60)
    except Exception as e:
        log.error("Senate scrape failed: %s", e)
        return []

    existing = load_existing_transactions()
    all_transactions = list(existing)

    for filing in filings:
        text = scraper.download_ptr(filing)
        if not text:
            continue

        name = filing.get("name", "")
        txs = extract_transactions_regex(
            text, name, "senate", filing.get("office", ""),
            "", filing.get("filing_date", ""))

        if not txs and TICKER_RE.search(text):
            txs = extract_transactions_claude(
                text, name, "senate", filing.get("office", ""),
                "", filing.get("filing_date", ""))

        if txs:
            log.info("  %s: %d transactions", name, len(txs))
        all_transactions.extend(txs)

    save_transactions(all_transactions)
    return all_transactions


def get_signal_for_ticker(ticker: str) -> dict:
    """Get congressional trading signal for a specific ticker."""
    transactions = load_existing_transactions()
    ticker = ticker.upper()

    cutoff = datetime.now() - timedelta(days=60)
    recent = []
    for tx in transactions:
        if tx.ticker != ticker:
            continue
        try:
            tx_date = datetime.strptime(tx.transaction_date, "%m/%d/%Y")
            if tx_date >= cutoff:
                recent.append(tx)
        except (ValueError, TypeError):
            recent.append(tx)

    if not recent:
        return {"ticker": ticker, "signal": "none", "transactions": []}

    buys = [tx for tx in recent if tx.direction == "buy"]
    sells = [tx for tx in recent if tx.direction == "sell"]
    buy_members = len(set(tx.member.lower() for tx in buys))
    sell_members = len(set(tx.member.lower() for tx in sells))

    if buy_members > sell_members:
        signal = "bullish"
    elif sell_members > buy_members:
        signal = "bearish"
    else:
        signal = "mixed"

    return {
        "ticker": ticker,
        "signal": signal,
        "buy_members": buy_members,
        "sell_members": sell_members,
        "total_transactions": len(recent),
        "transactions": [vars(tx) for tx in recent],
    }


def format_herd_for_thesis(signals: list[HerdSignal], ticker: str = None) -> str:
    """Format herd signals for injection into Sovereign thesis prompt."""
    if ticker:
        signals = [s for s in signals if s.ticker == ticker.upper()]

    if not signals:
        return ""

    lines = ["CONGRESSIONAL TRADING SIGNALS (STOCK Act disclosures):"]
    for s in signals[:5]:
        member_names = ", ".join(m["name"] for m in s.members[:4])
        if len(s.members) > 4:
            member_names += f" +{len(s.members) - 4} more"
        lines.append(
            f"  {s.ticker} — {len(s.members)} members {s.direction.upper()}ing "
            f"(${s.total_low:,}-${s.total_high:,}) | {s.conviction} conviction"
        )
        lines.append(f"    Members: {member_names}")
        lines.append(f"    Window: {s.date_range}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Congressional Trading Scraper")
    parser.add_argument("command", choices=["pull", "herd", "signal", "status"],
                        help="Command to run")
    parser.add_argument("ticker", nargs="?", help="Ticker for signal command")
    parser.add_argument("--max-pdfs", type=int, default=50,
                        help="Max PDFs to download per run")
    parser.add_argument("--house-only", action="store_true")
    parser.add_argument("--senate-only", action="store_true")
    args = parser.parse_args()

    if args.command == "pull":
        if not args.senate_only:
            pull_house(max_pdfs=args.max_pdfs)
        if not args.house_only:
            pull_senate()

    elif args.command == "herd":
        transactions = load_existing_transactions()
        if not transactions:
            print("No transactions loaded. Run 'pull' first.")
            sys.exit(1)

        signals = detect_herds(transactions)
        HERD_FILE.write_text(json.dumps(
            [vars(s) for s in signals], indent=2))

        print(f"\n{'='*60}")
        print(f"CONGRESSIONAL HERD SIGNALS — {datetime.now().strftime('%Y-%m-%d')}")
        print(f"{'='*60}")
        print(f"Transactions analyzed: {len(transactions)}")
        print(f"Herd signals found: {len(signals)}")

        for s in signals:
            print(f"\n{'🟢' if s.direction == 'buy' else '🔴'} {s.ticker} — "
                  f"{len(s.members)} members {s.direction.upper()}")
            print(f"  Conviction: {s.conviction} | "
                  f"Amount: ${s.total_low:,}-${s.total_high:,}")
            print(f"  Window: {s.date_range}")
            for m in s.members:
                print(f"    {m['name']} ({m['chamber']}/{m['state']}) "
                      f"— {m['amount_range']} on {m['date']}")

    elif args.command == "signal":
        if not args.ticker:
            print("Usage: congress_scraper.py signal TICKER")
            sys.exit(1)
        result = get_signal_for_ticker(args.ticker)
        print(json.dumps(result, indent=2))

    elif args.command == "status":
        transactions = load_existing_transactions()
        print(f"Transactions on file: {len(transactions)}")
        if transactions:
            members = set(tx.member for tx in transactions)
            tickers = set(tx.ticker for tx in transactions)
            print(f"Unique members: {len(members)}")
            print(f"Unique tickers: {len(tickers)}")
            buys = sum(1 for tx in transactions if tx.direction == "buy")
            sells = sum(1 for tx in transactions if tx.direction == "sell")
            print(f"Buys: {buys} | Sells: {sells}")
            house = sum(1 for tx in transactions if tx.chamber == "house")
            senate = sum(1 for tx in transactions if tx.chamber == "senate")
            print(f"House: {house} | Senate: {senate}")


if __name__ == "__main__":
    main()
