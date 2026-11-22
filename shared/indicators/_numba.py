"""Optional numba dispatch for the shared indicator kernels (issue #378).

The hot sequential loops in :mod:`shared.indicators.core` and
:mod:`shared.indicators.presets` are written once in pure Python/NumPy
(``*_kernel_py``) and compiled with ``numba.njit(cache=True,
fastmath=False)`` (``*_kernel_nb``) when numba is importable. Numba is an
optional runtime dependency of these kernels only: the pure Python/NumPy
implementation remains the reference and is used whenever

- numba cannot be imported (logged once at DEBUG), or
- ``TP_DISABLE_NUMBA=1`` is set in the environment, or
- the compiled kernel raises a numba typing/compile error
  (``numba.core.errors.NumbaError``; the kernel is marked failed for the
  rest of the process and the Python path takes over, logged at WARNING).

Array arguments are coerced to ``float64`` C-contiguous arrays before the
compiled kernel sees them, so every kernel compiles exactly one signature:
object-dtype arrays (e.g. from pandas nullable dtypes) would otherwise
raise a per-signature ``TypingError`` that disabled the kernel for the
whole process, and int/float32 inputs would each pay their own compile.

Both the ``import numba`` (~0.5 s) and the per-signature compilation
(0.1-0.8 s, paid once per kernel thanks to ``cache=True``) are deferred to
the first kernel call: :func:`compile_kernel` only wraps the Python
function in a lazy thunk, so importing ``shared.indicators`` stays cheap
for the viewer, signal bots and tests that never touch the hot loops. The
first *use* of a kernel — typically the first bar a live signal bot
processes — pays the one-time compile cost; numba's cache lives in the
``__pycache__`` directory next to the kernel's source file (or under
``NUMBA_CACHE_DIR`` when that is set), so a warm checkout does not recompile.

A numba failure that the Python kernel reproduces is an input error, not
a numba defect: it is re-raised without disabling the kernel. Only
``NumbaError`` subclasses mark a kernel failed, so one bad call can never
silently downgrade every later call to the ~100-600x slower Python path.

Parity between the two paths is pinned bit-for-bit by
``tests/indicators/test_numba_parity.py``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

import numpy as np

_log = logging.getLogger(__name__)

_DISABLE_ENV = "TP_DISABLE_NUMBA"

_checked = False
_njit: Callable[..., Any] | None = None
_numba_errors: tuple[type[BaseException], ...] = ()
_failed: set[str] = set()


def _load_njit() -> Callable[..., Any] | None:
    """Import numba once and return ``numba.njit``; None when unavailable."""
    global _checked, _njit, _numba_errors
    if _checked:
        return _njit
    _checked = True
    try:
        import numba
        import numba.core.errors
    except Exception:
        _log.debug(
            "numba is not importable; indicator kernels use the Python path",
            exc_info=True,
        )
        return None
    _njit = numba.njit
    _numba_errors = (numba.core.errors.NumbaError,)
    return _njit


class _NumbaUnavailable(Exception):
    """numba is not importable or the kernel could not be njit-wrapped."""


class _LazyKernel:
    """``njit(cache=True, fastmath=False)`` wrapper deferred to first call.

    Keeps ``import numba`` and the one-time compilation off the
    ``shared.indicators`` import path; see the module docstring.
    """

    __slots__ = ("_compiled", "_wrap_failed", "py_kernel")

    def __init__(self, py_kernel: Callable) -> None:
        self.py_kernel = py_kernel
        self._compiled: Callable | None = None
        self._wrap_failed = False

    def _get(self) -> Callable:
        if self._compiled is None and not self._wrap_failed:
            njit = _load_njit()
            if njit is None:
                self._wrap_failed = True
            else:
                try:
                    self._compiled = njit(cache=True, fastmath=False)(
                        self.py_kernel
                    )
                except Exception:
                    self._wrap_failed = True
                    _log.debug(
                        "numba could not wrap %s; using the Python kernel",
                        self.py_kernel.__name__,
                        exc_info=True,
                    )
        if self._compiled is None:
            raise _NumbaUnavailable(self.py_kernel.__name__)
        return self._compiled

    def __call__(self, *args: Any) -> Any:
        return self._get()(*args)


def compile_kernel(py_kernel: Callable) -> Callable:
    """Return a lazy ``njit(cache=True, fastmath=False)`` dispatcher.

    The returned callable compiles ``py_kernel`` on first invocation (or
    raises :class:`_NumbaUnavailable`, which :func:`call` turns into the
    Python path). Nothing here imports numba, so module import stays cheap.
    """
    return _LazyKernel(py_kernel)


def _disabled() -> bool:
    return os.environ.get(_DISABLE_ENV) == "1"


def _float64_args(args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Coerce array arguments to float64 for the compiled kernel only.

    The kernels are written for float64; giving numba one dtype keeps a
    single compiled signature per kernel and stops a per-signature
    ``TypingError`` (object arrays) from disabling the kernel process-wide.
    The Python reference kernel still receives the caller's original
    arrays, so its behaviour is unchanged.
    """
    return tuple(
        np.ascontiguousarray(arg, dtype=np.float64)
        if isinstance(arg, np.ndarray)
        else arg
        for arg in args
    )


def call(
    py_kernel: Callable,
    nb_kernel: Callable | None,
    *args: Any,
) -> Any:
    """Run ``nb_kernel(*args)`` when enabled and healthy, else ``py_kernel``.

    ``TP_DISABLE_NUMBA`` is read on every call so tests and operators can
    flip it without re-importing the module.

    When the numba path raises, the Python kernel runs once as the
    arbiter: if it raises too, the failure is an input error and is
    re-raised with the kernel left enabled. Only a
    ``numba.core.errors.NumbaError`` (typing/compile failure, which would
    recur on every input) marks the kernel failed for the process.
    """
    name = getattr(py_kernel, "__name__", repr(py_kernel))
    if nb_kernel is None or _disabled() or name in _failed:
        return py_kernel(*args)
    try:
        return nb_kernel(*_float64_args(args))
    except _NumbaUnavailable:
        return py_kernel(*args)
    except Exception as exc:
        try:
            result = py_kernel(*args)
        except Exception:
            raise
        if isinstance(exc, _numba_errors):
            _failed.add(name)
            _log.warning(
                "numba kernel for %s failed to compile; falling back to "
                "the Python kernel for the rest of the process",
                name,
                exc_info=True,
            )
        else:
            _log.warning(
                "numba kernel for %s raised %s; using the Python result",
                name,
                type(exc).__name__,
                exc_info=True,
            )
        return result
