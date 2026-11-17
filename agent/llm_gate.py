"""The only door between chat (and Gemini) and the agent.

Gemini never calls tools that spend money. It returns structured actions, and this gate checks each
one against an allowlist and hard limits before the backend does anything. Even a model that has
been talked into anything can only ask for actions in ALLOWED, with small limits, and none of them
is "check out".
"""
import re
from dataclasses import dataclass
from typing import Optional

from .config import Settings

ALLOWED = {"add_item", "remove_item", "watch_item", "approve", "cancel", "change_mode", "order_now", "none"}
KEYS = {"type", "item", "qty", "run_id", "mode", "ask"}
ITEM_RE = re.compile(r"^[A-Za-z][A-Za-z '\-]{1,39}$")
ONE_PER_MESSAGE = {"approve", "cancel", "order_now", "change_mode"}


@dataclass
class Action:
    type: str
    item: Optional[str] = None
    qty: int = 1
    run_id: Optional[str] = None
    mode: Optional[str] = None
    ask: Optional[int] = None          # which ask a button belongs to (text replies have none)


def validate_one(raw, cfg: Settings):
    if not isinstance(raw, dict):
        return None, "not an action"
    if set(raw) - KEYS:
        return None, "unknown field"
    t = raw.get("type")
    if t not in ALLOWED:
        return None, "action not allowed"
    a = Action(type=t)
    if t in ("add_item", "remove_item", "watch_item"):
        item = raw.get("item")
        if not isinstance(item, str) or not ITEM_RE.match(item.strip()):
            return None, "invalid item name"
        a.item = item.strip()
    if t == "add_item":
        qty = raw.get("qty", 1)
        if not isinstance(qty, int) or isinstance(qty, bool) or not (1 <= qty <= cfg.max_qty_per_item):
            return None, f"quantity must be 1 to {cfg.max_qty_per_item}"
        a.qty = qty
    if t in ("approve", "cancel"):
        if not isinstance(raw.get("run_id"), str):
            return None, "missing run"
        a.run_id = raw["run_id"]
        ask = raw.get("ask")
        if ask is not None:
            if not isinstance(ask, int) or isinstance(ask, bool) or ask < 0:
                return None, "invalid ask"
            a.ask = ask
    if t == "change_mode":
        if raw.get("mode") not in ("self", "permission"):
            return None, "invalid mode"
        a.mode = raw["mode"]
    return a, None


def validate_batch(raw_actions, cfg: Settings):
    """Returns (actions, errors). Limits: a few items per message and only one decision per message."""
    if not isinstance(raw_actions, list):
        return [], ["not a list of actions"]
    actions, errors = [], []
    adds = 0
    decisions = 0
    for raw in raw_actions:
        a, err = validate_one(raw, cfg)
        if err:
            errors.append(err)
            continue
        if a.type == "add_item":
            adds += 1
            if adds > cfg.max_chat_items_per_message:
                errors.append("too many items in one message")
                continue
        if a.type in ONE_PER_MESSAGE:
            decisions += 1
            if decisions > 1:
                errors.append("only one decision per message")
                continue
        actions.append(a)
    return actions, errors
