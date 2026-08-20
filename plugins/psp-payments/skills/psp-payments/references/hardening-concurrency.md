# Hardening — concurrency, crash recovery and transaction boundaries (level 2)

Purpose: the machinery that the **baseline** in `references/integration-patterns.md` deliberately
leaves out. Everything here costs schema, background jobs and operational surface, so it is only
worth adding when the project actually faces the conditions below. Language-agnostic SQL plus short
signatures; the per-language baseline code is in `references/code-examples-java.md`,
`references/code-examples-node.md`, `references/code-examples-python.md`.

## Do you need this?

Add level 2 only if at least one of these is true for the merchant project:

- **More than one app instance** serves checkout/refund/webhook traffic (or one instance with a
  worker pool that can process two requests for the same order at the same time).
- **Real concurrent traffic on the same order** — impatient double-clicks are handled by the
  baseline claim, but a marketplace/back-office where several actors act on one order is not.
- **You must survive a crash during the PSP POST**: the process can die between an accepted POST and
  the DB write, and nobody will come back to that checkout to trigger the reconcile-on-next-call path.
- **The customer may never return** and yet the order must settle (long-tail bank transfers, payouts
  awaiting approval) — nothing will drive the inline reconcile.
- **High refund volume**, where several refunds on one order are in flight simultaneously.
- **You cannot rely on undocumented webhook redelivery** to finish an event that arrived before its
  order existed.

**If none of these apply, the baseline is enough.** A single-instance shop with one operator does
not need a lease table, a sweep job or a four-state attempt model; adding them imposes an
architecture the project never asked for and gives you background jobs to operate.

## 1. The four-state attempt model

The baseline uses `IN_FLIGHT | READY | FAILED` and reconciles `IN_FLIGHT` on the next call. That
conflates "the owner is POSTing right now" with "the outcome is unknown", which is fine when a later
request always comes, and not fine when the answer must be produced by a background job.

| State | Meaning | Way out (none is a dead end) |
|---|---|---|
| `IN_FLIGHT` | the owner is mid-POST | owner promotes it, or a sweep ages it into `UNKNOWN` |
| `UNKNOWN` | call timed out / ambiguous `5xx`; the payment may exist | reconcile by the stored `reference_id` — on every later call **and** from a sweep |
| `READY` (deposits) / `DONE` (refunds) | result stored | replay the stored `redirect_url` / `psp_payment_id` |
| `FAILED` | confirmed refusal (`4xx`, or `DECLINED`/`CANCELLED` on reconcile) | outside the partial index: the order/refund key is free again |

**`IN_FLIGHT` must never be terminal — enforce it with a catch-all, not with careful catches.**
This is the one thing to get right when adopting this model, and it is what makes it *more*
dangerous than the baseline if you get it wrong: `IN_FLIGHT` is inside the partial unique index
(so it blocks new attempts) while the sweep looks only at `UNKNOWN` (so nothing resolves it). At
baseline both roles were the same state, so an unclassified error still landed on the recovery
path; here it strands the order behind a permanent `409`.

So in the orchestrator, around the PSP call:

```text
catch <the timeout/unknown type>   -> mark UNKNOWN, reconcile
catch <status-bearing type>        -> 5xx: mark UNKNOWN, reconcile; confirmed 4xx: mark FAILED
catch <anything else at all>       -> mark UNKNOWN, then rethrow
```

The last line is not defensive padding. An exhaustive list of client failure modes does not exist:
a library upgrade changes which exception a read timeout produces, a proxy returns an undecodable
body, `promoteReady` itself trips over the database. Running this model against a real client and a
real PostgreSQL is exactly how that was found — reading it was not enough.

```sql
-- FAILED stays OUTSIDE the index; UNKNOWN joins it, because an unresolved attempt must keep
-- blocking new payments for that order until it is settled.
drop index if exists psp_attempt_open;
create unique index psp_attempt_open on psp_attempt (order_id)
       where state in ('IN_FLIGHT', 'UNKNOWN', 'READY');

alter table psp_refund_attempt   -- IN_FLIGHT | UNKNOWN | DONE | FAILED
  add constraint psp_refund_state check (state in ('IN_FLIGHT','UNKNOWN','DONE','FAILED'));
```

Transition rules, on top of the baseline flow:

```text
timeout / 5xx          -> UNKNOWN, then reconcile immediately; still nothing found -> 409 + Retry-After
confirmed 4xx          -> FAILED
reconcile finds it     -> READY/DONE
reconcile: DECLINED    -> FAILED
```

