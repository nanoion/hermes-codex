"""Hermes directory-plugin entry point."""
from pathlib import Path
import sys

_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
if _PLUGIN_ROOT not in sys.path:
    # Hermes imports directory plugins under a synthetic namespace. The installer
    # places the pinned Codex SDK and CLI beside this package, not in Hermes' venv.
    sys.path.insert(0, _PLUGIN_ROOT)

from .hermes_codex.plugin import register

__all__ = ["register"]
