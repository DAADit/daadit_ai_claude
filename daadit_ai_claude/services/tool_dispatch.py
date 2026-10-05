# -*- coding: utf-8 -*-
"""Claude-kant van de toolaanroepen: alleen het Anthropic-formaat.

De uitvoering zelf staat in ``daadit_ai_agentic_system``; deze module
vertaalt tussen dat en Anthropic's envelope:

  * Het model stuurt ``tool_use``-blokken::
        {"type": "tool_use", "id": "toolu_…",
         "name": "ir_actions_server_search",
         "input": {...}}
  * Tooldefinities gaan naar Claude met ``input_schema`` (zie
    ``annotate_tools`` / ``normalize_tools``).
"""
from odoo.addons.daadit_ai_agentic_system.services import (
    tool_dispatch as _agentic,
)

current_agent = _agentic.current_agent
router_state = _agentic.router_state
TOOL_SCHEMAS = _agentic.TOOL_SCHEMAS


def annotate_tools(tool_names):
    """Anthropic-tooldefinities voor een lijst toolnamen."""
    agent = getattr(current_agent, "record", None)
    return normalize_tools(_agentic.annotate_tools(
        [n for n in tool_names or [] if isinstance(n, str)], agent=agent,
    )) or []


def normalize_tools(tools):
    """Convert various tool shapes to Anthropic's expected envelope.

    Accepts:
      * Already-Anthropic-shaped dicts ``{"name": ..., "description": ...,
        "input_schema": ...}`` — passed through.
      * OpenAI-shaped ``{"type": "function", "function": {...}}`` —
        unwrapped to Anthropic shape (``parameters`` → ``input_schema``).
      * Bare function dict ``{"name": ..., "parameters": ...}`` — converted
        to ``input_schema``.
      * Plain string (just a name) — wrapped via ``annotate_tools``.

    Returns ``None`` for empty / unrecognised input so the caller can omit
    the kwarg from the Anthropic payload.
    """
    if not tools:
        return None

    if isinstance(tools, dict):
        if "tools" in tools and isinstance(tools["tools"], (list, tuple)):
            tools = tools["tools"]
        else:
            converted = []
            for name, val in tools.items():
                if isinstance(val, dict):
                    converted.append({"name": name, **val})
                elif isinstance(val, str):
                    converted.append({"name": name, "description": val})
                elif isinstance(val, (list, tuple)):
                    # Stock ai_app's ``_get_ai_tools()`` format:
                    # {name: (description, allow_end_message, callable,
                    #         json_schema)}. Dropping the schema here is
                    # what made Claude see parameterless tools — keep it.
                    entry = {"name": name}
                    if val and isinstance(val[0], str):
                        entry["description"] = val[0]
                    schema = next(
                        (v for v in val
                         if isinstance(v, dict) and "properties" in v),
                        None,
                    )
                    if schema is not None:
                        entry["input_schema"] = schema
                    converted.append(entry)
                else:
                    converted.append({"name": name})
            tools = converted

    if not isinstance(tools, (list, tuple)):
        return None

    # All-strings case → use annotate_tools so we get the rich schemas.
    if all(isinstance(t, str) for t in tools):
        return annotate_tools(list(tools))

    out = []
    for tool in tools:
        if isinstance(tool, dict):
            if "input_schema" in tool and "name" in tool:
                # Already Anthropic-shaped.
                out.append({
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "input_schema": tool["input_schema"],
                })
            elif "function" in tool and isinstance(tool["function"], dict):
                # OpenAI-style envelope — unwrap.
                fn = tool["function"]
                out.append({
                    "name": fn.get("name") or "",
                    "description": fn.get("description") or "",
                    "input_schema": fn.get("parameters") or {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                })
            elif "name" in tool:
                out.append({
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "input_schema": (
                        tool.get("input_schema")
                        or tool.get("parameters")
                        or {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        }
                    ),
                })
        elif isinstance(tool, str):
            out.append({
                "name": tool,
                "description": tool.replace("_", " ").strip(),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            })
    return out or None


def run_tool_call(agent, tool_use):
    """Voer één Anthropic ``tool_use``-blok uit."""
    if not tool_use or not isinstance(tool_use, dict):
        return {"error": "Invalid tool_use object"}
    return _agentic.dispatch(
        agent, tool_use.get("name") or "", tool_use.get("input"),
    )
