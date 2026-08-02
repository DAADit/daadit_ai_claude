# -*- coding: utf-8 -*-
"""Monkey-patch ``odoo.addons.ai.utils.llm_api_service.LLMApiService`` so
``provider='anthropic'`` is supported end-to-end via the Anthropic
Messages API.

Why a monkey-patch: stock Enterprise's ``ai.agent._generate_response``
constructs ``LLMApiService`` directly by importing the class — it
doesn't go through a factory or a registry that we could override via
``_inherit``. The only practical hook is to replace ``__init__`` and
``request_llm`` on the class object itself.

Key Anthropic-specific quirks handled in this module:

* **``system`` is a top-level field** — not a message. We strip the
  first ``system`` role out of the conversation and pass it separately.
* **``content`` is a list of blocks** — text blocks AND tool_use blocks
  are returned together. We concatenate text blocks to produce a single
  output string, and separately collect tool_use blocks to drive the
  tool-execution loop.
* **Tool results are user messages** — when a tool runs, the result is
  fed back as a ``user`` message whose ``content`` is a list of
  ``tool_result`` blocks (one per tool_use the assistant made).
* **``max_tokens`` is required** — every call must specify it. The
  ``ClaudeClient`` falls back to a 4096 default when none is provided.
"""
import json
import logging

from .claude_client import ClaudeClient, is_claude_model
from . import tool_dispatch

_logger = logging.getLogger(__name__)

_PATCHED = False


def patch_llm_api_service() -> bool:
    """Install the Claude-aware patch on ``LLMApiService``."""
    global _PATCHED
    if _PATCHED:
        return True

    try:
        from odoo.addons.ai.utils import llm_api_service as _llm_mod
    except ImportError:
        _logger.warning(
            "daadit_ai_claude.llm_api_patch: "
            "odoo.addons.ai.utils.llm_api_service is not importable; "
            "the chat dispatch patch will not be active."
        )
        return False

    LLMApiService = getattr(_llm_mod, "LLMApiService", None)
    if LLMApiService is None:
        _logger.warning(
            "daadit_ai_claude.llm_api_patch: LLMApiService class not "
            "found in odoo.addons.ai.utils.llm_api_service."
        )
        return False

    if getattr(LLMApiService, "_daadit_claude_patched", False):
        _PATCHED = True
        return True

    _logger.info(
        "daadit_ai_claude.llm_api_patch: applying patch to %s.%s "
        "(id=%s)",
        LLMApiService.__module__, LLMApiService.__name__,
        id(LLMApiService),
    )

    original_init = LLMApiService.__init__
    original_request_llm = getattr(LLMApiService, "request_llm", None)

    # If another provider module has already wrapped __init__, we
    # compose patches so they coexist — we short-circuit on our own
    # provider name and otherwise call through to whatever __init__ is
    # currently bound (which may be a previously-installed wrapper or
    # the stock one).
    def _patched_init(api_self, env=None, provider=None, *args, **kwargs):
        if provider in ("anthropic", "claude"):
            api_self.env = env
            api_self.provider = "anthropic"
            return None
        return original_init(api_self, env=env, provider=provider,
                             *args, **kwargs)

    def _patched_request_llm(api_self, *args, **kwargs):
        if getattr(api_self, "provider", None) not in ("anthropic", "claude"):
            if original_request_llm is None:
                raise AttributeError(
                    "LLMApiService.request_llm not found on stock; "
                    "cannot delegate non-Claude call."
                )
            return original_request_llm(api_self, *args, **kwargs)
        try:
            return _request_llm_claude(api_self, *args, **kwargs)
        finally:
            try:
                tool_dispatch.current_agent.record = None
            except Exception:  # noqa: BLE001
                pass

    LLMApiService.__init__ = _patched_init
    if original_request_llm is not None:
        LLMApiService.request_llm = _patched_request_llm
    LLMApiService._daadit_claude_patched = True
    LLMApiService._daadit_claude_original_init = original_init
    LLMApiService._daadit_claude_original_request_llm = original_request_llm

    _PATCHED = True
    _logger.info(
        "daadit_ai_claude.llm_api_patch: LLMApiService patched to support "
        "provider='anthropic' (class: %s.%s)",
        LLMApiService.__module__, LLMApiService.__name__,
    )
    return True


