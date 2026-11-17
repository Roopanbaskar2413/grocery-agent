import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.api import ApiSecrets, create_app
from agent.config import Settings
from agent.db import Database
from agent.intent import RuleIntentParser
from agent.mock_fridge import MockFridge
from agent.mock_instamart import MockInstamart
from agent.orchestrator import Agent
from agent.whatsapp import (Dispatcher, OutboxMessenger, WhatsAppClient, buttons_payload, clean_param,
                            text_payload, template_payload, MAX_ATTEMPTS)
from tests.test_api import (Clock, P1, P2, P3, SECRETS, NOON, FOUR, sign, wa_text, wa_button, envelope, epoch)

BTNS = [("approve:R1:1", "Place order"), ("cancel:R1:1", "Cancel")]


class FakeMeta:
    """A scriptable stand-in for graph.facebook.com. Responses are consumed in order, then it says OK."""
    def __init__(self):
        self.sent = []          # decoded JSON payloads, in order
        self.script = []

    def handler(self, request: httpx.Request):
        self.sent.append(json.loads(request.content))
        if self.script:
            r = self.script.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return httpx.Response(200, json={"messaging_product": "whatsapp", "contacts": [{"wa_id": "x"}],
                                         "messages": [{"id": f"wamid.{len(self.sent)}"}]})


def err(status, code, message="boom", headers=None):
    return httpx.Response(status, json={"error": {"code": code, "message": message}}, headers=headers or {})


class Rig:
    def __init__(self, tmp_path):
        self.meta = FakeMeta()
        self.clock = Clock(NOON)
        self.db = Database(str(tmp_path / "w.db"))
        client = WhatsAppClient("tok", "12345", http=httpx.Client(transport=httpx.MockTransport(self.meta.handler)))
        self.d = Dispatcher(self.db.clone(), client, self.clock)
        self.out = OutboxMessenger(self.db, self.clock)

    def open_window(self, phone, when=None):
        self.db.kv_set(f"inbound:{phone}", (when or self.clock.t).isoformat())

    def row(self, i=1):
        return self.db.outbox_get(i)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------- payload builders
def test_button_payload_respects_meta_limits():
    p = buttons_payload("+919800000001", "x" * 2000, [("a:1", "A very long button title indeed"), ("b:2", "B"),
                                                      ("c:3", "C"), ("d:4", "D")])
    i = p["interactive"]
    assert p["to"] == "919800000001" and len(i["body"]["text"]) == 1024
    assert len(i["action"]["buttons"]) == 3
    assert all(len(b["reply"]["title"]) <= 20 for b in i["action"]["buttons"])


def test_duplicate_button_titles_are_made_unique():
    p = buttons_payload("1", "hi", [("a:1", "Same"), ("b:2", "Same")])
    titles = [b["reply"]["title"] for b in p["interactive"]["action"]["buttons"]]
    assert len(set(titles)) == 2


def test_template_parameter_has_no_newlines_tabs_or_long_space_runs():
    assert "\n" not in clean_param("Cart:\n- Tomato\t1\n\n- Onion     x2")
    assert "     " not in clean_param("a          b")
    assert len(clean_param("x" * 5000)) <= 900
    t = template_payload("+91 98000 00001", "grocery_cart", "en", "hi", ["approve:R1:1", "cancel:R1:1"])
    comps = t["template"]["components"]
    assert [c["type"] for c in comps] == ["body", "button", "button"]
    assert comps[1]["sub_type"] == "quick_reply" and comps[1]["index"] == "0"
    assert comps[1]["parameters"][0] == {"type": "payload", "payload": "approve:R1:1"}


# ---------------------------------------------------------------- the outbox messenger
def test_long_cart_text_is_split_into_text_then_buttons(rig):
    rig.out.send(P1, "Cart ready: " + "item " * 400, BTNS)
    rows = rig.db.outbox_all()
    assert len(rows) == 2
    assert rows[0]["buttons"] == [] and rows[1]["text"] == "Choose an option:" and rows[1]["buttons"] == BTNS


def test_short_cart_text_stays_one_message(rig):
    rig.out.send(P1, "Cart ready: Tomato", BTNS)
    assert len(rig.db.outbox_all()) == 1


