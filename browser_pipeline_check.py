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

        lines = []
        for item in items:
            found = store.search_products(item)
            print(f"  {item}: {len(found)} card(s) found")
            for p in found:
                print(f"    - {p.name!r} {p.size!r} ₹{p.price} in_stock={p.in_stock} id={p.id!r}")
            matches = [p for p in found if p.in_stock]
            if not matches:
                print(f"  {item}: no in-stock match found")
                continue
            chosen = matches[0]
            print(f"  {item}: matched {chosen.name} {chosen.size} (₹{chosen.price})")
            lines.append((chosen.id, 1))

        if not lines:
            print("Nothing matched, nothing added to the cart.")
            input("\nPress Enter to close the browser...")
            context.close()
            return

        store.update_cart([(pid, qty) for pid, qty in lines])
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