_MODEL_KEYS = ("model", "llm_model", "model_name", "name")
_MESSAGE_KEYS = ("inputs", "input", "messages", "msgs",
                 "prompt_messages", "history", "conversation",
                 "chat_history")
_TOOLS_KEYS = ("tools", "functions")
_TOOL_CHOICE_KEYS = ("tool_choice", "function_call")
_TEMPERATURE_KEYS = ("temperature", "temp")
_MAX_TOKENS_KEYS = ("max_tokens", "max_completion_tokens", "max_new_tokens")
_PROMPT_KEYS = ("prompt", "user_prompt", "user_message")
_SYSTEM_KEYS = ("system_prompt", "system", "system_message", "instructions")
_BODY_KEYS = ("body", "payload", "data", "params", "request_body",
              "request", "kwargs")


def _normalize_message_dict(d):
    """Coerce one message-like dict into ``{role, content}`` shape."""
    role = (
        d.get("role")
        or d.get("author")
        or d.get("from")
        or "user"
    )
    content = (
        d.get("content")
        if d.get("content") is not None else (
            d.get("text")
            or d.get("message")
            or d.get("body")
            or ""
        )
    )
    if not isinstance(content, (str, list)):
        content = str(content)
    return {"role": role, "content": content}


def _split_system_from_messages(messages):
    """Pull system messages out into a single top-level system string,
    return ``(system_text, [non_system_messages])``.

    Anthropic's API requires ``system`` as a top-level field on the
    request; system role inside ``messages`` is not accepted.
    Multiple system messages get concatenated with a blank line.
    """
    if not messages:
        return None, []
    system_parts = []
    rest = []
    for m in messages:
        if not isinstance(m, dict):
            rest.append(m)
            continue
        if m.get("role") == "system":
            content = m.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            if content:
                system_parts.append(str(content))
        else:
            rest.append(m)
    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, rest


def _coerce_messages_for_anthropic(messages):
    """Convert mixed message shapes to Anthropic's accepted form.

    Anthropic accepts:
      * ``role`` ∈ {``user``, ``assistant``}
      * ``content`` as either a plain string OR a list of content
        blocks (``[{"type":"text","text":...}]``, possibly with
        ``tool_use`` / ``tool_result`` blocks).

    We do minimal coercion: roles other than user/assistant get mapped
    to user (defensive). ``tool``-role messages (from stock's
    OpenAI-style loop) are converted to a user message containing a
    ``tool_result`` block.
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "tool":
            tool_use_id = m.get("tool_use_id") or m.get("tool_call_id") or ""
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content if isinstance(content, str) else json.dumps(content, default=str),
                }],
            })
            continue
        if role not in ("user", "assistant"):
            role = "user"
        out.append({"role": role, "content": content if content is not None else ""})
    return out


def _normalize_messages(value):
    """Convert arbitrary stock input shapes into a list of
    ``{role, content}`` dicts (still potentially containing system roles
    — those are split off later by ``_split_system_from_messages``).
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if isinstance(value, dict):
        return [_normalize_message_dict(value)]
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        out = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                out.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                out.append(_normalize_message_dict(item))
            elif hasattr(item, "_name"):
                txt = (
                    getattr(item, "content", None)
                    or getattr(item, "body", None)
                    or getattr(item, "text", None)
                    or ""
                )
                role = getattr(item, "role", None) or getattr(item, "author", None) or "user"
                out.append({"role": str(role), "content": str(txt)})
        return out or None
    return None


def _extract_from_dict(d, model, messages, tools, tool_choice,
                        temperature, max_tokens):
    if not isinstance(d, dict):
        return model, messages, tools, tool_choice, temperature, max_tokens
    for k in _MODEL_KEYS:
        if not model and k in d:
            model = d.get(k); break
    for k in _MESSAGE_KEYS:
        if not messages and k in d:
            cand = d.get(k)
            normalized = _normalize_messages(cand)
            if normalized:
                messages = normalized; break
    for k in _TOOLS_KEYS:
        if tools is None and k in d:
            tools = d.get(k); break
    for k in _TOOL_CHOICE_KEYS:
        if tool_choice is None and k in d:
            tool_choice = d.get(k); break
    for k in _TEMPERATURE_KEYS:
        if temperature is None and k in d:
            temperature = d.get(k); break
    for k in _MAX_TOKENS_KEYS:
        if max_tokens is None and k in d:
            max_tokens = d.get(k); break
    return model, messages, tools, tool_choice, temperature, max_tokens


