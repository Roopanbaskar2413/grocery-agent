"""Product matching: a plain grocery-list item name -> one chosen product from search results, or
out-of-stock / no-match. Order of attempts:
1) the product the household chose before (preferred map) -- always checked first, unchanged.
2) LLM disambiguation among the rule-filtered, in-stock candidates, when there's more than one
   and the LLM is available. Same safety posture as agent/gemini.py: free-tier Gemini, short
   timeout, cooldown after errors/rate-limits, and a match never fails because the LLM did --
   ANY failure (no API key, cooldown, network error, bad response, or the model picking something
   outside the candidate list) falls straight through to the rule-based pick below instead of
   crashing or guessing further.
3) rule-based: best fuzzy-match score, then cheapest (the original, only, behavior before this).

GEMINI_API_KEY unset means the LLM step is skipped every time (no network calls, no state) --
behavior is then byte-for-byte identical to the original rule-based-only matcher, which is also
exactly what happens in tests (no API key in the test environment).
"""
import json
import logging
import os
import time
import difflib
from dataclasses import dataclass
from typing import Optional

import httpx

from .models import Product

log = logging.getLogger("agent.matcher")

BLOCKLIST = {"sauce", "ketchup", "puree", "seeds", "chips", "powder", "paste", "masala"}
MIN_SCORE = 0.6

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

SYSTEM = """You are matching a plain grocery-list item name to the single best product from a
list of real search results for that query. Reply with JSON only: {"id": "<the chosen product's
id, exactly as given in the options>"}. Pick the product a household member most likely means by
that plain name -- prefer the plain/generic version over a flavoured, branded-ingredient, or
bulk/multipack variant unless the item name itself asks for one of those."""

SCHEMA = {"type": "OBJECT", "properties": {"id": {"type": "STRING"}}, "required": ["id"]}


def normalize(name: str) -> str:
    n = name.lower().strip()
    return n[:-1] if n.endswith("s") and len(n) > 3 else n


@dataclass
class MatchResult:
    kind: str                    # "match", "out_of_stock", "no_match"
    product: Optional[Product] = None


def _score(item: str, product: Product) -> float:
    item_n = normalize(item)
    words = [normalize(w) for w in product.name.split()]
    best = max(difflib.SequenceMatcher(None, item_n, w).ratio() for w in words)
    full = difflib.SequenceMatcher(None, item_n, normalize(product.name)).ratio()
    return max(best, full)


class GeminiMatcher:
    """Asks Gemini to disambiguate among already rule-filtered, in-stock candidates. `pick()`
    returns a product id, or None for ANY reason at all -- callers always fall back to the
    rule-based pick on None, never crash or guess further. Cooldown is in-memory (process
    lifetime), not DB-persisted like agent/gemini.py's chat budget: losing it on a restart is
    harmless here, since matching is naturally bounded by how many distinct items get searched."""

    def __init__(self, api_key: Optional[str], model: str = "gemini-3.5-flash-lite", timeout: float = 6.0,
                 http: Optional[httpx.Client] = None, base_url: str = GEMINI_BASE,
                 cooldown_seconds: float = 300, now_fn=time.monotonic):
        self.api_key = api_key
        self.url = f"{base_url}/models/{model}:generateContent"
        self.http = http or httpx.Client(timeout=timeout)
        self.cooldown_seconds = cooldown_seconds
        self._now = now_fn
        self._cooldown_until = 0.0

    def _cooldown(self, why: str):
        self._cooldown_until = self._now() + self.cooldown_seconds
        log.warning("matcher LLM cooling down for %ss: %s", self.cooldown_seconds, why)

    def pick(self, item_name: str, products: list[Product]) -> Optional[str]:
        if not self.api_key or len(products) < 2:
            return None  # not configured, or nothing to disambiguate
        if self._now() < self._cooldown_until:
            return None
        options = [{"id": p.id, "name": p.name, "size": p.size, "price": p.price} for p in products]
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps({"item": item_name, "options": options})}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA,
                                 "temperature": 0.0, "maxOutputTokens": 60},
        }
        try:
            r = self.http.post(self.url, json=body, headers={"x-goog-api-key": self.api_key})
        except httpx.HTTPError as e:
            self._cooldown(f"network: {type(e).__name__}")
            return None
        if r.status_code == 429:
            self._cooldown("rate limited (429)")
            return None
        if r.status_code >= 500:
            self._cooldown(f"server error {r.status_code}")
            return None
        if r.status_code >= 400:
            self._cooldown(f"client error {r.status_code}: {r.text[:120]}")
            return None
        try:
            data = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            chosen_id = data.get("id") if isinstance(data, dict) else None
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        valid_ids = {p.id for p in products}
        return chosen_id if chosen_id in valid_ids else None


_default_llm: Optional[GeminiMatcher] = None


def _get_default_llm() -> GeminiMatcher:
    """Lazy singleton, built from GEMINI_API_KEY on first use. Mirrors how run_dev.py constructs
    GeminiIntentParser from the same env var -- no wiring needed in orchestrator.py."""
    global _default_llm
    if _default_llm is None:
        _default_llm = GeminiMatcher(os.environ.get("GEMINI_API_KEY"),
                                     model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"))
    return _default_llm


def match_item(item_name: str, candidates, preferred: Optional[dict] = None,
               llm: Optional[GeminiMatcher] = None) -> MatchResult:
    """`llm` is for tests to inject a fake matcher; production callers (orchestrator.py) call this
    with just the original 3 positional arguments, exactly as before -- `llm=None` here means "use
    the lazy GEMINI_API_KEY-backed singleton", not "disable the LLM step"."""
    item_words = set(item_name.lower().split())
    pool = []
    for p in candidates:
        pwords = set(p.name.lower().split())
        # a blocked word only counts as a mismatch if the list item did not ask for it
        if (pwords & BLOCKLIST) - item_words:
            continue
        pool.append((p, _score(item_name, p)))
    pool = [(p, s) for p, s in pool if s >= MIN_SCORE]
    if not pool:
        return MatchResult("no_match")

    pref_id = (preferred or {}).get(item_name.lower())
    if pref_id:
        for p, _ in pool:
            if p.id == pref_id:
                return MatchResult("match", p) if p.in_stock else MatchResult("out_of_stock", p)

    in_stock_products = [p for p, _ in pool if p.in_stock]
    if len(in_stock_products) > 1:
        matcher = llm if llm is not None else _get_default_llm()
        chosen_id = matcher.pick(item_name, in_stock_products)
        if chosen_id:
            chosen = next((p for p in in_stock_products if p.id == chosen_id), None)
            if chosen:
                return MatchResult("match", chosen)

    pool.sort(key=lambda ps: (-ps[1], ps[0].price))
    in_stock = [(p, s) for p, s in pool if p.in_stock]
    if not in_stock:
        return MatchResult("out_of_stock", pool[0][0])
    return MatchResult("match", in_stock[0][0])
