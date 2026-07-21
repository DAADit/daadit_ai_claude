# -*- coding: utf-8 -*-
"""Extends ``ai.agent`` with Claude models and routes calls to Anthropic.

What this module does:

* Extends the ``llm_model`` selection with Claude entries.
* Installs a provider override that maps Claude models to the
  ``'anthropic'`` provider.
* Hooks the LLMApiService patch at register-time so chat dispatch
  reaches the Anthropic Messages API.

Key Anthropic-specific notes:

* The provider name we emit is ``'anthropic'`` (matches the wider
  industry convention and is what our ``LLMApiService`` patch expects).
* Anthropic does not provide an embeddings API. To keep stock's
  agent-save / Sources / RAG flow working, the embedding-lookup
  overrides on this class return ``'text-embedding-3-small'`` (stock
  OpenAI default) when the provider is ``'anthropic'``. Outcome:
  CHAT runs on Anthropic (Claude Messages API), EMBEDDINGS run on
  whichever provider stock routes ``text-embedding-3-small`` through
  (OpenAI direct key if configured, otherwise Odoo IAP).
"""
import logging

from odoo import api, fields, models

from ..services.claude_client import is_claude_model
from ..services import registry_patches
from ..services import llm_api_patch
from ..services import tool_dispatch
from ..services import diagnostics

_logger = logging.getLogger(__name__)


# Fallback embedding model — used whenever stock asks "what embedding
# model goes with provider='anthropic'?". Anthropic has no embeddings
# API so we deflect to OpenAI's default, which is always present in
# stock's ``ai.embedding.embedding_model`` selection and which stock
# already knows how to route (via the user's OpenAI key if set,
# otherwise via Odoo IAP).
ANTHROPIC_FALLBACK_EMBEDDING_MODEL = "text-embedding-3-small"


