"""Background researcher: a local model writes a short, news-grounded memo per stock.

Runs hourly via cron. Picks the tickers whose memo is stalest, fetches recent
headlines for each from Google News RSS, feeds them to a local Ollama model as
context, and writes a structured, dated memo to disk.

Why the headlines come first: a small local model writing from memory can
classify a business but cannot see this week's events, and tends to invent
them. Given fresh headlines it only has to summarise. The per-stock analyst
then starts from current context and can spend its own searches on harder
questions.

No API credits are used. With the scan universe of about 90 names and five
tickers an hour, every memo is refreshed in under a day.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import DEFAULT_UNIVERSE


_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
_MODEL = os.environ.get("OLLAMA_RESEARCH_MODEL", "llama3:8b")

CACHE_DIR = PROJECT_ROOT / "research_cache"
CACHE_DIR.mkdir(exist_ok=True)
TTL_HOURS = 12  # halved — we have fresh news now, so refresh more often


# ─── Free web search via Google News RSS ────────────────────────────────────
# Google News RSS is the most reliable free source for ticker-specific news.
# No API key, no rate limit, returns recent results in clean XML, and it
# answers data-center IPs.

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def ddg_search(query: str, n: int = 6, timeout: int = 10) -> list[dict]:
    """Free Google News RSS lookup (named ddg_search for historical reasons).

    Returns [{"title": ..., "url": ..., "snippet": ...}, ...] sorted by recency.
    """
    try:
        r = requests.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
            headers=_HEADERS,
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning(f"[news] {query!r} → HTTP {r.status_code}")
            return []
    except Exception as e:
        logger.warning(f"[news] {query!r} failed: {e}")
        return []

    xml = r.text
    # Google News RSS returns <item><title>...</title><link>...</link>
    # <description>...</description><pubDate>...</pubDate></item>
    item_pat = re.compile(r"<item>(.*?)</item>", re.DOTALL)
    title_pat = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.DOTALL)
    link_pat = re.compile(r"<link>(.*?)</link>", re.DOTALL)
    desc_pat = re.compile(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", re.DOTALL)
    pub_pat = re.compile(r"<pubDate>(.*?)</pubDate>", re.DOTALL)

    out = []
    for block in item_pat.findall(xml)[:n]:
        def first(pattern) -> str:
            m = pattern.search(block)
            return m.group(1).strip() if m else ""

        title, link, desc, pub = first(title_pat), first(link_pat), first(desc_pat), first(pub_pat)

        snippet = _strip_tags(desc)[:300]
        if title:
            out.append({
                "title": _strip_tags(title),
                "url": link,
                "snippet": snippet,
                "published": pub,
            })
    return out


_TAG_RE_HTML = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    s = _TAG_RE_HTML.sub("", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    s = s.replace("&#x27;", "'").replace("&lt;", "<").replace("&gt;", ">")
    return " ".join(s.split())


def _build_search_query(symbol: str, quality_hint: str | None = None) -> str:
    """Tailor the search query by what kind of stock it is."""
    if quality_hint == "LEVERAGED_PROXY":
        return f"{symbol} stock crypto Bitcoin price news recent"
    if quality_hint == "CYCLICAL":
        return f"{symbol} stock earnings outlook recent news"
    if quality_hint == "SPECULATIVE":
        return f"{symbol} stock news dilution funding recent"
    if quality_hint == "DISTRESSED":
        return f"{symbol} stock turnaround news recent earnings"
    return f"{symbol} stock recent news earnings"


def _format_news_context(results: list[dict]) -> str:
    """Format Google News RSS results for the LLM prompt.

    Google News RSS titles already include the source ("... - Yahoo Finance"),
    so the headline alone is enough context. The description field is just
    a link wrapper, no body text — we skip it.
    """
    if not results:
        return "(no recent news found)"
    lines = []
    for i, r in enumerate(results[:8], 1):
        title = r["title"]
        pub = r.get("published", "")
        # pubDate is "Wed, 14 May 2026 18:00:00 GMT" — keep just date portion
        if pub:
            pub = pub.split(":")[0].rsplit(" ", 1)[0]  # "Wed, 14 May 2026 18"
        lines.append(f"{i}. {title}  [{pub}]" if pub else f"{i}. {title}")
    return "\n".join(lines)


# ─── Ollama generation ──────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """You are a research analyst writing a short briefing about a publicly traded company for a CSP-wheel trader. The trader will only sell puts on stocks they'd be comfortable owning long-term.

Company ticker: {symbol}

Today's date: {today}

RECENT NEWS CONTEXT (from web search — use it!):
{news_context}

Now write a structured 4-section memo. No bullet lists, no preamble, no disclaimers. Be concise — under 200 words total. Use the news context to ground your assessment. If specific facts aren't in the news context, say "no recent data" rather than inventing.

1. WHAT IT DOES — One sentence: what does this company actually do for revenue?
2. SECTOR & BUSINESS MODEL — Sector + cyclical/defensive/growth/distressed/commodity-linked. Capital intensive or asset-light? Recurring revenue or transactional?
3. RECENT DEVELOPMENTS — Anything notable from the news context above? Earnings beat/miss, guidance change, sector move, regulatory news, M&A? Be specific. If nothing newsworthy, say "no major recent developments".
4. WHEEL FIT — Classify: BLUE_CHIP / QUALITY / CYCLICAL / SPECULATIVE / LEVERAGED_PROXY / DISTRESSED. One sentence on why, factoring in BOTH structural model AND recent news.