`409` is only ever returned after reconciliation was attempted. The claim loop keeps its second pass
(`insert ... on conflict do nothing` → `select active` → retry once), because the active attempt can
turn `FAILED` between the two statements.

### Refunds: an UNKNOWN attempt blocks new refunds on the order

With several refunds in flight per order, an unresolved payout must not be joined by a fresh one, or
the remainder arithmetic is guesswork. That guard spans two statements, so it needs the order row
locked — the only place the baseline's self-serialising conditional UPDATE is not sufficient:

```sql
begin;
  select psp_payment_id from orders where id = :order_id for update;    -- serialises the guards
  select 1 from psp_refund_attempt
   where order_id = :order_id and state = 'UNKNOWN' and refund_key <> :refund_key;  -- any row -> 409
  -- then the baseline claim + amount reservation, in this same transaction
commit;                                                                 -- before the PSP call
```

Settlement stays state-conditional and widens to both open states, since the owner and a reconciler
can settle the same attempt:

```sql
update psp_refund_attempt set state = :done_or_failed, psp_payment_id = :payment_id
 where id = :id and state in ('IN_FLIGHT', 'UNKNOWN');   -- 0 rows: someone already settled it
-- only if that matched 1 row AND the refund is confirmed failed:
update orders set refunded_amount = refunded_amount - :amount where id = :order_id;
```

## 2. Sweep jobs — resolving what no request will

Two jobs, both idempotent, both running outside any transaction because they do network I/O.

```text
sweepUnknownAttempts()            every ~2 min
  select attempts where state = 'UNKNOWN' order by id limit 100   -- own short transaction
  for each: resolve(attempt)      -- GET /api/v1/payments?referenceId.eq=<stored ref>
                                  -- swallow "still unknown" / "now failed": the next pass retries
```

Reconciling twice is harmless (a GET plus an idempotent promote), so this sweep needs no lease as
long as one instance runs it. The same `resolve()` the request path uses must be reachable from the
job — do not fork a second implementation.

```text
replayUnprocessed()               every ~1 min
  claim a leased batch (SQL below)
  for each: applyWebhook(GET /api/v1/payments/{payment_id})   -- the CURRENT truth, not the receipt
            if the outcome was not "no such order": close this receipt as superseded
```

The replay job re-fetches the payment rather than replaying the stored receipt: the receipt may be a
stale `AUTHORIZED` while the payment is now `COMPLETED`.

### Inbox lease with `FOR UPDATE SKIP LOCKED`

One statement claims the batch: `skip locked` keeps parallel sweeps (or two app instances) off the
same rows, and bumping `received_at` leases the claimed rows out of the next window.

```sql
with due as (
  select id from psp_webhook_event
   where processed_at is null and received_at < now() - interval '5 minutes'
   order by received_at limit 100 for update skip locked)
update psp_webhook_event e set received_at = now() from due
 where e.id = due.id
 returning e.id, e.payment_id, e.state;
```

Commit this claim before the network calls, so the row locks are not held across the PSP GETs. The
partial index `on (received_at) where processed_at is null` keeps the scan cheap.

### Closing stale receipts as `superseded`

```sql
update psp_webhook_event
   set processed_at     = coalesce(processed_at, now()),
       processed_reason = coalesce(processed_reason, 'superseded')   -- keeps an existing verdict
 where id = :id;
```

Why it is mandatory once you have a replay job: the applier settles the receipt for the state it
actually applied — a `(payment_id, 'COMPLETED')` row. The `(payment_id, 'AUTHORIZED')` row the job
started from stays `processed_at is null`, is re-selected on **every** sweep forever, and starves
newer events out of the `limit 100` window. `processed_reason` therefore has three values here:
`applied | duplicate | superseded`.

Only skip the close when the payment could not be linked to an order at all — that receipt must stay
open so a redelivery or a later sweep can still apply it.

### Test for the sweep (level 2; language-agnostic — the level-1 tests are in the code files)

The one race the baseline files cannot exercise, because it only exists with a replay job:

```text
given   an unprocessed psp_webhook_event (payment_id='pay8', state='AUTHORIZED'),
        received_at backdated past the lease window (e.g. now() - 10 minutes)
and     GET /api/v1/payments/pay8 stubbed to return state='COMPLETED', amount=10.01
when    replayUnprocessed() runs once
then    the order is PAID with the amount from the CURRENT payment, not from the receipt
and     the 'pay8'/'AUTHORIZED' row has processed_at set and processed_reason = 'superseded'
and     a second replayUnprocessed() claims 0 rows      -- nothing left to starve the batch
```

