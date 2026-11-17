"""The real-browser implementation of agent.browser_instamart.InstamartPage, using Playwright.

Selectors below are real -- taken from DevTools against the live site (most are data-testid
attributes, which survive CSS/class hashing changes). A few are still unverified placeholders,
called out in the Selectors docstring; CHECKOUT_BUTTON is deliberately left unset (see
agent/browser_instamart.py -- checkout() never clicks it, by design, so nothing failed by not
having it).

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


class VariantModalRequiresSelectors(Exception):
    """Raised when add_item() finds a pack-size variant-picker modal open after clicking the add
    button, but we don't have real selectors for the per-variant "ADD" buttons or the modal's
    "Confirm" button yet. Deliberately refuses to guess-click inside an unknown modal -- fill in
    Selectors.variant_modal_* once you've inspected the real modal markup, then this stops firing."""


@dataclass
class Selectors:
    """CSS selectors for every element this adapter touches, in one place so that's the only spot
    that needs hand-editing when Instamart's markup changes.

    Verified against the live site (confirmed in DevTools):
      search_button, search_input, account_button, cart_button, search_result_card,
      card_add_button, cart_line_qty_plus, cart_line_qty_minus, cart_url

    Still unverified placeholders -- fill these in against the real page/cart markup:
      card_name, card_size, card_price, card_out_of_stock, cart_line_item, cart_line_qty,
      cart_subtotal, cart_delivery_fee, payment_cod_option, payment_upi_option,
      orders_page_url, order_row, order_id_in_row, variant_modal, variant_modal_add_button,
      variant_modal_confirm_button

    checkout_button is deliberately left unset (TODO) -- the store was closed when last checked,
    so it never rendered. agent/browser_instamart.py never clicks it (checkout() always pauses
    before placing an order), so leaving this None breaks nothing; fill it in whenever convenient.
    """
    base_url: str = "https://www.swiggy.com/instamart"
    search_button: str = '[data-testid="search-container"]'
    search_input: str = 'form[data-testid="search-page-header-search-bar"] input[type="search"]'
    account_button: str = '[data-testid="user-account-button"]'
    cart_button: str = '[data-testid="review-cart-button"]'
    cart_url: str = "https://www.instamart.in/cart"

    search_result_card: str = 'a[href^="/p/"]'
    card_name: str = "[data-testid='product-name']"
    card_size: str = "[data-testid='product-size']"
    card_price: str = "[data-testid='product-price']"
    card_out_of_stock: str = "[data-testid='product-sold-out']"
    card_add_button: str = '[data-testid="buttonpair-add"]'

    # Pack-size variant picker: appears instead of adding immediately, for some products. We only
    # know the modal exists, not its real selectors yet -- see VariantModalRequiresSelectors.
    variant_modal: str = "[role='dialog']"
    variant_modal_add_button: str = None
    variant_modal_confirm_button: str = None

    cart_line_item: str = "[data-testid='cart-item']"
    cart_line_qty: str = "[data-testid='cart-item-qty']"
    cart_line_qty_plus: str = '[data-testid="add_buttons_plus"]'
    cart_line_qty_minus: str = '[data-testid="add_buttons_minus"]'
    cart_line_remove: str = "[data-testid='cart-item-remove']"
    cart_subtotal: str = "[data-testid='cart-subtotal']"
    cart_delivery_fee: str = "[data-testid='cart-delivery-fee']"

    checkout_button: str = None  # TODO: unset -- store was closed when last inspected.

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
        # Clicking the search button navigates to a dedicated search page (/search?custom_back=true)
        # where the text input then appears -- it's not a direct field on the home page.
        self.page.click(self.sel.search_button)
        self.page.wait_for_selector(self.sel.search_input, timeout=self.nav_timeout_ms)
        self.page.fill(self.sel.search_input, query)
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector(self.sel.search_result_card, timeout=self.nav_timeout_ms)
        cards = self.page.query_selector_all(self.sel.search_result_card)
        out = []
        for card in cards:
            pid = card.get_attribute("href") or self._fallback_id(card)
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
        """If a card has no href/data-product-id, fall back to a composite of whatever text is
        visible. Less reliable across page reloads -- prefer a real id attribute where one exists."""
        name_el = card.query_selector(self.sel.card_name)
        size_el = card.query_selector(self.sel.card_size)
        return f"{(name_el.text_content() if name_el else '').strip()}::{(size_el.text_content() if size_el else '').strip()}"

    def cart_items(self) -> list[dict]:
        self.page.goto(self.sel.cart_url)
        self.page.wait_for_load_state("networkidle", timeout=self.nav_timeout_ms)
        rows = self.page.query_selector_all(self.sel.cart_line_item)
        out = []
        for row in rows:
            pid = row.get_attribute("data-product-id") or self._fallback_id(row)
            qty_el = row.query_selector(self.sel.cart_line_qty)
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
        self._handle_variant_modal_if_present()
        for _ in range(qty - 1):
            card.query_selector(self.sel.cart_line_qty_plus).click()

    def _handle_variant_modal_if_present(self) -> None:
        """Some products open a pack-size picker modal instead of adding straight to the cart. We
        don't have real selectors for the per-variant "ADD" button or the modal's "Confirm" button
        yet, so refuse to guess-click inside it -- that's exactly the kind of thing that can misfire
        against a real site. Raises a clear error instead of silently doing the wrong thing."""
        modal = self.page.query_selector(self.sel.variant_modal)
        if modal is None:
            return
        if not self.sel.variant_modal_add_button or not self.sel.variant_modal_confirm_button:
            raise VariantModalRequiresSelectors(
                "A variant-picker modal opened after clicking add-to-cart, but "
                "Selectors.variant_modal_add_button / variant_modal_confirm_button aren't filled "
                "in yet. Inspect the modal in DevTools and set those two fields -- not guessing "
                "at them here on purpose."
            )
        modal.query_selector(self.sel.variant_modal_add_button).click()
        modal.query_selector(self.sel.variant_modal_confirm_button).click()

    def set_quantity(self, product_id: str, qty: int) -> None:
        row = self._find_row(product_id)
        current_el = row.query_selector(self.sel.cart_line_qty)
        current = int((current_el.text_content() if current_el else "1").strip() or "1")
        button = self.sel.cart_line_qty_plus if qty > current else self.sel.cart_line_qty_minus
        for _ in range(abs(qty - current)):
            row.query_selector(button).click()

    def remove_item(self, product_id: str) -> None:
        row = self._find_row(product_id)
        row.query_selector(self.sel.cart_line_remove).click()

    def _find_card(self, product_id: str):
        for card in self.page.query_selector_all(self.sel.search_result_card):
            if (card.get_attribute("href") or self._fallback_id(card)) == product_id:
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
