from datetime import datetime, timedelta

import pytest

from agent.config import Settings
from agent.decide import Decision, decide
from agent.llm_gate import validate_batch
from agent.matcher import match_item
from agent.messenger import ConsoleMessenger
from agent.mock_fridge import MockFridge
from agent.mock_instamart import MockInstamart
from agent.models import ListItem, RunState
from agent.orchestrator import Agent

A, B, C = "+91-PHONE-A", "+91-PHONE-B", "+91-PHONE-C"
NOON = datetime(2026, 11, 17, 12, 0)
FOUR = ["Tomato", "Onion", "Cucumber", "Mushroom"]


def make(items=FOUR, mode="permission", cod=True, **cfg):
    fridge = MockFridge([ListItem(n) for n in items])
    store = MockInstamart(cod_available=cod)
    msg = ConsoleMessenger()
    agent = Agent(fridge, store, msg, Settings(mode=mode, **cfg))
    return agent, fridge, store, msg


def texts_to(msg, phone):
    return [m.text for m in msg.outbox if m.to == phone]


# ---------------------------------------------------------------- trigger rules
def test_decide_rules():
    cfg = Settings()
    t = lambda h: datetime(2026, 11, 17, h, 0)
    assert decide(t(9), 4, False, cfg) == Decision.ORDER
    assert decide(t(9), 3, False, cfg) == Decision.HOLD
    assert decide(t(18), 2, False, cfg) == Decision.ORDER
    assert decide(t(18), 1, False, cfg) == Decision.HOLD       # a single item waits
    assert decide(t(9), 1, True, cfg) == Decision.ORDER        # Order now skips the wait
    assert decide(t(9), 0, True, cfg) == Decision.HOLD


def test_three_items_morning_hold_then_evening_order():
    agent, fridge, store, msg = make(["Tomato", "Onion", "Cucumber"], mode="self")
    assert agent.tick(NOON.replace(hour=9)) is None
    assert "Waiting for more items" in fridge.display
    run = agent.tick(NOON.replace(hour=18))
    assert run.state == RunState.DONE


def test_out_of_stock_items_do_not_count_and_stay_on_list():
    agent, fridge, store, msg = make(["Tomato", "Onion", "Cucumber", "Milk"])
    store.set_stock("p5", False)
    assert agent.tick(NOON) is None                      # only 3 in stock
    assert [i.name for i in fridge.get_list()] == ["Tomato", "Onion", "Cucumber", "Milk"]


# ---------------------------------------------------------------- matcher
def test_matcher():
    store = MockInstamart()
    assert match_item("tomatoes", store.search_products("tomatoes")).product.name == "Tomato Hybrid"
    assert match_item("Tomato", store.search_products("Tomato")).product.id == "p1"   # not ketchup
    assert match_item("Quinoa", store.search_products("Quinoa")).kind == "no_match"
    store.set_stock("p5", False)
    assert match_item("Milk", store.search_products("Milk")).kind == "out_of_stock"


# ---------------------------------------------------------------- permission mode
def test_permission_mode_asks_and_first_reply_wins():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    assert run.state == RunState.AWAITING_USER
    assert "checkout" not in store.calls and not store.orders
    assert len(fridge.get_list()) == 4                           # nothing removed yet
    assert all(any("Cart ready" in t for t in texts_to(msg, p)) for p in (A, B, C))

    agent.handle_reply(B, "approve", run.id, NOON + timedelta(minutes=5))
    assert run.state == RunState.DONE and len(store.orders) == 1
    assert fridge.get_list() == []                               # removed only after the order

    agent.handle_reply(C, "approve", run.id, NOON + timedelta(minutes=6))
    assert len(store.orders) == 1                                # never a second order
    assert "already handled by Person B" in texts_to(msg, C)[-1]


def test_unknown_sender_and_wrong_run_cannot_approve():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    agent.handle_reply("+91-STRANGER", "approve", run.id, NOON)
    agent.handle_reply(A, "approve", "R99", NOON)
    assert not store.orders and run.state == RunState.AWAITING_USER


def test_stale_button_after_expiry():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    agent.tick(NOON + timedelta(hours=7))
    agent.handle_reply(A, "approve", run.id, NOON + timedelta(hours=8))
    assert not store.orders
    assert "no longer open" in texts_to(msg, A)[-1]