End with one line in this exact format:
QUALITY_TAG: <one of: BLUE_CHIP, QUALITY, CYCLICAL, SPECULATIVE, LEVERAGED_PROXY, DISTRESSED>
"""


def _ollama_generate(prompt: str, timeout: int = 120) -> str:
    r = requests.post(
        f"{_OLLAMA_URL}/api/generate",
        json={"model": _MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": 0.3, "num_predict": 500}},
        timeout=timeout,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama {r.status_code}: {r.text[:200]}")
    return (r.json().get("response") or "").strip()


# ─── Cache I/O ──────────────────────────────────────────────────────────────

def memo_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}.json"


def load_memo(symbol: str) -> dict | None:
    p = memo_path(symbol)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


_TAG_RE = re.compile(
    r"QUALITY_TAG\s*:\s*(BLUE_CHIP|QUALITY|CYCLICAL|SPECULATIVE|LEVERAGED_PROXY|DISTRESSED)",
    re.IGNORECASE,
)


# ─── Main: research one stock ───────────────────────────────────────────────

def research_stock(symbol: str) -> dict:
    """Run one research pass for a single ticker. Caches to disk.

    Pipeline:
        1. Load prior memo (if any) → use its quality_tag to tailor search query
        2. DuckDuckGo search (free, ~5 sec)
        3. Format snippets as news context
        4. Send to Ollama with template (~7 sec)
        5. Parse QUALITY_TAG, save to disk
    """
    sym = symbol.upper()
    prior = load_memo(sym)
    quality_hint = prior.get("quality_tag") if prior else None

    # 1. Web search
    query = _build_search_query(sym, quality_hint)
    logger.info(f"[ollama] {sym}: searching DDG — {query!r}")
    news = ddg_search(query, n=6)
    logger.info(f"[ollama] {sym}: {len(news)} news snippets found")
    news_context = _format_news_context(news)

    # 2. Ollama generation
    prompt = _PROMPT_TEMPLATE.format(
        symbol=sym,
        today=datetime.date.today().isoformat(),
        news_context=news_context,
    )
    text = _ollama_generate(prompt)

    # 3. Tag extraction — a malformed/empty model reply must NOT null out a good
    # prior quality_tag: the wheel's DISTRESSED / LEVERAGED_PROXY "always exit"
    # signal reads it, so silently dropping it to None weakens a live guard. Carry
    # the previous tag forward on a parse miss and mark the memo degraded.
    m = _TAG_RE.search(text)
    tag_degraded = m is None
    if m:
        tag = m.group(1).upper()
    else:
        prior = {}
        try:
            if memo_path(sym).exists():
                prior = json.loads(memo_path(sym).read_text())
        except Exception:
            prior = {}
        tag = prior.get("quality_tag")
        logger.warning(f"[ollama] {sym}: no quality_tag parsed — carrying prior {tag!r} forward")

    record = {
        "symbol": sym,
        "memo": text,
        "quality_tag": tag,
        "tag_degraded": tag_degraded,
        "model": _MODEL,
        "news_sources": [{"title": n["title"], "url": n.get("url", "")} for n in news],
        "news_query": query,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "generated_at_unix": time.time(),
    }
    # Atomic write: a torn memo both bumps the wheel's freshness SLA and can
    # deserialize to a clean (None) quality_tag mid-write. Same-dir tmp → rename.
    _mp = memo_path(sym)
    _tmp = _mp.with_suffix(".json.tmp")
    _tmp.write_text(json.dumps(record, indent=2))
    os.replace(_tmp, _mp)
    return record


def pick_next_stocks(batch: int = 5, universe: list[str] | None = None) -> list[str]:
    """Pick the N stocks with the stalest (or missing) memo.

    Share lots tracked by hand (data/owned_positions.py) join the rotation even
    when they are outside DEFAULT_UNIVERSE. Rotation is strictly by staleness.
    """
    universe = universe or DEFAULT_UNIVERSE

    # Hand-tracked share lots join the rotation
    try:
        from data.owned_positions import get_symbols
        owned = get_symbols()
    except Exception:
        owned = []

    all_syms = list({*universe, *owned})
    items = []
    for sym in all_syms:
        memo = load_memo(sym)
        ts = memo.get("generated_at_unix", 0) if memo else 0
        # No per-stock preference: rotate strictly by memo staleness
        items.append((ts, sym))
    items.sort()
    return [s for _, s in items[:batch]]


def run_batch(batch: int = 5, universe: list[str] | None = None) -> list[dict]:
    picks = pick_next_stocks(batch=batch, universe=universe)
    out = []
    for sym in picks:
        try:
            logger.info(f"[ollama] researching {sym}")
            rec = research_stock(sym)
            out.append({
                "symbol": sym,
                "quality_tag": rec.get("quality_tag"),
                "news_count": len(rec.get("news_sources", [])),
            })
        except Exception as e:
            logger.warning(f"[ollama] {sym} failed: {e}")
            out.append({"symbol": sym, "error": str(e)})
    return out


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=5)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--search-only", action="store_true",
                        help="Just test the DDG search, don't call Ollama")
    args = parser.parse_args()

    if args.search_only and args.symbols:
        for s in args.symbols:
            q = _build_search_query(s)
            results = ddg_search(q, n=6)
            print(f"=== {s} ({q}) → {len(results)} results ===")
            for r in results:
                print(f"  • {r['title']}")
                print(f"    {r['snippet'][:200]}")
    elif args.symbols:
        for s in args.symbols:
            r = research_stock(s)
            print(f"{s}: tag={r.get('quality_tag')} · {len(r.get('news_sources', []))} sources")
    else:
        result = run_batch(batch=args.batch)
        print(json.dumps(result, indent=2))
