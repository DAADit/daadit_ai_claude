# -*- coding: utf-8 -*-
"""Daily spend circuit-breaker for the Claude provider.

Mirror of ``daadit_ai_mistral.services.cost_cap`` so both providers are
governed the same way: every Claude chat call is priced into
``daadit_ai_claude.usage`` (stored ``estimated_cost_usd`` per call), and
once today's spend reaches the cap further calls are refused with a
clear, translated message while an admin is notified once per day.

Config (all ``ir.config_parameter``, tunable without a code deploy):

    daadit_ai_claude.daily_cost_cap_usd     float; 0 = disabled. The
                                            Claude-only cap, in USD.
    daadit_ai.daily_cost_cap_usd            float; 0 = disabled. Shared
                                            ceiling over Claude AND
                                            Mistral spend combined, for
                                            operators who budget per
                                            tenant rather than per
                                            provider. Whichever cap is
                                            reached first blocks.
    daadit_ai_claude.cost_cap_notify_email  recipient for the "budget
                                            reached" mail. Empty →
                                            falls back to the admin
                                            user / company email.

Design notes
------------
* **Soft ceiling, by design.** The check sums ALREADY-RECORDED usage
  before a call runs; the call that crosses the line still completes
  (its cost is booked afterwards), and every call after it is blocked.
* **Single choke point.** The check runs at the top of
  ``_request_llm_claude``, which every Claude chat call funnels through.
* **Never breaks the chat flow.** Any error in the cap logic (DB read,
  mail send) is swallowed; a broken breaker must not take down the AI.
"""
import logging
from datetime import datetime, time

_logger = logging.getLogger(__name__)

_CAP_ICP = "daadit_ai_claude.daily_cost_cap_usd"
_SHARED_CAP_ICP = "daadit_ai.daily_cost_cap_usd"
_NOTIFY_EMAIL_ICP = "daadit_ai_claude.cost_cap_notify_email"
_NOTIFIED_ON_ICP = "daadit_ai_claude.cost_cap_notified_on"

_MISTRAL_USAGE_MODEL = "daadit_ai_mistral.usage"


def _today_start_utc(env):
    """Naive-UTC datetime for local midnight (start of 'today').

    Odoo stores ``create_date`` as naive UTC. We anchor 'today' to the
    user/company timezone so the budget resets at local midnight, then
    convert back to naive UTC for the domain comparison.
    """
    tzname = env.context.get("tz") or (
        env.user.tz if env.user else None
    ) or "Europe/Amsterdam"
    try:
        import pytz
        tz = pytz.timezone(tzname)
        now_local = datetime.now(tz)
        start_local = tz.localize(
            datetime.combine(now_local.date(), time.min)
        )
        return start_local.astimezone(pytz.utc).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        now = datetime.utcnow()
        return datetime.combine(now.date(), time.min)


def _float_param(env, key):
    """Read ``key`` as a positive float, or 0.0 when unset/unparseable."""
    try:
        raw = env["ir.config_parameter"].sudo().get_param(key, "0")
        value = float(str(raw).strip().replace(",", "."))
        return value if value > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def get_cap(env):
    """Claude-only daily cap, 0.0 when unset/disabled."""
    return _float_param(env, _CAP_ICP)


def get_shared_cap(env):
    """Cross-provider daily cap, 0.0 when unset/disabled."""
    return _float_param(env, _SHARED_CAP_ICP)


def _spend_since(env, model_name, start):
    """Sum ``estimated_cost_usd`` on ``model_name`` since ``start``.

    Returns 0.0 when the model is not installed or the read fails —
    fail-open so a broken breaker never blocks chat.
    """
    if model_name not in env:
        return 0.0
    try:
        groups = env[model_name].sudo().read_group(
            [("create_date", ">=", start.strftime("%Y-%m-%d %H:%M:%S"))],
            ["estimated_cost_usd:sum"],
            [],
        )
        if groups:
            return float(groups[0].get("estimated_cost_usd") or 0.0)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.cost_cap: failed to sum daily spend on "
            "%s; treating as 0", model_name,
        )
    return 0.0


def daily_spend(env):
    """Claude spend booked since local midnight."""
    return _spend_since(env, "daadit_ai_claude.usage", _today_start_utc(env))


