"""Turns a person's text into raw actions. The output ALWAYS goes to Agent.handle_chat, which runs the gate.

RuleIntentParser needs no network. A Gemini parser plugs into the same interface later: it receives the same
small context (no phone numbers, no tokens) and returns the same list of dicts. Whatever a parser returns is
untrusted input to the gate.
"""
import re
from dataclasses import dataclass, field
from typing import Optional, Protocol

YES = {"yes", "y", "ok", "okay", "place order", "place the order", "order it", "go ahead", "confirm", "yes order"}
NO = {"no", "cancel", "stop", "dont order", "don't order", "do not order"}
ORDER_NOW = {"order now", "order the list", "order what's on the list", "order whats on the list", "order everything"}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip()).rstrip(".!")


def is_exact_command(text: str, kind: str) -> bool:
    """Approving and starting an order from free text needs an exact known phrase, whatever parser
    produced the action. A language model that guesses "approve" from "sounds good maybe" is dropped."""
    phrases = {"approve": YES, "order_now": ORDER_NOW}.get(kind)
    return phrases is not None and _clean(text) in phrases


@dataclass
class ParseResult:
    """What a parser understood. `actions` go to the gate. `reply` is only used when no action ran."""
    actions: list = field(default_factory=list)
    reply: Optional[str] = None
    source: str = "rules"


class IntentParser(Protocol):
    def parse(self, text: str, ctx: dict) -> list: ...


class RuleIntentParser:
    def parse(self, text: str, ctx: dict) -> list:
        t = re.sub(r"\s+", " ", text.lower().strip()).rstrip(".!")
        run = ctx.get("open_run")

        if t in YES:
            return [{"type": "approve", "run_id": run["id"]}] if run and run["state"] == "awaiting_user" else []
        if t in NO:
            return [{"type": "cancel", "run_id": run["id"]}] if run else []
        if t in ORDER_NOW:
            return [{"type": "order_now"}]

        m = re.match(r"^add (?:(\d+) )?(.+)$", t)
        if m:
            return [{"type": "add_item", "item": m.group(2), "qty": int(m.group(1) or 1)}]
        m = re.match(r"^remind me when (.+?) (?:is|are) back(?: in stock)?$", t)
        if m:
            return [{"type": "watch_item", "item": m.group(1)}]
        m = re.match(r"^remove (.+)$", t)
        if m:
            return [{"type": "remove_item", "item": m.group(1)}]
        return []
