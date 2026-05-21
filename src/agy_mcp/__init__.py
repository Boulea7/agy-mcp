"""agy-mcp — Skill-first, MCP-second bridge to Google Antigravity CLI (agy)."""

from importlib import metadata

try:
    __version__ = metadata.version("agy-mcp")
except metadata.PackageNotFoundError:  # editable / source checkout before install
    __version__ = "0.1.4"

__all__ = ["__version__"]
