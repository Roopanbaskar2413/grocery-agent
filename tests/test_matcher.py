"""Matcher tests. Google is replaced by a scripted stand-in (httpx.MockTransport), same pattern as
tests/test_gemini.py: the sandbox cannot reach the real API."""
import json

import httpx
import pytest

from agent.matcher import GeminiMatcher, match_item
from agent.models import Product

P1 = Product("p1", "Tomato Hybrid", "500 g", 30, True)
P2 = Product("p2", "Tomato Ketchup", "200 g", 60, True)
P3 = Product("p3", "Tomato Cherry Small", "250 g", 90, True)
MILK_OUT_OF_STOCK = Product("p4", "Amul Milk", "500 ml", 28, False)


class FakeGoogle:
    """Replies with queued items: a dict (model JSON), or ('status', code), or an exception."""

    def __init__(self):
        self.queue, self.requests = [], []

    def push(self, item):
        self.queue.append(item)

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        item = self.queue.pop(0) if self.queue else {"id": "p1"}
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple) and item[0] == "status":
            return httpx.Response(item[1], text="err")
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(item)}]}}]})


def make_matcher(google, api_key="KEY-123", now=None):
    http = httpx.Client(transport=httpx.MockTransport(google))
    kwargs = {"http": http}
    if now is not None:
        kwargs["now_fn"] = now
    return GeminiMatcher(api_key, **kwargs)


# ---------------------------------------------------------------- match_item: unchanged behavior
def test_match_item_prefers_remembered_product_over_llm_or_rules(tmp_path):
    google = FakeGoogle()
    google.push({"id": "p2"})  # would pick the wrong one if this were ever reached
    llm = make_matcher(google)
    result = match_item("tomato", [P1, P2, P3], preferred={"tomato": "p3"}, llm=llm)
    assert result.kind == "match" and result.product.id == "p3"
    assert google.requests == []  # preferred short-circuits before the LLM is ever called


def test_match_item_skips_llm_entirely_with_no_api_key(tmp_path):
    """No GEMINI_API_KEY -- falls back to the exact original rule-based pick, no network calls."""
    llm = GeminiMatcher(api_key=None)
    result = match_item("tomato", [P1, P2, P3], llm=llm)
    assert result.kind == "match"
    # rule-based: Ketchup is blocklisted (tomato doesn't ask for ketchup), so it's between P1/P3 --
    # same fuzzy score path picks P1 (cheaper) over P3, same as the original matcher always did.
    assert result.product.id == "p1"


def test_match_item_uses_the_llm_pick_when_available_and_valid(tmp_path):
    google = FakeGoogle()
    google.push({"id": "p3"})
    llm = make_matcher(google)
    result = match_item("tomato", [P1, P2, P3], llm=llm)
    assert result.kind == "match" and result.product.id == "p3"
    assert len(google.requests) == 1


def test_match_item_falls_back_to_rules_when_llm_picks_an_id_outside_the_pool(tmp_path):
    google = FakeGoogle()
    google.push({"id": "not-a-real-id"})
    llm = make_matcher(google)
    result = match_item("tomato", [P1, P2, P3], llm=llm)
    assert result.kind == "match" and result.product.id == "p1"  # same rule-based fallback as above


def test_match_item_never_calls_the_llm_with_only_one_in_stock_candidate(tmp_path):
    google = FakeGoogle()
    llm = make_matcher(google)
    match_item("tomato", [P1], llm=llm)
    assert google.requests == []  # nothing to disambiguate


def test_match_item_out_of_stock_and_no_match_unaffected_by_llm(tmp_path):
    google = FakeGoogle()
    llm = make_matcher(google)
    assert match_item("milk", [MILK_OUT_OF_STOCK], llm=llm).kind == "out_of_stock"
    assert match_item("quinoa", [P1, P2, P3], llm=llm).kind == "no_match"


# ---------------------------------------------------------------- GeminiMatcher.pick(): safety posture
def test_pick_returns_none_and_never_raises_on_network_error():
    google = FakeGoogle()
    google.push(httpx.ConnectError("boom"))
    llm = make_matcher(google)
    assert llm.pick("tomato", [P1, P2, P3]) is None


def test_pick_returns_none_on_rate_limit_and_cools_down(tmp_path):
    clock = {"t": 0.0}
    google = FakeGoogle()
    google.push(("status", 429))
    llm = make_matcher(google, now=lambda: clock["t"])
    assert llm.pick("tomato", [P1, P2, P3]) is None
    assert len(google.requests) == 1
    # still cooling down -- a second call must not hit the network again
    clock["t"] += 1
    assert llm.pick("tomato", [P1, P2, P3]) is None
    assert len(google.requests) == 1
    # cooldown expired -- call reaches the network again
    clock["t"] += llm.cooldown_seconds + 1
    assert llm.pick("tomato", [P1, P2, P3]) == "p1"
    assert len(google.requests) == 2


def test_pick_returns_none_for_a_chosen_id_outside_the_given_products():
    google = FakeGoogle()
    google.push({"id": "nonexistent"})
    llm = make_matcher(google)
    assert llm.pick("tomato", [P1, P2, P3]) is None


def test_pick_returns_none_for_unusable_response_shapes():
    google = FakeGoogle()
    google.push({"not_id_at_all": "p1"})
    llm = make_matcher(google)
    assert llm.pick("tomato", [P1, P2, P3]) is None
