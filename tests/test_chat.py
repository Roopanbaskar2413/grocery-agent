"""In-app chat channel: same door as WhatsApp (parser -> gate), tokens instead of phone numbers."""
import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api import ApiSecrets, create_app
from agent.config import Settings
from agent.db import Database
from agent.intent import RuleIntentParser
from agent.messenger import ChatMessenger, ConsoleMessenger, RoutingMessenger
from agent.mock_fridge import MockFridge
from agent.mock_instamart import MockInstamart
from agent.models import RunState
from agent.orchestrator import Agent
from tests.test_api import FOUR, NOON, P1, SECRETS, Clock

T1, T2, T3 = "token-one-aaaaaaaaaaaaaaaa", "token-two-bbbbbbbbbbbbbbbb", "token-three-cccccccccccccc"
A1, A2, A3 = "app:1", "app:2", "app:3"


class ChatRig:
    def __init__(self, tmp_path, mode="permission", parser=None, with_phone=False, cod=True,
                 push=None, vapid_public_key=None):
        self.fridge, self.store = MockFridge(), MockInstamart(cod_available=cod)
        recipients = {A1: "Roopan", A2: "Person B", A3: "Person C"}
        if with_phone:
            recipients[P1] = "Phone person"
        self.db = Database(str(tmp_path / "c.db"))
        self.clock = Clock(NOON)
        self.wa = ConsoleMessenger()
        msg = RoutingMessenger(ChatMessenger(self.db, self.clock, push=push), self.wa)
        self.agent = Agent(self.fridge, self.store, msg, Settings(mode=mode, recipients=recipients), self.db)
        self.app = create_app(self.agent, parser or RuleIntentParser(), SECRETS, clock=self.clock,
                              scheduler_interval=None, chat_tokens={T1: A1, T2: A2, T3: A3},
                              vapid_public_key=vapid_public_key)
        self.client = TestClient(self.app)

    @staticmethod
    def auth(token):
        return {"Authorization": f"Bearer {token}"}

    def send(self, token, text):
        return self.client.post("/chat/send", json={"text": text}, headers=self.auth(token))

    def tap(self, token, button_id):
        return self.client.post("/chat/tap", json={"button_id": button_id}, headers=self.auth(token))

    def messages(self, token, after=0):
        return self.client.get(f"/chat/messages?after={after}", headers=self.auth(token)).json()["messages"]

    def texts(self, token):
        return [m["text"] for m in self.messages(token) if m["role"] == "agent"]

    def fridge_push(self, items=FOUR, order_now=False):
        return self.client.post("/fridge/list", json={"items": [{"name": n, "qty": q} for n, q in items],
                                                      "order_now": order_now}, headers={"X-Fridge-Key": "fridge-key"})

    @property
    def run(self):
        return self.agent.runs[-1]


@pytest.fixture
def rig(tmp_path):
    return ChatRig(tmp_path)


# ---------------------------------------------------------------- who may talk
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic " + T1},
                                     {"Authorization": "Bearer"}, {"Authorization": "Bearer "}])
def test_every_chat_endpoint_needs_a_valid_token(rig, headers):
    assert rig.client.post("/chat/send", json={"text": "add milk"}, headers=headers).status_code == 401
    assert rig.client.post("/chat/tap", json={"button_id": "approve:R1:1"}, headers=headers).status_code == 401
    assert rig.client.get("/chat/messages", headers=headers).status_code == 401
    assert not rig.agent.pending and not rig.store.orders


def test_the_page_and_script_are_public_but_hold_no_secrets_and_set_a_strict_policy(rig):
    page, js = rig.client.get("/chat"), rig.client.get("/chat/app.js")
    assert page.status_code == 200 and js.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "<script>" not in page.text.replace('<script src="/chat/app.js"></script>', "")   # no inline script
    for secret in (T1, T2, "fridge-key", "app-secret", "verify-me"):
        assert secret not in page.text and secret not in js.text


def test_the_page_discloses_gemini_before_any_message_is_sent(rig):
    page = rig.client.get("/chat")
    assert "Messages are processed by Google Gemini." in page.text
    assert page.text.index('id="notice"') < page.text.index('id="send"')


def test_chat_script_never_renders_text_as_html():
    js = Path(__file__).resolve().parents[1].joinpath("agent", "static", "chat.js").read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned


# ---------------------------------------------------------------- push notifications
def test_service_worker_is_public_and_holds_no_secrets(rig):
    sw = rig.client.get("/chat/sw.js")
    assert sw.status_code == 200
    for secret in (T1, T2, "fridge-key", "app-secret", "verify-me"):
        assert secret not in sw.text