# ---------------------------------------------------------------- the 24 hour window
def test_no_window_means_a_template(rig):
    rig.out.send(P1, "Cart ready (R1):\n- Tomato", BTNS)
    assert rig.d.run_once()["sent"] == 1
    sent = rig.meta.sent[0]
    assert sent["type"] == "template" and sent["template"]["name"] == "grocery_cart"
    assert "\n" not in sent["template"]["components"][0]["parameters"][0]["text"]


def test_open_window_means_free_form_buttons(rig):
    rig.open_window(P1, NOON - timedelta(hours=1))
    rig.out.send(P1, "Cart ready", BTNS)
    rig.d.run_once()
    assert rig.meta.sent[0]["type"] == "interactive"


def test_stale_window_goes_back_to_a_template(rig):
    rig.open_window(P1, NOON - timedelta(hours=25))
    rig.out.send(P1, "hello")
    rig.d.run_once()
    assert rig.meta.sent[0]["type"] == "template" and rig.meta.sent[0]["template"]["name"] == "grocery_update"


def test_cancel_only_buttons_outside_the_window_use_the_plain_template(rig):
    rig.out.send(P1, "COD is not available", [("cancel:R1:1", "Cancel")])
    rig.d.run_once()
    assert rig.meta.sent[0]["template"]["name"] == "grocery_update"


def test_meta_says_window_closed_so_we_retry_once_as_a_template(rig):
    rig.open_window(P1, NOON - timedelta(hours=1))               # we believed it was open
    rig.out.send(P1, "Reminder")
    rig.meta.script = [err(400, 131047, "Re-engagement message")]
    s = rig.d.run_once()
    assert s["retry"] == 1 and rig.row()["as_template"] is True
    rig.d.run_once()
    assert [p["type"] for p in rig.meta.sent] == ["text", "template"]
    assert rig.row()["status"] == "sent"


def test_template_also_refused_is_failed_not_looped(rig):
    rig.out.send(P1, "Reminder")
    rig.meta.script = [err(400, 131047), err(400, 131047)]
    rig.d.run_once(); rig.d.run_once(); rig.d.run_once()
    assert rig.row()["status"] == "failed" and len(rig.meta.sent) == 2


# ---------------------------------------------------------------- retries and rate limits
def test_throughput_limit_waits_then_succeeds(rig):
    rig.out.send(P1, "hi")
    rig.meta.script = [err(400, 130429, "Rate limit hit")]
    assert rig.d.run_once()["retry"] == 1
    assert rig.row()["attempts"] == 1 and rig.row()["next_attempt"] > rig.clock.t
    rig.d.run_once()
    assert len(rig.meta.sent) == 1                               # not retried before it is due
    rig.clock.t += timedelta(seconds=16)
    assert rig.d.run_once()["sent"] == 1 and rig.row()["status"] == "sent"


def test_retry_after_header_is_respected(rig):
    rig.out.send(P1, "hi")
    rig.meta.script = [err(429, 4, "too many", headers={"Retry-After": "120"})]
    rig.d.run_once()
    assert rig.row()["next_attempt"] >= rig.clock.t + timedelta(seconds=120)


def test_server_errors_and_network_errors_are_retried(rig):
    rig.out.send(P1, "a"); rig.out.send(P1, "b")
    rig.meta.script = [err(503, None, "unavailable"), httpx.ConnectError("down")]
    s = rig.d.run_once()
    assert s["retry"] == 2 and s["failed"] == 0
    assert {r["status"] for r in rig.db.outbox_all()} == {"pending"}


def test_gives_up_after_the_attempt_limit(rig):
    rig.out.send(P1, "hi")
    rig.meta.script = [err(500, None)] * MAX_ATTEMPTS
    for _ in range(MAX_ATTEMPTS):
        rig.clock.t += timedelta(hours=1)
        rig.d.run_once()
    assert rig.row()["status"] == "failed" and rig.row()["attempts"] >= MAX_ATTEMPTS - 1


def test_pair_rate_limit_waits_ten_minutes(rig):
    rig.out.send(P1, "hi")
    rig.meta.script = [err(400, 131056, "pair rate limit")]
    rig.d.run_once()
    assert rig.row()["next_attempt"] >= rig.clock.t + timedelta(minutes=10)


