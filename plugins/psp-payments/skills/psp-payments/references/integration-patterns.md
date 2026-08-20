# Integration patterns — the baseline every PSP integration needs

Purpose: the rules that must hold in **every** integration, at any traffic level, because without
them money is lost — a payment is created twice, a refund is paid out twice, or a webhook is
swallowed. Language-agnostic; the copy-adaptable code lives in `references/code-examples-java.md`,
`references/code-examples-node.md`, `references/code-examples-python.md`.

Because these rules and the SQL below are language-agnostic, they apply unchanged to a stack with no
example file of its own. What does **not** carry over is the framework-specific part — reading the
raw request body, transaction boundaries, which exception a timeout raises, what to mock — so on an
uncovered language warn the developer first and verify those four things by test, not by reading
(the procedure is in SKILL.md, step 1).

This file is the **baseline**. Multi-instance deployments, real concurrent traffic on the same
order and crash-during-POST tolerance need more machinery — that is level 2, in
`references/hardening-concurrency.md`. Do not copy level-2 machinery into a project that does not
need it: the skill's first rule is to fit into the project, not to impose an architecture.

Semantics (payment object, states, capture, error codes, signature header) are in
`payment-lifecycle.md`, `deposit-and-withdrawal.md`, `webhooks.md`, `authentication.md` and
`errors-and-troubleshooting.md` and are not restated here.

## Baseline schema

Column names are illustrative — map onto the project's existing order model instead of adding a
parallel one. What is *not* negotiable is the unique constraints and the partial index.

```sql
orders             (id, order_ref unique, status, paid_amount, paid_currency,
                    refunded_amount default 0, psp_payment_id, error_code, error_message,
                    updated_at)

psp_attempt        (id, order_id, reference_id unique, psp_payment_id, redirect_url, state,
                    created_at)          -- IN_FLIGHT | READY | FAILED
  -- one open attempt per order, FAILED deliberately left OUT so a refusal frees the order:
  create unique index psp_attempt_open on psp_attempt (order_id)
         where state in ('IN_FLIGHT', 'READY');

psp_refund_attempt (id, order_id, refund_key, reference_id unique, amount, currency,
                    psp_payment_id, state, created_at)   -- IN_FLIGHT | DONE | FAILED
  -- refund_key = the caller's key for ONE logical refund, so a retry never mints a 2nd referenceId:
  create unique index psp_refund_key on psp_refund_attempt (order_id, refund_key);

psp_webhook_event  (id, payment_id, state, received_at, processed_at null,
                    processed_reason null)     -- INBOX, not tombstone; reason: applied | duplicate
  create unique index psp_webhook_once on psp_webhook_event (payment_id, state);
  create index psp_webhook_open on psp_webhook_event (received_at) where processed_at is null;
```

## Payment state → order status

| Payment state | Order status | Applied only from |
|---|---|---|
| `COMPLETED` | `PAID` | `AWAITING_PAYMENT`, `PROCESSING`, `AUTHORIZED` |
| `AUTHORIZED` | `AUTHORIZED` — funds held, capture required, **do not fulfil** | `AWAITING_PAYMENT`, `PROCESSING` |
| `DECLINED`, `CANCELLED` | `PAYMENT_FAILED` | `AWAITING_PAYMENT`, `PROCESSING`, `AUTHORIZED` |
| `CHECKOUT`, `PENDING`, `AWAITING_APPROVAL`, anything unknown | *no change* | — |

The right-hand column is a whitelist, not a comment: it is the `WHERE status IN (...)` of the
UPDATE. That is what makes re-application and any downgrade of a final status a no-op.

The order-status names above (`PAID`, `AUTHORIZED`, `PAYMENT_FAILED`, `PROCESSING`) are
**illustrative**. Map every payment state onto one of the **project's existing order statuses**
(whatever they are named) — **do not add new statuses** to the merchant's enum, and map *all*
states, not just the happy path. If the existing enum genuinely has no home for a state you must
represent (e.g. no in-progress or no failed status), **ask the developer** which existing status to
use, or to add one — never invent one silently.

## Status polling (check state)

