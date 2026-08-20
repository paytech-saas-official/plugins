# Python / FastAPI — PSP integration code

Rules and the failures they prevent: `references/integration-patterns.md`. When the project outgrows
the baseline (several instances, real concurrency on one order, crash-during-POST, sweep jobs, plus
the session-per-request and no-transaction-over-I/O traps): `references/hardening-concurrency.md`.
The code below is the **baseline** level.

**Adapt, don't transplant:** reuse the project's HTTP client, ORM, logger, config and test
framework. Field names, endpoints and states are fixed by the API; everything else is yours.

## 1. PSP client

```python
# psp/client.py
import os
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

# Documented state enum: CHECKOUT | PENDING | AWAITING_APPROVAL | AUTHORIZED | COMPLETED
# | DECLINED | CANCELLED. Deliberately NOT a Literal on the field below — see _one().


class PaymentResult(BaseModel):
    model_config = ConfigDict(extra="ignore")     # the API sends many more fields, all optional
    id: str
    state: str                                    # str, never Literal[...]: see _one()
    referenceId: str | None = None
    paymentType: str | None = None                # DEPOSIT | WITHDRAWAL | REFUND
    amount: Decimal | None = None                 # decimal MAJOR units: 10.01 == 10.01 GBP
    currency: str | None = None
    redirectUrl: str | None = None
    parentPaymentId: str | None = None
    errorCode: str | None = None
    errorMessage: str | None = None


class PspTimeout(RuntimeError):
    """Call did not complete: the outcome is UNKNOWN — never treat it as a failure."""


class PspApiError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"PSP HTTP {status}")
        self.status, self.body = status, body


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")            # fail fast at startup
    return value


def _one(payload: Any) -> PaymentResult:
    """Validate INSIDE the fail-safe boundary. `PaymentResult.model_validate(...)` at the call site
    runs *after* the request was sent, and a ValidationError is neither PspTimeout nor PspApiError:
    it escapes the checkout's `except` clause and leaves the attempt claimed with nothing to resolve
    it. Same reason `state` is a plain `str` — a state the API adds later must reach the transition
    whitelist (which ignores what it does not know), and in the webhook handler a ValidationError is
    a 500, so the PSP redelivers that event forever instead of it being acked and ignored."""
    try:
        return PaymentResult.model_validate(payload)
    except ValidationError as exc:
        raise PspTimeout(f"undecodable payment body: {exc.error_count()} error(s)") from exc


class PspClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # An injected client must carry base_url, the auth header AND a timeout: with `timeout=None`
        # a POST can hang forever, so no PspTimeout is ever raised and nothing reconciles.
        self._http = client or httpx.AsyncClient(
            base_url=env("PSP_API_URL").rstrip("/"),
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            headers={"Authorization": f"Bearer {env('PSP_API_KEY')}",   # never log client.headers
                     "Content-Type": "application/json",
                     "User-Agent": "psp-integration/1.0"})   # some WL hosts (WAF) 403 the default client UA

    async def _call(self, method: str, url: str, json: dict | None = None) -> Any:
        # Classify FAIL-SAFE: only a real HTTP status tells you what the PSP did.
        # Everything else must become PspTimeout so it reaches the reconcile path —
        # letting a raw exception escape leaves the attempt claimed with nothing to
        # resolve it, which wedges the order behind a permanent 409.
        # Catch httpx.HTTPError, NOT TransportError: TimeoutException is one subclass of
        # TransportError, and TransportError itself misses two endings that happen AFTER the
        # request went out — DecodingError (a proxy answering with a broken Content-Encoding)
        # and TooManyRedirects. Both are RequestError. HTTPStatusError cannot occur here
        # because raise_for_status() is never called, so no real status is ever mislabelled.
        try:
            r = await self._http.request(method, url, json=json)
        except httpx.HTTPError as exc:
            raise PspTimeout(f"{method} {url}: {type(exc).__name__}") from exc
        if r.is_error:                          # status is KNOWN: 4xx/5xx, never retry a 4xx
            try:
                body: Any = r.json()            # structured error body...
            except ValueError:
                body = r.text[:500]             # ...unless a proxy answered HTML
            raise PspApiError(r.status_code, body)
        try:
            return r.json()["result"]           # responses are {timestamp, status, result}
        except (ValueError, KeyError, TypeError) as exc:   # non-JSON, or JSON that is not the
            raise PspTimeout(                              # envelope (a bare list/scalar subscripted
                f"{method} {url}: undecodable 2xx body") from exc      # by "result" -> TypeError)

    async def create_deposit(self, *, amount: Decimal, currency: str, reference_id: str,
                             return_url: str, webhook_url: str, customer: dict | None = None,
                             billing_address: dict | None = None) -> PaymentResult:
        body = {"paymentType": "DEPOSIT", "amount": float(amount), "currency": currency,
                "referenceId": reference_id, "returnUrl": return_url, "webhookUrl": webhook_url,
                "customer": customer, "billingAddress": billing_address}
        return _one(await self._call(
            "POST", "/api/v1/payments", {k: v for k, v in body.items() if v is not None}))

    async def create_refund(self, *, parent_payment_id: str, amount: Decimal, currency: str,
                            reference_id: str) -> PaymentResult:
        # No refund endpoint: a REFUND is a new payment linked via parentPaymentId.
        return _one(await self._call("POST", "/api/v1/payments", {
            "paymentType": "REFUND", "parentPaymentId": parent_payment_id,
            "amount": float(amount), "currency": currency, "referenceId": reference_id}))

    async def get_payment(self, payment_id: str) -> PaymentResult:
        return _one(await self._call("GET", f"/api/v1/payments/{payment_id}"))

    async def find_by_reference_id(self, reference_id: str) -> list[PaymentResult]:  # reconciliation
        result = await self._call("GET", f"/api/v1/payments?referenceId.eq={reference_id}")
        # `result or []`: an empty match can arrive as `null`, and iterating None on the ONE path
        # that must never fail unclassified — reconciliation — escapes as a raw TypeError.
        return [_one(p) for p in (result or [])]
```

