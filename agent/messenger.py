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
    """Messages for people using the in-app chat. They are stored, and the app fetches them."""

    def __init__(self, db, clock):
        self.db, self.clock = db, clock

    def send(self, to, text, buttons=None):
        self.db.chat_add(to, "agent", text, buttons or [], self.clock())


class RoutingMessenger:
    """In-app recipients ("app:1") go to the chat store. Everyone else goes to the other messenger (WhatsApp)."""

    def __init__(self, chat, other):
        self.chat, self.other = chat, other

    def send(self, to, text, buttons=None):
        (self.chat if str(to).startswith(APP_PREFIX) else self.other).send(to, text, buttons)