def test_vapid_public_key_needs_a_token_and_404s_when_not_configured(tmp_path):
    rig = ChatRig(tmp_path)        # no vapid_public_key passed
    assert rig.client.get("/chat/vapid-public-key").status_code == 401
    assert rig.client.get("/chat/vapid-public-key", headers=rig.auth(T1)).status_code == 404


def test_vapid_public_key_is_served_when_configured(tmp_path):
    rig = ChatRig(tmp_path, vapid_public_key="a-base64url-key")
    r = rig.client.get("/chat/vapid-public-key", headers=rig.auth(T1))
    assert r.status_code == 200 and r.json() == {"key": "a-base64url-key"}


def test_push_subscribe_requires_a_token(rig):
    body = {"endpoint": "https://push.example/ep1", "p256dh": "p1", "auth": "a1"}
    assert rig.client.post("/chat/push-subscribe", json=body).status_code == 401


def test_push_subscribe_stores_it_under_the_sender(rig):
    body = {"endpoint": "https://push.example/ep1", "p256dh": "p1", "auth": "a1"}
    r = rig.client.post("/chat/push-subscribe", json=body, headers=rig.auth(T1))
    assert r.status_code == 200
    subs = rig.db.push_subscriptions(A1)
    assert subs == [{"endpoint": "https://push.example/ep1", "keys": {"p256dh": "p1", "auth": "a1"}}]
    assert rig.db.push_subscriptions(A2) == []     # not visible to another person's token


def test_push_unsubscribe_removes_it(rig):
    body = {"endpoint": "https://push.example/ep1", "p256dh": "p1", "auth": "a1"}
    rig.client.post("/chat/push-subscribe", json=body, headers=rig.auth(T1))
    r = rig.client.post("/chat/push-unsubscribe", json={"endpoint": body["endpoint"]}, headers=rig.auth(T1))
    assert r.status_code == 200
    assert rig.db.push_subscriptions(A1) == []


def test_a_sent_cart_pushes_a_notification_when_push_is_configured(tmp_path):
    notified = []

    class StubPush:
        def notify(self, recipient, title, body):
            notified.append((recipient, title, body))

    rig = ChatRig(tmp_path, push=StubPush())
    rig.fridge_push()
    assert notified and notified[0][0] == A1


@pytest.mark.parametrize("weak", ["", "short", "x" * 15])
def test_short_or_empty_tokens_are_refused_at_startup(tmp_path, weak):
    r = ChatRig(tmp_path)
    with pytest.raises(ValueError):
        create_app(r.agent, RuleIntentParser(), SECRETS, scheduler_interval=None, chat_tokens={weak: A1})


def test_a_token_for_an_unknown_recipient_is_refused_at_startup(tmp_path):
    r = ChatRig(tmp_path)
    with pytest.raises(ValueError):
        create_app(r.agent, RuleIntentParser(), SECRETS, scheduler_interval=None, chat_tokens={"x" * 20: "app:9"})


# ---------------------------------------------------------------- conversation
def test_fridge_push_reaches_every_app_user_with_buttons_and_each_sees_only_their_own(rig):
    rig.fridge_push()
    for tok in (T1, T2, T3):
        m = rig.messages(tok)
        assert len(m) == 1 and "Cart ready (R1)" in m[0]["text"]
        assert [b["title"] for b in m[0]["buttons"]] == ["Place order", "Cancel"]
        assert [b["id"] for b in m[0]["buttons"]] == ["approve:R1:1", "cancel:R1:1"]
    rig.send(T2, "add milk")
    assert any("milk" in t for t in rig.texts(T2)) and not any("milk" in t for t in rig.texts(T1))
    assert "add milk" not in [m["text"] for m in rig.messages(T3)]


def test_messages_can_be_fetched_incrementally(rig):
    rig.fridge_push()
    first = rig.messages(T1)
    assert rig.messages(T1, after=first[-1]["id"]) == []
    rig.send(T1, "add milk")
    new = rig.messages(T1, after=first[-1]["id"])
    assert [m["role"] for m in new] == ["user", "agent"]


def test_adding_an_item_in_chat_goes_to_pending_only(rig):
    assert rig.send(T1, "add 2 milk").status_code == 200
    assert [(i.name, i.qty) for i in rig.agent.pending] == [("milk", 2)]
    assert not rig.store.orders


def test_help_for_something_unclear(rig):
    rig.send(T1, "blah blah")
    assert "did not catch that" in rig.texts(T1)[-1]


# ---------------------------------------------------------------- ordering through the chat
def test_tapping_place_order_orders_and_a_second_person_is_told_it_is_handled(rig):
    rig.fridge_push()
    rig.clock.t = NOON + timedelta(minutes=1)
    assert rig.tap(T1, "approve:R1:1").status_code == 200
    assert rig.run.state == RunState.DONE and len(rig.store.orders) == 1
    rig.tap(T2, "approve:R1:1")
    assert len(rig.store.orders) == 1                                   # the second tap cannot order again
    assert any("already" in t.lower() for t in rig.texts(T2))


