"""The agent: decides when to run, builds the cart, handles approval, and checks out.

Rules that live here (and nowhere else):
- Only this module calls store.checkout. Chat and Gemini can only ask through handle_reply/handle_chat.
- One open run at a time. The first valid reply moves the run out of AWAITING_USER, later replies lose.
- Right before checkout the cart is re-read: stock and price are checked again.
- Items leave the fridge list only after an order is confirmed.
"""
import functools
import logging
from datetime import datetime, timedelta
from typing import Optional

from .config import Settings
from .db import Database
from .decide import Decision, decide
from .llm_gate import validate_batch
from .matcher import match_item
from .models import CartLine, ListItem, Run, RunState

log = logging.getLogger("agent.orchestrator")


def persisted(method):
    """Save all agent state after a public entry point finishes, however it returns."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if self._depth == 0:
            self._load()                    # the database is the truth: start from what is on disk
        self._depth += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self._depth -= 1
            if self._depth == 0:            # nested calls (chat -> tick) save once, at the end
                self._save_all()
    return wrapper


class Agent:
    def __init__(self, fridge, store, messenger, cfg: Optional[Settings] = None, db: Optional[Database] = None):
        self.fridge = fridge
        self.store = store
        self.msg = messenger
        self.cfg = cfg or Settings()
        self.pending = []            # items added from WhatsApp, count toward the threshold
        self.watchlist = set()
        self.runs = []
        self.ledger = []             # placed orders: {"order_id", "time", "items"}
        self.preferred = {}          # item name -> product id the household chose before
        self.deferred = []           # messages held back during quiet hours
        self.chat_adds = {}          # date -> number of chat-added items that day
        self.now = datetime.now()
        self.db = db or Database()
        self._run_seq = 0
        self._depth = 0
        self._load()
        self._recover_interrupted()

    # ------------------------------------------------------------------ persistence
    def _load(self):
        db = self.db
        fresh = db.load_runs()
        known = {r.id: r for r in self.runs}
        merged = []
        for f in fresh:                         # update in place, so references to a run stay valid
            if f.id in known:
                known[f.id].__dict__.update(f.__dict__)
                merged.append(known[f.id])
            else:
                merged.append(f)
        self.runs = merged
        self.ledger = db.load_ledger()
        self.pending = [ListItem(n, q, src) for n, q, src in db.load_pending()]
        self.watchlist = db.load_watchlist()
        self.preferred = db.load_preferred()
        self.deferred = db.load_deferred()
        self.chat_adds = db.load_chat_adds()
        self._run_seq = int(db.kv_get("run_seq", 0))
        self.cfg.mode = db.kv_get("mode", self.cfg.mode)

    def _save_all(self):
        db = self.db
        for run in self.runs:
            db.save_run(run)
        db.save_ledger(self.ledger)
        db.replace_pending(self.pending)
        db.replace_watchlist(self.watchlist)
        db.replace_preferred(self.preferred)
        db.replace_deferred(self.deferred)
        db.replace_chat_adds(self.chat_adds)
        db.kv_set("run_seq", self._run_seq)
        db.kv_set("mode", self.cfg.mode)

    def _recover_interrupted(self):
        """A run found in ORDERING means the process died around checkout. We cannot know whether the
        order went through, so we never retry on our own: mark it failed and ask a person to look."""
        for run in self.runs:
            if run.state == RunState.ORDERING:
                run.state = RunState.FAILED
                run.note = "interrupted during checkout"
                self.deferred.append((f"{run.id} was interrupted during checkout. Check Instamart for an order "
                                      "before pressing Order now, so nothing is ordered twice.", None,
                                      list(self.cfg.recipients)))
        self._save_all()

    # ------------------------------------------------------------------ helpers
    @property
    def owner(self):
        return next(iter(self.cfg.recipients))

    def _name(self, phone):
        return self.cfg.recipients.get(phone, phone)

    def _quiet(self, now):
        return now.hour >= self.cfg.quiet_start_hour or now.hour < self.cfg.quiet_end_hour

    def _send(self, text, buttons=None, to=None, reply=False):
        """reply=True means the person just wrote to us, so quiet hours do not apply."""
        targets = [to] if to else list(self.cfg.recipients)
        if self._quiet(self.now) and not reply:
            self.deferred.append((text, buttons, targets))
            return
        for t in targets:
            self.msg.send(t, text, buttons)

    def _delivery_time(self):
        """When a message sent now will actually reach people. Reply timers start then, not earlier."""
        if not self._quiet(self.now):
            return self.now
        day = self.now.date() + timedelta(days=1) if self.now.hour >= self.cfg.quiet_start_hour else self.now.date()
        return datetime(day.year, day.month, day.day, self.cfg.quiet_end_hour)

    def _flush_deferred(self):
        if self._quiet(self.now) or not self.deferred:
            return
        held, self.deferred = self.deferred, []
        for text, buttons, targets in held:
            for t in targets:
                self.msg.send(t, text, buttons)

    def _working_list(self):
        merged = {}
        for it in list(self.fridge.get_list()) + list(self.pending):
            key = it.name.lower()
            if key in merged:
                merged[key].qty = min(merged[key].qty + it.qty, self.cfg.max_qty_per_item)
                if it.source == "whatsapp":
                    merged[key].source = "whatsapp"      # part of this quantity came from chat
            else:
                merged[key] = ListItem(it.name, min(it.qty, self.cfg.max_qty_per_item), it.source)
        return list(merged.values())

    def open_run(self) -> Optional[Run]:
        return next((r for r in self.runs if r.state.is_open), None)

    def _orders_last_7_days(self):
        cutoff = self.now - timedelta(days=7)
        return sum(1 for o in self.ledger if o["time"] >= cutoff)

    @staticmethod
    def _buttons(run, approve=True):
        """(id, title) pairs. The id carries the run and which ask it belongs to."""
        cancel = (f"cancel:{run.id}:{run.ask_version}", "Cancel")
        if not approve:
            return [cancel]
        return [(f"approve:{run.id}:{run.ask_version}", "Place order"), cancel]

    def _money(self, run):
        lines = ", ".join(f"{l.item_name} {l.product.size} x{l.qty} (₹{l.total})" for l in run.lines)
        cart = self.store.get_cart()
        fee = f", delivery ₹{cart.delivery_fee}" if cart.delivery_fee else ", free delivery"
        return f"{lines}. Items ₹{cart.subtotal}{fee}, total ₹{cart.total}."

    def _missing(self, run):
        parts = []
        if run.out_of_stock:
            parts.append("Out of stock: " + ", ".join(run.out_of_stock) + ".")
        if run.no_match:
            parts.append("No matching product: " + ", ".join(run.no_match) + ".")
        return " ".join(parts)

    # ------------------------------------------------------------------ trigger
    @persisted
    def tick(self, now: datetime, order_now: bool = False):
        """Called when the fridge sends its list, by the daily/6 PM schedule, or on Order now."""
        self.now = now
        self._flush_deferred()
        run = self.open_run()
        if run:
            if order_now:
                self._send("A run is already open. I will finish it first.", reply=True)
            self._service_open_run(run)
            return run

        items = self._working_list()
        if not items:
            return None

        matches = {}
        for it in items:
            candidates = self.store.search_products(it.name)
            matches[it.name] = (it, match_item(it.name, candidates, self.preferred))
        in_stock = sum(1 for _, m in matches.values() if m.kind == "match")

        if decide(now, in_stock, order_now, self.cfg) == Decision.HOLD:
            self.fridge.show(f"Waiting for more items ({in_stock}/{self.cfg.list_threshold}). "
                             f"Orders at {self.cfg.evening_hour % 12 or 12} PM if {self.cfg.evening_min_items} or more.")
            if order_now and in_stock == 0:
                self._send("Nothing on the list is in stock right now, so there is nothing to order.", reply=True)
            return None
        return self._start_run(matches, now)

    def _start_run(self, matches, now):
        cart = self.store.get_cart()
        if cart.lines:
            self._send("Your Instamart cart already has items, so I did not touch it. "
                       "Empty it or order it, then press Order now.")
            self.fridge.show("Instamart cart is not empty, waiting")
            return None

        self._run_seq += 1
        run = Run(id=f"R{self._run_seq}", mode=self.cfg.mode, created=now)
        for it, m in matches.values():
            if m.kind == "match":
                run.lines.append(CartLine(m.product, it.qty, it.name, it.source))
            elif m.kind == "out_of_stock":
                run.out_of_stock.append(it.name)
            else:
                run.no_match.append(it.name)

        self.store.update_cart([(l.product.id, l.qty) for l in run.lines])
        cart = self.store.get_cart()
        run.approved_total = cart.total
        self.runs.append(run)

        # Limits: above the spend cap or the weekly order limit the agent asks, even in self mode.
        reason = None
        if cart.total > self.cfg.spend_cap:
            reason = f"The total ₹{cart.total} is above your limit of ₹{self.cfg.spend_cap}."
        elif self._orders_last_7_days() >= self.cfg.max_orders_per_week:
            reason = f"{self.cfg.max_orders_per_week} orders already placed in the last 7 days."
        elif any(l.source == "whatsapp" for l in run.lines):
            chat = ", ".join(l.item_name for l in run.lines if l.source == "whatsapp")
            reason = f"This cart includes items added in chat ({chat})."

        if run.mode == "self" and not reason:
            self.fridge.show("Ordering now")
            self._checkout(run, by=None)
        else:
            self._ask(run, reason)
        return run

    def _ask(self, run, reason=None):
        run.state = RunState.AWAITING_USER
        run.state_since = self._delivery_time()
        run.reminded = False
        run.handled_by = None
        run.asked = True
        run.ask_version += 1                          # buttons from an earlier ask stop working
        head = (reason + " I need your OK. ") if reason else ""
        self._send(f"{head}Cart ready ({run.id}): {self._money(run)} {self._missing(run)}".strip(),
                   buttons=self._buttons(run, approve=True))
        self.fridge.show("Waiting for your reply")

    # ------------------------------------------------------------------ checkout
    def _checkout(self, run, by):
        run.state = RunState.ORDERING

        # Re-check stock and price right before the checkout call.
        dropped = []
        for line in list(run.lines):
            current = next((p for p in self.store.search_products(line.item_name) if p.id == line.product.id), None)
            if current is None or not current.in_stock:
                run.lines.remove(line)
                dropped.append(line.item_name)
            else:
                line.product = current
        if dropped:
            run.out_of_stock += dropped
        self.store.update_cart([(l.product.id, l.qty) for l in run.lines])

        if not run.lines:
            self.store.update_cart([])
            run.state = RunState.FAILED
            self._send(f"Everything on the cart went out of stock, so nothing was ordered. {self._missing(run)}")
            self.fridge.show("Nothing ordered, items out of stock")
            return

        cart = self.store.get_cart()
        limit = run.approved_total * (1 + self.cfg.price_tolerance_pct / 100)
        if cart.total > limit or cart.total > self.cfg.spend_cap:
            old, run.approved_total = run.approved_total, cart.total
            self._ask(run, f"The price changed from ₹{old} to ₹{cart.total} before checkout.")
            return

        run.approved_total = cart.total
        if "COD" in self.store.get_payment_options():
            self._save_all()                              # ORDERING is on disk before money moves
            order_id = self.store.checkout("COD")
            self._finish(run, order_id, by)
        else:
            run.state = RunState.AWAITING_APP_PAYMENT
            run.state_since = self._delivery_time()
            run.reminded = False
            run.orders_before = len(self.store.orders)
            self._send(f"COD is not available. Your cart is ready in Instamart: {self._money(run)} "
                       "Open the app and pay for it.", buttons=self._buttons(run, approve=False))
            self.fridge.show("COD not available, pay in the Instamart app")

    def _finish(self, run, order_id, by):
        run.state = RunState.DONE
        run.order_id = order_id
        ordered = [l.item_name for l in run.lines]
        self.ledger.append({"order_id": order_id, "time": self.now, "items": ordered})
        self.fridge.remove(ordered)                       # only now do items leave the list
        self.pending = [i for i in self.pending if i.name not in ordered]
        who = f" by {self._name(by)}" if by else ""
        missing = self._missing(run)
        self.fridge.show(f"Ordered {len(ordered)} items. {missing}".strip())
        # Self mode stays quiet unless something needs your attention.
        if run.asked or missing:
            self._send(f"Order {order_id} placed{who}: {', '.join(ordered)}. {missing}".strip())

    # ------------------------------------------------------------------ replies
    @persisted
    def handle_reply(self, sender, kind, run_id, now, ask=None, sent_at=None):
        """kind is 'approve' or 'cancel'. Reached only through handle_chat, so the gate has already run.
        ask:     which ask a tapped button belonged to. A stale button never approves a newer cart.
        sent_at: when a text reply was sent. A plain "yes" only counts if it was sent after the cart was shown."""
        self.now = now
        if sender not in self.cfg.recipients:
            return None
        run = next((r for r in self.runs if r.id == run_id), None)
        if run is None or not run.state.is_open and run.handled_by is None:
            self._send("That order is no longer open. Press Order now to start a new one.", to=sender, reply=True)
            return None

        if kind == "approve":
            if run.state == RunState.AWAITING_USER:
                if ask is not None and ask != run.ask_version:
                    self._send("That button is out of date. Use the latest message.", to=sender, reply=True)
                    return None
                skew = timedelta(seconds=self.cfg.reply_skew_seconds)
                if ask is None and sent_at is not None and run.state_since and sent_at < run.state_since - skew:
                    self._send("That reply was sent before the latest cart, so I ignored it.", to=sender, reply=True)
                    return None
                if self.db.claim_run(run.id, sender):       # atomic: the first reply wins, in any process
                    run.state = RunState.ORDERING
                    run.handled_by = sender
                    self._checkout(run, by=sender)
                    return run
                run = self._refresh_run(run.id)             # someone else claimed it first
        elif kind == "cancel":
            if run.state in (RunState.AWAITING_USER, RunState.AWAITING_APP_PAYMENT):
                run.state = RunState.CANCELLED
                run.handled_by = sender
                self.store.update_cart([])
                self._send(f"{run.id} cancelled by {self._name(sender)}. The items stay on the list.")
                self.fridge.show("Cancelled")
                return run

        who = self._name(run.handled_by) if run.handled_by else "someone"
        self._send(f"{run.id} was already handled by {who}.", to=sender, reply=True)
        return None

    @persisted
    def handle_change_qty(self, sender, run_id, item, qty, now, ask=None, sent_at=None):
        """Change the quantity of an item already in an AWAITING_USER cart. Reached only through
        handle_chat, so the gate has already run. Reconciles through store.update_cart, the same
        path _start_run/_checkout use, so an item already in the real cart gets its stepper clicked
        rather than being re-added as a duplicate line. Re-asks afterward so the new total gets a
        fresh approval (this also bumps ask_version, invalidating the old "Place order" button)."""
        self.now = now
        if sender not in self.cfg.recipients:
            return None
        run = next((r for r in self.runs if r.id == run_id), None)
        if run is None or not run.state.is_open and run.handled_by is None:
            self._send("That order is no longer open. Press Order now to start a new one.", to=sender, reply=True)
            return None
        if ask is not None and ask != run.ask_version:
            self._send("That button is out of date. Use the latest message.", to=sender, reply=True)
            return None
        skew = timedelta(seconds=self.cfg.reply_skew_seconds)
        if ask is None and sent_at is not None and run.state_since and sent_at < run.state_since - skew:
            self._send("That reply was sent before the latest cart, so I ignored it.", to=sender, reply=True)
            return None
        if run.state != RunState.AWAITING_USER:
            who = self._name(run.handled_by) if run.handled_by else "someone"
            self._send(f"{run.id} was already handled by {who}.", to=sender, reply=True)
            return None

        line = next((l for l in run.lines if l.item_name.lower() == item.lower()), None)
        if line is None:
            self._send(f"{item} is not in this order.", to=sender, reply=True)
            return None

        line.qty = qty
        self.store.update_cart([(l.product.id, l.qty) for l in run.lines])
        run.approved_total = self.store.get_cart().total
        self._ask(run, f"Updated {item} to x{qty}.")
        return run

    def _refresh_run(self, run_id):
        fresh = self.db.get_run(run_id)
        current = next(r for r in self.runs if r.id == run_id)
        current.__dict__.update(fresh.__dict__)
        return current

    @persisted
    def handle_chat(self, sender, raw_actions, now, sent_at=None):
        """raw_actions is what Gemini returned for this message. Nothing here can check out directly."""
        self.now = now
        if sender not in self.cfg.recipients:
            return []
        actions, errors = validate_batch(raw_actions, self.cfg)
        for _ in errors:
            self._send("I could not do part of that request.", to=sender, reply=True)

        done = []
        for a in actions:
            if a.type == "add_item":
                key = now.date()
                if self.chat_adds.get(key, 0) >= self.cfg.max_chat_items_per_day:
                    self._send("That is the limit for chat-added items today.", to=sender, reply=True)
                    continue
                self.chat_adds[key] = self.chat_adds.get(key, 0) + 1
                # Chat-added items only go to the pending list. They never touch an approved cart.
                existing = next((p for p in self.pending if p.name.lower() == a.item.lower()), None)
                if existing:
                    existing.qty = min(existing.qty + a.qty, self.cfg.max_qty_per_item)
                else:
                    self.pending.append(ListItem(a.item, a.qty, "whatsapp"))
                self._send(f"Added {a.item} x{a.qty} to the next order.", to=sender, reply=True)
            elif a.type == "remove_item":
                self.pending = [p for p in self.pending if p.name.lower() != a.item.lower()]
                self.fridge.remove([a.item])
                stopped = self._unwatch(a.item)
                self._send(f"Removed {a.item} from the list." + (" I also stopped watching it." if stopped else ""),
                           to=sender, reply=True)
            elif a.type == "watch_item":
                key = a.item.lower()
                if key in self.watchlist:
                    self._send(f"I am already watching {a.item}.", to=sender, reply=True)
                elif len(self.watchlist) >= self.cfg.watch_max_items:
                    self._send(f"I can watch {self.cfg.watch_max_items} items at once. "
                               "Say remove and an item name to stop watching one.", to=sender, reply=True)
                else:
                    self.watchlist.add(key)
                    self.db.kv_set(f"watch_since:{key}", now.isoformat())
                    self.db.kv_set("last_watch_check", "")        # first look at the next scheduler pass
                    self._send(f"I will add {a.item} to the next order when it is back in stock. "
                               f"I will check about every {self.cfg.watch_interval_min} minutes "
                               f"and stop after {self.cfg.watch_expiry_days} days.", to=sender, reply=True)
            elif a.type in ("approve", "cancel"):
                self.handle_reply(sender, a.type, a.run_id, now, ask=a.ask, sent_at=sent_at)
            elif a.type == "change_qty":
                self.handle_change_qty(sender, a.run_id, a.item, a.qty, now, ask=a.ask, sent_at=sent_at)
            elif a.type == "order_now":
                self.tick(now, order_now=True)
            elif a.type == "change_mode":
                if sender != self.owner:
                    self._send("Only the account owner can change the mode.", to=sender, reply=True)
                else:
                    self.cfg.mode = a.mode
                    self._send(f"Mode set to {a.mode}.", to=sender, reply=True)
            done.append(a.type)
        return done

    # ------------------------------------------------------------------ scheduler hooks
    @persisted
    def service(self, now: datetime):
        """Cheap, called every minute: held messages and timers of an open run. No Instamart searches."""
        self.now = now
        self._flush_deferred()
        run = self.open_run()
        if run:
            self._service_open_run(run)

    @persisted
    def evening_check(self, now: datetime) -> bool:
        """The 6 PM check, at most once a day. Catches up if the server was down at 6 PM."""
        if now.hour < self.cfg.evening_hour:
            return False
        today = now.date().isoformat()
        if self.db.kv_get("last_evening_check") == today:
            return False
        self.db.kv_set("last_evening_check", today)        # one attempt a day, no hammering Instamart
        self.tick(now)
        return True

    def _unwatch(self, name: str) -> bool:
        key = name.lower()
        if key not in self.watchlist:
            return False
        self.watchlist.discard(key)
        self.db.kv_set(f"watch_since:{key}", "")
        return True

    @persisted
    def watch_check(self, now: datetime) -> list:
        """Hourly, read-only: look up each watched item and, when it is back in stock, add it to the pending list.
        Only search_products is used. The cart is never touched, and no order starts here: the item joins the
        next run through the normal rules (it counts toward the 4)."""
        self.now = now
        if not self.watchlist:
            return []
        last = self.db.kv_get("last_watch_check")
        if last and now < datetime.fromisoformat(last) + timedelta(minutes=self.cfg.watch_interval_min):
            return []
        self.db.kv_set("last_watch_check", now.isoformat())
        found = []
        for key in sorted(self.watchlist):
            since = self.db.kv_get(f"watch_since:{key}")
            if since and now - datetime.fromisoformat(since) > timedelta(days=self.cfg.watch_expiry_days):
                self._unwatch(key)
                self._send(f"I stopped watching {key} after {self.cfg.watch_expiry_days} days. "
                           "Say remind me when it is back to start again.")
                continue
            try:
                result = match_item(key, self.store.search_products(key), self.preferred)
            except Exception:                                # a store hiccup keeps the watch and retries next time
                log.warning("watch lookup failed for %s", key, exc_info=True)
                continue
            if result.kind != "match":
                continue
            self._unwatch(key)
            on_list = any(i.name.lower() == key for i in self._working_list())
            if not on_list:
                self.pending.append(ListItem(key, 1, "watch"))
            self._send(f"{key} is back in stock" + (" and is already on your list." if on_list
                                                     else ". I added it to the next order."))
            found.append(key)
        return found

    def chat_context(self):
        """What a language model may know. Item names, sizes, quantities and prices only. No phone numbers,
        no tokens, no addresses. The run id and ask are for rule-based parsing and are removed before any
        model sees this (see gemini.py)."""
        run = self.open_run()
        return {
            "open_run": {
                "id": run.id, "state": run.state.value, "ask": run.ask_version,
                "items": [{"name": l.item_name, "size": l.product.size, "qty": l.qty, "price": l.total}
                          for l in run.lines],
                "total": run.approved_total, "out_of_stock": list(run.out_of_stock), "no_match": list(run.no_match),
            } if run else None,
            "pending": [{"name": i.name, "qty": i.qty} for i in self.pending],
            "mode": self.cfg.mode,
        }

    def status(self):
        run = self.open_run()
        return {"mode": self.cfg.mode,
                "open_run": {"id": run.id, "state": run.state.value} if run else None,
                "pending_items": len(self.pending),
                "orders_last_7_days": self._orders_last_7_days()}

    def note_fridge_push(self, now: datetime):
        self.db.kv_set("last_fridge_push", now.isoformat())

    @persisted
    def fridge_silence_check(self, now: datetime):
        """One message per silence: the fridge has not sent a list for a while."""
        self.now = now
        last = self.db.kv_get("last_fridge_push")
        if not last:
            return False
        last_dt = datetime.fromisoformat(last)
        if now - last_dt < timedelta(days=self.cfg.fridge_silence_days):
            return False
        if self.db.kv_get("silence_nudged_for") == last:
            return False
        self.db.kv_set("silence_nudged_for", last)
        self._send(f"No update from the fridge for {self.cfg.fridge_silence_days} days. "
                   "Check that it is online. Nothing will be ordered until it sends its list.")
        return True

    def say(self, sender, text, now):
        """A plain reply to one person, for messages we could not act on."""
        self.now = now
        self._send(text, to=sender, reply=True)

    # ------------------------------------------------------------------ timers
    def _service_open_run(self, run):
        waited = (self.now - (run.state_since or run.created)).total_seconds() / 60
        if run.state == RunState.AWAITING_USER:
            if waited >= self.cfg.approval_expiry_min:
                self._expire(run, f"No reply for {self.cfg.approval_expiry_min // 60} hours.")
            elif waited >= self.cfg.approval_reminder_min and not run.reminded:
                run.reminded = True
                self._send(f"Reminder: cart {run.id} is waiting for your reply.",
                           buttons=self._buttons(run, approve=True))
        elif run.state == RunState.AWAITING_APP_PAYMENT:
            if len(self.store.orders) > run.orders_before:        # the user paid in the app
                order = self.store.orders[-1]
                self._finish(run, order["id"], None)
            elif waited >= self.cfg.app_pay_expiry_min:
                self._expire(run, "The cart was not paid in time.")
            elif waited >= self.cfg.app_pay_reminder_min and not run.reminded:
                run.reminded = True
                self._send(f"Reminder: your Instamart cart {run.id} is waiting for payment in the app.",
                           buttons=self._buttons(run, approve=False))

    def _expire(self, run, why):
        run.state = RunState.EXPIRED
        self.store.update_cart([])                     # no stale cart left behind
        self._send(f"{why} I cleared the cart. The items stay on your list. Press Order now to try again.")
        self.fridge.show("Order expired")
