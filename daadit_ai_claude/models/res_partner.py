# -*- coding: utf-8 -*-
"""GDPR Art. 17 (right to erasure) helper for Claude usage history.

Adds a partner-form button that anonymises every
``daadit_ai_claude.usage`` row linked to this partner's internal
users. Aggregate cost figures (tokens + USD estimate) are kept; only
the user identifier is removed.

De knop op de relatiekaart is op 6 oktober 2026 weggehaald: elke module
maakte daarvoor zijn eigen ``<header>`` boven de sheet (de standaard
``res.partner``-form heeft er geen), wat vier gekleurde balken op elke
contactkaart opleverde. De wissing loopt nu via ``daadit.gdpr.erase``
in ``daadit_mcp_multi_tenant`` — tandwielmenu op de relatiekaart, alle
sporen in één venster met de aantallen erbij. Deze methode blijft de
uitvoering doen en wordt daar op naam aangeroepen; hernoem hem niet.

Let op: de rechtencontrole zat in het ``groups``-attribuut van die
knop en is met de knop verdwenen. Ze staat nu in de wizard; roep deze
methode dus niet aan zonder die controle.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    def action_daadit_claude_gdpr_erase(self):
        """Anonymise ``daadit_ai_claude.usage`` rows tied to this
        partner's users. Keeps the cost figures intact (token counts and
        USD estimates) but removes the user identifier.
        """
        self.ensure_one()
        if "daadit_ai_claude.usage" not in self.env:
            raise UserError(_(
                "The `daadit_ai_claude.usage` model is not registered "
                "— is the module properly installed?"
            ))
        Usage = self.env["daadit_ai_claude.usage"].sudo()

        users = self.env["res.users"].sudo().search(
            [("partner_id", "=", self.id)]
        )
        if not users:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Claude usage — GDPR erasure"),
                    "message": _(
                        "No internal user is linked to %s — nothing to "
                        "anonymise on the usage history."
                    ) % self.display_name,
                    "type": "warning",
                },
            }
        rows = Usage.search([("user_id", "in", users.ids)])
        if not rows:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Claude usage — GDPR erasure"),
                    "message": _(
                        "No usage rows reference %(name)s "
                        "(%(users)s linked user(s)) — nothing to do."
                    ) % {"name": self.display_name, "users": len(users)},
                    "type": "warning",
                },
            }
        rows.write({"user_id": False})
        when = fields.Datetime.now().strftime("%Y-%m-%d")
        _logger.info(
            "daadit_ai_claude.gdpr_erase: partner=%s users=%s "
            "usage_rows=%s wiped on %s",
            self.id, len(users), len(rows), when,
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Claude usage — GDPR erasure"),
                "message": _(
                    "Anonymised %(rows)s Claude usage row(s) for "
                    "%(name)s. Token counts and cost figures kept "
                    "(no PII), user link removed."
                ) % {"rows": len(rows), "name": self.display_name},
                "type": "success",
                "sticky": True,
            },
        }
