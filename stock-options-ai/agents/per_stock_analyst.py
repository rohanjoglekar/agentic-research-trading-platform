"""Per-stock grouping + risk/buy-thesis analyst.

Groups the flat list of options trades into one entry per stock with the
top-N strikes, then asks a model with web search (Gemini with Google Search
grounding by default, Claude as the fallback) for a short risk assessment and
buy thesis grounded in recent news.
"""

import os
import re
from typing import Optional

import pandas as pd
from loguru import logger

from config.settings import ANTHROPIC_API_KEY


_DEFAULT_MODEL = "claude-haiku-4-5"

# Provider switch (reversible + safe): PER_STOCK_LLM_PROVIDER ∈ {gemini, anthropic},
# default gemini. Gemini runs the SAME prompt (web-search-grounded) and its text is
# fed through the SAME _clean_preamble / _parse_would_own downstream. If Gemini
# returns nothing OR its reply lacks a parseable WOULD_OWN_SCORE, we fall back to
# the intact Claude call. Read via os.getenv so it can be flipped without a redeploy.
_PER_STOCK_PROVIDER = os.getenv("PER_STOCK_LLM_PROVIDER", "gemini").strip().lower()

# Gemini's grounded reply is longer than Claude's (search citations inflate it);
# give the wheel analyst headroom so the WOULD_OWN_SCORE line isn't truncated.
_GEMINI_MAX_TOKENS = 1200

# Optional FREE fallback wedged BETWEEN gemini and claude: a local Ollama model
# grounded on ddgs web snippets (no API key, no Gemini quota, no Claude cost).
# OFF by default (PER_STOCK_FALLBACK != "local_search") → behavior is unchanged.
# It is parse-gated exactly like the gemini path — a reply without a parseable
# WOULD_OWN_SCORE falls through to Claude — so the worst case is today's behavior
# and the best case is a Claude call saved when the Gemini quota is spent. The
# local model is slow (~1-2 min/stock) but this only runs on the 7:30 ET daily
# scan (off any request path), and only when Gemini has already failed for a name.
_PER_STOCK_FALLBACK = os.getenv("PER_STOCK_FALLBACK", "").strip().lower()
# Default to the 3B model: validated, qwen2.5-coder:7b TIMES OUT (>180s) on this
# 8GB box, so it just wastes the window and falls through to Claude anyway. The
# 3B is weaker but actually finishes (~100-130s). Override to a bigger model via
# PER_STOCK_LOCAL_MODEL only if you ALSO raise LOCAL_SEARCH_MODEL_TIMEOUT.
_LOCAL_SEARCH_MODEL = os.getenv("PER_STOCK_LOCAL_MODEL", "llama3.2:3b").strip()

# Steer the local model at the strict output contract (the web snippets ARE its
# grounding; the per-stock prompt itself carries the format + WOULD_OWN rubric).
_LOCAL_SEARCH_SYSTEM = (
    "You are a financial analyst. Use the web snippets provided to inform a "
    "concise risk assessment and buy thesis. Follow the requested output format "
    "EXACTLY, including the final WOULD_OWN_SCORE line. Base any specific numbers "
    "(prices, dates, figures) ONLY on the snippets — never invent them."
)


