# PSP Gateway API — Deposit & Withdrawal requests

Purpose: how to create DEPOSIT / WITHDRAWAL / REFUND payments via `POST /api/v1/payments`,
the redirect and skip-redirect flows, webhooks, and the traps. The concrete base URL and keys
live in `wl-config.md` at the skill root; examples use `$PSP_API_URL`.

## Prerequisites

- Active merchant account with the PSP; **Shop API Key** (API auth) and **Shop Signing Key**
  (webhook verification). Sandbox and production keys differ.
- HTTPS only; methods `POST`, `GET`, `PATCH`; JSON bodies for POST/PATCH.
- Auth header on every call: `Authorization: Bearer <Shop API Key>`.

```bash
curl -X POST "$PSP_API_URL/api/v1/payments" \
  -H "Authorization: Bearer $PSP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"paymentType":"DEPOSIT","amount":10.01,"currency":"GBP","referenceId":"order-12345"}'
```

## DEPOSIT — standard redirect flow

| # | Actor | Action |
|---|-------|--------|
| 1 | Customer browser | Customer clicks "Pay" on the merchant site. |
| 2 | Merchant backend | `POST /api/v1/payments` with `paymentType: "DEPOSIT"`, amount, currency, customer data, `returnUrl`, `webhookUrl`. Persist the returned `id` against your order. |
| 3 | PSP | Responds `200` with `state: "CHECKOUT"` and a `redirectUrl`. **Not paid yet.** |
| 4 | Merchant frontend | Redirects the customer to `redirectUrl` (the PSP checkout page). |
| 5 | Customer browser | Completes payment on the PSP checkout (card entry / 3DS / APM specifics — same flow for 3DS and non-3DS, and for merchant-side vs hosted card collection). |
| 6 | PSP | Redirects the customer back to your `returnUrl`. |
| 7 | Webhook handler | Receives POST when the payment reaches a final state (`COMPLETED`, `DECLINED`, `CANCELLED`, `AUTHORIZED`). Verify the `Signature` header, then update the order. |
| 8 | Merchant backend | (Belt-and-braces) `GET /api/v1/payments/{id}` to confirm state before marking the order `PAID`. |

Treat step 6 (customer returning to `returnUrl`) as UX only — the customer can close the tab
and never come back. Steps 7–8 are the source of truth.

## Option: skip redirect to checkout page

For cases where the merchant collects everything itself and no third-party redirect is needed
(bank transfer where you only show instructions, provider PUSH to the customer, merchant-collected
GooglePay/ApplePay). **Contact PSP support before integrating this flow.**

1. Merchant collects payment data from the customer (own checkout page).
2. `POST /api/v1/payments` with the collected data.
3. Extract `id` from the response; **ignore `redirectUrl`**.
4. `PATCH /api/v1/payments/{id}` with at least `{"customerIp": "..."}` (see payment-lifecycle.md
   for all browser fields).
5. The PSP calls the upstream provider and answers your PATCH with its result. The PATCH is
   **synchronous** — provider failure ⇒ PATCH failure.
6. Use `externalRefs` from the PATCH response to show further payment instructions to the customer.

Constraints: ApplePay tokens — always usable. GooglePay — only `Cryptogram 3DS` tokens
(`PAN Only` usually needs a redirect → use the standard flow). BASIC_CARD — non-3DS channels only.

## POST /api/v1/payments — request fields

Required by schema: **`paymentType`, `currency`** only. Everything else is optional per spec,
but specific methods/providers need more (each method has its own example set in the spec).