Run it against a real PostgreSQL: `ON CONFLICT`, partial indexes and `SKIP LOCKED` have no in-memory
equivalent, so a mocked or single-connection DB cannot prove any of this.

## 3. Transaction-boundary traps per language

These are not optional extras — they are the ways the code above silently stops working. Each one
turns a correct-looking snippet into no protection at all.

### Java / Spring — self-invocation defeats `@Transactional`

Spring's transaction support is a **proxy**. A call to `this.reserve(...)` from another method of the
same bean never crosses the proxy, so `@Transactional` (and `REQUIRES_NEW`) is not applied: the
work silently joins the caller's transaction, or runs with none at all. The attempt is then *not*
committed before the PSP call, which is exactly the defect rule 2 of the baseline exists to prevent.

```java
@Service                        // deliberately NOT @Transactional: this method does network I/O,
public class CheckoutService {  // and a transaction must never be open across a PSP call
  private final PspAttemptStore attempts;      // separate bean -> the proxy really applies
  private final RefundAttemptStore refunds;    // separate bean, same reason
  private final PspClient psp;
  // startCheckout(...): attempts.claim() [committed] -> psp.createDeposit() -> attempts.promote...()
}

@Service class PspAttemptStore {   record Claimed(PspAttempt attempt, boolean owner) {}
  @Transactional Claimed claim(long orderId) { /* insert ... on conflict, then select active */ }
  @Transactional void markUnknown(long id) { }
  @Transactional void markFailed(long id) { }      // outside the partial index: order is free
  @Transactional String promoteReady(long attemptId, long orderId, PaymentResult p) { return null; }
}

@Component class CheckoutSweeper {              // own bean: @Scheduled must call PROXIED collaborators
  @Scheduled(fixedDelay = 120_000) public void resolveUnknownAttempts() { /* see §2 */ }
}
@Component class WebhookReplayJob {             // same: injects the applier, never calls `this`
  @Scheduled(fixedDelay = 60_000) public void replayUnprocessed() { /* see §2 */ }
}
```

Also: repository methods that carry their own `@Transactional` (the batch-claim query in §2) exist
precisely because their caller is deliberately non-transactional. On Hibernate < 6, `INSERT ...
RETURNING` is unavailable — `save()` and catch `DataIntegrityViolationException`, then re-read the
active row; the owner/loser semantics are identical. In tests, autowire the **stores** and let them
commit; a test that calls the orchestrator's private helpers proves nothing about the wiring.

### Node / Express — middleware ordering, and one connection per transaction

`express.raw({ type: 'application/json' })` must be mounted **on the webhook route, above** any
global `express.json()`. A global JSON parser registered first consumes the stream and hands you a
parsed object; re-stringifying it reorders keys and drops whitespace, so the HMAC can never match.
Keep a runtime guard — `if (!Buffer.isBuffer(req.body)) return 500` — because the failure mode of a
later refactor is a signature mismatch on every live webhook.

```ts
app.post('/webhooks/psp', express.raw({ type: 'application/json', limit: '1mb' }), handler);
app.use(express.json());        // everything else, mounted AFTER the webhook route
// NestJS: NestFactory.create(AppModule, { rawBody: true }) + @Req() req: RawBodyRequest<Request>
//   -> req.rawBody (Buffer). Never a @Body() DTO here: Nest already consumed the stream.
```

For transactions, `pool.query` may pick a **different connection per statement**, so `begin` /
`commit` must run on one checked-out client (`const cx = await pool.connect()`, `try/finally
cx.release()`). Everything in §1 that spans statements depends on that; a `begin` issued on the pool
is not a transaction. Never `await` a PSP call between `begin` and `commit`.

### Python / FastAPI — session per request, and no open transaction over I/O

Inject the session per request (`session = Depends(get_session)`) and keep each committed unit in its
own `async with session.begin()`. Two concurrency-relevant consequences:

- A test that drives two concurrent checkouts must use a **session factory** and give each task its
  own session; sharing one `AsyncSession` across `asyncio.gather` serialises them and the race under
  test never happens (it may also corrupt the session state).
- `await psp.<call>()` must sit **outside** `async with session.begin()`. Inside, the reservation is
  rolled back by the timeout, and the connection is pinned for the whole PSP timeout — a handful of
  slow checkouts exhaust the pool.

`BackgroundTasks` run after the response, which is what you want for fulfilment — but they are
in-process: a restart loses them. Once level 2 applies, that heavy work belongs on a real queue,
driven by the same committed order state.
