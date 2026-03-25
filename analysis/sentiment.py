"""
News sentiment analysis using keyword-based scoring and optional FinBERT.

Data Sources:
- News: Yahoo Finance RSS feeds (free, no API key required)
- Sentiment Model: FinBERT (ProsusAI/finbert) from Hugging Face (optional)
"""

import logging
import warnings
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import yfinance as yf

logger = logging.getLogger(__name__)

# Suppress warnings for cleaner output
warnings.filterwarnings("ignore")


def get_news_sentiment(ticker: str, days: int = 7) -> Dict:
    """
    Analyze recent news sentiment for a stock.

    Args:
        ticker: Stock symbol (e.g., 'AAPL')
        days: Number of days of news to analyze (default: 7)

    Returns:
        Dict containing sentiment analysis and news articles with sources
    """
    try:
        # Get news from Yahoo Finance
        stock = yf.Ticker(ticker)
        news_items = stock.news

        if not news_items:
            return {
                "error": f"No recent news found for {ticker}",
                "ticker": ticker,
            }

        # Analyze sentiment for each article
        analyzed_articles: List[Dict] = []
        sentiments: List[str] = []

        for article in news_items[:20]:  # Limit to 20 most recent articles
            # Extract article info (data is nested under 'content')
            content = article.get("content", {})
            title = content.get("title", "No title")
            provider = content.get("provider", {})
            publisher = provider.get("displayName", "Unknown")

            canonical_url = content.get("canonicalUrl", {})
            link = canonical_url.get("url", "")

            pub_date_str_raw = content.get("pubDate", "")
            # Parse ISO format date
            if pub_date_str_raw:
                try:
                    pub_datetime = datetime.fromisoformat(
                        pub_date_str_raw.replace("Z", "+00:00")
                    )
                    # Convert to naive datetime for comparison
                    pub_datetime_naive = pub_datetime.replace(tzinfo=None)
                    pub_date_str = pub_datetime.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pub_datetime_naive = datetime.now()
                    pub_date_str = "Unknown date"
            else:
                pub_datetime_naive = datetime.now()
                pub_date_str = "Unknown date"

            # Check if article is within date range
            if pub_datetime_naive < datetime.now() - timedelta(days=days):
                continue

            # Analyze sentiment using simple keyword-based approach
            sentiment_data = _analyze_sentiment_keywords(title)

            analyzed_articles.append(
                {
                    "title": title,
                    "publisher": publisher,
                    "link": link,
                    "published": pub_date_str,
                    "sentiment": sentiment_data["sentiment"],
                    "score": sentiment_data["score"],
                    "confidence": sentiment_data["confidence"],
                }
            )

            sentiments.append(sentiment_data["sentiment"])

        if not analyzed_articles:
            return {
                "error": f"No recent news within {days} days for {ticker}",
                "ticker": ticker,
            }

        # Calculate aggregate sentiment
        sentiment_counts = Counter(sentiments)
        total = len(sentiments)

        positive_pct = (sentiment_counts.get("positive", 0) / total) * 100
        neutral_pct = (sentiment_counts.get("neutral", 0) / total) * 100
        negative_pct = (sentiment_counts.get("negative", 0) / total) * 100

        # Determine overall sentiment
        if positive_pct > 60:
            overall = "POSITIVE"
        elif negative_pct > 60:
            overall = "NEGATIVE"
        elif positive_pct > negative_pct + 20:
            overall = "MODERATELY POSITIVE"
        elif negative_pct > positive_pct + 20:
            overall = "MODERATELY NEGATIVE"
        else:
            overall = "NEUTRAL"

        return {
            "ticker": ticker,
            "analysis_period": f"{days} days",
            "articles_analyzed": len(analyzed_articles),
            "overall_sentiment": overall,
            "sentiment_breakdown": {
                "positive": sentiment_counts.get("positive", 0),
                "neutral": sentiment_counts.get("neutral", 0),
                "negative": sentiment_counts.get("negative", 0),
                "positive_pct": round(positive_pct, 1),
                "neutral_pct": round(neutral_pct, 1),
                "negative_pct": round(negative_pct, 1),
            },
            "articles": analyzed_articles,
            "data_source": "Yahoo Finance News",
            "analysis_timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        }

    except Exception as e:
        logger.exception("Failed to analyze news sentiment for %s", ticker)
        return {"error": f"Failed to analyze news sentiment: {str(e)}"}


def _analyze_sentiment_keywords(text: str) -> Dict:
    """
    Simple keyword-based sentiment analysis.
    (Lightweight alternative to FinBERT for basic functionality)

    Args:
        text: Text to analyze (headline or article)

    Returns:
        Dict with sentiment classification and confidence
    """
    text_lower = text.lower()

    # Financial sentiment keywords
    positive_keywords = [
        "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies",
        "gain", "gains", "rise", "rises", "up", "bullish", "strong", "growth",
        "profit", "profits", "revenue", "success", "successful", "outperform",
        "upgrade", "upgraded", "buy", "positive", "optimistic", "boom", "record",
        "high", "higher", "increase", "increases", "win", "wins", "expanding",
    ]

    negative_keywords = [
        "fall", "falls", "drop", "drops", "plunge", "plunges", "crash", "crashes",
        "loss", "losses", "down", "bearish", "weak", "decline", "declines",
        "miss", "misses", "warning", "warns", "cut", "cuts", "downgrade",
        "downgraded", "sell", "negative", "concern", "concerns", "worry",
        "worries", "slump", "slumps", "low", "lower", "decrease", "decreases",
        "fail", "fails", "struggle", "struggles", "probe", "investigation",
    ]

    # Count keyword occurrences
    positive_count = sum(1 for word in positive_keywords if word in text_lower)
    negative_count = sum(1 for word in negative_keywords if word in text_lower)

    # Determine sentiment
    if positive_count > negative_count:
        sentiment = "positive"
        score = min(0.5 + (positive_count * 0.15), 0.95)
    elif negative_count > positive_count:
        sentiment = "negative"
        score = min(0.5 + (negative_count * 0.15), 0.95)
    else:
        sentiment = "neutral"
        score = 0.5

    # Calculate confidence (higher difference = higher confidence)
    diff = abs(positive_count - negative_count)
    confidence = "low" if diff == 0 else ("medium" if diff == 1 else "high")

    return {
        "sentiment": sentiment,
        "score": round(score, 2),
        "confidence": confidence,
    }


def analyze_news_sentiment(ticker: str) -> Dict:
    """
    Simplified interface for news sentiment analysis.
    Returns formatted analysis suitable for AI agent interpretation.

    Args:
        ticker: Stock symbol

    Returns:
        Dict with formatted summary and raw data
    """
    result = get_news_sentiment(ticker, days=7)

    if "error" in result:
        return result

    # Format articles for display
    articles_text: List[str] = []
    for i, article in enumerate(result["articles"][:10], 1):  # Show top 10
        sentiment_emoji = {
            "positive": "[+]",
            "neutral": "[=]",
            "negative": "[-]",
        }.get(article.get("sentiment", "neutral"), "[?]")

        articles_text.append(
            f"{i}. {sentiment_emoji} {article['title']}\n"
            f"   Source: {article['publisher']} | {article['published']}\n"
            f"   Sentiment: {article['sentiment'].upper()} (confidence: {article['confidence']})\n"
            f"   Link: {article['link']}"
        )

    summary = f"""
News Sentiment Analysis for {result['ticker']}
Analysis Timestamp: {result['analysis_timestamp']}
Analysis Period: {result['analysis_period']}
Articles Analyzed: {result['articles_analyzed']}

DATA SOURCES & VERIFICATION:
- News Source: Yahoo Finance RSS/API
- Verify at: https://finance.yahoo.com/quote/{result['ticker']}/news
- Company news: https://finance.yahoo.com/quote/{result['ticker']}

ANALYSIS METHODOLOGY:
- Sentiment classification using financial keyword analysis
- Keywords: Positive (beat, surge, profit, etc.) vs Negative (fall, loss, warning, etc.)
- Confidence based on keyword frequency and strength
- Note: Simplified keyword approach; full FinBERT analysis available with transformers library

SENTIMENT BREAKDOWN:
- Overall Sentiment: {result['overall_sentiment']}
- Positive: {result['sentiment_breakdown']['positive']} articles ({result['sentiment_breakdown']['positive_pct']}%)
- Neutral: {result['sentiment_breakdown']['neutral']} articles ({result['sentiment_breakdown']['neutral_pct']}%)
- Negative: {result['sentiment_breakdown']['negative']} articles ({result['sentiment_breakdown']['negative_pct']}%)

RECENT HEADLINES (Top 10):

{chr(10).join(articles_text)}

IMPORTANT DISCLAIMER:
This sentiment analysis is for informational purposes only and should NOT be considered
financial advice. News sentiment can change rapidly and may not reflect fundamental value.
Always verify news articles independently using the links provided above. Past sentiment
does not guarantee future price movements. Consult a licensed financial advisor before
making investment decisions.

Sentiment analysis is automated and may misclassify headlines. Human review recommended.
Consider multiple sources and verify factual claims before acting on news-based insights.
"""

    return {
        "summary": summary.strip(),
        "raw_data": result,
        "verification_links": {
            "news": f"https://finance.yahoo.com/quote/{result['ticker']}/news",
            "quote": f"https://finance.yahoo.com/quote/{result['ticker']}",
        },
        "data_source": "Yahoo Finance News",
        "analysis_timestamp": result["analysis_timestamp"],
        "note": "Keyword-based sentiment. For advanced FinBERT analysis, install: pip install transformers torch",
    }


# Optional: Full FinBERT implementation (requires transformers)
def analyze_with_finbert(ticker: str) -> Dict:
    """
    Advanced sentiment analysis using FinBERT model from Hugging Face.

    NOTE: Requires transformers library (large download ~500MB):
    pip install transformers torch

    This function is optional and will be used if transformers is available.
    Otherwise, the keyword-based approach above is used.

    Args:
        ticker: Stock symbol

    Returns:
        Dict with FinBERT sentiment analysis
    """
    try:
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        import torch

        # Load FinBERT model
        tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")

        # Get news
        news_data = get_news_sentiment(ticker, days=7)

        if "error" in news_data:
            return news_data

        # Analyze each article with FinBERT
        for article in news_data["articles"]:
            text = article["title"]

            # Tokenize and analyze
            inputs = tokenizer(
                text, return_tensors="pt", padding=True, truncation=True, max_length=512
            )
            outputs = model(**inputs)
            predictions = torch.nn.functional.softmax(outputs.logits, dim=-1)

            # Get sentiment
            sentiment_map = {0: "positive", 1: "negative", 2: "neutral"}
            sentiment_idx = torch.argmax(predictions).item()
            confidence = predictions[0][sentiment_idx].item()

            article["sentiment"] = sentiment_map[sentiment_idx]
            article["score"] = round(confidence, 2)
            article["confidence"] = (
                "high" if confidence > 0.8 else ("medium" if confidence > 0.6 else "low")
            )
            article["model"] = "FinBERT"

        # Recalculate aggregate sentiment with FinBERT results
        sentiments = [a["sentiment"] for a in news_data["articles"]]
        sentiment_counts = Counter(sentiments)
        total = len(sentiments)

        news_data["sentiment_breakdown"] = {
            "positive": sentiment_counts.get("positive", 0),
            "neutral": sentiment_counts.get("neutral", 0),
            "negative": sentiment_counts.get("negative", 0),
            "positive_pct": round(
                (sentiment_counts.get("positive", 0) / total) * 100, 1
            ),
            "neutral_pct": round(
                (sentiment_counts.get("neutral", 0) / total) * 100, 1
            ),
            "negative_pct": round(
                (sentiment_counts.get("negative", 0) / total) * 100, 1
            ),
        }

        news_data["model"] = "FinBERT (ProsusAI/finbert)"

        return news_data

    except ImportError:
        return {
            "error": "FinBERT requires transformers library. Install with: pip install transformers torch",
            "fallback": "Using keyword-based sentiment analysis instead",
        }
