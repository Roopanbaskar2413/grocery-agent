# Fridge-to-cart grocery agent (milestone 8: in-app chat)

Turns a smart fridge's shopping list into an Instamart order, with chat on WhatsApp.
This milestone runs the whole flow against a **mock fridge, a mock Instamart and a console
messenger**. No network, no keys, no dependencies beyond pytest.

    pip install -r requirements.txt
    python3 demo.py          # prints six scenarios
    python3 -m pytest -q     # 181 tests

    # run the server locally (messages print to the console)
    export WHATSAPP_VERIFY_TOKEN=... WHATSAPP_APP_SECRET=... FRIDGE_API_KEY=...
    uvicorn run_dev:app --port 8000

## What is in it
| File | Job |
|---|---|
| `agent/decide.py` | When to start a run: 4+ in-stock items, 6 PM with 2+, or Order now |
| `agent/matcher.py` | Rule-based product matching. Out of stock and no match are different |
| `agent/orchestrator.py` | Run lifecycle, self and permission modes, timers, checkout, list cleanup |
| `agent/llm_gate.py` | The only door for Gemini and chat: allowlisted actions with hard limits |
| `agent/mock_instamart.py` | Same tool names as the Swiggy MCP, with settable price, stock and COD |
| `agent/mock_fridge.py`, `agent/messenger.py` | Fake fridge list and display, fake WhatsApp |
| `agent/whatsapp.py` | Outbox + dispatcher + Cloud API client: queued sending, retries, 24 h window, templates |
| `agent/api.py` | FastAPI: WhatsApp webhook, fridge endpoints, status. Thin, no path to ordering |
| `agent/intent.py` | Text to raw actions (rules now, Gemini later through the same interface) |
| `agent/scheduler.py`, `agent/clock.py` | Every-minute loop: timers, 6 PM check with restart catch-up, silent-fridge warning |
| `agent/db.py` | SQLite: runs, ledger, pending items, watchlist, held messages, mode. Atomic `claim_run` |
| `agent/config.py` | Thresholds, caps, timers, the 3 phone numbers |

## Safety rules enforced in code (and tested)
- Only `Agent._checkout` calls `store.checkout`. Gemini and chat have no path to it.
- Unanswered approval: one reminder at 60 min, expiry at 6 h, cart cleared, items stay on the list.
  The timer starts when the message is actually delivered, so quiet hours do not eat it.
- Stock and price are re-read immediately before the checkout call. A rise above the tolerance
  goes back to the user for a new OK instead of ordering.
- Chat-added items go to a pending list for the next run. They never change an approved cart.
  Limits: quantity 1 to 5, 3 items per message, 10 per day, one decision per message.
- Spend cap and weekly order limit make even self mode ask first.
- First reply from any of the 3 phones wins. Later replies never order twice.
- Items leave the fridge list only after the order is confirmed.
- Self mode asks for approval when the cart includes any item added in chat, even a quantity added to a fridge item.

## HTTP entry points
| Route | Who | What it does |
|---|---|---|
| `GET /webhook/whatsapp` | Meta | Verification handshake (token compared in constant time) |
| `POST /webhook/whatsapp` | Meta | Buttons and texts from the 3 numbers. HMAC signature required |
| `POST /fridge/list` | Fridge | Pushes the list (validated names and quantities), optional `order_now` |
| `POST /fridge/order-now` | Fridge | The Order now button |
| `GET /status`, `GET /health` | You, monitors | Status needs the fridge key |

There is exactly one door from the outside to the agent. Texts and button taps become raw action dicts and go to
`Agent.handle_chat`, where `llm_gate` validates them. The fridge goes to `Agent.tick`. `tests/test_api.py` reads
`api.py` and fails if it mentions checkout, the cart, claiming a run, or any agent method outside a short allowlist.

Webhook rules:
- Messages from numbers other than the 3 configured ones, future-dated messages and floods (over 20 a minute) are dropped silently.
- A message id is recorded only after it was handled. A crash returns 500 so Meta retries, and a repeat of a handled id is skipped.
- A tapped button carries the run and which ask it belongs to (`approve:R1:2`). After a price change the old button stops working.
- A typed reply only counts if it was sent after the cart was shown (10 second tolerance), so a replayed "yes" cannot approve a newer cart.
- `approve` and `order_now` from free text need an exact phrase ("yes", "place order", "order now"), whatever produced the action.
  A language model that guesses them from "sounds good maybe" is dropped. Cancel, add, remove and watch are allowed from a model.

