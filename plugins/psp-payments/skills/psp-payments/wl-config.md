# WL PSP Configuration

This file holds the values that are specific to this PSP: name, endpoints,
credentials configuration and supported payment methods.
Everything under `references/` is generic and refers to these values through
`$PSP_*` environment placeholders, so this is the one file to read when you need
a concrete URL or the name of an environment variable.

## Identity

| Key | Value |
|-----|-------|
| PSP name | pay.tech |
| Skill instance version | 1.9.2 |
| Supported WL API version | 1.0.341 (OpenAPI 3.1.1, bundled in `openapi/`) |

## Endpoints

| Purpose | URL |
|---------|-----|
| Production API base (`PSP_API_URL`) | `https://engine.pay.tech` |
| Sandbox API base (`PSP_API_URL`) | `https://engine-sandbox.pay.tech` |
| Gateway API base path | `/api/v1` |
| Merchant API base path | `/merchant-api/v1` |
| Public documentation | `https://paytech.api-docs.info/` |
| Gateway API reference (OpenAPI/Redoc) | `https://paytech.api-docs.info/gateway-api-reference/` |
| Merchant API reference (OpenAPI/Redoc) | `https://paytech.api-docs.info/merchant-api-reference/` |
| Secondary documentation mirror | `https://sandbox.api-docs.info/` — same doc set, **not** always in sync |

The two documentation hosts publish independently and have been observed drifting
in **both** directions at once: a page present on one and missing on the other,
while the other carried the newer OpenAPI version. Treat `paytech.api-docs.info`
as authoritative, but when something you expect to be documented is missing
there, check the mirror before concluding the feature does not exist.

## Authentication configuration

| Credential | Env var | Used for |
|------------|---------|----------|
| Shop API Key | `PSP_API_KEY` | `Authorization: Bearer <key>` on every API call |
| Shop Signing Key | `PSP_SIGNING_KEY` | HMAC-SHA256 verification of **inbound webhooks** only — outbound requests authenticate with the API Key alone (see `references/authentication.md`) |

Sandbox and production keys are **different**. Keys are issued per shop in the
merchant back office / by PSP support.

## Support, legal and distribution

These feed the generated public-facing documents (LICENSE, SECURITY.md, PRIVACY.md,
TERMS.md, the plugin's README). A value still written as `<...>` is treated as
unresolved: the build then refuses to call itself publishable, so a placeholder can
no longer reach a merchant unnoticed.

| Key | Value |
|-----|-------|
| Copyright holder (legal entity) | `paytech ltd` |
| Plugin marketplace repository | `paytech-saas-official/plugins` — where merchants add the marketplace from |
| Integration support | `https://github.com/paytech-saas-official/plugins/issues` — questions about the API and the integration |
| Security contact | `https://github.com/paytech-saas-official/plugins/security/advisories/new` — vulnerability reports about this skill or the API |

## Supported payment methods

The concrete set of payment methods is configured per shop/terminal by the PSP.
Methods visible in the public API include: `BASIC_CARD`, `APPLEPAY`,
`GOOGLEPAY`, `BANKTRANSFER`, `PIX`, and others — confirm the enabled list for
your shop with PSP support or via a sandbox test. Do not assume a method is
available without checking.

## WL-specific notes

- The "skip redirection to checkout page" flow requires prior approval by PSP
  support (see `references/deposit-and-withdrawal.md`).
- Hosted Fields SDK script host and checkout page domain are environment- and
  brand-specific; take them from the PSP's onboarding materials.