| Field | Req | Type | Notes |
|-------|-----|------|-------|
| `paymentType` | ✅ | enum | `DEPOSIT` \| `WITHDRAWAL` \| `REFUND`. |
| `currency` | ✅ | string | ISO 4217 for fiat, or crypto symbol (`EUR`, `BRL`, `USDT`). |
| `amount` | ➖* | number | **Decimal major units** (`11.12` = €11.12), min 0.00001, max 1e9. `0.0` allowed only for pre-auth. *Effectively required for withdrawals/refunds. |
| `referenceId` | ➖ | string ≤256 | Your reference; echoed unchanged, never sent outside the PSP. Free format (e.g. `payment_id=123;custom_ref=456`). |
| `paymentMethod` | ➖ | enum | A `PaymentMethod` value (`BASIC_CARD`, `PIX`, `BANKTRANSFER`, …). What happens when it is **omitted** is **not documented in the API docs** (plausible inference: the PSP checkout offers the available methods) — confirm with PSP support before relying on it. |
| `parentPaymentId` | ➖ | string ≤32 | REFUND: id of the original deposit. Also for recurring chains. |
| `description` | ➖ | string ≤512 | Shown to the customer; may be sent outside the PSP. |
| `customer` | ➖ | object | See below. Required in practice for most APMs and withdrawals. |
| `billingAddress` | ➖ | object | `addressLine1` ≤300, `addressLine2` ≤300, `city` ≤50, `countryCode` exactly 2, pattern `[A-Z]{2}` (ISO 3166-1 alpha-2, **uppercase**), `postalCode` ≤12, `state` ≤40. |
| `card` | ➖ | object | `cardNumber` ≤23, `cardToken` ≤32 (instead of PAN), `cardholderName` ≤128, `cardSecurityCode` 3–4, `expiryMonth` exactly 2 ("01"), `expiryYear` exactly 4 ("2030"). **PCI DSS required** to send this — otherwise omit and the PSP page collects it. |
| `googlePay` / `applePay` | ➖ | object | Wallet token payloads (see spec examples). |
| `returnUrl` | ➖ | string ≤512 | Where the customer lands after processing. Spec example shows placeholders: `https://mywebsite.com/{id}/{referenceId}/{state}/{type}`. |
| `webhookUrl` | ➖ | string ≤512 | Per-payment webhook target; **overrides** the shop-settings URL. |
| `startRecurring` | ➖ | boolean | `true` to start a recurring chain (default `false`). |
| `recurringToken` | ➖ | string | Continue a chain with the token from the initiating payment. |
| `preAuth` | ➖ | boolean | `true` = Pre-Authorization (2-phase deposit → `AUTHORIZED`, then capture/void). |
| `subscription` | ➖ | object | Subscription plan (see spec `SubscriptionRequest`). |
| `additionalParameters` | ➖ | object str→str | Provider-specific (e.g. `{"bankCode":"ABHY0065032"}`). Ask PSP support. |
| `checkoutStyle` | ➖ | string | Named custom checkout style for this payment. |

### `customer` object

Lengths below are the spec's hard limits — exceeding them fails validation (400), so enforce them
client-side.

| Field | Type | Notes |
|-------|------|-------|
| `referenceId` | string ≤128 | Your customer id (`customer_123`). Note: shorter limit than the top-level `referenceId` (≤256). |
| `firstName`, `lastName` | string ≤128 | |
| `email` | string ≤256 | `format: email`. |
| `phone` | string ≤18 | International, **no `+`**, space between dialing code and number: `"357 123123123"`. The 18-char cap includes the space. |
| `dateOfBirth` | string, exactly 10 | `2001-12-03`. |
| `citizenshipCountryCode` | string, exactly 2 | e.g. `AU`. |
| `locale` | string, **exactly 2** | Checkout display language (`ru`). `minLength` = `maxLength` = 2, so 5-char tags like `ru-RU` are rejected. |
| `documentType` / `documentNumber` | enum / string ≤64 | Government id (`BR_CPF` + CPF number for PIX). See the note under the PIX example about whether this is required. |
| `accountNumber`, `accountName`, `accountType` | string ≤256 / string ≤64 / enum | Provider-side account; used for some **withdrawals** (e.g. PIX key in `accountNumber`). `accountType`: `SAVINGS` \| `CHECKING`. |
| `bank`, `bankCode`, `bankBranch`, `bankBranchCode` | string ≤64 each | Bank routing data for bank-transfer withdrawals. |
| `routingGroup` | string ≤64 | Routing hint (e.g. `VIP`). |
| `kycStatus`, `paymentInstrumentKycStatus` | boolean | KYC flags you assert. |
| `dateOfFirstDeposit`, `depositsAmount`, `withdrawalsAmount`, `depositsCnt`, `withdrawalsCnt` | string / integer | Customer history (anti-fraud inputs), amounts in base currency. |
| `ip` | string | Customer IP. |

### WITHDRAWAL specifics

Same endpoint, `paymentType: "WITHDRAWAL"`. In the one documented example the response comes back
with `state: "PENDING"` and no `redirectUrl` — but `redirectUrl` is declared on `PaymentResult`
with **no** `paymentType` restriction, so its absence for withdrawals is not guaranteed by the
spec: don't rely on it. Destination details go in `customer`
(`accountNumber`/`bankCode`/`documentNumber`/…) or `card` (card payout). Depending on shop
configuration a withdrawal may enter `AWAITING_APPROVAL`, requiring your
`POST /payments/{id}/approve` or `/reject`. Check `GET /api/v1/balances` /
`GET /api/v1/available-withdrawal-balances` for available funds.

### REFUND

`paymentType: "REFUND"` + `parentPaymentId` (+ `amount`, `currency`). Creates a new payment
object in `state: "PENDING"` with its own lifecycle and webhooks. An `amount` below the original
is accepted in spec examples; explicit partial/multiple-refund rules are **not documented in the
API docs** — confirm with PSP support.

## Example: card deposit via PSP checkout (from spec examples)

```json
POST $PSP_API_URL/api/v1/payments
{
  "paymentType": "DEPOSIT",
  "amount": 10.01,
  "currency": "GBP",
  "customer": { "email": "jw@mail.com", "phone": "44 02072243688" },
  "billingAddress": {
    "countryCode": "GB", "city": "London",
    "addressLine1": "221b Baker St, Marylebone", "postalCode": "NW1 6XE"
  }
}
```

