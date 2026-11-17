from dataclasses import dataclass, field


@dataclass
class Settings:
    # Trigger rules
    list_threshold: int = 4            # in-stock items that start a run at once
    evening_hour: int = 18             # 6 PM check
    evening_min_items: int = 2         # at the evening check, order if this many or more

    # Guardrails
    spend_cap: int = 1500              # rupees per order, above this the agent asks first
    max_orders_per_week: int = 4       # automatic orders per rolling 7 days, above this it asks first
    price_tolerance_pct: float = 5.0   # allowed rise between approved total and checkout total
    max_qty_per_item: int = 5

    # Approval timers (permission mode)
    approval_reminder_min: int = 60
    approval_expiry_min: int = 360
    # Pay-in-app timers (COD unavailable)
    app_pay_reminder_min: int = 60
    app_pay_expiry_min: int = 180

    # Messaging
    quiet_start_hour: int = 22
    quiet_end_hour: int = 7
    recipients: dict = field(default_factory=lambda: {
        "+91-PHONE-A": "Roopan",
        "+91-PHONE-B": "Person B",
        "+91-PHONE-C": "Person C",
    })

    # Chat-added items (Gemini gate)
    max_chat_items_per_message: int = 3
    max_chat_items_per_day: int = 10

    fridge_silence_days: int = 2       # warn if the fridge has not sent a list for this long
    timezone: str = "Asia/Kolkata"
    reply_skew_seconds: int = 10       # tolerance between WhatsApp timestamps (whole seconds) and our clock

    mode: str = "permission"           # "self" or "permission"

    # Watched items ("remind me when X is back")
    watch_interval_min: int = 60       # how often watched items are looked up in the store
    watch_max_items: int = 10          # at most this many watched at once
    watch_expiry_days: int = 14        # a watch that never fires stops after this long

    # Real Instamart adapter (agent/real_instamart.py). Mock is always the default; "real" needs an
    # explicit opt-in and a stored OAuth token (agent/oauth.py), and nothing should run against the
    # real API before Builders Club access is approved.
    instamart_backend: str = "mock"           # "mock" or "real"
    instamart_cod_payment_value: str = "COD"  # the literal paymentMethod string sent to checkout;
                                               # Swiggy's own docs disagree on "COD" vs "Cash" -- verify
                                               # on the first real run (see DRYRUN.md) and adjust here
