"""Tests for agents/llm_gemini.py — no network (requests is monkeypatched)."""
import agents.llm_gemini as g


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _ok_payload(text="hello from gemini"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert g.gemini_available() is False
    assert g.gemini_generate("hi") is None


def test_generate_happy_path(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key-123")
    calls = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        calls["body"] = json
        return _Resp(200, _ok_payload("ok text"))

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    out = g.gemini_generate("question", use_search=True, system="be terse")
    assert out == "ok text"
    assert "generateContent" in calls["url"]
    # the key rides in a header: a URL ends up in logged error text, a header does not
    assert calls["headers"] == {"x-goog-api-key": "secret-key-123"}
    assert "secret-key-123" not in calls["url"]
    assert calls["body"]["tools"] == [{"google_search": {}}]          # grounding on
    assert calls["body"]["systemInstruction"]["parts"][0]["text"] == "be terse"


def test_thinking_disabled_by_default(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["body"] = json
        return _Resp(200, _ok_payload())

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    g.gemini_generate("q")                                    # default thinking_budget=0
    assert seen["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    # explicit None → no thinkingConfig (use model default)
    g.gemini_generate("q", thinking_budget=None)
    assert "thinkingConfig" not in seen["body"]["generationConfig"]


def test_no_search_omits_tools(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["body"] = json
        return _Resp(200, _ok_payload())

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    g.gemini_generate("q")
    assert "tools" not in seen["body"]


def test_429_backs_off_then_succeeds(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(g.time, "sleep", lambda *_: None)          # no real delay
    seq = [_Resp(429), _Resp(429), _Resp(200, _ok_payload("after backoff"))]

    def fake_post(url, headers=None, json=None, timeout=None):
        return seq.pop(0)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert g.gemini_generate("q", max_retries=3) == "after backoff"


def test_exhausted_retries_returns_none(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(g.time, "sleep", lambda *_: None)

    def fake_post(url, headers=None, json=None, timeout=None):
        return _Resp(503)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert g.gemini_generate("q", max_retries=2) is None


def test_blocked_or_empty_returns_none(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    def fake_post(url, headers=None, json=None, timeout=None):
        return _Resp(200, {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert g.gemini_generate("q") is None


def test_exception_never_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    def fake_post(url, headers=None, json=None, timeout=None):
        raise ValueError("boom")

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert g.gemini_generate("q") is None                         # swallowed
