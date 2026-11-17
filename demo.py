"""Run: python3 demo.py
Plays the main scenarios against the mock fridge and mock Instamart and prints what would be
sent on WhatsApp. No network, no keys."""
from datetime import datetime, timedelta

from agent.config import Settings
from agent.messenger import ConsoleMessenger
from agent.mock_fridge import MockFridge
from agent.mock_instamart import MockInstamart
from agent.models import ListItem
from agent.orchestrator import Agent

A, B, C = "+91-PHONE-A", "+91-PHONE-B", "+91-PHONE-C"
T0 = datetime(2026, 11, 17, 12, 0)


def setup(items, mode="permission", cod=True):
    fridge = MockFridge([ListItem(n) for n in items])
    store = MockInstamart(cod_available=cod)
    msg = ConsoleMessenger()
    return Agent(fridge, store, msg, Settings(mode=mode)), fridge, store, msg


def flush(msg, start):
    for m in msg.outbox[start:]:
        if m.to == "+91-PHONE-A":                       # same text goes to all 3, print once
            btn = f"   [{' | '.join(b[1] for b in m.buttons)}]" if m.buttons else ""
            print(f"   WhatsApp: {m.text}{btn}")
    return len(msg.outbox)


def scenario(title):
    print(f"\n=== {title}")


# 1 ---------------------------------------------------------------------------
scenario("Permission mode: 4 items, one out of stock, price changes before checkout, two phones reply")
agent, fridge, store, msg = setup(["Tomato", "Onion", "Cucumber", "Mushroom", "Milk"])
store.set_stock("p5", False)
n = 0
run = agent.tick(T0)
n = flush(msg, n)
print("   Fridge shows:", fridge.display)
store.set_price("p1", 90)
print("   -- tomato price rises to 90 while we wait; Person B taps Place order")
agent.handle_reply(B, "approve", run.id, T0 + timedelta(minutes=10)); n = flush(msg, n)
print("   -- Roopan approves the new total; Person C taps right after")
agent.handle_reply(A, "approve", run.id, T0 + timedelta(minutes=12)); n = flush(msg, n)
agent.handle_reply(C, "approve", run.id, T0 + timedelta(minutes=12)); 
print("   Person C received:", [m.text for m in msg.outbox if m.to == C][-1])
print("   Fridge list now:", [i.name for i in fridge.get_list()], "| display:", fridge.display)

# 2 ---------------------------------------------------------------------------
scenario("Permission mode: nobody replies")
agent, fridge, store, msg = setup(["Tomato", "Onion", "Cucumber", "Mushroom"])
n = 0
agent.tick(T0); n = flush(msg, n)
agent.tick(T0 + timedelta(minutes=61)); n = flush(msg, n)
agent.tick(T0 + timedelta(minutes=361)); n = flush(msg, n)
print("   Cart in Instamart:", store.get_cart().lines, "| list kept:", [i.name for i in fridge.get_list()])

# 3 ---------------------------------------------------------------------------
scenario("Self mode, COD not available: user pays in the app")
agent, fridge, store, msg = setup(["Tomato", "Onion", "Cucumber", "Mushroom"], mode="self", cod=False)
n = 0
agent.tick(T0); n = flush(msg, n)
print("   Fridge shows:", fridge.display)
store.simulate_app_payment()
agent.tick(T0 + timedelta(minutes=25)); n = flush(msg, n)
print("   Fridge list now:", [i.name for i in fridge.get_list()])

# 4 ---------------------------------------------------------------------------
scenario("Only 2 items: wait, then order at 6 PM (self mode, COD)")
agent, fridge, store, msg = setup(["Tomato", "Onion"], mode="self")
agent.tick(T0.replace(hour=9)); print("   9 AM fridge shows:", fridge.display)
run = agent.tick(T0.replace(hour=18)); print("   6 PM:", run.state.value, "| WhatsApp messages sent:", len(msg.outbox))

# 5 ---------------------------------------------------------------------------
scenario("Someone tries to talk Gemini into ordering extra stuff")
agent, fridge, store, msg = setup(["Tomato", "Onion", "Cucumber", "Mushroom"])
run = agent.tick(T0)
attempts = [
    {"type": "checkout"},
    {"type": "add_item", "item": "Chocolate Bar", "qty": 500},
    {"type": "add_item", "item": "Chocolate Bar", "qty": 3},
]
done = agent.handle_chat(B, attempts, T0)
print("   Actions accepted:", done)
print("   Approved cart lines unchanged:", [(l.item_name, l.qty) for l in run.lines])
print("   Waiting for the next order:", [(i.name, i.qty) for i in agent.pending])
print("   Orders placed:", len(store.orders))

# 6 ---------------------------------------------------------------------------
scenario("Restart while a cart waits for approval, and a chat-added item in self mode")
import os, tempfile
from agent.db import Database
path = os.path.join(tempfile.mkdtemp(), "agent.db")
fridge = MockFridge([ListItem(n) for n in ["Tomato", "Onion", "Cucumber"]])
store = MockInstamart()
msg1 = ConsoleMessenger()
agent = Agent(fridge, store, msg1, Settings(mode="self"), Database(path))
agent.handle_chat(B, [{"type": "add_item", "item": "Milk", "qty": 1}], T0)
run = agent.tick(T0)
print("   Self mode, but the cart has a chat-added item:", run.state.value)
msg2 = ConsoleMessenger()
agent = Agent(fridge, store, msg2, Settings(mode="self"), Database(path))      # the server restarts
print("   After restart, open run:", agent.open_run().id, agent.open_run().state.value)
agent.handle_reply(A, "approve", run.id, T0 + timedelta(minutes=5))
print("   Approved after restart:", agent.runs[0].state.value, "| orders:", len(store.orders))