def group_by_stock(df: pd.DataFrame, num_stocks: int = 20, strikes_per_stock: int = 5) -> list[dict]:
    """Collapse a flat ranked DataFrame into ``num_stocks`` unique tickers.

    Stock ranking: best Final Score per ticker (defines top-20 stocks).
    Within a stock: strikes are sorted by **POP descending** so the user
    sees the *least-likely-to-be-assigned* strike first. The 2% monthly
    floor is already enforced upstream — at this point every row meets the
    return target, so safety is the operative axis.
    """
    if df.empty:
        return []
    if "Final Score" not in df.columns or "Ticker" not in df.columns:
        logger.warning("[per_stock] DataFrame missing required columns")
        return []

    # POP can arrive as "76.5%" string or 0.765 float. Normalize.
    def _pop(val) -> float:
        if val is None:
            return 0.0
        try:
            if isinstance(val, str):
                return float(val.rstrip("%")) / (100 if "%" in val else 1)
            f = float(val)
            return f / 100 if f > 1 else f  # heuristic
        except (TypeError, ValueError):
            return 0.0

    df = df.copy()
    df["_pop_norm"] = df["POP"].apply(_pop) if "POP" in df.columns else 0.5

    # Stock-level ordering: best Final Score per ticker
    by_score = df.sort_values("Final Score", ascending=False)
    seen: set[str] = set()
    out: list[dict] = []
    for sym in by_score["Ticker"]:
        if sym in seen:
            continue
        seen.add(sym)
        # Within this ticker: pick the strikes_per_stock highest-POP rows
        sub = (df[df["Ticker"] == sym]
               .sort_values("_pop_norm", ascending=False)
               .head(strikes_per_stock)
               .drop(columns=["_pop_norm"])
               .reset_index(drop=True))
        best = sub.iloc[0]  # the "safest" strike, by design
        strikes = sub.to_dict("records")
        _sec = best["Sector"] if "Sector" in sub.columns else None
        sector = _sec if (isinstance(_sec, str) and _sec and _sec.lower() != "nan") else "Unknown"
        out.append({
            "symbol": sym,
            "sector": sector,
            "best_score": float(by_score[by_score["Ticker"] == sym].iloc[0]["Final Score"]),
            "best_rating": by_score[by_score["Ticker"] == sym].iloc[0].get("Rating", "?"),
            "current_price": float(best["Price"]) if "Price" in best.index else None,
            "strikes": strikes,
        })
        if len(out) >= num_stocks:
            break
    return out


# ─── LLM-with-web-search analysis ──────────────────────────────────────────


def _clean_preamble(text: str) -> str:
    """Strip LLM preamble like "I'll search for..." before the first **Risk assessment** header."""
    if not text:
        return text
    import re as _re
    # Drop everything before the first occurrence of **Risk assessment (case-insensitive)
    m = _re.search(r'\*\*Risk\s+assessment', text, _re.IGNORECASE)
    if m and m.start() > 0:
        return text[m.start():].strip()
    return text.strip()


def _extract_text(response) -> str:
    """Pull the final natural-language text out of an Anthropic response that
    may include web-search blocks."""
    parts: list[str] = []
    for block in response.content:
        # text blocks
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def _claude_analysis(prompt: str, sym: str, model: str, max_uses: int) -> str:
    """The original Claude (Anthropic + web_search) call — kept fully intact as
    the fallback path. Anthropic import stays lazy/guarded (SDK may be absent).
    Returns cleaned text (post-preamble-strip) or "" on any failure."""
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        import anthropic
    except ImportError:
        return ""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=model,
            max_tokens=700,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
            messages=[{"role": "user", "content": prompt}],
        )
        return _clean_preamble(_extract_text(msg))
    except Exception as e:
        logger.warning(f"[per_stock] Claude analysis failed for {sym}: {e}")
        return ""


def _gemini_analysis(prompt: str, sym: str) -> str:
    """Gemini path — SAME prompt, Google-Search grounding ON (the Claude call is
    web-search-grounded). Returns cleaned text or "" on missing key / any failure
    (gemini_generate never raises and returns None on failure). Its text is run
    through the SAME _clean_preamble / _parse_would_own downstream, so the caller
    can fall back to Claude when the score doesn't parse."""
    try:
        from agents.llm_gemini import gemini_generate
    except ImportError:
        return ""
    text = gemini_generate(prompt, use_search=True, max_tokens=_GEMINI_MAX_TOKENS)
    if not text:
        return ""
    return _clean_preamble(text)


def _local_search_analysis(prompt: str, sym: str) -> str:
    """FREE local-search path — ddgs web snippets + a local Ollama model, no
    Gemini quota / no Claude cost. SAME prompt (so the WOULD_OWN rubric is
    intact); its text runs through the SAME _clean_preamble / _parse_would_own
    downstream, so a malformed/unscored reply falls through to Claude. Returns
    cleaned text or "" on missing module / no results / model failure."""
    try:
        from agents.local_search import web_answer
    except ImportError:
        return ""
    text = web_answer(
        prompt,
        search_query=f"{sym} stock latest news earnings analyst rating price target outlook",
        system=_LOCAL_SEARCH_SYSTEM,
        model=_LOCAL_SEARCH_MODEL,
        max_results=5,
        num_predict=_GEMINI_MAX_TOKENS,
        num_ctx=8192,          # the per-stock prompt + snippets are sizeable
    )
    if not text:
        return ""
    return _clean_preamble(text)


