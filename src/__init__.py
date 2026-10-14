"""
Deprecated alias for :mod:`memory_bridge` — kept for one release (0.6.x).

Until 0.5.0 the importable package was called ``src``, which collides with
any other project that ships a top-level ``src`` package. It is now
``memory_bridge``. ``import src`` / ``from src.scanner import ...`` keep
working for 0.6.x and emit a DeprecationWarning; the alias is removed in
0.7.0.

Every ``src.<name>`` resolves to the *same module object* as
``memory_bridge.<name>`` (not a copy), so isinstance checks, module-level
state and monkeypatching behave identically under either name.
"""

import importlib
import importlib.abc
import importlib.util
import sys
import warnings

warnings.warn(
    "The 'src' package is deprecated; import 'memory_bridge' instead "
    "(e.g. 'from memory_bridge.scanner import discover_all'). "
    "The 'src' alias will be removed in memory-bridge 0.7.0.",
    DeprecationWarning,
    stacklevel=2,
)

import memory_bridge as _target  # noqa: E402

__version__ = _target.__version__
__path__ = []  # a package, but every submodule comes from the finder below

_PREFIX = __name__ + "."
_TARGET_PREFIX = _target.__name__ + "."


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target_name):
        self._target_name = target_name

    def create_module(self, spec):
        return importlib.import_module(self._target_name)

    def exec_module(self, module):
        pass  # already executed under its real name


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_PREFIX):
            return None
        real = _TARGET_PREFIX + fullname[len(_PREFIX):]
        real_spec = importlib.util.find_spec(real)
        if real_spec is None:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _AliasLoader(real),
            is_package=real_spec.submodule_search_locations is not None,
        )


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())


def __getattr__(name):
    # `import src; src.scanner` after `import memory_bridge.scanner` elsewhere.
    try:
        return importlib.import_module(_PREFIX + name)
    except ImportError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