Webhooks are the **push** path for the final state; polling
`GET /api/v1/payments/{id}` is the **pull** path — and a real integration needs
both, because a webhook can be delayed, lost, or not configured yet. Both paths
run through the *same* conditional UPDATE above, so they are idempotent and
race-free (a webhook and a poll resolving the same payment is a no-op, not a
double-apply).

Implement polling in **two** places, not just as a timeout fallback:

- **On return.** When the customer comes back to `returnUrl`, the backend reads
  `GET /api/v1/payments/{id}` and applies the mapping. Never mark the order from
  the redirect itself — the redirect is UX, the GET is the source of truth. This
  also shows the customer the correct status immediately, without waiting on the
  webhook.
- **Reconcile the stragglers.** A periodic job picks up orders left in a
  non-final state past a short grace period (the webhook never arrived) and reads
  `GET /api/v1/payments/{id}` — or `GET /api/v1/payments?referenceId.eq=...` for
  an attempt that never got an id — to finalize them. Without this, a single
  missed webhook strands an order forever.

The lease-based, multi-instance version of the reconcile job is level 2
(`hardening-concurrency.md`); the baseline just needs a simple periodic read.

## The seven baseline rules

### 1. Claim the attempt atomically — ownership comes free with the claim

```sql
insert into psp_attempt (order_id, reference_id, state)
values (:order_id, :reference_id, 'IN_FLIGHT')
 on conflict (order_id) where state in ('IN_FLIGHT','READY') do nothing
 returning *;
```

A returned row means **this** request owns the attempt and may call the PSP. **No row means it may
not**: it reuses the owner's stored `redirect_url`, or reconciles by the owner's `reference_id`, or
gets `409` + `Retry-After`. So the reservation returns **`(attempt, owner)`**, never just the row.

*Prevents:* two payments for one order. `SELECT`-then-`INSERT` loses this race — both callers miss
the `SELECT`, both mint a `referenceId`, one `INSERT` is silently discarded, **and its caller still
POSTs**. A double-click, a frontend retry or two browser tabs are enough. Handing back an existing
`IN_FLIGHT` row without the ownership flag has the same effect on refunds: both callers pay out.

### 2. Persist `referenceId`, **commit**, *then* call the PSP

The attempt row (and, for refunds, the amount reservation) commit in their own short transaction;
the PSP call happens strictly **outside** it.

*Prevents:* losing the only handle you have on an in-flight payment. Inside a transaction, a client
timeout rolls back the very `referenceId` you need to reconcile with, so the retry mints a new one
and the customer is charged (or refunded) twice.

### 3. A timeout is an unknown outcome — reconcile, never blind-retry a POST

On timeout (and on an ambiguous `5xx`) the payment may well exist. The only way out is
`GET /api/v1/payments?referenceId.eq=<the persisted referenceId>`:

- found and non-final/`COMPLETED` → store `psp_payment_id`/`redirect_url`, attempt `READY`;
- found `DECLINED`/`CANCELLED` → attempt `FAILED`, the order is free for a new attempt;
- not found yet → answer `409` + `Retry-After` and leave the attempt claimed, so the **next** call
  reconciles again by the same `reference_id`.

A confirmed `4xx` is different: nothing was created, so mark the attempt `FAILED` immediately.

**Classify fail-safe, and verify your HTTP client actually does.** Only a real HTTP status tells you
what the PSP did. Every other ending — connect/read timeout, connection reset, server disconnect, a
proxy answering `200 text/html`, a body you cannot decode — means *the request may have been
processed*, so it must map to "unknown" and enter the reconcile path above. This is the easiest place
in the whole integration to get wrong, because the failure is invisible in tests that only stub clean
JSON: if one of those endings escapes your client as a raw exception, the attempt stays claimed with
nothing to resolve it and the order is wedged behind a permanent `409`.

Two concrete traps found by running this against a real client, not by reading it:

- catching only the "timeout" exception your framework documents. Spring's `RestClient` extracts the
  body lazily, so a read timeout arrives as a generic `RestClientException` caused by
  `SocketTimeoutException` — not the `ResourceAccessException` the `RestTemplate` idiom catches.
  `httpx.TimeoutException` is likewise just one subclass of `TransportError`; `fetch` reports resets
  as `TypeError: fetch failed`.
