# PSP — Errors & Troubleshooting

Two distinct failure layers, the exact error response schema, the full
gateway error-code taxonomy (`errorCode` like `4.01`), and decision trees for
401s, 400s, declines and timeouts.

---

## 1. Two failure layers — don't confuse them

| Layer | How it shows up | Where to look |
|-------|-----------------|---------------|
| **HTTP/transport error** | HTTP 400/401/404 with an error JSON body (schema below) | `status`, `message`, `errors[]` |
| **Payment-level decline** | HTTP **200** with a normal payment object whose `state` is `DECLINED` (or `CANCELLED`) | `errorCode`, `errorMessage`, `externalResultCode` on the payment |

A declined card is **not** an HTTP error. Always check `state` +
`errorCode` on 200 responses.

## 2. Error response schema (from the OpenAPI spec)

All error bodies share this envelope; `errors[]` appears on validation 400s.

```json
{
  "timestamp": "2020-10-07T13:32:19.444+00:00",   // ISO 8601
  "status": 400,                                   // HTTP status code
  "error": "Bad Request",                          // HTTP status text
  "message": "…",                                  // human-readable summary
  "path": "/api/v1/payments",                      // requested path
  "errors": [                                      // 400 only: field errors
    {
      "codes": ["…"],
      "arguments": [ { "codes": ["…"], "defaultMessage": "…" } ],
      "defaultMessage": "…",   // what is wrong
      "objectName": "…",
      "field": "…",            // which request field failed validation
      "bindingFailure": false
    }
  ]
}
```

HTTP status usage per the spec:

| Status | Meaning | Body schema |
|--------|---------|-------------|
| 200 | Success (including payments that end up `DECLINED`) | resource |
| 400 | Validation / malformed request | `BadRequestError` (with `errors[]`) |
| 401 | Missing/invalid Bearer token | `UnauthorizedError` |
| 404 | Resource not found (e.g. unknown payment id) | `NotFoundError` |
| 403 | Forbidden (Merchant API spec defines a `ForbiddenError` response) | like 401 body, `error: "Forbidden"` |

**A `403` with a non-JSON body** (an HTML page, or `error code: 1010`) is not from
the API — it is an edge/WAF (e.g. Cloudflare) rejecting the request, typically
because the HTTP client sent a default library **User-Agent**
(`Python-urllib/…`, `Java-http-client/…`). Set an explicit `User-Agent` on the
client (the example clients do). This is distinct from the API's JSON
`ForbiddenError` above.

## 3. Gateway error codes (`errorCode` on the payment)

Format `X.YY`. The first digit identifies **who/what declined**. The full list
is in the docs "Error codes" page and in the spec; grouped below with the
recommended integration behavior. `errorMessage` carries the same text;
`externalResultCode` is the raw acquirer/provider code (useful for support
tickets, not for logic).

> **Provenance of the tables below.** The docs "Error codes" page is a plain
> two-column list (code → name) only. The **"What to do" column is authored
> guidance in this skill, inferred from each code's name — it is not documented
> API behavior.** The API documents retryability for **no** code. Validate the
> handling that matters to you against sandbox behavior or PSP support.

### 1.xx — Gateway / configuration / state errors

| Code | Meaning | What to do |
|------|---------|------------|
| 1.00 | Illegal Workflow State | Don't retry as-is: you called an operation invalid for the current `state` (e.g. capture on non-AUTHORIZED). Re-read the payment first. |
| 1.01 | Not Found | Fix the id/reference; don't retry blindly. |
| 1.02 | Communication Problem | Transient — outcome may be unknown; reconcile via GET, then retry with a **new** payment if confirmed failed. |
| 1.03 | Internal Server Error | Same as 1.02 — treat as unknown outcome, reconcile first. |
| 1.04 | Cancelled by Timeout | Payment expired unfinished. Create a new payment; don't reuse the old one. |
| 1.05 | Terminal not Found | Configuration issue — contact PSP support. Not retryable. |
| 1.06 | Recurring Token not Found | Fix/refresh the stored token; re-tokenize the customer. |
| 1.07 | Payer Unaccepted | Customer/payer rejected by configuration or rules. Don't auto-retry. |
| 1.08 | Invalid Amount | Fix request (amount/limits). Don't retry unchanged. |
| 1.09 | Invalid Currency | Fix request; currency not supported for the shop/terminal. |
| 1.10 | Insufficient Balance | Merchant-side balance too low (withdrawals/refunds). Top up, then retry. |
| 1.11 | Processing Limits Reached | Retry later or contact PSP support about limits. |

### 2.00 — Customer

| Code | Meaning | What to do |
|------|---------|------------|
| 2.00 | Cancelled by Customer | Final. Show "payment cancelled"; let the user start a new payment. |

### 3.xx — Declined by acquirer / provider

Generally **not retryable unchanged**; a few are transient.

