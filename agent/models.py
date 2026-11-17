from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class RunState(str, Enum):
    AWAITING_USER = "awaiting_user"          # permission mode: waiting for Place order
    ORDERING = "ordering"                    # a reply was accepted, checkout is running
    AWAITING_APP_PAYMENT = "awaiting_app_payment"   # COD unavailable, user pays in the app
    DONE = "done"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_open(self) -> bool:
        return self in (RunState.AWAITING_USER, RunState.ORDERING, RunState.AWAITING_APP_PAYMENT)


@dataclass
class ListItem:
    name: str
    qty: int = 1
    source: str = "fridge"      # "fridge" or "whatsapp"


@dataclass
class Product:
    id: str
    name: str
    size: str
    price: int                  # rupees
    in_stock: bool = True


@dataclass
class CartLine:
    product: Product
    qty: int
    item_name: str              # the list item this line came from
    source: str = "fridge"      # "fridge", or "whatsapp" if any part of it was added in chat

    @property
    def total(self) -> int:
        return self.product.price * self.qty


@dataclass
class Run:
    id: str
    mode: str
    created: datetime
    lines: list = field(default_factory=list)          # list[CartLine]
    out_of_stock: list = field(default_factory=list)   # item names
    no_match: list = field(default_factory=list)       # item names
    approved_total: int = 0                            # total the user saw (or the built total in self mode)
    state: RunState = RunState.AWAITING_USER
    state_since: Optional[datetime] = None
    reminded: bool = False
    handled_by: Optional[str] = None
    ask_version: int = 0                               # +1 each time the user is asked, buttons carry it
    asked: bool = False                                # the user was asked at some point (permission mode, cap, price change)
    order_id: Optional[str] = None
    orders_before: int = 0                             # store order count when the app-payment wait began
    note: str = ""
