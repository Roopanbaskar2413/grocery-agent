"""HTTP entry points. This module is deliberately thin and has NO path to ordering.

Everything a person (or the fridge) can ask for goes through exactly one door on the agent:
  - WhatsApp buttons and texts  ->  raw action dicts  ->  Agent.handle_chat  (the gate runs there)
  - fridge list / Order now     ->  Agent.tick
Nothing in here may call anything that spends money, touches the cart, or changes a run directly.
tests/test_api.py scans this file's source to keep it that way.

Delivery rules for the WhatsApp webhook:
  - the X-Hub-Signature-256 header must match an HMAC of the raw body, or the request is refused
  - a message id is recorded only AFTER it was handled, so a crash makes Meta retry (at least once),
    and a repeat of a handled id is skipped (never twice)
  - unknown senders, future-dated messages and floods are dropped without a reply
"""
import asyncio
import uuid
from pathlib import Path
import hashlib
import hmac
import json
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .clock import from_epoch, now_local
from .gemini import claims_completion, sanitize_reply
from .intent import ParseResult, is_exact_command
from .messenger import APP_PREFIX
from .scheduler import Scheduler

HELP = ("I did not catch that. You can say: add milk, add 2 curd, remind me when tomatoes are back, "
        "order now, or use the buttons.")
BUTTON_ID = re.compile(r"^(approve|cancel):(R\d+):(\d+)$")
MAX_TEXT = 500
STATIC = Path(__file__).parent / "static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
FLOOD_LIMIT, FLOOD_WINDOW = 20, 60          # messages per sender per seconds
ITEM_PATTERN = r"^[A-Za-z][A-Za-z '\-]{1,39}$"


@dataclass
class ApiSecrets:
    verify_token: str
    app_secret: str
    fridge_key: str

    def __post_init__(self):
        if not (self.verify_token and self.app_secret and self.fridge_key):
            raise ValueError("verify_token, app_secret and fridge_key must all be set")


class FridgeItem(BaseModel):
    name: str = Field(pattern=ITEM_PATTERN)
    qty: int = Field(1, ge=1, le=5)