def daily_spend_all_providers(env):
    """Claude + Mistral spend booked since local midnight."""
    start = _today_start_utc(env)
    return (
        _spend_since(env, "daadit_ai_claude.usage", start)
        + _spend_since(env, _MISTRAL_USAGE_MODEL, start)
    )


def check(env):
    """Return ``(blocked, spent, cap)``.

    Evaluates the Claude-only cap and the shared cross-provider cap;
    the first one reached blocks and reports its own spend/cap pair, so
    the user-facing message always matches the budget that tripped.
    Fail-open: any internal error yields ``blocked=False``.
    """
    try:
        cap = get_cap(env)
        shared_cap = get_shared_cap(env)
        if not cap and not shared_cap:
            return (False, 0.0, 0.0)

        spent = daily_spend(env) if cap else 0.0
        if cap and spent >= cap:
            _notify_once(env, spent, cap, shared=False)
            return (True, spent, cap)

        if shared_cap:
            shared_spent = daily_spend_all_providers(env)
            if shared_spent >= shared_cap:
                _notify_once(env, shared_spent, shared_cap, shared=True)
                return (True, shared_spent, shared_cap)
            if not cap:
                return (False, shared_spent, shared_cap)

        return (False, spent, cap)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.cost_cap: check() raised; failing open"
        )
        return (False, 0.0, 0.0)


def _notify_once(env, spent, cap, shared=False):
    """Email the configured admin that the daily budget is reached —
    once per day, guarded by a date marker in ir.config_parameter."""
    try:
        icp = env["ir.config_parameter"].sudo()
        today = _today_start_utc(env).strftime("%Y-%m-%d")
        if icp.get_param(_NOTIFIED_ON_ICP) == today:
            return  # already told them today

        recipient = (icp.get_param(_NOTIFY_EMAIL_ICP) or "").strip()
        if not recipient:
            admin = env.ref("base.user_admin", raise_if_not_found=False)
            recipient = (
                (admin and admin.partner_id.email)
                or (env.company.email or "")
            ).strip()
        if not recipient:
            _logger.warning(
                "daadit_ai_claude.cost_cap: daily budget reached "
                "(%.2f/%.2f) but no notify recipient configured "
                "(set %s).", spent, cap, _NOTIFY_EMAIL_ICP,
            )
            icp.set_param(_NOTIFIED_ON_ICP, today)
            return

        scope = (
            "alle AI-providers samen" if shared else "Claude"
        )
        param = _SHARED_CAP_ICP if shared else _CAP_ICP
        body = (
            "<p>Het AI-dagbudget voor %s is bereikt.</p>"
            "<ul>"
            "<li><strong>Besteed vandaag:</strong> $%.2f</li>"
            "<li><strong>Dagbudget:</strong> $%.2f</li>"
            "</ul>"
            "<p>De Claude-agents (Ask AI, de specialist-agents en "
            "geplande agents) zijn voor de rest van vandaag automatisch "
            "gepauzeerd. Het budget reset vannacht om middernacht.</p>"
            "<p>Wil je het budget verhogen? Pas de systeemparameter "
            "<code>%s</code> aan (Instellingen &rarr; Technisch &rarr; "
            "Systeemparameters).</p>"
        ) % (scope, spent, cap, param)

        mail = env["mail.mail"].sudo().create({
            "subject": "DAADit AI: dagbudget bereikt — Claude gepauzeerd",
            "body_html": body,
            "email_to": recipient,
            "auto_delete": True,
        })
        mail.send()
        icp.set_param(_NOTIFIED_ON_ICP, today)
        _logger.warning(
            "daadit_ai_claude.cost_cap: daily budget reached "
            "(%.2f/%.2f, shared=%s) — notified %s and paused Claude "
            "for today.", spent, cap, shared, recipient,
        )
    except Exception:  # noqa: BLE001
        _logger.exception(
            "daadit_ai_claude.cost_cap: notification failed; the cap "
            "still blocks, only the e-mail didn't send"
        )


def blocked_message_en(spent, cap):
    """English source for the user-facing 'budget reached' message.
    Translated into the chat language by the caller."""
    return (
        "_(The AI assistant has reached today's usage budget "
        "($%.2f of $%.2f) and is paused until tomorrow. Contact an "
        "administrator to raise the daily budget.)_" % (spent, cap)
    )
