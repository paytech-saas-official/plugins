# Security Policy

## Reporting a vulnerability

Report security issues here: **https://github.com/paytech-saas-official/plugins/security/advisories/new**. Please do not open a public
issue for anything exploitable — that tells everyone else first.

Include what you did, what you expected and what happened. If the issue concerns
the pay.tech API rather than this plugin, the same channel routes it onward.

## What this plugin is, in security terms

It ships **instructions and API reference material** — Markdown, the JSON API
specs, two example request bodies and an icon — and no executable code. It runs
no server, opens no network listener and phones nothing home. Two consequences
worth knowing:

- The agent makes sandbox calls while it integrates: from your machine, to the
  pay.tech API only, with credentials you put in your own environment. Every
  call is visible in the session; nothing runs unattended.
- An AI agent following these instructions writes code in your repository. Review
  its output the way you review a colleague's pull request, especially the parts
  that touch money, webhook signature verification and order state.

## Never send us credentials

No support case, issue or vulnerability report needs your API key or signing key.
If you have pasted one anywhere, rotate it: for keys issued by pay.tech,
removing the message is not enough.

## Scope

In scope: anything in this repository — the skill's instructions, the reference
material, the plugin manifests and the marketplace catalogs.

Out of scope: your own integration code, and the pay.tech API itself (report
those through the same channel; they are handled by a different team).
