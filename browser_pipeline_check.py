"""Manually verify the fridge -> search -> cart -> price/stock-recheck pipeline against the real,
logged-in Instamart site, using agent/browser_instamart.py. Not part of the automated test suite,
not wired into run_dev.py -- a one-off tool you run yourself.

    pip install playwright
    playwright install chromium
    python3 browser_pipeline_check.py milk curd eggs

First run: a browser window opens to Instamart. Log in by hand (this script never touches your
login or payment). The session is saved in ./.instamart_browser_profile, so later runs stay logged
in. checkout() always stops before placing the order (see agent/browser_instamart.py) -- the
browser tab is left open on the cart for you to review and, only if you want to, place the order
yourself.

The CSS selectors in agent/browser_instamart_playwright.py are unverified placeholders. This will
almost certainly fail on first run until you fill in the real ones from DevTools.
"""
import sys

from playwright.sync_api import sync_playwright

from agent.browser_instamart import BrowserInstamart, InstamartCheckoutPaused, InstamartMinOrderError
from agent.browser_instamart_playwright import PlaywrightInstamartPage, Selectors

PROFILE_DIR = "./.instamart_browser_profile"


def main(items):
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = context.new_page()
        store = BrowserInstamart(PlaywrightInstamartPage(page, Selectors(), debug=True))

        input(f"Browser open. Log in to Instamart by hand if this is the first run, "
             f"then press Enter to continue...")

        lines = []            # (item_query, pid, qty, chosen Product)
        no_match = []         # item queries with no in-stock card at all
        for item in items:
            found = store.search_products(item)
            print(f"  {item}: {len(found)} card(s) found")
            for p in found:
                print(f"    - {p.name!r} {p.size!r} ₹{p.price} in_stock={p.in_stock} id={p.id!r}")
            matches = [p for p in found if p.in_stock]
            if not matches:
                print(f"  {item}: no in-stock match found")
                no_match.append(item)
                continue
            chosen = matches[0]
            print(f"  {item}: matched {chosen.name} {chosen.size} (₹{chosen.price})")
            lines.append((item, chosen.id, 1, chosen))

        if not lines:
            print("Nothing matched, nothing added to the cart.")
            input("\nPress Enter to close the browser...")
            context.close()
            return

        # One flaky product must not stop the rest -- update_cart() now tries every item
        # independently and reports a per-item result instead of raising on the first failure.
        results = store.update_cart([(pid, qty) for _, pid, qty, _ in lines])
        outcome_by_pid = {r.pid: r for r in results}

        added, failed = [], []
        for item, pid, qty, chosen in lines:
            outcome = outcome_by_pid.get(pid)
            if outcome is None or outcome.ok:
                added.append((item, chosen))
            else:
                failed.append((item, chosen, outcome.reason))

        print("\n--- Add-to-cart summary ---")
        for item, chosen in added:
            print(f"  ADDED: {item!r} -> {chosen.name} (₹{chosen.price})")
        for item in no_match:
            print(f"  NO MATCH: {item!r} -- nothing in stock found")
        for item, chosen, reason in failed:
            print(f"  FAILED TO ADD: {item!r} -> {chosen.name} -- {reason}")

        cart = store.get_cart()
        print(f"\nCart: {len(cart.lines)} line(s), subtotal ₹{cart.subtotal}, "
             f"delivery ₹{cart.delivery_fee}, total ₹{cart.total}")
        print(f"Payment options available: {store.get_payment_options()}")

        try:
            store.checkout("COD")
        except InstamartCheckoutPaused as e:
            print(f"\n{e}")
            print("The browser tab is still open on the cart. Place the order yourself there if "
                 "you want it for real -- this script will not do it for you.")
        except InstamartMinOrderError as e:
            print(f"\n{e}")

        input("\nPress Enter to close the browser...")
        context.close()


if __name__ == "__main__":
    items = sys.argv[1:] or ["milk", "curd", "eggs", "onion"]
    main(items)