Response (deposit creation):

```json
{
  "timestamp": "2024-09-24T20:29:11.632+00:00",
  "status": 200,
  "result": {
    "id": "96365a23dca04a5a9bcbb0031e7b06ac",
    "paymentType": "DEPOSIT",
    "state": "CHECKOUT",
    "currency": "GBP",
    "redirectUrl": "https://gateway-domain.com/payment/96365a23dca04a5a9bcbb0031e7b06ac"
  }
}
```

APM example — PIX deposit (from spec examples; the example includes `documentType`/`documentNumber`,
but the spec marks **only** `currency` and `paymentType` as required on `PaymentRequest`, `Customer`
has no required array, and per-method requirements are **not documented**. The only indirect
evidence that PIX needs them is error code `3.25 Document type is required` — so send them, but
treat "required by the method" as an inference, not a documented contract):

```json
{
  "paymentType": "DEPOSIT",
  "paymentMethod": "PIX",
  "amount": 100,
  "currency": "BRL",
  "customer": {
    "firstName": "John", "lastName": "Doe", "email": "jd@mail.com",
    "documentType": "BR_CPF", "documentNumber": "65745728000"
  }
}
```

## Example: card withdrawal (from spec examples)

```json
POST $PSP_API_URL/api/v1/payments
{
  "paymentType": "WITHDRAWAL",
  "amount": 10.01,
  "currency": "GBP",
  "customer": { "email": "jw@mail.com", "phone": "44 02072243688" },
  "billingAddress": {
    "countryCode": "GB", "city": "London",
    "addressLine1": "221b Baker St, Marylebone", "postalCode": "NW1 6XE"
  },
  "card": {
    "cardNumber": "4000000000000002", "cardholderName": "John Watson",
    "expiryMonth": "01", "expiryYear": "2030"
  }
}
```

Response: `{ "result": { "id": "…", "paymentType": "WITHDRAWAL", "state": "PENDING", "currency": "GBP" } }`.

## Getting the final status

**Webhooks** (push): fire on `COMPLETED`, `DECLINED`, `CANCELLED`, `AUTHORIZED`. Configure the
URL in shop settings or per payment via `webhookUrl`. Each webhook carries a `Signature` header =
HMAC-SHA256 of the raw JSON body keyed with the **Shop Signing Key** — verify it before trusting
the payload. Example payload:

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

**Polling** (pull): `GET /api/v1/payments/{id}`. Also
`GET /api/v1/payments?referenceId.eq=order-12345` to find payments by your reference
(plus `created.gte/lt`, `updated.gte/lt`, `offset`, `limit` ≤1000; the list defaults to the
last 24 hours if `created.gte` is not passed).

## Idempotency

- There is **no idempotency header** and no dedicated idempotency-key parameter in the spec.
- `referenceId` is your correlation id, echoed unchanged — but the docs do **not** state that
  the PSP enforces its uniqueness or dedupes on it. (A provider-side decline `3.16 Duplicate
  Payment` exists in the error-code table, but that is an acquirer decline, not a contract.)
- Safe pattern: generate a unique `referenceId` per payment attempt; on timeout/unknown outcome
  of `POST /api/v1/payments`, **do not blind-retry** — first
  `GET /api/v1/payments?referenceId.eq=<ref>` and reuse the existing payment if one was created.

## Gotchas

- **200 ≠ paid.** Creation returns `CHECKOUT`/`PENDING`. Only `COMPLETED` (or captured
  `AUTHORIZED`) means money. Never fulfil on the customer's return to `returnUrl` alone.
- **Final amount can differ from the requested amount** for some methods (explicit caution in
  the docs). When FX applies, `amount`/`currency` = what went to the provider and
  `customerAmount`/`customerCurrency` = what you requested. Book the final values.
- **Amounts are decimal major units**, not minor units: `11.12` = €11.12, not 1112 cents.
- **Timeouts cancel payments**: an abandoned checkout ends as `CANCELLED` with
  `errorCode 1.04 Cancelled by Timeout` — handle it as a normal final state. The exact
  `redirectUrl` lifetime is not documented in the API docs.
- **Phone format** is strict: no `+`, space-separated dialing code (`"44 02072243688"`).
- **Do not send `card` unless you are PCI DSS certified** — omit it and the PSP checkout
  collects card data.
- **Webhook without `Signature` verification is an account-takeover-grade hole** — always verify
  the HMAC-SHA256 against the raw body, and remember per-request `webhookUrl` overrides shop
  settings.
- Withdrawals may stall in `AWAITING_APPROVAL` waiting for **you** — monitor for it, or payouts
  never leave.
- Sandbox limits: deposits < 10 000 000; withdrawals and refunds ≤ 10 000. Test cards, e.g.
  `4000 0000 0000 0002` (3DS, success), `4242 4242 4242 4242` (3DS, declined) — full table in
  the spec's Testing section.
