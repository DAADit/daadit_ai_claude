# -*- coding: utf-8 -*-
"""Defensive registry patching for closed-source Enterprise ``ai`` lookups.

Summary: we don't know the exact method name on stock ``ai.agent``
that raises ``UserError("No provider found for the selected model")``
so at registry hook time we scan the merged class's MRO + loaded
modules for the error string in bytecode and patch every match. We
also patch class-level provider→model dicts (if any).

Both are best-effort; if neither finds a target the install still
works, the original UserError will continue firing for non-Claude
agents, and the fallback ``_get_provider`` overrides in ``ai_agent.py``
remain in place.
"""
import logging
from typing import Callable, Iterable, List, Optional, Tuple

_logger = logging.getLogger(__name__)


ERROR_MARKERS: Tuple[str, ...] = (
    "No embedding model found",
    "No provider found for the selected",
)

# Provider-name keys we expect to see in a stock provider→model dict.
_PROVIDER_KEYS = {"openai", "google", "openai_provider", "google_provider"}


def _scan_code_for_markers(code, markers: Iterable[str]) -> bool:
    """Recursively scan a code object's constants for any of ``markers``."""
    try:
        consts = code.co_consts
    except AttributeError:
        return False
    for const in consts:
        if isinstance(const, str):
            for m in markers:
                if m in const:
                    return True
        elif hasattr(const, "co_consts"):
            if _scan_code_for_markers(const, markers):
                return True
    return False


def discover_lookup_methods(cls, *, our_module_marker: str = "daadit_ai_claude") -> List[Tuple[str, type]]:
    found: List[Tuple[str, type]] = []
    seen_names = set()
    for base in cls.__mro__:
        try:
            base_module = getattr(base, "__module__", "") or ""
        except Exception:  # noqa: BLE001
            continue
        if our_module_marker in base_module:
            continue
        try:
            attrs_iter = list(vars(base).items())
        except Exception:  # noqa: BLE001
            continue
        for attr_name, attr_value in attrs_iter:
            if attr_name.startswith("__") or attr_name in seen_names:
                continue
            try:
                if not callable(attr_value):
                    continue
                code = getattr(attr_value, "__code__", None)
            except Exception:  # noqa: BLE001
                continue
            if code is None:
                continue
            if _scan_code_for_markers(code, ERROR_MARKERS):
                seen_names.add(attr_name)
                found.append((attr_name, base))
    return found


def discover_provider_dicts(cls, *, our_module_marker: str = "daadit_ai_claude") -> List[Tuple[type, str, dict]]:
    found: List[Tuple[type, str, dict]] = []
    for base in cls.__mro__:
        base_module = getattr(base, "__module__", "") or ""
        if our_module_marker in base_module:
            continue
        for attr_name, attr_value in vars(base).items():
            if attr_name.startswith("__"):
                continue
            if not isinstance(attr_value, dict) or not attr_value:
                continue
            keys = {k for k in attr_value.keys() if isinstance(k, str)}
            if not (keys & _PROVIDER_KEYS):
                continue
            string_values = [v for v in attr_value.values() if isinstance(v, str)]
            if not string_values:
                continue
            looks_like_embedding = any(
                "embedding" in v.lower()
                or v.startswith(("text-", "gemini-"))
                for v in string_values
            )
            if looks_like_embedding:
                found.append((base, attr_name, attr_value))
    return found


