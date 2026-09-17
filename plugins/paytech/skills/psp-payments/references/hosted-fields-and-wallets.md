# PSP Gateway API — Checkout Integration Options, Hosted Fields SDK, Apple Pay & Google Pay

How to choose between the hosted payment page, the Hosted Fields SDK, and the
server-to-server card flow — plus the wallet (Apple Pay / Google Pay) specifics.

Concrete hosts (API base URL, SDK CDN URLs, checkout page domain) are
WL-specific — see `wl-config.md` at the skill root. Never hardcode them.

## Choosing an integration option

| Option | Card data touches merchant? | PCI burden | When to use |
|--------|------------------------------|-----------|-------------|
| **Hosted payment page** (default) | No — customer is redirected to the PSP checkout page | Lowest | Default choice. Also the only way to get the built-in Apple Pay / Google Pay buttons. |
| **Hosted Fields SDK** | No — inputs are PSP-served iframes embedded in the merchant page | Low (SDK is "designed to ensure PCI-compliance by isolating sensitive data from the merchant's site") | Merchant wants its own checkout look & feel without handling PANs. Supports new cards + saved cards (vaulted tokens). `BASIC_CARD` only. |
| **Server-to-server (StS)** — send `card` object in `POST /api/v1/payments` | Yes | Full PCI DSS. The `card` schema states: "You must be PCI DSS compliant to collect card data on your side. If you are not certified, do not add this field to your request and we will collect the data on our page." | Only when the merchant is explicitly PCI DSS certified and asks for it. |

**Rule for the AI agent: never route raw card data through the merchant
backend unless the merchant explicitly requests it and confirms PCI DSS
compliance.** Default to the hosted page; use Hosted Fields when an embedded
form is requested.

### Hosted payment page flow (standard flow)

1. `POST /api/v1/payments` (no `card` object) → response contains `redirectUrl`.
2. Redirect the customer to `redirectUrl` to complete payment on the PSP page.
3. Get the final state via webhook or `GET /api/v1/payments/{id}`.

Same flow for 3DS and non-3DS. A skip-redirect variant exists (create payment,
ignore `redirectUrl`, then `PATCH /api/v1/payments/{id}` with the customer IP;
provider instructions come back in `externalRefs`) — requires contacting PSP
support first; for `BASIC_CARD` it works only on non-3DS channels.

### Embedded checkout — the iframe `postMessage` signal

The hosted payment page can be shown in an **iframe** on the merchant page
instead of a full-page redirect. When it is, the checkout page posts a message to
the parent once the payment reaches a terminal state, so the frontend can close
or hide the iframe without waiting for the redirect. Message shape:

```text
{
  source: 'payment-checkout',
  type: 'checkout.state',
  paymentId: string | null,
  state: 'COMPLETED' | 'DECLINED' | 'CANCELLED' | 'AUTHORIZED' | 'ERROR',
  returnUrl?: string
}
```

- Sent **only** when the checkout runs inside an iframe; one message per terminal
  state.
- Delivered to the direct `window.parent`, **not** `window.top` — this matters
  when the checkout ends up nested more than one iframe deep.
- Independent of the `returnUrl` redirect (shop setting
  `resultPageRedirectTimeout`) and of the "Exit iFrame" option: it fires either
  way.

**It is a frontend UX signal, not a payment result.** The PSP's own page says so;
closing the iframe is all this event may drive. Confirm the outcome server-side
via the webhook or `GET /api/v1/payments/{id}` exactly as in the redirect flow —
see `webhooks.md`. Three traps specific to this event:

1. **Verifying `event.origin` is load-bearing, not hygiene.** The checkout posts
   with `targetOrigin: '*'`, so the message reaches *every* listener on the parent
   page, and `source`/`type` are guessable constants — any other iframe on the
   merchant page can post a byte-identical `{state:'COMPLETED'}`. The origin is
   the only thing that distinguishes the real message from a forged one. Compare
   it against the PSP checkout origin (`wl-config.md`), never against a substring.
2. **`AUTHORIZED` is "terminal" only for closing the iframe.** In the payment
   lifecycle it means funds are held and **not captured** — capture is still
   required, so it must not be shown to the customer as a completed payment
   (`payment-lifecycle.md`).
3. **`ERROR` is not a `PaymentState`.** The API enum is `CHECKOUT`, `PENDING`,
   `AUTHORIZED`, `CANCELLED`, `DECLINED`, `COMPLETED`, `AWAITING_APPROVAL` — this
   event's vocabulary is a superset. Never feed its `state` into the same mapping
   that consumes payment states without a whitelist; `paymentId` may also be
   `null`, so the message cannot always be correlated to a payment at all.

