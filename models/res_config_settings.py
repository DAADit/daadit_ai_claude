# -*- coding: utf-8 -*-
"""Settings UI for the Anthropic Claude provider.

Anthropic settings are presented as an inline block in
Settings → General Settings → AI, matching the visual style of the
other AI provider blocks (ChatGPT, Gemini, etc.). All values are
stored in ``ir.config_parameter`` under the
``daadit_ai_claude.*`` namespace.

Cross-provider safety net
-------------------------
Because ``res.config.settings`` is a TransientModel shared by all
modules that extend it, every module's ``@api.constrains`` fires on
every Save. If a user accidentally pastes an Anthropic URL into
another provider's URL field (e.g. Mistral's), that module's
validator would surface a confusing error.

This module installs two defensive layers to keep that from
happening:

  1. An ``@api.onchange("mistral_base_url")`` handler that, in the
     form UI, watches the Mistral URL field and immediately reroutes
     any Anthropic-flavoured URL to ``claude_base_url`` — before the
     form is ever submitted. Triggers on the standard paste→blur
     event in Odoo's form view.

  2. A ``create()`` / ``write()`` server-side override that does the
     same rerouting on the transient record's vals, BEFORE
     ``@api.constrains`` can fire. Covers edge cases: paste-and-
     immediately-Save, programmatic XML-RPC writes, stale browser JS.

Both layers are no-ops when the Mistral module is not installed
(``mistral_base_url`` not on the merged class).
"""
from urllib.parse import urlparse

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


CLAUDE_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
CLAUDE_DEFAULT_API_VERSION = "2023-06-01"

_DEFAULT_ALLOWED_HOSTS = (
    "api.anthropic.com",
)

