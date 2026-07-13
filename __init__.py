# -*- coding: utf-8 -*-
import logging

_logger = logging.getLogger(__name__)
_logger.info("daadit_ai_claude: __init__ loading (v19.0.1.0.0)")

# Services first so diagnostics installs its UserError tap before any model
# code is imported.
from . import services
from . import models

_logger.info("daadit_ai_claude: __init__ loaded")


# ============================================================================
# Lifecycle hooks (registered from __manifest__.py)
# ============================================================================
#
# Rationale: stock ``ai`` module's data files can be reloaded by
# Odoo.sh's ``--update=all`` build BEFORE our
# ``_inherit`` extensions are merged into ``ai.agent``. If the DB holds
# a Claude value in ``ai_agent.llm_model`` at that point, stock's
# ``_get_provider`` runs unwrapped and raises
# ``UserError("No provider found for the selected model")`` — the
# whole registry init fails.
#
# We can't fix that ordering from inside the module, but we *can* make
# sure the DB doesn't carry a Claude value into a fresh registry build:
#
#   * ``uninstall_hook`` — when the user explicitly uninstalls our
#     module, reset every Claude ``llm_model`` to ``gpt-4o`` so the
#     next stock reload is clean.
#
#   * ``pre_init_hook`` — runs on a FRESH install of our module. If the
#     DB already has stale Claude values from a previous install/
#     uninstall cycle, scrub them before our module's data files load.
#
# Neither hook helps the user who's already wedged — that case requires
# the SQL fix in the README's "Recovery" section to be run by hand.

_RESET_LLM_SQL = """
UPDATE ai_agent
   SET llm_model = 'gpt-4o'
 WHERE llm_model LIKE 'claude-%%'
"""


def _reset_claude_values(env):
    """Run the SQL fix-ups on whichever tables exist."""
    cr = env.cr
    cr.execute("""
        SELECT table_name FROM information_schema.tables
         WHERE table_name = 'ai_agent'
    """)
    tables = {row[0] for row in cr.fetchall()}
    if 'ai_agent' in tables:
        cr.execute(_RESET_LLM_SQL)
        _logger.info(
            "daadit_ai_claude: reset %d ai_agent.llm_model values to gpt-4o",
            cr.rowcount,
        )


def pre_init_hook(env):
    """Called immediately before our module's data files load on FRESH
    install. Scrubs any stray Claude values left in the DB by an earlier
    install/uninstall cycle."""
    _logger.info(
        "daadit_ai_claude.pre_init_hook: scrubbing stale Claude values"
    )
    _reset_claude_values(env)


def uninstall_hook(env):
    """Called when the module is being uninstalled. Reset every Claude
    ``llm_model`` so the DB doesn't carry an invalid-after-uninstall
    value into the next ``--update=all`` build.
    """
    _logger.info(
        "daadit_ai_claude.uninstall_hook: resetting Claude values "
        "across ai_agent"
    )
    _reset_claude_values(env)
