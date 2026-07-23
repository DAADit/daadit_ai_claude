# -*- coding: utf-8 -*-
"""Tool-call execution for Claude (Anthropic) messages API.

Look up the matching ``_ai_tool_*`` method on the agent, parse the
Anthropic-flavored ``input`` payload, run it, and return a
JSON-serialisable result.

Anthropic's tool envelope:

  * The model returns ``tool_use`` blocks inside ``response.content``:
        {"type": "tool_use", "id": "toolu_…",
         "name": "ir_actions_server_search",
         "input": {...}}
  * Tool *definitions* sent to Claude use ``input_schema``. See
    ``annotate_tools``.
  * Tool *results* come back as a user-role message containing
    ``tool_result`` blocks::
        {"role": "user",
         "content": [
            {"type": "tool_result",
             "tool_use_id": "toolu_…",
             "content": "<json>"}
         ]}
"""
import json
import logging
import re
import threading

_logger = logging.getLogger(__name__)

# Threadlocal where the ai.agent's _get_provider override stashes the
# record so request_llm can pick it up. Cleared in a try/finally so
# leftover state doesn't leak across requests on the same worker.
current_agent = threading.local()


# ---------------------------------------------------------------------------
# Tool name → ai.agent method mapping
# ---------------------------------------------------------------------------

_TOOL_PREFIX = "ir_actions_server_"
_AI_TOOL_PREFIX = "_ai_tool_"


def _tool_name_to_method(tool_name: str) -> str:
    """Map ``ir_actions_server_search`` ⇒ ``_ai_tool_search``."""
    if tool_name.startswith(_TOOL_PREFIX):
        return _AI_TOOL_PREFIX + tool_name[len(_TOOL_PREFIX):]
    if tool_name.startswith(_AI_TOOL_PREFIX):
        return tool_name
    return _AI_TOOL_PREFIX + tool_name


# Stock ai_app names a tool after its action's xml-id suffix
# (``ir_actions_server_search``) or ``action_<id>`` when the action has
# no xml-id (custom tools created in the UI).
_ACTION_ID_RE = re.compile(r"^action_(\d+)$")


def _resolve_tool_action(agent, fn_name):
    """Resolve a tool name to its backing ``ir.actions.server`` record.

    Executing the ACTION (via stock's ``_ai_tool_run``) instead of the
    underlying ``_ai_tool_*`` method matters twice over: custom tools
    (``action_<id>``) have no backing method at all, and operators put
    guard code in the action body that must run on every dispatch path.
    Returns a record in the agent's (non-sudo) environment, or None.
    """
    env = getattr(agent, "env", None)
    if env is None:
        return None
    try:
        Action = env["ir.actions.server"].sudo()
        action = None
        m = _ACTION_ID_RE.match(fn_name or "")
        if m:
            candidate = Action.browse(int(m.group(1)))
            if candidate.exists():
                action = candidate
        else:
            imd = env["ir.model.data"].sudo().search(
                [("model", "=", "ir.actions.server"), ("name", "=", fn_name)],
                limit=1,
            )
            if imd:
                candidate = Action.browse(imd.res_id)
                if candidate.exists():
                    action = candidate
        if action is not None and action.use_in_ai:
            return action.with_env(env)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.tool_dispatch: action resolution raised "
            "for %r — falling back to method dispatch", fn_name,
        )
    return None


# ---------------------------------------------------------------------------
# JSON schemas for the stock AI tools
# ---------------------------------------------------------------------------
#
# Stock's underlying ``_ai_tool_*`` methods have identical signatures
# regardless of provider, so we describe them once and expose them as
# Anthropic ``input_schema`` envelopes in ``annotate_tools`` below.


def _domain_schema():
    return {
        "type": "string",
        "description": (
            "Odoo domain filter as a JSON-encoded STRING (not an array). "
            "Stock parses this with json.loads internally, so wrap your "
            "filter array in quotes and JSON-escape its contents. "
            "Examples (literal strings): "
            "no filter → \"[]\", "
            "single condition → \"[[\\\"state\\\", \\\"=\\\", \\\"posted\\\"]]\", "
            "multiple ANDed → \"[[\\\"state\\\", \\\"=\\\", \\\"posted\\\"], "
            "[\\\"amount_total\\\", \\\">\\\", 1000]]\", "
            "OR clause → \"[\\\"|\\\", [\\\"state\\\", \\\"=\\\", \\\"draft\\\"], "
            "[\\\"state\\\", \\\"=\\\", \\\"posted\\\"]]\". "
            "Operators: '=', '!=', '>', '<', '>=', '<=', 'like', 'ilike', "
            "'in', 'not in', 'child_of'. Logical: '|' for OR over the "
            "next two terms, '&' for AND (default), '!' for NOT."
        ),
        "default": "[]",
    }