| Code | Meaning | What to do |
|------|---------|------------|
| 3.00 / 3.01 / 3.06 / 3.07 / 3.08 | Declined by Acquirer (generic / anti-fraud / card scheme / card data / business rules) | Final for this attempt; suggest another card/method. |
| 3.02 | Invalid request format or missing required parameters | **Fix your request** (fields/format), then retry. |
| 3.03 / 3.04 | Acquirer Malfunction / Acquirer Timeout | Transient on the provider side — retry later with a **new** payment; reconcile the old one first. |
| 3.05 | Acquirer Limits Reached | Retry later. |
| 3.09 | SCA required | Re-run with 3DS/strong authentication (don't bypass the challenge flow). |
| 3.10 | Unknown Error Code | Treat as final decline; escalate with `externalResultCode`. |
| 3.11 | Payer cannot pay | Final; other method. |
| 3.12 / 3.13 / 3.15 / 3.25 / 3.26 | ID document invalid / restricted / temp-restricted / required / wrong type | **Fix `customer.documentType` (enum, e.g. `AR_CDI`…`VN_TIN`) and/or `customer.documentNumber`** (string, maxLength 64 — see references data) and retry. |
| 3.14 | Declined Due to Age Requirements | Final — do not retry. |
| 3.16 | Duplicate Payment | The provider saw this payment already — **do not blind-retry**; reconcile before creating another. |
| 3.17 | Channel not active | Configuration — PSP support. |
| 3.18 | Cancelled or abandoned transaction | Final; new payment if user returns. |
| 3.19 / 3.20 / 3.22 | PAN blacklisted / not whitelisted / BIN country not allowed | Final for this card; another card. |
| 3.21 / 3.30 | Invalid phone number / Invalid Email | **Fix customer data**, retry. |
| 3.23 / 3.31 | Bank not supported / temporarily unavailable | Fix bank code (3.23) or retry later (3.31). |
| 3.24 | Requisites Unavailable | Retry later; escalate if persistent. |
| 3.27 / 3.28 | Amount lower than minimum / higher than maximum | **Fix amount**, retry. |
| 3.29 | Transaction has chargeback already | Final. |

### 4.xx — Declined by issuer (cardholder's bank)

| Code | Meaning | What to do |
|------|---------|------------|
| 4.00 / 4.02 / 4.08 / 4.09 | Declined / Do Not Honor / business rules / anti-fraud | Final for this attempt; user should contact bank or use another card. |
| 4.01 | Insufficient Funds | Show to user; they may retry after funding. |
| 4.03 / 4.13 | Invalid account / card number | User re-enters card data. |
| 4.04 | Invalid card expiration date | User fixes expiry. |
| 4.05 | Issuer Limits Reached | User retries later or with another card. |
| 4.06 | Card Lost or not active | Final for this card. |
| 4.07 | Invalid Security Code | User re-enters CVV. |
| 4.10 | Transaction not permitted to cardholder | Final for this card. |
| 4.11 | AVS failed | User fixes billing address. |
| 4.12 | Invalid email (issuer) | Fix email. |
| 4.14 | PIN attempts limit exceeded | Final for now. |
| 4.15 | The bank has requested a retry of the transaction | The docs give the name only and document no retryability. *Inference from the name:* the issuer is asking for a repeat, so a retry is reasonable — as a **new** payment, once, after reconciling the original. |

### 5.xx — 3-D Secure

| Code | Meaning | What to do |
|------|---------|------------|
| 5.00 | Declined by 3DS | Authentication failed — user retries, completing the challenge. |
| 5.01 | 3DS Timeout | User didn't finish the challenge — new attempt. |
| 5.02 | ACS malfunction | Issuer's ACS broken — retry later / other card. |

### 6.xx — Declined by internal (PSP) anti-fraud & limits

The list splits cleanly by whether the documented name carries the
`Limit exceeded:` prefix.

**Per-attribute rule checks** (no `Limit exceeded:` prefix) — `6.00–6.15`,
`6.35`, `6.36`, `6.39–6.41`, `6.44`, `6.50–6.52`:

- generic internal anti-fraud decline (`6.00`);
- blacklists — card PAN (`6.01`), email (`6.02`), phone (`6.14`), BIN (`6.39`),
  UPI VPA (`6.40`), ID document (`6.41`), IP address (`6.52`);
- country/currency restrictions — card-issuing country (`6.03`), IP country
  (`6.04`), currency (`6.05`), billing country (`6.35`), citizenship country
  (`6.36`) — plus country-**match** rules (`6.07–6.09`);
- PAN not whitelisted (`6.11`);
- card/data checks — `6.06` `Invalid Amount`, `6.10` `Payment Created Within
  Closed Period of Day` (time-of-day window), `6.12` invalid cardholder name,
  `6.13` `The same card is used by different customers`, `6.15` card brand not
  supported, `6.50` `Number of cards for customer`;
- email content checks — restricted word (`6.44`), suspicious email (`6.51`).

**Velocity / amount counters** (all named `Limit exceeded: …`) — `6.21–6.34`,
`6.37`, `6.38`, `6.42`, `6.43`, `6.45–6.49`: deposit and withdrawal counts and
amounts, decline/incomplete streaks and pending-deposit counters, keyed per
card, IP, email, customer and currency.

Watch out: `6.49` is `Limit exceeded: Number of cards for customer` but `6.50`
is `Number of cards for customer` **without** the prefix — don't classify these
codes by string-matching the name.

What to do: **never auto-retry** — these are rule-based and will decline again.
Codes named `Limit exceeded: …` may pass later once the window resets.
If legitimate traffic is blocked, the anti-fraud profile must be adjusted by
the PSP / in the back office. Show the user a generic decline message (don't
expose fraud-rule details).

### 7.xx — External anti-fraud

| Code | Meaning | What to do |
|------|---------|------------|
| 7.00 | Declined by External Anti-fraud | Final; as 6.xx. |
| 7.01 | External Anti-fraud Communication Problem | Transient — retry later. |

### 8.00 — Merchant

| Code | Meaning | What to do |
|------|---------|------------|
| 8.00 | Rejected by Merchant | You (or your back office) rejected it — e.g. `POST /payments/{id}/reject` on a withdrawal. Expected, final. |

## 4. Troubleshooting decision tree

### HTTP 401
1. `Authorization: Bearer $PSP_API_KEY` header present and well-formed?
2. **Environment mismatch** — sandbox key against production `$PSP_API_URL` or
   vice versa (the most common cause). Check `wl-config.md` values.
3. Key revoked/rotated → get the current Shop API Key from the back office.

### "Signature" problems (webhooks)
You never *send* a Signature — only verify it on inbound webhooks
(HMAC-SHA256 of the JSON body with the Shop Signing Key; see
`authentication.md`). If verification fails:
1. Wrong environment's Signing Key.
2. HMAC computed over a re-serialized body instead of the **raw request bytes**.
3. Wrong output encoding (hex vs base64 — not documented; test both once
   against a sandbox webhook and pin it).
Never mark an order paid from an unverified webhook — fall back to
`GET /api/v1/payments/{id}`.

### HTTP 400
Read `errors[].field` + `errors[].defaultMessage` — they name the exact
offending request field. Fix the request; **do not retry unchanged**. Common
causes: missing required field, wrong length/format (amounts, 2-letter country
codes, expiry `MM`/`YYYY`), invalid enum value.

### HTTP 404
Wrong payment `id` (or wrong environment — sandbox ids don't exist in
production). Verify the id you stored from the create response.

### Payment `state: DECLINED`
1. Route on `errorCode` using the tables above (fix data / other card / retry
   later / final).
2. Show the user a message based on the group (4.xx → "declined by your bank",
   3.27/3.28 → amount limits, etc.).
3. For opaque declines, include `externalResultCode` and the payment `id` when
   escalating to PSP support.

### Timeout / no response / 5xx on a write (create, capture, refund…)
**Treat the outcome as UNKNOWN — never assume failure.** The PSP may have
created/processed the payment even though you got no response.
1. Reconcile: `GET /api/v1/payments?referenceId.eq=<your referenceId>` (or
   `GET /api/v1/payments/{id}` if you already have the id).
2. Found → adopt that payment's actual `state`; do not create another.
3. Not found after reconciliation → safe to create again (same `referenceId`
   so future reconciliation still works).

## 5. Retry guidance

| Call | Safe to retry? |
|------|----------------|
| Any `GET` | Yes — read-only, always safe. |
| `POST /api/v1/payments` — create **and refund** | **Not blindly.** No idempotency key is documented in the API docs; `referenceId` is echoed back but is not documented to deduplicate. Reconcile by `referenceId.eq` first, then create anew only if missing. |
| `PATCH /payments/{id}`, `capture`, `void`, `approve`, `reject` | Operate on an existing payment: re-read the payment first; if the state already advanced, the work is done. Verify state — don't rely on the platform rejecting a stray repeat. |
| Declined payments | Retry only per the code tables above (e.g. 4.15 likely; 6.xx no). Always as a **new** payment. |

**A refund is not an operation on the parent payment.** There is no refund
endpoint under `/api/v1/payments/{id}` — the only sub-resource writes are
`capture`, `void`, `chargebacks`, `approve` and `reject`. A refund is a **new**
`POST /api/v1/payments` with `paymentType: REFUND` and `parentPaymentId`, so it
carries the **same double-submit risk as a create**: recover it with
`GET /api/v1/payments?referenceId.eq=<refund referenceId>`, not by re-reading
the parent. Give every refund its own `referenceId`.

Use exponential backoff for transient classes (1.02, 1.03, 3.03, 3.04, 3.31,
7.01, network timeouts), always preceded by reconciliation for writes.
