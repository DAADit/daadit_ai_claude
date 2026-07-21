# -*- coding: utf-8 -*-
"""Settings UI for the Anthropic Claude provider.

Adds an inline provider block to Settings → General Settings → AI,
matching the visual style of the other AI provider blocks (ChatGPT,
Gemini, etc.). All values persist to ``ir.config_parameter`` under
the ``daadit_ai_claude.*`` namespace.

The base-URL validator is intentionally named
``_daadit_claude_validate_base_url`` — a claude-prefixed name that
cannot collide with any static helper on other AI provider modules
that also ``_inherit`` ``res.config.settings``. See the inline comment
on the method for details.
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
    # Validation
    # ------------------------------------------------------------------
    @api.constrains("claude_base_url", "claude_key_enabled")
    def _check_claude_base_url(self):
        for rec in self:
            if not rec.claude_key_enabled:
                continue
            url = (rec.claude_base_url or "").strip()
            if not url:
                continue
            # IMPORTANT: use the daadit_claude-prefixed name here — a
            # generic ``self._validate_base_url(...)`` would resolve
            # via MRO on the merged ``res.config.settings`` class and
            # could hit another provider module's same-named helper
            # (some provider modules also declare a
            # ``_validate_base_url`` @staticmethod). Direct-class access
            # via ``ResConfigSettings._daadit_claude_validate_base_url``
            # avoids the MRO and calls the correct validator every time.
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
    # Claude-prefixed name so it cannot collide with a same-named
    # static method on any other AI provider module that also
    # ``_inherit``s ``res.config.settings``. Also called from
    # ``ClaudeClient.from_env()`` so a URL poked directly into
    # ``ir.config_parameter`` still gets validated before we send the
    # API key.
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

    # ------------------------------------------------------------------
    # Model list — manual refresh from the settings screen
    #
    # The Claude model dropdown on ai.agent is fed from the live
    # ``daadit.ai.claude.model`` registry, refreshed daily by cron. This
    # button lets an admin pull the current list on demand (e.g. right
    # after Anthropic announces a new model). Runs against the SAVED
    # configuration, so save the key first if you just entered it.
    # ------------------------------------------------------------------
    def action_daadit_claude_sync_models(self):
        self.ensure_one()
        return self.env["daadit.ai.claude.model"].sudo().action_sync_now()
