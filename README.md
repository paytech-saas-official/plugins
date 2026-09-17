# pay.tech plugins for coding agents

A plugin marketplace: add it once, and your agent can install the plugins below and
receive updates as they are released. Works with **Claude Code** and **Codex** —
the payload is identical, each host reads its own manifest.

## Plugins

| Plugin | What it does |
|--------|--------------|
| [`paytech`](plugins/paytech/README.md) | Integrate pay.tech payments into an existing application: deposits, withdrawals, refunds, card tokens, recurring, Hosted Fields, Apple Pay / Google Pay, webhooks — implemented in your codebase and verified against the sandbox. |

## Add the marketplace

Claude Code:

```
/plugin marketplace add paytech-saas-official/plugins
/plugin install paytech@paytech
/reload-plugins
```

Codex:

```
codex plugin marketplace add paytech-saas-official/plugins
codex plugin add paytech@paytech
```

Credentials, the first task to give the agent, and how updates work are in the
plugin's own page: [`plugins/paytech/README.md`](plugins/paytech/README.md).

## Legal and support

- [LICENSE](LICENSE) — MIT
- [SECURITY.md](SECURITY.md) — how to report a vulnerability
- [PRIVACY.md](PRIVACY.md) — what this plugin does and does not collect
- [TERMS.md](TERMS.md) — terms of use
- Integration support: https://github.com/paytech-saas-official/plugins/issues
