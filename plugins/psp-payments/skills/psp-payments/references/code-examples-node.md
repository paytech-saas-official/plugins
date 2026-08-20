# Node.js / TypeScript — PSP integration code

Rules and the failures they prevent: `references/integration-patterns.md`. When the project outgrows
the baseline (several instances, real concurrency on one order, crash-during-POST, sweep jobs, plus
the middleware-ordering and one-connection-per-transaction traps): `references/hardening-concurrency.md`.
The code below is the **baseline** level (Express; NestJS note in §2).

**Adapt, don't transplant:** reuse the project's HTTP client, ORM/query layer, logger, config and
test framework. Field names, endpoints and states are fixed by the API; everything else is yours.

## 1. PSP client

```ts
// psp/client.ts — global fetch + AbortSignal.timeout (Node >= 18); no extra dependency.
export type PaymentState = 'CHECKOUT' | 'PENDING' | 'AUTHORIZED' | 'AWAITING_APPROVAL'
                         | 'COMPLETED' | 'DECLINED' | 'CANCELLED';
export interface PaymentResult {
  id: string; state: PaymentState; referenceId?: string;
  paymentType?: 'DEPOSIT' | 'WITHDRAWAL' | 'REFUND';
  amount?: number; currency?: string; redirectUrl?: string;   // amount = decimal MAJOR units
  parentPaymentId?: string; errorCode?: string; errorMessage?: string;
}
export class PspTimeoutError extends Error {}                 // outcome UNKNOWN, not a failure
export class PspApiError extends Error {       // plain fields, NOT `readonly` ctor parameters: those
  httpStatus: number; body: unknown;           // are not erasable syntax, so type-stripping runtimes
  constructor(status: number, body: unknown) { // (`node --experimental-strip-types`) reject the file
    super(`PSP ${status}`); this.httpStatus = status; this.body = body;
  }
}
export const env = (k: string) => {
  const v = process.env[k]; if (!v) throw new Error(`${k} is not set`); return v;    // fail fast
};
const BASE = env('PSP_API_URL').replace(/\/$/, ''), KEY = env('PSP_API_KEY');
const TIMEOUT_MS = Number(process.env.PSP_TIMEOUT_MS ?? 30_000);  // tests set a few ms: a REAL abort
                                                     // then fires in ms instead of 30 s (see §5)

// Classify FAIL-SAFE: only a real HTTP status tells you what the PSP did. Every other
// outcome must become PspTimeoutError so it reaches the reconcile path — a raw rethrow
// here leaves the attempt claimed with nothing to resolve it, wedging the order.
async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { ...init, signal: AbortSignal.timeout(TIMEOUT_MS),
      headers: { authorization: `Bearer ${KEY}`, 'content-type': 'application/json',
                 'user-agent': 'psp-integration/1.0' } });  // explicit UA: some WL hosts (WAF) 403 the default
  } catch (e) {
    // TimeoutError/AbortError, but equally `TypeError: fetch failed` wrapping
    // ECONNRESET / socket hang up / DNS: the request may still have been processed.
    throw new PspTimeoutError(`PSP unreachable ${path}: ${(e as Error).name}`); // no headers in logs
  }
  let text: string;
  try { text = await res.text(); }                       // reading the body can time out too
  catch (e) { throw new PspTimeoutError(`PSP body unreadable ${path}: ${(e as Error).name}`); }
  let body: { status: number; result: T } | undefined;
  try { body = JSON.parse(text) as { status: number; result: T }; } catch { /* not JSON */ }
  if (!res.ok) throw new PspApiError(res.status, body ?? text.slice(0, 500));  // status is KNOWN
  // A proxy answering 200 text/html, or an envelope without `result`: handing `undefined` back as a
  // PaymentResult crashes the caller later (`const [found] = undefined` is a TypeError) instead of
  // entering the reconcile path, so an undecodable 2xx is UNKNOWN too.
  if (!body || body.result === undefined) throw new PspTimeoutError(
    `PSP returned ${res.status} with an undecodable body — outcome unknown`);
  return body.result;                        // declines are 200 + DECLINED, not an error here
}
const strip = <T extends object>(o: T) =>
  Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null));

export const psp = {
  createDeposit: (i: { amount: number; currency: string; referenceId: string; returnUrl: string;
                       webhookUrl: string; customer?: object; billingAddress?: object }) =>
    call<PaymentResult>('/api/v1/payments',
      { method: 'POST', body: JSON.stringify(strip({ paymentType: 'DEPOSIT', ...i })) }),
  // REFUND = a new payment with parentPaymentId; there is no /refund endpoint.
  createRefund: (i: { parentPaymentId: string; amount: number; currency: string; referenceId: string }) =>
    call<PaymentResult>('/api/v1/payments',
      { method: 'POST', body: JSON.stringify({ paymentType: 'REFUND', ...i }) }),
  getPayment: (id: string) => call<PaymentResult>(`/api/v1/payments/${encodeURIComponent(id)}`),
  findByReferenceId: (ref: string) =>                    // reconciliation lookup
    call<PaymentResult[]>(`/api/v1/payments?referenceId.eq=${encodeURIComponent(ref)}`),
};
```

