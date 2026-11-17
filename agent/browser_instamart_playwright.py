"""The real-browser implementation of agent.browser_instamart.InstamartPage, using Playwright.

EVERY SELECTOR BELOW IS AN UNVERIFIED PLACEHOLDER. I have no way to see Instamart's actual page
markup from here. Before this will do anything useful:
  1. Open https://www.swiggy.com/instamart in a real browser, log in, open DevTools.
  2. For each selector in `Selectors` below, find the real one (right-click an element -> Inspect)
     and replace it. Nothing else in this file should need to change.
  3. Expect this to break again whenever Instamart redeploys their frontend -- there is no changelog
     for a UI, unlike the MCP tool schemas in agent/real_instamart.py.

Not imported by agent/browser_instamart.py, run_dev.py, or any test: this is a manual tool, not a
production path. Run it yourself, once Playwright is installed (`pip install playwright && playwright
install chromium`), via a small script like:

    from playwright.sync_api import sync_playwright
    from agent.browser_instamart import BrowserInstamart
    from agent.browser_instamart_playwright import PlaywrightInstamartPage, Selectors

    with sync_playwright() as p:
        # launch_persistent_context keeps your login across runs -- log in by hand once, in this
        # same profile directory, and do not automate that step.
        context = p.chromium.launch_persistent_context("./.instamart_browser_profile", headless=False)
        page = context.new_page()
        store = BrowserInstamart(PlaywrightInstamartPage(page, Selectors()))
        # ... call store.search_products(...), store.update_cart(...), store.get_cart(), etc.
"""
from dataclasses import dataclass


@dataclass
class Selectors:
    """CSS selectors for every element this adapter touches. All placeholders -- fill these in
    against the real site. Keeping them in one dataclass, instead of scattered through the page
    methods below, is the whole point: this is the only place that should need hand-editing.
    """
    base_url: str = "https://www.swiggy.com/instamart"
    search_box: str = "input[data-testid='search-box']"
    search_result_card: str = "[data-testid='product-card']"
    card_name: str = "[data-testid='product-name']"
    card_size: str = "[data-testid='product-size']"
    card_price: str = "[data-testid='product-price']"
    card_out_of_stock: str = "[data-testid='product-sold-out']"
    card_add_button: str = "[data-testid='product-add']"

    cart_icon: str = "[data-testid='cart-icon']"
    cart_line_item: str = "[data-testid='cart-item']"
    cart_line_qty_plus: str = "[data-testid='cart-item-qty-plus']"
    cart_line_qty_minus: str = "[data-testid='cart-item-qty-minus']"
    cart_line_remove: str = "[data-testid='cart-item-remove']"
    cart_subtotal: str = "[data-testid='cart-subtotal']"
    cart_delivery_fee: str = "[data-testid='cart-delivery-fee']"

    payment_cod_option: str = "[data-testid='payment-cod']"
    payment_upi_option: str = "[data-testid='payment-upi']"

    orders_page_url: str = "https://www.swiggy.com/instamart/orders"
    order_row: str = "[data-testid='order-row']"
    order_id_in_row: str = "[data-testid='order-id']"


