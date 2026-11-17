import json
from datetime import datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from agent.config import Settings
from agent.oauth import OAuthClient, TokenSet, TokenStore
from agent.real_instamart import InstamartMinOrderError, InstamartOrderError, RealInstamart

ADDR = {"addresses": [{"id": "addr-1", "addressCategory": "Home"}],
        "resolution": {"defaultAddressId": "addr-1"}}


class FakeInstamartMcp:
    """A scriptable stand-in for mcp.swiggy.com/im. Each tool name maps to a queue of responses;
    a response is either a dict result or an httpx.Response/Exception to raise for that call."""

    def __init__(self):
        self.calls = []             # (tool_name, arguments)
        self.queued = {}            # tool_name -> list of dict results (consumed in order)
        self.raise_next = {}        # tool_name -> list of exceptions/httpx.Response to use instead

    def queue(self, tool_name, result):
        self.queued.setdefault(tool_name, []).append(result)

    def fail_next(self, tool_name, response_or_exc):
        self.raise_next.setdefault(tool_name, []).append(response_or_exc)

    def handler(self, request: httpx.Request):
        body = json.loads(request.content)
        name = body["params"]["name"]
        args = body["params"]["arguments"]
        self.calls.append((name, args))
        if self.raise_next.get(name):
            bad = self.raise_next[name].pop(0)
            if isinstance(bad, Exception):
                raise bad
            return bad
        result = self.queued.get(name, [{}]).pop(0) if self.queued.get(name) else self._default(name, args)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": {"structuredContent": result}})

    def _default(self, name, args):
        if name == "get_addresses":
            return ADDR
        if name == "get_orders":
            return {"orders": []}
        return {}


@pytest.fixture
def key():
    return Fernet.generate_key().decode()


@pytest.fixture
def mcp():
    return FakeInstamartMcp()


@pytest.fixture
def oauth(tmp_path, key):
    store = TokenStore(str(tmp_path / "tok.bin"), key=key)
    store.save(TokenSet("acc-1", "ref-1", datetime.now() + timedelta(days=1)))
    return OAuthClient(store, "http://localhost/callback")


@pytest.fixture
def real(oauth, mcp):
    http = httpx.Client(transport=httpx.MockTransport(mcp.handler))
    return RealInstamart(oauth, cfg=Settings(), http=http)


# ---------------------------------------------------------------- refuses to run without a token
def test_refuses_to_construct_with_no_stored_token(tmp_path, key):
    store = TokenStore(str(tmp_path / "empty.bin"), key=key)
    oauth = OAuthClient(store, "http://localhost/callback")
    with pytest.raises(RuntimeError):
        RealInstamart(oauth)


# ---------------------------------------------------------------- address resolution
def test_resolves_the_default_address_once_and_caches_it(real, mcp):
    real.search_products("milk")
    real.search_products("curd")
    address_calls = [c for c in mcp.calls if c[0] == "get_addresses"]
    assert len(address_calls) == 1


def test_falls_back_to_home_address_when_no_default(real, mcp):
    mcp.queue("get_addresses", {"addresses": [{"id": "a1", "addressCategory": "Work"},
                                               {"id": "a2", "addressCategory": "Home"}], "resolution": {}})
    assert real._resolve_address() == "a2"


def test_raises_when_the_account_has_no_saved_address(real, mcp):
    mcp.queue("get_addresses", {"addresses": [], "resolution": {}})
    with pytest.raises(RuntimeError):
        real._resolve_address()


# ---------------------------------------------------------------- search -> spinId:skuId mapping
def test_search_products_maps_variations_to_product_ids(real, mcp):
    mcp.queue("search_products", {"products": [
        {"name": "Milk", "variations": [
            {"spinId": "s1", "skuId": "k1", "name": "Milk 500ml", "unit": "500 ml", "price": 34,
             "isInStockAndAvailable": True},
            {"spinId": "s1", "skuId": "k2", "name": "Milk 1L", "unit": "1 L", "price": 60,
             "isInStockAndAvailable": False},
        ]}]})
    products = real.search_products("milk")
    assert [p.id for p in products] == ["s1:k1", "s1:k2"]
    assert products[0].price == 34 and products[0].in_stock is True
    assert products[1].in_stock is False


def test_search_products_tolerates_a_flat_shape(real, mcp):
    mcp.queue("search_products", {"products": [
        {"spinId": "s9", "skuId": "k9", "name": "Eggs", "price": 54, "isInStockAndAvailable": True}]})
    products = real.search_products("eggs")
    assert products[0].id == "s9:k9"


# ---------------------------------------------------------------- cart round trip
def test_update_cart_sends_spin_and_sku_ids_with_the_address(real, mcp):
    real.update_cart([("s1:k1", 2), ("s2:k2", 1)])
    name, args = [c for c in mcp.calls if c[0] == "update_cart"][0]
    assert args["selectedAddressId"] == "addr-1"
    assert args["items"] == [{"spinId": "s1", "skuId": "k1", "quantity": 2},
                             {"spinId": "s2", "skuId": "k2", "quantity": 1}]


def test_update_cart_with_no_lines_clears_the_cart_instead(real, mcp):
    real.update_cart([])
    assert mcp.calls[-1][0] == "clear_cart"


