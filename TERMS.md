# Terms of Use

## What you are getting

Integration guidance for the pay.tech payment API, packaged so a coding agent
can follow it. The software and documents in this repository are provided under
the MIT License (see `LICENSE`), including its disclaimer of warranties.

## What it is not

- **Not the API contract.** The authoritative description of the pay.tech API
  is pay.tech's own published specification and your agreement with
  pay.tech. Where this material and the live API disagree, the live API wins,
  and we would like to hear about it.
- **Not compliance advice.** Card-data handling, PCI DSS scope, strong customer
  authentication, tax and consumer-protection obligations remain yours. The
  material is written to keep card data out of your systems where the API allows
  it, which reduces scope but does not certify anything.
- **Not a substitute for review.** An agent following these instructions produces
  code you are responsible for. Test it, review it, and run it against the sandbox
  before it touches real money.

## Your responsibilities

- Keep API keys and signing keys server-side, and rotate any that leak.
- Start on the sandbox; move to production when it works there.
- Use the material for integrating with pay.tech, not for circumventing its
  controls, probing other merchants' shops or generating load against the API.

## Changes

Both this document and the plugin are versioned in this repository; the release
history is visible in its commits and tags. Continuing to use a newer version
means accepting the terms shipped with it.

## Contact

**https://github.com/paytech-saas-official/plugins/issues**
