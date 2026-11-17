"""Messenger interface. The console version just records what would be sent on WhatsApp."""
from dataclasses import dataclass, field


@dataclass
class Sent:
    to: str
    text: str
    buttons: list = field(default_factory=list)


class ConsoleMessenger:
    def __init__(self):
        self.outbox = []

    def send(self, to, text, buttons=None):
        self.outbox.append(Sent(to, text, list(buttons or [])))

    def texts(self):
        return [m.text for m in self.outbox]


APP_PREFIX = "app:"


class ChatMessenger:
    """Messages for people using the in-app chat. They are stored, and the app fetches them.
    If a push sender is given (agent/push.py), a Web Push notification is also sent best-effort --
    storage is what the page actually reads, the push is only a way to get someone's attention."""

    def __init__(self, db, clock, push=None):
        self.db, self.clock, self.push = db, clock, push

    def send(self, to, text, buttons=None):
        self.db.chat_add(to, "agent", text, buttons or [], self.clock())
        if self.push:
            self.push.notify(to, "Cart ready" if buttons else "Grocery agent", text[:180])


class RoutingMessenger:
    """In-app recipients ("app:1") go to the chat store. Everyone else goes to the other messenger (WhatsApp)."""

    def __init__(self, chat, other):
        self.chat, self.other = chat, other

    def send(self, to, text, buttons=None):
        (self.chat if str(to).startswith(APP_PREFIX) else self.other).send(to, text, buttons)