def _format_access_denied_message(info):
    """Render an admin-policy denial as a clean English markdown message."""
    requested = info.get("model_name") or "?"
    allowed = info.get("allowed_models") or []
    blocked = info.get("blocked_models") or []

    lines = [
        "**🔒 Access blocked by your administrator.**",
        "",
        f"This AI agent is not permitted to query the model `{requested}`.",
    ]

    if requested in blocked:
        lines.append(
            "It is on the **block list** for this agent — admins have "
            "explicitly chosen to keep this data out of AI responses."
        )
    elif allowed:
        lines.append("")
        lines.append(
            "The administrator has restricted this agent to the "
            "following models:"
        )
        lines.append("")
        for m in sorted(allowed):
            lines.append(f"- `{m}`")
        lines.append("")
        lines.append(
            "If you need information about something else, contact "
            "your system administrator to request access — they can "
            "extend the agent's permitted models."
        )
    else:
        lines.append("")
        lines.append(
            "Contact your system administrator if you believe this "
            "is wrong."
        )
    return "\n".join(lines)


MIN_LANG_REF_LETTERS = 12


def _letter_count(text):
    """Number of alphabetic characters in ``text`` (any script)."""
    return sum(1 for ch in text if ch.isalpha())


def _translate_to_chat_language(client, model, ref_messages, text):
    """Use Claude itself to translate ``text`` into the language of the
    conversation (so admin-policy denials match the user's locale).

    The reference is the last few *substantial* user turns. A single
    short turn (``"?"``, ``"ok"``, an agent's name) carries no language
    signal and made denials come back in a random language, so turns
    below ``MIN_LANG_REF_LETTERS`` letters are ignored.
    """
    if not text or not ref_messages:
        return text

    user_texts = [
        m["content"] for m in ref_messages
        if isinstance(m, dict)
        and m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m.get("content").strip()
    ]
    usable = [t for t in user_texts if _letter_count(t) >= MIN_LANG_REF_LETTERS]
    if not usable:
        return text
    last_user = "\n\n".join(usable[-3:])[-800:]

    try:
        response = client.create_message(
            model=model,
            system=(
                "You are a translator. Translate the user's input into "
                "the SAME language as the reference text below. "
                "Strictly preserve markdown: ** for bold, ` for inline "
                "code, - for bullet items, blank lines between paragraphs, "
                "and emoji. Do NOT translate identifiers inside backticks "
                "(like `account.move`, `res.partner`). Output only the "
                "translation — no preamble, no quotes, no explanation."
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Reference text (target language):\n"
                        f"---\n{last_user[:800]}\n---\n\n"
                        f"Translate this:\n{text}"
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=1024,
        )
        translated = ClaudeClient.extract_text(response)
        translated = (translated or "").strip()
        return translated or text
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.llm_api_patch: translation via Claude "
            "failed — falling back to English source"
        )
        return text


def _resolve_agent(api_self):
    """Find the ``ai.agent`` record that triggered this chat call.

    1. Threadlocal set by our ``_get_provider`` override.
    2. ``env.context['discuss_channel'].ai_agent_id`` fallback.
    """
    rec = getattr(tool_dispatch.current_agent, "record", None)
    if rec is not None:
        try:
            if rec.id:
                return api_self.env["ai.agent"].browse(rec.id)
        except Exception:  # noqa: BLE001
            pass

    try:
        ch = api_self.env.context.get("discuss_channel")
        if ch is None:
            return None
        if isinstance(ch, int):
            ch = api_self.env["discuss.channel"].sudo().browse(ch)
        if hasattr(ch, "sudo") and hasattr(ch, "ai_agent_id"):
            agent_id = ch.sudo().ai_agent_id.id
            if agent_id:
                ag = api_self.env["ai.agent"].browse(agent_id)
                _logger.info(
                    "daadit_ai_claude.llm_api_patch: agent resolved "
                    "via env.context['discuss_channel'].ai_agent_id "
                    "→ ai.agent(%s) as user %s",
                    agent_id, api_self.env.uid,
                )
                return ag
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.llm_api_patch: agent fallback lookup raised"
        )
    return None