## 2. Webhook route — the raw-body recipe

```ts
// psp/signature.ts
import { createHmac, timingSafeEqual } from 'node:crypto';
import { env } from './client';
const SIGNING_KEY = env('PSP_SIGNING_KEY');

export function verifySignature(raw: Buffer, header: string | undefined): boolean {
  if (!header) return false;
  const mac = createHmac('sha256', SIGNING_KEY).update(raw).digest();
  const presented = header.trim();
  // Encoding is NOT documented — accept hex OR base64, pin the winner after a sandbox webhook.
  return eq(mac.toString('hex'), presented.toLowerCase()) || eq(mac.toString('base64'), presented);
}
function eq(a: string, b: string): boolean {
  const x = Buffer.from(a), y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);   // length first: timingSafeEqual throws
}
```

```ts
// app.ts
import express from 'express';
import { verifySignature } from './psp/signature';
import { applyWebhook } from './orders/transition';
import { db } from './db';                        // the project's pg Pool
export const app = express();

// ORDER MATTERS. Mount express.raw() for THIS route before any global express.json(). A global
// app.use(express.json()) placed above consumes the stream and hands you a parsed object;
// re-stringifying it changes key order/whitespace and the HMAC can never match.
app.post('/webhooks/psp', express.raw({ type: 'application/json', limit: '1mb' }), async (req, res) => {
  const raw = req.body as Buffer;
  if (!Buffer.isBuffer(raw)) {                     // a JSON parser slipped in ahead of this route
    req.log?.error('psp webhook raw body missing — check middleware order');
    return res.status(500).json({ error: 'misconfigured' });
  }
  if (!verifySignature(raw, req.header('Signature'))) {
    req.log?.warn({ bytes: raw.length }, 'psp webhook signature mismatch');   // never log the key
    return res.status(401).json({ error: 'invalid signature' });
  }
  // Express 4 does not catch a rejected async handler: without this try the request hangs and the
  // process dies on the unhandled rejection. A 500 is also the right answer — nothing was committed,
  // so the redelivery re-applies the event instead of meeting a receipt marked processed.
  try {
    const event = JSON.parse(raw.toString('utf8'));  // parse only after verifying
    return res.status(200).json({ status: await applyWebhook(db, event) });    // 2xx, fast
  } catch (e) {
    req.log?.error({ err: e }, 'psp webhook apply failed');
    return res.status(500).json({ error: 'apply failed' });
  }
});

app.use(express.json());        // everything else, mounted AFTER the webhook route

// NestJS: const app = await NestFactory.create(AppModule, { rawBody: true }); then
//   @Post('webhooks/psp') handle(@Req() req: RawBodyRequest<Request>, @Headers('signature') s: string)
//   { const raw = req.rawBody!; }               // Buffer, unmodified
// Alternative: { bodyParser: false } + express.raw() on the webhook path only. Do NOT use a
// @Body() DTO here — ValidationPipe sees the parsed object, and Nest already consumed the stream.
```

## 3. Order state transition (idempotent, DB-guarded)