## 2. Webhook endpoint — the raw-body recipe

```python
# psp/signature.py
import base64, hashlib, hmac
from psp.client import env

_SIGNING_KEY = env("PSP_SIGNING_KEY").encode()


def signature_valid(raw_body: bytes, header: str | None) -> bool:
    if not header:
        return False
    mac = hmac.new(_SIGNING_KEY, raw_body, hashlib.sha256).digest()
    # BYTES, not str: compare_digest("…", "é") raises TypeError ("comparing strings with non-ASCII
    # characters is not supported"), so one crafted header turns an unauthenticated 401 into a 500.
    presented = header.strip().encode()
    # Encoding (hex vs base64) is NOT documented — accept both, then pin the one your sandbox
    # sends and delete the other branch (authentication.md §3). compare_digest = constant time.
    return (hmac.compare_digest(mac.hex().encode(), presented.lower())
            or hmac.compare_digest(base64.b64encode(mac), presented))
```

```python
# api/webhooks.py
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response

from db import get_session                  # YOUR session-per-request dependency: one AsyncSession
from orders.transition import apply_webhook  # per request, never one shared across requests
from psp.client import PaymentResult
from psp.signature import signature_valid

router, log = APIRouter(), logging.getLogger(__name__)


@router.post("/webhooks/psp")
async def psp_webhook(request: Request, background: BackgroundTasks,
                      session=Depends(get_session)) -> Response:
    # RAW bytes. Do NOT declare a Pydantic body parameter (e.g. `event: PaymentResult`) on this
    # endpoint: FastAPI would consume and re-parse the stream, and re-serialised JSON never
    # reproduces the byte sequence the HMAC was computed over.
    raw: bytes = await request.body()
    if not signature_valid(raw, request.headers.get("Signature")):
        log.warning("psp webhook signature mismatch (%d bytes)", len(raw))    # never log the key
        raise HTTPException(status_code=401, detail="invalid signature")
    event = PaymentResult.model_validate_json(raw)          # parse only after verifying
    outcome = await apply_webhook(session, event, background)
    log.info("psp webhook id=%s state=%s outcome=%s", event.id, event.state, outcome)
    return Response(status_code=200)                        # always 2xx once verified
```

## 3. Order state transition (idempotent, DB-guarded)

