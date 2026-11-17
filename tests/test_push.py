from datetime import datetime

import pytest
from pywebpush import WebPushException

from agent.db import Database
from agent.messenger import ChatMessenger
from agent.push import PushSender

SUB = {"endpoint": "https://push.example/ep1", "keys": {"p256dh": "p1", "auth": "a1"}}


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def db():
    return Database()


# ---------------------------------------------------------------- db subscription storage
def test_save_and_list_push_subscriptions(db):
    db.save_push_subscription("app:1", "ep1", "p1", "a1", datetime.now())
    db.save_push_subscription("app:1", "ep2", "p2", "a2", datetime.now())
    db.save_push_subscription("app:2", "ep3", "p3", "a3", datetime.now())
    subs = db.push_subscriptions("app:1")
    assert {s["endpoint"] for s in subs} == {"ep1", "ep2"}
    assert db.push_subscriptions("app:2")[0]["keys"] == {"p256dh": "p3", "auth": "a3"}


def test_saving_the_same_endpoint_again_updates_it(db):
    db.save_push_subscription("app:1", "ep1", "old-p", "old-a", datetime.now())
    db.save_push_subscription("app:1", "ep1", "new-p", "new-a", datetime.now())
    subs = db.push_subscriptions("app:1")
    assert len(subs) == 1 and subs[0]["keys"]["p256dh"] == "new-p"


def test_remove_push_subscription(db):
    db.save_push_subscription("app:1", "ep1", "p1", "a1", datetime.now())
    db.remove_push_subscription("ep1")
    assert db.push_subscriptions("app:1") == []


# ---------------------------------------------------------------- PushSender
def test_notify_sends_to_every_subscription_for_the_recipient(db):
    db.save_push_subscription("app:1", "ep1", "p1", "a1", datetime.now())
    db.save_push_subscription("app:1", "ep2", "p2", "a2", datetime.now())
    sent = []
    sender = PushSender(db, "priv-key", "mailto:a@b.com", send_fn=lambda **kw: sent.append(kw))
    sender.notify("app:1", "Title", "Body text")
    assert len(sent) == 2
    assert all(kw["vapid_private_key"] == "priv-key" for kw in sent)
    assert all(kw["vapid_claims"] == {"sub": "mailto:a@b.com"} for kw in sent)
    assert '"title": "Title"' in sent[0]["data"] or "Title" in sent[0]["data"]


def test_notify_with_no_subscriptions_does_nothing(db):
    calls = []
    sender = PushSender(db, "priv-key", "mailto:a@b.com", send_fn=lambda **kw: calls.append(kw))
    sender.notify("app:1", "Title", "Body")
    assert calls == []


def test_notify_removes_an_expired_subscription_on_410(db):
    db.save_push_subscription("app:1", "ep1", "p1", "a1", datetime.now())

    def fail(**kw):
        raise WebPushException("gone", response=FakeResponse(410))
    sender = PushSender(db, "priv-key", "mailto:a@b.com", send_fn=fail)
    sender.notify("app:1", "Title", "Body")
    assert db.push_subscriptions("app:1") == []


def test_notify_removes_an_expired_subscription_on_404(db):
    db.save_push_subscription("app:1", "ep1", "p1", "a1", datetime.now())

    def fail(**kw):
        raise WebPushException("gone", response=FakeResponse(404))
    sender = PushSender(db, "priv-key", "mailto:a@b.com", send_fn=fail)
    sender.notify("app:1", "Title", "Body")
    assert db.push_subscriptions("app:1") == []


def test_notify_keeps_a_subscription_on_a_non_terminal_failure(db):
    db.save_push_subscription("app:1", "ep1", "p1", "a1", datetime.now())

    def fail(**kw):
        raise WebPushException("server error", response=FakeResponse(500))
    sender = PushSender(db, "priv-key", "mailto:a@b.com", send_fn=fail)
    sender.notify("app:1", "Title", "Body")
    assert len(db.push_subscriptions("app:1")) == 1


def test_notify_swallows_an_unexpected_exception(db):
    db.save_push_subscription("app:1", "ep1", "p1", "a1", datetime.now())

    def fail(**kw):
        raise RuntimeError("network blew up")
    sender = PushSender(db, "priv-key", "mailto:a@b.com", send_fn=fail)
    sender.notify("app:1", "Title", "Body")        # must not raise
    assert len(db.push_subscriptions("app:1")) == 1


# ---------------------------------------------------------------- ChatMessenger wiring
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_chat_messenger_pushes_when_a_push_sender_is_configured(db):
    db.save_push_subscription("app:1", "ep1", "p1", "a1", datetime.now())
    notified = []

    class StubPush:
        def notify(self, recipient, title, body):
            notified.append((recipient, title, body))

    msg = ChatMessenger(db, Clock(datetime.now()), push=StubPush())
    msg.send("app:1", "Cart ready: milk, eggs", buttons=[("approve:R1:1", "Place order")])
    assert notified == [("app:1", "Cart ready", "Cart ready: milk, eggs")]


def test_chat_messenger_without_a_push_sender_does_not_error(db):
    msg = ChatMessenger(db, Clock(datetime.now()))
    msg.send("app:1", "hello")      # must not raise with push=None
    assert db.chat_since("app:1")[0]["text"] == "hello"