```ts
// orders/transition.ts  (node-postgres)
import type { Pool } from 'pg';
import type { PaymentResult, PaymentState } from '../psp/client';
import { enqueueFulfilment } from '../jobs/fulfilment';   // takes the client: an outbox row, not a
                                                          // broker call — see below
type OrderStatus = 'AWAITING_PAYMENT' | 'PROCESSING' | 'AUTHORIZED' | 'PAID' | 'PAYMENT_FAILED';

const STATE_MAP: Partial<Record<PaymentState, OrderStatus>> = {  // whitelist: integration-patterns.md
  COMPLETED: 'PAID', AUTHORIZED: 'AUTHORIZED', DECLINED: 'PAYMENT_FAILED', CANCELLED: 'PAYMENT_FAILED' };
const ALLOWED_FROM: Record<string, OrderStatus[]> = {
  PAID: ['AWAITING_PAYMENT', 'PROCESSING', 'AUTHORIZED'],
  AUTHORIZED: ['AWAITING_PAYMENT', 'PROCESSING'],
  PAYMENT_FAILED: ['AWAITING_PAYMENT', 'PROCESSING', 'AUTHORIZED'] };

export async function applyWebhook(db: Pool, e: PaymentResult) {
  const next = STATE_MAP[e.state];
  if (!next) return 'ignored' as const;                  // non-final / unknown: change nothing
  const cx = await db.connect();      // ONE connection: `begin` on the pool is not a transaction
  try {
    await cx.query('begin');
    // INBOX claim, not a tombstone: `do update ... where processed_at is null` takes over a receipt
    // that was recorded but never applied, so concurrent redeliveries serialise here. No row back
    // = already applied = a real duplicate.
    const claim = await cx.query<{ id: number }>(
      `insert into psp_webhook_event (payment_id, state, received_at) values ($1, $2, now())
       on conflict (payment_id, state) do update set received_at = now()
         where psp_webhook_event.processed_at is null
       returning id`, [e.id, e.state]);
    if (claim.rowCount === 0) { await cx.query('commit'); return 'duplicate' as const; }
    // Conditional UPDATE: re-application and any downgrade of a final status match 0 rows.
    // amount/currency come FROM THE PAYLOAD — the final amount may differ from the requested one.
    const upd = await cx.query<{ id: number }>(
      `update orders set status = $3, psp_payment_id = $1, updated_at = now(),
              paid_amount = coalesce($4, paid_amount), paid_currency = coalesce($5, paid_currency),
              error_code = $6, error_message = $7
        where (psp_payment_id = $1 or order_ref = $2) and status = any($8::text[])
        returning id`,
      [e.id, e.referenceId ?? null, next, e.amount ?? null, e.currency ?? null,
       e.errorCode ?? null, e.errorMessage ?? null, ALLOWED_FROM[next]]);
    if (upd.rowCount === 0) {
      const known = await cx.query(`select 1 from orders where psp_payment_id = $1 or order_ref = $2`,
                                   [e.id, e.referenceId ?? null]);
      // No order yet — the webhook can beat the create-payment response. Commit the RECEIPT but
      // leave processed_at NULL: marking it processed here loses the event forever, because the
      // redelivery would be dismissed as a duplicate and the order would never transition.
      if (known.rowCount === 0) { await cx.query('commit'); return 'unknown' as const; }  // ack 200
    }
    await cx.query(                    // settled either way: 0 rows = already past this transition
      `update psp_webhook_event set processed_at = now(), processed_reason = $2 where id = $1`,
      [claim.rows[0].id, upd.rowCount === 0 ? 'duplicate' : 'applied']);
    if (upd.rowCount === 0) { await cx.query('commit'); return 'duplicate' as const; }
    // Enqueue INSIDE the transaction, as an outbox row on `cx`: heavy work still runs async, but a
    // failure here rolls the receipt back with it, so the redelivery re-applies. Enqueued after the
    // commit instead, a broker outage would leave the order PAID, never fulfilled and the receipt
    // already processed — and `rollback` would then run on a committed transaction.
    if (next === 'PAID') await enqueueFulfilment(cx, upd.rows[0].id);
    await cx.query('commit');
    return 'applied' as const;
  } catch (err) {                                    // .catch: never mask the original error
    await cx.query('rollback').catch(() => {}); throw err;
  } finally { cx.release(); }
}
```

## 4. Creation and refund idempotency

