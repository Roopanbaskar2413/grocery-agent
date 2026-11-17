"""When to start a run. Pure function, easy to test."""
from datetime import datetime
from enum import Enum

from .config import Settings


class Decision(str, Enum):
    ORDER = "order"
    HOLD = "hold"


def decide(now: datetime, in_stock_count: int, order_now: bool, cfg: Settings) -> Decision:
    """in_stock_count counts only items that matched an in-stock product.
    Out-of-stock items stay on the list but do not count."""
    if order_now and in_stock_count >= 1:
        return Decision.ORDER                       # Order now skips the wait, at any count
    if in_stock_count >= cfg.list_threshold:
        return Decision.ORDER
    if now.hour >= cfg.evening_hour and in_stock_count >= cfg.evening_min_items:
        return Decision.ORDER
    return Decision.HOLD
