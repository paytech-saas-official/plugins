# Java / Spring Boot — PSP integration code

Rules and the failures they prevent: `references/integration-patterns.md`. When the project outgrows
the baseline (several instances, real concurrency on one order, crash-during-POST, sweep jobs, and
why the transactional work must live in separate beans): `references/hardening-concurrency.md`.
The code below is the **baseline** level.

**Adapt, don't transplant:** reuse the project's HTTP client, ORM, logger, config and test
framework. Field names, endpoints and states are fixed by the API; everything else is yours.

## 1. PSP client

```java
@JsonInclude(JsonInclude.Include.NON_NULL)   // nulls omitted from requests
record CreatePaymentRequest(String paymentType, BigDecimal amount, String currency, String referenceId,
    String returnUrl, String webhookUrl, String parentPaymentId,
    Map<String, String> customer, Map<String, String> billingAddress) {}
record PaymentResult(String id, String referenceId, String paymentType, String state, BigDecimal amount,
    String currency, String redirectUrl, String errorCode, String errorMessage) {}
record Envelope<T>(String timestamp, int status, T result) {}   // every response is wrapped

@Component
public class PspClient {
  private static final Logger log = LoggerFactory.getLogger(PspClient.class);
  private static final ParameterizedTypeReference<Envelope<PaymentResult>> ONE = new ParameterizedTypeReference<>() {};
  private static final ParameterizedTypeReference<Envelope<List<PaymentResult>>> MANY = new ParameterizedTypeReference<>() {};
  private final RestClient http;

  PspClient(@Value("${psp.api-url}") String baseUrl, @Value("${psp.api-key}") String apiKey) {
    var f = new SimpleClientHttpRequestFactory();
    f.setConnectTimeout(Duration.ofSeconds(5));
    f.setReadTimeout(Duration.ofSeconds(30));                        // checkout creation can be slow
    this.http = RestClient.builder().baseUrl(baseUrl).requestFactory(f)
        .defaultHeader(HttpHeaders.AUTHORIZATION, "Bearer " + apiKey) // never logged or echoed
        .defaultHeader(HttpHeaders.USER_AGENT, "psp-integration/1.0")  // some WL hosts sit behind a WAF that 403s the default client UA
        .defaultHeader(HttpHeaders.CONTENT_TYPE, MediaType.APPLICATION_JSON_VALUE).build();
  }

  public PaymentResult createDeposit(CreatePaymentRequest body) { return post(body); }

  /** REFUND = a new payment linked by parentPaymentId; there is no /refund endpoint. */
  public PaymentResult createRefund(String parentId, BigDecimal amount, String ccy, String ref) {
    return post(new CreatePaymentRequest("REFUND", amount, ccy, ref, null, null, parentId, null, null));
  }

  public PaymentResult getPayment(String id) {
    return call(() -> http.get().uri("/api/v1/payments/{id}", id).retrieve().body(ONE)).result();
  }

  public List<PaymentResult> findByReferenceId(String ref) {          // reconciliation lookup
    return call(() -> http.get().uri(b -> b.path("/api/v1/payments")
        .queryParam("referenceId.eq", ref).build()).retrieve().body(MANY)).result();
  }

  private PaymentResult post(CreatePaymentRequest body) {
    var r = call(() -> http.post().uri("/api/v1/payments").body(body).retrieve().body(ONE)).result();
    log.info("psp payment id={} ref={} state={}", r.id(), r.referenceId(), r.state());  // ids/state only
    return r;                                        // a decline is 200 + DECLINED, not an exception
  }

  // Two tiny RuntimeExceptions of your own: PspTimeoutException = outcome UNKNOWN;
  // PspApiException exposes status() — >= 500 is ambiguous, a 4xx is a CONFIRMED refusal.
  //
  // Classify FAIL-SAFE: only a real HTTP status tells you what happened. Everything
  // else means "the PSP may or may not have processed it", so it must become UNKNOWN
  // and reach the reconcile path. Catching just ResourceAccessException here (the
  // RestTemplate idiom) silently misses the most important case: RestClient extracts
  // the body lazily, so a read timeout surfaces as a plain RestClientException whose
  // cause is SocketTimeoutException. It also misses UnknownContentTypeException, which
  // is what you get when a proxy answers 200 text/html. Either one escaping this method
  // leaves the attempt IN_FLIGHT with nothing to resolve it — a permanently wedged order.
  private <T> Envelope<T> call(Supplier<Envelope<T>> op) {
    try { return op.get(); }
    catch (RestClientResponseException e) { throw new PspApiException(  // a real status + body
        e.getStatusCode().value(), e.getResponseBodyAsString(), e); }
    catch (RestClientException e) {   // transport, timeout (incl. lazy body read), undecodable body
      throw new PspTimeoutException("outcome unknown", e);
    }
  }
}
```

## 2. Webhook controller — the raw-body recipe