```ts
// orders/checkout.ts
import { randomUUID } from 'node:crypto';
import type { Pool } from 'pg';
import { psp, PspTimeoutError, PspApiError } from '../psp/client';
import type { PaymentResult } from '../psp/client';

export class CheckoutInProgressError extends Error {}    // 409: the attempt state is churning
/** Reconciliation ran and is STILL inconclusive -> 409 + Retry-After. The attempt stays claimed, so
 *  the next call reconciles again: never a dead end. */
export class PaymentOutcomeUnknownError extends Error {}
export class CheckoutFailedError extends Error {}        // attempt FAILED: a NEW checkout may start
export class RefundOutcomeUnknownError extends Error {}  // 409; reconcile, never start a new refund
export class RefundFailedError extends Error {}          // refund refused: reservation given back

const ATTEMPT_COLS = `id, order_id, reference_id, state, redirect_url`;
const ACTIVE = `('IN_FLIGHT','READY')`;   // FAILED excluded: it must never block a retry
interface Attempt {
  id: number; order_id: number; reference_id: string; redirect_url: string | null;
  state: 'IN_FLIGHT' | 'READY' | 'FAILED';
}

/** Atomic get-or-create, committed BEFORE the PSP call; `owner` says whether THIS call inserted the
 *  row — only the owner may POST. */
async function claimAttempt(db: Pool, orderId: number) {
  for (let i = 0; i < 2; i++) {        // 2nd pass: the active attempt turned FAILED in between
    const claim = await db.query<Attempt>(
      `insert into psp_attempt (order_id, reference_id, state) values ($1, $2, 'IN_FLIGHT')
       on conflict (order_id) where state in ${ACTIVE} do nothing
       returning ${ATTEMPT_COLS}`, [orderId, `order-${orderId}-${randomUUID()}`]);
    if (claim.rowCount) return { attempt: claim.rows[0], owner: true };
    const active = await db.query<Attempt>(
      `select ${ATTEMPT_COLS} from psp_attempt where order_id = $1 and state in ${ACTIVE}`, [orderId]);
    if (active.rowCount) return { attempt: active.rows[0], owner: false };
  }
  throw new CheckoutInProgressError(`order ${orderId}: attempt state is churning`);
}

export async function startCheckout(db: Pool, orderId: number, amount: number, currency: string) {
  const { attempt, owner } = await claimAttempt(db, orderId);
  if (!owner) return joinAttempt(db, attempt);      // a non-owner never POSTs, never mints a ref
  try {
    return promoteReady(db, attempt, await psp.createDeposit({ amount, currency,
      referenceId: attempt.reference_id,
      returnUrl: 'https://shop.example/return/{id}/{referenceId}/{state}/{type}',
      webhookUrl: 'https://shop.example/webhooks/psp',
      customer: { referenceId: `customer_${orderId}` } }));
  } catch (err) {
    // Timeout or ambiguous 5xx: the payment may exist. Reconcile — never a second POST.
    if (err instanceof PspTimeoutError || (err instanceof PspApiError && err.httpStatus >= 500))
      return resolveAttempt(db, attempt);
    if (err instanceof PspApiError)                    // CONFIRMED 4xx refusal: frees the order
      await setAttemptState(db, attempt.id, 'FAILED');
    // Anything else is NOT a confirmed refusal (a bug, a DB error while promoting): only a real HTTP
    // status says what the PSP did, so the attempt must stay IN_FLIGHT. Marking it FAILED here would
    // free the order although the payment may exist — a second payment. No catch-all is needed
    // beyond that only because `joinAttempt` reconciles IN_FLIGHT on the next call, i.e. the same
    // recovery path; that stops being true under the level-2 model, where a sweep looks only at
    // UNKNOWN and an unclassified error must be mapped to it (hardening-concurrency.md §1).
    throw err;
  }
}

/** READY -> the stored URL. Still IN_FLIGHT -> reconcile, so a 409 always follows real progress.
 *  (One extra GET per concurrent click; the cheaper UNKNOWN split: hardening-concurrency.md §1.) */
async function joinAttempt(db: Pool, a: Attempt) {
  if (a.state === 'READY') return { redirectUrl: a.redirect_url };
  return resolveAttempt(db, a);
}

/** The only way out of an unresolved attempt: GET by the PERSISTED referenceId. */
async function resolveAttempt(db: Pool, a: Attempt) {
  const [found] = await psp.findByReferenceId(a.reference_id);
  if (!found)                                   // stays claimed: retried by the next call
    throw new PaymentOutcomeUnknownError(`${a.reference_id} unresolved; retry reconciliation`);
  if (found.state === 'DECLINED' || found.state === 'CANCELLED') {
    await setAttemptState(db, a.id, 'FAILED');            // frees the order for a NEW attempt
    throw new CheckoutFailedError(found.errorCode ?? found.state);
  }
  return promoteReady(db, a, found);
}

async function promoteReady(db: Pool, a: Attempt, p: PaymentResult) {
  await db.query(`update psp_attempt set state = 'READY', psp_payment_id = $2, redirect_url = $3
                   where id = $1`, [a.id, p.id, p.redirectUrl ?? null]);
  await db.query(`update orders set status = 'PROCESSING', psp_payment_id = $2
                   where id = $1 and status = 'AWAITING_PAYMENT'`, [a.order_id, p.id]);
  return { redirectUrl: p.redirectUrl ?? null };   // CHECKOUT, not paid; null once past checkout
}

const setAttemptState = (db: Pool, id: number, state: Attempt['state']) =>
  db.query(`update psp_attempt set state = $2 where id = $1`, [id, state]);

interface RefundAttempt {
  id: number; order_id: number; reference_id: string;
  amount: string;         // pg returns `numeric` as a STRING: pass it back to SQL, never add it in JS
  state: 'IN_FLIGHT' | 'DONE' | 'FAILED'; psp_payment_id: string | null;
}

/** Idempotent per (orderId, refundKey) — refundKey identifies ONE logical refund. ONLY the caller
 *  that INSERTED the attempt may POST: referenceId is NOT an idempotency key at the PSP, so a
 *  second POST for the same refund is a second payout. */
export async function refundOrder(db: Pool, orderId: number, refundKey: string,
                                  amount: number, currency: string) {
  const { attempt, owner, parentPaymentId } =
    await reserveRefund(db, orderId, refundKey, amount, currency);
  if (attempt.psp_payment_id) return psp.getPayment(attempt.psp_payment_id);   // DONE/FAILED replay
  if (attempt.state === 'FAILED')                    // refused with nothing created and the amount
    throw new RefundFailedError(attempt.reference_id);   // already released: a NEW refundKey may try
  if (!owner) return reconcileRefund(db, attempt);   // someone else's row: reconcile or 409
  try {
    const r = await psp.createRefund({ parentPaymentId, amount, currency,
                                       referenceId: attempt.reference_id });
    return settleRefund(db, attempt, r);
  } catch (err) {
    if (err instanceof PspApiError && err.httpStatus < 500) {
      // CONFIRMED refusal: nothing was created, so this reference will NEVER be found. Leaving the
      // row IN_FLIGHT would 409 for this refundKey forever AND keep the amount reserved, so the
      // remainder could not be refunded either — the one dead end the refund path can produce.
      await finishRefund(db, attempt, true, null);
      throw err;
    }
    return reconcileRefund(db, attempt);   // timeout, 5xx or unclassified: outcome UNKNOWN, so
  }                                        // reconcile by the COMMITTED row's own referenceId
}

/** Attempt row AND amount reservation in ONE commit, before the PSP call. Returns `owner`: a row
 *  that already existed belongs to another (or an earlier crashed) call, and its amount must NOT be
 *  reserved a second time. */
export async function reserveRefund(db: Pool, orderId: number, refundKey: string, amount: number,
                                    currency: string) {
  const cols = `id, order_id, reference_id, amount, state, psp_payment_id`;
  const cx = await db.connect();
  try {
    await cx.query('begin');
    const order = await cx.query<{ psp_payment_id: string }>(
      `select psp_payment_id from orders where id = $1`, [orderId]);
    if (order.rowCount === 0) throw new Error('unknown order');
    const claim = await cx.query<RefundAttempt>(
      `insert into psp_refund_attempt (order_id, refund_key, reference_id, amount, currency, state,
                                       created_at)
       values ($1, $2, $3, $4, $5, 'IN_FLIGHT', now())
       on conflict (order_id, refund_key) do nothing
       returning ${cols}`,
      [orderId, refundKey, `refund-${orderId}-${randomUUID()}`, amount, currency]);
    if (claim.rowCount === 0) {          // the row exists: reuse ITS referenceId, reserve nothing
      const prev = await cx.query<RefundAttempt>(
        `select ${cols} from psp_refund_attempt where order_id = $1 and refund_key = $2`,
        [orderId, refundKey]);
      await cx.query('commit');
      return { attempt: prev.rows[0], owner: false, parentPaymentId: order.rows[0].psp_payment_id };
    }
    const res = await cx.query(          // refund only the remainder
      `update orders set refunded_amount = refunded_amount + $2
        where id = $1 and status = 'PAID' and refunded_amount + $2 <= paid_amount`, [orderId, amount]);
    if (res.rowCount === 0) throw new Error('refund exceeds remaining refundable amount');
    await cx.query('commit');            // COMMITTED before the PSP call, never inside it
    return { attempt: claim.rows[0], owner: true, parentPaymentId: order.rows[0].psp_payment_id };
  } catch (err) { await cx.query('rollback').catch(() => {}); throw err; } finally { cx.release(); }
}

async function reconcileRefund(db: Pool, a: RefundAttempt) {
  const [found] = await psp.findByReferenceId(a.reference_id);  // the PERSISTED ref, never a new one
  if (!found) throw new RefundOutcomeUnknownError(`refund ${a.reference_id} unresolved; retry later`);
  return settleRefund(db, a, found);
}

/** ONE connection, ONE transaction: settling and releasing the reservation on the pool would be two
 *  statements on two connections, so a crash in between leaves the amount reserved forever. */
async function finishRefund(db: Pool, a: RefundAttempt, failed: boolean, paymentId: string | null) {
  const cx = await db.connect();
  try {
    await cx.query('begin');
    // State-conditional: the owner and a reconciler can settle the same attempt, and releasing the
    // reservation twice would inflate the refundable amount.
    const done = await cx.query(
      `update psp_refund_attempt set state = $2, psp_payment_id = coalesce($3, psp_payment_id)
        where id = $1 and state = 'IN_FLIGHT'`, [a.id, failed ? 'FAILED' : 'DONE', paymentId]);
    if (failed && done.rowCount) await cx.query(   // only a CONFIRMED failure gives the amount back
      `update orders set refunded_amount = refunded_amount - $2 where id = $1`,
      [a.order_id, a.amount]);
    await cx.query('commit');
  } catch (err) { await cx.query('rollback').catch(() => {}); throw err; } finally { cx.release(); }
}

async function settleRefund(db: Pool, a: RefundAttempt, r: PaymentResult) {
  await finishRefund(db, a, r.state === 'DECLINED' || r.state === 'CANCELLED', r.id);
  return r;
}
```