def install_method_overrides(
    cls,
    method_names: Iterable[str],
    *,
    is_target_record: Callable,
    target_return_value: object,
    is_claude_string_predicate: Optional[Callable] = None,
    log_label: str = "daadit_ai_claude",
) -> List[str]:
    """Shadow the named methods on ``cls`` with a wrapper that returns
    ``target_return_value`` when EITHER:

      * ``is_target_record(record)`` is truthy (self is on a Claude
        model), OR
      * the call's args/kwargs contain a Claude indicator (``provider=
        "anthropic"``, a Claude model id, etc.).

    Two-pronged because stock's write/validate flow sometimes hits the
    embedding-lookup with stale ``self`` state (the new ``llm_model``
    hasn't yet propagated to ``self``) but with the freshly-resolved
    provider in args/kwargs. The args/kwargs check covers that case.
    """
    patched: List[str] = []

    def _looks_claude(args, kwargs) -> bool:
        if is_claude_string_predicate is None:
            return False
        if kwargs.get("provider") in ("anthropic", "claude"):
            return True
        for c in list(args) + list(kwargs.values()):
            if isinstance(c, str):
                if c in ("anthropic", "claude") or is_claude_string_predicate(c):
                    return True
        return False

    for name in method_names:
        sentinel_attr = "_daadit_claude_patched_" + name
        if getattr(cls, sentinel_attr, False):
            continue

        def _make_override(method_name: str):
            def _override(self, *args, **kwargs):
                original: Optional[Callable] = None
                for b in type(self).__mro__:
                    b_mod = getattr(b, "__module__", "") or ""
                    if "daadit_ai_claude" in b_mod:
                        continue
                    fn = b.__dict__.get(method_name)
                    if fn is not None and not getattr(fn, "_daadit_claude_patch", False):
                        original = fn
                        break

                ctx_is_claude = (
                    is_target_record(self) or _looks_claude(args, kwargs)
                )

                if original is None:
                    if ctx_is_claude:
                        return target_return_value
                    raise AttributeError(
                        "%s: no original implementation of %s in MRO"
                        % (log_label, method_name)
                    )

                # Short-circuit BEFORE calling original when context is
                # clearly Claude. This is important when the original
                # would raise on the very first instruction (e.g. a
                # dict lookup with no fallback) — wrapping it in
                # try/except wouldn't help in that case anyway.
                if ctx_is_claude:
                    _logger.debug(
                        "%s: short-circuited %s (claude context) → %r",
                        log_label, method_name, target_return_value,
                    )
                    return target_return_value

                try:
                    return original(self, *args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    is_target_err = any(m in msg for m in ERROR_MARKERS)
                    if not is_target_err:
                        if isinstance(exc, KeyError) and ("claude" in msg.lower() or "anthropic" in msg.lower()):
                            is_target_err = True
                    if is_target_err and ctx_is_claude:
                        _logger.info(
                            "%s: intercepted %s (%s); returning %r",
                            log_label, method_name, type(exc).__name__,
                            target_return_value,
                        )
                        return target_return_value
                    raise

            _override.__name__ = method_name
            _override.__qualname__ = "%s.%s" % (cls.__name__, method_name)
            _override._daadit_claude_patch = True
            return _override

        setattr(cls, name, _make_override(name))
        setattr(cls, sentinel_attr, True)
        patched.append(name)
    return patched


def patch_provider_dicts(
    dict_specs: Iterable[Tuple[type, str, dict]],
    *,
    new_key: str = "anthropic",
    new_value: str = "text-embedding-3-small",
    log_label: str = "daadit_ai_claude",
) -> List[str]:
    """Add ``{new_key: new_value}`` to every dict in ``dict_specs`` if absent.

    Default ``new_value`` is stock's OpenAI embedding model id, which
    is always present in ``ai.embedding.embedding_model`` and which
    stock already knows how to route. Anthropic has no embeddings API
    so chat goes through Claude while embeddings fall back to OpenAI /
    IAP.
    """
    modified: List[str] = []
    for base, attr_name, dict_obj in dict_specs:
        if new_key in dict_obj:
            continue
        dict_obj[new_key] = new_value
        path = "%s.%s.%s" % (base.__module__, base.__name__, attr_name)
        modified.append(path)
        _logger.info(
            "%s: patched provider dict %s → added %r=%r",
            log_label, path, new_key, new_value,
        )
    return modified


# ============================================================================
# GLOBAL SCAN — for callables that live outside any Odoo-registered model
# ============================================================================


def _looks_claude(args, kwargs, claude_predicate, claude_string_predicate) -> bool:
    """Return True if any positional or keyword arg suggests a Claude
    record/provider/model is in scope.
    """
    candidates = list(args) + list(kwargs.values())
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, str):
            if c in ("anthropic", "claude") or claude_string_predicate(c):
                return True
            continue
        try:
            llm = getattr(c, "llm_model", None)
            if llm and claude_string_predicate(llm):
                return True
        except Exception:  # noqa: BLE001
            pass
    if kwargs.get("provider") in ("anthropic", "claude"):
        return True
    model_kw = kwargs.get("model")
    if isinstance(model_kw, str) and claude_string_predicate(model_kw):
        return True
    return False