def test_text_yes_orders_but_loose_agreement_does_not(rig):
    rig.fridge_push()
    rig.clock.t = NOON + timedelta(minutes=1)
    rig.send(T1, "sounds good I guess")
    assert not rig.store.orders and rig.run.state == RunState.AWAITING_USER
    rig.send(T1, "yes")
    assert len(rig.store.orders) == 1


def test_cancel_button_keeps_the_items_on_the_list(rig):
    rig.fridge_push()
    rig.clock.t = NOON + timedelta(minutes=1)
    rig.tap(T3, "cancel:R1:1")
    assert rig.run.state == RunState.CANCELLED and not rig.store.orders
    assert {"Tomato", "Onion", "Cucumber", "Mushroom"} <= {i.name for i in rig.fridge.get_list()}


@pytest.mark.parametrize("bad", ["approve", "approve:R1", "approve:R1:x", "checkout:R1:1", "approve:R1:1;pay",
                                 "approve:../1:1", "x" * 200, ""])
def test_malformed_buttons_are_rejected(rig, bad):
    rig.fridge_push()
    assert rig.tap(T1, bad).status_code == 422
    assert not rig.store.orders


def test_a_tap_for_an_old_ask_cannot_approve(rig):
    rig.fridge_push()
    rig.clock.t = NOON + timedelta(minutes=1)
    rig.tap(T1, "approve:R1:99")                                        # wrong ask version
    rig.tap(T1, "approve:R7:1")                                         # a run that does not exist
    assert not rig.store.orders and rig.run.state == RunState.AWAITING_USER


class Hostile:
    def __init__(self, actions):
        self.actions = actions

    def parse(self, text, ctx):
        return self.actions


def test_a_hostile_parser_cannot_order_through_the_chat(tmp_path):
    r = ChatRig(tmp_path, parser=Hostile([{"type": "checkout"}, {"type": "approve", "run_id": "R1"},
                                          {"type": "order_now"}, {"type": "add_item", "item": "milk", "qty": 500}]))
    r.fridge_push()
    r.clock.t = NOON + timedelta(minutes=1)
    r.send(T1, "sounds great, thanks")
    assert not r.store.orders and r.run.state == RunState.AWAITING_USER and not r.agent.pending


def test_only_the_owner_can_change_mode_in_chat(rig):
    rig.send(T2, "set mode to self")
    rig.agent.handle_chat(A2, [{"type": "change_mode", "mode": "self"}], NOON)
    assert rig.agent.cfg.mode == "permission"
    rig.agent.handle_chat(A1, [{"type": "change_mode", "mode": "self"}], NOON)
    assert rig.agent.cfg.mode == "self"


def test_flood_is_cut_off_with_429(rig):
    codes = [rig.send(T1, f"hello {i}").status_code for i in range(25)]
    assert codes[:20] == [200] * 20 and 429 in codes[20:]


def test_oversized_message_is_rejected(rig):
    assert rig.send(T1, "x" * 501).status_code == 422
    assert rig.client.post("/chat/send", json={"text": ""}, headers=rig.auth(T1)).status_code == 422


def test_html_in_an_agent_reply_is_stored_as_plain_text_for_the_page_to_show_safely(rig):
    rig.agent.say(A1, "<script>alert(1)</script>", NOON)
    assert rig.texts(T1)[-1] == "<script>alert(1)</script>"            # data, and chat.js only uses text nodes


# ---------------------------------------------------------------- routing and persistence
def test_phone_and_app_recipients_each_get_the_cart_on_their_own_channel(tmp_path):
    r = ChatRig(tmp_path, with_phone=True)
    r.fridge_push()
    assert [m.to for m in r.wa.outbox] == [P1]                          # WhatsApp only for the phone number
    assert len(r.messages(T1)) == 1                                      # the app user got it in chat


def test_chat_history_survives_a_restart(tmp_path):
    r = ChatRig(tmp_path)
    r.fridge_push()
    r.send(T1, "add milk")
    again = Database(str(tmp_path / "c.db"))
    assert [m["role"] for m in again.chat_since(A1)] == ["agent", "user", "agent"]


def test_phone_webhook_cannot_impersonate_an_app_user(tmp_path):
    from tests.test_api import envelope, sign, wa_text
    import json
    r = ChatRig(tmp_path)
    body = json.dumps(envelope(wa_text("1", "m1", NOON, "add milk"))).encode()    # digits of "app:1"
    r.client.post("/webhook/whatsapp", content=body, headers={"Content-Type": "application/json",
                                                              "X-Hub-Signature-256": sign(body)})
    assert not r.agent.pending