# ---------------------------------------------------------------- timeout (new)
def test_unanswered_approval_gets_one_reminder_then_expires():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    agent.tick(NOON + timedelta(minutes=30))
    assert not any("Reminder" in t for t in texts_to(msg, A))
    agent.tick(NOON + timedelta(minutes=61))
    agent.tick(NOON + timedelta(minutes=120))
    assert sum("Reminder" in t for t in texts_to(msg, A)) == 1   # only one reminder
    agent.tick(NOON + timedelta(minutes=361))
    assert run.state == RunState.EXPIRED
    assert store.get_cart().lines == []                          # no stale cart left
    assert len(fridge.get_list()) == 4                           # items stay on the list
    assert not store.orders


# ---------------------------------------------------------------- re-check at checkout (new)
def test_price_rise_after_approval_is_caught_before_checkout():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    first_total = run.approved_total
    store.set_price("p1", 90)                                    # tomato jumps while we wait
    agent.handle_reply(A, "approve", run.id, NOON + timedelta(minutes=10))
    assert not store.orders and "checkout" not in store.calls    # stopped before checkout
    assert run.state == RunState.AWAITING_USER and run.approved_total > first_total
    assert any("price changed" in t for t in texts_to(msg, A))
    agent.handle_reply(A, "approve", run.id, NOON + timedelta(minutes=12))   # approves the new total
    assert run.state == RunState.DONE and store.orders[0]["total"] == run.approved_total


def test_small_price_change_within_tolerance_goes_through():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    store.set_price("p1", 31)                                    # +1 rupee
    agent.handle_reply(A, "approve", run.id, NOON)
    assert run.state == RunState.DONE


def test_item_out_of_stock_after_approval_is_dropped_not_ordered():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    store.set_stock("p4", False)                                 # mushroom gone
    agent.handle_reply(A, "approve", run.id, NOON)
    assert run.state == RunState.DONE
    assert [i.name for i in fridge.get_list()] == ["Mushroom"]   # still on the list
    assert any("Out of stock: Mushroom" in t for t in texts_to(msg, A))


# ---------------------------------------------------------------- self mode, COD
def test_self_mode_orders_quietly_and_cleans_list():
    agent, fridge, store, msg = make(mode="self")
    run = agent.tick(NOON)
    assert run.state == RunState.DONE and len(store.orders) == 1
    assert fridge.get_list() == [] and msg.outbox == []          # no message when nothing is missing


def test_self_mode_reports_out_of_stock():
    agent, fridge, store, msg = make(FOUR + ["Milk"], mode="self")
    store.set_stock("p5", False)
    agent.tick(NOON)
    assert any("Out of stock: Milk" in t for t in texts_to(msg, A))
    assert "Out of stock: Milk" in fridge.display
    assert [i.name for i in fridge.get_list()] == ["Milk"]


def test_cod_unavailable_then_user_pays_in_app():
    agent, fridge, store, msg = make(mode="self", cod=False)
    run = agent.tick(NOON)
    assert run.state == RunState.AWAITING_APP_PAYMENT
    assert "COD" in fridge.display and any("COD is not available" in t for t in texts_to(msg, A))
    assert len(fridge.get_list()) == 4                           # not removed until paid
    store.simulate_app_payment()
    agent.tick(NOON + timedelta(minutes=20))
    assert run.state == RunState.DONE and fridge.get_list() == []


def test_unpaid_app_cart_reminds_then_expires():
    agent, fridge, store, msg = make(mode="self", cod=False)
    run = agent.tick(NOON)
    agent.tick(NOON + timedelta(minutes=61))
    assert any("waiting for payment" in t for t in texts_to(msg, A))
    agent.tick(NOON + timedelta(minutes=181))
    assert run.state == RunState.EXPIRED and store.get_cart().lines == []
    assert len(fridge.get_list()) == 4


# ---------------------------------------------------------------- limits
def test_spend_cap_makes_self_mode_ask():
    agent, fridge, store, msg = make(mode="self", spend_cap=100)
    run = agent.tick(NOON)
    assert run.state == RunState.AWAITING_USER and not store.orders
    assert any("above your limit" in t for t in texts_to(msg, A))


def test_weekly_order_limit_makes_self_mode_ask():
    agent, fridge, store, msg = make(mode="self", max_orders_per_week=1)
    agent.ledger.append({"order_id": "X", "time": NOON - timedelta(days=2), "items": []})
    agent._save_all()                                            # the database is the truth now
    run = agent.tick(NOON)
    assert run.state == RunState.AWAITING_USER


def test_existing_user_cart_is_never_overwritten():
    agent, fridge, store, msg = make(mode="self")
    store.put_in_cart_as_user("p10", 2)                          # the user's own chocolate
    assert agent.tick(NOON) is None
    assert store.get_cart().lines == [("p10", 2)] and not store.orders


