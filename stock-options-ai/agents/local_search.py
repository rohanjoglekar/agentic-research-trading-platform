"""local_search.py — free, self-hosted web-grounded answers with a LOCAL model.

No API key, no cloud quota. DuckDuckGo (via the `ddgs` package) supplies fresh
web/news snippets; a local Ollama model summarizes them. Built as the FREE
fallback for the agents that search the web (the position agent, the per-stock
analyst) for when the Gemini free-tier daily quota is spent: it restores live
web grounding with no Gemini quota and no Claude cost.

Design notes / honest limits:
  * The bottleneck is the LOCAL MODEL (~60-150s on a CPU-only server), NOT the
    search (~1-2s). Use for background / fallback work, never interactive.
  * We do NOT rely on the small model to *decide* to search or to emit tool
    calls (unreliable at 3-8B). Instead this is a DETERMINISTIC augment: always
    fetch snippets for the query, feed them in, ask the model to answer strictly
    from them. Works with any small model because it is pure summarization.
  * Contract mirrors llm_gemini.gemini_generate: returns None on ANY failure
    (no ddgs installed, no results, model down, timeout) so callers FALL BACK
    and this never raises.
"""
from __future__ import annotations

import os
from typing import Optional

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
# llama3.2:3b is the fast (~2GB) default; pass model= (e.g. "qwen2.5-coder:7b"
# or "llama3:8b") for agents that need stronger reasoning and can eat the extra
# latency. Overridable fleet-wide via LOCAL_SEARCH_MODEL.
DEFAULT_MODEL = os.environ.get("LOCAL_SEARCH_MODEL", "llama3.2:3b").strip() or "llama3.2:3b"
MODEL_TIMEOUT = float(os.environ.get("LOCAL_SEARCH_MODEL_TIMEOUT", "180"))

_DEFAULT_SYSTEM = (
    "Answer using the web snippets below. Summarize the most relevant and recent "
    "information they contain — if the exact timeframe asked for isn't present, "
    "give the latest that IS present rather than refusing. Be concise and factual. "
    "Never invent specific numbers, dates, or prices that aren't in the snippets. "
    "Only say you couldn't find anything if the snippets truly don't touch the "
    "topic. This is information, not financial advice."
)


def web_snippets(
    query: str,
    *,
    max_results: int = 5,
    include_news: bool = True,
    region: str = "us-en",
) -> list[dict]:
    """Return [{title, body, url, source, date}] from DuckDuckGo, newest first.

    Fail-safe: returns whatever it gathered ([] on total failure / no ddgs).
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        from ddgs import DDGS  # `pip install ddgs` (successor to duckduckgo-search)
    except Exception:
        return []

    out: list[dict] = []
    try:
        with DDGS() as ddgs:
            if include_news:
                for r in ddgs.news(query, max_results=max_results, region=region):
                    out.append({
                        "title": (r.get("title") or "").strip(),
                        "body": (r.get("body") or "").strip(),
                        "url": r.get("url") or "",
                        "source": r.get("source") or "news",
                        "date": r.get("date") or "",
                    })
            for r in ddgs.text(query, max_results=max_results, region=region):
                out.append({
                    "title": (r.get("title") or "").strip(),
                    "body": (r.get("body") or "").strip(),
                    "url": r.get("href") or "",
                    "source": "web",
                    "date": "",
                })
    except Exception:
        return out  # partial results are still useful
    return out


def format_context(snips: list[dict], *, limit: int = 8, body_chars: int = 280) -> str:
    """Render snippets into a compact, model-friendly context block."""
    lines: list[str] = []
    for s in snips[:limit]:
        head = f"[{s.get('source', '')}"
        if s.get("date"):
            head += f" {s['date']}"
        head += f"] {s.get('title', '')}".rstrip()
        body = (s.get("body") or "").replace("\n", " ").strip()[:body_chars]
        lines.append(f"- {head}\n  {body}")
    return "\n".join(lines)


def web_answer(
    question: str,
    *,
    search_query: Optional[str] = None,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_results: int = 5,
    include_news: bool = True,
    num_predict: int = 400,
    num_ctx: int = 4096,
) -> Optional[str]:
    """Search the web, then have a LOCAL model answer grounded in the snippets.

    `search_query` defaults to `question` — pass a distinct query when the search
    phrasing should differ from the user's question (e.g. inject tickers). Raise
    `num_ctx` when the caller prepends a large context block. Returns the answer
    string, or None on any failure so the caller can fall back.
    """
    import requests

    snips = web_snippets(
        search_query or question, max_results=max_results, include_news=include_news
    )
    if not snips:
        return None

    prompt = (
        f"Web snippets (most recent first):\n{format_context(snips)}\n\n"
        f"Question: {question}\n\nAnswer:"
    )
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": (model or DEFAULT_MODEL),
                "system": system or _DEFAULT_SYSTEM,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "num_ctx": num_ctx, "num_predict": num_predict},
            },
            timeout=MODEL_TIMEOUT,
        )
        r.raise_for_status()
        ans = (r.json().get("response") or "").strip()
        return ans or None
    except Exception:  # noqa: BLE001 — never raise; caller falls back
        return None


def web_answer_with_sources(question: str, **kw) -> Optional[dict]:
    """Like web_answer but returns {'answer', 'sources': [{title,url}]} for UIs
    that show citations. None if no answer could be produced."""
    snips = web_snippets(
        kw.get("search_query") or question,
        max_results=kw.get("max_results", 5),
        include_news=kw.get("include_news", True),
    )
    if not snips:
        return None
    ans = web_answer(question, **kw)
    if not ans:
        return None
    seen, sources = set(), []
    for s in snips:
        u = s.get("url")
        if u and u not in seen:
            seen.add(u)
            sources.append({"title": s.get("title", ""), "url": u})
    return {"answer": ans, "sources": sources[:6]}


if __name__ == "__main__":  # quick manual check: python local_search.py "your question"
    import sys
    q = " ".join(sys.argv[1:]) or "What is the latest news on Apple AAPL?"
    print(web_answer(q) or "(no answer)")