# Claude model labels for the selection field. Order = display order.
CLAUDE_MODEL_SELECTION = [
    ("claude-opus-4-7", "Claude Opus 4.7"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
    # Older snapshots — keep so existing records keep working after
    # we move on. Hidden behind the "latest" labels above.
    ("claude-opus-4-5", "Claude Opus 4.5"),
    ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
    ("claude-haiku-4-5", "Claude Haiku 4.5 (rolling)"),
]


class AIAgent(models.Model):
    _inherit = "ai.agent"

    # ------------------------------------------------------------------ #
    # Selection extension                                                #
    # We re-declare the field with ``selection="<name>"`` as a string so #
    # Odoo resolves the method by name via the merged registry class on #
    # every call — a plain method override on a by-reference callable   #
    # selection wouldn't be picked up.                                   #
    # ------------------------------------------------------------------ #
    llm_model = fields.Selection(
        selection="_get_llm_model_selection",
        ondelete={key: "set gpt-4o" for key, _label in CLAUDE_MODEL_SELECTION},
    )

    # ------------------------------------------------------------------ #
    # Per-agent Claude overrides                                         #
    # ------------------------------------------------------------------ #
    daadit_claude_temperature_active = fields.Boolean(
        string="Apply temperature override",
        default=False,
        help=(
            "When unticked, the agent's response_style controls the "
            "temperature (analytical=0.2, balanced=0.6, creative=0.9). "
            "Tick this to force the value below — including 0.0 for "
            "fully deterministic responses."
        ),
    )
    daadit_claude_temperature = fields.Float(
        string="Claude temperature override",
        digits=(3, 2),
        default=0.0,
        help=(
            "Override the temperature sent to Anthropic for THIS agent. "
            "Effective only when 'Apply temperature override' is ticked. "
            "Range 0.0 – 1.0."
        ),
    )
    daadit_claude_max_tokens = fields.Integer(
        string="Claude max tokens override",
        default=0,
        help=(
            "Cap the completion length for THIS agent. 0 = use the "
            "Anthropic API default (4096). Useful for cost control on "
            "chatty agents."
        ),
    )

    # ------------------------------------------------------------------ #
    # Per-agent model access control                                     #
    # ------------------------------------------------------------------ #
    daadit_allowed_model_ids = fields.Many2many(
        "ir.model",
        relation="daadit_ai_claude_agent_allowed_model_rel",
        column1="agent_id",
        column2="model_id",
        string="Allowed models",
        help=(
            "If set, this agent can ONLY query the listed models. "
            "Empty = unrestricted (the agent can query any model the "
            "calling user has access to)."
        ),
    )
    daadit_blocked_model_ids = fields.Many2many(
        "ir.model",
        relation="daadit_ai_claude_agent_blocked_model_rel",
        column1="agent_id",
        column2="model_id",
        string="Blocked models",
        help=(
            "Models the agent must NEVER query, regardless of the "
            "allowed list. Blocked models always win over allowed models."
        ),
    )
    daadit_field_blocklist = fields.Char(
        string="Forbidden fields (PII)",
        help=(
            "Comma-separated list of `model.field` combinations that "
            "must never leave this agent (data-minimisation gate of "
            "last resort). Matching keys are scrubbed from tool "
            "responses; filter conditions referencing a blocked field "
            "are rejected. Example: "
            "'res.partner.vat,hr.employee.identification_id,"
            "res.partner.bank_ids'."
        ),
    )

    @api.constrains("daadit_field_blocklist")
    def _check_daadit_field_blocklist(self):
        import re
        from odoo.exceptions import ValidationError
        entry_re = re.compile(
            r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)+\.[a-z_][a-z0-9_]*$"
        )
        for rec in self:
            raw = rec.daadit_field_blocklist or ""
            if not raw.strip():
                continue
            for entry in raw.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                if not entry_re.match(entry):
                    raise ValidationError(
                        "Field blocklist entry %r is not in the "
                        "required 'model.field' shape (e.g. "
                        "'res.partner.vat')." % entry
                    )
                model_name, _, field_name = entry.rpartition(".")
                model = self.env["ir.model"].sudo().search(
                    [("model", "=", model_name)], limit=1,
                )
                if not model:
                    _logger.warning(
                        "daadit_ai_claude: blocklist entry %r references "
                        "model '%s' which is not installed.",
                        entry, model_name,
                    )
                    continue
                field = self.env["ir.model.fields"].sudo().search([
                    ("model", "=", model_name),
                    ("name", "=", field_name),
                ], limit=1)
                if not field:
                    _logger.warning(
                        "daadit_ai_claude: blocklist entry %r references "
                        "field '%s' which does not exist on model '%s'.",
                        entry, field_name, model_name,
                    )

    def _daadit_is_model_allowed(self, model_name):
        self.ensure_one()
        if not model_name:
            return False
        if self.daadit_blocked_model_ids:
            blocked = set(self.daadit_blocked_model_ids.mapped("model"))
            if model_name in blocked:
                return False
        if not self.daadit_allowed_model_ids:
            return True
        allowed = set(self.daadit_allowed_model_ids.mapped("model"))
        return model_name in allowed

    def _daadit_blocked_field_set(self, model_name):
        self.ensure_one()
        if not self.daadit_field_blocklist or not model_name:
            return set()
        prefix = model_name + "."
        out = set()
        for entry in self.daadit_field_blocklist.split(","):
            entry = entry.strip()
            if entry.startswith(prefix):
                out.add(entry[len(prefix):])
        return out

    def _daadit_scrub_record(self, model_name, record):
        self.ensure_one()
        if not isinstance(record, dict):
            return record
        for f in self._daadit_blocked_field_set(model_name):
            if f in record:
                record[f] = None
        return record

    def _daadit_scrub_result(self, model_name, result):
        self.ensure_one()
        if not self._daadit_blocked_field_set(model_name):
            return result
        if isinstance(result, list):
            return [self._daadit_scrub_record(model_name, r) for r in result]
        if isinstance(result, dict):
            if isinstance(result.get("records"), list):
                result["records"] = [
                    self._daadit_scrub_record(model_name, r)
                    for r in result["records"]
                ]
                return result
            return self._daadit_scrub_record(model_name, result)
        return result

    def _daadit_domain_uses_blocked_field(self, model_name, domain):
        self.ensure_one()
        blocked = self._daadit_blocked_field_set(model_name)
        if not blocked or not domain:
            return ""
        if isinstance(domain, str):
            try:
                import json as _json
                domain = _json.loads(domain)
            except Exception:  # noqa: BLE001
                return ""
        for clause in domain or []:
            if isinstance(clause, (list, tuple)) and len(clause) == 3:
                field = clause[0] or ""
                if field in blocked:
                    return field
        return ""

    # ------------------------------------------------------------------ #
    # Presets — one-click sane starting points                           #
    # ------------------------------------------------------------------ #
    _DAADIT_SUGGESTED_ALLOWED = [
        # CRM + Contacts
        "res.partner", "res.partner.category",
        "crm.lead", "crm.team", "crm.stage", "crm.tag",
        "calendar.event",
        # Sales
        "sale.order", "sale.order.line", "sale.report",
        # Purchases
        "purchase.order", "purchase.order.line",
        # Products + Inventory
        "product.template", "product.product", "product.category",
        "stock.picking", "stock.move", "stock.move.line",
        "stock.quant", "stock.location", "stock.warehouse",
        # Accounting (read-only-friendly)
        "account.move", "account.move.line", "account.account",
        "account.journal", "account.payment",
        # HR
        "hr.employee", "hr.department", "hr.job",
        "hr.leave", "hr.leave.type", "hr.attendance",
        # Projects / tasks
        "project.project", "project.task", "project.tags",
        # Manufacturing
        "mrp.production", "mrp.bom", "mrp.workorder",
        # Helpdesk (Enterprise — only if installed)
        "helpdesk.ticket", "helpdesk.team", "helpdesk.stage",
        # Subscription / Field Service / Recruitment (Enterprise)
        "sale.subscription",
        "industry.fsm.task",
        "hr.applicant", "hr.recruitment.stage",
        # Generic
        "res.company", "res.country", "res.currency",
    ]

    _DAADIT_SUGGESTED_BLOCKED = [
        # Identity / credentials
        "res.users", "res.users.log", "res.users.apikeys",
        "res.users.identitycheck",
        "auth.session.expired",
        # Configuration / system parameters
        "ir.config_parameter", "ir.cron", "ir.cron.trigger",
        # Attachments / private messages
        "ir.attachment", "mail.message", "mail.tracking.value",
        "mail.notification",
        # Logging / audit
        "ir.logging",
        # Model + permission metadata
        "ir.model", "ir.model.access", "ir.model.fields",
        "ir.model.data", "ir.model.constraint",
        "ir.rule", "ir.actions.server",
        # Claude-internal: never expose own usage rows via AI
        "daadit_ai_claude.usage",
    ]

    def action_daadit_apply_suggested_allowed_models(self):
        self.ensure_one()
        Model = self.env["ir.model"].sudo()
        existing = Model.search([("model", "in", self._DAADIT_SUGGESTED_ALLOWED)])
        self.daadit_allowed_model_ids = [(6, 0, existing.ids)]
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": "Allowed models applied",
                "message": (
                    f"{len(existing)} of {len(self._DAADIT_SUGGESTED_ALLOWED)} "
                    f"suggested business models exist in this database "
                    f"and are now on the allowed list. Adjust as needed."
                ),
                "sticky": False,
            },
        }

    def action_daadit_apply_default_blocked_models(self):
        self.ensure_one()
        Model = self.env["ir.model"].sudo()
        existing = Model.search([("model", "in", self._DAADIT_SUGGESTED_BLOCKED)])
        self.daadit_blocked_model_ids = [(6, 0, existing.ids)]
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": "Block list applied",
                "message": (
                    f"{len(existing)} of {len(self._DAADIT_SUGGESTED_BLOCKED)} "
                    f"sensitive system models exist in this database "
                    f"and are now on the block list."
                ),
                "sticky": False,
            },
        }

    def action_daadit_clear_allowed_models(self):
        self.ensure_one()
        self.daadit_allowed_model_ids = [(5, 0, 0)]
        return True

    def action_daadit_clear_blocked_models(self):
        self.ensure_one()
        self.daadit_blocked_model_ids = [(5, 0, 0)]
        return True

    # ------------------------------------------------------------------ #
    # Selection callable                                                 #
    # ------------------------------------------------------------------ #
    @api.model
    def _get_llm_model_selection(self):
        parent = getattr(super(), "_get_llm_model_selection", None)
        base = []
        if callable(parent):
            try:
                base = list(parent() or [])
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "daadit_ai_claude: super()._get_llm_model_selection raised; "
                    "continuing with empty base list"
                )
        else:
            for cand in ("_compute_llm_models", "_get_models",
                         "_selection_llm_model", "_default_llm_models"):
                fn = getattr(super(), cand, None)
                if callable(fn):
                    try:
                        base = list(fn() or [])
                        break
                    except Exception:  # noqa: BLE001
                        continue
            if not base:
                _logger.warning(
                    "daadit_ai_claude: could not find a parent llm_model "
                    "selection method; OpenAI/Gemini entries may be missing."
                )
        existing_keys = {value for value, _label in base}
        # Dynamic source: the live-synced registry (daadit.ai.claude.model),
        # refreshed daily from the Anthropic /v1/models API and via the
        # manual "Refresh models" button. Newly-released Claude models
        # appear here automatically — no redeploy needed. Fall back to the
        # built-in seed constant when the table is empty or unreachable
        # (fresh install/upgrade before seed data loads, or a DB error).
        entries = []
        try:
            entries = self.env["daadit.ai.claude.model"].sudo()._selection_entries()
        except Exception:  # noqa: BLE001
            _logger.debug(
                "daadit_ai_claude: model registry unavailable; using seed",
                exc_info=True,
            )
        if not entries:
            entries = list(CLAUDE_MODEL_SELECTION)
        added = 0
        for value, label in entries:
            if value not in existing_keys:
                base.append((value, label))
                existing_keys.add(value)
                added += 1
        _logger.debug(
            "daadit_ai_claude: _get_llm_model_selection extended with %d "
            "Claude entries (base size=%d, total=%d)",
            added,
            len(base) - added,
            len(base),
        )
        return base

    # ------------------------------------------------------------------ #
    # Registry hook                                                      #
    # ------------------------------------------------------------------ #
    @api.model
    def _register_hook(self):
        res = super()._register_hook()
        try:
            self._daadit_install_provider_patches()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_claude: provider patching on ai.agent failed"
            )
        try:
            diagnostics.maybe_install_trace_tap_from_env(self.env)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_claude: trace-tap toggle raised; continuing"
            )
        return res

    @api.model
    def _daadit_install_provider_patches(self):
        cls = type(self)

        targets = registry_patches.discover_lookup_methods(cls)
        method_names = [name for name, _base in targets]
        if targets:
            _logger.info(
                "daadit_ai_claude: ai.agent bytecode scan found "
                "lookup methods: %s",
                [(n, b.__module__) for n, b in targets],
            )

        patched_methods = registry_patches.install_method_overrides(
            cls,
            method_names,
            is_target_record=lambda rec: bool(
                is_claude_model(getattr(rec, "llm_model", None) or "")
            ),
            # Also short-circuit when args/kwargs carry a Claude
            # indicator (provider="anthropic" / claude-* model name).
            # This catches the call site where stock's write/validate
            # flow has already resolved the provider but ``self`` may
            # still hold a stale llm_model.
            is_claude_string_predicate=is_claude_model,
            # Anthropic has no embedding model. We return stock's
            # default OpenAI embedding model — always present in
            # ``ai.embedding.embedding_model``. Embedding generation
            # then routes through OpenAI / IAP while chat continues
            # via Anthropic.
            target_return_value=ANTHROPIC_FALLBACK_EMBEDDING_MODEL,
            log_label="daadit_ai_claude[ai.agent]",
        )

        dict_specs = registry_patches.discover_provider_dicts(cls)
        patched_dicts = registry_patches.patch_provider_dicts(
            dict_specs,
            log_label="daadit_ai_claude[ai.agent]",
        )

        # Global module scan — covers provider-lookup callables that
        # live in service/helper modules outside the registered model
        # class hierarchy.
        global_targets = registry_patches.discover_in_loaded_modules(
            module_prefix="odoo.addons.ai",
        )
        for extra_prefix in ("odoo.addons.ai_app", "odoo.addons.ai_crm",
                             "odoo.addons.ai_documents"):
            for spec in registry_patches.discover_in_loaded_modules(
                module_prefix=extra_prefix,
            ):
                if spec not in global_targets:
                    global_targets.append(spec)

        patched_globals = registry_patches.install_module_overrides(
            global_targets,
            target_return_value=ANTHROPIC_FALLBACK_EMBEDDING_MODEL,
            is_claude_string_predicate=is_claude_model,
            log_label="daadit_ai_claude[global]",
        )

        _logger.info(
            "daadit_ai_claude: registry patches — "
            "shadowed methods=%s, patched dicts=%s, global wraps=%s",
            patched_methods, patched_dicts, patched_globals,
        )

        # LLMApiService patch
        try:
            llm_api_patch.patch_llm_api_service()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_claude: LLMApiService patching failed"
            )

    # ------------------------------------------------------------------ #
    # Provider lookup overrides                                          #
    # ------------------------------------------------------------------ #

    def _get_provider_for_model(self, *args, **kwargs):
        target = kwargs.get("model") or (args[0] if args else self.llm_model)
        if is_claude_model(target):
            return "anthropic"
        try:
            return super()._get_provider_for_model(*args, **kwargs)
        except AttributeError:
            raise

    def _get_llm_provider(self, *args, **kwargs):
        if is_claude_model(self.llm_model):
            return "anthropic"
        try:
            return super()._get_llm_provider(*args, **kwargs)
        except AttributeError:
            raise

    def _get_provider(self, *args, **kwargs):
        if is_claude_model(self.llm_model):
            # Just-in-time self-heal.
            try:
                llm_api_patch.patch_llm_api_service()
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "daadit_ai_claude: just-in-time LLMApiService patch raised"
                )
            try:
                tool_dispatch.current_agent.record = self
            except Exception:  # noqa: BLE001
                pass
            return "anthropic"
        try:
            return super()._get_provider(*args, **kwargs)
        except AttributeError:
            raise

    # ------------------------------------------------------------------ #
    # Provider → embedding-model reverse lookup                          #
    #                                                                    #
    # After our provider-lookup returns ``'anthropic'``, stock asks the  #
    # inverse: "for provider anthropic, which embedding model should I  #
    # use?" If no mapping exists it raises:                              #
    #     UserError("No embedding model found for the selected provider")#
    # Anthropic has no embeddings API, so we deflect to OpenAI's stock  #
    # default (``text-embedding-3-small``). Chat keeps running on       #
    # Claude; embeddings run on whichever embedding provider stock      #
    # already supports.                                                  #
    #                                                                    #
    # Four candidate method names are overridden — stock Enterprise is  #
    # closed-source and the real method name has varied across          #
    # versions; this set covers every name we've observed plus a few    #
    # plausible variants. Whichever one exists in MRO gets called; the  #
    # others are inert.                                                  #
    # ------------------------------------------------------------------ #

    # ===== Actual stock method name (verified via introspect on        =====
    # ===== staging Enterprise 19): ``_get_embedding_model(self)``.     =====
    # ===== This is THE one that raises the "No embedding model found  =====
    # ===== for the selected provider" UserError. Standard ``_inherit`` =====
    # ===== + super() override is the most reliable intercept — no     =====
    # ===== bytecode monkey-patching needed.                           =====
    def _get_embedding_model(self, *args, **kwargs):
        # When this agent uses a Claude model, deflect to OpenAI's
        # default embedding model. Claude has no embeddings API; chat
        # stays on Anthropic while embeddings route through whichever
        # provider stock has configured for text-embedding-3-small.
        if is_claude_model(self.llm_model):
            return ANTHROPIC_FALLBACK_EMBEDDING_MODEL
        try:
            return super()._get_embedding_model(*args, **kwargs)
        except AttributeError:
            raise

    # Legacy candidate names — kept in case a future Enterprise version
    # renames the lookup again. They're inert when the active method
    # name is ``_get_embedding_model`` (above).
    def _get_embedding_model_for_provider(self, *args, **kwargs):
        provider = kwargs.get("provider") or (args[0] if args else None)
        if provider == "anthropic":
            return ANTHROPIC_FALLBACK_EMBEDDING_MODEL
        try:
            return super()._get_embedding_model_for_provider(*args, **kwargs)
        except AttributeError:
            raise

    def _get_default_embedding_model(self, *args, **kwargs):
        provider = kwargs.get("provider") or (args[0] if args else None)
        if provider == "anthropic" or is_claude_model(self.llm_model):
            return ANTHROPIC_FALLBACK_EMBEDDING_MODEL
        try:
            return super()._get_default_embedding_model(*args, **kwargs)
        except AttributeError:
            raise

    def _get_provider_embedding_model(self, *args, **kwargs):
        provider = kwargs.get("provider") or (args[0] if args else None)
        if provider == "anthropic":
            return ANTHROPIC_FALLBACK_EMBEDDING_MODEL
        try:
            return super()._get_provider_embedding_model(*args, **kwargs)
        except AttributeError:
            raise

    def _embedding_model_for_provider(self, *args, **kwargs):
        provider = kwargs.get("provider") or (args[0] if args else None)
        if provider == "anthropic":
            return ANTHROPIC_FALLBACK_EMBEDDING_MODEL
        try:
            return super()._embedding_model_for_provider(*args, **kwargs)
        except AttributeError:
            raise

    # ------------------------------------------------------------------ #
    # Test button intercept                                              #
    # ------------------------------------------------------------------ #
    def open_agent_chat(self, *args, **kwargs):
        if is_claude_model(self.llm_model):
            _logger.info(
                "daadit_ai_claude: open_agent_chat called for Claude "
                "agent '%s' (model=%s)",
                self.name, self.llm_model,
            )
        try:
            return super().open_agent_chat(*args, **kwargs)
        except AttributeError:
            _logger.warning(
                "daadit_ai_claude: ai.agent has no open_agent_chat — "
                "this should not happen, the form view declares the button."
            )
            raise
