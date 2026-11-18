"""A third Instamart adapter: drives the real, logged-in Instamart website through a browser,
instead of calling an API. Same interface as MockInstamart and RealInstamart (search_products,
get_cart, update_cart, get_payment_options, checkout, .orders), so orchestrator.py does not change.

This exists to verify the pipeline (fridge trigger -> search -> cart -> price/stock re-check) against
the real site right now, without waiting on Swiggy Builders Club access. It is NOT wired into
run_dev.py or Settings.instamart_backend -- it is a manual verification tool you run yourself, not a
third production backend choice.

Deliberate safety choice, not a limitation to fix later: checkout() NEVER clicks the final
"place order" button. It re-reads price and stock, then raises InstamartCheckoutPaused with the
reconciled cart so a human (you) decides whether to click through in the same open browser tab.
Reasons, in order:
  - checkout is the one action with real account risk (ToS, bot detection) and real money.
  - if the live checkout flow has an OTP / app-confirmation / re-auth step, there is nowhere in this
    interface (checkout() -> order_id, synchronous, no human-in-the-loop) to put "waiting for you."
  - because code never performs the risky action, the "was the order actually placed" ambiguity that
    agent/real_instamart.py resolves with get_orders() does not arise here: you see the result
    directly in the browser when you click it yourself.
search_products / get_cart / update_cart carry essentially no account risk (ordinary browsing), so
those ARE fully automated.

Talks to the page through the small InstamartPage protocol below, not to raw Playwright APIs, so:
  - tests run against FakeInstamartPage, a fully in-memory double -- no browser, no Playwright import,
    fast and deterministic, same spirit as MockInstamart.
  - the one place that holds real, unverified-against-the-live-site CSS selectors is
    PlaywrightInstamartPage, isolated so you only have to edit one spot once you inspect the real page.
"""
import logging
import random
import time
from typing import Optional, Protocol

from .mock_instamart import CartView
from .models import Product

log = logging.getLogger("agent.browser_instamart")

MIN_ORDER_RUPEES = 99                # Instamart's own minimum, same as real_instamart.py
PRICE_RECHECK_MAX_ATTEMPTS = 4       # give up and raise if the cart never settles this many reads
PRICE_RECHECK_GAP_SECONDS = 1.5
ADD_ITEM_VERIFY_ATTEMPTS = 3         # give up and raise if an add never shows up in the real cart
ADD_ITEM_VERIFY_GAP_SECONDS = 5      # a confirmed add was seen taking up to 15s to actually persist


class InstamartMinOrderError(RuntimeError):
    """The cart is below Instamart's own minimum order value. Not a spend-cap condition."""


class InstamartCheckoutPaused(RuntimeError):
    """Raised by every call to checkout(), always. Automation stops here on purpose: the final
    "place order" click is never done in code. `cart` is the freshly re-checked cart it stopped at --
    review it in the still-open browser tab and click through yourself if you want to order it."""
    def __init__(self, cart: CartView, payment: str):
        super().__init__(f"stopped before checkout by design: {payment} cart ready, total "
                         f"₹{cart.total} ({len(cart.lines)} line(s)). Review and place it "
                         "yourself in the browser if you want it for real.")
        self.cart = cart
        self.payment = payment


class InstamartPageError(RuntimeError):
    """The page didn't do what we expected (element not found, stale price between reads,
    navigation failure). Distinct from InstamartCheckoutPaused: this is an automation problem,
    not the deliberate stop before ordering."""


class InstamartPlaceOrderNotImplemented(RuntimeError):
    """Raised when allow_real_orders=True but checkout() reaches the point where it would need to
    click a real final "Place/Confirm Order" button. There is no selector for that button
    anywhere in this codebase -- the live testing that confirmed search, cart, variant modals,
    and COD selection never went this far, on purpose (see the module docstring and
    agent/browser_instamart_playwright.py). This needs its own DevTools inspection round before
    it can be implemented for real; raising here instead of guessing a selector."""


class AddOutcome:
    """One item's result from update_cart()'s add step. `ok=False` means the add never verified
    in the real cart -- never treat this as a success, even though the page itself may have shown
    no error at all."""
    def __init__(self, pid, qty, ok, reason=None):
        self.pid = pid
        self.qty = qty
        self.ok = ok
        self.reason = reason

    def __repr__(self):
        status = "ok" if self.ok else f"FAILED ({self.reason})"
        return f"AddOutcome({self.pid!r}, qty={self.qty}, {status})"

    def __eq__(self, other):
        if not isinstance(other, AddOutcome):
            return NotImplemented
        return (self.pid, self.qty, self.ok) == (other.pid, other.qty, other.ok)