```js
window.addEventListener('message', (event) => {
  if (event.origin !== CHECKOUT_ORIGIN) return;          // exact match, always
  const data = event.data;
  if (data?.source !== 'payment-checkout') return;
  if (data?.type !== 'checkout.state') return;
  closeCheckoutIframe();                                  // UX only
  refreshOrderFromOwnBackend();   // the backend is what asks the PSP, and decides
});
```

## Hosted Fields SDK

Secure SDK that renders card inputs (`cardNumber`, `expiryDate`, `cvv`,
`cardholderName`) inside PSP-served iframes on the merchant page. Card data and
CVV never touch the merchant server — the SDK sends them directly to the PSP.

**Session prerequisites**: the payment must be created server-side first
(`POST /api/v1/payments`), and must have payment method `BASIC_CARD`, state
`CHECKOUT`, and non-zero `amount` + `currency`. Its `id` is the `paymentId`
for the SDK.

### End-to-end flow

1. **Include the SDK** on the checkout page. It is distributed via CDN only;
   the concrete script/style URLs are WL-specific (see `wl-config.md`). Per the
   docs: "Inject these URLs via your server-side config or a build-time
   variable — never hardcode production URLs in source code."

2. **Create the instance** (plain function call, no `new`):

```javascript
const sdk = HostedFieldsSDK({ paymentId: 'payment_id_required', theme: 'dark' });
// optional skeleton while session data loads (call before init())
sdk.renderLoadingSkeleton('#loading-container', 4);
```

Config: `paymentId` (string, required), `theme` (`'light' | 'dark'`, default
`'light'`; with custom `fieldStyles` + dark theme also provide
`fieldStyles.dark` overrides).

3. **Register callbacks, then init.** `onError` must be registered **before**
`init()` or init errors fall back to `console.error`. `onReady` fires when
`init()` has loaded the session.

```javascript
sdk.onError((err) => showErrorToast(err.message));
sdk.onFieldValid((info) => console.log('valid:', info.fieldType));
sdk.onReady(() => {
  document.getElementById('loading-container').style.display = 'none';
  sdk.mountField('#cardNumber',     { fieldType: 'cardNumber',     label: 'Card Number' });
  sdk.mountField('#expiryDate',     { fieldType: 'expiryDate',     label: 'Expiry Date' });
  sdk.mountField('#cvv',            { fieldType: 'cvv',            label: 'Card Security Code' });
  sdk.mountField('#cardholderName', { fieldType: 'cardholderName', label: 'Cardholder Name' });
});
sdk.init();
```

Each container `div` needs an explicit height (e.g. `3.5rem`) or the iframe
collapses. `mountField` on an already-mounted `fieldType` is a **no-op** —
`unmountField(fieldType)` first to re-mount with new options.

4. **Submit** — collects/validates the iframe fields and submits the payment:

```javascript
async function submitPayment(e) {
  e.preventDefault();
  try {
    const result = await sdk.handleSubmit({
      customerEmail: 'jane@example.com',
      billingCountryCode: 'US'
    });
    if (result?.redirectUrl) window.location.replace(result.redirectUrl);
  } catch (error) {
    console.error('Submit failed:', error); // already surfaced via onError
  }
}
```