def _string_array(desc, default=None):
    schema = {
        "type": "array",
        "items": {"type": "string"},
        "description": desc,
    }
    if default is not None:
        schema["default"] = default
    return schema


TOOL_SCHEMAS = {
    "ir_actions_server_search": {
        "description": (
            "Search records of an Odoo model. Returns a list of dicts "
            "with the requested fields. Prefer this when answering "
            "factual questions about specific records."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {
                    "type": "string",
                    "description": "Technical model name, e.g. 'res.partner', 'account.move', 'sale.order'.",
                },
                "domain": _domain_schema(),
                "fields": _string_array(
                    "Field names to read. Keep this short — 3–6 fields "
                    "is usually enough."
                ),
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 80,
                          "description": "Max records to return (cap at 200)."},
                "order": {
                    "type": "string",
                    "description": "Sort spec like 'name asc, id desc'. Optional.",
                },
            },
            "required": ["model_name"],
        },
    },
    "ir_actions_server_read_group": {
        "description": (
            "Aggregate (count, sum, avg) records of a model, grouped "
            "by one or more fields. Use this for questions like "
            "'how many invoices', 'total revenue per month', "
            "'top customers by sales'. "
            "For a simple total count with NO grouping, pass "
            "groupby=[] and aggregates=['__count']."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {"type": "string",
                    "description": "Technical model name. Common ones: "
                    "'account.move' (invoices/bills), 'sale.order', "
                    "'purchase.order', 'product.template', 'res.partner', "
                    "'stock.picking', 'crm.lead', 'project.task'."},
                "domain": _domain_schema(),
                "groupby": _string_array(
                    "JSON array of field names to group by. "
                    "Empty array [] = no grouping (single-row result). "
                    "Date fields can have an interval suffix: "
                    "['create_date:month'], ['create_date:year']. "
                    "Examples: [], ['state'], ['partner_id'], "
                    "['create_date:month']."
                ),
                "aggregates": _string_array(
                    "JSON array of aggregations as 'field:operator' "
                    "strings, except '__count' which counts records. "
                    "Operators: 'sum', 'avg', 'min', 'max', "
                    "'count_distinct'. "
                    "Examples: ['__count'], ['amount_total:sum'], "
                    "['amount_total:avg', '__count'], "
                    "['partner_id:count_distinct']."
                ),
                "having": _domain_schema(),
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 80},
                "order": {"type": "string",
                    "description": "Sort spec on aggregates, e.g. "
                    "'amount_total desc' to find biggest values first."},
            },
            "required": ["model_name", "groupby", "aggregates"],
        },
    },
    "ir_actions_server_get_fields": {
        "description": (
            "Introspect a model: return the list of available fields "
            "with their types. Use this if you need to know what fields "
            "exist before searching or grouping."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {"type": "string"},
                "include_description": {"type": "boolean", "default": False},
            },
            "required": ["model_name"],
        },
    },
    "ir_actions_server_get_menu_details": {
        "description": "Return menu metadata (model, default views, etc.) for one or more menu IDs.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of menu IDs to inspect.",
                },
            },
            "required": ["menu_ids"],
        },
    },
    "ir_actions_server_open_menu_kanban": {
        "description": "Open a menu's kanban view in the UI with optional filters.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_id": {"type": "integer"},
                "model_name": {"type": "string"},
                "selected_filters": _string_array("Names of search filters to apply.", default=[]),
                "selected_groupbys": _string_array("Names of group-by fields to apply.", default=[]),
                "search": {"type": "string", "description": "Free-text search.", "default": ""},
                "custom_domain": _domain_schema(),
            },
            "required": ["menu_id", "model_name"],
        },
    },
    "ir_actions_server_open_menu_list": {
        "description": "Open a menu's list view in the UI with optional filters.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_id": {"type": "integer"},
                "model_name": {"type": "string"},
                "selected_filters": _string_array("Names of search filters to apply.", default=[]),
                "selected_groupbys": _string_array("Names of group-by fields to apply.", default=[]),
                "search": {"type": "string", "description": "Free-text search.", "default": ""},
                "custom_domain": _domain_schema(),
            },
            "required": ["menu_id", "model_name"],
        },
    },
    "ir_actions_server_open_menu_graph": {
        "description": "Open a menu's graph view (bar/line/pie) with grouping and measure config.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_id": {"type": "integer"},
                "model_name": {"type": "string"},
                "selected_filters": _string_array("Names of search filters to apply.", default=[]),
                "selected_groupbys": _string_array("Names of group-by fields to apply.", default=[]),
                "measure": {"type": "string", "description": "Measure to plot, e.g. 'amount_total:sum'."},
                "mode": {"type": "string", "enum": ["bar", "line", "pie"], "default": "bar"},
                "order": {"type": "string", "default": ""},
                "search": {"type": "string", "default": ""},
                "stacked": {"type": "boolean", "default": False},
                "cumulated": {"type": "boolean", "default": False},
                "custom_domain": _domain_schema(),
            },
            "required": ["menu_id", "model_name"],
        },
    },
    "ir_actions_server_open_menu_pivot": {
        "description": "Open a menu's pivot view (cross-tab) with row/column groupings and measures.",
        "parameters": {
            "type": "object",
            "properties": {
                "menu_id": {"type": "integer"},
                "model_name": {"type": "string"},
                "selected_filters": _string_array("Filters to apply.", default=[]),
                "row_groupbys": _string_array("Row group-by fields.", default=[]),
                "col_groupbys": _string_array("Column group-by fields.", default=[]),
                "measures": _string_array("Measures to compute, e.g. ['amount_total:sum'].", default=[]),
                "search": {"type": "string", "default": ""},
                "custom_domain": _domain_schema(),
            },
            "required": ["menu_id", "model_name"],
        },
    },
    "ir_actions_server_adjust_search": {
        "description": (
            "Adjust filters / groupings / measures of the currently-open view. "
            "Use only when the user is already looking at a view and asks to "
            "filter, sort, group, or change the chart settings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {"type": "string"},
                "remove_facets": _string_array("Active facets to remove.", default=[]),
                "toggle_filters": _string_array("Filters to toggle.", default=[]),
                "toggle_groupbys": _string_array("Groupbys to toggle.", default=[]),
                "apply_searches": _string_array("Free-text searches to add.", default=[]),
                "measures": _string_array("Measures to apply.", default=[]),
                "mode": {"type": "string", "default": ""},
                "order": {"type": "string", "default": ""},
                "stacked": {"type": "boolean", "default": False},
                "cumulated": {"type": "boolean", "default": False},
                "custom_domain": _domain_schema(),
                "switch_view_type": {"type": "string", "description": "Switch to 'list', 'kanban', 'graph', 'pivot', etc.", "default": ""},
            },
            "required": ["model_name"],
        },
    },
    "ir_actions_server_compute_report_measures": {
        "description": "Get the list of available measures for a report action (used before plotting).",
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {"type": "integer"},
                "model_name": {"type": "string"},
            },
            "required": ["action_id", "model_name"],
        },
    },
}


