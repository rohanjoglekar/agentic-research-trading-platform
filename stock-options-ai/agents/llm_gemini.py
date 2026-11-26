"""Gemini API helper — text generation with optional Google Search grounding.

Uses the REST API via `requests` (no google-generativeai dependency), matching
the repo's Ollama helper style. Reads GEMINI_API_KEY + GEMINI_MODEL from env.

Design contract (so callers can convert off Claude safely):
  * gemini_available() is False when no key is set → callers FALL BACK to Claude.
  * gemini_generate() NEVER raises — any failure returns None (logged), so a
    caller's `gemini_generate(...) or _claude(...)` pattern degrades cleanly.
  * Free-tier friendly: retries 429/5xx with exponential backoff (the wheel can
    burst several calls per run; backoff keeps us under the RPM limit).
  * use_search=True turns on Google Search grounding (tools:[{google_search:{}}]),
    so the model can read current news rather than answer from memory.
  * The key travels in a header, never in the URL: `requests` puts the full URL
    in its error text, and those errors are logged.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from loguru import logger

_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _model(explicit: Optional[str] = None) -> str:
    return explicit or os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"


def gemini_available() -> bool:
    """True when a GEMINI_API_KEY is configured (so callers can gate on it)."""
    return bool(os.getenv("GEMINI_API_KEY", "").strip())


def gemini_generate(prompt: str, *, model: Optional[str] = None,
                    system: Optional[str] = None, use_search: bool = False,
                    temperature: float = 0.7, max_tokens: int = 1024,
                    timeout: int = 60, max_retries: int = 3,
                    thinking_budget: Optional[int] = 0) -> Optional[str]:
    """Return Gemini's text for `prompt`, or None on missing key / any failure.

    use_search=True enables Google Search grounding (needs a 2.0+/2.5 model).

    thinking_budget (2.5 models): 0 (default) DISABLES the model's internal
    "thinking". This is deliberate for our STRUCTURED-output callers: 2.5-flash's
    thoughts count against maxOutputTokens, and with grounding on they can eat
    the whole budget and TRUNCATE the answer (a 6-line decision cut off mid-line
    → lost fields / parse-fail → needless Claude fallback). Disabling thinking
    guarantees the full structured reply and grounding still works. Pass None to
    use the model default, or a positive budget to allow bounded thinking.
    Never raises — the caller treats None as "provider unavailable" and falls
    back to Claude.
    """
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    try:
        import requests
    except ImportError:                       # requests missing → let caller fall back
        logger.warning("[gemini] requests not installed — cannot call Gemini")
        return None

    mdl = _model(model)
    url = f"{_BASE}/models/{mdl}:generateContent"
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature,
                             "maxOutputTokens": max_tokens},
    }
    if thinking_budget is not None:
        # 2.5-only. thinkingBudget=0 disables thinking so the full token budget
        # goes to the (structured) answer. Harmless-ignored/None-fallback on
        # models that don't support it.
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if use_search:
        # 2.0+/2.5 grounding tool. (1.5 used "google_search_retrieval" — not
        # used here since GEMINI_MODEL defaults to 2.5-flash.)
        body["tools"] = [{"google_search": {}}]

    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                logger.warning(f"[gemini] HTTP {r.status_code} (attempt {attempt}/"
                               f"{max_retries}) — backing off {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                fb = (data.get("promptFeedback") or {}).get("blockReason")
                logger.warning(f"[gemini] no candidates"
                               f"{f' (blocked: {fb})' if fb else ''}")
                return None
            parts = ((cands[0].get("content") or {}).get("parts")) or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
            if not text:
                logger.warning("[gemini] empty text in candidate")
                return None
            return text
        except requests.Timeout:
            logger.warning(f"[gemini] timeout (attempt {attempt}/{max_retries}) — "
                           f"backing off {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
        except Exception as e:                # noqa: BLE001 — never raise to caller
            logger.warning(f"[gemini] error: {type(e).__name__}: {e}")
            return None
    logger.warning("[gemini] exhausted retries — falling back")
    return None
