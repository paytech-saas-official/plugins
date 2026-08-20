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

## 7. Reference handler (pseudocode)

```text
POST /webhooks/psp:
    raw   = request.raw_body_bytes()
    sig   = request.header("Signature")
    mine  = HMAC_SHA256(key = env.PSP_SIGNING_KEY, data = raw)   # encoding: see §4
    if not constant_time_equal(mine, sig):
        log.warn("webhook signature mismatch")
        return 401

    event = json.parse(raw)
    order = db.find_order(psp_payment_id = event.id,
                          or_reference_id = event.referenceId)
    if order is null:
        log.warn("webhook for unknown payment", event.id, event.state)
        return 200                       # ack; nothing to update

    with db.lock(order):                 # serialize per payment
        if order.is_final():             # duplicate / lost race with polling
            return 200
        switch event.state:
            COMPLETED:  order.mark_paid(event.amount, event.currency)
            AUTHORIZED: order.mark_authorized()
            DECLINED, CANCELLED:
                        order.mark_failed(event.errorCode, event.errorMessage)
            default:    log.warn("unexpected webhook state", event.state)
        db.save(order)

    queue.enqueue(post_payment_tasks, order.id)   # heavy work async
    return 200
```

Concrete environment values (base URLs, portal links) live in `wl-config.md`
at the skill root.