def annotate_tools(tool_names):
    """Given a list of tool name strings, return Anthropic tool
    definitions backed by ``TOOL_SCHEMAS`` for known names and a stub
    schema for unknowns.

    Anthropic's tool envelope::

        [{"name": "...",
          "description": "...",
          "input_schema": {<JSON schema>}}]

    No outer ``{"type": "function", "function": {...}}`` wrapper —
    schemas land in ``input_schema`` directly.
    """
    out = []
    for name in tool_names or []:
        if not isinstance(name, str):
            continue
        schema = TOOL_SCHEMAS.get(name)
        if schema is not None:
            out.append({
                "name": name,
                "description": schema["description"],
                "input_schema": schema["parameters"],
            })
        else:
            out.append({
                "name": name,
                "description": name.replace("_", " ").strip(),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            })
    return out


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
                    "input_schema": tool.get("input_schema")
                        or tool.get("parameters")
                        or {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        },
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


# ---------------------------------------------------------------------------
# Tool-call execution
# ---------------------------------------------------------------------------


def _safe_jsonable(value, depth=0):
    if depth > 6:
        return f"<truncated {type(value).__name__}>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_jsonable(v, depth + 1) for v in value]
    if hasattr(value, "_name") and hasattr(value, "ids"):
        try:
            return {
                "_model": getattr(value, "_name", None),
                "ids": list(value.ids),
            }
        except Exception:  # noqa: BLE001
            return f"<recordset {value!r}>"
    return str(value)


