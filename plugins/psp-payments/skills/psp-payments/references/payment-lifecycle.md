# PSP Gateway API — Payment object & lifecycle

Purpose: exact field/enum reference for the `Payment` object, the full state machine, safe
merchant-order mapping, and the lifecycle endpoints (`operations`, `capture`, `void`,
`chargebacks`, `approve`, `reject`, `PATCH`). Base URL and keys live in `wl-config.md` at the
skill root; examples use `$PSP_API_URL`.

## Response envelope

Every endpoint wraps its payload:

```json
{ "timestamp": "2020-10-07T13:36:32.595+00:00", "status": 200, "result": { ... } }
```

`GET /api/v1/payments` (list) adds `hasMore: boolean` and returns `result` as an array.

## The Payment object (`result` of payment endpoints)

| Field | Type | Notes |
|-------|------|-------|
| `id` | string ≤32 | PSP payment id. Store it — all lifecycle calls key on it. |
| `referenceId` | string ≤256 | Your reference from the request, echoed unchanged. Never leaves the PSP system. |
| `created` | string | ISO 8601 `YYYY-MM-DD'T'HH24:MI:SS`, **UTC**. |
| `paymentType` | enum | `DEPOSIT` \| `WITHDRAWAL` \| `REFUND` (full enum — chargebacks are not a separate type). |
| `state` | enum | See state table below. |
| `description` | string ≤512 | Transaction description. |
| `parentPaymentId` | string ≤32 | For a REFUND: id of the original deposit. Also used for recurring chains. |
| `paymentMethod` | enum | `BASIC_CARD`, `PIX`, `BANKTRANSFER`, `APPLEPAY`, `GOOGLEPAY`, `CRYPTO`, `UPI`, `SPEI`, … (300+ values in the spec). |
| `paymentMethodDetails` | object | Card details when method is BASIC_CARD: `customerAccountNumber` (masked PAN), `cardToken`, `cardholderName`, `cardExpiryMonth` ("01"), `cardExpiryYear` ("2030"), `cardBrand`, `cardIssuingCountryCode`, `cardIssuingOrganization`. |
| `amount` | number | Amount **sent to the payment provider** — may differ from what you requested. Decimal major units (`11.12` = €11.12). Range 0.00001–1 000 000 000. |
| `currency` | string | Currency sent to the provider. ISO 4217 for fiat, or a cryptocurrency symbol. |
| `customerAmount` | number | Amount from your request — filled **only if** the request currency differs from the one sent to the provider (FX applied). |
| `customerCurrency` | string | Currency from your request — same fill rule as `customerAmount`. |
| `redirectUrl` | string ≤256 | Checkout URL to send the customer to (deposit flow). |
| `errorCode` / `errorMessage` | string | Set on failures, e.g. `4.01` / `Insufficient Funds`. See "Error codes" tag in the spec. |
| `externalId` | string | Provider-side id (e.g. `order_23733jd3u892`). |
| `externalResultCode` | string | Raw result code from the external provider (e.g. `03`). |
| `customer` | object | Echo of customer data. On single-payment responses the schema is `ResponseCustomer` = the request `Customer` **minus** `accountNumber`, `depositsAmount`, `withdrawalsAmount`, `depositsCnt`, `withdrawalsCnt` (see deposit-and-withdrawal.md for the remaining fields). |
| `billingAddress` | object | `addressLine1`/`addressLine2` (≤300), `city` (≤50), `countryCode` (ISO 3166-1 alpha-2, pattern `[A-Z]{2}`), `postalCode` (≤12), `state` (≤40). Request-side limits are listed in deposit-and-withdrawal.md. |
| `startRecurring` | boolean | This payment started a recurring chain. |
| `preAuth` | boolean | This is a Pre-Authorization (2-phase deposit). |
| `recurringToken` | string | Token to continue a recurring chain. |
| `shopName` | string | Shop this payment belongs to. |
| `terminalName` | string | Name of the provider that processed the payment. |
| `externalFeeAmount` / `externalFeeCurrency` | number / string | Provider fee — only if the provider supports it. |
| `additionalParameters` | object (string→string) | Provider-specific extras, e.g. `{"tokenContractStandard":"BEP-20","cryptoCurrency":"USDT"}`. |
| `externalRefs` | object (string→string) | Provider references, e.g. `{"txId":"11483992398383"}`. Carries payment instructions in the skip-redirect flow. |

`externalId`, `additionalParameters`, `externalRefs` appear on single-payment responses
(`PaymentResult`); the list endpoint returns the `Payment` shape without them. The `customer`
sub-object goes the *other* way: `PaymentResult.customer` is the slimmer `ResponseCustomer`
(no `accountNumber`, `depositsAmount`, `withdrawalsAmount`, `depositsCnt`, `withdrawalsCnt`),
while the list endpoint's `Payment.customer` is the **full `Customer`** schema and does carry
those five fields. So neither shape is a strict subset of the other.

## paymentType