def test_get_cart_reads_total_and_delivery_fee_from_bill_breakdown(real, mcp):
    mcp.queue("get_cart", {"items": [{"spinId": "s1", "skuId": "k1", "quantity": 2}],
                          "billBreakdown": {"toPay": {"value": 68}, "deliveryFee": {"value": 25}}})
    cart = real.get_cart()
    assert cart.total == 68 and cart.delivery_fee == 25 and cart.subtotal == 43
    assert cart.lines == [("s1:k1", 2)]


# ---------------------------------------------------------------- payment options
def test_get_payment_options_normalizes_cod_and_upi(real, mcp):
    mcp.queue("get_payment_options", {"cod": True, "platforms": {"mobile": {"methods": []}}})
    assert set(real.get_payment_options()) == {"COD", "UPI"}


def test_get_payment_options_without_cod_available(real, mcp):
    mcp.queue("get_payment_options", {"cod": False, "platforms": {"mobile": {}}})
    assert real.get_payment_options() == ["UPI"]


# ---------------------------------------------------------------- minimum order, separate from spend cap
def test_checkout_below_minimum_order_raises_before_calling_checkout(real, mcp):
    mcp.queue("get_cart", {"items": [{"spinId": "s1", "skuId": "k1", "quantity": 1}],
                          "billBreakdown": {"toPay": {"value": 50}}})
    with pytest.raises(InstamartMinOrderError):
        real.checkout("COD")
    assert all(c[0] != "checkout" for c in mcp.calls)


# ---------------------------------------------------------------- checkout happy path and payment setting
def test_checkout_uses_the_configured_cod_payment_string(oauth, mcp):
    http = httpx.Client(transport=httpx.MockTransport(mcp.handler))
    real = RealInstamart(oauth, cfg=Settings(instamart_cod_payment_value="Cash"), http=http)
    mcp.queue("get_cart", {"items": [{"spinId": "s1", "skuId": "k1", "quantity": 1}],
                          "billBreakdown": {"toPay": {"value": 500}}})
    mcp.queue("checkout", {"orderId": "ORD1"})
    order_id = real.checkout("COD")
    assert order_id == "ORD1"
    name, args = [c for c in mcp.calls if c[0] == "checkout"][0]
    assert args["paymentMethod"] == "Cash"


def test_checkout_rejects_non_cod_payment(real):
    with pytest.raises(RuntimeError):
        real.checkout("UPI")


# ---------------------------------------------------------------- non-idempotent checkout
def test_checkout_5xx_with_no_new_order_raises_and_does_not_retry(real, mcp):
    mcp.queue("get_cart", {"items": [{"spinId": "s1", "skuId": "k1", "quantity": 1}],
                          "billBreakdown": {"toPay": {"value": 500}}})
    mcp.fail_next("checkout", httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(InstamartOrderError):
        real.checkout("COD")
    # confirmed no order was placed by calling get_orders, and nothing retried checkout
    assert [c[0] for c in mcp.calls].count("checkout") == 1
    assert any(c[0] == "get_orders" for c in mcp.calls)


def test_checkout_5xx_but_order_actually_placed_is_detected_via_get_orders(real, mcp):
    mcp.queue("get_cart", {"items": [{"spinId": "s1", "skuId": "k1", "quantity": 1}],
                          "billBreakdown": {"toPay": {"value": 500}}})
    mcp.queue("get_orders", {"orders": []})                           # snapshot taken before checkout
    mcp.fail_next("checkout", httpx.Response(503, json={"error": "boom"}))
    mcp.queue("get_orders", {"orders": [{"orderId": "ORD9"}]})        # after the failed call, one exists
    order_id = real.checkout("COD")
    assert order_id == "ORD9"


def test_checkout_timeout_is_treated_as_ambiguous_too(real, mcp):
    mcp.queue("get_cart", {"items": [{"spinId": "s1", "skuId": "k1", "quantity": 1}],
                          "billBreakdown": {"toPay": {"value": 500}}})
    mcp.fail_next("checkout", httpx.TimeoutException("timed out"))
    with pytest.raises(InstamartOrderError):
        real.checkout("COD")


def test_checkout_clean_4xx_does_not_trigger_the_ambiguous_dance(real, mcp):
    mcp.queue("get_cart", {"items": [{"spinId": "s1", "skuId": "k1", "quantity": 1}],
                          "billBreakdown": {"toPay": {"value": 500}}})
    mcp.fail_next("checkout", httpx.Response(400, json={"error": "bad paymentMethod"}))
    with pytest.raises(httpx.HTTPStatusError):
        real.checkout("COD")
    # get_orders is read once as the "before" snapshot ahead of every checkout attempt, but a clean
    # 4xx is not ambiguous, so there must be no second ("after") get_orders call confirming anything.
    assert [c[0] for c in mcp.calls].count("get_orders") == 1


# ---------------------------------------------------------------- orders property (polled by the orchestrator)
def test_orders_property_reflects_get_orders(real, mcp):
    mcp.queue("get_orders", {"orders": [{"orderId": "ORD1"}, {"orderId": "ORD2"}]})
    orders = real.orders
    assert [o["id"] for o in orders] == ["ORD1", "ORD2"]


# ---------------------------------------------------------------- backend selection stays mock by default
def test_settings_default_backend_is_mock():
    assert Settings().instamart_backend == "mock"
