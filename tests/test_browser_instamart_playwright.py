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
    """query_selector returns whatever's registered for that selector string; wait_for_function
    either returns normally or raises, controlled by `fail_wait`."""

    def __init__(self, fail_wait: bool = False):
        self.elements = {}
        self.fail_wait = fail_wait
        self.wait_calls = []

    def query_selector(self, selector):
        return self.elements.get(selector)

    def wait_for_function(self, expression, arg=None, timeout=None):
        self.wait_calls.append(arg)
        if self.fail_wait:
            raise TimeoutError(f"simulated timeout waiting on {arg!r}")


def test_select_cod_raises_if_the_cod_row_is_not_found():
    page = FakePage()
    adapter = PlaywrightInstamartPage(page, Selectors())
    with pytest.raises(InstamartPageError):
        adapter.select_cod()
    assert page.wait_calls == []  # never even tried to wait -- nothing was clicked


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