```python
# orders/transition.py  (SQLAlchemy 2.x async, PostgreSQL)
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from db import orders, webhook_event    # YOUR tables/mapped classes — integration-patterns.md schema
from orders.fulfilment import fulfil_order      # YOUR heavy post-payment work: e-mails, ledger, …

_STATE_MAP = {"COMPLETED": "PAID", "AUTHORIZED": "AUTHORIZED",   # whitelist: integration-patterns.md
              "DECLINED": "PAYMENT_FAILED", "CANCELLED": "PAYMENT_FAILED"}
_ALLOWED_FROM = {"PAID": ("AWAITING_PAYMENT", "PROCESSING", "AUTHORIZED"),
                 "AUTHORIZED": ("AWAITING_PAYMENT", "PROCESSING"),
                 "PAYMENT_FAILED": ("AWAITING_PAYMENT", "PROCESSING", "AUTHORIZED")}


async def apply_webhook(session, event, background) -> str:
    next_status = _STATE_MAP.get(event.state)
    if next_status is None:
        return "ignored"                        # non-final / unknown state: change nothing
    async with session.begin():
        # INBOX claim, not a tombstone: `do update ... where processed_at is null` takes over (and
        # row-locks) a receipt that was recorded but never applied, so concurrent redeliveries
        # serialise here; no row back means it was already applied = a real duplicate.
        claim = (await session.execute(insert(webhook_event)
            .values(payment_id=event.id, state=event.state)
            .on_conflict_do_update(index_elements=["payment_id", "state"],
                                   set_={"received_at": func.now()},
                                   where=webhook_event.c.processed_at.is_(None))
            .returning(webhook_event.c.id))).first()
        if claim is None:
            return "duplicate"
        match = [orders.c.psp_payment_id == event.id]
        if event.referenceId:
            match.append(orders.c.order_ref == event.referenceId)
        # Conditional UPDATE: re-application and any downgrade of a final status match 0 rows. Book
        # amount/currency FROM THE PAYLOAD (the final amount may differ from the requested one).
        values = {"status": next_status, "psp_payment_id": event.id, "updated_at": func.now(),
                  "error_code": event.errorCode, "error_message": event.errorMessage}
        if event.amount is not None:
            values |= {"paid_amount": event.amount, "paid_currency": event.currency}
        res = await session.execute(update(orders)
            .where(or_(*match), orders.c.status.in_(_ALLOWED_FROM[next_status]))
            .values(**values).returning(orders.c.id))
        row = res.first()
        if row is None:
            known = (await session.execute(select(orders.c.id).where(or_(*match)))).first()
            # The webhook can beat the create-payment response. Commit the RECEIPT but leave
            # processed_at NULL: marking it processed here loses the event forever, because the
            # redelivery would be dismissed as a duplicate and the order would never transition.
            if known is None:
                return "unknown"                # ack with 200 so the PSP stops redelivering
        # 0 rows with a known order = already past this transition: the receipt is settled either way.
        await session.execute(update(webhook_event)
            .where(webhook_event.c.id == claim.id)
            .values(processed_at=func.now(),
                    processed_reason="applied" if row is not None else "duplicate"))
        if row is None:
            return "duplicate"
    if next_status == "PAID":
        background.add_task(fulfil_order, row.id)       # heavy work, after the 200
    return "applied"
```

## 4. Creation and refund idempotency