- parsing the body outside the guarded block. `res.json()` / `r.json()` on a non-JSON `2xx` throws an
  exception that is nobody's "timeout", and it happens *after* the request was sent.

Structure the client so the default is unknown: catch the status-bearing exception first, treat
everything else as unknown, and parse the body inside the guard.

*Prevents:* double charges. `referenceId` is **not** an idempotency key at the PSP — re-POSTing it,
or minting a fresh one for the same logical operation, creates a second payment.

### 4. Move the order with a conditional UPDATE over a whitelist

```sql
update orders set status = :next, psp_payment_id = :payment_id,
       paid_amount = coalesce(:amount, paid_amount),
       paid_currency = coalesce(:currency, paid_currency),
       error_code = :error_code, error_message = :error_message, updated_at = now()
 where (psp_payment_id = :payment_id or order_ref = :reference_id)
   and status in (:allowed_from);          -- the whitelist from the table above
```

Zero rows updated is information, not an error: the order is already past this transition. Book
`amount`/`currency` **from the payload** — decimal major units, and the final amount may differ from
the requested one (FX moves your figures into `customerAmount`/`customerCurrency`).

*Prevents:* a duplicate webhook applying a transition twice, a late `AUTHORIZED` downgrading a
`PAID` order, and the webhook-versus-poll race booking the wrong amount.

### 5. The webhook table is an INBOX, never a tombstone

```sql
insert into psp_webhook_event (payment_id, state, received_at) values (:id, :state, now())
 on conflict (payment_id, state) do update set received_at = now()
  where psp_webhook_event.processed_at is null
 returning id;
```

A row back = this delivery owns the receipt (either fresh, or an unprocessed leftover it just
claimed and row-locked, so concurrent redeliveries serialise here). No row back = already processed
= a genuine duplicate. `processed_at` is set **only after** a successful transition — or after the
UPDATE matched 0 rows *because the order was already past it* (`processed_reason = 'duplicate'`).

If no order can be found for the event yet, **commit the receipt but leave `processed_at` NULL** and
ack `200`. The webhook can beat the create-payment response.

*Prevents:* permanently losing an event. Writing the dedup row before the order is found makes the
early event suppress its own redelivery, and the order never transitions. Baseline recovery relies
on PSP redelivery plus the next `GET /api/v1/payments/{id}` read path; redelivery is **not**
documented, so if the project cannot tolerate that, add the replay job from
`hardening-concurrency.md`.

### 6. Refunds: caller-supplied key, committed attempt, owner-only POST, remainder only

A refund is a **new payment** with `parentPaymentId` — there is no `/refund` endpoint. Per logical
refund the caller supplies a `refund_key`; `(order_id, refund_key)` is unique, so a retry reuses the
same `reference_id` instead of minting a second one. In one committed transaction, before any PSP
call: insert the attempt (`ON CONFLICT DO NOTHING RETURNING`, giving `owner`) and reserve the amount

```sql
update orders set refunded_amount = refunded_amount + :amount
 where id = :order_id and status = 'PAID' and refunded_amount + :amount <= paid_amount;
```

Zero rows = the refund exceeds the remainder; reject it. In PostgreSQL's default READ COMMITTED this
single statement is self-serialising (a concurrent updater blocks on the row and re-evaluates the
condition), so the baseline needs no explicit row lock. Only the owner POSTs; a non-owner reconciles
by the stored `reference_id` or gets `409`. On timeout the attempt simply stays `IN_FLIGHT` and is
reconciled by that same `reference_id` (a dedicated `UNKNOWN` state, so a background job can resolve
it, is level 2). Settling is state-conditional
(`... where state in ('IN_FLIGHT')`), and only a **confirmed** failure gives the reserved amount
back — never a timeout.

*Prevents:* double payouts — from two concurrent refunds sharing a key, from a retry after a crash
between an accepted POST and settle, and from refunding more than was captured.

### 7. No attempt state may be a dead end

