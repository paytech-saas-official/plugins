# PSP Gateway API — Webhooks (Final Payment Status)

Webhooks are the **primary** mechanism for learning a payment's final state;
`GET /api/v1/payments/{id}` is the polling fallback. A payment is **not**
done when the customer returns to your success page — only the webhook (or a
final state via GET) is authoritative. Never credit an order from the
browser redirect alone.

The same applies to the iframe `postMessage` event of an embedded checkout. The
docs list it beside webhooks and payment-by-id under "getting the final payment
status", but it is a **browser** signal whose only job is closing the iframe —
it is not a third way to learn the outcome. Contract and its traps:
`hosted-fields-and-wallets.md`.

---

## 1. When webhooks fire

The PSP POSTs to your webhook URL when a payment reaches a **final state**:

| State | Meaning |
|-------|---------|
| `COMPLETED` | Payment succeeded |
| `DECLINED` | Payment failed (see `errorCode` / `errorMessage`) |
| `CANCELLED` | Payment cancelled (e.g. `1.04 Cancelled by Timeout`) |
| `AUTHORIZED` | Funds authorized (2-phase / pre-auth flow — capture or void next) |

That positive statement is what the docs guarantee. Whether the non-final
`PaymentState` values (`CHECKOUT`, `PENDING`, `AWAITING_APPROVAL`) can also
arrive is **not documented** — the docs never say they are excluded. The
reasonable *inference* is that you normally only see them when polling, but
do not build on it: `POST /api/v1/payments/{id}/chargebacks` has its own
`webhookUrl`, and a chargeback with no explicit `state` is "created in an
open state awaiting processing" — i.e. notifications are not strictly
limited to the four states above. Handlers must ignore states they do not
recognize rather than assume they cannot arrive (§5).

**Caution (from the docs):** for some payment methods the final transaction
amount may differ from the initially requested amount. Book the `amount` /
`currency` from the webhook payload, not the amount you originally sent.

## 2. Configuring the webhook URL

Two ways (per the docs):

1. **Shop settings** — a default URL for the shop, or
2. **`webhookUrl` parameter** in the `POST /api/v1/payments` request —
   **overrides the shop settings** for that payment.

Also available: `webhookUrl` on the chargeback registration request
(`POST /api/v1/payments/{id}/chargebacks`) for chargeback status
notifications.

Point it at an endpoint on your **backend** that the PSP can reach, and
**use HTTPS** — strongly recommended, since the payload carries payment and
customer data. Note this is a security recommendation, **not a documented API
requirement**: `webhookUrl` is just a `string` (`maxLength: 512`) with no
`pattern` or `format` constraint, and the only HTTPS requirement in the docs
applies to *your* outbound calls to the PSP. The example URL in the spec's
webhook `servers` entry is an example, not a rule.

The expected response is **`200`** ("to indicate that the data was received
successfully" — per the spec's webhook definition).

## 3. Payload schema

`POST` to your URL, `Content-Type: application/json`. The body **is** the
`PaymentResult` object — the same object returned in `result` by
`GET /api/v1/payments/{id}`. Its full field reference lives in
`payment-lifecycle.md` (single source); it is not restated here.

Webhook-specific notes:

- **All fields are optional** in the schema — code defensively.
- **Book `amount` / `currency` from the payload**, not the amount you sent: the
  final amount can differ (FX — see `customerAmount` / `customerCurrency`).
- A webhook's `state` is a **final** one (`COMPLETED` / `DECLINED` / `CANCELLED`
  / `AUTHORIZED`); on a non-success, `errorCode` / `errorMessage` explain it.

Example payload (DEPOSIT → CANCELLED, from the docs):

```json
{
  "id": "9e9003d7f3324fc6828481c665d8bab5",
  "paymentType": "DEPOSIT",
  "state": "CANCELLED",
  "paymentMethod": "BANKTRANSFER",
  "amount": 2000,
  "currency": "CLP",
  "errorCode": "1.04",
  "errorMessage": "Cancelled by Timeout"
}
```

## 4. Signature verification

Every webhook carries a **`Signature`** HTTP header:

> HMAC-SHA256 hash generated from the JSON body using the **Shop Signing
> Key** as the secret.

Rules:

- Compute the HMAC over the **raw request body bytes** — before any JSON
  parsing or re-serialization (re-serialized JSON may reorder keys or change
  whitespace and will not match).
- Compare against the header using a **constant-time** comparison
  (`hmac.compare_digest`, `crypto.timingSafeEqual`, `MessageDigest.isEqual`).
- **Verify before parsing or acting** on the payload. Reject failures with
  `401`/`403` and do not touch order state.
- The output **encoding (hex vs base64) is not documented in the API
  docs** — verify against a sandbox webhook by computing both and matching
  the header, then pin the winner (see `authentication.md` §3).

## 5. Required handler behavior (checklist)

Implement all of these — each one covers a real failure mode:

1. **Verify the signature first** (raw body, constant-time). Fail → `401`,
   log, stop.
2. **Look up the payment** by `id` (PSP id) and/or `referenceId` (your id).
3. **Unknown payment**: not documented in the API docs. Recommended: log at
   warning level and return `200` (so the PSP does not keep redelivering
   something you will never match); alert if it keeps happening.
4. **Idempotency / duplicates**: the same webhook may arrive more than once,
   and a webhook can race your own `GET /payments/{id}` poll. Before
   applying, check whether the order is already in a final state for this
   payment `id` + `state`; if so, ack with `200` and do nothing. Serialize
   updates per payment `id` (DB row lock / unique constraint on
   `(payment_id, state)` transition).
5. **Safe state mapping** — only ever move forward:
   - `COMPLETED` → mark paid, credit the order, use payload `amount`/`currency`.
   - `AUTHORIZED` → mark authorized; trigger your capture/void logic.
   - `DECLINED` / `CANCELLED` → mark failed; store `errorCode`/`errorMessage`.
   - Never downgrade a final state (e.g. a late duplicate must not overwrite
     `COMPLETED`).
   - **Any other / unrecognized `state`**: log it and ack with `200` without
     changing order state. Do not treat an unexpected state as an error and do
     not assume it cannot arrive (see §1).
6. **Respond fast with `2xx`** — persist the event, return `200`, and do
   heavy work (emails, fulfillment, ledger postings) asynchronously.
7. **Log everything except sensitive data** — log `id`, `referenceId`,
   `state`, `errorCode`, amounts; never log the Signing Key, and treat
   `customer` / `paymentMethodDetails` contents as PII (mask or omit).

## 6. Retry / redelivery

**Not documented in the API docs** — no retry schedule, count, or timeout is
specified. Assume redelivery is possible (and that a non-`2xx` or slow
response may trigger it) and make the handler idempotent per §5. Because
delivery is not guaranteed either, keep a reconciliation path: poll
`GET /api/v1/payments/{id}` for payments still non-final after a reasonable
timeout.

## 7. Reference handler (Flask template)

Working code for Spring Boot, Express/NestJS and FastAPI — with the raw-body
recipe for each — is in the `code-examples-*.md` file for the project, and the
rules are in `integration-patterns.md`. This is the framework-neutral minimum,
so that the five properties below fit on one screen. Adapt it to the merchant's
framework, ORM and logging; whatever changes, these must survive:

1. Verify the HMAC-SHA256 `Signature` header against the **raw** body first.
2. Be idempotent — the same webhook may arrive more than once, and may race
   your own `GET /payments/{id}` polling.
3. Map payment state → order state through an explicit whitelist; never mark
   an order paid on anything but a final success state.
4. Answer `2xx` quickly; do slow work (emails, fulfilment) asynchronously.
5. Never log full card numbers, tokens or the signing key.

