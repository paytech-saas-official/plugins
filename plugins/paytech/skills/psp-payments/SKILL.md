---
name: psp-payments
description: >
  Integrate a merchant application with the PSP payment API (deposits,
  withdrawals, refunds, captures/voids, card tokens, recurring/subscriptions,
  hosted fields, Apple Pay / Google Pay), implement webhook handling and safe
  payment-state to order-state mapping, and verify the flow against the PSP
  sandbox. Use whenever the task mentions the PSP by name, pay.tech, accepting
  payments, card deposits, payouts/withdrawals, refunds, payment webhooks,
  checkout redirect, recurringToken, Hosted Fields, or reviewing an existing
  PSP integration.
metadata:
  version: 1.10.0
  wl-api-version: "1.0.341"
  spec: openapi/gateway-api.json, openapi/merchant-api.json (OpenAPI 3.1.1)
  wl-config: wl-config.md
---

# PSP Payment Integration Skill

You are an integration guide between a merchant application and the PSP's
payment API. Your responsibility ends at the PSP's public API: never speculate
about the PSP's internal architecture or downstream providers.

All brand-specific values (PSP name, base URLs, docs links)
live in **`wl-config.md`**. Everything else here is generic. The bundled
**`openapi/*.json`** specs are the authoritative source for endpoint
structure, field names and enums — when in doubt, read the spec, don't guess.

## Which files to read

Do not read everything. Load the API references for the flow you are building
(see the table in step 3), plus:

| Always | `references/integration-patterns.md` — the correctness rules every integration needs (idempotency, state mapping, webhook inbox) |
| One language | `references/code-examples-java.md`, `-node.md` or `-python.md` — pick the one matching the project you detected in step 1, and ignore the others. These three are the **covered** languages; for anything else warn the developer first (step 1) and then adapt from the closest one — the rules and the SQL are language-agnostic, the framework idioms are not |
| Only if needed | `references/hardening-concurrency.md` — the heavier machinery (attempt state model, sweep jobs, lease-based inbox claiming). Read it when the project runs **multiple app instances**, expects **genuinely concurrent** requests on the same order, or must survive a **process crash mid-payment**. A single-instance shop with modest traffic does not need it, and adding it there is imposing architecture the merchant did not ask for. |

## The API in 60 seconds

- Two APIs: **Gateway API** (`$PSP_API_URL/api/v1`, Bearer `PSP_API_KEY`) for
  payment operations, and **Merchant API** (`/merchant-api/v1`, HTTP Basic
  with dashboard credentials) for read-mostly reporting — see
  `references/merchant-api-and-reference-data.md`.
- One universal endpoint creates money movements: `POST /api/v1/payments` with
  `paymentType`: `DEPOSIT`, `WITHDRAWAL` or `REFUND` (refund = new payment
  with `parentPaymentId`; only `paymentType` and `currency` are required).
- Default deposit flow: create payment → get `redirectUrl` → redirect the
  customer → learn the outcome **two ways, both feeding the same safe state
  mapping**: the **webhook** (push) and **status polling** `GET /payments/{id}`
  (pull — poll when the customer returns to `returnUrl`, and reconcile orders
  left non-final). The redirect itself is never proof of payment; implement both
  paths (see `integration-patterns.md` → "Status polling").
- Payment states: `CHECKOUT`, `PENDING`, `AUTHORIZED`, `AWAITING_APPROVAL`
  (non-final) → `COMPLETED`, `DECLINED`, `CANCELLED` (final). Webhooks fire on
  COMPLETED / DECLINED / CANCELLED / AUTHORIZED.
- Webhooks carry a `Signature` header: HMAC-SHA256 of the raw JSON body with
  `PSP_SIGNING_KEY`. Verify before trusting.
- Amounts are decimal major units (`11.12` = 11.12 EUR). The final amount may
  differ from the requested one (`customerAmount`/`customerCurrency` on FX).

## Integration workflow (follow in order)

### 1. Analyze the merchant project first

Detect language, framework, architecture, persistence, build tool, existing
order/payment models, controllers/routing, HTTP clients, config mechanism,
authentication/authorization, logging, test framework, error handling. The
integration must **fit into** this project — reuse its conventions and
components; do not impose a parallel architecture, a new framework, or a
different language.

