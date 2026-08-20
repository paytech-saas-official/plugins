# Privacy

## Short version

This plugin collects nothing. It has no telemetry, no analytics and no server of
its own.

## What it actually is

A folder of Markdown instructions, OpenAPI specifications, example payloads and
two Python helper scripts. Installing it copies those files to your machine. From
then on, everything happens locally, inside your own coding agent.

## Where data goes

- **Your code and prompts** stay between you and whichever agent you run (Claude
  Code or Codex). That relationship is governed by your agreement with that
  vendor, not by us — we receive nothing from it.
- **The helper scripts** call the pay.tech API directly from your machine,
  authenticating with credentials **you** provide through environment variables.
  Requests go to pay.tech; nothing is routed through a third party.
- **Credentials** are never written into the skill, never logged by it and never
  transmitted anywhere except to the pay.tech API endpoint you configured.
- **Payment data** that flows through your integration is covered by your existing
  agreement with pay.tech. This plugin does not add a processor, an
  intermediary or a storage location.

## Updates

When you install from the marketplace, your agent fetches this repository from
GitHub. GitHub sees that request, as it would for any public repository clone. We
receive no report of who installed what.

## Contact

Questions about this document: **https://github.com/paytech-saas-official/plugins/issues**
