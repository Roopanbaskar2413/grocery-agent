from pathlib import Path

import pytest

from agent.browser_instamart import (ADD_ITEM_VERIFY_ATTEMPTS, BrowserInstamart, InstamartCheckoutPaused,
                                     InstamartMinOrderError, InstamartPageError, PRICE_RECHECK_MAX_ATTEMPTS)


class FakeInstamartPage:
    """A fully in-memory double for agent.browser_instamart.InstamartPage. No browser, no Playwright."""

    def __init__(self):
        self.catalog = {}            # id -> {"name", "size", "price", "in_stock"}
        self._cart = {}              # id -> qty
        self._orders = []
        self._payment_options = ["COD", "UPI"]
        self.totals_script = None    # optional list of {"subtotal","delivery_fee"} dicts, in order
        self._totals_calls = 0
        self.calls = []
        self.add_item_noop_count = 0  # next N add_item calls look like they happened but don't stick

    def search(self, query):
        self.calls.append(("search", query))
        return [dict(id=pid, **data) for pid, data in self.catalog.items()
               if query.lower() in data["name"].lower()]

    def cart_items(self):
        self.calls.append(("cart_items",))
        return [{"id": pid, "qty": qty} for pid, qty in self._cart.items()]

    def cart_totals(self):
        self.calls.append(("cart_totals",))
        if self.totals_script is not None:
            idx = min(self._totals_calls, len(self.totals_script) - 1)
            self._totals_calls += 1
            return self.totals_script[idx]
        subtotal = sum(self.catalog[pid]["price"] * qty for pid, qty in self._cart.items())
        return {"subtotal": subtotal, "delivery_fee": 0}

    def add_item(self, product_id, qty):
        self.calls.append(("add_item", product_id, qty))
        if self.add_item_noop_count > 0:
            self.add_item_noop_count -= 1
            return  # looks like it happened (no error) but never touches self._cart
        self._cart[product_id] = qty

    def set_quantity(self, product_id, qty):
        self.calls.append(("set_quantity", product_id, qty))
        self._cart[product_id] = qty

    def remove_item(self, product_id):
        self.calls.append(("remove_item", product_id))
        self._cart.pop(product_id, None)

    def payment_options(self):
        self.calls.append(("payment_options",))
        return list(self._payment_options)

    def orders(self):
        self.calls.append(("orders",))
        return list(self._orders)


@pytest.fixture
def page():
    p = FakeInstamartPage()
    p.catalog = {
        "p1": {"name": "Milk Toned", "size": "500 ml", "price": 34, "in_stock": True},
        "p2": {"name": "Curd", "size": "400 g", "price": 40, "in_stock": True},
        "p3": {"name": "Eggs", "size": "6 pcs", "price": 54, "in_stock": False},
    }
    return p


@pytest.fixture
def store(page):
    return BrowserInstamart(page, pace_seconds=(0, 0), sleep=lambda s: None)


# ---------------------------------------------------------------- search
def test_search_products_maps_page_results_to_products(store, page):
    products = store.search_products("milk")
    assert len(products) == 1
    p = products[0]
    assert p.id == "p1" and p.name == "Milk Toned" and p.size == "500 ml" and p.price == 34
    assert p.in_stock is True


def test_search_products_reports_out_of_stock(store):
    products = store.search_products("eggs")
    assert products[0].in_stock is False


# ---------------------------------------------------------------- cart read
def test_get_cart_reads_items_and_totals(store, page):
    page._cart = {"p1": 2}
    cart = store.get_cart()
    assert cart.lines == [("p1", 2)]
    assert cart.subtotal == 68 and cart.total == 68


# ---------------------------------------------------------------- update_cart reconciliation
def test_update_cart_adds_missing_items(store, page):
    store.update_cart([("p1", 2), ("p2", 1)])
    assert ("add_item", "p1", 2) in page.calls
    assert ("add_item", "p2", 1) in page.calls


def test_update_cart_adjusts_an_existing_quantity(store, page):
    page._cart = {"p1": 1}
    store.update_cart([("p1", 3)])
    assert ("set_quantity", "p1", 3) in page.calls
    assert not any(c[0] == "add_item" for c in page.calls)


def test_update_cart_removes_items_not_in_the_new_list(store, page):
    page._cart = {"p1": 1, "p2": 1}
    store.update_cart([("p1", 1)])
    assert ("remove_item", "p2") in page.calls
    assert not any(c[0] in ("add_item", "set_quantity") and c[1] == "p1" for c in page.calls)


def test_update_cart_with_no_lines_removes_everything(store, page):
    page._cart = {"p1": 1, "p2": 2}
    store.update_cart([])
    assert {c for c in page.calls if c[0] == "remove_item"} == {("remove_item", "p1"), ("remove_item", "p2")}


