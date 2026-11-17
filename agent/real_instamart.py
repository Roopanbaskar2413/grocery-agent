"""The real Instamart adapter: same interface as MockInstamart, backed by Swiggy's Instamart MCP
server (mcp.swiggy.com/im, confirmed 16 tools) instead of an in-memory store. The orchestrator never
imports this module directly -- it only sees whatever object `search_products`/`get_cart`/
`update_cart`/`get_payment_options`/`checkout`/`.orders` is handed to it, so nothing in orchestrator.py
changes to support this.

Only used when Settings.instamart_backend == "real", and only after agent/oauth.py has a stored
token -- __init__ refuses to run otherwise. Never exercised against the real API yet (Builders Club
access is pending); tested here against a scripted stand-in. Two points are unverified and called
out in DRYRUN.md:
  - the exact field names for delivery fee / free-delivery threshold inside get_cart's bill breakdown
    (we only have confirmation of `billBreakdown.toPay.value` as the grand total)
  - whether checkout's payment-method string for the no-UPI fallback is "COD" or "Cash" -- this is
    why it is a setting (Settings.instamart_cod_payment_value) rather than hard-coded.

Address handling and the spinId/skuId -> product-id mapping are adapter-internal: a Product's `id`
here is the string "<spinId>:<skuId>", so update_cart can round-trip it without the orchestrator
knowing addresses or Instamart's variation ids exist at all.
"""
import logging
from typing import Optional

import httpx

from .mcp_client import McpClient
from .mock_instamart import CartView
from .models import Product
from .oauth import OAuthClient

log = logging.getLogger("agent.real_instamart")

INSTAMART_BASE_URL = "https://mcp.swiggy.com/im"
MIN_ORDER_RUPEES = 99       # Instamart's own minimum, separate from Settings.spend_cap


class InstamartMinOrderError(RuntimeError):
    """The cart is below Instamart's own minimum order value. Not a spend-cap condition."""


class InstamartOrderError(RuntimeError):
    """checkout's outcome is unknown. checkout is not idempotent: a 5xx or timeout does not tell us
    whether Swiggy actually placed the order, so the caller must not retry blindly."""


