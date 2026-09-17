# PSP — Testing & Sandbox

How to prove the integration works in the sandbox before touching production:
test cards, sandbox amount limits, and a smoke-test checklist to run after
implementing.

---

## 1. Sandbox-first workflow

**Never develop against production.** The PSP exposes two environments with the
same API contract; concrete base URLs live in `wl-config.md` at the skill root.

| Environment | Base URL | Credentials |
|-------------|----------|-------------|
| Sandbox | `$PSP_API_URL` → sandbox value from `wl-config.md` | sandbox Shop API Key + Signing Key |
| Production | `$PSP_API_URL` → production value from `wl-config.md` | production Shop API Key + Signing Key |

What differs between the two:

- **Base URL** — switch via the `$PSP_API_URL` env var, never a code change.
- **Credentials** — sandbox and production keys are different and issued
  separately per shop; that much is documented. Crossing them (sandbox key on
  the production URL or vice versa) should therefore fail authentication —
  *expect* `401`, though the docs don't state the status. Switch URL **and**
  keys together.
- **Money** — sandbox transactions are simulated; test cards below drive the
  outcome.
- Everything else (paths, schemas, error format) is identical. Webhook
  behavior in sandbox isn't documented — see section 4.

Required order of work:

1. Implement against sandbox (`$PSP_API_URL` = sandbox, sandbox keys).
2. Prove the **full flow end-to-end**: create deposit → redirect/execute →
   receive + verify webhook → your order marked PAID → issue a refund and see
   it complete.
3. Only then flip env vars to production values and re-run a minimal live check.

## 2. Sandbox amount rules

From the docs (Testing section):

> For a successful deposit in the sandbox environment, the amount should be
> less than `10000000`. For test withdrawals and refunds, the limit is `10000`.

| Operation | Sandbox rule |
|-----------|--------------|
| Deposit | amount `< 10000000` to succeed |
| Withdrawal / refund | amount limit `10000` |

If a sandbox transaction unexpectedly declines, check the amount against these
limits first.

No other amount-driven outcome simulation (e.g. "magic amounts" that force
specific error codes) is documented in the API docs — outcomes are driven by
the **test card number**.

## 3. Test cards

Exactly as documented. Visa cards appear on the docs "Testing" page; the
Mastercard set comes from the Gateway API spec's "Testing" section.

### Visa

| Card number | Simulated result |
|---------------------|--------------------------------------------------|
| 4000 0000 0000 0002 | 3D-Secure enrolled, successful authorization |
| 4242 4242 4242 4242 | 3D-Secure enrolled, declined authorization |
| 4000 0000 0000 0408 | Not enrolled for 3D-Secure, successful authorization |
| 4000 0000 0000 0416 | Not enrolled for 3D-Secure, declined authorization |

### Mastercard

| Card number | Simulated result |
|---------------------|--------------------------------------------------|
| 5555 0000 0000 0008 | 3D-Secure enrolled, successful authorization |
| 5555 0000 0000 0438 | 3D-Secure enrolled, declined authorization |
| 5555 0000 0000 0107 | Not enrolled for 3D-Secure, successful authorization |
| 5555 0000 0000 0115 | Not enrolled for 3D-Secure, declined authorization |

**Expiry date and CVV for test cards: not documented in the API docs.** The
schema requires a 2-digit month (`expiryMonth`) and a 4-digit year
(`expiryYear`) — those are length constraints only; **the spec states no
future-date requirement.** Use any well-formed future date (e.g. `01`/`2030`,
the spec's own examples) and any well-formed CVV; if the sandbox rejects them,
ask PSP support for the expected values.

Coverage note: the four scenarios per brand let you test both the 3DS
(challenge/redirect) path and the frictionless path, each with a success and a
decline. Always test at least:
- one **3DS-enrolled success** (proves your redirect/return handling),
- one **decline** (proves you surface `errorCode`/`errorMessage` and mark the
  order failed, not stuck).

## 4. Other documented testing capabilities

- The Gateway API spec has a dedicated **Testing** section, but it contains
  only the amount rules and test-card tables above — there are **no callable
  testing endpoints** (no simulate-webhook, no force-state API) documented.
- Webhooks in sandbox: the docs' Testing section **says nothing about webhook
  behavior in sandbox** — that they fire just as in production is an
  **assumption, not documented**. Verify it on your first sandbox payment
  (checklist step 4) before relying on it. The mechanics, when they do fire:
  point `webhookUrl` at a tunnel to your dev machine, and verify the signature
  with the sandbox Signing Key (see `authentication.md`).
- Test cards for non-card methods (bank transfer, PIX, wallets): not documented
  in the API docs — ask PSP support for method-specific sandbox behavior.

## 5. Smoke-test checklist (run after implementing)

Run in the sandbox with sandbox keys. All calls: `Authorization: Bearer
$PSP_API_KEY`.

```
[ ] 1. Auth sanity: GET $PSP_API_URL/api/v1/payments returns 200 (not 401).
[ ] 2. Create deposit: POST /api/v1/payments (DEPOSIT, amount well below
       10000000, unique referenceId, webhookUrl set) → 200, payment id
       returned, state CHECKOUT/PENDING.
[ ] 3. Pay with 4000 0000 0000 0002 (3DS success) → complete the 3DS redirect.
[ ] 4. Webhook received; Signature header verifies with $PSP_SIGNING_KEY;
       handler returns 200.
[ ] 5. GET /api/v1/payments/{id} → state COMPLETED; your order is PAID.
[ ] 6. Negative path: repeat with 4242 4242 4242 4242 → state DECLINED with
       errorCode/errorMessage; order marked failed, user sees a message.
[ ] 7. Refund: POST /api/v1/payments (REFUND, parentPaymentId = deposit id,
       amount ≤ 10000) → refund completes; webhook received; order updated.
[ ] 8. Reconciliation: GET /api/v1/payments?referenceId.eq=<your ref> finds
       the payment (this is your timeout-recovery path — see
       errors-and-troubleshooting.md).
```

Only after every box is checked, switch `$PSP_API_URL` + keys to production.
