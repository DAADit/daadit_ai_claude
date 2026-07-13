# DAADit AI — Claude (Anthropic) Provider

Adds **Anthropic Claude** as an additional LLM provider for Odoo 19 Enterprise's
built-in `ai` module, alongside OpenAI (ChatGPT) and Google (Gemini).

## What it does

### Chat completions (`ai.agent`)

- Adds Claude models to the `ai.agent` LLM Model dropdown:
  - `claude-opus-4-7` — Claude Opus 4.7 (most capable)
  - `claude-sonnet-4-6` — Claude Sonnet 4.6 (balanced)
  - `claude-haiku-4-5-20251001` — Claude Haiku 4.5 (fastest / cheapest)
  - `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-haiku-4-5` (older snapshots)
- Routes any agent whose `llm_model` is a Claude option to
  `https://api.anthropic.com/v1/messages` instead of Odoo IAP.
- Maps the agent's response style to a Claude `temperature` unless the agent's
  `daadit_claude_temperature_active` flag is set (in which case the per-agent
  override wins).

### Tool calling / Topics (`ai.topic` + server actions)

- Passes the agent's tools through to Claude in Anthropic's `input_schema`
  envelope.
- Builds tool definitions for the standard 10 Odoo AI tools (Search, Read
  Group, Get Fields, Open Menu * etc.) from the schemas in
  `services/tool_dispatch.py`.
- Runs Anthropic's `tool_use → tool_result` loop until Claude returns a final
  text response or a 6-iteration cap is hit.
- Per-tool security gates:
  - **Allowed-models / blocked-models** lists on the agent record.
  - **Field-level blocklist** (`daadit_field_blocklist`) for PII scrubbing on
    tool results and rejection of filter / groupby / aggregate clauses that
    reference blocked fields.

### Embeddings — **NOT supported**

Anthropic does not offer an embeddings API. RAG / Sources should keep running
on whichever embedding provider Odoo already supports in your install. This
module does **not** add Claude to the `ai.embedding.embedding_model` selection.

### Usage tracking + GDPR

- Every chat call writes a row to `daadit_ai_claude.usage` with token counts,
  iteration count, tools usage, and an estimated cost (USD per 1M tokens).
- Per-user record rule: internal users see only their own rows; settings users
  see everything in their company.
- Partner form gets an **Erase Claude usage (GDPR)** button (Settings users
  only) that anonymises the `user_id` link on usage rows while keeping the
  aggregate cost figures.

### Settings UI

- Adds an **Anthropic** provider block under **Settings → General Settings →
  AI**, with a coral accent border so it's unmistakably identified as the
  Claude provider.
- Toggle → key field → link to `console.anthropic.com`.
- Extra advanced fields: base URL, API version, request timeout.
- All values stored in `ir.config_parameter` under the `daadit_ai_claude.*`
  namespace; only visible to `base.group_system`.

## Install

This is a custom Odoo module — install via Odoo.sh or a local Odoo build, not
through XML-RPC.

1. Push the `daadit_ai_claude` folder into your Odoo.sh repo, e.g. under
   `enterprise_custom_addons/` or wherever your custom addons live.
2. Commit and push to your dev / staging branch first.
3. On Odoo.sh, watch the build. Once green, open the database and update the
   apps list, then install **DAADit AI — Claude (Anthropic) Provider**.
4. Open **Settings → General Settings → AI**, toggle **Use your own Anthropic
   account**, paste the key from <https://console.anthropic.com/settings/keys>,
   save.
5. Edit (or create) an `ai.agent`, set **LLM Model** to one of the Claude
   options (e.g. `claude-sonnet-4-6`), save, and try a prompt.

## ⚠️ Recovery: "No provider found for the selected model" at registry load

If a build ever fails with this in the log:

```
File "/home/odoo/src/enterprise/ai/utils/llm_providers.py", line 72, in get_provider
    raise UserError(env._("No provider found for the selected model"))
```

…during loading of `enterprise/ai/data/ai_agent_data.xml`, the database has
a Claude value in `ai_agent.llm_model` that stock can't process. Stock's
data load runs *before* our `_inherit` extensions are merged into
`ai.agent`, so our `_get_provider` override isn't yet in effect.

**Unblock**: connect to the DB and run

```sql
UPDATE ai_agent
   SET llm_model = 'gpt-4o'
 WHERE llm_model LIKE 'claude-%';
```

…then trigger a rebuild. After this, the module's `uninstall_hook` and
`pre_init_hook` keep the DB from getting stuck on the next install/uninstall
cycle.

## Configuration reference

All values stored in `ir.config_parameter`:

| Key | Default | Notes |
| --- | --- | --- |
| `daadit_ai_claude.claude_key_enabled` | `False` | Master toggle |
| `daadit_ai_claude.claude_key` | — | API key (group_system) |
| `daadit_ai_claude.claude_base_url` | `https://api.anthropic.com/v1` | Allowlisted; ICP `daadit_ai_claude.allowed_base_url_hosts` extends |
| `daadit_ai_claude.claude_api_version` | `2023-06-01` | `anthropic-version` header |
| `daadit_ai_claude.claude_timeout` | `60` | Seconds, range 1–600 |
| `daadit_ai_claude.allowed_base_url_hosts` | — | Comma-separated extra hosts |
| `daadit_ai_claude.log_tool_results` | `False` | Persist tool args + results to `ir.logging` (PII risk) |
| `daadit_ai_claude.diag_trace_user_errors` | `False` | Log stack of every `UserError` matching the target markers |
| `daadit_ai_claude.price.<model>.input` | snapshot | USD per 1M input tokens |
| `daadit_ai_claude.price.<model>.output` | snapshot | USD per 1M output tokens |

## Compatibility

Designed for Odoo 19 Enterprise. The provider plumbing uses defensive
registry-patching so it only intercepts calls whose `llm_model` starts with
`claude-` — other AI provider modules can be installed side-by-side without
conflict.

## License

LGPL-3 — see top of each source file.
