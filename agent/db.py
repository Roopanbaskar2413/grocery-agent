"""SQLite persistence. Everything the agent must not forget across a restart lives here.

Not stored here: the fridge list and the Instamart cart. Those belong to the fridge and to Instamart.
The one operation that needs to be atomic across processes is claim_run: a single UPDATE that moves
a run from awaiting_user to ordering, so two replies (or two server processes) can never both win.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from .models import CartLine, Product, Run, RunState

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    created TEXT NOT NULL,
    state TEXT NOT NULL,
    state_since TEXT,
    reminded INTEGER NOT NULL DEFAULT 0,
    asked INTEGER NOT NULL DEFAULT 0,
    handled_by TEXT,
    ask_version INTEGER NOT NULL DEFAULT 0,
    approved_total INTEGER NOT NULL DEFAULT 0,
    order_id TEXT,
    orders_before INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    lines TEXT NOT NULL,
    out_of_stock TEXT NOT NULL,
    no_match TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    order_id TEXT PRIMARY KEY,
    placed_at TEXT NOT NULL,
    items TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_items (
    name TEXT PRIMARY KEY,
    qty INTEGER NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS preferred (item TEXT PRIMARY KEY, product_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deferred (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    buttons TEXT,
    targets TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_adds (day TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient TEXT NOT NULL,
    text TEXT NOT NULL,
    buttons TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt TEXT NOT NULL,
    as_template INTEGER NOT NULL DEFAULT 0,
    wamid TEXT,
    error TEXT,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    buttons TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_recipient ON chat_messages (recipient, id);
CREATE TABLE IF NOT EXISTS chat_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_key TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_messages (id TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
"""


def _dt(s):
    return datetime.fromisoformat(s) if s else None


