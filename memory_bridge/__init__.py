"""
Antharmaya Memory Bridge — unified agent memory, with a plugin for Hermes Agent.

``__version__`` below is the single source of truth for the release number:
pyproject.toml reads it at build time (``[tool.setuptools.dynamic]``), and the
CLI, the MCP server and the Hermes plugin entry point import it at runtime.
It lives in source rather than in installed metadata because the curl
installer runs the package from a git checkout, where no metadata exists.
"""

__version__ = "0.6.0"