```python
# orders/checkout.py
import uuid
from decimal import Decimal

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from db import orders, psp_attempt, psp_refund_attempt      # YOUR tables/mapped classes
from psp.client import PspApiError, PspClient, PspTimeout


class CheckoutInProgress(RuntimeError):
    """The attempt state is churning -> HTTP 409 + Retry-After."""


class PaymentOutcomeUnknown(RuntimeError):
    """Reconciliation ran and is STILL inconclusive -> HTTP 409 + Retry-After. The attempt stays
    claimed, so the next call reconciles again: never a dead end."""


class CheckoutFailed(RuntimeError):
    """The attempt is FAILED and the order is free again: the client may start a NEW checkout."""


class RefundOutcomeUnknown(RuntimeError):
    """A committed refund attempt is unresolved -> HTTP 409; reconcile it, never start a new one."""


_ACTIVE = ("IN_FLIGHT", "READY")   # FAILED excluded: it must never block a new attempt
_ATTEMPT = (psp_attempt.c.id, psp_attempt.c.order_id, psp_attempt.c.reference_id,
            psp_attempt.c.state, psp_attempt.c.redirect_url)


async def _claim_attempt(session, order_id: int):
    """Atomic get-or-create, committed BEFORE the PSP call. Returns (attempt, owner): only the caller
    that INSERTED the row may POST."""
    for _ in range(2):      # 2nd pass: the active attempt turned FAILED between the two statements
        async with session.begin():
            fresh = (await session.execute(insert(psp_attempt)
                .values(order_id=order_id, reference_id=f"order-{order_id}-{uuid.uuid4()}",
                        state="IN_FLIGHT")
                .on_conflict_do_nothing(
                    index_elements=["order_id"],
                    index_where=text("state in ('IN_FLIGHT','READY')"))
                .returning(*_ATTEMPT))).first()
            if fresh is not None:
                return fresh, True
            active = (await session.execute(select(*_ATTEMPT).where(
                psp_attempt.c.order_id == order_id,
                psp_attempt.c.state.in_(_ACTIVE)))).first()
            if active is not None:
                return active, False
    raise CheckoutInProgress(order_id)


async def start_checkout(session, psp: PspClient, order_id: int,
                         amount: Decimal, currency: str) -> str | None:
    attempt, owner = await _claim_attempt(session, order_id)
    if not owner:
        return await _join_attempt(session, psp, attempt)   # a non-owner never POSTs, never mints
    try:
        # Outside session.begin(): the claim is committed, and no transaction is held over the call.
        payment = await psp.create_deposit(
            amount=amount, currency=currency, reference_id=attempt.reference_id,
            return_url="https://shop.example/return/{id}/{referenceId}/{state}/{type}",
            webhook_url="https://shop.example/webhooks/psp",
            customer={"referenceId": f"customer_{order_id}"})
    except (PspTimeout, PspApiError) as exc:
        if isinstance(exc, PspApiError) and exc.status < 500:
            await _set_attempt_state(session, attempt.id, "FAILED")   # confirmed: nothing created
            raise                                                     # ...so the order is free
        # Timeout or ambiguous 5xx: the payment may exist. Reconcile, never a second POST.
        return await resolve_attempt(session, psp, attempt)
    return await _promote_ready(session, attempt, payment)


async def _join_attempt(session, psp: PspClient, attempt):
    """READY -> the stored URL. Still IN_FLIGHT -> reconcile, so a 409 always follows real progress.
    (One extra GET per concurrent click; the cheaper UNKNOWN split: hardening-concurrency.md §1.)"""
    if attempt.state == "READY":
        return attempt.redirect_url
    return await resolve_attempt(session, psp, attempt)


async def resolve_attempt(session, psp: PspClient, attempt):
    """The only way out of an unresolved attempt: GET by the PERSISTED referenceId."""
    found = await psp.find_by_reference_id(attempt.reference_id)
    if not found:                        # stays claimed: retried by the next call
        raise PaymentOutcomeUnknown(attempt.reference_id)
    if found[0].state in ("DECLINED", "CANCELLED"):
        await _set_attempt_state(session, attempt.id, "FAILED")   # frees the order for a NEW attempt
        raise CheckoutFailed(found[0].errorCode or found[0].state)
    return await _promote_ready(session, attempt, found[0])


async def _promote_ready(session, attempt, payment) -> str | None:
    async with session.begin():
        await session.execute(update(psp_attempt).where(psp_attempt.c.id == attempt.id)
            .values(state="READY", psp_payment_id=payment.id, redirect_url=payment.redirectUrl))
        await session.execute(update(orders)      # AWAITING_PAYMENT only: a webhook may have won
            .where(orders.c.id == attempt.order_id, orders.c.status == "AWAITING_PAYMENT")
            .values(status="PROCESSING", psp_payment_id=payment.id))
    return payment.redirectUrl   # CHECKOUT, not paid; None once the payment moved past checkout


async def _set_attempt_state(session, attempt_id: int, state: str) -> None:
    async with session.begin():
        await session.execute(update(psp_attempt)
            .where(psp_attempt.c.id == attempt_id).values(state=state))


_REFUND = (psp_refund_attempt.c.id, psp_refund_attempt.c.order_id,
           psp_refund_attempt.c.reference_id, psp_refund_attempt.c.amount,
           psp_refund_attempt.c.state, psp_refund_attempt.c.psp_payment_id)


async def refund_order(session, psp: PspClient, order_id: int, refund_key: str,
                       amount: Decimal, currency: str):
    """Idempotent per (order_id, refund_key) — refund_key identifies ONE logical refund. ONLY the
    caller that INSERTED the attempt may POST: referenceId is NOT an idempotency key at the PSP, so
    a second POST for the same refund is a second payout."""
    attempt, owner, parent_id = await reserve_refund(session, order_id, refund_key, amount, currency)
    if attempt.psp_payment_id:
        return await psp.get_payment(attempt.psp_payment_id)        # DONE/FAILED: pure replay
    if not owner:
        # Someone else's (or an earlier crashed) attempt: reconcile or 409, never POST.
        return await _reconcile_refund(session, psp, attempt)
    try:
        result = await psp.create_refund(parent_payment_id=parent_id, amount=amount,
                                         currency=currency, reference_id=attempt.reference_id)
    except PspTimeout:
        # Outcome UNKNOWN. The row is already committed: reconcile by ITS referenceId.
        return await _reconcile_refund(session, psp, attempt)
    await _settle_refund(session, attempt, result)
    return result


async def reserve_refund(session, order_id, refund_key, amount, currency):   # public: tests use it
    """Returns (attempt, owner, parent_payment_id). ONE commit for the attempt AND the amount
    reservation, before the PSP call. A row that already existed belongs to another call, so its
    amount must NOT be reserved a second time and its caller must NOT POST."""
    async with session.begin():
        order = (await session.execute(select(orders.c.psp_payment_id)
            .where(orders.c.id == order_id))).first()
        if order is None:
            raise ValueError("unknown order")
        row = (await session.execute(insert(psp_refund_attempt)
            .values(order_id=order_id, refund_key=refund_key, amount=amount, currency=currency,
                    reference_id=f"refund-{order_id}-{uuid.uuid4()}", state="IN_FLIGHT")
            .on_conflict_do_nothing(index_elements=["order_id", "refund_key"])
            .returning(*_REFUND))).first()
        if row is None:                  # the row exists: reuse ITS referenceId, reserve nothing
            row = (await session.execute(select(*_REFUND).where(
                psp_refund_attempt.c.order_id == order_id,
                psp_refund_attempt.c.refund_key == refund_key))).first()
            return row, False, order.psp_payment_id
        reserved = await session.execute(update(orders)      # refund only the remainder
            .where(orders.c.id == order_id, orders.c.status == "PAID",
                   orders.c.refunded_amount + amount <= orders.c.paid_amount)
            .values(refunded_amount=orders.c.refunded_amount + amount))
        if reserved.rowcount == 0:
            raise ValueError("refund exceeds remaining refundable amount")
    return row, True, order.psp_payment_id


async def _reconcile_refund(session, psp: PspClient, attempt):
    # GET by the PERSISTED referenceId. A fresh referenceId here is a SECOND payout.
    found = await psp.find_by_reference_id(attempt.reference_id)
    if not found:
        raise RefundOutcomeUnknown(f"refund {attempt.reference_id} unresolved; retry reconciliation")
    await _settle_refund(session, attempt, found[0])
    return found[0]


async def _settle_refund(session, attempt, result):
    failed = result.state in ("DECLINED", "CANCELLED")
    async with session.begin():
        # State-conditional: the owner and a reconciler can settle the same attempt, and releasing
        # the reservation twice would inflate the refundable amount.
        done = await session.execute(update(psp_refund_attempt)
            .where(psp_refund_attempt.c.id == attempt.id,
                   psp_refund_attempt.c.state == "IN_FLIGHT")
            .values(state="FAILED" if failed else "DONE", psp_payment_id=result.id))
        if failed and done.rowcount:   # only a CONFIRMED failure gives the amount back, not a timeout
            await session.execute(update(orders).where(orders.c.id == attempt.order_id)
                .values(refunded_amount=orders.c.refunded_amount - attempt.amount))
```