def discover_in_loaded_modules(
    *,
    module_prefix: str = "odoo.addons.ai",
    our_module_marker: str = "daadit_ai_claude",
):
    """Walk ``sys.modules`` and yield matching callables.

    For each module under ``module_prefix`` we inspect both
    module-level functions and class methods, returning callables
    whose bytecode references one of the error markers. Caller wraps
    each to short-circuit when the args look Claude-flavored.
    """
    import sys

    found = []
    seen_codes = set()

    for module_name, module in list(sys.modules.items()):
        if module is None:
            continue
        if not module_name or module_prefix not in module_name:
            continue
        if our_module_marker in module_name:
            continue
        try:
            module_vars = list(vars(module).items())
        except Exception:  # noqa: BLE001
            continue

        for attr_name, attr in module_vars:
            try:
                if callable(attr) and not isinstance(attr, type):
                    code = getattr(attr, "__code__", None)
                    if code is None or id(code) in seen_codes:
                        continue
                    if _scan_code_for_markers(code, ERROR_MARKERS):
                        seen_codes.add(id(code))
                        found.append((module, attr_name, attr))
                elif isinstance(attr, type):
                    if attr.__module__ != module_name:
                        continue
                    try:
                        cls_vars = list(vars(attr).items())
                    except Exception:  # noqa: BLE001
                        continue
                    for method_name, method in cls_vars:
                        try:
                            if not callable(method):
                                continue
                            code = getattr(method, "__code__", None)
                        except Exception:  # noqa: BLE001
                            continue
                        if code is None or id(code) in seen_codes:
                            continue
                        if _scan_code_for_markers(code, ERROR_MARKERS):
                            seen_codes.add(id(code))
                            found.append((attr, method_name, method))
            except Exception:  # noqa: BLE001
                _logger.debug(
                    "discover_in_loaded_modules: skipped %s.%s due to "
                    "unexpected exception", module_name, attr_name,
                    exc_info=True,
                )
                continue
    return found


def install_module_overrides(
    found,
    *,
    target_return_value,
    is_claude_string_predicate,
    is_claude_record_predicate=None,
    log_label: str = "daadit_ai_claude",
) -> List[str]:
    """Wrap each callable in ``found`` with a Claude-aware short-circuit."""
    patched: List[str] = []
    is_claude_record_predicate = is_claude_record_predicate or (lambda _: False)
    for parent, attr_name, original in found:
        sentinel = "_daadit_claude_patched_" + attr_name
        if getattr(parent, sentinel, False):
            continue
        if getattr(original, "_daadit_claude_patch", False):
            continue

        def _make_wrapper(orig, name, parent_obj):
            def _wrapper(*args, **kwargs):
                ctx_is_claude = _looks_claude(
                    args, kwargs,
                    is_claude_record_predicate,
                    is_claude_string_predicate,
                )
                if ctx_is_claude:
                    _logger.debug(
                        "%s: short-circuited %s.%s → %r",
                        log_label,
                        getattr(parent_obj, "__name__", repr(parent_obj)),
                        name, target_return_value,
                    )
                    return target_return_value
                try:
                    return orig(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    if any(m in str(exc) for m in ERROR_MARKERS):
                        if ctx_is_claude:
                            _logger.info(
                                "%s: caught %s in %s.%s → returning %r",
                                log_label, type(exc).__name__,
                                getattr(parent_obj, "__name__", repr(parent_obj)),
                                name, target_return_value,
                            )
                            return target_return_value
                    raise
            _wrapper.__name__ = name
            _wrapper.__qualname__ = "%s.%s" % (
                getattr(parent_obj, "__name__", "?"), name,
            )
            _wrapper._daadit_claude_patch = True
            _wrapper._daadit_claude_original = orig
            return _wrapper

        try:
            wrapped = _make_wrapper(original, attr_name, parent)
            setattr(parent, attr_name, wrapped)
            setattr(parent, sentinel, True)
            path = "%s.%s.%s" % (
                getattr(parent, "__module__", "?"),
                getattr(parent, "__name__", "?"),
                attr_name,
            )
            patched.append(path)
            _logger.info("%s: wrapped %s", log_label, path)
        except (TypeError, AttributeError) as exc:
            _logger.warning(
                "%s: could not patch %s.%s: %s",
                log_label, parent, attr_name, exc,
            )
    return patched