_DEFAULT_MAX_TOOL_RESULT_CHARS = 6000


def _cap_tool_result(env, content):
    """Bound the size of one tool result before it enters the context.

    A tool result stays in the conversation for every following
    round-trip, so an unbounded ``search`` of 80 full records is paid
    for again on each iteration. Truncate with an instruction the model
    can act on instead of silently feeding it half a record.

    Tunable via ``daadit_ai_claude.max_tool_result_chars``; 0 disables.
    """
    try:
        raw = env["ir.config_parameter"].sudo().get_param(
            "daadit_ai_claude.max_tool_result_chars",
            _DEFAULT_MAX_TOOL_RESULT_CHARS,
        )
        limit = int(raw)
    except Exception:  # noqa: BLE001
        limit = _DEFAULT_MAX_TOOL_RESULT_CHARS
    if limit <= 0 or not isinstance(content, str) or len(content) <= limit:
        return content
    _logger.info(
        "daadit_ai_claude.llm_api_patch: tool result truncated "
        "%d → %d chars", len(content), limit,
    )
    return (
        content[:limit]
        + "\n\n[TRUNCATED: this result was %d characters; only the "
          "first %d are shown. Do not guess the rest — narrow the "
          "domain, request fewer fields, or aggregate with read_group.]"
          % (len(content), limit)
    )


# Why this exists: prod chats keep ending in a question the agent could
# have answered itself. Observed 2026-07-27: the blog tool RETURNED the
# list of two blogs, and the agent relayed "which blog should I use?" to
# the user instead of picking one. Every such turn costs a full extra
# round-trip and makes the colleague feel useless.
_SELF_SERVICE_INSTRUCTION = (
    "CRITICAL — DECIDE, DON'T ASK: You have tools that read this Odoo "
    "database. Never ask the user for anything you can look up, and "
    "never hand back a choice you can make yourself. Before you ask a "
    "question: (1) search Odoo for the answer; (2) if a tool result "
    "already lists the options, pick the one that best fits the task; "
    "(3) if the options are genuinely equivalent, take the most "
    "obvious one, act, and state which one you took and how to change "
    "it. Asking 'which blog should I use?' right after a tool handed "
    "you the list of blogs is a failure, not caution. Only ask the "
    "user when the answer cannot exist in Odoo, or when the action is "
    "irreversible or costly (publishing, sending email, deleting, "
    "spending money) — and then ask once, with your recommendation "
    "already in the question. If the work belongs to another "
    "colleague and you have a tool to reach them, use it; do not tell "
    "the user to go ask that colleague."
)


_LANGUAGE_MIRROR_INSTRUCTION = (
    "IMPORTANT: Always respond in the same language as the user's most "
    "recent message. If the user writes in Dutch, reply in Dutch. If "
    "the user writes in French, reply in French. Do NOT translate "
    "technical identifiers inside backticks (such as `account.move`, "
    "`res.partner`, field names like `stage_id`).\n\n"
    + _SELF_SERVICE_INSTRUCTION
)


