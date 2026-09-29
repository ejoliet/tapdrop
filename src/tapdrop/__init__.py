"""tapdrop: a temporary, standards-compliant IVOA TAP service over files in place."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tapdrop")
except PackageNotFoundError:  # pragma: no cover - only hit in a source tree without install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