class FridgeList(BaseModel):
    items: list[FridgeItem] = Field(max_length=40)
    order_now: bool = False


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def create_app(agent, parser, secrets: ApiSecrets, clock=None, lock=None, scheduler_interval: Optional[int] = 60,
               dispatcher=None, dispatcher_interval: int = 3, chat_tokens: Optional[dict] = None, oauth=None,
               vapid_public_key: Optional[str] = None):
    clock = clock or (lambda: now_local(agent.cfg.timezone))
    lock = lock or threading.Lock()
    scheduler = Scheduler(agent, clock, lock)
    people = {_digits(p): p for p in agent.cfg.recipients
              if not p.startswith(APP_PREFIX)}              # webhook digits -> configured phone recipient
    chat_tokens = dict(chat_tokens or {})                        # secret token -> in-app recipient ("app:1")
    for tok, who in chat_tokens.items():
        if len(tok) < 16:
            raise ValueError("chat tokens must be at least 16 characters")
        if who not in agent.cfg.recipients:
            raise ValueError(f"chat token points at an unknown recipient: {who}")
    recent = defaultdict(deque)                                  # sender -> receive times, for the flood limit

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        if scheduler_interval:
            tasks.append(asyncio.create_task(scheduler.loop(scheduler_interval)))
        if dispatcher is not None:
            dispatcher.clear_auth_broken()                   # a restart usually means a new token: try again
            tasks.append(asyncio.create_task(dispatcher.loop(dispatcher_interval)))
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="Fridge-to-cart agent", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.state.lock = lock

    # ------------------------------------------------------------------ helpers
    def fridge_auth(key: Optional[str]):
        if not key or not hmac.compare_digest(key.encode(), secrets.fridge_key.encode()):
            raise HTTPException(status_code=401, detail="bad key")

    def flooded(sender) -> bool:
        t = time.monotonic()
        q = recent[sender]
        while q and t - q[0] > FLOOD_WINDOW:
            q.popleft()
        q.append(t)
        return len(q) > FLOOD_LIMIT

    def prepare(msg: dict):
        """Phase 1, under the lock: cheap checks only. Returns None if the message is dropped or answered."""
        now = clock()
        sender = people.get(_digits(msg.get("from", "")))
        msg_id = msg.get("id")
        if sender is None or not msg_id:
            return None                                           # not one of the 3 numbers: ignore silently
        if agent.db.seen(msg_id):
            return None                                           # a retry of something already handled
        if flooded(sender):
            return None
        try:
            sent_at = from_epoch(msg["timestamp"], agent.cfg.timezone)
        except (KeyError, ValueError, TypeError, OverflowError):
            return None
        if sent_at > now + timedelta(minutes=5):
            return None                                           # dated in the future: not a real reply

        # every message from one of the 3 numbers (re)opens that person's 24 hour window for free-form replies
        agent.db.kv_set(f"inbound:{sender}", min(sent_at, now).isoformat())

        kind = msg.get("type")
        prep = {"sender": sender, "id": msg_id, "sent_at": sent_at, "kind": kind, "raw": [], "body": "", "ctx": None}
        if kind in ("interactive", "button"):
            if kind == "interactive":                             # a tap on a normal reply-button message
                button_id = ((msg.get("interactive") or {}).get("button_reply") or {}).get("id")
            else:                                                 # a tap on a TEMPLATE quick-reply button
                button_id = (msg.get("button") or {}).get("payload")
            m = BUTTON_ID.match(str(button_id or ""))
            if m:
                prep["raw"] = [{"type": m.group(1), "run_id": m.group(2), "ask": int(m.group(3))}]
            return prep
        if kind == "text":
            prep["body"] = str((msg.get("text") or {}).get("body", ""))[:MAX_TEXT]
            ctx = dict(agent.chat_context())
            ctx["chat_key"] = hashlib.sha256(sender.encode()).hexdigest()[:12]   # a stable key, not a phone number
            prep["ctx"] = ctx
            return prep
        agent.say(sender, "I can read text and buttons only.", now)
        agent.db.mark_processed(msg_id, now)
        return None

    def run_parser(prep):
        """Phase 2, NO lock held: this may call a language model over the network for several seconds."""
        try:
            if hasattr(parser, "parse_result"):
                res = parser.parse_result(prep["body"], prep["ctx"])
            else:
                res = ParseResult(parser.parse(prep["body"], prep["ctx"]))
            if not isinstance(res.actions, list):
                raise TypeError("actions must be a list")
            return res
        except Exception:                                         # a broken or odd parser must not break delivery
            return ParseResult([], None, "error")

    def finish(prep, parsed):
        """Phase 3, under the lock: act on the result. The only way in is handle_chat, so the gate always runs."""
        now = clock()
        sender, body = prep["sender"], prep["body"]
        if agent.db.seen(prep["id"]):                             # a concurrent duplicate got here first
            return
        if prep["kind"] == "text":
            proposed = [a for a in parsed.actions if isinstance(a, dict)]
            # approve and order_now from free text need an exact phrase, even if a model suggested them
            kept = [a for a in proposed if not (a.get("type") in ("approve", "order_now")
                                                and not is_exact_command(body, a["type"]))]
            if kept:
                agent.handle_chat(sender, kept, now, sent_at=prep["sent_at"])
            elif len(kept) != len(proposed):
                agent.say(sender, "To place an order, tap Place order or reply exactly: yes", now)
            elif parsed.reply:
                reply = sanitize_reply(parsed.reply)
                if reply and claims_completion(reply):            # a model must never announce that something happened
                    agent.say(sender, "I have not changed anything. Use the buttons, or say: order now", now)
                elif reply:
                    agent.say(sender, reply, now)
                else:
                    agent.say(sender, HELP, now)
            else:
                agent.say(sender, HELP, now)
        elif prep["raw"]:
            agent.handle_chat(sender, prep["raw"], now, sent_at=prep["sent_at"])
        else:
            agent.say(sender, HELP, now)
        agent.db.mark_processed(prep["id"], now)                  # recorded only after it was handled

    def process_message(msg: dict):
        with lock:
            prep = prepare(msg)
        if prep is None:
            return
        parsed = run_parser(prep) if prep["kind"] == "text" else None
        with lock:
            finish(prep, parsed)

    def handle_status(st: dict):
        """Delivery receipts for messages we sent: sent, delivered, read, failed."""
        errors = st.get("errors") or []
        detail = None
        if errors:
            e = errors[0]
            detail = f"{e.get('code')}: {e.get('title') or e.get('message') or ''}"[:300]
        if st.get("id") and st.get("status"):
            agent.db.outbox_status(st["id"], st["status"], detail)

    def process_payload(payload: dict):
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                with lock:
                    for st in value.get("statuses", []) or []:
                        handle_status(st)
                for msg in value.get("messages", []) or []:
                    process_message(msg)

    # ------------------------------------------------------------------ routes
    @app.get("/health")
    def health():
        broken = dispatcher is not None and dispatcher.auth_broken()
        return {"ok": not broken, "whatsapp_sending": "paused: token rejected" if broken else "ok"}

    @app.get("/webhook/whatsapp")
    def verify(hub_mode: str = Query("", alias="hub.mode"), hub_verify_token: str = Query("", alias="hub.verify_token"),
               hub_challenge: str = Query("", alias="hub.challenge")):
        if hub_mode == "subscribe" and hmac.compare_digest(hub_verify_token.encode(), secrets.verify_token.encode()):
            return Response(content=hub_challenge, media_type="text/plain")
        raise HTTPException(status_code=403, detail="verification failed")

    @app.post("/webhook/whatsapp")
    async def webhook(request: Request, x_hub_signature_256: Optional[str] = Header(None)):
        raw = await request.body()
        expected = "sha256=" + hmac.new(secrets.app_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not x_hub_signature_256 or not hmac.compare_digest(x_hub_signature_256, expected):
            raise HTTPException(status_code=403, detail="bad signature")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad json")
        await run_in_threadpool(process_payload, payload)         # a crash here returns 500, so Meta retries
        return {"ok": True}

    # ------------------------------------------------------------------ in-app chat
    # Same door as WhatsApp: text -> parser -> handle_chat (gate), tap -> handle_chat. Nothing here can order.
    def chat_user(authorization: Optional[str]) -> str:
        token = (authorization or "")[7:] if (authorization or "").lower().startswith("bearer ") else ""
        found = None
        for tok, who in chat_tokens.items():                      # compare against every token, no early exit
            if token and hmac.compare_digest(token.encode(), tok.encode()):
                found = who
        if found is None:
            raise HTTPException(status_code=401, detail="bad token")
        return found

    class ChatSend(BaseModel):
        text: str = Field(min_length=1, max_length=MAX_TEXT)

    class ChatTap(BaseModel):
        button_id: str = Field(max_length=64)

    def process_chat(who: str, kind: str, body: str = "", raw=None, shown: str = ""):
        with lock:
            now = clock()
            if flooded(who):
                raise HTTPException(status_code=429, detail="slow down")
            agent.db.chat_add(who, "user", shown or body, [], now)
            prep = {"sender": who, "id": "chat-" + uuid.uuid4().hex, "sent_at": now, "kind": kind,
                    "raw": raw or [], "body": body, "ctx": None}
            if kind == "text":
                ctx = dict(agent.chat_context())
                ctx["chat_key"] = hashlib.sha256(who.encode()).hexdigest()[:12]
                prep["ctx"] = ctx
        parsed = run_parser(prep) if kind == "text" else None
        with lock:
            finish(prep, parsed)

    @app.get("/chat")
    def chat_page():
        return HTMLResponse((STATIC / "chat.html").read_text(), headers={"Content-Security-Policy": CSP,
                            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/chat/app.js")
    def chat_js():
        return Response((STATIC / "chat.js").read_text(), media_type="text/javascript",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/chat/sw.js")
    def chat_sw():
        return Response((STATIC / "sw.js").read_text(), media_type="text/javascript",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    # ------------------------------------------------------------------ push notifications (optional)
    # Best-effort on top of the chat's own poll loop (agent/push.py); nothing here can send a message,
    # only subscribe/unsubscribe this person's own browser to get notified of one.
    class PushSubscribe(BaseModel):
        endpoint: str = Field(min_length=1, max_length=2000)
        p256dh: str = Field(min_length=1, max_length=512)
        auth: str = Field(min_length=1, max_length=512)

    class PushUnsubscribe(BaseModel):
        endpoint: str = Field(min_length=1, max_length=2000)

    @app.get("/chat/vapid-public-key")
    def chat_vapid_public_key(authorization: Optional[str] = Header(None)):
        chat_user(authorization)
        if not vapid_public_key:
            raise HTTPException(status_code=404, detail="push not configured")
        return {"key": vapid_public_key}

    @app.post("/chat/push-subscribe")
    def chat_push_subscribe(body: PushSubscribe, authorization: Optional[str] = Header(None)):
        who = chat_user(authorization)
        with lock:
            agent.db.save_push_subscription(who, body.endpoint, body.p256dh, body.auth, clock())
        return {"ok": True}

    @app.post("/chat/push-unsubscribe")
    def chat_push_unsubscribe(body: PushUnsubscribe, authorization: Optional[str] = Header(None)):
        chat_user(authorization)
        with lock:
            agent.db.remove_push_subscription(body.endpoint)
        return {"ok": True}

    @app.post("/chat/send")
    async def chat_send(body: ChatSend, authorization: Optional[str] = Header(None)):
        who = chat_user(authorization)
        await run_in_threadpool(process_chat, who, "text", body.text.strip())
        return {"ok": True}

    @app.post("/chat/tap")
    async def chat_tap(body: ChatTap, authorization: Optional[str] = Header(None)):
        who = chat_user(authorization)
        m = BUTTON_ID.match(body.button_id)
        if not m:
            raise HTTPException(status_code=422, detail="unknown button")
        raw = [{"type": m.group(1), "run_id": m.group(2), "ask": int(m.group(3))}]
        await run_in_threadpool(process_chat, who, "interactive", "", raw,
                                "Place order" if m.group(1) == "approve" else "Cancel")
        return {"ok": True}

    @app.get("/chat/messages")
    def chat_messages(after: int = Query(0, ge=0), authorization: Optional[str] = Header(None)):
        who = chat_user(authorization)
        with lock:
            return {"name": agent.cfg.recipients.get(who, ""), "messages": agent.db.chat_since(who, after)}

    @app.post("/fridge/list")
    def fridge_list(body: FridgeList, x_fridge_key: Optional[str] = Header(None)):
        fridge_auth(x_fridge_key)
        with lock:
            now = clock()
            agent.fridge.set_list([(i.name, i.qty) for i in body.items])
            agent.note_fridge_push(now)
            run = agent.tick(now, order_now=body.order_now)
            return {"run": run.id if run else None, "state": run.state.value if run else None,
                    "display": agent.fridge.display}

    @app.post("/fridge/order-now")
    def fridge_order_now(x_fridge_key: Optional[str] = Header(None)):
        fridge_auth(x_fridge_key)
        with lock:
            run = agent.tick(clock(), order_now=True)
            return {"run": run.id if run else None, "state": run.state.value if run else None,
                    "display": agent.fridge.display}

    # ------------------------------------------------------------------ Swiggy OAuth
    # Dormant until Builders Club access is approved: oauth is only wired in when
    # INSTAMART_BACKEND=real, and even then there is nothing for Swiggy to redirect to until the
    # client is registered and allowlisted. Owner-only (fridge key) so a stranger with the URL
    # cannot trigger dynamic client registration against the account.
    @app.get("/auth/start")
    def auth_start(x_fridge_key: Optional[str] = Header(None)):
        fridge_auth(x_fridge_key)
        if oauth is None:
            raise HTTPException(status_code=404, detail="not found")
        if not oauth.client_id:
            oauth.register_client()
        return RedirectResponse(oauth.build_authorize_url())

    @app.get("/auth/callback")
    def auth_callback(code: str = Query(""), state: str = Query(""), error: str = Query("")):
        if oauth is None:
            raise HTTPException(status_code=404, detail="not found")
        if error or not code or not state:
            return HTMLResponse("<p>Sign-in did not complete. Close this tab and try again.</p>", status_code=400)
        try:
            oauth.exchange_code(code, state)
        except Exception:
            return HTMLResponse("<p>Sign-in did not complete. Close this tab and try again.</p>", status_code=400)
        return HTMLResponse("<p>Signed in with Swiggy. You can close this tab.</p>")

    @app.get("/status")
    def status(x_fridge_key: Optional[str] = Header(None)):
        fridge_auth(x_fridge_key)
        with lock:
            out = agent.status()
            out["outbox"] = agent.db.outbox_counts()
            out["outbox_recent"] = agent.db.outbox_recent()
            out["gemini"] = {"last_error": agent.db.kv_get("gemini_last_error"),
                             "cooldown_until": agent.db.kv_get("gemini_cooldown_until")}
            return out

    return app