# ---------------------------------------------------------------------- the page protocol
class InstamartPage(Protocol):
    """What BrowserInstamart needs from "the page," whether that's a real browser tab
    (PlaywrightInstamartPage) or a fake one in tests (FakeInstamartPage). Every product reference
    returned by search()/cart_items() is an opaque string id: BrowserInstamart never interprets it,
    just round-trips it back into add_item/set_quantity/remove_item."""

    def search(self, query: str) -> list[dict]:
        """-> [{"id": str, "name": str, "size": str, "price": int, "in_stock": bool}, ...]"""

    def cart_items(self) -> list[dict]:
        """-> [{"id": str, "qty": int}, ...], the cart as it currently stands on the page."""

    def cart_totals(self) -> dict:
        """-> {"subtotal": int, "delivery_fee": int} read from the page's bill summary."""

    def add_item(self, product_id: str, qty: int) -> None: ...
    def set_quantity(self, product_id: str, qty: int) -> None: ...
    def remove_item(self, product_id: str) -> None: ...

    def payment_options(self) -> list[str]:
        """-> normalized subset of ["COD", "UPI"], read from the checkout review page."""

    def address_selected(self) -> bool:
        """-> True if a delivery address is already chosen on the cart page (the footer shows
        "make payment" rather than "select address")."""

    def select_address(self, nickname: str) -> bool:
        """Opens the saved-address picker if needed and clicks the one whose nickname matches
        `nickname` (e.g. "Roopan home"). -> True if a matching address was found and clicked,
        False otherwise. Never raises; checkout() decides what a False means."""

    def go_to_payment(self) -> None:
        """Click the cart footer's "make payment" button, navigating to /payment. Only called
        after address_selected() is True."""

    def select_cod(self) -> None:
        """Select "Pay on Delivery" on the /payment page -- the only payment method this agent
        ever selects, by design. Never clicks anything past this."""

    def orders(self) -> list[dict]:
        """-> [{"id": str, ...}, ...] from the order-history page, most recent last."""


