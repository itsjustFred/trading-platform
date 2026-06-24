"""Abstract base class for backtest-result importers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from validation.reference import ValidationReference


class BaseImporter(ABC):
    """Base class for all backtest-result importers.

    Implementations must override ``load`` and return a fully hydrated
    ``ValidationReference``.
    """

    @abstractmethod
    def load(self, source: str | Path, **kwargs: Any) -> ValidationReference:
        """Import a backtest result from *source* and normalize it.

        Parameters
        ----------
        source:
            File path or URI pointing to the external report.
        **kwargs:
            Implementation-specific options (e.g. sheet name, encoding).

        Returns
        -------
        ValidationReference
            Canonical representation of the imported result.
        """
        ...