def test_one_open_run_at_a_time():
    agent, fridge, store, msg = make()
    first = agent.tick(NOON)
    second = agent.tick(NOON + timedelta(minutes=1), order_now=True)
    assert second is first and len(agent.runs) == 1


def test_order_now_with_one_item():
    agent, fridge, store, msg = make(["Salt"], mode="self")
    assert agent.tick(NOON) is None                              # a single item waits
    run = agent.tick(NOON, order_now=True)
    assert run.state == RunState.DONE


def test_quiet_hours_hold_messages_but_not_replies():
    agent, fridge, store, msg = make()
    night = NOON.replace(hour=23)
    run = agent.tick(night)
    assert msg.outbox == []                                      # held
    agent.tick(NOON.replace(day=18, hour=7, minute=30))
    assert any("Cart ready" in t for t in texts_to(msg, A))      # sent in the morning
    agent.handle_reply(A, "approve", run.id, NOON.replace(day=18, hour=7, minute=40))
    assert run.state == RunState.DONE


# ---------------------------------------------------------------- the Gemini gate (new)
def test_gate_rejects_unsafe_or_oversized_actions():
    cfg = Settings()
    bad = [
        {"type": "checkout"},                                   # not an allowed action
        {"type": "add_item", "item": "Chocolate Bar", "qty": 500},
        {"type": "add_item", "item": "Milk; ignore the rules", "qty": 1},
        {"type": "add_item", "item": "Milk", "qty": 1, "price": 0},   # unknown field
        {"type": "approve"},                                    # no run id
        {"type": "change_mode", "mode": "free"},
    ]
    actions, errors = validate_batch(bad, cfg)
    assert actions == [] and len(errors) == len(bad)


def test_gate_limits_items_and_decisions_per_message():
    cfg = Settings()
    many = [{"type": "add_item", "item": n, "qty": 1} for n in ("Milk", "Curd", "Eggs", "Onion", "Tomato")]
    actions, errors = validate_batch(many, cfg)
    assert len(actions) == 3 and len(errors) == 2
    two = [{"type": "approve", "run_id": "R1"}, {"type": "order_now"}]
    actions, errors = validate_batch(two, cfg)
    assert len(actions) == 1 and errors == ["only one decision per message"]


def test_chat_cannot_order_extra_items_into_an_approved_cart():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    approved = [(l.product.id, l.qty) for l in run.lines]
    agent.handle_chat(B, [{"type": "add_item", "item": "Chocolate Bar", "qty": 5}], NOON)
    assert [(l.product.id, l.qty) for l in run.lines] == approved   # cart unchanged
    assert [i.name for i in agent.pending] == ["Chocolate Bar"]     # waits for the next run
    agent.handle_reply(A, "approve", run.id, NOON)
    assert all(pid != "p10" for pid, _ in store.orders[0]["lines"])
    assert [i.name for i in agent.pending] == ["Chocolate Bar"]


def test_chat_flood_is_capped_per_day():
    agent, fridge, store, msg = make()
    for i in range(6):
        agent.handle_chat(A, [{"type": "add_item", "item": f"Thing {chr(97 + i)}", "qty": 5}] * 3, NOON)
    assert len(agent.pending) <= agent.cfg.max_chat_items_per_day


def test_chat_cannot_checkout_and_gate_has_no_path_to_it():
    agent, fridge, store, msg = make()
    agent.handle_chat(A, [{"type": "checkout"}, {"type": "pay"}], NOON)
    assert not store.orders and "checkout" not in store.calls


def test_only_owner_can_change_mode_and_strangers_are_ignored():
    agent, fridge, store, msg = make()
    agent.handle_chat(B, [{"type": "change_mode", "mode": "self"}], NOON)
    assert agent.cfg.mode == "permission"
    agent.handle_chat("+91-STRANGER", [{"type": "change_mode", "mode": "self"}], NOON)
    assert agent.cfg.mode == "permission"
    agent.handle_chat(A, [{"type": "change_mode", "mode": "self"}], NOON)
    assert agent.cfg.mode == "self"


def test_chat_items_count_toward_the_threshold():
    agent, fridge, store, msg = make(["Tomato", "Onion", "Cucumber"])
    agent.handle_chat(A, [{"type": "add_item", "item": "Milk", "qty": 1}], NOON)
    run = agent.tick(NOON)
    assert run is not None and run.state == RunState.AWAITING_USER