def test_permanent_error_fails_that_message_and_the_rest_still_go(rig):
    rig.out.send(P1, "bad"); rig.out.send(P2, "good")
    rig.meta.script = [err(400, 131026, "Message undeliverable")]
    s = rig.d.run_once()
    assert s["failed"] == 1 and s["sent"] == 1
    assert rig.row(1)["status"] == "failed" and "131026" in rig.row(1)["error"]
    assert rig.row(2)["status"] == "sent"


# ---------------------------------------------------------------- dead token
def test_rejected_token_pauses_sending_without_losing_messages(rig):
    rig.out.send(P1, "a"); rig.out.send(P2, "b")
    rig.meta.script = [err(401, 190, "Access token expired")]
    s = rig.d.run_once()
    assert s["paused"] and rig.d.auth_broken()
    assert [r["status"] for r in rig.db.outbox_all()] == ["pending", "pending"]
    before = len(rig.meta.sent)
    assert rig.d.run_once()["paused"] and len(rig.meta.sent) == before      # no hammering Meta
    rig.d.clear_auth_broken()                                                 # new token configured
    s = rig.d.run_once()
    assert s["sent"] == 2 and not rig.d.auth_broken()


# ---------------------------------------------------------------- delivery receipts
def test_receipts_move_forward_never_backward(rig):
    rig.out.send(P1, "hi")
    rig.d.run_once()
    wamid = rig.row()["wamid"]
    assert rig.db.outbox_status(wamid, "delivered") and rig.row()["status"] == "delivered"
    assert rig.db.outbox_status(wamid, "read") and rig.row()["status"] == "read"
    assert not rig.db.outbox_status(wamid, "sent") and rig.row()["status"] == "read"
    assert rig.db.outbox_status(wamid, "failed", "131049: not delivered") and rig.row()["status"] == "failed"
    assert not rig.db.outbox_status("wamid.unknown", "delivered")


# ---------------------------------------------------------------- crash safety
def test_pending_messages_survive_a_restart_and_send_exactly_once(tmp_path):
    r = Rig(tmp_path)
    r.out.send(P1, "cart"); r.out.send(P2, "cart")
    r2 = Rig(tmp_path)                                           # the process restarted: new connections
    assert r2.d.run_once()["sent"] == 2
    assert r2.d.run_once()["sent"] == 0 and len(r2.meta.sent) == 2


# ---------------------------------------------------------------- end to end through the webhook
class E2E:
    def __init__(self, tmp_path, mode="permission"):
        self.meta = FakeMeta()
        self.clock = Clock(NOON)
        db = Database(str(tmp_path / "e2e.db"))
        self.fridge, self.store = MockFridge(), MockInstamart()
        cfg = Settings(mode=mode, recipients={P1: "Roopan", P2: "Person B", P3: "Person C"})
        self.agent = Agent(self.fridge, self.store, OutboxMessenger(db, self.clock), cfg, db)
        client = WhatsAppClient("tok", "12345", http=httpx.Client(transport=httpx.MockTransport(self.meta.handler)))
        self.d = Dispatcher(db.clone(), client, self.clock)
        self.app = create_app(self.agent, RuleIntentParser(), SECRETS, clock=self.clock, scheduler_interval=None, dispatcher=self.d)
        self.c = TestClient(self.app)

    def push(self):
        return self.c.post("/fridge/list", headers={"X-Fridge-Key": "fridge-key"},
                           json={"items": [{"name": n, "qty": q} for n, q in FOUR]})

    def wa(self, *msgs, statuses=None):
        env = envelope(*msgs)
        if statuses:
            env["entry"][0]["changes"][0]["value"]["statuses"] = statuses
        body = json.dumps(env).encode()
        return self.c.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"})


