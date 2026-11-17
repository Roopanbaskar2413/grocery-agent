"""Web Push notifications for the in-app chat (agent/static/chat.html), so a message can get
someone's attention even when the chat tab is closed -- without this, a new message is only seen
by the 2-second poll while the page is open.

Encryption (RFC 8291) and the VAPID signature (RFC 8292) are delegated to pywebpush, a maintained
library for exactly this; this module only wires it to our subscription storage and decides what to
do with a failure, the same spirit as agent/whatsapp.py classifying Meta's error codes.

Best-effort by design: a push that never arrives is not a lost message, since chat_messages (what
the poll loop reads) is the source of truth regardless. An expired subscription (404/410) is
removed so we stop trying it; any other failure is logged and left for the next message.
"""
import json
import logging
from typing import Callable, Optional

from pywebpush import WebPushException, webpush

log = logging.getLogger("agent.push")

GONE_STATUSES = (404, 410)


def _default_send(subscription_info, data, vapid_private_key, vapid_claims):
    webpush(subscription_info=subscription_info, data=data, vapid_private_key=vapid_private_key,
           vapid_claims=dict(vapid_claims))


class PushSender:
    def __init__(self, db, vapid_private_key: str, vapid_subject: str, send_fn: Optional[Callable] = None):
        self.db = db
        self.vapid_private_key = vapid_private_key
        self.vapid_subject = vapid_subject
        self._send = send_fn or _default_send

    def notify(self, recipient: str, title: str, body: str) -> None:
        payload = json.dumps({"title": title, "body": body})
        for sub in self.db.push_subscriptions(recipient):
            try:
                self._send(subscription_info=sub, data=payload, vapid_private_key=self.vapid_private_key,
                          vapid_claims={"sub": self.vapid_subject})
            except WebPushException as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in GONE_STATUSES:
                    self.db.remove_push_subscription(sub["endpoint"])
                else:
                    log.warning("push to %s failed (%s): %s", recipient, status, e)
            except Exception:
                log.warning("push to %s failed", recipient, exc_info=True)