## Sending on WhatsApp (agent/whatsapp.py)
The agent never calls Meta. `Messenger.send` writes a row to the `outbox` table (fast, no network). The `Dispatcher`
delivers rows in the background, on its own database connection, so a slow network call never sits inside the agent lock.

- **24 hour window.** Free-form text and reply buttons only work within 24 h of a person's last message. The webhook
  records every inbound message, so we know each person's window. Outside it, a template is sent. If Meta says
  131047 anyway, the message is retried once as a template.
- **Long carts.** A button message body is limited to 1024 characters, so a long cart goes as a text message and the
  choice follows as a short message with the buttons.
- **Errors.** 190, 0, 200-299 and HTTP 401 mean the token is dead: sending pauses, nothing is lost, `/health` turns
  not-ok, and a restart clears the pause. 130429, 4, 80007, 429 and 5xx are retried with backoff (15 s doubling, up to
  30 min, 6 attempts). 131056 waits 10 minutes. Other 4xx fail that one message and the rest continue.
- **Delivery receipts** (sent, delivered, read, failed) arrive on the webhook and update the outbox. Status never moves backwards.
- **Button taps.** A tap on a normal reply-button message arrives as `interactive.button_reply`. A tap on a *template*
  quick-reply button arrives as `type: "button"` with `button.payload`. Both are handled.
- **At least once.** If the process dies after Meta accepts a message but before we record it, it is sent again after
  the restart. A duplicate reminder is better than a lost cart.

### Two templates to create in Meta's WhatsApp Manager
A template body cannot start or end with a variable, and variables cannot contain line breaks, so the agent
flattens the cart into one line. Suggested text (language `en`):

| Name | Body | Quick-reply buttons |
|---|---|---|
| `grocery_cart` | `Grocery cart is ready: {{1}} Choose an option below, or reply here.` | `Place order`, `Cancel` |
| `grocery_update` | `Grocery agent update: {{1}} Reply here any time to chat with the agent.` | none |

Meta decides the category (utility or marketing) and may re-categorise; that affects price. Approval can take up to a day.
Outside the 24 h window the "Cancel only" message (cash on delivery not available) goes as `grocery_update` without a button.

