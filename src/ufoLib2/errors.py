from __future__ import annotations

from typing import Any


class Error(Exception):
    """The base exception for ufoLib2."""


class ExtrasNotInstalledError(Error):
    """The extras required for this feature are not installed."""

    def __init__(self, extras: str) -> None:
        super().__init__(f"Extras not installed: ufoLib2[{extras}]")

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        raise self


class CircularReferenceError(Error):
    """Raised when components reference each other cyclically.

    The rounding analysis walks component references recursively and cannot
    terminate on a glyph whose component graph contains a cycle.
    """

    def __init__(self, glyph: str, path: tuple[str, ...]) -> None:
        self.glyph = glyph
        """The glyph from which the cyclic walk started."""

        self.path = path
        """The component chain that closes the cycle, including the repeated
        glyph name at the end."""

        super().__init__(
            "Circular component reference in glyph "
            f"{glyph!r}: {' -> '.join(path)}"
        )