```java
@RestController
public class PspWebhookController {
  private static final Logger log = LoggerFactory.getLogger(PspWebhookController.class);
  private final WebhookSignatureVerifier verifier;
  private final ObjectMapper mapper;
  private final OrderPaymentService orders;

  // CRITICAL: byte[] (ByteArrayHttpMessageConverter) yields the UNMODIFIED request bytes.
  // `@RequestBody Map<String,Object>` or a DTO makes Jackson parse the body; re-serialising that to
  // compute the HMAC reorders keys and drops whitespace, so the signature NEVER matches.
  // Equivalent: HttpServletRequest req -> req.getInputStream().readAllBytes().
  @PostMapping(path = "/webhooks/psp", consumes = MediaType.APPLICATION_JSON_VALUE)
  public ResponseEntity<Map<String, String>> handle(@RequestBody byte[] rawBody,
      @RequestHeader(name = "Signature", required = false) String signature) throws IOException {
    if (!verifier.verify(rawBody, signature)) {
      log.warn("psp webhook signature mismatch, {} bytes", rawBody.length);   // never log the key
      return ResponseEntity.status(HttpStatus.UNAUTHORIZED).body(Map.of("error", "invalid signature"));
    }
    var event = mapper.readValue(rawBody, PaymentResult.class);   // parse only after verifying
    var outcome = orders.applyWebhook(event);
    log.info("psp webhook id={} state={} outcome={}", event.id(), event.state(), outcome);
    return ResponseEntity.ok(Map.of("status", outcome.name().toLowerCase(Locale.ROOT)));  // 2xx, fast
  }
}

@Component
class WebhookSignatureVerifier {
  private final byte[] key;
  WebhookSignatureVerifier(@Value("${psp.signing-key}") String signingKey) {   // PSP_SIGNING_KEY
    if (signingKey == null || signingKey.isBlank())
      throw new IllegalStateException("PSP_SIGNING_KEY is not configured");    // fail fast at startup
    this.key = signingKey.getBytes(StandardCharsets.UTF_8);
  }

  boolean verify(byte[] rawBody, String header) {
    if (header == null || header.isBlank()) return false;
    byte[] mac;
    try {
      var hmac = Mac.getInstance("HmacSHA256");
      hmac.init(new SecretKeySpec(key, "HmacSHA256"));
      mac = hmac.doFinal(rawBody);
    } catch (GeneralSecurityException e) { throw new IllegalStateException(e); }
    String presented = header.trim();
    // Encoding (hex vs base64) is NOT documented: accept both, then pin the one your sandbox sends
    // and delete the other branch (authentication.md §3). isEqual = constant time.
    return eq(HexFormat.of().formatHex(mac), presented.toLowerCase(Locale.ROOT))
        || eq(Base64.getEncoder().encodeToString(mac), presented);
  }

  private static boolean eq(String a, String b) {
    return MessageDigest.isEqual(a.getBytes(StandardCharsets.UTF_8), b.getBytes(StandardCharsets.UTF_8));
  }
}
```

## 3. Order state transition (idempotent, DB-guarded)