| Value | Created how |
|-------|-------------|
| `DEPOSIT` | `POST /api/v1/payments` — pay-in. |
| `WITHDRAWAL` | `POST /api/v1/payments` — payout to customer. |
| `REFUND` | `POST /api/v1/payments` with `paymentType: "REFUND"` and `parentPaymentId` of the original deposit. **No separate refund endpoint.** |

## Payment states — the full enum

Exact enum: `CHECKOUT`, `PENDING`, `AUTHORIZED`, `CANCELLED`, `DECLINED`, `COMPLETED`, `AWAITING_APPROVAL`.

| State | Final? | Meaning |
|-------|--------|---------|
| `CHECKOUT` | no | Deposit created; customer has not completed the checkout page yet (initial state of a redirect deposit). |
| `PENDING` | no | In processing (initial state of withdrawals/refunds; deposits pass through it too). |
| `AWAITING_APPROVAL` | no | WITHDRAWAL waiting for **your** decision — call `/approve` or `/reject`. |
| `AUTHORIZED` | webhook-final* | Pre-auth (2-phase) deposit: funds held, not captured. You must `/capture` or `/void`. |
| `COMPLETED` | **yes** | Success. The only state that means "money moved". |
| `DECLINED` | **yes** | Rejected by provider/issuer/anti-fraud — see `errorCode`. |
| `CANCELLED` | **yes** | Cancelled — by customer, by timeout (`errorCode 1.04`), by `/void`, or by `/reject`. |

\* Webhooks fire when a payment reaches `COMPLETED`, `DECLINED`, `CANCELLED`, or `AUTHORIZED`.
`AUTHORIZED` is final for the *authorization* phase only — the money is not yours until capture.

Per-state prose definitions beyond the above are not documented in the API docs; the meanings
here are derived from the documented endpoint preconditions and examples.

**The enum above is the whole vocabulary of the API.** One browser-side signal uses a different,
wider one: the embedded-checkout `postMessage` event calls `COMPLETED`, `DECLINED`, `CANCELLED`,
`AUTHORIZED` and `ERROR` its "terminal states" — `ERROR` has no counterpart here, and its
"terminal" means only "the iframe may now be closed", not the `Final?` column above. Never let that
event's `state` reach code that assumes a `PaymentState` (`hosted-fields-and-wallets.md`).

### Typical transitions

```
DEPOSIT (redirect):        CHECKOUT → PENDING → COMPLETED | DECLINED | CANCELLED
DEPOSIT (pre-auth):        CHECKOUT → PENDING → AUTHORIZED → capture → COMPLETED
                                                AUTHORIZED → void    → CANCELLED
WITHDRAWAL:                PENDING → COMPLETED | DECLINED | CANCELLED
WITHDRAWAL (with review):  AWAITING_APPROVAL → approve → PENDING → …
                           AWAITING_APPROVAL → reject  → CANCELLED
REFUND:                    PENDING → COMPLETED | DECLINED | CANCELLED
```

(The state reached immediately after `capture`/`approve` is not spelled out in the docs; treat
the webhook/`GET` state as authoritative.)

## Safe order-state mapping

| Payment state | Merchant order status | Action |
|---------------|----------------------|--------|
| `CHECKOUT` | `awaiting_payment` | Customer still on checkout. Do nothing. |
| `PENDING` | `processing` | Wait for webhook / keep polling. |
| `AWAITING_APPROVAL` | `pending_review` (withdrawal) | Trigger your approval workflow. |
| `AUTHORIZED` | `authorized` / `on_hold` | Reserve stock; capture or void. **Not paid yet.** |
| `COMPLETED` | `PAID` (deposit) / `paid_out` (withdrawal) / `refunded` (refund) | The only success signal. |
| `DECLINED` | `failed` | Show `errorCode`/`errorMessage`; allow retry with a **new** payment. |
| `CANCELLED` | `cancelled` | Same — retry means a new payment. |

**Hard rule: HTTP 200 from `POST /api/v1/payments` ≠ paid.** A 200 means the payment record was
created — the body shows `state: "CHECKOUT"` (deposit) or `"PENDING"` (withdrawal/refund). Mark
an order `PAID` only when the payment reaches `COMPLETED` (or `AUTHORIZED` then captured, in the
pre-auth flow), confirmed via a **verified** webhook or `GET /api/v1/payments/{id}`.

Also: for some payment methods the **final amount may differ from the requested amount** — book
the `amount`/`currency` from the final payment object, not from your request.

## Lifecycle endpoints

All are Bearer-authenticated with the Shop API Key; `{id}` is the PSP payment `id`.

