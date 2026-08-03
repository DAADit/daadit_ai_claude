# Changelog — daadit_ai_claude

## 19.0.4.4.0 — 2026-08-03

Zichtbare denkstappen in de chat, gelijk aan `daadit_ai_mistral`
19.0.6.17.0. Claude stuurt nu tijdens een antwoord korte, PII-vrije
voortgangsregels over de bus (per round-trip, per tool-aanroep, en een
`done`-markering aan het eind). Het Anthropic `tool_use`-formaat wordt
naar de gedeelde labelfunctie vertaald met alléén de toolnaam — nooit de
`input` (argumenten). De labels en het bus-verkeer komen uit de gedeelde
laag `daadit_ai_agent_schedule.services.agent_steps`. Claude kent geen
routing/sub-runs, dus alle stappen staan op `depth = 0`.

## 19.0.4.3.0 — 2026-07-26

Een half rapport heet niet langer 'klaar'.

Run 481 (Bram op claude-sonnet-4-6) kreeg status `done` terwijl de
tekst midden in een zin ophield: *"Ik zoek nu naar open tickets…"*. De
agent was niet klaar, hij liep tegen `MAX_ITER = 6`. De Mistral-kant
meldt zo'n afbreking al via `router_state`, en de scheduler leest
precies die vlaggen om 'error' te geven in plaats van 'done'; Claude
zweeg. Daardoor is een afgekapt rapport niet te onderscheiden van een
compleet rapport — dezelfde misleiding die voor Mistral in 19.0.2.1.0
is opgelost.

Drie wijzigingen, allemaal spiegelend op de Mistral-kant:

- `tool_dispatch.router_state` toegevoegd met `top_level_exhausted`,
  `exhaustion_reason` en `run_deadline_monotonic`. De namen liggen vast:
  `daadit_ai_agent_schedule.services.provider_bridge` leest ze letterlijk.
- Bij afbreken wordt de vlag gezet met reden `max_iter` of `deadline`,
  en aan het begin van elke beurt geschoond zodat een vorige beurt op
  dezelfde worker deze niet besmet.
- `MAX_ITER` komt uit `daadit_ai_claude.max_tool_iterations`
  (standaard 12, begrensd op 1–40). Zes is te krap voor een
  rapportagetaak: zoeken, verdiepen, samenvatten en wegschrijven zijn
  er samen al meer.

Claude kende bovendien geen tijdgrens. Nu wordt tussen de round-trips
op `run_deadline_monotonic` gecontroleerd, dus een run overschrijdt
hooguit één call plus zijn tools in plaats van de volle lus te blijven
hangen.

## 19.0.4.2.0 — 2026-08-02

Agents kept handing decisions back to the user. Prod, 2026-07-27:
`create_draft_blogpost` returned the list of the two blogs and the
agent relayed *"in welke blog moet het concept?"* instead of picking
`Nieuws`. `_SELF_SERVICE_INSTRUCTION` is now appended to every system
prompt, matching `daadit_ai_mistral` 19.0.7.4.0: look it up, decide,
state your choice, and ask only when the answer cannot exist in Odoo or
the action is irreversible or costly.

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
- **Per-agent / per-tenant budgets with fair use.** When
  `daadit.ai.budget` is installed (it lives in `daadit_ai_mistral`, so
  this is a soft dependency), Claude calls are evaluated against the
  same daily/monthly ceilings as Mistral calls: fair-use notice from
  the warning threshold, hard stop at 100%. Budgets count spend across
  both providers, so switching model cannot dodge the ceiling.
- **Tool results are bounded** before they enter the conversation
  (`daadit_ai_claude.max_tool_result_chars`, default 6000, 0 disables).
  Each tool result is re-sent on every following round-trip, so an
  unbounded search was paid for again each iteration.
- **Denial messages follow the conversation language again.**
  `_translate_to_chat_language` joins the last three user turns of at
  least `MIN_LANG_REF_LETTERS` letters instead of trusting a single
  last turn, which could be `"?"` — the same fix shipped for Mistral in
  19.0.6.3.0, where a Dutch conversation got a French denial.