## 5. Tests (vitest + nock + supertest)

```ts
import { beforeEach, expect, it } from 'vitest';
import nock from 'nock';       // >= 14: earlier versions intercept only http(s), never global fetch
import request from 'supertest';
import { createHmac } from 'node:crypto';
import { app } from '../src/app';
import { psp, PspTimeoutError, PspApiError } from '../src/psp/client';
import { startCheckout, refundOrder, reserveRefund, PaymentOutcomeUnknownError,
         RefundOutcomeUnknownError } from '../src/orders/checkout';
// db = a REAL PostgreSQL (Testcontainers; pg-mem won't do): the guarantees rest on ON CONFLICT,
// partial indexes and committed transactions, which a mocked or single-connection DB cannot prove.
// `db` and the small read helpers below (orderStatus, activeAttempt, refundedAmount, …) are the
// project's own; the test env also sets PSP_TIMEOUT_MS=100, so an abort fires well inside vitest's
// 5 s default testTimeout — a 31 s delay would fail the test instead of the client.

const BASE = process.env.PSP_API_URL!;        // sandbox values from the test env, never production
const PAY = '/api/v1/payments';
const BODY = JSON.stringify({ id: 'pay1', referenceId: 'order-1-a', state: 'COMPLETED',
                              amount: 10.01, currency: 'GBP' });
const sign = (b: string, enc: 'hex' | 'base64' = 'hex') =>
  createHmac('sha256', process.env.PSP_SIGNING_KEY!).update(b).digest(enc);
const send = (body: string, sig = sign(body)) => request(app).post('/webhooks/psp')
  .set('Signature', sig).set('content-type', 'application/json').send(body);
const found = (...results: object[]) => ({ status: 200, result: results });
const CHECKOUT_1 = { id: 'pay1', state: 'CHECKOUT', redirectUrl: 'https://checkout.example/pay1' };
const REFUND_5 = { state: 'COMPLETED', paymentType: 'REFUND', amount: 5, currency: 'GBP' };

beforeEach(() => nock.cleanAll());

it('creates a deposit -> CHECKOUT + redirectUrl', async () => {
  nock(BASE).post(PAY, (b) => b.paymentType === 'DEPOSIT' && b.amount === 10.01)
    .matchHeader('authorization', /^Bearer /).reply(200, { status: 200, result: CHECKOUT_1 });
  const p = await psp.createDeposit({ amount: 10.01, currency: 'GBP', referenceId: 'order-1-a',
    returnUrl: 'https://shop.example/r', webhookUrl: 'https://shop.example/webhooks/psp' });
  expect(p.state).toBe('CHECKOUT');                                  // created, not paid
  expect(p.redirectUrl).toBe('https://checkout.example/pay1');
});

it('surfaces a decline as HTTP 200 + DECLINED', async () => {
  nock(BASE).get(`${PAY}/pay2`).reply(200, { status: 200,
    result: { id: 'pay2', state: 'DECLINED', errorCode: '4.01', errorMessage: 'Insufficient Funds' } });
  await expect(psp.getPayment('pay2')).resolves.toMatchObject({ state: 'DECLINED', errorCode: '4.01' });
});

it('maps a timeout to PspTimeoutError (outcome unknown)', async () => {
  nock(BASE).post(PAY).delayConnection(300).reply(200, {});
  await expect(psp.createDeposit({ amount: 1, currency: 'GBP', referenceId: 'order-9-a',
    returnUrl: 'x', webhookUrl: 'y' })).rejects.toBeInstanceOf(PspTimeoutError);
});

it.each(['hex', 'base64'] as const)('accepts a valid %s signature', async (enc) => {
  await send(BODY, sign(BODY, enc)).expect(200, { status: 'applied' });
  expect(await orderStatus('order-1-a')).toBe('PAID');
  expect(await paidAmount('order-1-a')).toBe(10.01);            // from the payload, not the request
});

it('rejects an invalid signature without touching the order', async () => {
  await send(BODY, 'deadbeef').expect(401);
  expect(await orderStatus('order-1-a')).toBe('AWAITING_PAYMENT');
});

it('is a no-op on duplicate delivery', async () => {
  await send(BODY).expect(200, { status: 'applied' });
  await send(BODY).expect(200, { status: 'duplicate' });
  expect(await eventCount('pay1', 'COMPLETED')).toBe(1);
});

it('a webhook arriving before the order link is not swallowed', async () => {
  // referenceId is the ATTEMPT's reference (not order_ref) and psp_payment_id is not stored yet:
  // the real create-payment/webhook race. The receipt must stay unprocessed.
  const early = JSON.stringify({ id: 'pay7', referenceId: 'order-1-9f2c', state: 'COMPLETED',
                                 amount: 10.01, currency: 'GBP' });
  await send(early).expect(200, { status: 'unknown' });
  expect(await inboxProcessedAt(db, 'pay7', 'COMPLETED')).toBeNull();
  await linkPayment(db, 'order-1-a', 'pay7');                      // the create response lands late
  await send(early).expect(200, { status: 'applied' });            // redelivery still applies it
  expect(await orderStatus('order-1-a')).toBe('PAID');
});

it('concurrent startCheckout creates exactly ONE payment', async () => {
  const created = nock(BASE).post(PAY).once()                    // .once(): a 2nd POST would 404
    .reply(200, { status: 200, result: CHECKOUT_1 });
  nock(BASE).get(/referenceId\.eq=/).reply(200, found());        // the loser reconciles, finds nothing
  const results = await Promise.allSettled([startCheckout(db, 1, 10.01, 'GBP'),
                                            startCheckout(db, 1, 10.01, 'GBP')]);
  const deferred = results.filter((r) => r.status === 'rejected' &&
    r.reason instanceof PaymentOutcomeUnknownError);             // the loser is told to retry (409)
  expect(results.filter((r) => r.status === 'fulfilled').length + deferred.length).toBe(2);
  expect(created.isDone()).toBe(true);
  expect(nock.pendingMocks()).toEqual([]);                       // nothing else was POSTed
  expect(await attemptCount(db, 1)).toBe(1);                     // ONE attempt, ONE referenceId
});

// Baseline recovery path: no UNKNOWN state and no sweep job — the NEXT call reconciles.
it('checkout: a timeout plus an empty reconciliation still recovers on a later call', async () => {
  nock(BASE).post(PAY).delayConnection(300).reply(200, {});
  nock(BASE).get(/referenceId\.eq=/).reply(200, found());                       // not visible yet
  await expect(startCheckout(db, 2, 10.01, 'GBP')).rejects.toBeInstanceOf(PaymentOutcomeUnknownError);
  const stuck = await activeAttempt(db, 2);
  expect(stuck.state).toBe('IN_FLIGHT');                   // still claimed, not a permanent 409
  nock.cleanAll();
  let posts = 0;
  nock(BASE).post(PAY).reply(200, () => { posts += 1; return {}; });
  nock(BASE).get(`${PAY}?referenceId.eq=${stuck.reference_id}`).reply(200,
    found({ id: 'pay2', state: 'CHECKOUT', redirectUrl: 'https://checkout.example/pay2' }));
  await expect(startCheckout(db, 2, 10.01, 'GBP'))
    .resolves.toEqual({ redirectUrl: 'https://checkout.example/pay2' });
  expect((await activeAttempt(db, 2)).state).toBe('READY');
  expect(posts).toBe(0);                                   // ONE referenceId, no second POST
});

it('a FAILED attempt lets a new checkout start', async () => {
  nock(BASE).post(PAY).reply(400, { status: 400, errorCode: '2.01' });   // confirmed refusal
  await expect(startCheckout(db, 3, 1, 'GBP')).rejects.toBeInstanceOf(PspApiError);
  expect(await activeAttempt(db, 3)).toBeUndefined();      // FAILED sits outside the partial index
  nock(BASE).post(PAY).reply(200, { status: 200,
    result: { id: 'pay3', state: 'CHECKOUT', redirectUrl: 'https://checkout.example/pay3' } });
  await expect(startCheckout(db, 3, 1, 'GBP'))
    .resolves.toEqual({ redirectUrl: 'https://checkout.example/pay3' });   // NEW attempt + ref
  expect(await attemptCount(db, 3)).toBe(2);
});

it('a refund timeout then a retry does not refund twice', async () => {
  nock(BASE).post(PAY).delayConnection(300).reply(200, {});
  nock(BASE).get(/referenceId\.eq=/).reply(200, found());                       // not visible yet
  await expect(refundOrder(db, 1, 'rk-1', 5, 'GBP')).rejects.toBeInstanceOf(RefundOutcomeUnknownError);
  const stuck = await refundAttempt(db, 1, 'rk-1');
  expect(stuck.state).toBe('IN_FLIGHT');                   // the attempt row SURVIVED
  expect(await refundedAmount(db, 1)).toBe(5);             // reservation COMMITTED, not rolled back

  // The PSP had processed it all along; only the client timed out. The retry must reconcile the
  // SAME referenceId, never POST a second REFUND.
  nock.cleanAll();
  const second = nock(BASE).post(PAY).reply(200, {});
  nock(BASE).get(`${PAY}?referenceId.eq=${stuck.reference_id}`)
    .reply(200, found({ id: 'rf1', ...REFUND_5 }));
  await expect(refundOrder(db, 1, 'rk-1', 5, 'GBP')).resolves.toMatchObject({ id: 'rf1' });
  expect(second.isDone()).toBe(false);                      // no second payout
  expect(await refundedAmount(db, 1)).toBe(5);              // reserved once, not twice
});

it('two concurrent refunds with the same refundKey POST exactly once', async () => {
  let posts = 0;
  nock(BASE).post(PAY).twice().reply(200, () => {           // twice(): a 2nd POST would show up
    posts += 1;
    return { status: 200, result: { id: 'rf9', ...REFUND_5 } };
  });
  nock(BASE).get(/referenceId\.eq=/).reply(200, found());   // the non-owner reconciles, finds nothing
  nock(BASE).get(`${PAY}/rf9`).reply(200, { status: 200, result: { id: 'rf9', ...REFUND_5 } });
  const out = await Promise.allSettled([refundOrder(db, 1, 'rk-9', 5, 'GBP'),
                                        refundOrder(db, 1, 'rk-9', 5, 'GBP')]);
  const deferred = out.filter((r) => r.status === 'rejected' &&
    r.reason instanceof RefundOutcomeUnknownError);       // non-owner: reconciled, inconclusive -> 409
  expect(out.filter((r) => r.status === 'fulfilled').length + deferred.length).toBe(2);
  expect(posts).toBe(1);                                  // ONE payout, never two
  expect(await refundedAmount(db, 1)).toBe(5);            // reserved once
});

it('a crash between an accepted refund POST and settle does not POST again', async () => {
  // The crash state: attempt committed IN_FLIGHT, amount reserved, the PSP already holds the payment.
  const { attempt } = await reserveRefund(db, 1, 'rk-2', 5, 'GBP');
  let posts = 0;
  nock(BASE).post(PAY).reply(200, () => { posts += 1; return {}; });
  nock(BASE).get(`${PAY}?referenceId.eq=${attempt.reference_id}`)
    .reply(200, found({ id: 'rf2', ...REFUND_5 }));
  await expect(refundOrder(db, 1, 'rk-2', 5, 'GBP')).resolves.toMatchObject({ id: 'rf2' });
  expect(posts).toBe(0);                                   // reconciled, never re-POSTed
  expect(await refundedAmount(db, 1)).toBe(5);
});
```

The replay-job test (a stale `AUTHORIZED` receipt closed as `superseded`) exercises the hardened
variant: `references/hardening-concurrency.md` §2.