_MIN_TIMEOUT_SECONDS = 1
_MAX_TIMEOUT_SECONDS = 600


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    claude_key_enabled = fields.Boolean(
        string="Enable custom Anthropic API key",
        config_parameter="daadit_ai_claude.claude_key_enabled",
        help=(
            "When enabled, AI agents configured to use a Claude model "
            "will call api.anthropic.com directly using the key below."
        ),
    )
    claude_key = fields.Char(
        string="Anthropic API key",
        config_parameter="daadit_ai_claude.claude_key",
        help=(
            "Personal API key from console.anthropic.com. Starts with "
            "sk-ant-. Stored in ir.config_parameter — only visible to "
            "users with the Settings right (group_system)."
        ),
    )
    claude_base_url = fields.Char(
        string="Anthropic API base URL",
        config_parameter="daadit_ai_claude.claude_base_url",
        default=CLAUDE_DEFAULT_BASE_URL,
        help=(
            "Anthropic API endpoint. Defaults to "
            "https://api.anthropic.com/v1. Must be https:// and the "
            "host must be on the allowlist."
        ),
    )
    claude_api_version = fields.Char(
        string="Anthropic API version",
        config_parameter="daadit_ai_claude.claude_api_version",
        default=CLAUDE_DEFAULT_API_VERSION,
        help=(
            "anthropic-version header sent on every request. Default "
            "2023-06-01."
        ),
    )
    claude_timeout = fields.Integer(
        string="Anthropic request timeout (seconds)",
        config_parameter="daadit_ai_claude.claude_timeout",
        default=60,
        help=(
            "How long to wait for an Anthropic response before giving "
            "up. Minimum 1, maximum 600 seconds."
        ),
    )

    # ------------------------------------------------------------------
    # Cross-provider safety net — server-side
    # ------------------------------------------------------------------
    @staticmethod
    def _daadit_claude_is_anthropic_url(url):
        if not url or not isinstance(url, str):
            return False
        url = url.strip()
        if not url:
            return False
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:  # noqa: BLE001
            host = url.lower()
        return "anthropic" in host

    @staticmethod
    def _daadit_claude_sanitize_vals(vals, mistral_field_present):
        """If ``vals`` is routing an Anthropic URL to mistral_base_url,
        move it to claude_base_url and REMOVE mistral_base_url from
        vals entirely.

        Why ``.pop()`` instead of resetting to default: previous tests
        (v19.0.1.14.0 diagnostic) showed that even after setting
        ``mistral_base_url`` to the Mistral default in vals, Mistral's
        validator still saw the original Anthropic URL. The exact
        mechanism is fuzzy (likely config_parameter inverse + cache
        interaction), but popping the key entirely avoids the issue
        — the field then falls back to its ICP-backed default, which
        Mistral's validator short-circuits on (idempotent skip).
        """
        if not isinstance(vals, dict) or not mistral_field_present:
            return vals
        url = vals.get("mistral_base_url")
        if not ResConfigSettings._daadit_claude_is_anthropic_url(url):
            return vals
        existing_claude = (vals.get("claude_base_url") or "").strip()
        if not existing_claude or existing_claude == CLAUDE_DEFAULT_BASE_URL:
            vals["claude_base_url"] = url
            vals["claude_key_enabled"] = True
        # POP — don't set to default. Let the field-default kick in.
        vals.pop("mistral_base_url", None)
        return vals

    def _daadit_claude_log_diag(self, where, payload):
        """Best-effort diagnostic logger — writes to ir.logging so
        the override's behavior can be inspected via XML-RPC."""
        try:
            self.env["ir.logging"].sudo().create({
                "name": "daadit_ai_claude.coexistence",
                "type": "server",
                "level": "INFO",
                "message": f"{where}: {payload!r}",
                "path": "daadit_ai_claude.res_config_settings",
                "func": where,
                "line": "0",
            })
            self.env.cr.commit()
        except Exception:  # noqa: BLE001
            pass

    @api.model_create_multi
    def create(self, vals_list):
        # ALWAYS log entry to ir.logging — first thing we do, so we
        # can verify the override runs even when no mutation is needed.
        self._daadit_claude_log_diag(
            "create_entry",
            {
                "vals_list_type": type(vals_list).__name__,
                "vals_list_len": len(vals_list) if hasattr(vals_list, "__len__") else "?",
                "vals_preview": [
                    {k: v for k, v in (v.items() if isinstance(v, dict) else [])
                     if k in ("mistral_base_url", "claude_base_url",
                              "mistral_key_enabled", "claude_key_enabled")}
                    for v in (vals_list if isinstance(vals_list, list) else [vals_list])
                ],
                "mistral_field_present": "mistral_base_url" in self._fields,
            },
        )
        mistral_field_present = "mistral_base_url" in self._fields
        if mistral_field_present:
            list_form = vals_list if isinstance(vals_list, list) else [vals_list]
            for vals in list_form:
                if not isinstance(vals, dict):
                    continue
                before_url = vals.get("mistral_base_url")
                had_mistral_key = "mistral_base_url" in vals
                self._daadit_claude_sanitize_vals(vals, True)
                still_has = "mistral_base_url" in vals
                if had_mistral_key and not still_has:
                    self._daadit_claude_log_diag(
                        "create_popped",
                        {
                            "popped_value": before_url,
                            "claude_base_url": vals.get("claude_base_url"),
                            "claude_key_enabled": vals.get("claude_key_enabled"),
                        },
                    )
        # Log final vals_list right before super() — this is what
        # super().create() actually receives.
        self._daadit_claude_log_diag(
            "calling_super",
            {"final_vals_list": [
                {k: v for k, v in (v.items() if isinstance(v, dict) else [])
                 if k in ("mistral_base_url", "claude_base_url",
                          "mistral_key_enabled", "claude_key_enabled")}
                for v in (vals_list if isinstance(vals_list, list) else [vals_list])
            ]},
        )
        return super().create(vals_list)

    def write(self, vals):
        self._daadit_claude_log_diag(
            "write_entry",
            {
                "vals_keys": sorted(vals.keys()) if isinstance(vals, dict) else "?",
                "has_mistral_field": "mistral_base_url" in self._fields,
            },
        )
        mistral_field_present = "mistral_base_url" in self._fields
        if mistral_field_present and isinstance(vals, dict) and "mistral_base_url" in vals:
            before = vals.get("mistral_base_url")
            self._daadit_claude_sanitize_vals(vals, True)
            after = vals.get("mistral_base_url")
            if before != after:
                self._daadit_claude_log_diag(
                    "write_sanitized",
                    {"before": before, "after": after},
                )
        return super().write(vals)

    # ------------------------------------------------------------------
    # Cross-provider safety net — UI side (form onchange)
    # ------------------------------------------------------------------
    @api.onchange("mistral_base_url")
    def _daadit_claude_redirect_misplaced_anthropic_url(self):
        for rec in self:
            if "mistral_base_url" not in rec._fields:
                continue
            mistral_url = (
                getattr(rec, "mistral_base_url", None) or ""
            ).strip()
            if not mistral_url:
                continue
            try:
                host = (urlparse(mistral_url).hostname or "").lower()
            except Exception:  # noqa: BLE001
                host = mistral_url.lower()
            if "anthropic" not in host:
                continue
            current_claude_url = (rec.claude_base_url or "").strip()
            if not current_claude_url or current_claude_url == CLAUDE_DEFAULT_BASE_URL:
                rec.claude_base_url = mistral_url
                rec.claude_key_enabled = True
            try:
                rec.mistral_base_url = "https://api.mistral.ai/v1"
            except Exception:  # noqa: BLE001
                pass
            return {
                "warning": {
                    "title": _("URL routed to the Anthropic block"),
                    "message": _(
                        "You pasted an Anthropic URL into the Mistral "
                        "block — that's the wrong field. I've moved it "
                        "to the Anthropic Claude block."
                    ),
                }
            }

    # ------------------------------------------------------------------
    # Validation (fires only when the Claude toggle is on)
    # ------------------------------------------------------------------
    @api.constrains("claude_base_url", "claude_key_enabled")
    def _check_claude_base_url(self):
        for rec in self:
            if not rec.claude_key_enabled:
                continue
            url = (rec.claude_base_url or "").strip()
            if not url:
                continue
            # IMPORTANT: do NOT call self._validate_base_url here.
            # Both Mistral and Claude define a static method with that
            # same name. With both modules _inheriting res.config.
            # settings, the merged class only has ONE such method
            # (whichever module loaded later wins via MRO). Calling
            # via ``self.`` routes through MRO and we'd hit the wrong
            # validator — Mistral's, which rejects Anthropic URLs
            # under its own allowlist. Call our unambiguous helper
            # directly instead.
            ResConfigSettings._daadit_claude_validate_base_url(
                self.env, url,
            )

    @api.constrains("claude_timeout")
    def _check_claude_timeout(self):
        for rec in self:
            t = rec.claude_timeout or 0
            if t < _MIN_TIMEOUT_SECONDS:
                raise ValidationError(_(
                    "Anthropic request timeout must be at least %(min)s "
                    "second(s).",
                    min=_MIN_TIMEOUT_SECONDS,
                ))
            if t > _MAX_TIMEOUT_SECONDS:
                raise ValidationError(_(
                    "Anthropic request timeout %(t)ss is unreasonably "
                    "high (max %(max)ss).",
                    t=t, max=_MAX_TIMEOUT_SECONDS,
                ))

    # ------------------------------------------------------------------
    # URL allowlist validator
    #
    # IMPORTANT NAME: ``_daadit_claude_validate_base_url`` (NOT
    # ``_validate_base_url``). The Mistral module defines a method
    # with the latter name on the same _inherit'd class, so a generic
    # name would collide via MRO and route OUR Claude URL through
    # MISTRAL'S allowlist. Use a unique daadit_claude_ prefix.
    # ------------------------------------------------------------------
    @staticmethod
    def _daadit_claude_validate_base_url(env, url):
        try:
            parsed = urlparse(url)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(_(
                "Anthropic base URL is not a valid URL: %(err)s",
                err=str(exc),
            )) from exc

        if parsed.scheme.lower() != "https":
            raise ValidationError(_(
                "Anthropic base URL must use https:// — got %(scheme)r.",
                scheme=parsed.scheme or "(empty)",
            ))
        if parsed.username or parsed.password:
            raise ValidationError(_(
                "Anthropic base URL must not contain userinfo "
                "(user:pass@host)."
            ))
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValidationError(_("Anthropic base URL has no host."))

        import re
        if re.match(r"^[\d.:a-fA-F]+$", host) and any(
            ch.isdigit() for ch in host
        ) and (host.count(".") == 3 or ":" in host):
            raise ValidationError(_(
                "Anthropic base URL must use a hostname, not an IP "
                "literal (%(h)s).",
                h=host,
            ))

        allowed = set(_DEFAULT_ALLOWED_HOSTS)
        try:
            extra = env["ir.config_parameter"].sudo().get_param(
                "daadit_ai_claude.allowed_base_url_hosts", default=""
            ) or ""
            for entry in extra.split(","):
                entry = entry.strip().lower()
                if entry:
                    allowed.add(entry)
        except Exception:  # noqa: BLE001
            pass

        if host not in allowed:
            raise ValidationError(_(
                "Anthropic base URL host %(host)r is not on the "
                "allowlist. Allowed hosts: %(allowed)s.",
                host=host, allowed=", ".join(sorted(allowed)),
            ))
