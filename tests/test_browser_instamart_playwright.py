"""agent/browser_instamart_playwright.py normally holds real, unverified-against-the-live-site
selectors and isn't unit tested (see its module docstring) -- but select_cod()'s two-step
wait-and-click sequence plus wait-for-aria-checked logic is plain Python control flow,
independent of any real selector, so it's worth covering with a minimal hand-rolled double for
Playwright's Page/ElementHandle API. No real browser or Playwright package needed (this module is
duck-typed on purpose)."""
import pytest

from agent.browser_instamart import InstamartPageError
from agent.browser_instamart_playwright import PlaywrightInstamartPage, Selectors


class FakeElement:
    def __init__(self):
        self.clicked = 0

    def click(self):
        self.clicked += 1


class FakePage:
    """query_selector returns whatever's registered for that selector string. wait_for_selector
    simulates a render delay: if `appears_after_wait` has an element queued for that selector and
    it isn't registered yet, waiting "renders" it (puts it into `elements`) before returning;
    otherwise raises if the selector is still absent. wait_for_function either returns normally
    or raises, controlled by `fail_wait`."""

    def __init__(self, fail_wait: bool = False):
        self.elements = {}
        self.appears_after_wait = {}
        self.fail_wait = fail_wait
        self.wait_calls = []
        self.wait_for_selector_calls = []

    def query_selector(self, selector):
        return self.elements.get(selector)

    def wait_for_selector(self, selector, timeout=None):
        self.wait_for_selector_calls.append(selector)
        if selector in self.elements:
            return
        if selector in self.appears_after_wait:
            self.elements[selector] = self.appears_after_wait.pop(selector)
            return
        raise TimeoutError(f"simulated timeout waiting for {selector!r} to appear")

    def wait_for_function(self, expression, arg=None, timeout=None):
        self.wait_calls.append(arg)
        if self.fail_wait:
            raise TimeoutError(f"simulated timeout waiting on {arg!r}")


def make_adapter(page, sel=None):
    return PlaywrightInstamartPage(page, sel or Selectors())


def put_both_pages_in_place(page, sel):
    """The common "everything renders instantly" setup: both the Payment Options list page's
    COD category row and the COD sub-page's confirm row already present."""
    category_row = FakeElement()
    confirm_row = FakeElement()
    page.elements[sel.payment_method_cod_category] = category_row
    page.elements[sel.cod_payment_option] = confirm_row
    return category_row, confirm_row


def test_select_cod_raises_if_the_category_row_is_not_found():
    """go_to_payment() lands on the Payment Options LIST page first -- if even the COD category
    row never appears there, select_cod() must stop before ever touching the sub-page."""
    sel = Selectors()
    page = FakePage()
    adapter = make_adapter(page, sel)
    with pytest.raises(InstamartPageError):
        adapter.select_cod()
    assert sel.cod_payment_option not in page.wait_for_selector_calls  # never got to step 2
    assert page.wait_calls == []  # never got to the tick wait either


def test_select_cod_raises_if_the_confirm_row_is_not_found_after_the_category_click():
    """The category row exists and gets clicked, but the sub-page's confirm row never shows up."""
    sel = Selectors()
    page = FakePage()
    category_row = FakeElement()
    page.elements[sel.payment_method_cod_category] = category_row
    adapter = make_adapter(page, sel)
    with pytest.raises(InstamartPageError):
        adapter.select_cod()
    assert category_row.clicked == 1  # step 1 did happen
    assert page.wait_calls == []      # never reached the tick wait


def test_select_cod_clicks_both_rows_in_order_then_waits_for_the_tick_to_flip():
    sel = Selectors()
    page = FakePage(fail_wait=False)
    category_row, confirm_row = put_both_pages_in_place(page, sel)
    adapter = make_adapter(page, sel)
    adapter.select_cod()  # must not raise
    assert category_row.clicked == 1
    assert confirm_row.clicked == 1
    # category row must be waited for/clicked before the confirm row (it's on a different page
    # reached only by clicking the category row first)
    assert (page.wait_for_selector_calls.index(sel.payment_method_cod_category)
           < page.wait_for_selector_calls.index(sel.cod_payment_option))
    assert page.wait_calls == [sel.cod_payment_tick]


def test_select_cod_raises_if_the_tick_never_reports_aria_checked_true():
    """Confirmed live: a click landing doesn't mean the selected-state indicator has caught up --
    select_cod() must not let checkout() proceed believing COD is selected when it isn't."""
    sel = Selectors()
    page = FakePage(fail_wait=True)
    category_row, confirm_row = put_both_pages_in_place(page, sel)
    adapter = make_adapter(page, sel)
    with pytest.raises(InstamartPageError):
        adapter.select_cod()
    assert category_row.clicked == 1 and confirm_row.clicked == 1  # both clicks still happened


def test_select_cod_waits_for_the_category_row_to_render_after_a_short_delay():
    """go_to_payment() only waits for networkidle, which doesn't guarantee
    payment_method_cod_category has rendered yet -- confirmed live, the row was real but appeared
    moments after an instant check already gave up."""
    sel = Selectors()
    page = FakePage(fail_wait=False)
    page.appears_after_wait[sel.payment_method_cod_category] = FakeElement()
    page.elements[sel.cod_payment_option] = FakeElement()
    adapter = make_adapter(page, sel)
    adapter.select_cod()  # must not raise
    assert sel.payment_method_cod_category in page.wait_for_selector_calls


def test_select_cod_waits_for_the_confirm_row_to_render_after_a_short_delay():
    """Same render-delay race on the second page: the COD sub-page's confirm row doesn't have to
    be present the instant navigation lands either."""
    sel = Selectors()
    page = FakePage(fail_wait=False)
    page.elements[sel.payment_method_cod_category] = FakeElement()
    page.appears_after_wait[sel.cod_payment_option] = FakeElement()
    adapter = make_adapter(page, sel)
    adapter.select_cod()  # must not raise
    assert sel.cod_payment_option in page.wait_for_selector_calls
