# DAADit AI — Claude (Anthropic) Provider

Adds **Anthropic Claude** as an additional LLM provider for Odoo 19 Enterprise's
built-in `ai` module, alongside OpenAI (ChatGPT) and Google (Gemini).

## What it does

### Chat completions (`ai.agent`)

- Adds Claude models to the `ai.agent` LLM Model dropdown:
  - `claude-opus-4-7` — Claude Opus 4.7 (most capable)
  - `claude-sonnet-4-6` — Claude Sonnet 4.6 (balanced)
  - `claude-haiku-4-5-20251001` — Claude Haiku 4.5 (fastest / cheapest)
  - `claude-opus-4-5`, `claude-sonnet-4-5`, `claude-haiku-4-5` (older snapshots
    kept for compatibility with existing agent records).
- Routes agents whose `llm_model` is a Claude option to
  `https://api.anthropic.com/v1/messages` using your Anthropic key.

### Tool calling / Topics (`ai.topic` + server actions)

- All ten standard Odoo AI tools (Search, Read Group, Get Fields, Open Menu *)
  are exposed to Claude with typed JSON Schema definitions.
- Runs Anthropic's `tool_use → tool_result` loop until Claude returns a final
  text response or a 6-iteration safety cap is hit.
- Per-tool security gates:
  - **Allowed-models / blocked-models** lists on the agent record.
  - **Field-level blocklist** (`daadit_field_blocklist`) for PII scrubbing on
    tool results and rejection of filter / groupby / aggregate clauses that
    reference blocked fields.

### Embeddings — **NOT supported by Anthropic**

Anthropic does not offer an embeddings API. The module deflects the
embedding-model lookup to `text-embedding-3-small` so RAG / Sources keep
running through whichever embedding provider Odoo already has configured
(OpenAI direct key or IAP). Chat runs on Anthropic; embeddings run on the
existing provider — no changes to your Sources setup required.

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
  AI** next to the other AI provider blocks.
- Toggle → key field → link to `console.anthropic.com`.
- Advanced fields: base URL, API version, request timeout.
- All values stored in `ir.config_parameter` under the `daadit_ai_claude.*`
  namespace; only visible to `base.group_system`.

## Install

1. Copy the `daadit_ai_claude` folder into your Odoo addons path (Odoo.sh
   custom-addons repo, `extra_addons/`, or wherever your install expects
   custom modules to live).
2. Restart the Odoo server (or trigger an Odoo.sh build).
3. Open **Apps**, update the apps list, and install
   **DAADit AI — Claude (Anthropic) Provider**.
4. Open **Settings → General Settings → AI**, toggle **Use your own Anthropic
   account**, paste the key from <https://console.anthropic.com/settings/keys>,
   save.
5. Edit (or create) an `ai.agent`, set **LLM Model** to one of the Claude
   options (e.g. `claude-sonnet-4-6`), save, and try a prompt.

## Recovery: "No provider found for the selected model"

If an Odoo build ever fails with this in the log:

```
File "/home/odoo/src/enterprise/ai/utils/llm_providers.py", line 72, in get_provider
    raise UserError(env._("No provider found for the selected model"))
```

…during loading of `enterprise/ai/data/ai_agent_data.xml`, the database has
a Claude value in `ai_agent.llm_model` that stock can't process. Stock's data
load runs *before* the `_inherit` extensions are merged into `ai.agent`, so
the `_get_provider` override isn't yet active.

**Unblock**: connect to the DB and run

```sql
UPDATE ai_agent
   SET llm_model = 'gpt-4o'
 WHERE llm_model LIKE 'claude-%';
```

…then trigger a rebuild. The module's `uninstall_hook` and `pre_init_hook`
keep the DB from getting stuck on subsequent install/uninstall cycles.

## Web research (`_daadit_claude_research`)

`ai.agent._daadit_claude_research(prompt, max_uses=5)` runs the agent as
a one-shot Claude call with Anthropic's server-side **web_search** tool
and returns the answer text (with source URLs when the prompt asks for
them). This gives orchestrating agents real, up-to-date online research
instead of model-memory guesses — e.g. a marketing manager agent calling
a research agent ("find the current hot topics for a blog post on X").
The called agent's `system_prompt` frames the role; `max_uses` caps how
many searches Claude may run. Requires the Anthropic key to be enabled.

## Dynamic model list (auto-sync)

The Claude models offered in the agent's **LLM Model** dropdown are no
longer a hardcoded list. They live in the `daadit.ai.claude.model`
registry, which is refreshed from the Anthropic `GET /v1/models` API:

* **Daily** — a scheduled action (`ir.cron`) syncs the list, so a newly
  released Claude model appears on its own without a redeploy.
* **On demand** — *Settings → AI → Refresh Claude model list*, or the
  *Refresh from Anthropic* button under **AI → Configuration → Claude
  Models**.

A built-in seed keeps the dropdown usable before the first sync (fresh
install, no key yet, or API unreachable). Untick a row's *Active* flag
to hide a model without breaking agents that still reference it. The
sync only runs when the Anthropic key is enabled.

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

Designed for **Odoo 19 Enterprise**. The provider plumbing uses defensive
patterns so it only intercepts calls whose `llm_model` starts with `claude-`
— other AI provider modules can be installed side-by-side without conflict.

## License

LGPL-3 — see the `LICENSE` file. Anthropic API usage is billed by Anthropic
directly to the customer; this module only routes the calls.

## Support

For issues, feature requests, or consulting: <https://daadit.group>