# ---------------------------------------------------------------- self-mode gap (closed)
def test_self_mode_asks_when_a_chat_added_item_is_in_the_cart():
    agent, fridge, store, msg = make(["Tomato", "Onion", "Cucumber"], mode="self")
    agent.handle_chat(A, [{"type": "add_item", "item": "Milk", "qty": 1}], NOON)
    run = agent.tick(NOON)
    assert run.state == RunState.AWAITING_USER and not store.orders
    assert any("added in chat (Milk)" in t for t in texts_to(msg, A))
    agent.handle_reply(A, "approve", run.id, NOON)
    assert run.state == RunState.DONE and len(store.orders) == 1


def test_self_mode_asks_when_chat_adds_to_a_fridge_item():
    agent, fridge, store, msg = make(FOUR, mode="self")
    agent.handle_chat(A, [{"type": "add_item", "item": "Tomato", "qty": 3}], NOON)   # tomato x1 -> x4
    run = agent.tick(NOON)
    assert run.state == RunState.AWAITING_USER and not store.orders


def test_self_mode_still_orders_quietly_with_fridge_items_only():
    agent, fridge, store, msg = make(FOUR, mode="self")
    assert agent.tick(NOON).state == RunState.DONE and msg.outbox == []



# ---------------------------------------------------------------- persistence (new)
from agent.db import Database


def make_pair(tmp_path, items=FOUR, mode="permission", cod=True):
    """Two agents sharing one database file, like a restart or a second server process."""
    path = str(tmp_path / "agent.db")
    fridge = MockFridge([ListItem(n) for n in items])
    store = MockInstamart(cod_available=cod)

    def new_agent():
        msg = ConsoleMessenger()
        return Agent(fridge, store, msg, Settings(mode=mode), Database(path)), msg
    return new_agent, fridge, store


def test_open_run_survives_a_restart(tmp_path):
    new_agent, fridge, store = make_pair(tmp_path)
    a1, msg1 = new_agent()
    run = a1.tick(NOON)
    a2, msg2 = new_agent()                                       # the process restarted
    assert [r.id for r in a2.runs] == [run.id]
    restored = a2.open_run()
    assert restored.state == RunState.AWAITING_USER and restored.approved_total == run.approved_total
    assert [(l.item_name, l.qty, l.product.id) for l in restored.lines] == \
           [(l.item_name, l.qty, l.product.id) for l in run.lines]
    a2.handle_reply(A, "approve", run.id, NOON + timedelta(minutes=5))
    assert len(store.orders) == 1 and fridge.get_list() == []


def test_timers_and_reminder_flag_survive_a_restart(tmp_path):
    new_agent, fridge, store = make_pair(tmp_path)
    a1, msg1 = new_agent()
    a1.tick(NOON)
    a1.tick(NOON + timedelta(minutes=61))                        # reminder sent
    a2, msg2 = new_agent()
    a2.tick(NOON + timedelta(minutes=90))
    assert not any("Reminder" in t for t in texts_to(msg2, A))   # not sent twice after the restart
    a2.tick(NOON + timedelta(minutes=361))
    assert a2.runs[0].state == RunState.EXPIRED and store.get_cart().lines == []


def test_ledger_pending_watchlist_deferred_and_mode_survive(tmp_path):
    new_agent, fridge, store = make_pair(tmp_path, mode="self")
    a1, _ = new_agent()
    a1.handle_chat(A, [{"type": "add_item", "item": "Curd", "qty": 2},
                       {"type": "watch_item", "item": "Mushroom"}], NOON)
    a1.handle_chat(A, [{"type": "change_mode", "mode": "permission"}], NOON)
    a1.tick(NOON.replace(hour=23))                               # quiet hours: message held
    a1.ledger.append({"order_id": "X1", "time": NOON, "items": ["Milk"]})
    a1._save_all()
    a2, msg2 = new_agent()
    assert [(i.name, i.qty, i.source) for i in a2.pending] == [("Curd", 2, "whatsapp")]
    assert a2.watchlist == {"mushroom"} and a2.cfg.mode == "permission"
    assert [o["order_id"] for o in a2.ledger] == ["X1"]
    assert len(a2.deferred) == 1
    a2.tick(NOON.replace(day=18, hour=7, minute=30))             # morning: the held message goes out
    assert any("Cart ready" in t for t in texts_to(msg2, A))


def test_two_processes_cannot_both_order(tmp_path):
    new_agent, fridge, store = make_pair(tmp_path)
    a1, msg1 = new_agent()
    run = a1.tick(NOON)
    a2, msg2 = new_agent()
    b1, b2 = a1.open_run(), a2.open_run()
    assert b1.state == b2.state == RunState.AWAITING_USER
    a1.handle_reply(A, "approve", run.id, NOON)
    a2.handle_reply(B, "approve", run.id, NOON)                  # a2 still believed it was open
    assert len(store.orders) == 1
    assert "already handled by Roopan" in texts_to(msg2, B)[-1]


