"""Helper for the first real-WhatsApp dry run. Standard library only.

    python3 dryrun.py env                 check your environment variables (no network)
    python3 dryrun.py local               check the running server: health, handshake, signature rejection
    python3 dryrun.py push [--order-now]  push a 4 item fridge list (starts a cart message to your phones)
    python3 dryrun.py status              show run state, outbox counts and the newest message errors
    python3 dryrun.py watch               same as status, every 5 seconds (Ctrl+C to stop)

Server address: BASE_URL (default http://localhost:8000). It reads the same environment variables as run_dev.py.
Nothing here places a real order: the store is still the mock.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")


def need(name):
    v = os.environ.get(name, "")
    return v


def call(method, path, body=None, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def line(ok, text):
    print(("  ok   " if ok else "  FAIL ") + text)
    return ok


def cmd_env():
    ok = True
    print("Required:")
    for n in ("WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET", "FRIDGE_API_KEY"):
        ok &= line(bool(need(n)), f"{n} is set")
    print("Needed for real sending (otherwise messages print to the server console):")
    tok, pid = need("WHATSAPP_TOKEN"), need("WHATSAPP_PHONE_NUMBER_ID")
    line(bool(tok), "WHATSAPP_TOKEN is set")
    line(bool(pid) and pid.isdigit(), "WHATSAPP_PHONE_NUMBER_ID is set and numeric (the phone number ID, not the phone number)")
    if tok:
        line(len(tok) > 100, "token looks long enough for a system-user token (a short dashboard token expires in about a day)")
    print("Phones (country code, digits only after +):")
    for n in ("PHONE_1", "PHONE_2", "PHONE_3"):
        v = need(n)
        line(bool(re.fullmatch(r"\+\d{8,15}", v)), f"{n} looks like +<countrycode><number>" if v else f"{n} not set (the default fake number is used)")
    print("Optional:")
    line(True, f"GEMINI_API_KEY {'set' if need('GEMINI_API_KEY') else 'not set (rule parser only)'}")
    line(True, f"QUIET_START/QUIET_END {'overridden' if need('QUIET_START') or need('QUIET_END') else 'default 22 to 7'}: "
               "messages sent in quiet hours are held until morning, which looks like nothing happening")
    return 0 if ok else 1


def cmd_local():
    ok = True
    code, text = call("GET", "/health")
    ok &= line(code == 200 and '"ok":true' in text.replace(" ", ""), f"/health -> {code} {text[:120]}")
    verify, secret = need("WHATSAPP_VERIFY_TOKEN"), need("WHATSAPP_APP_SECRET")
    q = urllib.parse.urlencode({"hub.mode": "subscribe", "hub.verify_token": verify, "hub.challenge": "4242"})
    code, text = call("GET", "/webhook/whatsapp?" + q)
    ok &= line(code == 200 and text == "4242", f"handshake with your verify token -> {code} {text[:40]}")
    q = urllib.parse.urlencode({"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "4242"})
    code, _ = call("GET", "/webhook/whatsapp?" + q)
    ok &= line(code == 403, f"handshake with a wrong token is refused -> {code}")
    body = json.dumps({"entry": []}).encode()
    code, _ = call("POST", "/webhook/whatsapp", body, {"Content-Type": "application/json"})
    ok &= line(code == 403, f"unsigned webhook is refused -> {code}")
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    code, _ = call("POST", "/webhook/whatsapp", body, {"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    ok &= line(code == 200, f"webhook signed with your app secret is accepted -> {code}")
    print("\nThis proves the server and your secret agree with each other. It does NOT prove Meta signs the same way:\n"
          "that is checked by the real message in step 5 of DRYRUN.md.")
    return 0 if ok else 1


def cmd_push(order_now):
    items = [{"name": n, "qty": 1} for n in ("Tomato", "Onion", "Cucumber", "Mushroom")]
    code, text = call("POST", "/fridge/list", {"items": items, "order_now": order_now},
                      {"Content-Type": "application/json", "X-Fridge-Key": need("FRIDGE_API_KEY")})
    print(code, text)
    return 0 if code == 200 else 1


def cmd_status():
    code, text = call("GET", "/status", headers={"X-Fridge-Key": need("FRIDGE_API_KEY")})
    if code != 200:
        print(code, text)
        return 1
    s = json.loads(text)
    print(f"mode={s['mode']}  open_run={s['open_run']}  pending_items={s['pending_items']}")
    print(f"outbox counts: {s.get('outbox')}")
    for r in s.get("outbox_recent", []):
        extra = f"  error={r['error']}" if r["error"] else ""
        print(f"  msg {r['id']}: {r['status']:<10} attempts={r['attempts']} template={r['as_template']} wamid={'yes' if r['has_wamid'] else 'no'}{extra}")
    g = s.get("gemini") or {}
    if g.get("last_error") or g.get("cooldown_until"):
        print(f"gemini: last_error={g.get('last_error')} cooldown_until={g.get('cooldown_until')}")
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    c = argv[0]
    if c == "env":
        return cmd_env()
    if c == "local":
        return cmd_local()
    if c == "push":
        return cmd_push("--order-now" in argv)
    if c == "status":
        return cmd_status()
    if c == "watch":
        try:
            while True:
                print("\n" + time.strftime("%H:%M:%S"))
                cmd_status()
                time.sleep(5)
        except KeyboardInterrupt:
            return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
