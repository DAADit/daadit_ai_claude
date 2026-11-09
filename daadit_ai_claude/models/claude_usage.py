# -*- coding: utf-8 -*-
"""Token-usage tracking for the Claude (Anthropic) provider.

Each chat call records one row in ``daadit_ai_claude.usage`` with the
agent / channel / user / model / token counts and an estimated cost.

Pricing
-------
Per-million-token prices for input/output are stored in a module-level
constant ``_PRICING_USD_PER_1M``. They're a snapshot of Anthropic's
public list price at the time of writing — keep them roughly accurate
but don't rely on this module for invoicing. Override via
``ir.config_parameter`` if needed:

    daadit_ai_claude.price.<model>.input    -> USD per 1M input tokens
    daadit_ai_claude.price.<model>.output   -> USD per 1M output tokens

Currency is USD; the ``estimated_cost_usd`` field stores the raw USD
value.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# Pricing snapshot — USD per 1,000,000 tokens (input, output).
# Bron: de modellentabel van Anthropic, stand 3 november 2026. Loopt
# achter zodra er een model bijkomt — overschrijven kan via ICP.
#
# v19.0.4.5.3: de 5.x-familie stond hier niet. Bo werkt sinds begin
# november 2026 op ``claude-haiku-5-5`` en elke run werd daardoor op
# nul dollar geteld: 116 regels, 24,5 miljoen tokens, kosten 0,00. Een
# meter die een onbekend model gratis vindt, is geen meter. Opus 4.7
# stond bovendien op 15/75 terwijl het 5/25 is.
#
# Haiku 5.5 kost boven de 100.000 tokens per prompt vijf keer zoveel
# (0,50 / 2,50). Een regel hier telt de tokens van alle rondes van een
# beurt bij elkaar, dus welke prompt over die grens ging is niet meer
# te zien. We rekenen met het basistarief; het echte bedrag kan voor
# zulke beurten hoger liggen.
_PRICING_USD_PER_1M = {
    "claude-fable-5-2":         (8.0,  40.0),
    "claude-fable-5-1":         (10.0, 50.0),
    "claude-fable-5":           (10.0, 50.0),
    "claude-opus-5-5":          (4.0,  20.0),
    "claude-opus-5":            (5.0,  25.0),
    "claude-opus-4-8":          (5.0,  25.0),
    "claude-opus-4-7":          (5.0,  25.0),
    "claude-opus-4-6":          (5.0,  25.0),
    "claude-sonnet-5-5":        (2.0,  10.0),
    "claude-sonnet-5":          (2.0,  10.0),
    "claude-sonnet-4-6":        (3.0,  15.0),
    "claude-haiku-5-5":         (0.1,  0.5),
    "claude-haiku-4-5-20251001": (1.0,  5.0),
    "claude-haiku-4-5":         (1.0,  5.0),
    # Ouder, niet meer in de tabel van Anthropic; laten staan voor
    # regels uit het verleden.
    "claude-opus-4-5":          (15.0, 75.0),
    "claude-sonnet-4-5":        (3.0,  15.0),
}


def _get_unit_price(env, model_id, kind):
    """USD per 1M tokens voor ``kind`` ('input' of 'output').

    Volgorde: ICP-overschrijving, dan de tabel hierboven. Geeft 0.0 als
    het model nergens staat — wie een bedrag nodig heeft gebruikt
    ``_resolve_unit_prices``, dat een onbekend model niet gratis laat.
    """
    icp = env["ir.config_parameter"].sudo()
    key = f"daadit_ai_claude.price.{model_id}.{kind}"
    raw = icp.get_param(key)
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    pair = _PRICING_USD_PER_1M.get(model_id)
    if not pair:
        return 0.0
    return pair[0] if kind == "input" else pair[1]


def _fallback_unit_prices():
    """Het duurste tarief dat we kennen, per richting.

    Voor een model dat niet in de tabel staat. Te hoog tellen laat een
    budgetgrens te vroeg dichtgaan en dat merkt iemand; te laag tellen
    (nul) merkt niemand tot de rekening van de provider komt.
    """
    return (
        max(pair[0] for pair in _PRICING_USD_PER_1M.values()),
        max(pair[1] for pair in _PRICING_USD_PER_1M.values()),
    )


def _resolve_unit_prices(env, model_id):
    """``(input, output, bekend)`` voor het vastleggen van verbruik.

    ``bekend`` is False als geen van beide richtingen een prijs heeft;
    dan komen de tarieven uit ``_fallback_unit_prices``.
    """
    unit_in = _get_unit_price(env, model_id or "", "input")
    unit_out = _get_unit_price(env, model_id or "", "output")
    if unit_in or unit_out:
        return unit_in, unit_out, True
    fallback_in, fallback_out = _fallback_unit_prices()
    return fallback_in, fallback_out, False


class ClaudeUsage(models.Model):
    _name = "daadit_ai_claude.usage"
    _description = "Anthropic Claude API Usage Record"
    _order = "create_date desc, id desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=False)

    # --- Provenance ----------------------------------------------------
    agent_id = fields.Many2one(
        "ai.agent",
        string="AI Agent",
        ondelete="cascade",
        index=True,
    )
    channel_id = fields.Many2one(
        "discuss.channel",
        string="Channel",
        ondelete="set null",
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        default=lambda self: self.env.user,
        ondelete="set null",
        index=True,
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company,
        index=True,
    )

    # --- Call metadata -------------------------------------------------
    kind = fields.Selection(
        [("chat", "Chat completion")],
        string="Kind",
        required=True,
        default="chat",
        help="Anthropic only offers a messages API — no embeddings.",
    )
    model = fields.Char(string="Model", required=True, index=True)
    iterations = fields.Integer(
        string="Iterations",
        help="Number of round-trips to Anthropic (≥2 means tool calls).",
        default=1,
    )
    has_tools = fields.Boolean(string="Used tools")
    error = fields.Text(string="Error", help="Empty when call succeeded.")

    # --- Tokens --------------------------------------------------------
    prompt_tokens = fields.Integer(string="Input tokens")
    completion_tokens = fields.Integer(string="Output tokens")
    total_tokens = fields.Integer(string="Total tokens",
                                  compute="_compute_total_tokens", store=True)

    # --- Cost ----------------------------------------------------------
    unit_input_usd = fields.Float(
        string="Input price (USD/1M)",
        digits=(12, 6),
    )
    unit_output_usd = fields.Float(
        string="Output price (USD/1M)",
        digits=(12, 6),
    )
    estimated_cost_usd = fields.Float(
        string="Estimated cost (USD)",
        digits=(12, 6),
        compute="_compute_estimated_cost",
        store=True,
    )
    price_fallback = fields.Boolean(
        string="Prijs geschat",
        index=True,
        help="Het model stond niet in de prijstabel; er is gerekend met "
             "het duurste tarief dat we kennen. Zet de echte prijs in "
             "de tabel of als systeemparameter "
             "(daadit_ai_claude.price.<model>.input / .output).",
    )

    # ------------------------------------------------------------------
    @api.depends("prompt_tokens", "completion_tokens")
    def _compute_total_tokens(self):
        for r in self:
            r.total_tokens = (r.prompt_tokens or 0) + (r.completion_tokens or 0)

    @api.depends("prompt_tokens", "completion_tokens",
                 "unit_input_usd", "unit_output_usd")
    def _compute_estimated_cost(self):
        for r in self:
            r.estimated_cost_usd = (
                (r.prompt_tokens or 0) * (r.unit_input_usd or 0) / 1_000_000
                + (r.completion_tokens or 0) * (r.unit_output_usd or 0) / 1_000_000
            )

    @api.depends("model", "create_date", "kind")
    def _compute_display_name(self):
        for r in self:
            r.display_name = f"[{r.kind}] {r.model or '?'} @ {r.create_date or '?'}"

    # ------------------------------------------------------------------
    @api.model
    def record_usage(self, *, kind="chat", model=None, agent_id=None,
                     channel_id=None, prompt_tokens=0, completion_tokens=0,
                     iterations=1, has_tools=False, error=None):
        """Convenience constructor used by the Claude dispatch helpers.

        Best-effort: a logging hiccup must never break the chat flow,
        so we wrap creation in a savepoint and swallow exceptions.
        """
        try:
            unit_in, unit_out, known = _resolve_unit_prices(
                self.env, model or "")
            if not known:
                _logger.warning(
                    "daadit_ai_claude.usage: geen prijs bekend voor model "
                    "%s; gerekend met het duurste tarief (%s / %s USD per "
                    "1M). Vul de prijstabel aan.", model, unit_in, unit_out,
                )
            vals = {
                "kind": kind,
                "model": model or "?",
                "agent_id": agent_id,
                "channel_id": channel_id,
                "prompt_tokens": prompt_tokens or 0,
                "completion_tokens": completion_tokens or 0,
                "iterations": iterations or 1,
                "has_tools": bool(has_tools),
                "error": (error or "")[:8000] if error else False,
                "unit_input_usd": unit_in,
                "unit_output_usd": unit_out,
                "price_fallback": not known,
            }
            with self.env.cr.savepoint():
                self.sudo().create(vals)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_claude.usage: record_usage failed for "
                "model=%s agent=%s — swallowed", model, agent_id,
            )
