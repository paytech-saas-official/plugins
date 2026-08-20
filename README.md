# PSP Payments Skill — Install & Use

An Agent Skill for **Claude Code** and **OpenAI Codex** that integrates a
merchant application with the pay.tech PSP payment API.

## 1. Install

**As a plugin (recommended).** Updates reach you this way; a copied folder never
changes on its own.

Claude Code:

```
/plugin marketplace add paytech-saas-official/plugins
/plugin install psp-payments@paytech
/reload-plugins
```

Codex:

```
codex plugin marketplace add paytech-saas-official/plugins
codex plugin add psp-payments@paytech
```

**Or copy the folder**, if you would rather pin one version by hand. Copy
`psp-payments/` into your project's skills directory:

- **Claude Code:** `.claude/skills/psp-payments/`
- **OpenAI Codex:** `.agents/skills/psp-payments/`

Verify discovery before relying on it: in Claude Code run `/skills` — it lists
`psp-payments` for a copied folder and `psp-payments:psp-payments` for the plugin
(plugin skills are namespaced by the plugin's name). In Codex, ask it to list its
available skills.

You do not need to tell the agent to read the skill: it activates on a task that
mentions pay.tech, payments, refunds, payouts or payment webhooks.

## 2. Keep it up to date

In Claude Code, auto-update is **off by default** for third-party marketplaces —
it does not update a plugin you did not ask it to. Turn it on once per machine:

1. run `/plugin`
2. open **Marketplaces** and select `paytech`
3. choose **Enable auto-update**

Claude Code then refreshes the marketplace in the background shortly after a
session starts, and the new version loads on the next launch. To update on demand
instead: `/plugin update psp-payments@paytech`. In Codex, refresh
the catalog with `codex plugin marketplace upgrade paytech`.

For a whole team, declare the marketplace in the project's
`.claude/settings.json`, so everyone who trusts the repository is offered it with
auto-update already on:

```json
{
  "extraKnownMarketplaces": {
    "paytech": {
      "source": { "source": "github", "repo": "paytech-saas-official/plugins" },
      "autoUpdate": true
    }
  }
}
```

## 3. Provide sandbox credentials

The base URL is fixed (sandbox default below; see the skill's `wl-config.md`).
Your shop's `PSP_API_KEY` and `PSP_SIGNING_KEY` come from pay.tech (merchant
back office / support). Once you have the values, make them available as
environment variables — pick one:

**A. Export in the terminal** where you run the agent (and later the app) — no
project file needed:

```
export PSP_API_URL=https://engine-sandbox.pay.tech
export PSP_API_KEY=<your shop API key>
export PSP_SIGNING_KEY=<your shop signing key>
```

**B. A local env file:** copy the skill's `assets/env.example` to a new `.env` in
your project root, put the real values there, add `.env` to `.gitignore`, and
load it (your framework's dotenv, or `source .env`).

Never hardcode or commit credentials. Sandbox and production keys differ.

## 4. Use it

Give the agent a task naming the operations, method and flow, for example:

> Integrate card payments via pay.tech: operations DEPOSIT and REFUND, method
> BASIC_CARD, flow hosted payment page (redirect).

The agent will analyze your project, choose the right flow, implement the backend
client, webhook handling, state mapping, idempotency, configuration and tests,
and smoke-test against the sandbox — adapting to your language, framework and
existing order model (it maps onto your existing order statuses, not new ones).

## 5. Webhooks

The PSP delivers the final payment result by webhook. In the PSP back office, set
the shop's webhook URL to a public HTTPS URL that reaches your backend's webhook
endpoint.

## Notes

- Start on the **sandbox**; move to production only after it works there.
- The agent maps payment states onto your existing order statuses. If your enum
  cannot represent a state it needs, it will ask rather than invent one.
- A browser signal never means "paid": only a verified webhook or a status check
  does. The skill implements it that way — do not ask it to shortcut this.
