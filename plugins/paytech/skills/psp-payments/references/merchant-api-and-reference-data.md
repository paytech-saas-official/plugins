# PSP — Merchant API & Reference Data

The read-mostly reporting/back-office companion API (`/merchant-api/v1`), when
a merchant integration actually needs it, and what lookup/reference data (LatAm
document types, per-country bank codes) exists and where to fetch it.

---

## 1. What the Merchant API is

Per the docs: *"Merchant API serves to provide access to data available in the
Merchant back office account."*

- Separate base path: **`/merchant-api/v1`** on the same host as the Gateway
  API (`$PSP_API_URL`, values in `wl-config.md`).
- **Read-mostly**: all endpoints are `GET` except two `POST`s that add entries
  to email/card groups (anti-fraud list management).
- It **cannot move money** — no create/capture/refund here. Payment processing
  stays on the Gateway API (`/api/v1`).

### Authentication (differs from the Gateway API)

The Merchant API spec declares **HTTP Basic auth**:

> "Use your merchant dashboard credentials (email and password) as username
> and password for Basic Authorization."

```
GET $PSP_API_URL/merchant-api/v1/payments
Authorization: Basic base64(<dashboard email>:<password>)
```

This is a *person-level* back-office credential, not the Shop API Key. Treat
it like any secret (env vars only, never logged, never in the frontend); the
Gateway API's Bearer key does **not** work here.

## 2. Endpoint groups

| Endpoint(s) | Purpose (one line) |
|-------------|--------------------|
| `GET /payments` | Search payments across the account (filter by `created.gte/lt`; default window: current day). |
| `GET /operations` | Payment operations log — filter by `completed.gte/lt`, `operation.in`, `paymentState.in`; the natural feed for reconciliation. |
| `GET /counterparties` | List counterparties. |
| `GET /shops`, `GET /shop-groups` | List the merchant's shops and shop groups. |
| `GET /terminals`, `GET /terminals/{terminalId}/balance` | List terminals; read a specific terminal's balance. |
| `GET /balances` | Merchant balances (Financial Module). |
| `GET /available-withdrawal-balances` | Balances available for withdrawals via the Gateway API. |
| `GET /bins`, `GET /bin-groups`, `GET /bin-groups/{groupId}` | Card BIN reference data and BIN groups. |
| `GET /customers` | Search customers known to the account. |
| `GET /email-groups`, `POST /email-groups/{groupId}/emails` | Email lists (e.g. black/white lists); the POST adds emails. |
| `GET /card-groups`, `POST /card-groups/{groupId}/cards` | Card lists; the POST adds cards. |
| `GET /country-groups` | Country groups used in rules. |
| `GET /connectors` | Configured connectors (routes to acquirers/providers). |
| `GET /antifraud-profiles` | Anti-fraud profile configuration. |
| `GET /currency-rates` | Currency exchange rates (paginated). |
| `GET /bank-codes`, `GET /bank-code-mappings` | Bank code reference data and mappings — source of truth for bank-transfer methods. |
| `GET /error-code-groups` | Merchant-defined error-code groupings. |

Note: two of these also exist on the Gateway API with Bearer auth —
`GET /api/v1/balances` and `GET /api/v1/available-withdrawal-balances` — so a
server that only holds the Shop API Key can still do balance checks.

### Conventions (visible in the spec)

- **Pagination**: `offset` (default 0) + `limit` (default 50) query params on
  list endpoints. Most list responses carry `hasMore` to signal further pages —
  but **not all**. Per the spec, `hasMore` is present on 18 list schemas
  (payments, operations, customers, bins/bin-groups, card-groups, email-groups,
  country-groups, bank-codes, bank-code-mappings, connectors, currency-rates,
  anti-fraud profiles, error-code-groups, shop-groups) and **absent** from five:
  `CounterpartiesListResponse`, `ShopsListResponse`, `TerminalsListResponse`,
  `BalanceListResponse`, `AvailableWithdrawalBalancesListResponse`.
  For those five, don't branch on `hasMore` — page
  until a short/empty `result` instead. (`TerminalBalanceResponse.result` is a
  single object, not a list, so it never paginates.)
- **Response envelope**: `{ "timestamp", "status", "hasMore", "result": [...] }`
  (`hasMore` omitted on the five above).
- **Time filters**: dotted operator params, e.g. `created.gte` / `created.lt`
  on payments, `completed.gte` / `completed.lt` plus `operation.in`,
  `paymentState.in` (comma-separated) on operations. `gte` is inclusive, `lt`
  exclusive — page through time windows with them.
- **Errors**: same envelope as the Gateway API (`timestamp/status/error/
  message/path`, plus `errors[]` on 400); 401/403/404 responses are defined.

## 3. Do you actually need it?

**Needed** (typical cases):
- **Reconciliation reports** — nightly job pulling `GET /operations` /
  `GET /payments` for a time window and diffing against your own ledger.
- **Balance monitoring** — alerting on `GET /balances` /
  `GET /available-withdrawal-balances` before running payouts (or use the
  Gateway `/api/v1` twins with the Shop API Key).
- **Bank codes / BINs** — fetching current reference data programmatically
  (section 4).
- Back-office tooling: dashboards over shops/terminals/customers, anti-fraud
  list upkeep (email/card groups).

**Not needed** for a plain accept-payments integration: creating payments,
webhooks, status polling, refunds and withdrawals are all Gateway API
(`/api/v1`) with the Shop API Key. Skip the Merchant API entirely unless one
of the cases above applies — it needs a second, person-level credential you'd
otherwise not have to manage.

## 4. Reference data (lookup tables)

The docs site has a "References" page ("Reference Values for API Methods")
with two large lookup sections. **This skill deliberately does not embed those
tables** — they are huge and change; treat the live docs page and the Merchant
API endpoints as the source of truth.

### Document types (for `customer.documentType`)

The document is sent as **two** fields: `customer.documentType` (enum of
per-country codes, 70 values from `AR_CDI` to `VN_TIN`, e.g. `BR_CPF`) and
`customer.documentNumber` (string, maxLength 64 — the number itself). There is
no single `documentId` field.

Per-country lists of accepted identity-document type codes, needed when a
payment method requires the customer's document (mostly LatAm — errors
`3.12/3.13/3.25/3.26` point here). Countries covered: Argentina, Bolivia,
Brazil, Chile, Colombia, Costa Rica, Ecuador, Guatemala, Honduras, Kazakhstan,
Mexico, Panama, Peru, Russia, Turkey, Uruguay. Example shape: Brazil accepts
`CPF`/`CNPJ`-style document types; look up the exact codes for the target
country on the References page before building the payment form.

### Bank codes (for bank-transfer payment methods)

Per-country bank lists (code ↔ bank name) used when a bank-transfer-type
method requires the customer's bank. ~56 countries across LatAm, Europe, Asia
and Africa (Argentina … Vietnam, South Africa). Error `3.23 Bank does not
exist or not supported` means the code you sent isn't in this list.

**Source of truth at runtime**: prefer fetching `GET /merchant-api/v1/bank-codes`
(and `/bank-code-mappings`, `/bins`) or checking the live References page over
hardcoding — bank lists change. If you must cache, refresh periodically and
fail soft to the docs page.