_JSON_STRING_PARAMS = ("domain", "having", "custom_domain")

_WRAPPER_KEYS = ("params", "body", "payload", "data", "arguments",
                 "input", "request", "kwargs")

_PARAM_ALIASES = {
    "model": "model_name",
    "model_id": "model_name",
    "model_technical_name": "model_name",
    "ir_model": "model_name",
    "modelName": "model_name",
}


def _coerce_args(raw_args):
    """Parse a Claude tool_use ``input`` field (or stringified arguments)
    into a kwargs dict for the underlying ``_ai_tool_*`` method.

    Anthropic delivers tool inputs already-decoded as JSON objects,
    but we still accept the string form for forward-compatibility with
    any future format change.
    """
    if isinstance(raw_args, dict):
        kwargs = dict(raw_args)
    elif isinstance(raw_args, str):
        kwargs = json.loads(raw_args) if raw_args.strip() else {}
    else:
        kwargs = dict(raw_args)

    if not isinstance(kwargs, dict):
        return kwargs

    for _ in range(3):
        if (
            len(kwargs) == 1
            and next(iter(kwargs)) in _WRAPPER_KEYS
            and isinstance(next(iter(kwargs.values())), dict)
        ):
            kwargs = dict(next(iter(kwargs.values())))
        else:
            break

    out = {}
    for k, v in kwargs.items():
        target_key = _PARAM_ALIASES.get(k, k)

        if isinstance(v, str) and v and v[0] in "[{":
            try:
                v = json.loads(v)
            except (ValueError, TypeError):
                pass

        if target_key in _JSON_STRING_PARAMS:
            if isinstance(v, (list, dict)):
                try:
                    v = json.dumps(v)
                except Exception:  # noqa: BLE001
                    v = "[]"
            elif v is None or v == "":
                v = "[]"
            elif not isinstance(v, str):
                v = str(v)

        if target_key in out and target_key != k:
            continue
        out[target_key] = v
    return out


def _build_signature_hint(method, fn_name):
    try:
        import inspect
        sig = inspect.signature(method)
        return f"Expected signature: {fn_name}{sig}"
    except (TypeError, ValueError):
        return ""


_LOG_TOOL_FLAG_ICP = "daadit_ai_claude.log_tool_results"
_LOG_TOOL_FLAG_WARNED = False


def _result_logging_enabled(env):
    global _LOG_TOOL_FLAG_WARNED
    try:
        flag = env["ir.config_parameter"].sudo().get_param(
            _LOG_TOOL_FLAG_ICP, default="False"
        )
    except Exception:  # noqa: BLE001
        return False
    on = str(flag).strip().lower() in ("1", "true", "t", "yes", "y")
    if on and not _LOG_TOOL_FLAG_WARNED:
        _LOG_TOOL_FLAG_WARNED = True
        _logger.warning(
            "daadit_ai_claude: PII-logging flag is ENABLED — every "
            "Claude tool call's arguments and results (capped at "
            "1500 chars) are being persisted to ir.logging. This is "
            "intended only for short-lived debugging sessions. Set "
            "ir.config_parameter %r to False to disable.",
            _LOG_TOOL_FLAG_ICP,
        )
    return on


def _record_in_ir_logging(env, level, name, message):
    if level == "INFO" and not _result_logging_enabled(env):
        return
    try:
        env["ir.logging"].sudo().create({
            "name": name,
            "type": "server",
            "level": level,
            "message": message[:8000],
            "path": "daadit_ai_claude",
            "func": "run_tool_call",
            "line": "0",
        })
        env.cr.commit()
    except Exception:  # noqa: BLE001
        pass


