# PSP Gateway API — Authentication & Credentials

How to authenticate every API call (Bearer) and what the Signing Key is
actually for (webhook verification — **not** request signing). Getting this
split wrong is the number-one source of confusion: you never *send* a
`Signature` header; you only *verify* one.

---

## 1. The two credentials

| Credential | Where it is used | Direction |
|------------|------------------|-----------|
| **Shop API Key** | `Authorization: Bearer <key>` header on every API request you make | Merchant → PSP |
| **Shop Signing Key** | Verifying the `Signature` header on webhooks the PSP sends to you (HMAC-SHA256 of the JSON body) | PSP → Merchant |

- Both are issued per shop by the PSP.
- **Sandbox and production keys are different.** Never mix environments.
- The Signing Key is set in the **shop settings** (PSP merchant portal /
  support). Without it, webhooks cannot be verified.

Concrete base URLs and any environment-specific values live in
`wl-config.md` at the skill root — do not hardcode them.

---

## 2. Request authentication (every call)

Every API request carries the Shop API Key as a Bearer token:

```
POST $PSP_API_URL/api/v1/payments
Authorization: Bearer $PSP_API_KEY
Content-Type: application/json
```

- Applies to **all** operations in the spec (`security: [{BearerAuth: []}]`
  is declared globally): `POST/GET /api/v1/payments`,
  `GET/PATCH /api/v1/payments/{id}`, `capture`, `void`, `chargebacks`,
  `approve`, `reject`, `operations`, card-token lookups, subscriptions,
  balances.
- All requests must use **HTTPS**.
- Methods are `POST`, `GET`, or `PATCH` as specified per endpoint; request
  bodies for POST/PATCH are **JSON**.

A `401` almost always means: wrong key, wrong environment (sandbox key
against production URL or vice versa), or a missing/malformed
`Authorization` header.

---

## 3. Request signing — you don't do any

Per the OpenAPI spec, **no merchant-called endpoint requires a `Signature`
header**. Every operation under `/api/v1/*` uses `BearerAuth` only.

The `SignatureAuth` scheme exists in the spec, but it is attached solely to
the **webhook** delivery (the request the PSP makes *to your server*):

> `SignatureAuth` — "HMAC-SHA256 hash generated from JSON body using Shop
> Signing Key as a secret", delivered in the `Signature` HTTP header.

So the Signing Key is used in exactly one place: **verifying inbound
webhooks** (see `webhooks.md`). Do not add a `Signature` header to your
outbound API calls — it is not part of the contract. (How the server treats an
unexpected `Signature` header is **not documented**; do not rely on it being
ignored.)

### Signature encoding
The docs state the algorithm (HMAC-SHA256 over the JSON body, Signing Key as
secret) but the **output encoding (hex vs base64) is not documented in the
API docs**. Verify empirically against a sandbox webhook: compute both
encodings of the HMAC over the **raw request body bytes** and see which one
matches the `Signature` header, then pin that in your code with a comment.

---

## 4. Browser / backend boundary

| Operation | Where it runs |
|-----------|---------------|
| `POST /api/v1/payments` (create payment) | **Backend only** |
| `PATCH /api/v1/payments/{id}` (execute deposit / submit IP) | **Backend only** |
| `GET /api/v1/payments/{id}` (status polling) | **Backend only** |
| Capture / void / chargeback / approve / reject / operations | **Backend only** |
| Webhook endpoint (receives PSP notifications) | **Backend only** |
| Redirecting the customer to `redirectUrl` from the create-payment response | Browser (a plain redirect — no credentials involved) |

**No payment API call is ever made from the browser, and no credential ever
reaches it.** The Shop API Key and Signing Key must never appear in frontend
code, HTML, JS bundles, mobile apps, or any response you send to the
customer. If a key was ever shipped to a client, treat it as leaked and
rotate it.

Client-side involvement is limited to things that carry no credentials:
the plain redirect to `redirectUrl`, the Hosted Fields SDK (card data is
entered into **iframes hosted by the PSP** and submitted by the SDK — your
page never sees the raw values), and merchant-collected Apple Pay /
Google Pay `Cryptogram 3DS` wallet tokens, which you then send from your
**backend**. See `references/hosted-fields-and-wallets.md`.

---

## 5. Credential hygiene (rules for generated code)

- Read credentials from **environment variables only**:

  ```
  PSP_API_KEY      # Shop API Key  → Authorization: Bearer
  PSP_SIGNING_KEY  # Shop Signing Key → webhook HMAC verification
  PSP_API_URL      # Base URL for the target environment (see wl-config.md)
  ```

- **Never** hardcode keys in source, config files committed to git, Docker
  images, or CI logs. `.env` files must be gitignored.
- **Never log** the API Key or Signing Key — not even at debug level. If you
  must identify a key, log a masked form (`abcd...wxyz`).
- **Never** send either key to the frontend/browser or embed it in URLs
  (URLs end up in access logs and referrers).
- **Never use production credentials in tests.** Tests and CI run against
  the sandbox environment with sandbox keys.
- Fail fast at startup if a required variable is missing — do not fall back
  to a default or an empty string (an empty Bearer token produces confusing
  401s; an empty signing key makes every webhook verification "fail").

---

## Quick reference

```
Outbound (you → PSP):   Authorization: Bearer $PSP_API_KEY   (all endpoints)
Inbound  (PSP → you):   Signature: HMAC-SHA256(raw JSON body, $PSP_SIGNING_KEY)
                        (encoding not documented — verify with sandbox)
```