Every attempt row must have a documented way forward: `IN_FLIGHT` is reconciled by the next call,
`READY`/`DONE` is replayed from the stored result, `FAILED` is outside the partial unique index and
frees the order (or the refund key) for a new attempt. `409` + `Retry-After` is returned only
*after* reconciliation was actually attempted — never as a permanent answer.

*Prevents:* an order that can never be paid again. A timeout that leaves the row exactly as it was
POSTed makes every later call `409` forever; a confirmed failure kept inside the unique index blocks
the order for good.

## Checklist — the mistakes these patterns exist to prevent

1. **HMAC over re-serialised JSON.** Hash the raw bytes: Spring `@RequestBody byte[]`, Express
   `express.raw()` on that route, FastAPI `await request.body()`.
2. **A JSON parser mounted ahead of the webhook** (global `express.json()`, `@Body()` DTO,
   Jackson-bound `Map`/DTO) — the raw bytes are gone by the time you need them.
3. **`==` / `equals` on signatures.** Use `MessageDigest.isEqual`, `crypto.timingSafeEqual`,
   `hmac.compare_digest`, length-checked first.
4. **Assuming hex.** The encoding is undocumented: accept hex and base64, confirm against a
   sandbox webhook, then pin one with a comment.
5. **PAID on HTTP 200 from `POST /api/v1/payments`.** Creation returns `CHECKOUT`/`PENDING`; only
   `COMPLETED` means money moved, and `AUTHORIZED` still needs a capture — never fulfil on it.
6. **Trusting `returnUrl` — or the embedded checkout's `postMessage`.** Both are browser signals
   and neither is a payment result: the redirect lands the customer, the message closes the iframe.
   A verified webhook (or `GET /api/v1/payments/{id}`) is the only source of truth. The message is
   the more tempting of the two because it *looks* authoritative — it carries a `state` field, and
   it is broadcast with `targetOrigin: '*'`, so an unverified `event.origin` means anyone can send
   you a `COMPLETED` (`hosted-fields-and-wallets.md`).
7. **Booking the requested amount.** Book `amount`/`currency` from the final payload — decimal
   major units, and FX moves your figures into `customerAmount`/`customerCurrency`.
8. **Application-level-only idempotency.** Back it with the DB: unique `(payment_id, state)` plus
   `UPDATE ... WHERE status IN (allowed_from)`, so duplicates and webhook-vs-poll races are no-ops
   and final states never downgrade.
9. **Blind-retrying a POST after a timeout.** Persist `referenceId` first — and **commit** it before
   the call, outside any transaction the timeout could roll back — then reconcile via
   `GET /api/v1/payments?referenceId.eq=<the persisted one>`. Retrying, or minting a fresh
   `referenceId` for the same logical operation, double-charges or double-refunds.
10. **`SELECT` then `INSERT` as "idempotency", or a reservation that hides who won.** Both callers
    miss the `SELECT`, the loser's `INSERT` is discarded — and it still POSTs, so a double-click
    creates two payments. Use `INSERT ... ON CONFLICT DO NOTHING RETURNING` and return
    **`(attempt, owner)`**: only the owner of the newly inserted row calls the PSP; everyone else
    reuses its `redirect_url`, reconciles by its `reference_id`, or gets `409` + `Retry-After`.
    Handing back an existing `IN_FLIGHT` row without that flag double-pays refunds.
11. **An attempt state that is a dead end.** See rule 7 — reconcile the in-flight/unknown ones,
    keep `FAILED` out of the partial index.
12. **Treating the webhook dedup row as a tombstone.** If the row is written before the order is
    found, an event that arrived early suppresses its own redelivery and the order never
    transitions. Make it an inbox (`processed_at`), settled only after a successful transition.
13. **Credentials in logs or the browser.** Env vars only (`PSP_API_URL`, `PSP_API_KEY`,
    `PSP_SIGNING_KEY`), fail fast when unset; never log keys, headers, PAN, CVV or the `customer`
    block; all Gateway API calls are backend-only.
14. **Slow webhook handlers.** Verify, persist, return 200 — e-mails, fulfilment and ledger
    postings go to a queue or background task.