class Database:
    def __init__(self, path=":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def clone(self):
        """A second connection to the same file, for a background thread (WAL allows concurrent use)."""
        if self.path == ":memory:":
            raise ValueError("an in-memory database cannot be shared")
        return Database(self.path)

    # ---------------------------------------------------------------- outbox
    _RANK = {"pending": 0, "sent": 1, "delivered": 2, "read": 3}

    def outbox_add(self, recipient, text, buttons, now) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO outbox (recipient, text, buttons, next_attempt, created) VALUES (?,?,?,?,?)",
                (recipient, text, json.dumps(buttons) if buttons else None, now.isoformat(), now.isoformat()))
        return cur.lastrowid

    def outbox_due(self, now, limit=20):
        rows = self.conn.execute(
            "SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT ?",
            (now.isoformat(), limit)).fetchall()
        return [self._outbox_row(r) for r in rows]

    @staticmethod
    def _outbox_row(r):
        return {"id": r["id"], "recipient": r["recipient"], "text": r["text"],
                "buttons": [tuple(b) for b in json.loads(r["buttons"])] if r["buttons"] else [],
                "status": r["status"], "attempts": r["attempts"], "as_template": bool(r["as_template"]),
                "wamid": r["wamid"], "error": r["error"], "next_attempt": _dt(r["next_attempt"])}

    def outbox_get(self, row_id):
        r = self.conn.execute("SELECT * FROM outbox WHERE id=?", (row_id,)).fetchone()
        return self._outbox_row(r) if r else None

    def outbox_all(self):
        return [self._outbox_row(r) for r in self.conn.execute("SELECT * FROM outbox ORDER BY id")]

    def outbox_mark_sent(self, row_id, wamid):
        with self.conn:
            self.conn.execute("UPDATE outbox SET status='sent', wamid=?, error=NULL WHERE id=?", (wamid, row_id))

    def outbox_retry(self, row_id, next_attempt, error, as_template=None, count_attempt=True):
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET attempts=attempts+?, next_attempt=?, error=?, "
                "as_template=COALESCE(?, as_template) WHERE id=?",
                (1 if count_attempt else 0, next_attempt.isoformat(), error,
                 None if as_template is None else int(as_template), row_id))

    def outbox_fail(self, row_id, error):
        with self.conn:
            self.conn.execute("UPDATE outbox SET status='failed', error=? WHERE id=?", (error, row_id))

    def outbox_status(self, wamid, status, error=None):
        """Delivery receipts from Meta. Never moves a message backwards (read -> sent)."""
        r = self.conn.execute("SELECT id, status FROM outbox WHERE wamid=?", (wamid,)).fetchone()
        if not r:
            return False
        if status == "failed":
            new = "failed"
        elif self._RANK.get(status, -1) > self._RANK.get(r["status"], -1):
            new = status
        else:
            return False
        with self.conn:
            self.conn.execute("UPDATE outbox SET status=?, error=COALESCE(?, error) WHERE id=?", (new, error, r["id"]))
        return True

    def outbox_recent(self, limit=15):
        """Newest outbox rows for diagnosis. Message text and phone numbers are left out on purpose:
        the status endpoint must be useful for debugging without becoming a way to read private messages."""
        rows = self.conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["id"], "status": r["status"], "attempts": r["attempts"], "as_template": bool(r["as_template"]),
                 "error": r["error"], "has_wamid": bool(r["wamid"])} for r in rows]

    def outbox_counts(self):
        return {r["status"]: r["n"] for r in self.conn.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status")}

    # ---------------------------------------------------------------- runs
    @staticmethod
    def _lines_to_json(lines):
        return json.dumps([{"item_name": l.item_name, "qty": l.qty, "source": l.source,
                            "product": vars(l.product)} for l in lines])

    @staticmethod
    def _row_to_run(r) -> Run:
        lines = [CartLine(Product(**d["product"]), d["qty"], d["item_name"], d.get("source", "fridge"))
                 for d in json.loads(r["lines"])]
        return Run(id=r["id"], mode=r["mode"], created=_dt(r["created"]), lines=lines,
                   out_of_stock=json.loads(r["out_of_stock"]), no_match=json.loads(r["no_match"]),
                   approved_total=r["approved_total"], state=RunState(r["state"]),
                   state_since=_dt(r["state_since"]), reminded=bool(r["reminded"]),
                   asked=bool(r["asked"]), ask_version=r["ask_version"], handled_by=r["handled_by"], order_id=r["order_id"],
                   orders_before=r["orders_before"], note=r["note"])

    def save_run(self, run: Run):
        with self.conn:
            self.conn.execute(
                """INSERT INTO runs (id, mode, created, state, state_since, reminded, asked, handled_by, ask_version,
                       approved_total, order_id, orders_before, note, lines, out_of_stock, no_match)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, state=excluded.state,
                       state_since=excluded.state_since, reminded=excluded.reminded, asked=excluded.asked,
                       handled_by=excluded.handled_by, ask_version=excluded.ask_version, approved_total=excluded.approved_total,
                       order_id=excluded.order_id, orders_before=excluded.orders_before, note=excluded.note,
                       lines=excluded.lines, out_of_stock=excluded.out_of_stock, no_match=excluded.no_match""",
                (run.id, run.mode, run.created.isoformat(), run.state.value,
                 run.state_since.isoformat() if run.state_since else None, int(run.reminded), int(run.asked),
                 run.handled_by, run.ask_version, run.approved_total, run.order_id, run.orders_before, run.note,
                 self._lines_to_json(run.lines), json.dumps(run.out_of_stock), json.dumps(run.no_match)))

    def load_runs(self):
        return [self._row_to_run(r) for r in self.conn.execute("SELECT * FROM runs ORDER BY rowid")]

    def get_run(self, run_id) -> Optional[Run]:
        r = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._row_to_run(r) if r else None

    def claim_run(self, run_id, sender) -> bool:
        """Atomic compare-and-set. True for exactly one caller, across threads and processes."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE runs SET state=?, handled_by=? WHERE id=? AND state=?",
                (RunState.ORDERING.value, sender, run_id, RunState.AWAITING_USER.value))
        return cur.rowcount == 1

    # ---------------------------------------------------------------- ledger
    def save_ledger(self, entries):
        with self.conn:
            for e in entries:
                self.conn.execute("INSERT OR IGNORE INTO ledger VALUES (?,?,?)",
                                  (e["order_id"], e["time"].isoformat(), json.dumps(e["items"])))

    def load_ledger(self):
        return [{"order_id": r["order_id"], "time": _dt(r["placed_at"]), "items": json.loads(r["items"])}
                for r in self.conn.execute("SELECT * FROM ledger ORDER BY placed_at")]

    # ---------------------------------------------------------------- small tables
    def replace_pending(self, items):
        with self.conn:
            self.conn.execute("DELETE FROM pending_items")
            self.conn.executemany("INSERT INTO pending_items VALUES (?,?,?)",
                                  [(i.name, i.qty, i.source) for i in items])

    def load_pending(self):
        return [(r["name"], r["qty"], r["source"]) for r in self.conn.execute("SELECT * FROM pending_items")]

    def replace_watchlist(self, names):
        with self.conn:
            self.conn.execute("DELETE FROM watchlist")
            self.conn.executemany("INSERT INTO watchlist VALUES (?)", [(n,) for n in sorted(names)])

    def load_watchlist(self):
        return {r["name"] for r in self.conn.execute("SELECT name FROM watchlist")}

    def replace_preferred(self, mapping):
        with self.conn:
            self.conn.execute("DELETE FROM preferred")
            self.conn.executemany("INSERT INTO preferred VALUES (?,?)", list(mapping.items()))

    def load_preferred(self):
        return {r["item"]: r["product_id"] for r in self.conn.execute("SELECT * FROM preferred")}

    def replace_deferred(self, held):
        with self.conn:
            self.conn.execute("DELETE FROM deferred")
            self.conn.executemany("INSERT INTO deferred (text, buttons, targets) VALUES (?,?,?)",
                                  [(t, json.dumps(b), json.dumps(x)) for t, b, x in held])

    def load_deferred(self):
        return [(r["text"], json.loads(r["buttons"]), json.loads(r["targets"]))
                for r in self.conn.execute("SELECT * FROM deferred ORDER BY id")]

    def replace_chat_adds(self, counts):
        with self.conn:
            self.conn.execute("DELETE FROM chat_adds")
            self.conn.executemany("INSERT INTO chat_adds VALUES (?,?)",
                                  [(d.isoformat(), n) for d, n in counts.items()])

    def load_chat_adds(self):
        return {datetime.fromisoformat(r["day"]).date(): r["n"] for r in self.conn.execute("SELECT * FROM chat_adds")}

    def seen(self, message_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM processed_messages WHERE id=?", (message_id,)).fetchone() is not None

    def mark_processed(self, message_id: str, now: datetime) -> bool:
        """True the first time a message id is seen, False for every repeat (webhook retries, replays)."""
        with self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO processed_messages VALUES (?,?)",
                                    (message_id, now.isoformat()))
        return cur.rowcount == 1

    # ---------------------------------------------------------------- in-app chat (what the web or phone app shows)
    def chat_add(self, recipient, role, text, buttons, now) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO chat_messages (recipient, role, text, buttons, at) VALUES (?,?,?,?,?)",
                (recipient, role, text, json.dumps([list(b) for b in buttons]) if buttons else None, now.isoformat()))
            self.conn.execute("DELETE FROM chat_messages WHERE at < ?", ((now - timedelta(days=14)).isoformat(),))
            return cur.lastrowid

    def chat_since(self, recipient, after_id=0, limit=100):
        rows = self.conn.execute(
            "SELECT * FROM chat_messages WHERE recipient=? AND id>? ORDER BY id LIMIT ?",
            (recipient, after_id, limit)).fetchall()
        return [{"id": r["id"], "role": r["role"], "text": r["text"], "at": r["at"],
                 "buttons": [{"id": b[0], "title": b[1]} for b in json.loads(r["buttons"])] if r["buttons"] else []}
                for r in rows]

    # ---------------------------------------------------------------- short conversation memory
    def add_turn(self, chat_key, role, text, now):
        with self.conn:
            self.conn.execute("INSERT INTO chat_turns (chat_key, role, text, at) VALUES (?,?,?,?)",
                              (chat_key, role, text, now.isoformat()))
            self.conn.execute("DELETE FROM chat_turns WHERE at < ?", ((now.replace(microsecond=0) - timedelta(days=2)).isoformat(),))

    def recent_turns(self, chat_key, now, window_minutes=30, limit=6):
        """The last few turns of one conversation, oldest first. A quiet gap ends the conversation."""
        cutoff = (now - timedelta(minutes=window_minutes)).isoformat()
        rows = self.conn.execute(
            "SELECT role, text FROM chat_turns WHERE chat_key=? AND at>=? ORDER BY id DESC LIMIT ?",
            (chat_key, cutoff, limit)).fetchall()
        return [(r["role"], r["text"]) for r in reversed(rows)]

    def kv_get(self, key, default=None):
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return r["v"] if r else default

    def kv_set(self, key, value):
        with self.conn:
            self.conn.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                              (key, str(value)))