# ---------------------------------------------------------------------- the adapter
class BrowserInstamart:
    def __init__(self, page: InstamartPage, pace_seconds=(0.8, 2.2), sleep=time.sleep,
                 random_fn=random.uniform, allow_real_orders: bool = False,
                 address_nickname: str = "Roopan home"):
        self.page = page
        self.pace_seconds = pace_seconds
        self._sleep = sleep
        self._random = random_fn
        self.calls = []        # action names, for tests (mirrors MockInstamart.calls)
        # Default False everywhere, including browser_pipeline_check.py, which never sets this --
        # checkout() always pauses there, exactly as before. Only production wiring (run_dev.py,
        # when instamart_backend == "browser") may set this True, and even then checkout() raises
        # InstamartPlaceOrderNotImplemented rather than guessing a final "place order" selector.
        self.allow_real_orders = allow_real_orders
        # The saved-address nickname checkout() picks when none is already selected. Default is
        # Roopan's own home address -- configurable since row order on the picker isn't guaranteed
        # stable, so "just click the first one" would be a real address-mixup risk, not a guess
        # worth taking.
        self.address_nickname = address_nickname

    def _pace(self, action: str):
        """A small, randomised delay before every page-touching action, so a sequence of calls
        doesn't read as a bot hammering the site at machine speed. Not an attempt to defeat
        CAPTCHA or fingerprinting -- just not acting inhumanly fast."""
        self.calls.append(action)
        low, high = self.pace_seconds
        self._sleep(self._random(low, high))

    # ---- tools the agent may use, same shapes as MockInstamart / RealInstamart --------------------
    def search_products(self, query: str):
        self._pace("search_products")
        items = self.page.search(query)
        return [Product(it["id"], it["name"], it.get("size", ""), int(it["price"]),
                        bool(it.get("in_stock", True))) for it in items]

    def get_cart(self) -> CartView:
        self._pace("get_cart")
        return self._read_cart()

    def _read_cart(self) -> CartView:
        items = self.page.cart_items()
        totals = self.page.cart_totals()
        lines = [(it["id"], it["qty"]) for it in items]
        return CartView(lines, int(totals.get("subtotal", 0)), int(totals.get("delivery_fee", 0)))

    def update_cart(self, lines) -> list[AddOutcome]:
        """Replaces the whole cart, like the other adapters -- but the UI has no single bulk-set
        action, so this reconciles: add what's missing, adjust quantities that differ, remove
        anything not in the new list.

        One flaky product's add must not abort the rest of the list -- a live run showed a
        specific product's add click doing nothing visible at all within the timeout, which used
        to raise and stop every item after it in the batch. Each add is now attempted and verified
        independently; a failure is recorded, never silently treated as success, and the batch
        keeps going. Returns the list of AddOutcome for every item update_cart tried to add (not
        for removes/quantity-adjusts, which still happen inline and still raise on a real page
        error -- only the "is this specific product even addable right now" case is batch-tolerant)."""
        self._pace("update_cart")
        wanted = {pid: qty for pid, qty in lines if qty > 0}
        current = {it["id"]: it["qty"] for it in self.page.cart_items()}

        for pid in current:
            if pid not in wanted:
                self.page.remove_item(pid)

        results = []
        for pid, qty in wanted.items():
            if pid not in current:
                results.append(self._add_and_verify(pid, qty))
            elif current[pid] != qty:
                self.page.set_quantity(pid, qty)
        return results

    def _add_and_verify(self, pid, qty) -> AddOutcome:
        """The page's own on-screen stepper confirming an add is only a client-side signal -- a
        live run with several items added back to back showed one whose stepper DID appear
        afterwards missing from the cart anyway, while two others timed out outright. Turned out
        to be plain backend latency, not a real failure or bot-detection, for the ones that DO
        eventually land: an isolated one-item test (add once, wait 15s, check once) found the
        item there all along. Give the server a moment (ADD_ITEM_VERIFY_GAP_SECONDS) before each
        check instead of reading the cart back immediately. But some products genuinely never
        land at all (seen live: a product whose add click produced no visible change within the
        timeout, every attempt) -- report that as a failed AddOutcome instead of raising, so one
        bad product doesn't stop update_cart() from trying the rest of the list.

        page.add_item() can also raise outright (seen live: VariantModalRequiresSelectors, when a
        pack-size modal opens and nothing matches the target size/price) -- that's caught here
        too, deliberately broad (any Exception, not an allowlist), because the whole point of this
        method is that NO single product's failure, of any kind, is allowed to crash the batch."""
        last_error = None
        for _ in range(ADD_ITEM_VERIFY_ATTEMPTS):
            try:
                self.page.add_item(pid, qty)
            except Exception as e:
                last_error = str(e)
                continue
            self._sleep(ADD_ITEM_VERIFY_GAP_SECONDS)
            current = {it["id"]: it["qty"] for it in self.page.cart_items()}
            if current.get(pid) == qty:
                return AddOutcome(pid, qty, True)
        reason = last_error or f"could not confirm it was added to the cart after {ADD_ITEM_VERIFY_ATTEMPTS} attempts"
        return AddOutcome(pid, qty, False, reason=reason)


    def get_payment_options(self):
        self._pace("get_payment_options")
        return self.page.payment_options()

    def checkout(self, payment: str) -> str:
        """By default (allow_real_orders=False -- always the case for browser_pipeline_check.py,
        which never sets it), always raises InstamartCheckoutPaused after selecting COD. See the
        module docstring for why. Still does the real safety work first: a fresh,
        stability-checked price/stock read, the minimum-order guard, confirms a delivery address
        is already selected, clicks through to /payment, and selects "Pay on Delivery" -- so what
        you're told to review is the actual confirmed-COD state, not just a re-checked cart total.

        By design this agent only ever uses COD: never cards, wallets, or netbanking. If no
        delivery address is already selected, checkout() now picks one itself -- the saved
        address matching self.address_nickname -- rather than stopping immediately; it only
        raises if that nickname doesn't match any saved address either. There is still no
        selector anywhere in this codebase for a final "Place Order" confirm button on the COD
        flow. If allow_real_orders is True (production wiring only), checkout() reaches the point
        of needing that final click and raises InstamartPlaceOrderNotImplemented instead of
        guessing a selector that was never confirmed live -- see that exception's docstring."""
        if payment != "COD":
            raise ValueError("only COD (pay on delivery) is supported by design")
        self._pace("checkout")
        cart = self._stable_cart()
        if cart.total < MIN_ORDER_RUPEES:
            raise InstamartMinOrderError(
                f"cart total ₹{cart.total} is below Instamart's ₹{MIN_ORDER_RUPEES} minimum order")
        if not self.page.address_selected():
            self.page.select_address(self.address_nickname)
            if not self.page.address_selected():
                raise InstamartPageError(
                    f"no delivery address is selected, and no saved address matched the "
                    f"configured nickname {self.address_nickname!r} -- pick one yourself in the "
                    "browser, or update the configured nickname, then try again"
                )
        self.page.go_to_payment()
        self.page.select_cod()
        if not self.allow_real_orders:
            raise InstamartCheckoutPaused(cart, payment)
        raise InstamartPlaceOrderNotImplemented(
            f"allow_real_orders=True and COD is selected (cart total ₹{cart.total}), but there is "
            "no selector yet for the final Place/Confirm Order button -- this was never gone "
            "through live. Needs a DevTools inspection round before it can be implemented."
        )

    def _stable_cart(self) -> CartView:
        """Re-reads the cart, with a short gap, until two consecutive reads agree on the total --
        a read mid-render can catch a stale or partial total. Raises rather than handing back a
        number that might be wrong right before an order decision, instead of silently trusting
        whatever the last read happened to be."""
        previous = self._read_cart()
        for _ in range(PRICE_RECHECK_MAX_ATTEMPTS - 1):
            self._sleep(PRICE_RECHECK_GAP_SECONDS)
            current = self._read_cart()
            if (current.subtotal, current.delivery_fee) == (previous.subtotal, previous.delivery_fee):
                return current
            previous = current
        raise InstamartPageError(
            f"cart total did not settle after {PRICE_RECHECK_MAX_ATTEMPTS} reads; "
            "the page may still be loading or changing")

    @property
    def orders(self) -> list:
        self._pace("orders")
        return self.page.orders()
