"""agent/browser_instamart_playwright.py normally holds real, unverified-against-the-live-site
selectors and isn't unit tested (see its module docstring) -- but select_cod()'s wait-for-
aria-checked logic is plain Python control flow, independent of any real selector, so it's worth
covering with a minimal hand-rolled double for Playwright's Page/ElementHandle API. No real
browser or Playwright package needed (this module is duck-typed on purpose)."""
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


def test_select_cod_raises_if_the_cod_row_is_not_found():
    page = FakePage()
    adapter = PlaywrightInstamartPage(page, Selectors())
    with pytest.raises(InstamartPageError):
        adapter.select_cod()
    assert page.wait_calls == []  # never even tried to wait for the tick -- nothing was clicked


def test_select_cod_clicks_the_row_and_waits_for_the_tick_to_flip():
    sel = Selectors()
    page = FakePage(fail_wait=False)
    row = FakeElement()
    page.elements[sel.cod_payment_option] = row
    adapter = PlaywrightInstamartPage(page, sel)
    adapter.select_cod()  # must not raise
    assert row.clicked == 1
    assert page.wait_calls == [sel.cod_payment_tick]


def test_select_cod_raises_if_the_tick_never_reports_aria_checked_true():
    """Confirmed live: a click landing doesn't mean the selected-state indicator has caught up --
    select_cod() must not let checkout() proceed believing COD is selected when it isn't."""
    sel = Selectors()
    page = FakePage(fail_wait=True)
    row = FakeElement()
    page.elements[sel.cod_payment_option] = row
    adapter = PlaywrightInstamartPage(page, sel)
    with pytest.raises(InstamartPageError):
        adapter.select_cod()
    assert row.clicked == 1  # the click itself still happened, only the confirmation wait failed


def test_select_cod_waits_for_a_row_that_renders_after_a_short_delay():
    """go_to_payment() only waits for networkidle, which doesn't guarantee cod_payment_option has
    rendered yet -- confirmed live, the row was real but appeared moments after an instant check
    already gave up. select_cod() must not conclude "not found" just because the row wasn't there
    on the very first look."""
    sel = Selectors()
    page = FakePage(fail_wait=False)
    row = FakeElement()
    page.appears_after_wait[sel.cod_payment_option] = row  # not in `elements` yet, renders mid-wait
    adapter = PlaywrightInstamartPage(page, sel)
    adapter.select_cod()  # must not raise
    assert sel.cod_payment_option in page.wait_for_selector_calls
    assert row.clicked == 1
