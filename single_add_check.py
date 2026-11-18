"""One-off, minimal isolated test: search for one item, click add ONCE, wait a long time, check the
cart ONCE. Not the full pipeline -- built to debug why scripted add-to-cart silently didn't stick
on a live run (page showed success, cart didn't) without hammering the site with repeated full runs.

    python3 single_add_check.py curd

Uses the same persistent profile as browser_pipeline_check.py.
"""
import sys

from playwright.sync_api import sync_playwright

from agent.browser_instamart_playwright import PlaywrightInstamartPage, Selectors

PROFILE_DIR = "./.instamart_browser_profile"
WAIT_SECONDS = 15


def main(query):
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = context.new_page()
        store_page = PlaywrightInstamartPage(page, Selectors(), debug=True)

        input("Browser open. Press Enter once you're logged in and ready...")

        products = store_page.search(query)
        in_stock = [p for p in products if p["in_stock"]]
        if not in_stock:
            print(f"No in-stock match for {query!r}.")
            context.close()
            return

        target = in_stock[0]
        print(f"Adding {target['name']!r} (₹{target['price']}) once, then waiting "
             f"{WAIT_SECONDS}s before checking the cart...")
        store_page.add_item(target["id"], 1)

        print(f"Waiting {WAIT_SECONDS}s...")
        page.wait_for_timeout(WAIT_SECONDS * 1000)

        cart_items = store_page.cart_items()
        print(f"\nCart items after waiting: {cart_items}")
        found = any(it["id"] == target["id"] for it in cart_items)
        print(f"{'FOUND' if found else 'NOT FOUND'} in cart: {target['name']!r}")

        input("\nPress Enter to close the browser...")
        context.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "curd")