def analyze_stock(stock_entry: dict, model: str = _DEFAULT_MODEL,
                  max_uses: int = 2) -> str:
    """Returns a 2-paragraph risk + buy-thesis analysis for one stock.

    Provider is env-switched (PER_STOCK_LLM_PROVIDER, default gemini): Gemini
    (Google-Search-grounded) runs first, and if it returns nothing OR its reply
    lacks a parseable WOULD_OWN_SCORE, we fall back to the intact Claude call
    (Anthropic + web_search, up to ``max_uses`` searches). Same prompt, same
    parser, same output shape either way — only the text generator changes.
    """
    sym = stock_entry["symbol"]
    price = stock_entry.get("current_price")
    rating = stock_entry.get("best_rating", "?")
    score = stock_entry.get("best_score", 0)
    strikes = stock_entry.get("strikes", [])

    # Compact strikes summary
    rows = []
    for s in strikes[:5]:
        k = s.get("Strike", "?")
        exp = s.get("Expiration", "?")
        prem = s.get("Premium", "?")
        mo = s.get("Monthly %", "?")
        rows.append(f"  ${k} strike, {exp}, ${prem} premium, {mo}%/mo")
    strike_block = "\n".join(rows) or "  (none)"

    # Pull Ollama's prior research memo, if any. It's generic-knowledge classification —
    # Claude is told to verify or contradict it with current web data, not parrot it.
    ollama_block = ""
    try:
        from agents.ollama_researcher import load_memo
        memo = load_memo(sym)
        if memo and memo.get("memo"):
            ollama_block = (
                "\n\n--- Prior research memo (from local Ollama, possibly stale, MAY BE WRONG) ---\n"
                + memo["memo"].strip()
                + "\n--- end memo ---\n"
                + "Treat this memo as a starting hypothesis, NOT ground truth. Use your web search "
                + "to verify or refute its claims. Note any factual errors you spot.\n"
            )
    except Exception as e:
        logger.warning(f"[per_stock] failed to load Ollama memo for {sym}: {e}")

    # Wall-Street consensus (Buy/Hold/Sell + mean price target) — a conviction
    # input for a strategy that can be assigned the stock. Precomputed in
    # analyze_top_stocks() so it's available here for the would-own judgment.
    consensus = stock_entry.get("analyst_consensus") or {}
    consensus_block = ""
    _csum = consensus.get("summary")
    if _csum and _csum not in ("no analyst coverage", "no consensus"):
        consensus_block = (
            f"\nAnalyst consensus (from market data): {_csum}."
            + (f" ⚠️ {'; '.join(consensus['flags'])}." if consensus.get("flags") else "")
            + "\n"
        )

    prompt = (
        f"You are advising a retail trader running a wheel strategy on {sym} "
        f"(currently ${price}). The trade only matters if {sym} is something they would be comfortable "
        f"owning long-term if assigned at the strike. High premium alone is not enough. "
        f"The rule scorer says {score:.0f}/100 ({rating}).\n\n"
        f"Top 5 candidate strikes:\n{strike_block}\n"
        f"{ollama_block}\n"
        f"{consensus_block}\n"
        f"Search the web for the latest news on {sym} (1-2 searches max: earnings, sector outlook, "
        f"regulatory/legal, recent price action, dilution/buybacks). Then output exactly:\n\n"
        f"**Risk assessment** (3-4 sentences): biggest near-term risks of being assigned 100 shares "
        f"of {sym} and holding 3-6 months while selling covered calls. Cite specific recent news.\n\n"
        f"**Why it is a wheel candidate** (3-4 sentences): bullish thesis. Is {sym} a quality stock "
        f"with a long-term upward trajectory? Cite valuation, competitive position, business momentum, "
        f"balance sheet. If the analyst consensus above is Hold/Sell or {sym} is already at/above its "
        f"mean price target, address it head-on — say whether you agree the upside is limited.\n\n"
        f"Then on the LAST line, output exactly this format (no other text on that line):\n"
        f"WOULD_OWN_SCORE: <integer 1-10>\n\n"
        f"Where 10 = blue-chip quality. 7-8 = solid quality. 4-6 = speculative but defensible. "
        f"1-3 = leveraged proxy, frequent dilution, or other red flags. "
        f"Weigh the analyst consensus: a Hold consensus or a name already at/above its mean price "
        f"target should cap WOULD_OWN around 6 (ownable, but the Street sees limited upside and "
        f"assignment risk is real); a Sell/Underperform consensus caps it around 4; a Buy/Strong Buy "
        f"with clear upside to target supports 7+. Be honest.\n\n"
        f"CRITICAL OUTPUT FORMAT: Start the response IMMEDIATELY with the literal text "
        f"**Risk assessment** as the first characters. "
        f"DO NOT include any preamble like 'I will search for', 'Let me look up', "
        f"'Based on my search', 'I will provide'. Go straight into the analysis. "
        f"DO NOT acknowledge the request or describe what you are about to do. "
        f"No disclaimers, no bullet lists. Just: **Risk assessment** paragraph + "
        f"**Why it is a wheel candidate** paragraph + WOULD_OWN_SCORE line."
    )

    # ── Provider dispatch ───────────────────────────────────────────────────
    # Default: try Gemini first (same prompt, search-grounded). Accept its answer
    # ONLY if it parses into a WOULD_OWN_SCORE — the whole point of this site is
    # that score, so an ungrounded/malformed reply must fall back to Claude rather
    # than feed a scoreless analysis downstream (None would_own → fail-SAFE keep).
    if _PER_STOCK_PROVIDER == "gemini":
        g_text = _gemini_analysis(prompt, sym)
        if g_text and _parse_would_own(g_text) is not None:
            logger.info(f"[per_stock] {sym} analysis provider=gemini")
            return g_text
        logger.info(
            f"[per_stock] {sym} gemini "
            f"{'empty' if not g_text else 'unparseable score'} — falling back"
        )

        # FREE web-grounded fallback before paying for Claude. Off unless
        # PER_STOCK_FALLBACK=local_search. Parse-gated: an unscored local reply
        # falls through to Claude below, so this can only save a Claude call,
        # never degrade the pipeline (a missing WOULD_OWN → fail-SAFE keep).
        if _PER_STOCK_FALLBACK == "local_search":
            l_text = _local_search_analysis(prompt, sym)
            if l_text and _parse_would_own(l_text) is not None:
                logger.info(f"[per_stock] {sym} analysis provider=local_search")
                return l_text
            logger.info(
                f"[per_stock] {sym} local_search "
                f"{'empty' if not l_text else 'unparseable score'} — falling back to claude"
            )

    c_text = _claude_analysis(prompt, sym, model, max_uses)
    if c_text:
        logger.info(f"[per_stock] {sym} analysis provider=anthropic")
    return c_text


