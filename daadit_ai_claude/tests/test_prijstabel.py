# -*- coding: utf-8 -*-
"""Een model zonder prijs telt niet als gratis (v19.0.4.5.3).

Bo werkte in november 2026 op ``claude-haiku-5-5``; dat model stond niet
in de tabel en 116 regels kwamen op 0,00 dollar uit.
"""
from odoo.tests import common, tagged

from ..models import claude_usage


@tagged("post_install", "-at_install")
class TestPrijstabel(common.TransactionCase):

    def _laatste(self, model):
        return self.env["daadit_ai_claude.usage"].search(
            [("model", "=", model)], order="id desc", limit=1)

    def test_haiku_5_5_heeft_een_prijs(self):
        self.env["daadit_ai_claude.usage"].record_usage(
            model="claude-haiku-5-5",
            prompt_tokens=1_000_000, completion_tokens=1_000_000,
        )
        row = self._laatste("claude-haiku-5-5")
        self.assertAlmostEqual(row.estimated_cost_usd, 0.6, places=4)
        self.assertFalse(row.price_fallback)

    def test_huidige_modellen_staan_in_de_tabel(self):
        for model in ("claude-fable-5-2", "claude-opus-5-5",
                      "claude-sonnet-5-5", "claude-haiku-5-5"):
            self.assertIn(model, claude_usage._PRICING_USD_PER_1M)
        self.assertEqual(
            claude_usage._PRICING_USD_PER_1M["claude-opus-4-7"], (5.0, 25.0))

    def test_onbekend_model_is_niet_gratis(self):
        self.env["daadit_ai_claude.usage"].record_usage(
            model="claude-bestaat-niet-9", prompt_tokens=1_000_000,
            completion_tokens=0,
        )
        row = self._laatste("claude-bestaat-niet-9")
        duurste_in, _uit = claude_usage._fallback_unit_prices()
        self.assertTrue(row.price_fallback)
        self.assertAlmostEqual(row.estimated_cost_usd, duurste_in, places=4)
        self.assertGreater(row.estimated_cost_usd, 0.0)

    def test_overschrijving_via_parameter_wint(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "daadit_ai_claude.price.claude-bestaat-niet-9.input", "2.5")
        self.env["daadit_ai_claude.usage"].record_usage(
            model="claude-bestaat-niet-9", prompt_tokens=1_000_000,
            completion_tokens=0,
        )
        row = self._laatste("claude-bestaat-niet-9")
        self.assertFalse(row.price_fallback)
        self.assertAlmostEqual(row.estimated_cost_usd, 2.5, places=4)
