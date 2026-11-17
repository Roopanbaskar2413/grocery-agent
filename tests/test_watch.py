"""Stock watcher: 'remind me when X is back' against the mock store. Read-only, hourly, capped, expires."""
from datetime import timedelta

from agent.db import Database
from agent.models import RunState
from agent.scheduler import Scheduler
from tests.test_agent import A, B, C, FOUR, NOON, make, texts_to


def watch(agent, item, now=NOON, sender=A):
    return agent.handle_chat(sender, [{"type": "watch_item", "item": item}], now)


def milk(store):
    return next(p for p in store.products.values() if p.name.startswith("Milk"))


def test_back_in_stock_adds_to_pending_and_tells_everyone_once():
    agent, fridge, store, msg = make(items=[])
    store.set_stock(milk(store).id, False)
    watch(agent, "milk")
    assert agent.watch_check(NOON) == []                              # still out of stock: nothing happens
    assert not agent.pending and agent.watchlist == {"milk"}
    store.set_stock(milk(store).id, True)
    assert agent.watch_check(NOON + timedelta(hours=1)) == ["milk"]
    assert [(i.name, i.qty, i.source) for i in agent.pending] == [("milk", 1, "watch")]
    assert agent.watchlist == set()                                   # one notification per watch
    for phone in (A, B, C):
        assert any("milk is back in stock" in t for t in texts_to(msg, phone))
    assert agent.watch_check(NOON + timedelta(hours=2)) == []         # nothing more to say


def test_watching_an_item_already_in_stock_fires_on_the_next_pass_not_never():
    agent, fridge, store, msg = make(items=[])
    watch(agent, "milk")
    assert agent.watch_check(NOON + timedelta(minutes=1)) == ["milk"]


def test_checked_at_most_once_per_interval():
    agent, fridge, store, msg = make(items=[])
    store.set_stock(milk(store).id, False)
    watch(agent, "milk")
    agent.watch_check(NOON)
    store.calls.clear()
    agent.watch_check(NOON + timedelta(minutes=30))
    assert store.calls == []                                          # too soon, no store traffic
    agent.watch_check(NOON + timedelta(minutes=61))
    assert store.calls == ["search_products"]


def test_watcher_is_read_only_it_never_touches_the_cart_or_orders():
    agent, fridge, store, msg = make(items=[])
    watch(agent, "milk")
    agent.watch_check(NOON + timedelta(minutes=1))
    assert set(store.calls) == {"search_products"} and not store.orders
    assert agent.open_run() is None                                   # no order is started by the watcher


def test_watched_item_joins_the_next_run_and_counts_toward_the_four():
    agent, fridge, store, msg = make(items=["Tomato", "Onion", "Cucumber"])
    watch(agent, "milk")
    agent.watch_check(NOON + timedelta(minutes=1))
    run = agent.tick(NOON + timedelta(minutes=2))                     # 3 on the fridge + milk = 4
    assert run is not None and run.state == RunState.AWAITING_USER
    assert {l.item_name for l in run.lines} == {"Tomato", "Onion", "Cucumber", "milk"}


def test_item_already_on_the_list_is_not_added_twice():
    agent, fridge, store, msg = make(items=["Milk"])
    watch(agent, "milk")
    agent.watch_check(NOON + timedelta(minutes=1))
    assert not agent.pending
    assert any("already on your list" in t for t in texts_to(msg, A))


def test_store_error_keeps_the_watch_and_retries_next_time():
    agent, fridge, store, msg = make(items=[])
    watch(agent, "milk")
    real = store.search_products
    store.search_products = lambda q: (_ for _ in ()).throw(ConnectionError("down"))
    assert agent.watch_check(NOON + timedelta(minutes=1)) == []
    assert agent.watchlist == {"milk"}
    store.search_products = real
    assert agent.watch_check(NOON + timedelta(minutes=70)) == ["milk"]


def test_one_failing_item_does_not_block_the_others():
    agent, fridge, store, msg = make(items=[])
    watch(agent, "curd"); watch(agent, "milk")
    real = store.search_products
    def flaky(q):
        if q == "curd":
            raise ConnectionError("down")
        return real(q)
    store.search_products = flaky
    assert agent.watch_check(NOON + timedelta(minutes=1)) == ["milk"]
    assert agent.watchlist == {"curd"}


def test_watch_cap_and_duplicates():
    agent, fridge, store, msg = make(items=[], watch_max_items=2)
    watch(agent, "milk"); watch(agent, "milk"); watch(agent, "curd"); watch(agent, "eggs")
    assert agent.watchlist == {"milk", "curd"}
    texts = texts_to(msg, A)
    assert any("already watching milk" in t for t in texts) and any("2 items at once" in t for t in texts)


def test_a_watch_that_never_fires_expires_with_a_message():
    agent, fridge, store, msg = make(items=[], watch_expiry_days=3)
    store.set_stock(milk(store).id, False)
    watch(agent, "milk")
    agent.watch_check(NOON + timedelta(days=1))
    assert agent.watchlist == {"milk"}
    agent.watch_check(NOON + timedelta(days=4))
    assert agent.watchlist == set()
    assert any("stopped watching milk after 3 days" in t for t in texts_to(msg, B))


def test_remove_stops_the_watch_too():
    agent, fridge, store, msg = make(items=[])
    watch(agent, "milk")
    agent.handle_chat(A, [{"type": "remove_item", "item": "milk"}], NOON)
    assert agent.watchlist == set()
    assert agent.watch_check(NOON + timedelta(hours=2)) == []


def test_notification_waits_for_morning_in_quiet_hours():
    agent, fridge, store, msg = make(items=[])
    watch(agent, "milk")
    night = NOON.replace(hour=23)
    agent.watch_check(night)
    assert not any("milk is back in stock" in t for t in texts_to(msg, A))    # held, not sent at night
    agent.service(NOON.replace(day=18, hour=7, minute=5))
    assert any("milk is back in stock" in t for t in texts_to(msg, A))
    assert [i.name for i in agent.pending] == ["milk"]                # but the item is already queued


def test_watchlist_and_expiry_survive_a_restart(tmp_path):
    from agent.messenger import ConsoleMessenger
    from agent.mock_fridge import MockFridge
    from agent.mock_instamart import MockInstamart
    from agent.config import Settings
    from agent.orchestrator import Agent
    path = str(tmp_path / "w.db")
    cfg = Settings(watch_expiry_days=3)
    a1 = Agent(MockFridge([]), MockInstamart(), ConsoleMessenger(), cfg, Database(path))
    a1.store.set_stock(milk(a1.store).id, False)
    watch(a1, "milk")
    a2 = Agent(MockFridge([]), MockInstamart(), ConsoleMessenger(), cfg, Database(path))
    a2.store.set_stock(milk(a2.store).id, False)
    assert a2.watchlist == {"milk"}
    a2.watch_check(NOON + timedelta(days=4))
    assert a2.watchlist == set()                                       # the start date was remembered


def test_scheduler_runs_the_watcher():
    agent, fridge, store, msg = make(items=[])
    import threading
    s = Scheduler(agent, lambda: NOON + timedelta(minutes=1), threading.RLock())
    watch(agent, "milk")
    s.run_once()
    assert [i.name for i in agent.pending] == ["milk"]
