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
import re
from dataclasses import dataclass


SEARCH_RESULT_POLL_MS = 30000  # how long to keep polling for the real result grid to settle


class VariantModalRequiresSelectors(Exception):
    """Raised when add_item() finds a pack-size variant-picker modal open after clicking the add
    button, but can't confidently pick an option: either Selectors.variant_modal_add_button /
    variant_modal_confirm_button aren't filled in yet, or none of the modal's option rows match
    the requested product's size/price. Deliberately refuses to guess which variant to click."""


@dataclass
class Selectors:
    """CSS selectors for every element this adapter touches, in one place so that's the only spot
    that needs hand-editing when Instamart's markup changes.

    Verified against the live site (confirmed in DevTools):
      search_button, search_input, account_button, cart_button, search_result_card,
      card_add_button, cart_line_qty_plus, cart_line_qty_minus, cart_url,
      variant_modal_add_button, variant_modal_confirm_button

    Still unverified placeholders -- fill these in against the real page/cart markup:
      card_size, card_out_of_stock, cart_subtotal, cart_delivery_fee, payment_cod_option,
      payment_upi_option, orders_page_url, order_row, order_id_in_row, variant_modal

    cart_line_item ([role="listitem"]) is a real attribute but a loose one -- it may also match
    unrelated list items elsewhere on the page (e.g. a "did you forget?" carousel). We filter to
    rows that have cart_line_name, which should rule those out, but verify this against the real
    page once you can see one with a non-product listitem nearby.

    cart row price has no selector at all (same hashed-class problem as the product card) -- read
    via a ₹<digits> regex over the row's own text instead, taking the LAST match so a
    struck-through original price doesn't win over the real discounted one (see _row_price).
    cart_subtotal has no confirmed selector yet either, so cart_totals() falls back to summing
    each row's own price when it's missing -- self-consistent, but unverified against a real
    subtotal line (the "store unserviceable" banner was hiding it when last checked).

    card name and price have no selector at all -- their text divs only have hashed CSS classes
    with no stable hook, so PlaywrightInstamartPage reads the name off the card's <img alt=...>
    and the price via a ₹<digits> regex over the card's visible text instead (see _card_name /
    _card_price below).

    checkout_button is deliberately left unset (TODO) -- the store was closed when last checked,
    so it never rendered. agent/browser_instamart.py never clicks it (checkout() always pauses
    before placing an order), so leaving this None breaks nothing; fill it in whenever convenient.
    """
    base_url: str = "https://www.instamart.in"
    search_button: str = '[data-testid="search-container"]'
    search_input: str = 'form[data-testid="search-page-header-search-bar"] input[type="search"]'
    account_button: str = '[data-testid="user-account-button"]'
    cart_button: str = '[data-testid="review-cart-button"]'
    cart_url: str = "https://www.instamart.in/cart"

    search_result_card: str = '[data-testid="item-collection-card-full"]'
    card_size: str = "[data-testid='product-size']"
    card_out_of_stock: str = "[data-testid='product-sold-out']"
    card_add_button: str = '[data-testid="buttonpair-add"]'
    # Once a product is already in the cart, its search card shows a stepper instead of a plain
    # add button -- this is the stepper's own quantity number, a DIFFERENT testid than the cart
    # page's add_buttons_center. Excluded from card price detection (see _card_price).
    card_qty_count: str = '[data-testid="buttonpair-count"]'

    # Pack-size variant picker: appears instead of adding immediately, for some products.
    # variant_modal is still an unverified heuristic (ARIA dialog role); the two add/confirm
    # selectors below are real. variant_modal_add_button's testid repeats once per option row in
    # the modal, so it's always scoped to a single matched row -- see _handle_variant_modal_if_present.
    variant_modal: str = "[role='dialog']"
    variant_modal_add_button: str = '[data-testid="add_buttons_center"]'
    variant_modal_confirm_button: str = '[data-testid="InstamartItemCustomizationWidget-cta"]'

    cart_line_item: str = '[role="listitem"]'
    cart_line_name: str = '[data-testid="cart-item-name"]'
    cart_line_size: str = '[data-testid="cart-item-quantity"]'  # really the pack size, not the qty
    cart_line_qty: str = '[data-testid="add_buttons_center"]'
    cart_line_qty_plus: str = '[data-testid="add_buttons_plus"]'
    cart_line_qty_minus: str = '[data-testid="add_buttons_minus"]'
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

    def __init__(self, page, selectors: Selectors = None, nav_timeout_ms: int = 15000, debug: bool = False):
        self.page = page
        self.sel = selectors or Selectors()
        self.nav_timeout_ms = nav_timeout_ms
        self.debug = debug

    def search(self, query: str) -> list[dict]:
        cards = self._run_search(query)
        if self.debug:
            print(f"    [debug] raw cards matching search_result_card: {len(cards)}")
        out = []
        for card in cards:
            name = self._card_name(card)
            container = self._card_container(card)
            price = self._card_price(container)
            if self.debug:
                print(f"    [debug] card name={name!r} price={price!r}")
            if name is None or price is None:
                continue  # can't trust this card without both -- skip rather than guess
            size_el = container.query_selector(self.sel.card_size)
            out_of_stock = container.query_selector(self.sel.card_out_of_stock) is not None
            out.append({"id": self._card_id(name), "name": name,
                       "size": (size_el.text_content() if size_el else "").strip(),
                       "price": price, "in_stock": not out_of_stock})
        return out

    def _run_search(self, query: str) -> list:
        """Navigate to a fresh search for `query` and return the raw result-card elements. Used
        by search() directly, and by _find_card() too -- update_cart() calls cart_items() (which
        navigates to the cart page) before add_item(), so by the time a card needs to be located
        the browser is very likely NOT sitting on that product's search results anymore. Re-running
        the search instead of assuming the current page already has it fixed a live crash where
        add_item looked for a card on the cart page and found nothing."""
        self.page.goto(self.sel.base_url)
        # Clicking the search button navigates to a dedicated search page (/search?custom_back=true)
        # where the text input then appears -- it's not a direct field on the home page.
        self.page.click(self.sel.search_button)
        self.page.wait_for_selector(self.sel.search_input, timeout=self.nav_timeout_ms)
        self.page.fill(self.sel.search_input, query)
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector(self.sel.search_result_card, timeout=self.nav_timeout_ms)
        # wait_for_selector resolving doesn't mean the result list has finished rendering -- seen
        # it resolve on a transient/early card while the real grid takes much longer (tens of
        # seconds observed live) to actually show up. Poll on a separate, longer budget than the
        # nav timeout rather than trusting one quick read.
        cards = self.page.query_selector_all(self.sel.search_result_card)
        elapsed_ms = 0
        poll_ms = 500
        while not cards and elapsed_ms < SEARCH_RESULT_POLL_MS:
            self.page.wait_for_timeout(poll_ms)
            elapsed_ms += poll_ms
            cards = self.page.query_selector_all(self.sel.search_result_card)
        return cards

    def _card_container(self, card):
        """search_result_card ([data-testid="item-collection-card-full"]) matches only the <a>
        wrapping the image/name -- price, discount badge, and out-of-stock badge live in a
        SIBLING div under the same parent, not inside that <a> at all (confirmed in DevTools: both
        are direct children of the same wrapping div). Climb to that parent to search for them."""
        handle = card.evaluate_handle("el => el.parentElement")
        parent = handle.as_element()
        return parent if parent is not None else card

    def _card_name(self, card) -> str | None:
        """The card's name text div only has a hashed CSS class, no stable hook -- read the
        product name off the <img>'s alt text instead (confirmed in DevTools to hold the full
        name, e.g. "7 Up Zero Sugar soft Drink PET")."""
        img = card.query_selector("img")
        alt = img.get_attribute("alt") if img else None
        return alt.strip() if alt else None

    def _rupee_amounts(self, element, exclude_selector: str = None) -> list[dict]:
        """The ₹ glyph in front of a price isn't a plain ₹ (U+20B9) text character -- DevTools
        showed it's drawn by an icon font (what prints as garbled boxes when you copy it), and
        checking computed ::before content for it turned out unreliable too (the element DevTools
        had selected was a stale/recycled virtualized-list node by the time it got checked).
        So: don't look for the symbol at all. A price div's own text node is just the bare digits
        ("45", not "₹45", confirmed in DevTools), with nothing else in it -- unlike a size/weight
        label ("430 g"), which always has a unit attached in the same text node. Walk every leaf
        descendant and keep the ones whose trimmed text is *purely* digits; that's specific enough
        to rule out size/weight without needing to find the currency symbol at all.

        But a cart row ALSO has a quantity counter ("1") that is itself a pure-digit leaf -- a
        live run picked that up as the price. `exclude_selector` (e.g. cart_line_qty) rules that
        out; callers with no such competing element (the product card, pre-add) pass None."""
        return element.evaluate("""(root, excludeSelector) => {
            const out = [];
            for (const el of root.querySelectorAll('*')) {
                if (el.children.length > 0) continue;  // leaves only
                if (excludeSelector && el.matches(excludeSelector)) continue;
                const text = (el.textContent || '').trim();
                if (!/^\\d+$/.test(text)) continue;
                const struck = window.getComputedStyle(el).textDecorationLine.includes('line-through');
                out.push({amount: parseInt(text, 10), struck});
            }
            return out;
        }""", exclude_selector)

    def _card_price(self, card) -> int | None:
        """Prefer a non-struck-through amount (the real/current price); fall back to whatever's
        there if every amount found happens to be struck through. Excludes card_qty_count: a
        live run confirmed the search card's own stepper number (shown once the product is
        already in the cart) uses a different testid than the cart row's, and still got picked
        up as the price until this was added too."""
        amounts = self._rupee_amounts(card, self.sel.card_qty_count)
        live = [a["amount"] for a in amounts if not a["struck"]]
        if live:
            return live[0]
        return amounts[0]["amount"] if amounts else None

    def _card_id(self, name: str) -> str:
        """Just the name -- a real product-page href would be more stable as an id, but search_result_card
        already matches the <a> itself (not a wrapper around it), so there's no nested link to pull a
        href from without self-referencing. More importantly, _find_card() re-searches by typing the
        id into the search box (see _run_search), so the id needs to be real searchable text anyway --
        an href wouldn't work there even if we could get one. Two same-named variants would collide,
        a known gap (see README)."""
        return name

    def _fallback_id(self, row) -> str:
        """Cart-row id -- just the name, same as _card_id. This MUST match the search card's id
        scheme: update_cart() diffs `current` (built from this) against `wanted` (built from
        _card_id) by exact key equality. A live run showed what happens when they don't match --
        every item looked "not in current" and "not in wanted" at once, so update_cart removed
        and immediately re-added everything on every single call instead of diffing incrementally,
        which is suspected to have caused several items to go missing on a 4-item run (racing a
        remove against a re-add instead of leaving an already-correct line untouched)."""
        name_el = row.query_selector(self.sel.cart_line_name)
        return (name_el.text_content() if name_el else "").strip()

    def _cart_rows(self):
        """[role="listitem"] can match unrelated list items elsewhere on the page -- keep only the
        ones that look like an actual product row (have the real cart-item-name testid)."""
        rows = self.page.query_selector_all(self.sel.cart_line_item)
        return [r for r in rows if r.query_selector(self.sel.cart_line_name) is not None]

    def _row_price(self, row) -> int | None:
        """The pure-digit-leaf approach that works for the product card kept failing here -- two
        different live runs both returned a wrong but identical value (first "1", then "2") for
        two different products, which smelled like something shared leaking into both rows rather
        than each row's own price. DevTools found something much better anyway: each cart row has
        an aria-hidden accessibility span with the full sentence (e.g. "Arokya Full Cream Milk,
        500 ml, quantity 1, price 43 rupees") -- textContent() picks it up regardless of the
        aria-hidden attribute, so just regex the number straight out of that instead of hunting
        leaf elements at all."""
        match = re.search(r"price\s+(\d+)\s+rupees?", row.text_content() or "", re.IGNORECASE)
        return int(match.group(1)) if match else None

    def cart_items(self) -> list[dict]:
        self.page.goto(self.sel.cart_url)
        self.page.wait_for_load_state("networkidle", timeout=self.nav_timeout_ms)
        rows = self._cart_rows()
        if self.debug:
            print(f"    [debug] cart rows found: {len(rows)}")
        out = []
        for row in rows:
            qty_el = row.query_selector(self.sel.cart_line_qty)
            qty = int((qty_el.text_content() if qty_el else "1").strip() or "1")
            fid = self._fallback_id(row)
            if self.debug:
                print(f"    [debug] cart row id={fid!r} qty={qty}")
            out.append({"id": fid, "qty": qty})
        return out

    def cart_totals(self) -> dict:
        subtotal_el = self.page.query_selector(self.sel.cart_subtotal)
        if subtotal_el is not None:
            subtotal = _parse_rupees(subtotal_el.text_content())
        else:
            # No confirmed subtotal selector yet -- fall back to summing each row's own price.
            rows = self._cart_rows()
            prices = []
            for row in rows:
                p = self._row_price(row)
                if self.debug:
                    print(f"    [debug] cart row price={p!r}")
                prices.append(p or 0)
            subtotal = sum(prices)
        fee_el = self.page.query_selector(self.sel.cart_delivery_fee)
        delivery_fee = _parse_rupees(fee_el.text_content()) if fee_el else 0
        return {"subtotal": subtotal, "delivery_fee": delivery_fee}

    def add_item(self, product_id: str, qty: int) -> None:
        card = self._find_card(product_id)
        card.query_selector(self.sel.card_add_button).click()
        self._handle_variant_modal_if_present(card)
        # Wait for the card's own stepper to confirm the add before returning. A live run with
        # several items added in a row (one search+add per item, each a full page navigation)
        # silently lost most of them with no error -- the next item's navigation most likely
        # canceled this add's network request before it reached the server.
        try:
            card.wait_for_selector(self.sel.card_qty_count, timeout=self.nav_timeout_ms)
            if self.debug:
                print(f"    [debug] add_item({product_id!r}): stepper confirmed")
        except Exception as e:
            if self.debug:
                print(f"    [debug] add_item({product_id!r}): stepper never appeared ({e})")
        for _ in range(qty - 1):
            card.query_selector(self.sel.card_add_button).click()

    def _handle_variant_modal_if_present(self, card) -> None:
        """Some products open a pack-size picker modal instead of adding straight to the cart,
        offering several variant rows to choose from. variant_modal_add_button's testid repeats
        once per row, so we can't just click the first match on the page -- we match rows by the
        target product's size/price (read off the search card that was clicked) and only click
        within that one row's scope. If nothing matches, raise rather than guess."""
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
        container = self._card_container(card)
        target_size_el = container.query_selector(self.sel.card_size)
        target_size = (target_size_el.text_content() if target_size_el else "").strip()
        target_price = self._card_price(container)

        row_buttons = modal.query_selector_all(self.sel.variant_modal_add_button)
        matched = None
        for button in row_buttons:
            row_text = self._ancestor_text(button)
            if target_size and target_size not in row_text:
                continue
            if target_price is not None and str(target_price) not in row_text:
                continue
            matched = button
            break

        if matched is None:
            raise VariantModalRequiresSelectors(
                f"A variant-picker modal opened with {len(row_buttons)} option row(s), but none "
                f"matched the requested size={target_size!r} price={target_price!r}. Not "
                "guessing which one to add -- check the match text, or that the modal's rows are "
                "actually wrapped by an ancestor close enough to variant_modal_add_button for "
                "_ancestor_text to pick it up."
            )
        matched.click()
        modal.query_selector(self.sel.variant_modal_confirm_button).click()

    def _ancestor_text(self, element, levels: int = 6) -> str:
        """Text content of an element's nearest ancestor, climbing up to `levels` parents. Used to
        read a variant-picker row's visible size/price without a known row-container selector --
        a heuristic, not a guarantee. Live run: a 2-option modal for "Fresh Eggs White eggs"
        matched neither row against price=122 at the previous level of 4 -- bumped to 6 on the
        theory the row wrapper for this product's modal layout sits further up than 4 parents.
        Unconfirmed against the live page; if it's still not matching, the real fix is a known
        row-container selector instead of climbing further blindly."""
        node = element
        for _ in range(levels):
            parent_handle = node.evaluate_handle("el => el.parentElement")
            parent = parent_handle.as_element()
            if parent is None:
                break
            node = parent
        return node.text_content() or ""

    def set_quantity(self, product_id: str, qty: int) -> None:
        row = self._find_row(product_id)
        current_el = row.query_selector(self.sel.cart_line_qty)
        current = int((current_el.text_content() if current_el else "1").strip() or "1")
        button = self.sel.cart_line_qty_plus if qty > current else self.sel.cart_line_qty_minus
        for _ in range(abs(qty - current)):
            row.query_selector(button).click()

    def remove_item(self, product_id: str) -> None:
        """No separate remove/delete button was found on a live run (cart_line_remove is an
        unverified guess, and the real page has no element there) -- same quantity stepper as
        set_quantity, decrementing to 0 removes the line instead."""
        row = self._find_row(product_id)
        current_el = row.query_selector(self.sel.cart_line_qty)
        current = int((current_el.text_content() if current_el else "1").strip() or "1")
        for _ in range(current):
            row.query_selector(self.sel.cart_line_qty_minus).click()

    def _find_card(self, product_id: str):
        """product_id is the product's own name (see _card_id), so re-run a fresh search for it
        as the query -- the browser is very likely not sitting on that product's search results
        anymore by the time this is called (see _run_search)."""
        for card in self._run_search(product_id):
            name = self._card_name(card)
            if name is not None and self._card_id(name) == product_id:
                return card
        raise LookupError(f"product {product_id!r} not found when re-searching for it")

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