def _request_llm_claude(api_self, *args, **kwargs):
    """Claude-side replacement for ``LLMApiService.request_llm``."""
    _log_first_call_args(args, kwargs)

    model = messages = tools = tool_choice = temperature = max_tokens = None

    # --- 1. Direct kwargs --------------------------------------------
    (model, messages, tools, tool_choice, temperature, max_tokens) = \
        _extract_from_dict(
            kwargs, model, messages, tools, tool_choice,
            temperature, max_tokens,
        )

    # --- 2. Positional sniffing --------------------------------------
    for a in args:
        if isinstance(a, str) and not model:
            if is_claude_model(a):
                model = a
        elif isinstance(a, (list, tuple)) and not messages:
            normalized = _normalize_messages(a)
            if normalized:
                messages = normalized
        elif isinstance(a, dict):
            (model, messages, tools, tool_choice, temperature, max_tokens) = \
                _extract_from_dict(
                    a, model, messages, tools, tool_choice,
                    temperature, max_tokens,
                )

    # --- 3. Nested body kwargs ---------------------------------------
    for k in _BODY_KEYS:
        nested = kwargs.get(k)
        if isinstance(nested, dict):
            (model, messages, tools, tool_choice, temperature, max_tokens) = \
                _extract_from_dict(
                    nested, model, messages, tools, tool_choice,
                    temperature, max_tokens,
                )

    # --- 4. prompt / system_prompt ⇒ assemble messages ---------------
    if not messages:
        prompt = next(
            (kwargs.get(k) for k in _PROMPT_KEYS if kwargs.get(k)),
            None,
        )
        system = next(
            (kwargs.get(k) for k in _SYSTEM_KEYS if kwargs.get(k)),
            None,
        )
        if prompt or system:
            messages = []
            if system:
                messages.append({"role": "system", "content": str(system)})
            if prompt:
                messages.append({"role": "user", "content": str(prompt)})

    # --- Defaults / hard validation ----------------------------------
    if not model:
        _logger.warning(
            "daadit_ai_claude.llm_api_patch: no 'model' found in "
            "request_llm call; falling back to claude-sonnet-4-6"
        )
        model = "claude-sonnet-4-6"

    if not messages:
        from odoo.exceptions import UserError
        from odoo.tools.translate import _
        raise UserError(_(
            "DAADit AI Claude: could not extract 'messages' from "
            "request_llm call (args types=%(at)s, kwargs keys=%(kk)s). "
            "Look for the 'first request_llm(claude) call' line in the "
            "Odoo log to see the exact arg names stock is using.",
            at=str([type(a).__name__ for a in args]),
            kk=str(list(kwargs.keys())),
        ))

    # ---- Build proper tool definitions ------------------------------
    normalized_tools = None
    if tools:
        if isinstance(tools, (list, tuple)) and all(isinstance(t, str) for t in tools):
            normalized_tools = tool_dispatch.annotate_tools(tools)
        else:
            normalized_tools = tool_dispatch.normalize_tools(tools)
        if not normalized_tools:
            _logger.warning(
                "daadit_ai_claude.llm_api_patch: tools were provided but "
                "could not be normalized to Anthropic format; dropping."
            )

    # ---- Resolve the agent + apply per-agent overrides --------------
    agent = _resolve_agent(api_self)

    if agent is not None:
        try:
            if getattr(agent, "daadit_claude_temperature_active", False):
                temperature = agent.daadit_claude_temperature
        except Exception:  # noqa: BLE001
            pass
        try:
            agent_max = agent.daadit_claude_max_tokens
            if agent_max:
                max_tokens = agent_max
        except Exception:  # noqa: BLE001
            pass

    # ---- Build the Anthropic-shaped conversation --------------------
    # 1. Split system out of the messages list (Anthropic top-level
    #    field, not a message).
    # 2. Inject the language-mirror instruction into the system text.
    raw_conv = list(messages)
    system_text, non_system = _split_system_from_messages(raw_conv)
    if system_text:
        system_text = f"{system_text}\n\n{_LANGUAGE_MIRROR_INSTRUCTION}"
    else:
        system_text = _LANGUAGE_MIRROR_INSTRUCTION

    conversation = _coerce_messages_for_anthropic(non_system)

    client = ClaudeClient.from_env(api_self.env)
    iteration = 0
    MAX_ITER = 6
    response = None
    access_denial = None
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    # --- Daily cost cap (v19.0.4.0.0) --------------------------------
    # Single choke point for every Claude chat call, mirroring the
    # Mistral provider. When today's spend has reached the configured
    # cap, refuse the call with a clear, translated message instead of
    # hitting Anthropic — and notify the admin once per day.
    # Fail-open: a broken breaker never blocks chat.
    try:
        from . import cost_cap
        blocked, spent, cap = cost_cap.check(api_self.env)
        if blocked:
            _logger.warning(
                "daadit_ai_claude.llm_api_patch: daily cost cap reached "
                "(%.2f/%.2f) — refusing call for agent=%s",
                spent, cap, agent.id if agent else None,
            )
            return [_translate_to_chat_language(
                client, model, conversation,
                cost_cap.blocked_message_en(spent, cap),
            )]
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.llm_api_patch: cost-cap gate raised; "
            "failing open"
        )

    # --- Per-agent / per-tenant budget + fair use --------------------
    # ``daadit.ai.budget`` lives in daadit_ai_mistral and counts spend
    # across BOTH providers, so an agent can't escape its ceiling by
    # switching model. Soft dependency: no budget model, no gate.
    fair_use_notice = ""
    try:
        if "daadit.ai.budget" in api_self.env:
            blocked, budget_msg, detail = api_self.env[
                "daadit.ai.budget"].sudo().evaluate(agent=agent)
            if blocked:
                _logger.warning(
                    "daadit_ai_claude.llm_api_patch: budget exhausted "
                    "(%s) — refusing call for agent=%s",
                    detail, agent.id if agent else None,
                )
                return [_translate_to_chat_language(
                    client, model, conversation, budget_msg,
                )]
            if budget_msg:
                _logger.info(
                    "daadit_ai_claude.llm_api_patch: fair-use notice "
                    "(%s)", detail,
                )
                fair_use_notice = budget_msg
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.llm_api_patch: budget gate raised; "
            "failing open"
        )

    while iteration < MAX_ITER:
        response = client.create_message(
            model=model,
            messages=conversation,
            system=system_text,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=normalized_tools,
            tool_choice=tool_choice,
        )

        # Accumulate usage across iterations.
        u = ClaudeClient.extract_usage(response)
        total_usage["prompt_tokens"] += u.get("prompt_tokens") or 0
        total_usage["completion_tokens"] += u.get("completion_tokens") or 0

        stop_reason = ClaudeClient.extract_stop_reason(response)
        tool_uses = ClaudeClient.extract_tool_uses(response)

        if stop_reason != "tool_use" or not tool_uses or agent is None:
            break

        # Echo the assistant's full content (text + tool_use blocks)
        # back into the conversation so Claude can correlate tool
        # results with their requests.
        conversation.append({
            "role": "assistant",
            "content": response.get("content") or [],
        })

        tool_result_blocks = []
        for tu in tool_uses:
            result = tool_dispatch.run_tool_call(agent, tu)

            if isinstance(result, dict) and result.get("_daadit_access_denied"):
                access_denial = result
                break

            try:
                content_str = json.dumps(result, default=str)
            except Exception:  # noqa: BLE001
                content_str = str(result)
            content_str = _cap_tool_result(api_self.env, content_str)
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tu.get("id"),
                "content": content_str,
            })

        if access_denial:
            break

        # Feed all tool results back in a single user message.
        conversation.append({
            "role": "user",
            "content": tool_result_blocks,
        })

        iteration += 1
        _logger.info(
            "daadit_ai_claude.llm_api_patch: tool iteration %d, "
            "%d tool_use(s) executed",
            iteration, len(tool_uses),
        )

    if iteration >= MAX_ITER:
        _logger.warning(
            "daadit_ai_claude.llm_api_patch: hit MAX_ITER=%d in tool "
            "loop; returning whatever final response we have",
            MAX_ITER,
        )

    # Admin-policy denial short-circuit — surface a clean user-facing
    # message in the chat language instead of letting the LLM
    # mis-paraphrase the denial.
    if access_denial:
        en_message = _format_access_denied_message(access_denial)
        translated = _translate_to_chat_language(
            client, model, conversation, en_message,
        )
        adapted = [translated]
        _logger.info(
            "daadit_ai_claude.llm_api_patch: chat short-circuited "
            "by admin-policy denial — model=%s requested=%s",
            model, access_denial.get("model_name"),
        )
        try:
            ch = api_self.env.context.get("discuss_channel")
            channel_id = ch.id if ch and hasattr(ch, "id") else (
                ch if isinstance(ch, int) else False
            )
            api_self.env["daadit_ai_claude.usage"].sudo().record_usage(
                kind="chat", model=model,
                agent_id=agent.id if agent else False,
                channel_id=channel_id,
                prompt_tokens=total_usage["prompt_tokens"],
                completion_tokens=total_usage["completion_tokens"],
                iterations=iteration + 1,
                has_tools=bool(normalized_tools),
                error=f"Access denied: {access_denial.get('model_name')}",
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_claude.llm_api_patch: usage row creation "
                "failed during access-denial; continuing"
            )
        return adapted

    # Normal completion — extract concatenated text from the final
    # response. Stock's _post_ai_response iterates the return value and
    # feeds each item to markdown(), so we return a list of plain
    # strings (one per response choice — Anthropic only returns one).
    text = ClaudeClient.extract_text(response) if response else ""
    adapted = [text] if text and text.strip() else []

    _logger.info(
        "daadit_ai_claude.llm_api_patch: Claude chat ok "
        "(model=%s iterations=%d tokens=%s/%s text_chunks=%d "
        "agent_seen=%s)",
        model,
        iteration + 1,
        total_usage["prompt_tokens"],
        total_usage["completion_tokens"],
        len(adapted),
        bool(agent),
    )

    # ---- Persist usage row -----------------------------------------
    try:
        ch = api_self.env.context.get("discuss_channel")
        channel_id = ch.id if ch and hasattr(ch, "id") else (
            ch if isinstance(ch, int) else False
        )
        api_self.env["daadit_ai_claude.usage"].sudo().record_usage(
            kind="chat",
            model=model,
            agent_id=agent.id if agent else False,
            channel_id=channel_id,
            prompt_tokens=total_usage["prompt_tokens"],
            completion_tokens=total_usage["completion_tokens"],
            iterations=iteration + 1,
            has_tools=bool(normalized_tools),
        )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.llm_api_patch: usage row creation failed; "
            "continuing"
        )

    if not adapted:
        # Claude returned only tool_use blocks AND we couldn't loop
        # (probably because no agent was found).
        last_tool_uses = ClaudeClient.extract_tool_uses(response) if response else []
        if last_tool_uses:
            fallback_en = (
                "_(Claude wanted to call a function but I couldn't "
                "find the AI Agent record to dispatch it. Try sending "
                "the message from inside the AI chat panel.)_"
            )
        else:
            fallback_en = "_(Empty response from Claude.)_"
        adapted = [
            _translate_to_chat_language(
                client, model, conversation, fallback_en,
            )
        ]

    if fair_use_notice and adapted:
        adapted.append(_translate_to_chat_language(
            client, model, conversation, fair_use_notice,
        ))
    return adapted