# line-anchored + markdown-tolerant so Gemini's "**WOULD_OWN_SCORE:** 8" bolding
# still parses (else the gemini path would always fall back to Claude) and a
# stray in-prose number can't be grabbed. Take the LAST anchored match.
_SCORE_RE = re.compile(r"^\s*\**\s*WOULD_OWN_SCORE\s*\**\s*:\s*\**\s*(\d{1,2})", re.I | re.M)


def _parse_would_own(text: str) -> Optional[int]:
    if not text:
        return None
    matches = _SCORE_RE.findall(text)
    if not matches:
        return None
    try:
        v = int(matches[-1])
        return max(1, min(10, v))
    except ValueError:
        return None


def analyze_top_stocks(stocks: list[dict], max_workers: int = 4) -> list[dict]:
    """Run analyze_stock concurrently. Sets ``analysis`` and ``would_own_score``
    on each entry, then re-ranks by
    ``best_score × (would_own_score / 10) × sqrt(POP) × analyst consensus``.
    """
    if not stocks:
        return stocks

    # Fetch Wall-Street consensus for each name up front (shared cached client)
    # so analyze_stock() can factor it into would-own, and the re-rank below can
    # apply it deterministically. Best-effort — never breaks the scan.
    try:
        from agents.analyst_consensus import fetch as fetch_consensus
        from data.yfinance_client import YFinanceClient
        _yf = YFinanceClient()
        for s in stocks:
            try:
                s["analyst_consensus"] = fetch_consensus(s["symbol"], s.get("current_price"), _yf)
            except Exception as e:
                logger.warning(f"[per_stock] consensus fetch failed for {s.get('symbol')}: {e}")
                s["analyst_consensus"] = None
    except Exception as e:
        logger.warning(f"[per_stock] analyst_consensus module unavailable: {e}")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    logger.info(f"[per_stock] running web-search analysis on {len(stocks)} stocks")
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(analyze_stock, s): s for s in stocks}
        for fut in as_completed(futures):
            stock = futures[fut]
            try:
                text = fut.result()
                stock["analysis"] = text
                # None when the model didn't answer / parse failed — do NOT coerce to a
                # real-looking 5. The wheel's ride/cut gate treats unknown as fail-SAFE
                # (keep), so an API blip can't silently flip a quality name into a forced
                # loss-cut. (5 < the 6 ride-threshold, so a fake 5 == "cut".)
                stock["would_own_score"] = _parse_would_own(text)
            except Exception as e:
                logger.warning(f"[per_stock] {stock['symbol']} failed: {e}")
                stock["analysis"] = ""
                stock["would_own_score"] = None

    # Persist each stock's thesis so the trader's agentic manager has MEMORY of
    # how conviction/risk evolves across scans (not just this single snapshot).
    # Best-effort: a failure here must never break the scan.
    try:
        from trading.thesis_store import upsert_from_scan
        for s in stocks:
            try:
                upsert_from_scan(
                    s["symbol"],
                    would_own=s.get("would_own_score"),
                    analysis_text=s.get("analysis", ""),
                    price=s.get("current_price"),
                    score=s.get("best_score"),
                    sector=s.get("sector"),
                )
            except Exception as e:
                logger.warning(f"[per_stock] thesis upsert failed for {s.get('symbol')}: {e}")
    except Exception as e:
        logger.warning(f"[per_stock] thesis store unavailable: {e}")

    # Re-rank: composite = best_score × (would_own/10) × sqrt(POP)
    # sqrt(POP) gives diminishing weight — 90% POP→0.95, 70%→0.84. Quality
    # still matters most after the 2% gate, but high-POP names aren't
    # outranked by marginally-better-quality names with much lower POP.
    import math
    for s in stocks:
        best = (s.get("strikes") or [{}])[0]
        pop_raw = best.get("POP", 0.70)
        if isinstance(pop_raw, str):
            try:
                pop = float(pop_raw.rstrip("%")) / (100 if "%" in pop_raw else 1)
            except ValueError:
                pop = 0.70
        else:
            try:
                pop = float(pop_raw)
                if pop > 1.5:
                    pop = pop / 100
            except (TypeError, ValueError):
                pop = 0.70
        pop = max(0.0, min(1.0, pop))
        s["pop_used"] = round(pop, 3)
        # Analyst-consensus conviction multiplier (1.0 when coverage is thin /
        # data missing) — down-ranks Hold/Sell names vs comparable Buy names.
        cons = s.get("analyst_consensus") or {}
        cons_mult = cons.get("multiplier", 1.0) or 1.0
        s["consensus_mult"] = round(cons_mult, 3)
        s["composite_rank_score"] = (
            float(s.get("best_score", 0))
            * ((s.get("would_own_score") or 5) / 10.0)   # unknown → neutral 0.5 for RANKING only
            * math.sqrt(pop)
            * cons_mult
        )
    stocks.sort(key=lambda s: -s.get("composite_rank_score", 0))
    logger.info(
        "[per_stock] re-ranked top 5: "
        + ", ".join(
            f"{s['symbol']}(wo={s.get('would_own_score','?')}/10 "
            f"pop={s.get('pop_used','?')} cons×{s.get('consensus_mult',1.0):.2f}"
            f"→{s['composite_rank_score']:.1f})"
            for s in stocks[:5]
        )
    )
    return stocks