Concretely, before writing anything: find the order model and its status enum
(map **all** payment states onto its **existing** values whatever they are named —
never add new statuses; if the enum can't represent a needed state, ask the
developer), find how outbound
HTTP is already done (client, timeouts, retries), find how existing controllers
are registered and secured (your webhook endpoint must be reachable *without*
the merchant's user auth), find how config/secrets are read, and find how tests
mock HTTP.

#### If the project's language is not one of the covered ones

Covered languages — the ones with reviewed, tested example code — are **Java /
Spring Boot**, **Node.js / TypeScript** and **Python / FastAPI**. If the project
is in anything else (PHP, Go, C#, Ruby, Kotlin, Rust, …), **say so before you
write code**, in your own words but with this substance:

> This project is in <language>, which this integration guide does not cover
> with tested examples. The API rules, the database schema and the payment-state
> mapping apply to any language and I will follow them. What is *not* verified
> for <language> is the framework-specific part: how to read the raw request
> body, where transaction boundaries sit, which exception a timeout produces,
> and what to mock in tests. I can proceed and will flag those spots, or you can
> ask <PSP> support to add first-class support for this language.

Point the developer at their PSP's support. Then **wait for the
developer's answer** — do not silently continue, and do not refuse either: if
they say go ahead, go ahead.

When you do proceed, these four things are on you, and reading is not enough —
prove each one:

1. **Raw body.** Find how this framework exposes the unmodified request bytes,
   and write the HMAC test **first**: a fixed body plus a known-good signature
   must verify. This is the one defect that stays silent until production.
2. **Transactions.** Establish where a transaction begins and commits in this
   stack, and prove the attempt row is committed *before* the PSP call — not
   rolled back with it.
3. **Driver support.** Confirm the DB layer can express
   `INSERT ... ON CONFLICT ... DO NOTHING RETURNING` and partial unique indexes.
   If it cannot, say so — the idempotency guarantees depend on them, and a
   silent fallback is worse than a warning.
4. **Error classification.** Determine exactly which exception each ending
   produces (connect timeout, read timeout, reset, undecodable body) and default
   everything that is not a real HTTP status to "unknown". Getting this wrong is
   what wedges orders.

Adapt the patterns from the closest covered language — the SQL is identical
across all three — and tell the developer which parts you could not verify.

### 2. Pin down the business requirement

Before writing code, establish: operation (deposit / withdrawal / refund),
payment method (card, bank transfer, wallet...), flow (redirect / hosted
fields / skip-redirect), currencies, one-off vs recurring. Take answers from
the task, the project, or the API docs; if a real choice remains (e.g. hosted
payment page vs embedded Hosted Fields), **ask the developer a concrete
question** — never invent business requirements yourself.

### 3. Pick the flow

| Requirement | Flow | Reference |
|-------------|------|-----------|
| Card / APM deposit, simplest, lowest PCI scope | Redirect to PSP checkout (`redirectUrl`) | `references/deposit-and-withdrawal.md` |
| PSP checkout shown in an iframe, not a full-page redirect | Same flow; the page posts a `checkout.state` message so the frontend can close the iframe — **UX only, verify `event.origin`** | `references/hosted-fields-and-wallets.md` |
| Card form embedded in merchant page | Hosted Fields SDK | `references/hosted-fields-and-wallets.md` |
| Merchant collects everything, no redirect possible | Skip-redirect (POST + PATCH), **requires PSP support sign-off** | `references/deposit-and-withdrawal.md` |
| Charge a saved card | `card.cardToken` in POST /payments | `references/tokens-customers-recurring.md` |
| Recurring / subscriptions | `startRecurring` + `recurringToken` / `subscription` object | `references/tokens-customers-recurring.md` |
| Auth-then-capture | `preAuth: true` → `/capture` or `/void` | `references/payment-lifecycle.md` |
| Payouts | `paymentType: WITHDRAWAL` (+ `/approve`/`/reject` if AWAITING_APPROVAL) | `references/deposit-and-withdrawal.md` |
| Apple Pay / Google Pay | via PSP checkout page (default) or direct token | `references/hosted-fields-and-wallets.md` |

### 4. Implement — non-negotiable rules

**State handling** (`references/payment-lifecycle.md`):
- `POST /payments` returning HTTP 200/201 means the payment was *created*,
  **not paid**. Never mark an order PAID on creation.
- Map states through an explicit whitelist: order becomes PAID only on
  `COMPLETED` (or your capture flow's completion). `AUTHORIZED` = funds held,
  capture still required. Unknown/non-final states change nothing.
- Learn the final state **two ways**, both through that whitelist: the webhook
  (push) and status polling (`GET /payments/{id}` on return, plus a periodic
  reconcile of orders left non-final) — a missed webhook must not strand an
  order. See `integration-patterns.md` → "Status polling".
- No browser signal moves an order — not `returnUrl`, and not the embedded
  checkout's `postMessage` (it closes the iframe, nothing more, and its `state`
  vocabulary is not `PaymentState`). Only a verified webhook or a `GET` does.

**Webhooks** (`references/webhooks.md`, template in
`assets/webhook_handler.example.py`, working code in the language file for this
project — see "Which files to read" above):
- Verify the HMAC signature against the **raw body** before parsing. This is
  the single most common way the integration breaks: frameworks that parse JSON
  into an object and re-serialize it produce different bytes and the hash never
  matches. The language file gives the raw-body recipe for your framework.
- The digest encoding (hex vs base64) is **not documented** — accept either
  until you have observed which one this PSP sends, then pin it.
- Idempotent: duplicates and webhook-vs-polling races must be no-ops.
- Handle unknown payments (log + 200), respond 2xx fast, heavy work async.
- `AUTHORIZED` is not paid: never fulfil an order on it, capture first.

**Idempotency** (rules and baseline schema in
`references/integration-patterns.md`): the API documents no idempotency key, so
all four directions must be handled explicitly. Everything below is the
**baseline** — it costs a unique index and a conditional write, and without it
money is lost even at low traffic:
- *Outbound create*: one payment attempt = one unique `referenceId`, persisted
  **before** the call. On timeout the outcome is *unknown* — reconcile via
  `GET /payments?referenceId.eq=...`, never blind-retry a POST.
- *Inbound double-submit* (double click, frontend retry): claim the single open
  attempt for an order **atomically** (`INSERT ... ON CONFLICT DO NOTHING
  RETURNING`) and let only the claim's owner call the PSP; a caller that lost
  the race reuses the stored `redirectUrl` or is told to retry. A
  `SELECT`-then-`INSERT` check loses this race and creates two payments.
- *Inbound webhook*: treat the deduplication table as an **inbox**, not a
  tombstone. Mark an event processed only *after* the state transition
  succeeded; an event that could not be linked to an order yet must stay
  unprocessed so a redelivery or a replay job can finish it. Marking receipt
  before applying loses the event permanently.
- *Refunds*: give each logical refund a caller-supplied key, persist the attempt
  **and commit it** before calling the PSP, and refund only the remainder. Only
  the caller that *created* the attempt row may POST — a second caller that
  finds an existing in-flight attempt must reconcile by the stored
  `referenceId`, never POST alongside it. On timeout mark the attempt unknown
  and reconcile by that same `referenceId`; a fresh `referenceId` for the same
  refund is a second payout.

And whatever states you give an attempt, **no state may be a dead end**: an
unknown outcome must be re-reconciled by a later call, and a confirmed failure
must free the order to start a new attempt — otherwise one timeout turns into a
permanent 409 for that order. When the project needs the full treatment (state
model with background resolution, lease-based sweeps), take it from
`references/hardening-concurrency.md`; do not invent it, and do not add it to a
project that has no concurrency to defend against.

**Credentials & security** (`references/authentication.md`):
- Config only via env/secret store: `PSP_API_URL`, `PSP_API_KEY`,
  `PSP_SIGNING_KEY` (template: `assets/env.example`). Never hardcode, commit,
  or log them; never use production creds in tests.
- All Gateway API calls are **backend-only**. Never ship the API key to the
  browser or call payment endpoints from frontend code.
- Never log or store full card numbers or CVV; never store CVV at all. Do not
  route raw card data through the merchant backend (that expands PCI scope)
  unless explicitly requested and supported (StS mode).
- Never disable TLS verification.

**Errors** (`references/errors-and-troubleshooting.md`): declines arrive as
HTTP 200 with `state: DECLINED` + `errorCode`/`externalResultCode` — surface a
user-friendly message and do not retry automatically. The API documents
retryability for **no** error code; `4.15` ("the bank has requested a retry")
merely reads as retryable, which is an inference, not part of the contract. Even
there: reconcile the original payment first, create at most one new payment, and
never loop without an explicit merchant decision. 4xx responses carry a
structured error body — fix the request, don't retry.

### 5. Tests (required, use the project's test framework)

Mock/stub the PSP API. Minimum matrix where applicable: successful payment,
declined, pending→webhook completion, redirect handling, webhook (valid /
invalid signature / duplicate / unknown payment), API timeout (outcome
unknown, no double-charge), PSP 5xx, malformed response, refund, duplicate
submit. Never call the real API (even sandbox) from unit tests.

The language file for this project has test skeletons with the standard mocking
library for its ecosystem (WireMock/MockWebServer, nock, respx).

### 6. Verify on sandbox

Sandbox first, always (`references/testing-and-sandbox.md`: test cards,
sandbox limits). Smoke-test with the bundled script:

```bash
export PSP_API_URL=<sandbox url from wl-config.md> PSP_API_KEY=... PSP_SIGNING_KEY=...
python3 scripts/psp_call.py demo-deposit             # create → expect redirectUrl
python3 scripts/psp_call.py payment <id>             # poll state
python3 scripts/psp_call.py find <referenceId>       # reconcile after a timeout
python3 scripts/verify_webhook.py verify body.raw.json "<Signature header>"
```

Capture one real sandbox webhook and run `verify_webhook.py verify` on its raw
body: it reports whether the digest is **hex or base64**, which the API does not
document. Pin the merchant's handler to whichever it is. Money-moving commands
refuse to run against a non-sandbox-looking host unless you pass
`--i-know-this-is-production`.

### 7. Hand over

Finish by telling the developer: what was implemented (files, endpoints,
state mapping), what to configure manually (credentials, webhook URL in shop
settings or `webhookUrl`, enabling payment methods / skip-redirect with PSP
support), and how to run the tests and the sandbox smoke test.

## Review mode

When asked to *review* an existing PSP integration rather than build one, audit
against the checklist below. Read the code first; do not assume a defect from
naming alone, and verify each finding against the actual control flow.

| Area | What makes it a defect |
|------|------------------------|
| Payment flow | Wrong endpoint/order of calls; `redirectUrl` ignored or cached; skip-redirect used without the PATCH |
| State mapping | Order marked paid on HTTP 200, on `PENDING`, or on `AUTHORIZED` without capture; unhandled final states |
| Webhook auth | Signature not verified; verified against re-serialized JSON instead of raw bytes; non-constant-time compare; endpoint behind user auth so the PSP can't reach it |
| Idempotency | Duplicate webhook applies a transition twice; double-submit creates a second payment; refund not bounded by the remaining amount |
| Credentials | Hardcoded/committed/logged secrets; API key reachable from the browser; production creds in tests |
| Timeouts & retries | No timeout on PSP calls; blind POST retry; timeout treated as failure instead of unknown-then-reconcile |
| Error handling | Declines (HTTP 200 + `DECLINED`) treated as transport errors or vice versa; raw provider messages shown to the customer |
| Boundary | Payment API called from frontend code; card data routed through the backend without need |
| Tests | Missing cases from the §5 matrix, or tests that hit the live API |

Report each finding as: file:line → what is wrong → concrete failure scenario
(inputs/state that produce a wrong outcome) → suggested fix. Order by severity:
money-losing or security defects first, then correctness, then robustness. If a
check passes, say so briefly rather than padding the report.

## File map

```
SKILL.md                                  <- you are here
wl-config.md                              WL-specific: PSP name, URLs
openapi/gateway-api.json                  authoritative Gateway API spec (3.1.1)
openapi/merchant-api.json                 authoritative Merchant API spec
references/
  authentication.md                       Bearer key, signing key, cred hygiene
  payment-lifecycle.md                    Payment object, states, safe mapping,
                                          capture/void/approve/chargebacks
  deposit-and-withdrawal.md               flows step-by-step, field tables,
                                          idempotency pattern, gotchas
  webhooks.md                             payload, verification, handler rules
  integration-patterns.md                 BASELINE correctness rules: idempotency,
                                          state mapping, webhook inbox, schema
  hardening-concurrency.md                LEVEL 2, only under real concurrency:
                                          attempt state model, sweeps, lease
  code-examples-java.md                   Java/Spring: client, webhook (raw body),
                                          transitions, idempotency, tests
  code-examples-node.md                   same, Node.js/TypeScript
  code-examples-python.md                 same, Python/FastAPI
  tokens-customers-recurring.md           card tokens, recurringToken, subscriptions
  hosted-fields-and-wallets.md            Hosted Fields SDK, Apple/Google Pay, PCI
  testing-and-sandbox.md                  test cards, sandbox limits, smoke test
  errors-and-troubleshooting.md           error schema, code groups, decision tree
  merchant-api-and-reference-data.md      reporting API, bank codes / document types
scripts/
  psp_call.py                             build/send sandbox calls (stdlib only)
  verify_webhook.py                       HMAC signature check (hex & base64)
assets/
  env.example                             credential template
  deposit_request.example.json            valid deposit body
  refund_request.example.json             valid refund body
  webhook_handler.example.py              idempotent webhook handler template
```

## Versioning

This skill targets WL API **1.0.341** (see frontmatter), and the bundled
`openapi/*.json` are that version's specs. If a response contradicts them, trust
the live API and tell the developer the skill is behind — do not silently invent
the new shape.
