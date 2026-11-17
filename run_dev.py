"""Local run against the mock fridge and mock Instamart, with messages printed to the console.

    export WHATSAPP_VERIFY_TOKEN=... WHATSAPP_APP_SECRET=... FRIDGE_API_KEY=...
    # optional, turns on real sending (otherwise messages print to the console):
    export WHATSAPP_TOKEN=... WHATSAPP_PHONE_NUMBER_ID=...
    uvicorn run_dev:app --port 8000
    ngrok http 8000          # then point Meta's webhook at https://<ngrok>/webhook/whatsapp

The real WhatsApp sender, the real Gemini parser and the real Instamart adapter plug in here later.
"""
import os

from agent.api import ApiSecrets, create_app
from agent.clock import now_local
from agent.config import Settings
from agent.db import Database
from agent.gemini import GeminiIntentParser, HybridIntentParser
from agent.intent import RuleIntentParser
from agent.messenger import APP_PREFIX, ChatMessenger, ConsoleMessenger, RoutingMessenger
from agent.mock_fridge import MockFridge
from agent.mock_instamart import MockInstamart
from agent.oauth import OAuthClient, TokenStore
from agent.orchestrator import Agent
from agent.real_instamart import RealInstamart
from agent.whatsapp import Dispatcher, OutboxMessenger, WhatsAppClient


class PrintMessenger(ConsoleMessenger):
    def send(self, to, text, buttons=None):
        super().send(to, text, buttons)
        titles = f"  [{' | '.join(b[1] for b in buttons)}]" if buttons else ""
        print(f"WhatsApp to {to}: {text}{titles}", flush=True)


# People: phones (WhatsApp) and/or app chat users. CHAT_TOKEN_1..3 create in-app users "app:1".."app:3".
recipients, chat_tokens = {}, {}
for i in (1, 2, 3):
    token = os.environ.get(f"CHAT_TOKEN_{i}", "")
    if token:
        recipients[f"app:{i}"] = "Roopan" if i == 1 else f"Person {i}"
        chat_tokens[token] = f"app:{i}"
for i, default in ((1, "+919800000001"), (2, "+919800000002"), (3, "+919800000003")):
    phone = os.environ.get(f"PHONE_{i}", "" if chat_tokens else default)       # default fake phones only without chat
    if phone:
        recipients[phone] = "Roopan" if not recipients else f"Person {len(recipients) + 1}"
cfg = Settings(quiet_start_hour=int(os.environ.get("QUIET_START", 22)), quiet_end_hour=int(os.environ.get("QUIET_END", 7)),
               recipients=recipients,
               instamart_backend=os.environ.get("INSTAMART_BACKEND", "mock"),
               instamart_cod_payment_value=os.environ.get("INSTAMART_COD_PAYMENT_VALUE", "COD"))
db = Database(os.environ.get("AGENT_DB", "agent.db"))
clock = lambda: now_local(cfg.timezone)

# Real Instamart: explicit opt-in (INSTAMART_BACKEND=real) and a stored OAuth token, or this refuses
# to start. Nothing here has run against the real API yet -- see DRYRUN.md.
if cfg.instamart_backend == "real":
    oauth = OAuthClient(TokenStore(os.environ.get("SWIGGY_TOKEN_PATH", ".swiggy_token")),
                        os.environ.get("SWIGGY_OAUTH_REDIRECT_URI", "http://localhost/callback"))
    store = RealInstamart(oauth, cfg)
else:
    store = MockInstamart()

dispatcher = None
if os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"):
    # real sending: messages go to the outbox, the dispatcher delivers them through Meta's Cloud API
    messenger = OutboxMessenger(db, clock)
    client = WhatsAppClient(os.environ["WHATSAPP_TOKEN"], os.environ["WHATSAPP_PHONE_NUMBER_ID"],
                            os.environ.get("WHATSAPP_API_VERSION", "v25.0"))
    dispatcher = Dispatcher(db.clone(), client, clock,
                            cart_template=os.environ.get("WA_TEMPLATE_CART", "grocery_cart"),
                            update_template=os.environ.get("WA_TEMPLATE_UPDATE", "grocery_update"),
                            language=os.environ.get("WA_TEMPLATE_LANG", "en"))
else:
    messenger = PrintMessenger()                       # no token set: print instead of sending

if chat_tokens:
    messenger = RoutingMessenger(ChatMessenger(db, clock), messenger)      # app users get chat, phones get WhatsApp
agent = Agent(MockFridge(), store, messenger, cfg, db)
import secrets as _secrets
# WhatsApp values may be left unset for a chat-only run. A random secret nobody knows keeps the webhook closed.
secrets = ApiSecrets(os.environ.get("WHATSAPP_VERIFY_TOKEN") or _secrets.token_hex(16),
                     os.environ.get("WHATSAPP_APP_SECRET") or _secrets.token_hex(16),
                     os.environ.get("FRIDGE_API_KEY") or _secrets.token_hex(16))
if os.environ.get("GEMINI_API_KEY"):
    # chat understanding by Gemini; exact commands and any Gemini failure fall back to the rules
    parser = HybridIntentParser(GeminiIntentParser(
        os.environ["GEMINI_API_KEY"], db.clone(), clock,
        model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        daily_budget=int(os.environ.get("GEMINI_DAILY_BUDGET", "40"))))
else:
    parser = RuleIntentParser()
app = create_app(agent, parser, secrets, clock=clock, dispatcher=dispatcher, chat_tokens=chat_tokens)
