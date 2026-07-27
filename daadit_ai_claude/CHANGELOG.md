# Changelog — daadit_ai_claude

## 19.0.4.0.0 — 2026-07-27

Governance parity with `daadit_ai_mistral`. Until now the two hard
gates that keep an agent inside its budget and inside its records only
existed on the Mistral branch, while the most expensive agents run on
Claude.

- **Daily cost cap.** `services/cost_cap.py` sums the stored
  `estimated_cost_usd` on `daadit_ai_claude.usage` since local midnight
  and refuses further calls once the cap is reached, in the
  conversation's language, notifying an admin once per day. Two knobs:
  `daadit_ai_claude.daily_cost_cap_usd` (Claude only, seeded at 5) and
  `daadit_ai.daily_cost_cap_usd` (Claude + Mistral combined, seeded
  disabled). Fail-open — a broken breaker never blocks chat.
- **Hard read scope.** `run_tool_call` now AND-s the agent's
  `daadit.ai.agent.read.scope` domain into `ir_actions_server_search`
  and `ir_actions_server_read_group`, fail-closed when the scope cannot
  be applied. The scope model lives in `daadit_ai_mistral`, so this is
  a soft dependency: no scope model installed, no gate, no error.
- **Denial messages follow the conversation language again.**
  `_translate_to_chat_language` joins the last three user turns of at
  least `MIN_LANG_REF_LETTERS` letters instead of trusting a single
  last turn, which could be `"?"` — the same fix shipped for Mistral in
  19.0.6.3.0, where a Dutch conversation got a French denial.
