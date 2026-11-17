# First real WhatsApp dry run (laptop + ngrok + Meta test number)

Goal: find the Meta-side surprises while the system is still simple. The store is still the mock, so nothing real is
ordered. Meta's dashboard menus change, so names below are approximate: go by the goal of each step.

Before you start: if it is between 10 PM and 7 AM, set `QUIET_START=24 QUIET_END=0`, otherwise messages are
held until morning and it will look like nothing works.

## 1. Meta setup (once)
1. Create an app in Meta for Developers (type Business) and add the WhatsApp product.
2. WhatsApp > API Setup gives a **test phone number**, its **Phone number ID** and a temporary token.
3. Add your 3 phones as recipients of the test number (there is an allow-list, and each person confirms with a code).
4. Make a **long-lived token**: Business Settings > System users > create one, give it the app and WhatsApp
   permissions, generate a token that does not expire. The dashboard token lasts about a day and will fail overnight.
5. Find the **App secret** (App settings > Basic). This is what signs webhooks. It is not the token.

## 2. Start the agent
    export WHATSAPP_VERIFY_TOKEN=<any string you invent> WHATSAPP_APP_SECRET=<app secret> FRIDGE_API_KEY=<invent>
    export WHATSAPP_TOKEN=<long-lived token> WHATSAPP_PHONE_NUMBER_ID=<phone number id>
    export PHONE_1=+91... PHONE_2=+91... PHONE_3=+91...        # digits with country code
    export GEMINI_API_KEY=<optional>
    python3 dryrun.py env                    # all lines should say ok
    uvicorn run_dev:app --port 8000          # terminal 1
    ngrok http 8000                          # terminal 2
    BASE_URL=http://localhost:8000 python3 dryrun.py local     # all ok

## 3. Connect the webhook
In WhatsApp > Configuration set Callback URL `https://<ngrok-host>/webhook/whatsapp` and your verify token, click
Verify, then subscribe to the **messages** field.
- Verify fails (403): token typo, or ngrok is not forwarding. Look at terminal 1 for the GET request.
- No GET at all: wrong URL or ngrok stopped. Free ngrok URLs change on every restart.

## 4. Message the test number first
From your phone send `hi` to the test number. Expect: a reply "I did not catch that..." with the help text.
- Reply arrives: signature check, inbound parsing and free-form sending all work.
- No reply and `dryrun.py status` shows nothing in the outbox: the webhook did not arrive or the signature was
  refused (terminal 1 shows `403 bad signature`). The app secret is wrong, or you used the token by mistake.
- Outbox row failed: read its error in `dryrun.py status` (see the table).

## 5. The real test: a cart with buttons
    python3 dryrun.py push
All 3 phones should get "Cart ready (R1)..." with **Place order** and **Cancel** buttons (they arrive as free-form
buttons because you just messaged, so the 24 hour window is open).
Then check each, noting what happens:
1. Tap **Place order** on phone 1. Expect "Order placed" on all phones. The server log should show the tap handled.
2. Run `push` again, tap **Place order** on phone 2, then on phone 3. Expect the second tap to be told it was
   already handled. This is the double-order race, now over real WhatsApp.
3. Run `push` again, tap **Cancel**. Expect cancelled, and the items stay on the list.
4. Run `push` again, reply by text with exactly `yes`. Expect an order. Reply `sounds good` instead, expect no order.
5. Send `add 2 milk` then `remind me when curd is back`. With Gemini on, also try a looser one like
   `we ran out of eggs`. Check `dryrun.py status` shows `gemini: last_error` empty.

## 6. Delivery receipts
`python3 dryrun.py watch` while you do step 5. Each message should go pending > sent > delivered (> read).
If rows stay at `sent`, the receipts are not reaching the webhook (the messages subscription is missing).

## 7. After 24 hours (optional, needs templates)
Create the two templates from the README (`grocery_cart` with quick replies Place order/Cancel, `grocery_update`)
and wait for approval. Do not message the test number for 24 hours, then `push`. Expect the cart to arrive as a
template, and a tap on a template quick reply to be understood as an order. This is the least certain path:
a template tap arrives as type `button`, which was built from the docs and never seen live.

## What the failures mean (`dryrun.py status`, `error=`)
| You see | Probably | Do this |
|---|---|---|
| 131047 | 24 hour window closed | expected after a day of silence: the template path should take over; if it does not, the templates are missing or unapproved |
| 131030 or recipient not allowed | phone not on the test number's allow-list | add it in step 1.3 |
| 190, 0, 401, or 200-299 | token rejected, or missing permission | new long-lived token. `/health` will say sending is paused |
| 131056 | too many messages to one person | wait, the agent retries on its own |
| 130429, 4, 80007 | throughput limits | retried with backoff |
| status `failed` with an error you do not know | new case | send me the line, I will add a rule and a test for it |

## What to send me afterwards
`dryrun.py status` output, and for any step that did not match "Expect", what you saw on the phone and the last
lines of terminal 1. Please do not paste tokens or the app secret.

## First real run against Instamart (once Builders Club access is approved)
The real adapter (`agent/real_instamart.py`) is built and unit-tested against a scripted stand-in for
`mcp.swiggy.com/im`, following the documented tool names and recipes, but it has never been run against
the real server. Set `INSTAMART_BACKEND=real` only after confirming these, one at a time, with `INSTAMART_BACKEND`
left at `mock` as the safe fallback:

1. **Free-delivery field name.** `get_cart`'s `billBreakdown` is confirmed to carry `toPay.value` (the grand
   total); the adapter guesses `deliveryFee.value` for the fee. Call `get_cart` once for real and check the
   actual field name, then fix `RealInstamart.get_cart` if it differs.
2. **COD vs "Cash".** Swiggy's own docs use both strings for the no-UPI fallback. The adapter sends whatever
   `Settings.instamart_cod_payment_value` is set to (default `"COD"`). If checkout rejects it, try `"Cash"`
   (`INSTAMART_COD_PAYMENT_VALUE=Cash` as an env var, or the setting directly) before assuming COD is unavailable
   for your account.
3. **search_products response shape.** The adapter accepts either a flat list or a `variations` sub-list per
   product; confirm which one the real API returns for a few queries before trusting the price/stock mapping.
4. **OAuth sign-in.** The login flow (dynamic client registration, PKCE, phone+OTP) is implemented in
   `agent/oauth.py` but the browser round trip itself has not been run end to end. The first login needs a
   reachable HTTPS redirect URI (or `http://localhost` for this kind of local testing).
5. **checkout's non-idempotency.** On a 5xx or timeout the adapter calls `get_orders` before giving up, to avoid
   a false "nothing happened." Watch the log line "checkout call failed ... but a new order ... appeared" on the
   first few real runs to make sure that path actually works as intended.

Tokens for the real adapter are stored encrypted (`SWIGGY_TOKEN_KEY`, a Fernet key) in a file kept out of git
(`.swiggy_token` by default, already in `.gitignore`). Never paste a token or `SWIGGY_TOKEN_KEY` into chat or a
commit.
