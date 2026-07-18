# -*- coding: utf-8 -*-
"""Extends ``ai.embedding`` to register provider-lookup patches on its
merged class — without adding any Claude entry to the embedding-model
selection.

Why this file exists
--------------------
Anthropic doesn't have an embeddings API, so we deliberately do NOT
add a Claude option to ``ai.embedding.embedding_model.selection``.

BUT — stock Enterprise's ``ai.agent`` write/save flow asks an inverse
lookup ("for provider X, which embedding model?") and that lookup
sometimes lives on the ``ai.embedding`` model. If we only run our
registry-patches scan from ``ai.agent._register_hook`` (as previously),
methods on ``ai.embedding`` aren't part of the same MRO and may be
missed.

By adding a no-op ``_inherit`` here with a ``_register_hook``, we get
a second scan pass on the merged ``ai.embedding`` class. The scan
finds any method whose bytecode raises ``UserError("No embedding model
found for the selected provider")`` and wraps it to return
``text-embedding-3-small`` when the call context is Claude-flavoured
(``provider="anthropic"``, ``llm_model="claude-..."``, ...).

End result: ``ai.agent.write({"llm_model": "claude-..."})`` no longer
raises during validation, even when ``restrict_to_sources=True`` and
stock's flow asks ai.embedding to resolve the provider→model mapping.
"""
import logging

from odoo import api, models

from ..services.claude_client import is_claude_model
from ..services import registry_patches

_logger = logging.getLogger(__name__)


# Same constant as ai_agent.ANTHROPIC_FALLBACK_EMBEDDING_MODEL — kept
# local here so this module doesn't take a model-level dependency on
# ai_agent.py.
_FALLBACK_EMBEDDING_MODEL = "text-embedding-3-small"


class AIEmbedding(models.Model):
    _inherit = "ai.embedding"

    # No selection extension — Anthropic has no embedding model.
    # The whole point of this _inherit is the _register_hook below.

    @api.model
    def _register_hook(self):
        res = super()._register_hook()
        try:
            self._daadit_claude_install_provider_patches()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "daadit_ai_claude: provider patching on ai.embedding failed"
            )
        return res

    @api.model
    def _daadit_claude_install_provider_patches(self):
        cls = type(self)

        targets = registry_patches.discover_lookup_methods(cls)
        method_names = [name for name, _base in targets]
        if targets:
            _logger.info(
                "daadit_ai_claude: ai.embedding bytecode scan found "
                "lookup methods: %s",
                [(n, b.__module__) for n, b in targets],
            )
        else:
            _logger.info(
                "daadit_ai_claude: ai.embedding bytecode scan found no "
                "lookup methods (this is fine if the lookup is on "
                "ai.agent instead)."
            )

        # On ai.embedding records the relevant signal is
        # ``embedding_model``, but the call may also pass an
        # ``ai.agent`` recordset whose ``llm_model`` is Claude-flavoured.
        def _is_target(rec):
            try:
                llm = getattr(rec, "llm_model", None)
                if llm and is_claude_model(llm):
                    return True
            except Exception:  # noqa: BLE001
                pass
            return False

        patched_methods = registry_patches.install_method_overrides(
            cls,
            method_names,
            is_target_record=_is_target,
            is_claude_string_predicate=is_claude_model,
            target_return_value=_FALLBACK_EMBEDDING_MODEL,
            log_label="daadit_ai_claude[ai.embedding]",
        )

        dict_specs = registry_patches.discover_provider_dicts(cls)
        patched_dicts = registry_patches.patch_provider_dicts(
            dict_specs,
            log_label="daadit_ai_claude[ai.embedding]",
        )

        _logger.info(
            "daadit_ai_claude: ai.embedding registry patches — "
            "shadowed methods=%s, patched dicts=%s",
            patched_methods, patched_dicts,
        )
