"""The real WhatsApp Cloud API sender: an outbox in the database plus a dispatcher that delivers it.

Why an outbox instead of calling Meta from inside the agent:
  - the agent holds a lock while it decides things; a slow network call must never sit inside it
  - a message is written to disk before it is sent, so a crash cannot lose it
  - retries, backoff and "token is dead, stop" live in one place

Rules this file encodes (from Meta's Cloud API docs):
  - Free-form text and reply buttons only work inside 24 hours of the person's last message to us.
    Outside that window only an approved template can be sent. We track the window ourselves and also
    fall back to a template if Meta answers 131047 ("re-engagement").
  - Reply buttons: 1 to 3, title up to 20 characters and unique, id up to 256, body up to 1024.
    Text bodies: up to 4096. Template body parameters: no newlines or tabs, no runs of 5+ spaces.
  - Errors: 190 / 0 / 200-299 / HTTP 401 mean the token is dead (stop sending, do not hammer),
    130429 / 4 / 80007 and 5xx are retried with backoff, 131056 (too many messages to one person) waits longer,
    everything else in 4xx is permanent for that message.
Delivery is at-least-once: if the process dies between Meta accepting a message and us recording it, the
message is sent again after the restart. A duplicate reminder is better than a lost cart.
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx

log = logging.getLogger("agent.whatsapp")

GRAPH = "https://graph.facebook.com"
TEXT_MAX, BODY_MAX, TITLE_MAX, ID_MAX, PARAM_MAX = 4096, 1024, 20, 256, 900
WINDOW = timedelta(hours=23)                 # 24 h minus a safety margin
MAX_ATTEMPTS = 6
AUTH_CODES = {0, 190}
RETRY_CODES = {4, 80007, 130429}
TEMPLATE_CODE = 131047
PAIR_LIMIT_CODE = 131056


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


# ---------------------------------------------------------------------- payload builders
def clean_param(text: str) -> str:
    """Template parameters may not contain newlines, tabs or 5+ consecutive spaces."""
    t = re.sub(r"[\r\n\t]+", " | ", text)
    t = re.sub(r" {2,}", " ", t).strip()
    return t[:PARAM_MAX]


def text_payload(to: str, text: str) -> dict:
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": digits(to),
            "type": "text", "text": {"preview_url": False, "body": text[:TEXT_MAX]}}


def buttons_payload(to: str, body: str, buttons) -> dict:
    seen, out = set(), []
    for bid, title in list(buttons)[:3]:
        t = title.strip()[:TITLE_MAX] or "Option"
        while t in seen:                                  # titles must be unique within the message
            t = (t[:TITLE_MAX - 1] + "'")
        seen.add(t)
        out.append({"type": "reply", "reply": {"id": bid.strip()[:ID_MAX], "title": t}})
    return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": digits(to), "type": "interactive",
            "interactive": {"type": "button", "body": {"text": body[:BODY_MAX]}, "action": {"buttons": out}}}


def template_payload(to: str, name: str, lang: str, body_text: str, quick_reply_ids=()) -> dict:
    components = [{"type": "body", "parameters": [{"type": "text", "text": clean_param(body_text)}]}]
    for i, bid in enumerate(quick_reply_ids):
        components.append({"type": "button", "sub_type": "quick_reply", "index": str(i),
                           "parameters": [{"type": "payload", "payload": bid[:128]}]})
    return {"messaging_product": "whatsapp", "to": digits(to), "type": "template",
            "template": {"name": name, "language": {"code": lang}, "components": components}}


# ---------------------------------------------------------------------- outbox messenger
class OutboxMessenger:
    """Implements Messenger.send by writing to the outbox. Fast, no network."""

    def __init__(self, db, clock):
        self.db, self.clock = db, clock

    def send(self, to, text, buttons=None):
        now = self.clock()
        buttons = [tuple(b) for b in (buttons or [])]
        if buttons and len(text) > BODY_MAX:
            # a cart list is longer than a button message body can be: the list goes as plain text,
            # then the choice follows as a short message with the buttons
            self.db.outbox_add(to, text[:TEXT_MAX], None, now)
            self.db.outbox_add(to, "Choose an option:", buttons, now)
        else:
            self.db.outbox_add(to, text[:TEXT_MAX], buttons or None, now)


# ---------------------------------------------------------------------- client
@dataclass
class SendResult:
    ok: bool
    wamid: Optional[str] = None
    http_status: int = 0
    code: Optional[int] = None
    message: str = ""
    retry_after: Optional[int] = None
    network_error: bool = False


class WhatsAppClient:
    def __init__(self, token, phone_number_id, api_version="v25.0", http: Optional[httpx.Client] = None, timeout=10.0):
        self.token = token
        self.url = f"{GRAPH}/{api_version}/{phone_number_id}/messages"
        self.http = http or httpx.Client(timeout=timeout)

    def send(self, payload: dict) -> SendResult:
        try:
            r = self.http.post(self.url, json=payload, headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as e:                      # timeouts, connection resets, DNS
            return SendResult(False, message=f"network: {type(e).__name__}", network_error=True)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code < 300 and body.get("messages"):
            return SendResult(True, wamid=body["messages"][0].get("id"), http_status=r.status_code)
        err = body.get("error", {}) if isinstance(body, dict) else {}
        retry_after = r.headers.get("Retry-After")
        return SendResult(False, http_status=r.status_code, code=err.get("code"),
                          message=str(err.get("message") or r.text[:200]),
                          retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None)


def classify(res: SendResult) -> str:
    if res.network_error or res.http_status >= 500:
        return "retry"
    if res.code == TEMPLATE_CODE:
        return "template"
    if res.code == PAIR_LIMIT_CODE:
        return "pair_limit"
    if res.code in RETRY_CODES or res.http_status == 429:
        return "retry"
    if res.code in AUTH_CODES or (res.code is not None and 200 <= res.code <= 299) or res.http_status == 401:
        return "auth"
    return "permanent"


# ---------------------------------------------------------------------- dispatcher
class Dispatcher:
    def __init__(self, db, client: WhatsAppClient, clock, cart_template="grocery_cart",
                 update_template="grocery_update", language="en"):
        self.db, self.client, self.clock = db, client, clock
        self.cart_template, self.update_template, self.language = cart_template, update_template, language

    # --- window tracking (the agent's webhook records every inbound message)
    def window_open(self, recipient, now) -> bool:
        last = self.db.kv_get(f"inbound:{recipient}")
        return bool(last) and now - datetime.fromisoformat(last) < WINDOW

    def auth_broken(self) -> bool:
        return self.db.kv_get("whatsapp_auth_broken") == "1"

    def clear_auth_broken(self):
        self.db.kv_set("whatsapp_auth_broken", "0")

    def build(self, row, now) -> dict:
        to = row["recipient"]
        buttons = row["buttons"]
        if row["as_template"] or not self.window_open(to, now):
            has_approve = any(bid.startswith("approve:") for bid, _ in buttons)
            if has_approve:
                return template_payload(to, self.cart_template, self.language, row["text"], [bid for bid, _ in buttons[:2]])
            return template_payload(to, self.update_template, self.language, row["text"])
        if buttons:
            return buttons_payload(to, row["text"], buttons)
        return text_payload(to, row["text"])

    def run_once(self, limit=20):
        """Send what is due. Safe to call often. Returns a small summary for logs and tests."""
        now = self.clock()
        summary = {"sent": 0, "retry": 0, "failed": 0, "paused": False}
        if self.auth_broken():
            summary["paused"] = True
            return summary
        for row in self.db.outbox_due(now, limit):
            res = self.client.send(self.build(row, now))
            if res.ok:
                self.db.outbox_mark_sent(row["id"], res.wamid)
                summary["sent"] += 1
                continue
            kind = classify(res)
            err = (res.message if res.network_error else f"{res.code or res.http_status}: {res.message}")[:300]
            if kind == "auth":
                self.db.kv_set("whatsapp_auth_broken", "1")
                self.db.kv_set("whatsapp_auth_error", err)
                log.error("WhatsApp token rejected, sending paused: %s", err)
                summary["paused"] = True
                break                                       # the rows stay pending, nothing is lost
            if kind == "template" and not row["as_template"]:
                self.db.outbox_retry(row["id"], now, err, as_template=True, count_attempt=False)
                summary["retry"] += 1
                continue
            if kind == "permanent" or (kind == "template") or row["attempts"] + 1 >= MAX_ATTEMPTS:
                self.db.outbox_fail(row["id"], err)
                summary["failed"] += 1
                log.warning("message %s failed: %s", row["id"], err)
                continue
            delay = (600 if kind == "pair_limit" else min(15 * 2 ** row["attempts"], 1800))
            if res.retry_after:
                delay = max(delay, res.retry_after)
            self.db.outbox_retry(row["id"], now + timedelta(seconds=delay), err)
            summary["retry"] += 1
        return summary

    async def loop(self, interval_seconds=3):
        while True:
            try:
                await asyncio.to_thread(self.run_once)
            except Exception:
                log.exception("dispatcher pass failed")
            await asyncio.sleep(interval_seconds)