def test_update_cart_leaves_an_already_correct_line_untouched(store, page):
    page._cart = {"p1": 2}
    store.update_cart([("p1", 2)])
    assert not any(c[0] in ("add_item", "set_quantity", "remove_item") for c in page.calls)


# ---------------------------------------------------------------- break this on purpose: a confirmed
# add that didn't actually stick (live run: the page's own stepper appeared, but the item was
# missing from the cart afterward anyway)
def test_update_cart_retries_an_add_that_didnt_stick_until_it_verifies(store, page):
    page.add_item_noop_count = 2  # first two add_item calls for p1 are no-ops
    store.update_cart([("p1", 1)])
    assert [c for c in page.calls if c[0] == "add_item"] == [("add_item", "p1", 1)] * 3
    assert page._cart == {"p1": 1}


def test_update_cart_raises_if_an_add_never_verifies(store, page):
    page.add_item_noop_count = 999  # never actually sticks
    with pytest.raises(InstamartPageError):
        store.update_cart([("p1", 1)])
    assert len([c for c in page.calls if c[0] == "add_item"]) == ADD_ITEM_VERIFY_ATTEMPTS
    assert page._cart == {}


# ---------------------------------------------------------------- payment options / orders passthrough
def test_get_payment_options_passes_through(store, page):
    page._payment_options = ["UPI"]
    assert store.get_payment_options() == ["UPI"]


def test_orders_passes_through(store, page):
    page._orders = [{"id": "ORD1"}, {"id": "ORD2"}]
    assert [o["id"] for o in store.orders] == ["ORD1", "ORD2"]


# ---------------------------------------------------------------- checkout never completes, by design
def test_checkout_always_raises_checkout_paused_and_never_places_an_order(store, page):
    page._cart = {"p1": 5}        # 5 * 34 = 170, above the minimum
    with pytest.raises(InstamartCheckoutPaused) as exc:
        store.checkout("COD")
    assert exc.value.cart.total == 170
    assert exc.value.payment == "COD"
    assert page._orders == []     # nothing was ever placed


def test_checkout_below_minimum_order_raises_before_pausing(store, page):
    page._cart = {"p1": 1}        # 34, below the 99 minimum
    with pytest.raises(InstamartMinOrderError):
        store.checkout("COD")


def test_the_page_protocol_has_no_place_order_style_method():
    """Structural guarantee, same spirit as tests/test_api.py's scan of api.py: nothing in this
    module's page protocol or implementation can submit an order, so a future change can't quietly
    wire one in without this test being touched too."""
    src = Path(__file__).resolve().parents[1].joinpath("agent", "browser_instamart.py").read_text()
    for forbidden in ("place_order", "submit_order", "confirm_order", "click_checkout", "pay_now"):
        assert forbidden not in src.lower()


# ---------------------------------------------------------------- break this on purpose: unstable reads
def test_checkout_retries_the_cart_read_until_two_consecutive_reads_agree(store, page):
    page._cart = {"p1": 5}
    page.totals_script = [
        {"subtotal": 170, "delivery_fee": 0},   # read 1: baseline
        {"subtotal": 999, "delivery_fee": 0},   # read 2: still rendering, differs from read 1
        {"subtotal": 999, "delivery_fee": 0},   # read 3: agrees with read 2 -> considered stable
    ]
    with pytest.raises(InstamartCheckoutPaused) as exc:
        store.checkout("COD")
    assert exc.value.cart.total == 999          # trusts the value both reads agreed on, not read 1


def test_checkout_gives_up_if_the_cart_never_settles(store, page):
    page._cart = {"p1": 5}
    # every read disagrees with the last, forever: never two consecutive matches
    page.totals_script = [{"subtotal": 100 + i, "delivery_fee": 0} for i in range(PRICE_RECHECK_MAX_ATTEMPTS + 2)]
    with pytest.raises(InstamartPageError):
        store.checkout("COD")


def test_checkout_uses_the_settled_total_for_the_minimum_order_check_not_a_stale_one(store, page):
    page._cart = {"p1": 3}
    page.totals_script = [
        {"subtotal": 102, "delivery_fee": 0},   # read 1: above minimum
        {"subtotal": 50, "delivery_fee": 0},    # read 2: price dropped, below minimum
        {"subtotal": 50, "delivery_fee": 0},    # read 3: agrees with read 2 -> stable at 50
    ]
    with pytest.raises(InstamartMinOrderError):
        store.checkout("COD")                    # must use the settled 50, not the stale 102


# ---------------------------------------------------------------- pacing
def test_every_page_touching_call_is_paced(store, page):
    sleeps = []
    store._sleep = lambda s: sleeps.append(s)
    store.search_products("milk")
    store.get_cart()
    assert len(sleeps) == 2   # one pace per call, using the injected sleep instead of a real delay
