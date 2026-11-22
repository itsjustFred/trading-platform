"""Optional numba dispatch for the shared indicator kernels (issue #378).

The hot sequential loops in :mod:`shared.indicators.core` and
:mod:`shared.indicators.presets` are written once in pure Python/NumPy
(``*_kernel_py``) and compiled with ``numba.njit(cache=True,
fastmath=False)`` (``*_kernel_nb``) when numba is importable. Numba is an
optional runtime dependency of these kernels only: the pure Python/NumPy
implementation remains the reference and is used whenever

- numba cannot be imported (logged once at DEBUG), or
- ``TP_DISABLE_NUMBA=1`` is set in the environment, or
- the compiled kernel raises (compile or runtime failure; the kernel is
  marked failed and the Python path takes over, logged once at DEBUG).

Parity between the two paths is pinned bit-for-bit by
``tests/indicators/test_numba_parity.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

_log = logging.getLogger(__name__)

_DISABLE_ENV = "TP_DISABLE_NUMBA"

_checked = False
_njit: Callable[..., Any] | None = None
_failed: set[str] = set()


def _load_njit() -> Callable[..., Any] | None:
    """Import numba once and return ``numba.njit``; None when unavailable."""
    global _checked, _njit
    if _checked:
        return _njit
    _checked = True
    try:
        import numba
    except Exception:
        _log.debug(
            "numba is not importable; indicator kernels use the Python path",
            exc_info=True,
        )
        return None
    _njit = numba.njit
    return _njit


def compile_kernel(py_kernel: Callable) -> Callable | None:
    """Return ``njit(cache=True, fastmath=False)(py_kernel)`` or None.

    ``njit`` wraps lazily, so this only fails when numba itself rejects the
    decoration; actual compilation errors surface on first call and are
    handled by :func:`call`.
    """
    njit = _load_njit()
    if njit is None:
        return None
    try:
        return njit(cache=True, fastmath=False)(py_kernel)
    except Exception:
        _log.debug(
            "numba could not wrap %s; using the Python kernel",
            py_kernel.__name__,
            exc_info=True,
        )
        return None


def _disabled() -> bool:
    return os.environ.get(_DISABLE_ENV) == "1"


def call(
    py_kernel: Callable,
    nb_kernel: Callable | None,
    *args: Any,
) -> Any:
    """Run ``nb_kernel(*args)`` when enabled and healthy, else ``py_kernel``.

    ``TP_DISABLE_NUMBA`` is read on every call so tests and operators can
    flip it without re-importing the module.
    """
    name = getattr(py_kernel, "__name__", repr(py_kernel))
    if nb_kernel is None or _disabled() or name in _failed:
        return py_kernel(*args)
    try:
        return nb_kernel(*args)
    except Exception:
        _failed.add(name)
        _log.debug(
            "numba kernel for %s failed; falling back to the Python kernel",
            name,
            exc_info=True,
        )
        return py_kernel(*args)