5. **Redirect**: the `handleSubmit` response **always includes `redirectUrl`**
   — navigate the customer there to complete the flow ("e.g. 3-D Secure or
   success page"). That is the entire documented 3DS handling: no extra
   challenge API on the merchant side.

### Key API surface

| Method | Purpose |
|--------|---------|
| `HostedFieldsSDK(config)` | Create instance (`paymentId`, `theme`) |
| `.init()` | Validate paymentId, load session — required before anything else |
| `.onReady(cb)` / `.onError(cb)` / `.onFieldValid(cb)` | Lifecycle; `onFieldValid` gets `{fieldType}` |
| `.mountField(selector, {fieldType, label?, cardBrand?, fieldStyles?})` | Mount an iframe field; `fieldStyles`: `variant: 'standard'|'outlined'`, `labelPosition: 'floating'|'above'`, `fieldWrapper/inputBase/labelBase/invalid*` CSS, `dark` overrides |
| `.unmountField(type)` / `.unmountFields([types])` | Remove fields (also clears their state) |
| `.handleSubmit(additionalFields?)` | Validate + submit; resolves with `{redirectUrl, ...}` |
| `.setTheme('light'|'dark')` | Switch theme on all mounted fields |
| `.setCardBrand('visa'|'mastercard'|'amex'|…)` | Set brand for a standalone CVV field (CVV length, e.g. Amex = 4) |
| `.getSavedCards()` | `[{ id, brand, panMasked, expiryMonth, expiryYear }]` — only valid inside/after `onReady` |
| `.deleteCard(cardId)` | Delete a saved card via the PSP API; updates the local list |
| `.renderLoadingSkeleton(selector, count, height?)` | Theme-aware loading placeholders |

`handleSubmit(additionalFields)` accepted keys: `saveCard` (**string**
`'true'`/`'false'`, not boolean — tokenize & save the new card),
`selectedCardId` (pay with a saved card), `customerFirstName`,
`customerLastName`, `customerDateOfBirth`, `customerEmail`,
`customerCitizenshipCountryCode`, `customerPhone`, `customerAccountNumber`,
`customerPersonalId`, `billingCountryCode`, `billingAddressLine1/2`,
`billingCity`, `billingState`, `billingPostalCode`, `documentType`,
`documentNumber`, `pin`.

### Saved cards inside the SDK

- After `onReady`, `sdk.getSavedCards()` → render your own list (plain merchant
  HTML; SDK only supplies the data).
- **CVV is always required for saved-card payments** — mount a `cvv` field,
  call `sdk.setCardBrand(brand)` (card number field isn't mounted, so brand
  can't be auto-detected), then:

```javascript
sdk.unmountField('cvv'); // always unmount first — clears a previously typed CVV
sdk.mountField('#cvv-container', { fieldType: 'cvv', label: 'Card Security Code' });
sdk.setCardBrand(brand);
// ...
const result = await sdk.handleSubmit({ selectedCardId, billingCountryCode: 'US' });
if (result?.redirectUrl) window.location.replace(result.redirectUrl);
```

With `selectedCardId`, only the CVV field value is collected; card number,
expiry and holder name come from the saved card on the backend.
`deleteCard()` is irreversible and has no built-in confirmation UI.

### SDK errors worth handling

| Message | Cause |
|---|---|
| `Invalid or unauthorized merchant token` | `paymentId` missing/empty in config |
| `Invalid checkout state or method` | Session isn't `CHECKOUT` + `BASIC_CARD` + amount/currency set |
| `HostedFields not initialized. Call .init() first.` | Method called before `init()` resolved |
| `Container not found: <selector>` | `mountField` selector matches nothing |
| `Please fill in all required fields correctly.` | Invalid fields; SDK focuses the first invalid one |
| `Card number brand is not valid.` / `Card number country is not valid.` | Checkout-session BIN restrictions |
| `Field "<type>" failed to load` | iframe load failure (network/CSP); fallback placeholder shown |

## Apple Pay

Per the docs, Apple Pay is **fully integrated into the PSP payment page** — "no
coding or configuration on your part". Once the PSP enables the credentials,
the branded Apple Pay button appears on the hosted checkout page for card
payments. Works in iOS apps and Safari on the web.

Merchant-side certificate setup / merchant validation flow: **not documented in
the API docs** ("Once you get the credentials from our platform" is all that is
stated) — coordinate with PSP support.

**Merchant-collected Apple Pay tokens** (skip-redirect flow, requires PSP
support sign-off): the OpenAPI `PaymentRequest` has an `applePay` object —
`transactionIdentifier`, `paymentData` (`version`, `data` = encrypted payload,
`decryptedData`, `signature`, `header`), `paymentMethod`
(`displayName`, `network`, `type`), `billingContact`, `shippingContact`.
The workflows doc states merchant-collected **ApplePay tokens may be used at
all times** in that flow.

## Google Pay

Offered as a button on the PSP hosted checkout page: create the payment with
`POST /api/v1/payments`, `paymentMethod: "BASIC_CARD"`, **without card
information**, and redirect to `redirectUrl` — the customer authorizes in the
Google Pay payment sheet there.

Documented constraints:

- **Web merchants only** — Android is not supported.
- Only cards with **3DS** (mandatory in the EEA under PSD2).
- Supported CardAuthMethods: **`PAN_ONLY`** (3DS is triggered for it).
- Networks: VISA, MasterCard, JCB, American Express, Diners Club.
- Billing address not required for processing.
- The merchant does **not** send Google encrypted payment/transaction data —
  the button lives on the PSP checkout page on behalf of the merchant.
- For Google's `PaymentGatewayTokenizationSpecification`: the `gateway`
  identifier and the `gatewayMerchantId` (= shop ID in the PSP system) are
  WL-specific values — see `wl-config.md`.

**Merchant-collected Google Pay tokens** (skip-redirect flow): the OpenAPI has
a `googlePay` object (`apiVersion`, `apiVersionMinor`, `paymentMethodData` with
`info` and `tokenizationData.token` / `decryptedToken`). Per the workflows doc
only **`Cryptogram 3DS`** tokens can be used there; `PAN_ONLY` tokens usually
need a redirect (3DS or CVV collection) — use the standard hosted-page flow for
those.

## PCI note (mandatory)

- **Hosted payment page** and **Hosted Fields SDK** keep the merchant out of
  handling raw card data: with Hosted Fields, PAN/expiry/CVV are captured in
  isolated PSP iframes and "never touch your server".
- The direct `card`-object flow requires the merchant to be **PCI DSS
  certified** (explicit warning in the API schema). Do not generate code that
  posts raw card fields from the merchant backend unless the user explicitly
  requests it and confirms certification.
