"""
Antharmaya Memory Bridge — Hermes Agent memory provider plugin entry point.

Hermes discovers user-installed memory providers in
``$HERMES_HOME/plugins/<name>/`` by scanning this file for the text
``register_memory_provider`` / ``MemoryProvider``, then loads it as
``_hermes_user_memory.<name>`` with this directory as the package path.
``register(ctx)`` calls ``ctx.register_memory_provider()`` with an
AntharmayaMemoryProvider.

The imports are relative on purpose: Hermes does not put this directory on
``sys.path``, so an absolute ``from memory_bridge...`` (or the old
``from src...``) fails with ModuleNotFoundError and the provider silently
never loads.
"""

if __package__:
    from .memory_bridge import __version__
    from .memory_bridge.provider import register
else:  # imported as a plain module, e.g. by pytest collecting the repo root
    from memory_bridge import __version__
    from memory_bridge.provider import register

__all__ = ["__version__", "register"]
