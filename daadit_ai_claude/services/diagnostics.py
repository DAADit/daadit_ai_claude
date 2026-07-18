# -*- coding: utf-8 -*-
"""Process-wide diagnostic instrumentation for the Claude provider.

Installs an opt-in ``UserError.__init__`` trace tap so we can see the
exact stack that produced a ``"No provider found for the selected
model"`` or ``"No embedding model found"`` UserError. Off by default
because it pays a substring scan on every ``UserError`` ever
constructed in the process.

Re-enable the tap per-database via::

    ir.config_parameter:
      daadit_ai_claude.diag_trace_user_errors = True
"""
import logging
import traceback

_logger = logging.getLogger(__name__)

# Markers we care about. Keep in sync with services/registry_patches.py.
_TARGET_MARKERS = (
    "No embedding model found",
    "No provider found for the selected",
)

_PATCH_INSTALLED = False


def _install_user_error_trace_tap():
    """Idempotently wrap ``odoo.exceptions.UserError.__init__``."""
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return
    try:
        from odoo.exceptions import UserError
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.diagnostics: cannot import UserError; "
            "skipping trace tap."
        )
        return

    original_init = UserError.__init__
    if getattr(original_init, "_daadit_claude_diag_wrapped", False):
        _PATCH_INSTALLED = True
        return

    def _wrapped_init(self, *args, **kwargs):
        message_arg = None
        if args:
            message_arg = args[0]
        elif "message" in kwargs:
            message_arg = kwargs["message"]
        try:
            message_str = str(message_arg) if message_arg is not None else ""
        except Exception:  # noqa: BLE001
            message_str = ""

        if any(marker in message_str for marker in _TARGET_MARKERS):
            stack = "".join(traceback.format_stack()[:-1])
            _logger.warning(
                "daadit_ai_claude.diagnostics: UserError raised with target "
                "message %r — caller stack:\n%s",
                message_str, stack,
            )

        return original_init(self, *args, **kwargs)

    _wrapped_init._daadit_claude_diag_wrapped = True
    UserError.__init__ = _wrapped_init
    _PATCH_INSTALLED = True
    _logger.info(
        "daadit_ai_claude.diagnostics: UserError trace tap installed "
        "for markers %r",
        _TARGET_MARKERS,
    )


_logger.info("daadit_ai_claude: diagnostics module loaded")


def maybe_install_trace_tap_from_env(env):
    """Conditionally install the UserError trace tap based on the
    ``daadit_ai_claude.diag_trace_user_errors`` ICP. Called from
    ``ai.agent._register_hook`` so we have an env to read config from.
    """
    try:
        flag = env["ir.config_parameter"].sudo().get_param(
            "daadit_ai_claude.diag_trace_user_errors", default="False"
        )
    except Exception:  # noqa: BLE001
        return False
    enabled = str(flag).strip().lower() in ("1", "true", "t", "yes", "y")
    if enabled:
        _install_user_error_trace_tap()
        return True
    _logger.debug(
        "daadit_ai_claude.diagnostics: UserError trace tap disabled "
        "(set ir.config_parameter daadit_ai_claude.diag_trace_user_errors "
        "= True to enable for debugging)"
    )
    return False
