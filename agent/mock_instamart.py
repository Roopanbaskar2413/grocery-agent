"""A fake Instamart with the same shape as the real MCP tools we plan to use.

Tool names mirror the Swiggy MCP: search_products, get_cart, update_cart (replaces the whole
cart), get_payment_options, checkout. Prices, stock and COD are values we can change in tests.
The delivery fee and free-delivery amount here are mock values, the real ones come from the
get_cart bill breakdown after approval.
"""
from dataclasses import dataclass
from .models import Product

FREE_DELIVERY_MIN = 100
DELIVERY_FEE = 25


@dataclass
class CartView:
    lines: list          # list of (product_id, qty)
    subtotal: int
    delivery_fee: int

    @property
    def total(self) -> int:
        return self.subtotal + self.delivery_fee


class MockInstamart:
    def __init__(self, catalog=None, cod_available=True):
        self.products = {p.id: p for p in (catalog if catalog is not None else default_catalog())}
        self.cod_available = cod_available
        self._cart = []                  # list of [product_id, qty]
        self.orders = []                 # list of dicts
        self.calls = []                  # names of tools called, for tests

    # ---- tools the agent may use -------------------------------------------------
    def search_products(self, query: str):
        self.calls.append("search_products")
        q = query.lower().strip()
        words = {q, q.split()[0]}
        for w in list(words):                      # the real search handles plurals, so does the mock
            if w.endswith("es"):
                words.add(w[:-2])
            if w.endswith("s"):
                words.add(w[:-1])
        return [p for p in self.products.values() if any(w in p.name.lower() for w in words)]

    def get_cart(self) -> CartView:
        self.calls.append("get_cart")
        subtotal = sum(self.products[pid].price * qty for pid, qty in self._cart)
        fee = 0 if (subtotal >= FREE_DELIVERY_MIN or not self._cart) else DELIVERY_FEE
        return CartView([(pid, qty) for pid, qty in self._cart], subtotal, fee)

    def update_cart(self, lines):
        """Replaces the whole cart, like the real tool."""
        self.calls.append("update_cart")
        self._cart = [[pid, qty] for pid, qty in lines if qty > 0]

    def get_payment_options(self):
        self.calls.append("get_payment_options")
        return ["COD", "UPI"] if self.cod_available else ["UPI"]

    def checkout(self, payment: str):
        """Only the backend calls this. COD places the order, UPI waits for the user in the app."""
        self.calls.append("checkout")
        if payment != "COD":
            raise RuntimeError("UPI must be paid in the Instamart app")
        return self._place()

    # ---- helpers for tests and demos ---------------------------------------------
    def simulate_app_payment(self):
        """The user opens the Instamart app and pays the cart."""
        return self._place()

    def _place(self):
        cart = self.get_cart()
        oid = f"ORD{len(self.orders) + 1:03d}"
        self.orders.append({"id": oid, "lines": list(cart.lines), "total": cart.total})
        self._cart = []
        return oid

    def set_price(self, pid, price):
        self.products[pid].price = price

    def set_stock(self, pid, in_stock):
        self.products[pid].in_stock = in_stock

    def put_in_cart_as_user(self, pid, qty):
        self._cart.append([pid, qty])


def default_catalog():
    return [
        Product("p1", "Tomato Hybrid", "500 g", 30),
        Product("p2", "Onion", "1 kg", 45),
        Product("p3", "Cucumber", "500 g", 28),
        Product("p4", "Mushroom Button", "200 g", 60),
        Product("p5", "Milk Toned", "500 ml", 34),
        Product("p6", "Curd", "400 g", 40),
        Product("p7", "Salt Iodised", "1 kg", 25),
        Product("p8", "Tomato Ketchup", "500 g", 95),
        Product("p9", "Eggs Farm Fresh", "6 pcs", 54),
        Product("p10", "Chocolate Bar", "50 g", 120),
    ]