## 5. Tests (pytest + respx + httpx.ASGITransport)

```python
import asyncio, base64, hashlib, hmac, json
from decimal import Decimal

import httpx, pytest, respx
from orders.checkout import (PaymentOutcomeUnknown, RefundOutcomeUnknown,
                            refund_order, reserve_refund, start_checkout)
from psp.client import PspApiError, PspClient, PspTimeout

BASE = "https://sandbox.psp.invalid"          # sandbox stub; never production credentials
PAY = f"{BASE}/api/v1/payments"
SIGNING_KEY = b"test-signing-key"
BODY = json.dumps({"id": "pay1", "referenceId": "order-1-a", "state": "COMPLETED",
                   "amount": 10.01, "currency": "GBP"}).encode()
CHECKOUT_1 = {"id": "pay1", "state": "CHECKOUT", "redirectUrl": "https://checkout.example/pay1"}
REFUND_5 = {"state": "COMPLETED", "paymentType": "REFUND", "amount": 5.00, "currency": "GBP"}


def sign(body: bytes, encoding: str = "hex") -> str:
    mac = hmac.new(SIGNING_KEY, body, hashlib.sha256).digest()
    return mac.hex() if encoding == "hex" else base64.b64encode(mac).decode()


def wrapped(result) -> httpx.Response:        # every response is {timestamp, status, result}
    return httpx.Response(200, json={"status": 200, "result": result})


@respx.mock
async def test_successful_deposit(psp: PspClient):
    route = respx.post(PAY).mock(return_value=wrapped(CHECKOUT_1))
    p = await psp.create_deposit(amount=Decimal("10.01"), currency="GBP", reference_id="order-1-a",
                                return_url="https://shop.example/r",
                                webhook_url="https://shop.example/webhooks/psp")
    assert p.state == "CHECKOUT" and p.redirectUrl == CHECKOUT_1["redirectUrl"]   # created, not paid
    sent = json.loads(route.calls.last.request.content)
    assert sent["paymentType"] == "DEPOSIT" and sent["amount"] == 10.01
    assert route.calls.last.request.headers["authorization"].startswith("Bearer ")


@respx.mock
async def test_decline_is_http_200_with_declined_state(psp: PspClient):
    respx.get(f"{PAY}/pay2").mock(return_value=wrapped(
        {"id": "pay2", "state": "DECLINED", "errorCode": "4.01"}))
    p = await psp.get_payment("pay2")
    assert (p.state, p.errorCode) == ("DECLINED", "4.01")


@respx.mock
async def test_timeout_means_unknown_outcome(psp: PspClient):
    respx.post(PAY).mock(side_effect=httpx.ReadTimeout("boom"))
    with pytest.raises(PspTimeout):
        await psp.create_deposit(amount=Decimal("1"), currency="GBP", reference_id="order-9-a",
                                 return_url="x", webhook_url="y")


@respx.mock
async def test_every_ending_without_a_status_is_unknown_never_a_raw_exception(psp: PspClient):
    # None of these is a TransportError and none carries a usable status, yet all of them happen
    # AFTER the request went out. Each must be PspTimeout; a raw exception here would escape
    # start_checkout's except clause and leave the attempt claimed with nothing to resolve it.
    for ending in ({"side_effect": httpx.DecodingError("broken Content-Encoding")},  # a proxy
                   {"return_value": httpx.Response(200, text="<html>proxy</html>")},  # undecodable
                   {"return_value": httpx.Response(200, json=[])},   # 2xx, but not the envelope
                   {"return_value": wrapped({"state": "CHECKOUT"})},  # no id: the model rejects it
                   {"return_value": wrapped(None)}):                  # result: null
        respx.post(PAY).mock(**ending)
        with pytest.raises(PspTimeout):
            await psp.create_deposit(amount=Decimal("1"), currency="GBP", reference_id="order-9-a",
                                     return_url="x", webhook_url="y")
    respx.get(f"{PAY}/pay8").mock(return_value=wrapped({"id": "pay8", "state": "ADDED_IN_v2"}))
    assert (await psp.get_payment("pay8")).state == "ADDED_IN_v2"   # a state the API added later is
    #                          accepted and left to the transition whitelist, NOT a validation error


@pytest.mark.parametrize("encoding", ["hex", "base64"])
async def test_valid_webhook_marks_order_paid(client: httpx.AsyncClient, db, encoding):
    r = await client.post("/webhooks/psp", content=BODY, headers={
        "Signature": sign(BODY, encoding), "Content-Type": "application/json"})
    assert r.status_code == 200
    order = await fetch_order(db, "order-1-a")
    assert order.status == "PAID"
    assert order.paid_amount == Decimal("10.01")        # from the payload, not from the request


async def test_invalid_signature_rejected_and_order_untouched(client, db):
    r = await client.post("/webhooks/psp", content=BODY,
                          headers={"Signature": "deadbeef", "Content-Type": "application/json"})
    assert r.status_code == 401
    assert (await fetch_order(db, "order-1-a")).status == "AWAITING_PAYMENT"


async def test_duplicate_webhook_is_a_no_op(client, db):
    headers = {"Signature": sign(BODY), "Content-Type": "application/json"}
    assert (await client.post("/webhooks/psp", content=BODY, headers=headers)).status_code == 200
    before = await fetch_order(db, "order-1-a")
    assert (await client.post("/webhooks/psp", content=BODY, headers=headers)).status_code == 200
    after = await fetch_order(db, "order-1-a")
    assert (after.status, after.updated_at) == (before.status, before.updated_at)
    assert await count_events(db, "pay1", "COMPLETED") == 1


async def test_webhook_before_the_order_link_is_not_swallowed(client, db):
    # referenceId is the ATTEMPT's reference (not order_ref) and psp_payment_id is not stored yet:
    # the real create-payment/webhook race. The receipt must stay unprocessed.
    early = json.dumps({"id": "pay7", "referenceId": "order-1-9f2c", "state": "COMPLETED",
                        "amount": 10.01, "currency": "GBP"}).encode()
    headers = {"Signature": sign(early), "Content-Type": "application/json"}
    assert (await client.post("/webhooks/psp", content=early, headers=headers)).status_code == 200
    assert await fetch_processed_at(db, "pay7", "COMPLETED") is None
    await link_payment(db, "order-1-a", "pay7")             # the create response lands late
    assert (await client.post("/webhooks/psp", content=early, headers=headers)).status_code == 200
    assert (await fetch_order(db, "order-1-a")).status == "PAID"       # redelivery still applies it


# The tests below need a REAL PostgreSQL (Testcontainers) and independent sessions: the guarantees
# rest on ON CONFLICT, partial indexes and committed transactions, which no in-memory/mocked DB
# reproduces. Sharing one AsyncSession across asyncio.gather serialises the race away.

@respx.mock
async def test_concurrent_start_checkout_creates_exactly_one_payment(session_factory, psp, db):
    route = respx.post(PAY).mock(return_value=wrapped(CHECKOUT_1))
    respx.get(url__startswith=f"{PAY}?referenceId.eq=").mock(       # the loser reconciles...
        return_value=wrapped([]))                                   # ...and finds nothing yet

    async def attempt():
        async with session_factory() as s:
            return await start_checkout(s, psp, 1, Decimal("10.01"), "GBP")

    outcomes = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    deferred = [o for o in outcomes if isinstance(o, PaymentOutcomeUnknown)]   # loser -> 409, no POST
    assert len(deferred) + len([o for o in outcomes if isinstance(o, str)]) == 2
    assert route.call_count == 1                       # exactly ONE payment created
    assert await count_open_attempts(db, 1) == 1       # ONE attempt, ONE referenceId


# Baseline recovery path: no UNKNOWN state and no sweep job — the NEXT call reconciles.
@respx.mock
async def test_checkout_timeout_then_empty_reconciliation_recovers_later(session, psp, db):
    respx.post(PAY).mock(side_effect=httpx.ReadTimeout("boom"))
    respx.get(url__startswith=f"{PAY}?referenceId.eq=").mock(return_value=wrapped([]))  # not yet
    with pytest.raises(PaymentOutcomeUnknown):
        await start_checkout(session, psp, 2, Decimal("10.01"), "GBP")
    stuck = await fetch_active_attempt(db, 2)
    assert stuck.state == "IN_FLIGHT"                     # still claimed, not a permanent 409
    respx.reset()
    posts = respx.post(PAY)
    respx.get(f"{PAY}?referenceId.eq={stuck.reference_id}").mock(return_value=wrapped(
        [{"id": "pay2", "state": "CHECKOUT", "redirectUrl": "https://checkout.example/pay2"}]))
    assert (await start_checkout(session, psp, 2, Decimal("10.01"), "GBP")
            == "https://checkout.example/pay2")           # the later call reconciles and serves it
    assert (await fetch_active_attempt(db, 2)).state == "READY"
    assert posts.call_count == 0                          # ONE referenceId, no second POST


@respx.mock
async def test_a_failed_attempt_lets_a_new_checkout_start(session, psp, db):
    respx.post(PAY).mock(return_value=httpx.Response(          # confirmed refusal, nothing created
        400, json={"status": 400, "errorCode": "2.01"}))
    with pytest.raises(PspApiError):
        await start_checkout(session, psp, 3, Decimal("1.00"), "GBP")
    assert await fetch_active_attempt(db, 3) is None      # FAILED sits outside the partial index
    respx.reset()
    respx.post(PAY).mock(return_value=wrapped(
        {"id": "pay3", "state": "CHECKOUT", "redirectUrl": "https://checkout.example/pay3"}))
    assert (await start_checkout(session, psp, 3, Decimal("1.00"), "GBP")
            == "https://checkout.example/pay3")           # a NEW attempt, a NEW referenceId
    assert await count_attempts(db, 3) == 2


@respx.mock
async def test_refund_timeout_then_retry_does_not_refund_twice(session, psp, db):
    respx.post(PAY).mock(side_effect=httpx.ReadTimeout("boom"))
    respx.get(url__startswith=f"{PAY}?referenceId.eq=").mock(return_value=wrapped([]))  # not yet
    with pytest.raises(RefundOutcomeUnknown):
        await refund_order(session, psp, 1, "rk-1", Decimal("5.00"), "GBP")
    stuck = await fetch_refund_attempt(db, 1, "rk-1")
    assert stuck.state == "IN_FLIGHT"                                  # the attempt row SURVIVED
    assert (await fetch_order(db, "order-1-a")).refunded_amount == Decimal("5.00")  # commit survived

    # The PSP had processed it; only the client timed out. The retry must reconcile the SAME
    # referenceId, never POST a second REFUND.
    respx.reset()
    posted = respx.post(PAY)
    respx.get(f"{PAY}?referenceId.eq={stuck.reference_id}").mock(
        return_value=wrapped([{"id": "rf1", **REFUND_5}]))
    result = await refund_order(session, psp, 1, "rk-1", Decimal("5.00"), "GBP")
    assert result.id == "rf1" and posted.call_count == 0                # no second payout
    assert (await fetch_order(db, "order-1-a")).refunded_amount == Decimal("5.00")  # reserved once


@respx.mock
async def test_concurrent_refunds_with_the_same_key_post_once(session_factory, psp, db):
    posts = respx.post(PAY).mock(return_value=wrapped({"id": "rf9", **REFUND_5}))
    respx.get(url__startswith=f"{PAY}?referenceId.eq=").mock(   # the non-owner reconciles...
        return_value=wrapped([]))                               # ...and finds nothing yet
    respx.get(f"{PAY}/rf9").mock(return_value=wrapped({"id": "rf9", **REFUND_5}))

    async def attempt():
        async with session_factory() as s:
            return await refund_order(s, psp, 1, "rk-9", Decimal("5.00"), "GBP")

    outcomes = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    deferred = [o for o in outcomes if isinstance(o, RefundOutcomeUnknown)]   # non-owner -> 409
    assert len(deferred) + len([o for o in outcomes if not isinstance(o, Exception)]) == 2
    assert posts.call_count == 1                                  # ONE payout, never two
    assert (await fetch_order(db, "order-1-a")).refunded_amount == Decimal("5.00")   # reserved once


@respx.mock
async def test_crash_after_an_accepted_refund_post_does_not_post_again(session, psp, db):
    # The crash state: attempt committed IN_FLIGHT, amount reserved, the PSP already holds the payment.
    attempt, _, _ = await reserve_refund(session, 1, "rk-2", Decimal("5.00"), "GBP")
    posts = respx.post(PAY)
    respx.get(f"{PAY}?referenceId.eq={attempt.reference_id}").mock(
        return_value=wrapped([{"id": "rf2", **REFUND_5}]))
    result = await refund_order(session, psp, 1, "rk-2", Decimal("5.00"), "GBP")
    assert result.id == "rf2" and posts.call_count == 0            # reconciled, never re-POSTed
    assert (await fetch_refund_attempt(db, 1, "rk-2")).state == "DONE"
```

The replay-job test (a stale `AUTHORIZED` receipt closed as `superseded`) exercises the hardened
variant: `references/hardening-concurrency.md` §2.