def test_full_round_trip_with_template_button_taps_and_receipts(tmp_path):
    e = E2E(tmp_path)
    e.push()
    assert e.d.run_once()["sent"] == 3
    by_to = {p["to"]: p for p in e.meta.sent}
    assert set(by_to) == {"919800000001", "919800000002", "919800000003"}
    cart = by_to["919800000002"]
    assert cart["type"] == "template" and cart["template"]["name"] == "grocery_cart"
    payloads = [c["parameters"][0]["payload"] for c in cart["template"]["components"] if c["type"] == "button"]
    assert payloads == ["approve:R1:1", "cancel:R1:1"]

    # Person B taps the TEMPLATE quick-reply button: Meta sends type "button", not "interactive"
    tap = {"from": "919800000002", "id": "wamid.in1", "timestamp": str(epoch(NOON + timedelta(minutes=2))),
           "type": "button", "button": {"payload": "approve:R1:1", "text": "Place order"}}
    assert e.wa(tap).status_code == 200
    assert len(e.store.orders) == 1 and e.fridge.get_list() == []

    # the confirmation: B just wrote, so B gets free-form text; A and C have no window, so they get a template
    e.clock.t = NOON + timedelta(minutes=3)
    e.meta.sent.clear()
    assert e.d.run_once()["sent"] == 3
    kinds = {p["to"]: p["type"] for p in e.meta.sent}
    assert kinds == {"919800000002": "text", "919800000001": "template", "919800000003": "template"}
    assert "placed by Person B" in [p for p in e.meta.sent if p["type"] == "text"][0]["text"]["body"]

    # delivery receipts arrive on the same webhook
    wamid = e.agent.db.outbox_all()[0]["wamid"]
    e.wa(statuses=[{"id": wamid, "status": "delivered", "timestamp": "1"}])
    e.wa(statuses=[{"id": wamid, "status": "failed", "errors": [{"code": 131049, "title": "Not delivered"}]}])
    row = e.agent.db.outbox_all()[0]
    assert row["status"] == "failed" and "131049" in row["error"]


def test_inbound_text_opens_the_window_and_the_normal_button_tap_still_works(tmp_path):
    e = E2E(tmp_path)
    e.push()
    e.wa(wa_text(P1, "m1", NOON + timedelta(minutes=1), "hello"))          # Roopan writes: window opens for P1
    assert e.d.window_open(P1, e.clock.t)
    assert not e.d.window_open(P2, e.clock.t)
    e.wa(wa_button(P3, "m2", NOON + timedelta(minutes=1), "approve:R1:1"))  # a normal interactive reply
    assert len(e.store.orders) == 1


def test_malformed_template_payload_cannot_approve(tmp_path):
    e = E2E(tmp_path)
    e.push()
    for bad in ("approve", "approve:R1", "approve:R1:x", "checkout:R1:1", "approve:R1:1; checkout", ""):
        e.wa({"from": "919800000001", "id": f"b{bad}", "timestamp": str(epoch(NOON)), "type": "button",
              "button": {"payload": bad, "text": "x"}})
    assert not e.store.orders


def test_quiet_hours_keep_the_outbox_empty(tmp_path):
    e = E2E(tmp_path)
    e.clock.t = NOON.replace(hour=23)
    e.c.post("/fridge/list", headers={"X-Fridge-Key": "fridge-key"},
             json={"items": [{"name": n, "qty": q} for n, q in FOUR]})
    assert e.agent.db.outbox_all() == []                         # held by the agent until morning
    e.clock.t = NOON.replace(day=18, hour=7, minute=30)
    e.agent.service(e.clock.t)
    assert len(e.agent.db.outbox_all()) == 3


def test_health_reports_a_dead_token_and_status_shows_the_outbox(tmp_path):
    e = E2E(tmp_path)
    e.push()
    e.meta.script = [err(401, 190)]
    e.d.run_once()
    assert e.c.get("/health").json()["ok"] is False
    st = e.c.get("/status", headers={"X-Fridge-Key": "fridge-key"}).json()
    assert st["outbox"] == {"pending": 3}


@pytest.mark.parametrize("status,code", [(400, 190), (400, 0), (403, 200), (400, 299), (401, None)])
def test_each_kind_of_auth_failure_pauses_sending(rig, status, code):
    """Meta reports a dead token in several ways: by error code alone, or by HTTP 401 alone."""
    rig.out.send(P1, "a")
    rig.meta.script = [err(status, code, "auth")]
    assert rig.d.run_once()["paused"] and rig.d.auth_broken()
    assert rig.row()["status"] == "pending"


def test_an_unknown_client_error_is_permanent_not_retried_forever(rig):
    rig.out.send(P1, "a")
    rig.meta.script = [err(400, 100, "Invalid parameter")]
    assert rig.d.run_once()["failed"] == 1 and rig.row()["status"] == "failed"
