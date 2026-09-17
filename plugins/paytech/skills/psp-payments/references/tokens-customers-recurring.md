# PSP Gateway API — Customers, Card Tokens, Recurring & Subscriptions

How to identify customers, reuse saved cards via tokens, and run recurring
chains / scheduled subscriptions on the PSP Gateway API.

Base URL, API keys and any concrete hosts are WL-specific — see `wl-config.md`
at the skill root. All requests: HTTPS, `Authorization: Bearer <Shop API Key>`,
JSON bodies for POST/PATCH.

## Customer object (`customer` in payment requests)

`customer.referenceId` is **the merchant's own customer id** — you assign it,
the PSP uses it to group tokens and saved cards per customer. Send the same
`referenceId` on every payment for that customer, or token lookup and the
Hosted Fields saved-cards feature won't find anything.

| Field | Constraints | Notes |
|-------|-------------|-------|
| `referenceId` | ≤128 chars | Id assigned by the Merchant. Key for all token endpoints. The body field itself only constrains `maxLength: 128` — but the token endpoints' **path** parameter is `[a-zA-Z0-9_-]{1,128}`, so a `referenceId` containing anything outside that set cannot be looked up via those path-based endpoints. Stick to that character set. |
| `email` | ≤256, email format | |
| `firstName` / `lastName` | ≤128 each | |
| `dateOfBirth` | `YYYY-MM-DD` | |
| `phone` | ≤18 | International, **no `+`**, space between country code and number: `357 123123123` |
| `citizenshipCountryCode` | 2 chars | ISO country |
| `locale` | 2 chars | Preferred display language (affects hosted page) |
| `ip` | | Customer IP. **Optional everywhere** — not required by any request. Do **not** confuse it with the top-level `customerIp` (≤39 chars) in the `PATCH /api/v1/payments/{id}` body, which is that request's only required field in the skip-redirect flow — see `deposit-and-withdrawal.md`. |
| `routingGroup` | ≤64 | Tag for routing rules, e.g. `VIP` |
| `kycStatus`, `paymentInstrumentKycStatus` | boolean | KYC flags the merchant can pass in |
| `accountNumber`, `accountName`, `bank*`, `documentType`, `documentNumber` | | Mostly for withdrawals / local methods |
| `dateOfFirstDeposit`, `depositsAmount`, `withdrawalsAmount`, `depositsCnt`, `withdrawalsCnt` | | Merchant-side stats, can help risk/routing |

## Card tokens

### How a token is created

There is **no standalone "create token" endpoint**. A card token is produced by
processing a payment:

- A `BASIC_CARD` payment **may** carry `paymentMethodDetails.cardToken` in the
  payment object (`GET /api/v1/payments/{id}`, webhooks) — the field is
  documented as "for `BASIC_CARD` payment method only" but it is **optional**,
  and the docs never state it is always populated. Treat its absence as normal:
  read it if present, never require it, and use the `card-tokens` listing as
  the source of truth for a customer's saved cards.
- Tokens are flagged by how they were saved: `savedByCustomer: true` — the
  customer explicitly ticked "save card" (e.g. Hosted Fields `saveCard: 'true'`,
  or the save option on the hosted checkout page); `false` — saved
  automatically by the platform.

### Listing tokens — two endpoints, different purposes

| Endpoint | Returns | Use for |
|----------|---------|---------|
| `GET /api/v1/customers/{customer.referenceId}/card-tokens` | `CustomerCardToken[]` — card tokens with display data | Building your own "saved cards" UI, charging saved cards server-side |
| `GET /api/v1/customers/{customerReferenceId}/terminals/{terminalId}/tokens` | `CustomerTerminalToken[]` — generic `key`/`value` pairs **scoped to one terminal** | Terminal-specific tokens, incl. wallet tokens (e.g. key `tokenType:APPLEPAY,lastDigits:1234`, value = token) |

**`card-tokens`** supports the query filter `savedByCustomer.eq`:
`true` → only customer-saved tokens, `false` → only auto-saved, omit → all.

```http
GET /api/v1/customers/customer_123/card-tokens?savedByCustomer.eq=true
Authorization: Bearer <Shop API Key>
```

```json
{
  "timestamp": "2020-10-07T13:36:32.595+00:00",
  "status": 200,
  "result": [
    {
      "id": "09386bdeae4b4b0d9bff34eab812b41d",
      "cardholderName": "John Doe",
      "panMasked": "411111***1112",
      "expiryMonth": "1",
      "expiryYear": "2030",
      "brand": "VISA",
      "updated": "2020-10-07T13:36:32.595+00:00",
      "savedByCustomer": true
    }
  ]
}
```

`id` **is the token value** — pass it as `card.cardToken`. `brand` is one of
`AMEX, DINERS, DISCOVER, JCB, MAESTRO, MASTERCARD, MIR, RUPAY, UNIONPAY, VISA,
HUMO, VERVE, TROY, UZCARD, UNKNOWN`.

The terminal-scoped variant takes `customerReferenceId` + numeric `terminalId`
path params and returns opaque `{key, value}` pairs. How the merchant learns a
`terminalId` is not documented in the API docs (the payment response carries
`terminalName`, not an id) — ask PSP support if you need this endpoint.

### Charging a saved card

The exact field is **`card.cardToken`** in `POST /api/v1/payments`
("Card token which can be used instead of full card number", ≤32 chars):

```json
{
  "paymentType": "DEPOSIT",
  "paymentMethod": "BASIC_CARD",
  "amount": 11.12,
  "currency": "EUR",
  "customer": { "referenceId": "customer_123" },
  "card": { "cardToken": "09386bdeae4b4b0d9bff34eab812b41d" },
  "returnUrl": "https://merchant.example/return"
}
```