class PlaywrightInstamartPage:
    """Implements agent.browser_instamart.InstamartPage against a real Playwright `Page`. Duck-typed
    on purpose: no `playwright` import here, so this file never needs the package installed just to
    be imported (only to actually run it with a real page object)."""

    def __init__(self, page, selectors: Selectors = None, nav_timeout_ms: int = 15000):
        self.page = page
        self.sel = selectors or Selectors()
        self.nav_timeout_ms = nav_timeout_ms

    def search(self, query: str) -> list[dict]:
        self.page.goto(self.sel.base_url)
        self.page.fill(self.sel.search_box, query)
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector(self.sel.search_result_card, timeout=self.nav_timeout_ms)
        cards = self.page.query_selector_all(self.sel.search_result_card)
        out = []
        for card in cards:
            pid = card.get_attribute("data-product-id") or self._fallback_id(card)
            name_el = card.query_selector(self.sel.card_name)
            size_el = card.query_selector(self.sel.card_size)
            price_el = card.query_selector(self.sel.card_price)
            out_of_stock = card.query_selector(self.sel.card_out_of_stock) is not None
            out.append({"id": pid, "name": (name_el.text_content() if name_el else "").strip(),
                       "size": (size_el.text_content() if size_el else "").strip(),
                       "price": _parse_rupees(price_el.text_content() if price_el else "0"),
                       "in_stock": not out_of_stock})
        return out

    def _fallback_id(self, card) -> str:
        """If Instamart doesn't expose a stable product id attribute, fall back to a composite of
        whatever text is visible. Less reliable across page reloads -- a real id attribute, if one
        exists in the real markup, should be preferred. Verify this against the live site."""
        name_el = card.query_selector(self.sel.card_name)
        size_el = card.query_selector(self.sel.card_size)
        return f"{(name_el.text_content() if name_el else '').strip()}::{(size_el.text_content() if size_el else '').strip()}"

    def cart_items(self) -> list[dict]:
        self.page.goto(self.sel.base_url)
        self.page.click(self.sel.cart_icon)
        self.page.wait_for_load_state("networkidle", timeout=self.nav_timeout_ms)
        rows = self.page.query_selector_all(self.sel.cart_line_item)
        out = []
        for row in rows:
            pid = row.get_attribute("data-product-id") or self._fallback_id(row)
            qty_el = row.query_selector("[data-testid='cart-item-qty']")
            qty = int((qty_el.text_content() if qty_el else "1").strip() or "1")
            out.append({"id": pid, "qty": qty})
        return out

    def cart_totals(self) -> dict:
        subtotal_el = self.page.query_selector(self.sel.cart_subtotal)
        fee_el = self.page.query_selector(self.sel.cart_delivery_fee)
        return {"subtotal": _parse_rupees(subtotal_el.text_content() if subtotal_el else "0"),
               "delivery_fee": _parse_rupees(fee_el.text_content() if fee_el else "0")}

    def add_item(self, product_id: str, qty: int) -> None:
        card = self._find_card(product_id)
        card.query_selector(self.sel.card_add_button).click()
        for _ in range(qty - 1):
            card.query_selector(self.sel.cart_line_qty_plus).click()

    def set_quantity(self, product_id: str, qty: int) -> None:
        row = self._find_row(product_id)
        current_el = row.query_selector("[data-testid='cart-item-qty']")
        current = int((current_el.text_content() if current_el else "1").strip() or "1")
        button = self.sel.cart_line_qty_plus if qty > current else self.sel.cart_line_qty_minus
        for _ in range(abs(qty - current)):
            row.query_selector(button).click()

    def remove_item(self, product_id: str) -> None:
        row = self._find_row(product_id)
        row.query_selector(self.sel.cart_line_remove).click()

    def _find_card(self, product_id: str):
        for card in self.page.query_selector_all(self.sel.search_result_card):
            if (card.get_attribute("data-product-id") or self._fallback_id(card)) == product_id:
                return card
        raise LookupError(f"product {product_id!r} not found on the current search results page")

    def _find_row(self, product_id: str):
        for row in self.page.query_selector_all(self.sel.cart_line_item):
            if (row.get_attribute("data-product-id") or self._fallback_id(row)) == product_id:
                return row
        raise LookupError(f"product {product_id!r} not found in the cart")

    def payment_options(self) -> list[str]:
        methods = []
        if self.page.query_selector(self.sel.payment_cod_option):
            methods.append("COD")
        if self.page.query_selector(self.sel.payment_upi_option):
            methods.append("UPI")
        return methods

    def orders(self) -> list[dict]:
        self.page.goto(self.sel.orders_page_url)
        self.page.wait_for_load_state("networkidle", timeout=self.nav_timeout_ms)
        rows = self.page.query_selector_all(self.sel.order_row)
        out = []
        for row in rows:
            id_el = row.query_selector(self.sel.order_id_in_row)
            out.append({"id": (id_el.text_content() if id_el else "").strip()})
        return out


def _parse_rupees(text: str) -> int:
    digits = "".join(c for c in text if c.isdigit())
    return int(digits) if digits else 0
