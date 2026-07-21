# -*- coding: utf-8 -*-
"""Live registry of Claude models available via the Anthropic API.

The ``ai.agent.llm_model`` selection is fed from this table (see
``ai_agent.py``) so newly-released Claude models appear automatically
once the daily sync — or the manual "Refresh models" button — has run.
No code change or redeploy is needed when Anthropic ships a new model.

A small hardcoded seed (``data/claude_models_seed.xml``) keeps the
dropdown usable before the first successful sync (fresh install, no key
yet, or the API is unreachable).
"""
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class DaaditAiClaudeModel(models.Model):
    _name = "daadit.ai.claude.model"
    _description = "Claude model (synced from Anthropic API)"
    _order = "sequence, technical_name"

    technical_name = fields.Char(
        required=True,
        index=True,
        help="Model id sent to the Anthropic API, e.g. 'claude-opus-4-8'.",
    )
    display_name = fields.Char(
        help="Human-readable label shown in the agent model dropdown.",
    )
    active = fields.Boolean(
        default=True,
        help="Untick to hide this model from the agent dropdown without "
             "deleting it (keeps existing agents that use it valid).",
    )
    sequence = fields.Integer(default=10)
    synced_from_api = fields.Boolean(
        string="From API",
        readonly=True,
        help="True when this row was created/updated by a sync with the "
             "Anthropic API (as opposed to the built-in seed).",
    )
    last_synced = fields.Datetime(readonly=True)

    _sql_constraints = [
        ("technical_name_uniq", "unique(technical_name)",
         "This Claude model id already exists in the registry."),
    ]

    # ------------------------------------------------------------------ #
    # Selection source                                                   #
    # ------------------------------------------------------------------ #
    @api.model
    def _selection_entries(self):
        """Return ``(value, label)`` tuples for the ``llm_model`` field.

        Active rows only, ordered by sequence. Empty list if the table
        has no active rows — the caller then falls back to the seed
        constant so the dropdown is never empty.
        """
        recs = self.sudo().search(
            [("active", "=", True)], order="sequence, technical_name",
        )
        return [(r.technical_name, r.display_name or r.technical_name)
                for r in recs]

    # ------------------------------------------------------------------ #
    # Sync                                                               #
    # ------------------------------------------------------------------ #
    @api.model
    def _sync_from_api(self):
        """Fetch models from Anthropic and upsert rows. Returns the count.

        Raises (via ``ClaudeClient.from_env``) if the Anthropic key is
        not enabled — callers that must not crash (the cron) guard for
        that first.
        """
        from ..services.claude_client import ClaudeClient

        client = ClaudeClient.from_env(self.env)
        models_data = client.list_models()
        now = fields.Datetime.now()
        touched = 0
        for seq, item in enumerate(models_data, start=1):
            mid = item["id"]
            vals = {
                "display_name": item.get("display_name") or mid,
                "synced_from_api": True,
                "last_synced": now,
                "active": True,
                "sequence": seq,
            }
            rec = self.sudo().search(
                [("technical_name", "=", mid)], limit=1,
            )
            if rec:
                rec.write(vals)
            else:
                self.sudo().create(dict(vals, technical_name=mid))
            touched += 1
        _logger.info(
            "daadit_ai_claude: synced %d Claude models from the Anthropic "
            "API", touched,
        )
        return touched

    @api.model
    def _cron_sync_models(self):
        """Daily cron entry point. Never raises — a transient API error
        must not leave the scheduled action in a failed state."""
        ICP = self.env["ir.config_parameter"].sudo()
        if ICP.get_param("daadit_ai_claude.claude_key_enabled") not in (
            "True", "1", True,
        ):
            _logger.info(
                "daadit_ai_claude: model sync skipped (Anthropic key "
                "disabled)"
            )
            return False
        try:
            self._sync_from_api()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_claude: scheduled Claude model sync failed"
            )
            return False
        return True

    def action_sync_now(self):
        """Manual refresh from the list view / settings button."""
        count = self._sync_from_api()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("Claude models refreshed"),
                "message": _(
                    "%s model(s) synced from the Anthropic API.", count,
                ),
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