### What has and has not been tested
Everything above is tested against a scripted stand-in for Meta (`tests/test_whatsapp.py`), which proves our handling of
the documented responses. It does **not** prove Meta's real behaviour. Check these on the first real run:
1. the verify handshake and signature (the app secret must be the app's, not the access token),
2. that a template quick-reply tap really arrives as `type: "button"` in your webhook,
3. that the test number's recipient allow-list contains all three phones,
4. that the access token is a long-lived system-user token, not a short-lived one from the dashboard,
5. the exact error codes you get for a closed window and a rate limit (`GET /status` shows each failed message's error).

## Persistence rules
- The database is the truth. Each public call (`tick`, `handle_reply`, `handle_chat`) reloads state at the
  start and saves at the end.
- `claim_run` is a single `UPDATE ... WHERE state='awaiting_user'`. Only one reply, in any process, can win.
- A run found in `ordering` after a restart means the process died near checkout. It is marked failed and
  never retried. A person checks Instamart first. That is deliberate: a missed order is better than a double one.
- Reply timers and the reminder flag are stored, so a restart does not reset or repeat them.
- Assumption for now: one server process. Two processes are safe for the claim above, but other tables are
  last-write-wins.

## Gemini chat parser (agent/gemini.py)
Set `GEMINI_API_KEY` (and optionally `GEMINI_MODEL`, default `gemini-3.5-flash-lite`, and `GEMINI_DAILY_BUDGET`, default 40)
before starting `run_dev.py`. Without a key the rule parser is used. Check the model name and your free-tier limits
in Google AI Studio, they change.

What Gemini may do: suggest actions (add, remove, watch, cancel, order now, approve, change mode) and write one short
reply or a clarifying question. What it may not do, enforced in code and tested:
- **Exact commands never reach it.** `yes`, `no`, `order now` and their exact variants are handled by rules.
- **Fuzzy approval is impossible.** Any `approve` or `order_now` it suggests is dropped unless the message is an exact
  phrase. All its actions still pass `llm_gate` (item name pattern, quantity 1 to 5, 3 items per message, 10 per day).
- **It never sees or writes run ids.** The id comes from the open run. A model-supplied `run_id` is ignored.
- **Its reply is used only when it suggested no action**, so it cannot announce that something happened. Replies that
  claim an order was placed or paid are replaced. Replies are cut to 300 characters and links removed.
- **At most 2 clarifying questions** in a row, then a fixed fallback line.
- **Privacy:** the request holds your message, up to 6 recent turns of that conversation (30 minute window), item
  names, sizes, quantities and prices. No phone numbers, tokens, addresses or ids. The key goes in the
  `x-goog-api-key` header. On the free tier Google may use prompts to improve its products.
- **Failure never blocks chat:** 429 (cooldown from `Retry-After`, at least 60 s, 5 min if absent), 5xx and network errors
  (60 s), other 4xx (30 min, for example a bad key or model name), bad JSON, safety blocks and an exhausted daily
  budget all fall back to the rule parser. The last error is in the `gemini_last_error` kv row.
- The model call runs without the agent lock, so a slow Gemini response never delays the scheduler or other replies.

Not verified against real Google: the request shape follows Google's docs, the tests use a scripted stand-in.
The first real run should confirm that a reply parses, that `responseSchema` is accepted by your model, and what
your actual free-tier limit is.

## Stock watcher ("remind me when milk is back")
`Agent.watch_check`, run by the scheduler every minute but doing real work at most once per `watch_interval_min` (60).
- **Read-only.** It only calls `search_products`. It never touches the cart and never starts an order.
- When a watched item is in stock it is added to the pending list (counts toward the 4, joins the next run by the
  normal rules), everyone gets one message, and the watch ends. If the item is already on the fridge list it is not added twice.
- Watching an item that is already in stock fires on the next scheduler pass, it does not wait an hour.
- A store error keeps the watch and retries next time. One failing item does not block the others.
- At most `watch_max_items` (10) at once. A watch that never fires stops after `watch_expiry_days` (14) with a message.
  "remove X" also stops the watch. The list and start dates are stored, so a restart loses nothing.
- The back-in-stock message follows quiet hours (held until 7 AM), but the item is queued straight away.
- Not done on purpose: a watch hit does not start an order by itself. The fridge push, Order now or the 6 PM check does.
- Only matched against the mock store. Real Instamart search behavior (stock flags, naming) is unverified.

## In-app chat (no Meta needed)
`./start_chat.sh` starts the agent with a chat page at `/chat` (open it in any browser, phone included).
- **Same door as WhatsApp.** Text goes to the parser, then the gate. Button taps use the same `approve:R1:2` ids with the
  same stale-ask protection. The chat code in `api.py` has no path to ordering, and the structural test covers it.
- **Sign-in:** one secret token per person (at least 16 characters, kept in `.chat_tokens`, at most 3). Tokens map to
  recipients `app:1` to `app:3`. Each person only sees their own messages. Only `app:1`, the owner, can change the mode.
- **Page safety:** strict Content-Security-Policy (own script only, no inline script, no framing). Message text is shown
  with text nodes only, never HTML. A test fails if `chat.js` ever uses `innerHTML` or `eval`.
- **Flood limit** of 20 messages per minute per person. Messages are stored in the database for 14 days.
- **Phones and app users can coexist.** Recipients starting with `app:` go to the chat store, all others to WhatsApp.
- **Push notifications** (optional, `agent/push.py`): a Web Push subscription wakes up a closed chat tab. Set
  `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` (generate once with `pip install py-vapid && vapid --gen`) and
  `VAPID_SUBJECT` (a `mailto:` address); without them the page still works, it just polls every 2 seconds while
  open. Push is best-effort on top of that poll, never a replacement for it -- an expired subscription (404/410)
  is dropped, anything else is logged and left for the next message.
- **Not built:** HTTPS (plain http is fine on your own network only, the real deploy at DRYRUN.md uses Fly.io's
  managed TLS instead) and a native app. A native app can reuse the same four endpoints (`/chat/send`,
  `/chat/tap`, `/chat/messages`, bearer token).

## First real run (WhatsApp)
See DRYRUN.md for the step by step, and `python3 dryrun.py` for the helper (env check, local check, push a list, watch the outbox).

## Real Instamart adapter (agent/real_instamart.py, agent/oauth.py, agent/mcp_client.py)
Built and unit-tested against a scripted stand-in for Swiggy's Instamart MCP server
(`mcp.swiggy.com/im`, 16 tools), following the tool names and recipes in Swiggy's Builders Club docs. Not yet
run against the real API -- Builders Club access is pending, and `Settings.instamart_backend` defaults to
`"mock"` so nothing switches over on its own.
- Same interface as `MockInstamart` (`search_products`, `get_cart`, `update_cart`, `get_payment_options`,
  `checkout`, `.orders`), so `agent/orchestrator.py` does not know or care which one it is talking to.
- A product's `id` here is `"<spinId>:<skuId>"`; address resolution (`get_addresses`) is cached internally and
  never surfaces to the orchestrator.
- `checkout` is **not idempotent**: on a 5xx or timeout, the adapter calls `get_orders` and checks for a new
  order before raising, instead of leaving an unknown outcome or retrying blindly.
- Instamart's own ₹99 minimum order is checked in the adapter, separate from `Settings.spend_cap`.
- The no-UPI fallback payment string (`"COD"` vs `"Cash"`, Swiggy's docs disagree) is
  `Settings.instamart_cod_payment_value`, not hard-coded.
- OAuth: dynamic client registration (RFC 7591, no client secret to request), PKCE (S256), encrypted token
  storage (`agent/oauth.py`, Fernet key from `SWIGGY_TOKEN_KEY`, token file kept out of git), and refresh-token
  rotation (the old refresh token is discarded on every refresh). `RealInstamart` refuses to construct with no
  stored token.
- See DRYRUN.md's "First real run against Instamart" section for exactly what to verify once access arrives.

## Browser adapter (agent/browser_instamart.py, agent/browser_instamart_playwright.py) -- manual verification only
A third Instamart implementation, driving the real logged-in Instamart website with Playwright instead of
calling an API. It exists to verify the pipeline (search -> cart -> price/stock re-check) against the real
site right now, without waiting on Builders Club access. Same interface as the other two adapters, so
`agent/orchestrator.py` does not change -- but **this is not wired into `run_dev.py` or `Settings.instamart_backend`**,
it is a tool you run yourself with `browser_pipeline_check.py`, not a third production backend choice.

Deliberate, permanent safety choice, not a gap to fill in later: `checkout()` here never clicks the final
"place order" button. It re-reads price and stock (retrying until two consecutive reads agree, to ride out
page-load delays), checks the ₹99 minimum, and -- now that the real flow has been walked through by hand --
navigates as far as confirming an address is selected, clicking through to `/payment`, and selecting "Pay on
Delivery" (the only payment method this agent ever picks; never cards, wallets, or netbanking). It then
raises `InstamartCheckoutPaused` with the reconciled cart every single time, so a human decides whether to
place it in the still-open browser tab. There is no selector anywhere in this codebase for picking a saved
delivery address (checkout stops and tells you to pick one by hand if none is already selected) or for a
final "Place Order" confirm button on the COD flow -- both are deliberately never looked for. Reasons:
- checkout is the one action with real account and money risk (Swiggy's ToS almost certainly prohibits
  automated access to the consumer site; a flagged account is a real possible outcome of this adapter,
  unlike the mock and MCP adapters which never touch a real account at all).
- if the live checkout flow needs an OTP or app confirmation, there is nowhere in this synchronous interface
  to wait for that -- you being present for the one real click sidesteps the problem entirely.
- because code never performs the risky action, the "was the order actually placed" ambiguity that
  `real_instamart.py` resolves with `get_orders()` never arises here.

Most CSS selectors in `agent/browser_instamart_playwright.py`'s `Selectors` dataclass are now real, confirmed
against the live site by hand (search, cart, variant modal, checkout footer, COD). A few are still unverified
placeholders (card out-of-stock badge, cart subtotal fallback, delivery fee, UPI option, order history) --
see the dataclass docstring for the current list. Expect real ones to break again on any Instamart frontend
redeploy, with no changelog to warn you, unlike the MCP tool schemas.

Add-to-cart is per-item resilient: `update_cart()` tries every item independently and returns a list of
`AddOutcome` instead of aborting the whole batch on the first product that fails to add (seen live: some
products take several seconds to actually persist after a confirmed click; others never stick at all, or
throw outright, e.g. an unhandled variant-picker modal). A failed add is always reported, never silently
treated as a success. `browser_pipeline_check.py` prints a summary of what got added, what had no in-stock
match, and what failed with why.

Tested with `FakeInstamartPage`, a fully in-memory double (`tests/test_browser_instamart.py`) -- no real
browser or Playwright needed to run the test suite; Playwright is only required to actually run
`browser_pipeline_check.py` against the live site.

## Not built yet (next milestones)
exercising the real Instamart adapter against the live API (needs Builders Club access, requested separately),
UPI payment polling (the MCP tools exist -- `check_payment_status`, `confirm_order` -- but the orchestrator's
state machine only drives the COD path today), and a mobile app (on hold for now; see the chat section above
for the four endpoints a native app would reuse).
