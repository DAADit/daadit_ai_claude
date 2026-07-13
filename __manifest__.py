# -*- coding: utf-8 -*-
{
    "name": "DAADit AI — Claude (Anthropic) Provider",
    "summary": "Add Anthropic Claude as an LLM provider for Odoo's built-in AI features",
    "description": """
DAADit AI — Claude (Anthropic) Provider
========================================
Adds **Anthropic Claude** as an additional LLM provider for Odoo 19
Enterprise's built-in ``ai`` module, alongside OpenAI (ChatGPT) and
Google (Gemini).

Features
--------
* **Chat completions** — adds Claude models to ``ai.agent.llm_model``
  (``claude-opus-4-7``, ``claude-sonnet-4-6``,
  ``claude-haiku-4-5-20251001``, plus the 4.5 family for compatibility)
  and routes them to ``POST https://api.anthropic.com/v1/messages``.
* **Tool calling** — passes ``tools`` and ``tool_choice`` through to
  Claude (converted to Anthropic's ``input_schema`` envelope) when an
  agent has ``topic_ids`` configured. Tool results are fed back as
  ``tool_result`` content blocks so the standard ``_ai_tool_*`` methods
  on ``ai.agent`` run unchanged.
* **No embeddings** — Anthropic does not provide an embeddings API at
  this time, so this module deliberately does NOT add Claude entries to
  ``ai.embedding.embedding_model``. RAG/Sources can keep running on the
  embedding providers Odoo already supports.
* **Per-agent access control** — allowed-models / blocked-models /
  field blocklist.
* **Usage tracking** — every chat call writes a row to
  ``daadit_ai_claude.usage`` with token counts and an estimated cost
  (USD per 1M tokens, overridable via ICP).
* **Settings UI** — adds a clearly-branded Anthropic provider block
  under General Settings → AI.

Notes
-----
The Anthropic API uses a different request envelope from OpenAI
(``system`` is a top-level field, ``messages`` only carries
user/assistant turns, and tool calls travel as ``content`` blocks of
``type: tool_use``). The ``services/claude_client.py`` and
``services/llm_api_patch.py`` modules adapt stock's call shape into and
out of that envelope so the rest of Odoo's AI plumbing (topics, tool
actions, language mirroring) keeps working unchanged.
""",
    "version": "19.0.1.17.0",
    "category": "Productivity/Discuss",
    "author": "DAADit",
    "website": "https://daadit.group",
    "license": "LGPL-3",
    "depends": [
        "base",
        "ai",
        "ai_app",
    ],
    "external_dependencies": {
        "python": ["requests"],
    },
    "data": [
        "security/ir.model.access.csv",
        "security/claude_usage_security.xml",
        "views/res_config_settings_views.xml",
        "views/claude_usage_views.xml",
        "views/ai_agent_views.xml",
        "views/res_partner_views.xml",
    ],
    "pre_init_hook": "pre_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
    "application": False,
    "auto_install": False,
}