```java
enum OrderStatus { AWAITING_PAYMENT, PROCESSING, AUTHORIZED, PAID, PAYMENT_FAILED }
enum WebhookOutcome { APPLIED, DUPLICATE, IGNORED, UNKNOWN_PAYMENT }

@Service
public class OrderPaymentService {
  private static final Map<String, OrderStatus> MAPPING = Map.of(  // whitelist: integration-patterns.md
      "COMPLETED", OrderStatus.PAID, "AUTHORIZED", OrderStatus.AUTHORIZED,
      "DECLINED", OrderStatus.PAYMENT_FAILED, "CANCELLED", OrderStatus.PAYMENT_FAILED);
  private static final Map<OrderStatus, Set<OrderStatus>> ALLOWED_FROM = Map.of(
      OrderStatus.PAID, EnumSet.of(AWAITING_PAYMENT, PROCESSING, AUTHORIZED),
      OrderStatus.AUTHORIZED, EnumSet.of(AWAITING_PAYMENT, PROCESSING),
      OrderStatus.PAYMENT_FAILED, EnumSet.of(AWAITING_PAYMENT, PROCESSING, AUTHORIZED));

  private final OrderRepository orders;
  private final WebhookEventRepository events;
  private final FulfilmentQueue queue;

  @Transactional
  public WebhookOutcome applyWebhook(PaymentResult e) {
    OrderStatus next = MAPPING.get(e.state());
    if (next == null) return WebhookOutcome.IGNORED;                // non-final / unknown: no change
    Long inboxId = events.claim(e.id(), e.state()).orElse(null);    // INBOX claim, not a tombstone
    if (inboxId == null) return WebhookOutcome.DUPLICATE;           // already applied
    // Conditional UPDATE: re-application and any downgrade of a final status match 0 rows. Book
    // amount/currency FROM THE PAYLOAD — the final amount may differ from the requested one.
    int updated = orders.applyTransition(e.id(), e.referenceId(), next, e.amount(), e.currency(),
                                        e.errorCode(), e.errorMessage(), ALLOWED_FROM.get(next));
    if (updated == 0) {
      // No order yet: the webhook can beat the create-payment response. COMMIT the receipt but leave
      // processed_at NULL, or the redelivery is dismissed as a duplicate and the event is lost.
      if (!orders.existsForPayment(e.id(), e.referenceId())) return WebhookOutcome.UNKNOWN_PAYMENT;
      events.markProcessed(inboxId, "duplicate");     // order already past this transition
      return WebhookOutcome.DUPLICATE;                                // both ack with 200
    }
    events.markProcessed(inboxId, "applied");
    if (next == OrderStatus.PAID) queue.enqueueFulfilment(e.id());     // heavy work async
    return WebhookOutcome.APPLIED;
  }
}

interface WebhookEventRepository extends JpaRepository<WebhookEventEntity, Long> {
  /** Inbox claim: insert, or take over a row that was received but never processed.
   *  `@Transactional` (read-write) is required: Spring Data marks plain query methods
   *  `@Transactional(readOnly = true)`, and this one is an INSERT ... RETURNING, so a
   *  call outside an existing read-write transaction fails. Inside `applyWebhook` the
   *  caller's transaction already covers it; this annotation is what makes a direct
   *  call (a test, a replay job) work too. No `@Modifying` — that would discard the
   *  RETURNING value. */
  @Transactional
  @Query(value = """
      insert into psp_webhook_event (payment_id, state, received_at) values (:id, :state, now())
       on conflict (payment_id, state) do update set received_at = now()
        where psp_webhook_event.processed_at is null
       returning id""", nativeQuery = true)
  Optional<Long> claim(String id, String state);
  @Modifying(clearAutomatically = true)
  @Query(value = "update psp_webhook_event set processed_at = now(), processed_reason = :reason"
               + " where id = :id", nativeQuery = true)
  void markProcessed(long id, String reason);
  Optional<WebhookEventEntity> findByPaymentIdAndState(String paymentId, String state);
}

interface OrderRepository extends JpaRepository<OrderEntity, Long> {
  Optional<OrderEntity> findByOrderRef(String orderRef);   // derived query: must be declared
  @Modifying(clearAutomatically = true)
  @Query("""
      update OrderEntity o set o.status = :next, o.pspPaymentId = :paymentId,
             o.paidAmount = coalesce(:amount, o.paidAmount),
             o.paidCurrency = coalesce(:currency, o.paidCurrency),
             o.errorCode = :errorCode, o.errorMessage = :errorMessage
       where (o.pspPaymentId = :paymentId or o.orderRef = :referenceId) and o.status in :allowedFrom""")
  int applyTransition(String paymentId, String referenceId, OrderStatus next, BigDecimal amount,
      String currency, String errorCode, String errorMessage, Set<OrderStatus> allowedFrom);
  @Modifying   // refund only the remainder; self-serialising under READ COMMITTED
  @Query("update OrderEntity o set o.refundedAmount = o.refundedAmount + :amount where o.id = :orderId"
       + " and o.status = 'PAID' and o.refundedAmount + :amount <= o.paidAmount")
  int reserveRefund(long orderId, BigDecimal amount);
  @Modifying   // only a CONFIRMED failure gives the amount back; a timeout must not
  @Query("update OrderEntity o set o.refundedAmount = o.refundedAmount - :amount where o.id = :orderId")
  int releaseRefund(long orderId, BigDecimal amount);
  @Modifying   // stores the id the webhook matches on; may land AFTER the first webhook
  @Query("update OrderEntity o set o.pspPaymentId = :paymentId where o.orderRef = :ref")
  int linkPayment(String ref, String paymentId);
  @Modifying   // AWAITING_PAYMENT only: a webhook may already have moved the order to PAID
  @Query("update OrderEntity o set o.status = 'PROCESSING', o.pspPaymentId = :paymentId"
       + " where o.id = :orderId and o.status = 'AWAITING_PAYMENT'")
  int linkProcessing(long orderId, String paymentId);
  @Query("select count(o) > 0 from OrderEntity o where o.pspPaymentId = :id or o.orderRef = :ref")
  boolean existsForPayment(String id, String ref);
}
```

## 4. Creation and refund idempotency

