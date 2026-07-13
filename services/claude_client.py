# -*- coding: utf-8 -*-
"""HTTP client for the Anthropic Claude API (Messages endpoint).

Anthropic's wire format:

  * Endpoint:  POST /v1/messages
  * Auth:      ``x-api-key: <key>`` (NOT Bearer)
  * Required:  ``anthropic-version: 2023-06-01`` header
  * Request:   ``system`` is a TOP-LEVEL field (not a message),
               ``messages`` only carries user/assistant turns.
  * Tools:     ``[{"name":"...", "description":"...",
                    "input_schema": {...JSON Schema...}}]``
  * Response:  ``content`` is a list of blocks
               ([{"type":"text","text":"..."},
                 {"type":"tool_use","id":"...","name":"...","input":{}}])
  * Tool result: passed back as a user message with
                 ``[{"type":"tool_result","tool_use_id":"...",
                     "content":"..."}]`` content blocks.
  * Required:  ``max_tokens`` is REQUIRED on every call (not optional).
  * No /v1/embeddings — Anthropic does not provide an embeddings API.

Docs: https://docs.anthropic.com/en/api/messages
"""
import json
import logging
from typing import List, Mapping, Optional, Sequence

import requests

from odoo.exceptions import UserError
from odoo.tools.translate import _

_logger = logging.getLogger(__name__)


# Chat / completion models we expose. Keep in sync with ai_agent.py's selection.
# As of 2026-05: Claude 4.7 (Opus), 4.6 (Sonnet), 4.5 family (Haiku/Sonnet/Opus).
SUPPORTED_MODELS = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    # Compatibility / older snapshots — keep so existing agent records
    # don't break after a model retirement.
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
)


# Anthropic's API version header. Required on every request.
# https://docs.anthropic.com/en/api/versioning
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

# Hard ceiling for max_tokens. Anthropic's actual per-model limits are
# higher (e.g. 200k context, 8k–64k output), but we cap at a sane default
# so a mis-saved override can't run up a huge bill on a runaway loop.
_DEFAULT_MAX_TOKENS = 4096
_HARD_MAX_TOKENS = 64000


# Keys that ``extra`` is allowed to set on a messages payload. Anything
# outside this set is silently dropped — preventing a misbehaving
# caller from overriding ``messages`` / ``tools`` / ``model`` via the
# convenience kwarg.
_ALLOWED_EXTRA_KEYS = frozenset({
    "top_p",
    "top_k",
    "stop_sequences",
    "metadata",
    "service_tier",
})


def is_claude_model(model_id: str) -> bool:
    """Return True if ``model_id`` is one we should route to Claude."""
    if not model_id:
        return False
    return model_id in SUPPORTED_MODELS or model_id.startswith("claude-")