def run_tool_call(agent, tool_use):
    """Execute one Anthropic ``tool_use`` block against an ``ai.agent``
    record.

    ``agent`` is the recordset on which to dispatch (singleton).
    ``tool_use`` is a dict shaped like::

        {"type": "tool_use",
         "id": "toolu_…",
         "name": "ir_actions_server_search",
         "input": {...}}

    Returns a JSON-serialisable result, or ``{"error": "…"}`` on
    failure. Each call is persisted to ``ir.logging`` so args + outcome
    are inspectable post-hoc.
    """
    if not tool_use or not isinstance(tool_use, dict):
        return {"error": "Invalid tool_use object"}

    fn_name = tool_use.get("name") or ""
    raw_args = tool_use.get("input")
    if raw_args is None:
        raw_args = {}
    env = getattr(agent, "env", None)

    try:
        kwargs = _coerce_args(raw_args)
    except (ValueError, TypeError) as exc:
        _logger.warning(
            "daadit_ai_claude.tool_dispatch: could not parse arguments "
            "for %s: %s | raw=%r", fn_name, exc, str(raw_args)[:300],
        )
        if env is not None:
            _record_in_ir_logging(
                env, "WARNING", "daadit_ai_claude.tool_dispatch",
                f"PARSE_ERROR fn={fn_name} raw={str(raw_args)[:500]} "
                f"err={exc}",
            )
        return {"error": (
            f"Could not parse tool input as JSON: {exc}. "
            f"Pass arguments as a JSON object whose values are typed."
        )}

    action = _resolve_tool_action(agent, fn_name)
    method_name = _tool_name_to_method(fn_name)
    method = getattr(agent, method_name, None)
    if action is None and not callable(method):
        _logger.warning(
            "daadit_ai_claude.tool_dispatch: unknown tool %r → %s "
            "(method not on ai.agent, no matching server action)",
            fn_name, method_name,
        )
        if env is not None:
            _record_in_ir_logging(
                env, "WARNING", "daadit_ai_claude.tool_dispatch",
                f"UNKNOWN_TOOL fn={fn_name} method={method_name}",
            )
        return {"error": f"Unknown tool: {fn_name}"}

    if not isinstance(kwargs, dict):
        return {"error": (
            f"Tool {fn_name} input must be a JSON object, got "
            f"{type(kwargs).__name__}."
        )}

    # ----- Per-agent model access control -----------------------------
    requested_model = kwargs.get("model_name")
    if requested_model and hasattr(agent, "_daadit_is_model_allowed"):
        try:
            allowed = agent._daadit_is_model_allowed(requested_model)
        except Exception:  # noqa: BLE001
            allowed = True
            _logger.exception(
                "daadit_ai_claude.tool_dispatch: model-allow check raised "
                "for agent=%s model=%s — falling back to allow",
                agent.id, requested_model,
            )
        if not allowed:
            allowed_list = sorted(
                agent.daadit_allowed_model_ids.mapped("model")
            ) if agent.daadit_allowed_model_ids else []
            blocked_list = sorted(
                agent.daadit_blocked_model_ids.mapped("model")
            ) if agent.daadit_blocked_model_ids else []
            _logger.info(
                "daadit_ai_claude.tool_dispatch: ACCESS_DENIED "
                "agent=%s tool=%s model=%s (allowed=%s, blocked=%s)",
                agent.id, fn_name, requested_model,
                allowed_list, blocked_list,
            )
            if env is not None:
                _record_in_ir_logging(
                    env, "WARNING", "daadit_ai_claude.tool_dispatch",
                    f"ACCESS_DENIED fn={fn_name} model={requested_model} "
                    f"agent={agent.id} allowed={allowed_list} "
                    f"blocked={blocked_list}",
                )
            hint = ""
            if allowed_list:
                hint = (
                    f" This agent can only query the following models: "
                    f"{', '.join(allowed_list)}."
                )
            elif blocked_list and requested_model in blocked_list:
                hint = (
                    f" Model '{requested_model}' is on this agent's "
                    f"block list."
                )
            return {
                "_daadit_access_denied": True,
                "tool_name": fn_name,
                "model_name": requested_model,
                "allowed_models": allowed_list,
                "blocked_models": blocked_list,
                "error": (
                    f"Access denied: agent is not permitted to query "
                    f"model '{requested_model}'.{hint}"
                ),
            }

    # ----- Field-level privacy gate (request side) --------------------
    _model_for_priv = kwargs.get("model_name") or kwargs.get("model") or ""
    if _model_for_priv:
        if kwargs.get("domain") is not None:
            try:
                bad_field = agent._daadit_domain_uses_blocked_field(
                    _model_for_priv, kwargs.get("domain"),
                )
            except Exception:  # noqa: BLE001
                bad_field = ""
            if bad_field:
                return {"error": (
                    f"Filtering on '{bad_field}' is forbidden by this "
                    f"agent's privacy policy on '{_model_for_priv}'."
                )}
        try:
            blocked_fields = agent._daadit_blocked_field_set(_model_for_priv)
        except Exception:  # noqa: BLE001
            blocked_fields = set()
        if blocked_fields:
            def _strip_suffix(s):
                if not isinstance(s, str):
                    return ""
                return s.split(":", 1)[0].strip()

            for arg_name in ("groupby", "aggregates"):
                values = kwargs.get(arg_name)
                if not values:
                    continue
                if isinstance(values, str):
                    values = [values]
                for v in values:
                    field_part = _strip_suffix(v)
                    if field_part in blocked_fields:
                        return {"error": (
                            f"Using '{field_part}' in {arg_name} is "
                            f"forbidden by this agent's privacy policy "
                            f"on '{_model_for_priv}'."
                        )}
            if kwargs.get("having") is not None:
                try:
                    bad_having = agent._daadit_domain_uses_blocked_field(
                        _model_for_priv, kwargs.get("having"),
                    )
                except Exception:  # noqa: BLE001
                    bad_having = ""
                if bad_having:
                    return {"error": (
                        f"Filtering on '{bad_having}' in having is "
                        f"forbidden by this agent's privacy policy on "
                        f"'{_model_for_priv}'."
                    )}

    try:
        if action is not None:
            # Preferred path: stock's executor validates the action's
            # ai_tool_schema and runs the action BODY — including any
            # operator guard code — exactly like the native AI flow.
            result = action._ai_tool_run(agent, kwargs)
        else:
            result = method(**kwargs)
    except TypeError as exc:
        sig_hint = _build_signature_hint(method, fn_name)
        _logger.warning(
            "daadit_ai_claude.tool_dispatch: %s(**%r) raised TypeError: %s",
            method_name, kwargs, exc,
        )
        if env is not None:
            _record_in_ir_logging(
                env, "WARNING", "daadit_ai_claude.tool_dispatch",
                f"TYPEERROR fn={fn_name} args={json.dumps(kwargs, default=str)[:1500]} "
                f"err={exc} | {sig_hint}",
            )
        return {"error": (
            f"Tool {fn_name} signature error: {exc}. {sig_hint} "
        )}
    except Exception as exc:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.tool_dispatch: %s raised", method_name,
        )
        if env is not None:
            _record_in_ir_logging(
                env, "ERROR", "daadit_ai_claude.tool_dispatch",
                f"RAISED fn={fn_name} args={json.dumps(kwargs, default=str)[:1500]} "
                f"err_type={type(exc).__name__} err={exc}",
            )
        return {"error": (
            f"Tool {fn_name} raised {type(exc).__name__}: {exc}."
        )}

    # ----- Field-level privacy gate (response side) -------------------
    if _model_for_priv and result is not None:
        try:
            result = agent._daadit_scrub_result(_model_for_priv, result)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_claude.tool_dispatch: scrub_result failed for "
                "%s on model %s", method_name, _model_for_priv,
            )

    safe = _safe_jsonable(result)
    _logger.info(
        "daadit_ai_claude.tool_dispatch: %s ok (args_keys=%s, "
        "result_type=%s)",
        method_name, list(kwargs.keys()), type(result).__name__,
    )
    if env is not None:
        try:
            result_summary = json.dumps(safe, default=str)[:1500]
        except Exception:  # noqa: BLE001
            result_summary = str(safe)[:1500]
        _record_in_ir_logging(
            env, "INFO", "daadit_ai_claude.tool_dispatch",
            f"OK fn={fn_name} args={json.dumps(kwargs, default=str)[:1500]} "
            f"result={result_summary}",
        )
    return safe