```java
// All @ResponseStatus(HttpStatus.CONFLICT) + Retry-After, except CheckoutFailedException.
class PaymentOutcomeUnknownException extends RuntimeException {  // reconciled, STILL inconclusive:
  PaymentOutcomeUnknownException(String ref) { super(ref); } }  // the next call reconciles again
class CheckoutFailedException extends RuntimeException {         // attempt FAILED, order free again
  CheckoutFailedException(String code) { super(code); } }
class CheckoutInProgressException extends RuntimeException {}    // attempt state is churning
class RefundOutcomeUnknownException extends RuntimeException {}  // committed refund unresolved
class RefundFailedException extends RuntimeException {           // key was refused; reservation
  RefundFailedException(String refundKey) { super(refundKey); } } // released. A corrected refund
                                                                 // needs a NEW refund key.

// The transactional work lives in the two *Store beans, NOT here: Spring's proxy does not intercept
// self-invocation, so `this.claim(...)` would not commit before the PSP call —
// hardening-concurrency.md §3.
@Service                          // deliberately NOT @Transactional: these methods do network I/O
public class CheckoutService {
  private final PspAttemptStore attempts;
  private final RefundAttemptStore refunds;
  private final OrderRepository orders;
  private final PspClient psp;

  /** Double-click safe: only the request that OWNS the freshly inserted attempt may POST. */
  public String startCheckout(long orderId, BigDecimal amount, String currency) {
    var claimed = attempts.claim(orderId);                        // committed before any PSP call
    var attempt = claimed.attempt();
    if (!claimed.owner()) return join(attempt);      // a non-owner never POSTs, never mints a ref
    try {
      return attempts.promoteReady(attempt.getId(), orderId,
          psp.createDeposit(new CreatePaymentRequest("DEPOSIT", amount, currency,
              attempt.getReferenceId(), "https://shop.example/return/{id}/{referenceId}/{state}/{type}",
              "https://shop.example/webhooks/psp", null,
              Map.of("referenceId", "customer_" + orderId),
              Map.of("countryCode", "GB", "city", "London"))));
    } catch (PspTimeoutException e) {
      return resolve(attempt);                       // outcome unknown: GET, never a second POST
    } catch (PspApiException e) {
      if (e.status() >= 500) return resolve(attempt);  // ambiguous: the payment may exist after all
      attempts.markFailed(attempt.getId());  // CONFIRMED refusal, nothing created: frees the order
      throw e;
    }
    // No catch-all is needed HERE only because at baseline an unhandled exception leaves the
    // attempt IN_FLIGHT, and `join()` reconciles IN_FLIGHT on the next call — the recovery path
    // is the same one. That stops being true the moment you adopt the level-2 model, where a
    // sweep resolves UNKNOWN and never looks at IN_FLIGHT: there, an unclassified error must be
    // mapped to UNKNOWN explicitly. See hardening-concurrency.md §1 ("never terminal").
  }

  /** READY -> the stored URL. Still IN_FLIGHT -> reconcile, so a 409 always follows real progress.
   *  (One extra GET per concurrent click; the cheaper UNKNOWN split: hardening-concurrency.md §1.) */
  private String join(PspAttempt a) {
    return "READY".equals(a.getState()) ? a.getRedirectUrl() : resolve(a);
  }

  /** The only way out of an unresolved attempt: GET by the PERSISTED referenceId. */
  public String resolve(PspAttempt a) {
    var found = psp.findByReferenceId(a.getReferenceId()).stream().findFirst().orElse(null);
    if (found == null) throw new PaymentOutcomeUnknownException(a.getReferenceId()); // stays claimed
    if ("DECLINED".equals(found.state()) || "CANCELLED".equals(found.state())) {
      attempts.markFailed(a.getId());                        // frees the order for a NEW attempt
      throw new CheckoutFailedException(found.errorCode());
    }
    return attempts.promoteReady(a.getId(), a.getOrderId(), found);
  }

  /** Idempotent per (orderId, refundKey); ONLY the inserter of the attempt may POST — referenceId is
   *  NOT an idempotency key at the PSP, so a second POST is a second payout. */
  public PaymentResult refund(long orderId, BigDecimal amount, String currency, String refundKey) {
    var reserved = refunds.reserve(orderId, amount, currency, refundKey);         // committed
    var a = reserved.attempt();
    if (a.getPspPaymentId() != null) return psp.getPayment(a.getPspPaymentId());  // DONE
    // A settled refusal must keep giving the SAME answer. Falling through to reconciliation here
    // would answer "outcome unknown" forever, because no payment was ever created.
    if ("FAILED".equals(a.getState())) throw new RefundFailedException(refundKey);
    if (!reserved.owner()) return reconcileRefund(a);  // someone else's row: reconcile, never POST
    var parentId = orders.findById(orderId).orElseThrow().getPspPaymentId();
    try {
      return refunds.settle(a, psp.createRefund(parentId, amount, currency, a.getReferenceId()));
    } catch (PspTimeoutException e) {
      return reconcileRefund(a);                       // outcome unknown: reconcile the SAME ref
    } catch (PspApiException e) {
      // A confirmed 4xx created nothing. Letting it propagate (or calling it unknown) strands the
      // attempt holding the amount reservation: the remainder becomes unrefundable and every retry
      // of this key answers 409 forever. Hand the reservation back and close the attempt.
      if (e.status() < 500) { refunds.markFailedAndRelease(a); throw e; }
      return reconcileRefund(a);                       // 5xx: the refund may exist after all
    }
  }

  /** Same logical refund => same referenceId, always. A fresh one here is a second payout. */
  private PaymentResult reconcileRefund(RefundAttempt a) {
    return refunds.settle(a, psp.findByReferenceId(a.getReferenceId()).stream().findFirst()
        .orElseThrow(RefundOutcomeUnknownException::new));   // still unresolved: 409, retry later
  }
}

/** Separate bean = the tx proxy really applies, so the attempt is COMMITTED before the PSP call. */
@Service
class PspAttemptStore {
  private final PspAttemptRepository attempts;
  private final OrderRepository orders;
  record Claimed(PspAttempt attempt, boolean owner) {}

  /** Atomic get-or-create; `owner` says whether THIS call inserted the row. */
  @Transactional
  Claimed claim(long orderId) {
    for (int i = 0; i < 2; i++) {      // 2nd pass: the active attempt turned FAILED in between
      var fresh = attempts.insertIfAbsent(orderId, "order-%d-%s".formatted(orderId, UUID.randomUUID()));
      if (fresh.isPresent()) return new Claimed(fresh.get(), true);
      var active = attempts.findActiveByOrderId(orderId);
      if (active.isPresent()) return new Claimed(active.get(), false);
    }
    throw new CheckoutInProgressException();
  }

  /** FAILED sits outside the partial unique index: the order can start a NEW attempt at once. */
  @Transactional void markFailed(long id) { attempts.setState(id, "FAILED"); }

  @Transactional
  String promoteReady(long attemptId, long orderId, PaymentResult p) {
    attempts.promoteReady(attemptId, p.id(), p.redirectUrl());  // every later call reuses this URL
    orders.linkProcessing(orderId, p.id());
    return p.redirectUrl();     // null once the payment moved past CHECKOUT: poll the order then
  }
}

/** Separate bean for the same reason: reserve() must COMMIT before the PSP call, not join it. */
@Service
class RefundAttemptStore {
  private final RefundAttemptRepository refunds;
  private final OrderRepository orders;
  record Reserved(RefundAttempt attempt, boolean owner) {}

  /** Attempt row + amount reservation in ONE commit, BEFORE the PSP call. An already existing row
   *  belongs to another call: reuse ITS referenceId and do NOT reserve the amount again. */
  @Transactional
  Reserved reserve(long orderId, BigDecimal amount, String currency, String refundKey) {
    var ref = "refund-%d-%s".formatted(orderId, UUID.randomUUID());
    var fresh = refunds.insertIfAbsent(orderId, refundKey, ref, amount, currency);
    if (fresh.isEmpty())
      return new Reserved(refunds.findByOrderIdAndRefundKey(orderId, refundKey).orElseThrow(), false);
    if (orders.reserveRefund(orderId, amount) == 0)           // refund only the remainder
      throw new IllegalStateException("refund exceeds remaining refundable amount");
    return new Reserved(fresh.get(), true);
  }

  @Transactional
  PaymentResult settle(RefundAttempt a, PaymentResult r) {
    boolean failed = "DECLINED".equals(r.state()) || "CANCELLED".equals(r.state());
    // State-conditional: the owner and a reconciler can settle the same attempt, and releasing the
    // reservation twice would inflate the refundable amount.
    if (refunds.finish(a.getId(), r.id(), failed ? "FAILED" : "DONE") == 1 && failed)
      orders.releaseRefund(a.getOrderId(), a.getAmount());      // confirmed failure only
    return r;
  }

  /** Confirmed 4xx: nothing was created, so give the reservation back and close the attempt.
   *  State-conditional for the same reason as settle(). */
  @Transactional
  void markFailedAndRelease(RefundAttempt a) {
    if (refunds.finish(a.getId(), null, "FAILED") == 1)
      orders.releaseRefund(a.getOrderId(), a.getAmount());
  }
}

interface PspAttemptRepository extends JpaRepository<PspAttempt, Long> {
  // Hibernate 6 runs INSERT ... RETURNING as a native query. On older versions: save() and catch
  // DataIntegrityViolationException, then re-read the active row — same owner/loser semantics.
  @Query(value = """
      insert into psp_attempt (order_id, reference_id, state) values (:orderId, :ref, 'IN_FLIGHT')
       on conflict (order_id) where state in ('IN_FLIGHT','READY') do nothing
       returning *""", nativeQuery = true)
  Optional<PspAttempt> insertIfAbsent(long orderId, String ref);
  @Query("select a from PspAttempt a where a.orderId = :orderId and a.state in ('IN_FLIGHT','READY')")
  Optional<PspAttempt> findActiveByOrderId(long orderId);     // FAILED rows are never returned
  @Modifying(clearAutomatically = true)
  @Query("update PspAttempt a set a.state = :state where a.id = :id")
  void setState(long id, String state);
  @Modifying(clearAutomatically = true)
  @Query("update PspAttempt a set a.state = 'READY', a.pspPaymentId = :paymentId,"
       + " a.redirectUrl = :redirectUrl where a.id = :id")
  void promoteReady(long id, String paymentId, String redirectUrl);
  long countByOrderId(long orderId);
}

interface RefundAttemptRepository extends JpaRepository<RefundAttempt, Long> {
  @Query(value = """
      insert into psp_refund_attempt (order_id, refund_key, reference_id, amount, currency, state,
                                      created_at)
       values (:orderId, :key, :ref, :amount, :ccy, 'IN_FLIGHT', now())
       on conflict (order_id, refund_key) do nothing
       returning *""", nativeQuery = true)
  Optional<RefundAttempt> insertIfAbsent(long orderId, String key, String ref, BigDecimal amount,
      String ccy);
  Optional<RefundAttempt> findByOrderIdAndRefundKey(long orderId, String refundKey);
  @Modifying   // only from a non-terminal state: settling twice must not release the amount twice
  @Query("update RefundAttempt r set r.state = :state, r.pspPaymentId = :paymentId"
       + " where r.id = :id and r.state = 'IN_FLIGHT'")
  int finish(long id, String paymentId, String state);
}
```