Whether a CVV re-entry / 3DS redirect is required for a token payment created
this way is not documented in the API docs — always handle `redirectUrl` in the
response as in the standard flow. (In the Hosted Fields SDK saved-card flow the
CVV **is** always required; see `hosted-fields-and-wallets.md`.)

Do not confuse `card.cardToken` (saved card credential) with `recurringToken`
(recurring-chain credential, below) — they are different fields with different
values.

## Recurring payments (merchant-managed chain)

The merchant schedules charges itself; the PSP just links them via a token.

1. **Initial payment**: `POST /api/v1/payments` with `"startRecurring": true`
   (default `false`). Customer completes it interactively (hosted page /
   Hosted Fields).
2. The payment object returns **`recurringToken`** — "Token that can be used to
   continue the recurring chain". Store it against the customer.
3. **Subsequent MIT charge** — no customer present, no redirect:

```json
{
  "paymentType": "DEPOSIT",
  "paymentMethod": "BASIC_CARD",
  "amount": 9.99,
  "currency": "EUR",
  "recurringToken": "<token from the initial payment>",
  "parentPaymentId": "<id of the initial recurring payment>",
  "customer": { "referenceId": "customer_123" }
}
```

`parentPaymentId` is documented as "Id of initial recurring payment for
subsequent payments" (it doubles as the initial-deposit id for refunds).

## Subscriptions (PSP-managed schedule)

Instead of charging manually, let the PSP auto-bill: add a `subscription`
object to the **initial** `POST /api/v1/payments`. Per the schema it is
"Used only with `startRecurring=true`".

### Starting a subscription — `subscription` fields (SubscriptionRequest)

| Field | Req | Default / notes |
|-------|-----|-----------------|
| `frequency` | ✅ | Intervals between charges (e.g. `2` + `DAY` = every 2 days) |
| `frequencyUnit` | ➖ | `MINUTE` (testing only!), `DAY`, `WEEK`, `MONTH` |
| `amount` | ➖ | Per-cycle amount; defaults to the original payment's amount |
| `startTime` | ➖ | 1st cycle, ISO 8601; default `initialDeposit.createTime + frequency×frequencyUnit` |
| `numberOfCycles` | ➖ | Number of recurring charges; **unlimited if omitted** |
| `description` | ➖ | ≤512, shown on subsequent payments |
| `retryStrategy` | ➖ | See below. **If omitted, the subscription is CANCELLED after the first failed charge.** |

`retryStrategy`: `frequency` (✅), `numberOfCycles` (✅), `frequencyUnit`,
`amountAdjustments` — an array where the nth element is the **percentage of the
initial amount** charged on the nth retry (dunning with decreasing amounts).

```json
{
  "paymentType": "DEPOSIT",
  "paymentMethod": "BASIC_CARD",
  "amount": 99.99,
  "currency": "EUR",
  "customer": { "referenceId": "customer_123" },
  "startRecurring": true,
  "subscription": {
    "description": "Subscription to service",
    "frequency": 1,
    "frequencyUnit": "MONTH",
    "numberOfCycles": 12,
    "retryStrategy": { "frequency": 2, "frequencyUnit": "DAY", "numberOfCycles": 3 }
  },
  "returnUrl": "https://merchant.example/return"
}
```

### Managing a subscription

| Call | What it does |
|------|--------------|
| `GET /api/v1/subscriptions/{id}` | Fetch subscription (id: 32-char string) |
| `PATCH /api/v1/subscriptions/{id}` | Body `{"state": "CANCELLED"}` — **cancel is the only documented patch**; the `state` enum in the patch request contains only `CANCELLED` |

Subscription object highlights: `id`, `customerReferenceId` (from the initial
payment), `amount`, `currency`, `createTime`, `startTime`, `frequency`,
`frequencyUnit`, `requestedNumberOfCycles`, `state`, `recurringToken` ("used to
continue the recurring chain"), `retryStrategy`, and `cycles[]` — the payments
generated so far: `{sequence, type: REGULAR|RETRY, startTime, paymentId,
paymentState, amount}`.

**States:** `ACTIVE` → `CANCELLED` (via PATCH, or automatically after a failed
charge with no/exhausted retryStrategy) or `COMPLETED` (all
`requestedNumberOfCycles` done). No pause/resume is documented.

How the merchant obtains the subscription `id` after creation is **not
documented in the API docs** — the payment response schema does not include a
subscription id field. Confirm the delivery mechanism (webhook payload or
support) with the PSP.

## Gotchas

- **Token scope**: `card-tokens` are looked up per `customer.referenceId`
  (shop-level, keyed by your id); the second endpoint is additionally scoped
  per **terminal**. Keep `referenceId` stable and unique per real customer —
  reusing one id across users would leak saved cards between them.
- `subscription` without `startRecurring: true` is invalid per the schema
  description; always send both.
- `frequencyUnit: MINUTE` is for sandbox testing only.
- Omitting `numberOfCycles` = **unlimited** billing until cancelled.
- Omitting `retryStrategy` = one failed charge kills the subscription.
- `recurringToken` appears both on the payment object and on the subscription
  object; `cardToken` appears in `paymentMethodDetails` and in the card-tokens
  listing. They are separate credentials.
- Payment final states arrive via webhook (`Signature` header = HMAC-SHA256 of
  the JSON body with the Shop Signing Key) or `GET /api/v1/payments/{id}` —
  poll/verify subscription cycle payments the same way as any other payment.