class RealInstamart:
    def __init__(self, oauth: OAuthClient, cfg=None, base_url: str = INSTAMART_BASE_URL,
                 http: Optional[httpx.Client] = None):
        if oauth.store.load() is None:
            raise RuntimeError("no stored Instamart OAuth token; run the sign-in flow before using "
                              "the real adapter")
        self.oauth = oauth
        self.cod_payment_value = getattr(cfg, "instamart_cod_payment_value", "COD") if cfg else "COD"
        self.client = McpClient(base_url, oauth.get_access_token, http=http)
        self._address_id: Optional[str] = None
        self.calls = []        # tool names called, for tests (mirrors MockInstamart.calls)

    def _call(self, tool_name: str, **arguments) -> dict:
        self.calls.append(tool_name)
        return self.client.call(tool_name, **arguments)

    # ---- address (adapter-internal, never seen by the orchestrator) --------------
    def _resolve_address(self) -> str:
        if self._address_id:
            return self._address_id
        result = self._call("get_addresses")
        addresses = result.get("addresses", [])
        default_id = (result.get("resolution") or {}).get("defaultAddressId")
        chosen = next((a for a in addresses if a.get("id") == default_id), None) if default_id else None
        if not chosen:
            chosen = next((a for a in addresses if a.get("addressCategory") == "Home"), None)
        if not chosen and addresses:
            chosen = addresses[0]
        if not chosen:
            raise RuntimeError("Instamart account has no saved address; add one in the app first")
        self._address_id = chosen["id"]
        return self._address_id

    # ---- tools the agent may use, same shapes as MockInstamart --------------------
    def search_products(self, query: str):
        address_id = self._resolve_address()
        result = self._call("search_products", addressId=address_id, query=query)
        out = []
        for entry in result.get("products", []):
            for v in entry.get("variations", [entry]):       # tolerate a flat or nested shape
                spin_id, sku_id = v.get("spinId"), v.get("skuId")
                if not (spin_id and sku_id):
                    continue
                price = v.get("price", v.get("sellingPrice", v.get("mrp", 0)))
                size = v.get("unit", v.get("quantity", v.get("weight", "")))
                in_stock = bool(v.get("isInStockAndAvailable", True))
                out.append(Product(f"{spin_id}:{sku_id}", v.get("name", entry.get("name", query)),
                                   size, int(price), in_stock))
        return out

    def get_cart(self) -> CartView:
        result = self._call("get_cart")
        lines = []
        for it in result.get("items", []):
            spin_id, sku_id = it.get("spinId"), it.get("skuId")
            if spin_id and sku_id:
                lines.append((f"{spin_id}:{sku_id}", it.get("quantity", 1)))
        bill = result.get("billBreakdown", {})
        total = (bill.get("toPay") or {}).get("value")
        if total is None:
            total = sum(it.get("price", 0) * it.get("quantity", 1) for it in result.get("items", []))
        fee = bill.get("deliveryFee")
        fee = fee.get("value", 0) if isinstance(fee, dict) else (fee or 0)
        return CartView(lines, total - fee, fee)

    def update_cart(self, lines):
        if not lines:
            self._call("clear_cart")
            return
        address_id = self._resolve_address()
        items = [{"spinId": pid.split(":", 1)[0], "skuId": pid.split(":", 1)[1], "quantity": qty}
                for pid, qty in lines if qty > 0]
        self._call("update_cart", selectedAddressId=address_id, items=items)

    def get_payment_options(self):
        result = self._call("get_payment_options")
        data = result.get("data", result)
        methods = []
        if data.get("cod"):
            methods.append("COD")
        if data.get("platforms"):
            methods.append("UPI")
        return methods

    def checkout(self, payment: str) -> str:
        if payment != "COD":
            raise RuntimeError(f"RealInstamart only supports the COD path today, got {payment!r}")
        cart = self.get_cart()
        if cart.total < MIN_ORDER_RUPEES:
            raise InstamartMinOrderError(
                f"cart total ₹{cart.total} is below Instamart's ₹{MIN_ORDER_RUPEES} minimum order")
        address_id = self._resolve_address()
        before = self._order_ids()
        try:
            result = self._call("checkout", addressId=address_id, paymentMethod=self.cod_payment_value)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            return self._resolve_ambiguous_outcome(before, e)
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                return self._resolve_ambiguous_outcome(before, e)
            raise
        order_id = result.get("orderId") or result.get("order_id")
        if not order_id:
            raise InstamartOrderError(f"checkout returned no order id: {result}")
        return order_id

    def _resolve_ambiguous_outcome(self, before, cause) -> str:
        """checkout is not idempotent. A 5xx or timeout does not tell us whether Swiggy placed the
        order, so before letting the caller retry, check get_orders for a new order that matches.
        If one appears, treat it as placed (return its id) instead of raising."""
        new = self._order_ids() - before
        if new:
            order_id = next(iter(new))
            log.warning("checkout call failed (%s) but a new order %s appeared; treating it as placed",
                       cause, order_id)
            return order_id
        raise InstamartOrderError(
            f"checkout failed and no new order appeared, do not retry blindly: {cause}") from cause

    def _order_ids(self) -> set:
        return {o["id"] for o in self.orders}

    @property
    def orders(self) -> list:
        result = self._call("get_orders")
        raw = result.get("orders", result.get("data", {}).get("orders", []))
        return [{**o, "id": o.get("orderId") or o.get("id")} for o in raw]

    # ---- tracking, not used by the orchestrator yet, kept for dryrun.py / a future milestone ------
    def get_delivery_status(self, order_id, address_id=None):
        return self._call("get_delivery_status", orderId=order_id, addressId=address_id or self._resolve_address())

    def track_order(self, order_id, lat, lng):
        return self._call("track_order", orderId=order_id, lat=lat, lng=lng)