## 5. Tests (JUnit 5 + WireMock + MockMvc)

```java
/** Stub helpers: every API response is wrapped in {timestamp, status, result}. */
abstract class PspStubs {
  static ResponseDefinitionBuilder one(String result) {
    return okJson("{\"status\":200,\"result\":" + result + "}"); }
  static ResponseDefinitionBuilder many(String... rs) {
    return okJson("{\"status\":200,\"result\":[" + String.join(",", rs) + "]}"); }
  static final String CHECKOUT_1 =
      "{\"id\":\"pay1\",\"state\":\"CHECKOUT\",\"redirectUrl\":\"https://checkout.example/pay1\"}";
  static final String REFUND_5 = "{\"id\":\"rf9\",\"state\":\"COMPLETED\",\"paymentType\":\"REFUND\","
      + "\"amount\":5.00,\"currency\":\"GBP\"}";
}

class PspClientTest extends PspStubs {
  @RegisterExtension static WireMockExtension psp =
      WireMockExtension.newInstance().options(wireMockConfig().dynamicPort()).build();
  PspClient client = new PspClient(psp.baseUrl(), "sandbox-key");   // never production credentials
  CreatePaymentRequest deposit(String ref) { return new CreatePaymentRequest("DEPOSIT",
      new BigDecimal("10.01"), "GBP", ref, null, null, null, null, null); }

  @Test void successfulDeposit() {
    psp.stubFor(post("/api/v1/payments").willReturn(one(CHECKOUT_1)));
    assertEquals("CHECKOUT", client.createDeposit(deposit("order-1-a")).state());  // created, NOT paid
    psp.verify(postRequestedFor(urlEqualTo("/api/v1/payments"))
        .withHeader("Authorization", equalTo("Bearer sandbox-key"))
        .withRequestBody(matchingJsonPath("$.paymentType", equalTo("DEPOSIT"))));
  }
  @Test void declineIsHttp200WithDeclinedState() {
    psp.stubFor(get(urlPathEqualTo("/api/v1/payments/pay2")).willReturn(
        one("{\"id\":\"pay2\",\"state\":\"DECLINED\",\"errorCode\":\"4.01\"}")));
    assertEquals("4.01", client.getPayment("pay2").errorCode());    // a decline is not an exception
  }
  @Test void timeoutMeansUnknownOutcome() {
    psp.stubFor(post("/api/v1/payments").willReturn(aResponse().withFixedDelay(40_000)));
    assertThrows(PspTimeoutException.class, () -> client.createDeposit(deposit("order-9-a")));
  }
}

@SpringBootTest @AutoConfigureMockMvc
class PspWebhookControllerTest {
  @Autowired MockMvc mvc; @Autowired OrderRepository orders; @Autowired WebhookEventRepository events;
  static final String KEY = "test-signing-key";       // == psp.signing-key in test properties
  static final String BODY = """
      {"id":"pay1","referenceId":"order-1-a","state":"COMPLETED","amount":10.01,"currency":"GBP"}""";
  // EARLY: referenceId is the ATTEMPT's reference, not order_ref, and psp_payment_id is not stored
  // yet — the real create-payment/webhook race.
  static final String EARLY = """
      {"id":"pay7","referenceId":"order-1-9f2c","state":"COMPLETED","amount":10.01,"currency":"GBP"}""";

  static String sign(String b) throws Exception {
    var mac = Mac.getInstance("HmacSHA256");
    mac.init(new SecretKeySpec(KEY.getBytes(UTF_8), "HmacSHA256"));
    return HexFormat.of().formatHex(mac.doFinal(b.getBytes(UTF_8)));   // base64 must pass too
  }
  ResultActions send(String body, String sig) throws Exception { return mvc.perform(
      MockMvcRequestBuilders.post("/webhooks/psp").contentType(MediaType.APPLICATION_JSON)
          .header("Signature", sig).content(body.getBytes(UTF_8))); }
  ResultActions send(String body) throws Exception { return send(body, sign(body)); }
  OrderEntity order() { return orders.findByOrderRef("order-1-a").orElseThrow(); }

  @Test void validWebhookMarksOrderPaidWithPayloadAmount() throws Exception {
    send(BODY).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("applied"));
    assertEquals(OrderStatus.PAID, order().getStatus());
    assertEquals(new BigDecimal("10.01"), order().getPaidAmount());   // payload, not the request
  }
  @Test void invalidSignatureRejectedAndOrderUntouched() throws Exception {
    send(BODY, "deadbeef").andExpect(status().isUnauthorized());
    assertEquals(OrderStatus.AWAITING_PAYMENT, order().getStatus());
  }
  @Test void duplicateWebhookIsANoOp() throws Exception {
    send(BODY).andExpect(status().isOk());
    long version = order().getVersion();
    send(BODY).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("duplicate"));
    assertEquals(version, order().getVersion());
  }
  /** The receipt must NOT be marked processed, or the redelivery is dismissed as a duplicate. */
  @Test void webhookArrivingBeforeTheOrderLinkIsNotSwallowed() throws Exception {
    send(EARLY).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("unknown_payment"));
    assertNull(events.findByPaymentIdAndState("pay7", "COMPLETED").orElseThrow().getProcessedAt());
    orders.linkPayment("order-1-a", "pay7");          // the create-payment response finally lands
    send(EARLY).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("applied"));
    assertEquals(OrderStatus.PAID, order().getStatus());             // redelivery wins
  }
}

// Needs a REAL PostgreSQL (Testcontainers, not H2): the guarantees rest on ON CONFLICT, partial
// indexes and committed transactions, which no in-memory DB reproduces. The stores are autowired as
// PROXIES, so this also exercises the wiring that actually commits.
@SpringBootTest
class CheckoutServiceConcurrencyTest extends PspStubs {
  @RegisterExtension static WireMockExtension psp = WireMockExtension.newInstance()
      .options(wireMockConfig().dynamicPort()).build();
  @Autowired CheckoutService checkout; @Autowired PspAttemptRepository attempts;
  @Autowired RefundAttemptStore refundStore; @Autowired RefundAttemptRepository refunds;
  @Autowired OrderRepository orders;
  static final BigDecimal TEN = new BigDecimal("10.01"), FIVE = new BigDecimal("5.00");
  static final String POST_PAYMENTS = "/api/v1/payments";

  OrderEntity order() { return orders.findById(1L).orElseThrow(); }
  void stubFind(String ref, String... results) {         // reconciliation lookup by referenceId
    var m = get(urlPathEqualTo(POST_PAYMENTS));
    psp.stubFor((ref == null ? m : m.withQueryParam("referenceId.eq", equalTo(ref)))
        .willReturn(many(results)));
  }
  /** Two callers, one instant. `deferred` = the exception the loser is allowed to fail with. */
  int race(Callable<?> body, Class<? extends RuntimeException> deferred) throws Exception {
    var pool = Executors.newFixedThreadPool(2);
    var gate = new CountDownLatch(1);
    var futures = Stream.generate(() -> pool.submit(() -> { gate.await(); return body.call(); }))
        .limit(2).toList();
    gate.countDown();
    int done = 0;
    for (var f : futures) {
      try { assertNotNull(f.get()); done++; }
      catch (ExecutionException e) { assertInstanceOf(deferred, e.getCause()); done++; }
    }
    return done;
  }

  @Test void concurrentStartCheckoutCreatesExactlyOnePayment() throws Exception {
    psp.stubFor(post(POST_PAYMENTS).willReturn(one(CHECKOUT_1)));
    stubFind(null);                             // the loser reconciles and finds nothing yet -> 409
    assertEquals(2, race(() -> checkout.startCheckout(1L, TEN, "GBP"),
                         PaymentOutcomeUnknownException.class));
    assertEquals(1, attempts.countByOrderId(1L));                    // ONE referenceId
    psp.verify(1, postRequestedFor(urlEqualTo(POST_PAYMENTS)));       // ONE payment created
  }

  /** Baseline recovery path: no UNKNOWN state and no sweep job — the NEXT call reconciles. */
  @Test void checkoutTimeoutThenEmptyReconciliationRecoversOnALaterCall() {
    psp.stubFor(post(POST_PAYMENTS).willReturn(aResponse().withFixedDelay(40_000)));
    stubFind(null);                                                  // not visible yet
    assertThrows(PaymentOutcomeUnknownException.class, () -> checkout.startCheckout(2L, TEN, "GBP"));
    var stuck = attempts.findActiveByOrderId(2L).orElseThrow();       // still claimed, not a dead end
    psp.resetAll();
    stubFind(stuck.getReferenceId(),
        "{\"id\":\"pay2\",\"state\":\"CHECKOUT\",\"redirectUrl\":\"https://checkout.example/pay2\"}");
    assertEquals("https://checkout.example/pay2", checkout.startCheckout(2L, TEN, "GBP"));
    assertEquals("READY", attempts.findActiveByOrderId(2L).orElseThrow().getState());
    psp.verify(0, postRequestedFor(urlEqualTo(POST_PAYMENTS)));       // ONE referenceId, no re-POST
  }

  @Test void aFailedAttemptLetsANewCheckoutStart() {
    psp.stubFor(post(POST_PAYMENTS)                    // confirmed 4xx: nothing was created
        .willReturn(aResponse().withStatus(400).withBody("{\"status\":400,\"errorCode\":\"2.01\"}")));
    assertThrows(PspApiException.class, () -> checkout.startCheckout(3L, TEN, "GBP"));
    assertTrue(attempts.findActiveByOrderId(3L).isEmpty());  // FAILED is outside the partial index
    psp.resetAll();
    psp.stubFor(post(POST_PAYMENTS).willReturn(one(
        "{\"id\":\"pay3\",\"state\":\"CHECKOUT\",\"redirectUrl\":\"https://checkout.example/pay3\"}")));
    assertEquals("https://checkout.example/pay3",            // a NEW attempt, a NEW referenceId
        checkout.startCheckout(3L, TEN, "GBP"));
    assertEquals(2, attempts.countByOrderId(3L));
  }

  @Test void refundTimeoutThenRetryDoesNotRefundTwice() {
    psp.stubFor(post(POST_PAYMENTS).willReturn(aResponse().withFixedDelay(40_000)));
    stubFind(null);                                                  // not visible yet
    assertThrows(RefundOutcomeUnknownException.class, () -> checkout.refund(1L, FIVE, "GBP", "rk-1"));
    var stuck = refunds.findByOrderIdAndRefundKey(1L, "rk-1").orElseThrow();
    assertEquals("IN_FLIGHT", stuck.getState());                     // the attempt row SURVIVED
    assertEquals(FIVE, order().getRefundedAmount());                 // and so did the reservation
    // The PSP had processed it all along. The retry must reconcile the SAME referenceId.
    psp.resetRequests();
    stubFind(stuck.getReferenceId(), REFUND_5.replace("rf9", "rf1"));
    assertEquals("COMPLETED", checkout.refund(1L, FIVE, "GBP", "rk-1").state());
    psp.verify(0, postRequestedFor(urlEqualTo(POST_PAYMENTS)));      // no second payout
    assertEquals(FIVE, order().getRefundedAmount());                 // reserved once, not twice
  }

  @Test void concurrentRefundsWithTheSameKeyPostExactlyOnce() throws Exception {
    psp.stubFor(post(POST_PAYMENTS).willReturn(one(REFUND_5)));
    stubFind(null);                                   // the non-owner reconciles, finds nothing yet
    psp.stubFor(get(urlPathEqualTo("/api/v1/payments/rf9")).willReturn(one(REFUND_5)));
    assertEquals(2, race(() -> checkout.refund(1L, FIVE, "GBP", "rk-9"),
                         RefundOutcomeUnknownException.class));
    psp.verify(1, postRequestedFor(urlEqualTo(POST_PAYMENTS)));      // ONE payout, never two
    assertEquals(FIVE, order().getRefundedAmount());                 // reserved once
  }

  @Test void crashBetweenAnAcceptedRefundPostAndSettleDoesNotPostAgain() {
    // The crash state, reproduced through the PROXIED store: attempt committed IN_FLIGHT, amount
    // reserved, the PSP already holds the payment, nothing settled.
    var a = refundStore.reserve(1L, FIVE, "GBP", "rk-2").attempt();
    stubFind(a.getReferenceId(), REFUND_5.replace("rf9", "rf2"));
    assertEquals("rf2", checkout.refund(1L, FIVE, "GBP", "rk-2").id());
    psp.verify(0, postRequestedFor(urlEqualTo(POST_PAYMENTS)));      // reconciled, not re-POSTed
    assertEquals("DONE", refunds.findByOrderIdAndRefundKey(1L, "rk-2").orElseThrow().getState());
    assertEquals(FIVE, order().getRefundedAmount());                 // reserved once
  }
}
```

The replay-job test (a stale `AUTHORIZED` receipt closed as `superseded`) exercises the hardened
variant: `references/hardening-concurrency.md` §2.