| Endpoint | Precondition (per spec) | Effect |
|----------|------------------------|--------|
| `POST /api/v1/payments/{id}/capture` | payment in `AUTHORIZED` state | Captures a pre-auth fully, or partially via body `{"amount": 11.12}` (must not exceed authorized amount; omit = full capture). |
| `POST /api/v1/payments/{id}/void` | payment in `AUTHORIZED` state | Releases the hold → payment goes to `CANCELLED`, no longer capturable. No body. |
| `POST /api/v1/payments/{id}/chargebacks` | `DEPOSIT` in `COMPLETED` state, no existing chargeback | Registers a chargeback. **The body is required** (`requestBody.required: true`) — omitting it entirely yields `400`. `RegisterChargebackRequest` itself has no required properties, so a minimal `{}` satisfies it. Fields: `description` (≤512), `created` (ISO 8601, defaults to now), `state` (`CANCELLED`\|`DECLINED`\|`COMPLETED`; omitted = open, awaiting processing), `webhookUrl` (≤512). |
| `POST /api/v1/payments/{id}/approve` | `WITHDRAWAL` in `AWAITING_APPROVAL` | Merchant approves the payout. No body. |
| `POST /api/v1/payments/{id}/reject` | `WITHDRAWAL` in `AWAITING_APPROVAL` | Rejects → `CANCELLED`. Optional body `{"rejectReason": "Suspicious activity"}` — stored as the payment's external result code. |
| `GET /api/v1/payments/{id}/operations` | — | Audit trail (below). |
| `PATCH /api/v1/payments/{id}` | skip-redirect deposits (see below) | Executes the deposit server-to-server. |

All of the above return the standard `PaymentResponse` envelope with the updated payment —
**except `GET /api/v1/payments/{id}/operations`**, which returns `OperationListResponse`: `result`
is an **array** of `PaymentOperation`, not a payment (see "Operations" below).

### Refund

Not an action endpoint — a new payment:

```json
POST $PSP_API_URL/api/v1/payments
{ "paymentType": "REFUND", "amount": 10.01, "currency": "GBP",
  "parentPaymentId": "96365a23dca04a5a9bcbb0031e7b06ac" }
```

Response: a new payment (`paymentType: "REFUND"`, `state: "PENDING"`, its own `id`,
`parentPaymentId` pointing at the deposit). `amount` may be less than the original (the
sandbox notes cap test refunds at 10 000), but explicit partial/multiple-refund rules are
not documented in the API docs.

## Operations — `GET /api/v1/payments/{id}/operations`

Returns processing steps, most recent first. Each `PaymentOperation`:

| Field | Type | Notes |
|-------|------|-------|
| `id` | integer | Operation id. |
| `operation` | enum | `CREATE_PAYMENT`, `CHECKOUT`, `CANCEL`, `CONFIRMATION`, `CASCADING`, `REDIRECT`, `CONTINUE`, `CONTINUE_PRE_PROCESSING`, `DETECT_FRAUD`, `PRE_PROCESSING`, `DEPOSIT`, `WITHDRAWAL`, `REFUND`, `CHARGEBACK`, `CHECK_STATE`, `TRIGGER_WEBHOOK`, `HANDLE_WEBHOOK`, `CAPTURE`, `VOID`, `APPROVE`, `REJECT`, `UPLOAD_FILE`, `MANUAL_UPDATE`, `MANUAL_CHECK_STATE`. |
| `started` / `completed` | string | ISO 8601 timestamps. |
| `paymentState` | enum | A `PaymentState` value associated with the operation. The spec declares it as a bare `$ref` with no description — whether it is the state *before* or *after* the operation is **not documented**; do not rely on either reading. |
| `outgoingMessages` / `incomingMessages` | string | Messages exchanged with external APIs — gold for debugging declines. |

Use it for support/debugging (e.g. "why DECLINED?" → read `incomingMessages` of the failed
operation), not for order-state decisions.

## PATCH /api/v1/payments/{id} — "Execute deposit"

Executes a deposit **without** redirecting the customer to the checkout page (skip-redirect
flow — consult PSP support before using). Body (`DepositPatchRequest`) — `customerIp` required,
rest optional browser/3DS data:

| Field | Req | Type | Notes |
|-------|-----|------|-------|
| `customerIp` | ✅ | string ≤39 | Customer IP, v4 or v6. |
| `ipCountryCode` | ➖ | string (2) | Country of that IP. |
| `customerUserAgent` | ➖ | string ≤512 | `navigator.userAgent`. |
| `browserWindowWidth` / `browserWindowHeight` | ➖ | integer | `document.body.clientWidth/Height`. |
| `browserScreenWidth` / `browserScreenHeight` | ➖ | integer | `screen.width/height`. |
| `browserScreenColorDepth` | ➖ | integer | `screen.colorDepth`. |
| `browserLanguage` | ➖ | string ≤32 | `navigator.language`. |
| `browserJavaEnabled` | ➖ | boolean | `navigator.javaEnabled()`. |
| `browserTimezoneOffset` | ➖ | integer | `new Date().getTimezoneOffset()`, minutes. |

The PATCH is **synchronous**: the PSP calls the upstream provider and answers with its result —
if the provider times out, your PATCH fails too. The response's `externalRefs` may carry payment
instructions to show the customer. It does not update arbitrary payment fields — that is its
only documented use.
