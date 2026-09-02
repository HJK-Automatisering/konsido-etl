from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("konsido-etl")
except PackageNotFoundError:  # pragma: no cover
    # Pakken er ikke installeret (fx kørsel direkte fra kildetræet)
    __version__ = "0.0.0+ukendt"

__all__ = ["__version__"]