_FIRST_CALL_LOGGED = False


def _redact_value_for_log(v):
    """Structural-only log fragment (no PII echoes)."""
    t = type(v).__name__
    if v is None:
        return "type=NoneType"
    if isinstance(v, bool):
        return f"type={t} value={v}"
    if isinstance(v, (int, float)):
        return f"type={t}"
    if isinstance(v, str):
        return f"type=str len={len(v)}"
    if isinstance(v, (list, tuple)):
        item_types = sorted({type(x).__name__ for x in v[:10]})
        return f"type={t} len={len(v)} item_types={item_types}"
    if isinstance(v, dict):
        keys = sorted(str(k) for k in list(v.keys())[:30])
        return f"type=dict len={len(v)} keys={keys}"
    if isinstance(v, (bytes, bytearray)):
        return f"type={t} len={len(v)}"
    return f"type={t}"


def _log_first_call_args(args, kwargs):
    """One-shot structural log of the actual ``request_llm`` signature."""
    global _FIRST_CALL_LOGGED
    if _FIRST_CALL_LOGGED:
        return
    _FIRST_CALL_LOGGED = True
    _logger.info(
        "daadit_ai_claude.llm_api_patch: FIRST request_llm(claude) "
        "call — positional=%d kwargs=%d kw_keys=%s",
        len(args), len(kwargs), sorted(kwargs.keys()),
    )
    for i, a in enumerate(args):
        _logger.info("  arg[%d] %s", i, _redact_value_for_log(a))
    for k, v in kwargs.items():
        _logger.info("  kw[%s] %s", k, _redact_value_for_log(v))