```python
import base64
import hashlib
import hmac
import json
import os

from flask import Flask, request

app = Flask(__name__)

SIGNING_KEY = os.environ["PSP_SIGNING_KEY"].encode()

# Payment state -> merchant order state. Explicit whitelist: anything not listed
# here must NOT change the order. AUTHORIZED means the funds are only held — a
# capture is still required — so it must not trigger fulfilment; only COMPLETED
# is "paid" (see payment-lifecycle.md).
FINAL_STATE_MAPPING = {
    "COMPLETED": "PAID",
    "AUTHORIZED": "AUTHORIZED",
    "DECLINED": "PAYMENT_FAILED",
    "CANCELLED": "PAYMENT_FAILED",
}
FULFILMENT_STATES = {"PAID"}   # the only order states allowed to start fulfilment


def signature_valid(raw_body: bytes, header_value: str) -> bool:
    """Constant-time check of the inbound Signature header.

    The encoding of the digest (hex vs base64) is not documented (§4), so both
    are accepted. Once one sandbox webhook has shown which one this PSP sends,
    narrow this to that single encoding.
    """
    if not header_value:
        return False
    digest = hmac.new(SIGNING_KEY, raw_body, hashlib.sha256).digest()
    provided = header_value.strip()
    # hex is case-insensitive, base64 is not — compare hex lowercased, base64 as-is.
    return (hmac.compare_digest(provided.lower(), digest.hex())
            or hmac.compare_digest(provided, base64.b64encode(digest).decode()))


@app.post("/webhooks/psp")
def psp_webhook():
    raw = request.get_data()  # RAW bytes — never request.json for signing
    if not signature_valid(raw, request.headers.get("Signature", "")):
        return {"error": "invalid signature"}, 401   # reject; reveal nothing

    event = json.loads(raw)
    payment_id, payment_state = event.get("id"), event.get("state")

    order = find_order_by_payment(payment_id, event.get("referenceId"))
    if order is None:
        # Genuinely unknown, OR this webhook overtook the create-payment response
        # that stores the id — a real race. Do not drop it: persist it as
        # unresolved and let a replay job re-apply it once the payment is linked.
        # Ack with 2xx so the PSP does not keep retrying an event safely stored.
        record_unresolved_event(payment_id, event.get("referenceId"), raw)
        app.logger.warning("webhook for unlinked payment %s — stored for replay",
                           payment_id)
        return {"status": "deferred"}, 200

    new_state = FINAL_STATE_MAPPING.get(payment_state)
    if new_state is None:
        # Non-final or unrecognized state: acknowledge, change nothing.
        app.logger.info("webhook for payment %s in state %s — ignored",
                        payment_id, payment_state)
        return {"status": "ignored"}, 200

    if not order_transition(order, new_state, payment_id=payment_id, event=event):
        return {"status": "duplicate"}, 200   # already applied: a no-op, not an error

    if new_state in FULFILMENT_STATES:
        enqueue_post_payment_work(order)      # async: emails, fulfilment, ...
    return {"status": "ok"}, 200


# --- integrate these with the merchant's persistence layer -------------------

def find_order_by_payment(payment_id, reference_id):
    raise NotImplementedError


def order_transition(order, new_state, **audit) -> bool:
    """Atomically apply the transition; return False if already applied.

    A conditional write over an explicit whitelist of source states —
    `UPDATE orders SET status = :new WHERE id = :id AND status IN (:allowed_from)`
    — returning whether a row was affected; never read-then-write. The whitelist,
    not just `status <> :new`, is what stops a late or out-of-order delivery from
    downgrading an already-final order. Book the amount/currency from the webhook
    payload, not from what you requested: for some methods the final amount differs.
    """
    raise NotImplementedError


def record_unresolved_event(payment_id, reference_id, raw_body):
    """Persist a webhook that could not be linked to an order yet.

    Store the raw body (signature already verified) plus a received timestamp,
    and have a periodic job retry these — and reconcile long-unresolved ones via
    `GET /api/v1/payments/{id}`. Redelivery is not documented, so do not count on
    the event arriving again. The replay job has two traps — a stored event that
    is stale because the payment has moved on, and two sweeps claiming the same
    row — both covered in `hardening-concurrency.md`.
    """
    raise NotImplementedError


def enqueue_post_payment_work(order):
    raise NotImplementedError
```

Concrete environment values (base URLs, portal links) live in `wl-config.md`
at the skill root.