def test_atomic_claim_is_first_come_only(tmp_path):
    new_agent, fridge, store = make_pair(tmp_path)
    a1, _ = new_agent()
    run = a1.tick(NOON)
    db = Database(str(tmp_path / "agent.db"))                   # a second connection, like a second process
    assert db.claim_run(run.id, A) is True
    assert db.claim_run(run.id, B) is False
    assert db.get_run(run.id).handled_by == A


def test_run_interrupted_during_checkout_is_never_retried(tmp_path):
    new_agent, fridge, store = make_pair(tmp_path)
    a1, _ = new_agent()
    run = a1.tick(NOON)
    assert a1.db.claim_run(run.id, A)                            # claimed, then the process "died"
    a2, msg2 = new_agent()
    restored = a2.runs[0]
    assert restored.state == RunState.FAILED and restored.note == "interrupted during checkout"
    assert not store.orders and "checkout" not in store.calls
    a2.tick(NOON.replace(day=18, hour=8))
    assert any("Check Instamart for an order" in t for t in texts_to(msg2, A))
    a2.handle_reply(A, "approve", run.id, NOON.replace(day=18, hour=8, minute=5))
    assert not store.orders                                      # a failed run can never be approved


def test_run_ids_keep_counting_after_a_restart(tmp_path):
    new_agent, fridge, store = make_pair(tmp_path, mode="self")
    a1, _ = new_agent()
    assert a1.tick(NOON).id == "R1"
    fridge.items = [ListItem(n) for n in FOUR]
    a2, _ = new_agent()
    assert a2.tick(NOON + timedelta(days=1)).id == "R2"


# ---------------------------------------------------------------- unforgeable approvals (new)
def test_old_button_cannot_approve_after_the_price_changed():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    assert run.ask_version == 1
    store.set_price("p1", 90)
    agent.handle_reply(A, "approve", run.id, NOON, ask=1)            # re-asks with the new total
    assert run.ask_version == 2 and not store.orders
    agent.handle_reply(B, "approve", run.id, NOON, ask=1)            # an old button from the first message
    assert not store.orders
    assert "out of date" in texts_to(msg, B)[-1]
    agent.handle_reply(B, "approve", run.id, NOON, ask=2)            # the new button works
    assert run.state == RunState.DONE and store.orders[0]["total"] == run.approved_total


def test_text_yes_sent_before_the_cart_was_shown_is_ignored():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    agent.handle_reply(A, "approve", run.id, NOON, sent_at=NOON - timedelta(minutes=1))
    assert not store.orders and "before the latest cart" in texts_to(msg, A)[-1]
    agent.handle_reply(A, "approve", run.id, NOON, sent_at=NOON + timedelta(minutes=1))
    assert run.state == RunState.DONE


def test_gate_accepts_a_valid_ask_and_rejects_a_bad_one():
    actions, errors = validate_batch([{"type": "approve", "run_id": "R1", "ask": 2}], Settings())
    assert actions[0].ask == 2 and not errors
    for bad in ("2", -1, True, 1.5):
        actions, errors = validate_batch([{"type": "approve", "run_id": "R1", "ask": bad}], Settings())
        assert actions == [] and errors == ["invalid ask"]


def test_service_is_cheap_and_never_searches():
    agent, fridge, store, msg = make()
    agent.tick(NOON)
    before = store.calls.count("search_products")
    agent.service(NOON + timedelta(minutes=61))
    assert store.calls.count("search_products") == before
    assert any("Reminder" in t for t in texts_to(msg, A))


def test_fridge_silence_nudges_once_per_silence():
    agent, fridge, store, msg = make()
    agent.note_fridge_push(NOON)
    assert agent.fridge_silence_check(NOON + timedelta(days=1)) is False
    assert agent.fridge_silence_check(NOON + timedelta(days=2)) is True
    assert agent.fridge_silence_check(NOON + timedelta(days=3)) is False      # only once
    agent.note_fridge_push(NOON + timedelta(days=3))                          # fridge is back
    assert agent.fridge_silence_check(NOON + timedelta(days=5, hours=1)) is True


def test_text_reply_within_clock_skew_counts_but_older_does_not():
    agent, fridge, store, msg = make()
    run = agent.tick(NOON)
    agent.handle_reply(A, "approve", run.id, NOON, sent_at=NOON - timedelta(seconds=30))
    assert not store.orders                                                  # 30 s before the cart: ignored
    agent.handle_reply(A, "approve", run.id, NOON, sent_at=NOON - timedelta(seconds=5))
    assert run.state == RunState.DONE                                        # same-second timestamp: fine