def _stringify_error(value) -> str:
    """Coerce an Anthropic JSON error field to a one-line string.

    Anthropic's error shape::

        {"type": "error",
         "error": {"type": "invalid_request_error", "message": "..."}}
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        msg = value.get("message") or value.get("msg")
        if msg:
            return _stringify_error(msg)
        if "error" in value:
            return _stringify_error(value["error"])
        if "detail" in value:
            return _stringify_error(value["detail"])
        return json.dumps(value)[:500]
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                msg = item.get("message") or item.get("msg") or ""
                if msg:
                    parts.append(str(msg))
                else:
                    parts.append(json.dumps(item)[:200])
            else:
                parts.append(str(item))
        return "; ".join(parts)[:500]
    return str(value)[:300]


class ClaudeClient:
    """Thin wrapper around requests for Anthropic's Messages API.

    Stateless and instantiated per-call from the dispatch overrides on
    ``ai.agent``. The ``from_env`` factory pulls config from
    ``ir.config_parameter`` so no model env binding is needed at import
    time.
    """

    def __init__(self, api_key: str, base_url: str, timeout: int = 60,
                 anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
                 env=None):
        if not api_key:
            raise UserError(_(
                "No Anthropic API key is configured. Enable 'Use your own "
                "Anthropic account' in General Settings → AI and paste your "
                "key from console.anthropic.com."
            ))
        normalised = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        if env is not None:
            from odoo.addons.daadit_ai_claude.models.res_config_settings import (
                ResConfigSettings,
            )
            try:
                # Use Claude-specific name to avoid colliding with
                # Mistral's _validate_base_url on the merged class.
                ResConfigSettings._daadit_claude_validate_base_url(
                    env, normalised,
                )
            except Exception:
                raise
        try:
            timeout = max(1, min(int(timeout or 60), 600))
        except (TypeError, ValueError):
            timeout = 60
        self.api_key = api_key
        self.base_url = normalised
        self.timeout = timeout
        self.anthropic_version = anthropic_version or DEFAULT_ANTHROPIC_VERSION

    # --- factory ---------------------------------------------------------

    @classmethod
    def from_env(cls, env) -> "ClaudeClient":
        """Build a client from ``ir.config_parameter`` values."""
        ICP = env["ir.config_parameter"].sudo()
        if ICP.get_param("daadit_ai_claude.claude_key_enabled") not in (
            "True",
            "1",
            True,
        ):
            raise UserError(_(
                "Anthropic API key is not enabled. Toggle 'Use your own "
                "Anthropic account' in General Settings → AI."
            ))
        return cls(
            api_key=ICP.get_param("daadit_ai_claude.claude_key", default=""),
            base_url=ICP.get_param(
                "daadit_ai_claude.claude_base_url",
                default="https://api.anthropic.com/v1",
            ),
            timeout=int(
                ICP.get_param("daadit_ai_claude.claude_timeout", default="60")
                or 60
            ),
            anthropic_version=ICP.get_param(
                "daadit_ai_claude.claude_api_version",
                default=DEFAULT_ANTHROPIC_VERSION,
            ),
            env=env,
        )

    # --- internal HTTP helper -------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = requests.post(
                url,
                headers=headers,
                data=json.dumps(payload),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Anthropic request failed: {exc}") from exc

        if 400 <= resp.status_code < 500:
            try:
                err = resp.json() or {}
            except ValueError:
                err = {}

            detail = (
                _stringify_error(err.get("error"))
                or _stringify_error(err.get("message"))
                or _stringify_error(err.get("detail"))
                or resp.text[:500]
            )

            _logger.warning(
                "Anthropic API %d on %s | model=%s msgs=%d tools=%d | "
                "detail=%s",
                resp.status_code, path,
                payload.get("model") if isinstance(payload, dict) else "?",
                len(payload.get("messages") or []) if isinstance(payload, dict) else 0,
                len(payload.get("tools") or []) if isinstance(payload, dict) else 0,
                (detail or "")[:300],
            )
            raise UserError(_(
                "Anthropic API rejected the request (HTTP %(status)s): %(detail)s",
                status=resp.status_code,
                detail=detail,
            ))
        if resp.status_code >= 500:
            raise RuntimeError(
                f"Anthropic API server error {resp.status_code}: {resp.text[:200]}"
            )

        return resp.json()

    # --- messages (chat) -------------------------------------------------

    def create_message(
        self,
        model: str,
        messages: List[Mapping[str, object]],
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[Sequence[Mapping[str, object]]] = None,
        tool_choice: Optional[object] = None,
        extra: Optional[Mapping[str, object]] = None,
    ) -> dict:
        """Call ``POST /messages``.

        ``messages`` must contain only user/assistant turns. The
        ``system`` prompt (if any) goes in the top-level ``system``
        field. ``max_tokens`` is REQUIRED by the Anthropic API — if the
        caller didn't provide one we fall back to ``_DEFAULT_MAX_TOKENS``.

        Tools follow Anthropic's envelope:

            tools = [{"name": "...", "description": "...",
                      "input_schema": {...JSON Schema...}}]
            tool_choice = {"type": "auto"}
                        | {"type": "any"}
                        | {"type": "tool", "name": "..."}
                        | {"type": "none"}
        """
        # max_tokens is mandatory.
        try:
            mt = int(max_tokens) if max_tokens else _DEFAULT_MAX_TOKENS
        except (TypeError, ValueError):
            mt = _DEFAULT_MAX_TOKENS
        mt = max(1, min(mt, _HARD_MAX_TOKENS))

        payload: dict = {
            "model": model,
            "messages": list(messages),
            "max_tokens": mt,
        }
        if system:
            payload["system"] = str(system)
        if temperature is not None:
            try:
                payload["temperature"] = float(temperature)
            except (TypeError, ValueError):
                pass
        if tools:
            payload["tools"] = list(tools)
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if extra:
            for k, v in extra.items():
                if k in _ALLOWED_EXTRA_KEYS:
                    payload[k] = v
                else:
                    _logger.debug(
                        "Anthropic messages: dropped non-allowlisted extra "
                        "kwarg %r", k,
                    )

        _logger.debug(
            "Anthropic → model=%s msgs=%d tools=%d max_tokens=%d",
            model, len(messages), len(tools or []), mt,
        )
        return self._post("/messages", payload)

    # --- convenience extractors -----------------------------------------

    @staticmethod
    def extract_text(response: Mapping[str, object]) -> str:
        """Concatenate all text blocks from a Claude response."""
        content = response.get("content") or []
        if not isinstance(content, list):
            return ""
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "".join(parts)

    @staticmethod
    def extract_tool_uses(response: Mapping[str, object]) -> list:
        """Return the ``tool_use`` content blocks from the response.

        Each tool_use block::

            {"type": "tool_use",
             "id": "toolu_…",
             "name": "ir_actions_server_search",
             "input": {...}}
        """
        content = response.get("content") or []
        if not isinstance(content, list):
            return []
        return [
            block for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]

    @staticmethod
    def extract_stop_reason(response: Mapping[str, object]) -> str:
        return str(response.get("stop_reason") or "")

    @staticmethod
    def extract_usage(response: Mapping[str, object]) -> dict:
        """Normalise Anthropic's usage block to the
        ``prompt_tokens`` / ``completion_tokens`` shape the usage
        recorder expects."""
        u = dict(response.get("usage") or {})
        return {
            "prompt_tokens": u.get("input_tokens") or 0,
            "completion_tokens": u.get("output_tokens") or 0,
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens") or 0,
            "cache_read_input_tokens": u.get("cache_read_input_tokens") or 0,
        }
