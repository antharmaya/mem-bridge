"""Packaging, versioning and Hermes-loading regression tests.

Pins three field failures found in the v0.5.0 audit:

* the version number disagreed with itself in four places (0.5.0 / 0.1.1 /
  0.3.0);
* the wheel installed a top-level package called ``src``;
* Hermes discovered the plugin but could not import it
  (``No module named 'src'``), because Hermes loads the plugin's
  ``__init__.py`` as ``_hermes_user_memory.<name>`` without putting the
  plugin directory on ``sys.path``.
"""

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import memory_bridge
from memory_bridge import __version__

REPO = Path(__file__).resolve().parent.parent


def run_isolated(code, cwd):
    """Run *code* with ``python -I``: no cwd or PYTHONPATH on sys.path.

    That is the only way to prove the plugin loads through relative imports
    alone — in the test process, ``memory_bridge`` is importable anyway.
    """
    return subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(code)],
        cwd=cwd, capture_output=True, text=True, timeout=120,
    )


# ─── one version, everywhere ───────────────────────────────────────────────


def test_version_is_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_plugin_yaml_matches_package_version():
    text = (REPO / "plugin.yaml").read_text()
    m = re.search(r'^version:\s*"([^"]+)"', text, re.M)
    assert m and m.group(1) == __version__


def test_changelog_leads_with_this_version():
    first = next(line for line in (REPO / "CHANGELOG.md").read_text().splitlines()
                 if line.startswith("## "))
    assert first.startswith(f"## v{__version__} ")


def test_installer_pins_this_version():
    assert f'VERSION="${{VERSION:-v{__version__}}}"' in (REPO / "install.sh").read_text()


def test_mcp_server_reports_package_version():
    from memory_bridge.mcp_server import handle_message
    reply = handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert reply["result"]["serverInfo"]["version"] == __version__


def test_cli_version_flag():
    out = subprocess.run([sys.executable, "-m", "memory_bridge", "--version"],
                         cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"memory-bridge {__version__}"


# ─── the Hermes plugin loads the way Hermes loads it ───────────────────────

# A faithful copy of what Hermes v0.19.0 does in
# plugins/memory/__init__.py:_load_provider_from_dir and
# discover_plugin_cli_commands for a user-installed provider.
HERMES_LOADER = """
    import argparse, importlib.machinery, importlib.util, sys
    from pathlib import Path

    plugin_dir = Path({plugin_dir!r})
    name = plugin_dir.name
    ns = "_hermes_user_memory"

    def shell(pkg, locations):
        spec = importlib.machinery.ModuleSpec(pkg, None, is_package=True)
        spec.submodule_search_locations = locations
        sys.modules[pkg] = importlib.util.module_from_spec(spec)

    # discover_plugin_cli_commands(): cli.py first, before __init__.py
    shell(ns, [])
    shell(f"{{ns}}.{{name}}", [str(plugin_dir)])
    spec = importlib.util.spec_from_file_location(f"{{ns}}.{{name}}.cli", plugin_dir / "cli.py")
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    spec.loader.exec_module(cli)
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    cli.register_cli(sub.add_parser(name))
    args = parser.parse_args([name, "stats"])
    assert args.command == name, args.command  # Hermes' own dest untouched
    assert args.memory_bridge_command == "stats"
    assert args.func.__name__ == "cmd_stats"

    # _load_provider_from_dir()
    del sys.modules[f"{{ns}}.{{name}}"]
    spec = importlib.util.spec_from_file_location(
        f"{{ns}}.{{name}}", plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    class Collector:
        provider = None
        def register_memory_provider(self, p): self.provider = p
        def register_tool(self, *a, **k): pass
        def register_hook(self, *a, **k): pass
        def register_cli_command(self, *a, **k): pass

    c = Collector()
    mod.register(c)
    print(c.provider.name, mod.__version__, "memory_bridge" in sys.modules, "src" in sys.modules)
"""


def test_hermes_discovery_heuristic_matches_entry_point():
    # Hermes only treats a plugin dir as a memory provider if __init__.py
    # mentions one of these, within its first 8 KiB.
    head = (REPO / "__init__.py").read_text()[:8192]
    assert "register_memory_provider" in head or "MemoryProvider" in head


def test_plugin_loads_through_the_hermes_loader(tmp_path):
    result = run_isolated(HERMES_LOADER.format(plugin_dir=str(REPO)), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    # Relative imports only: neither a top-level memory_bridge nor src is used.
    assert result.stdout.split() == ["antharmaya-bridge", __version__, "False", "False"]


# ─── the deprecated `src` alias ────────────────────────────────────────────


def test_src_alias_warns_and_is_the_same_module(tmp_path):
    code = f"""
        import sys, warnings
        sys.path.insert(0, {str(REPO)!r})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from src.scanner import discover_all
            from src.scanners.base import _validate_table_name
            import src.indexer
        import memory_bridge.scanner, memory_bridge.indexer
        assert any(issubclass(w.category, DeprecationWarning) and "memory_bridge" in str(w.message)
                   for w in caught), [str(w.message) for w in caught]
        assert discover_all is memory_bridge.scanner.discover_all
        assert src.indexer is memory_bridge.indexer
        print("ok")
    """
    result = run_isolated(code, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_package_imports_never_use_the_src_name():
    offenders = [
        f"{p.relative_to(REPO)}:{i}"
        for p in (REPO / "memory_bridge").rglob("*.py")
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if re.match(r"\s*(from|import)\s+src\b", line)
    ]
    assert not offenders


def test_mcp_server_starts_and_lists_tools():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in msgs)
    out = subprocess.run([sys.executable, "-m", "memory_bridge", "mcp"], cwd=REPO,
                         input=stdin, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    replies = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    assert replies[0]["result"]["serverInfo"]["version"] == memory_bridge.__version__
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert {"search_memory", "recall", "list_decisions", "memory_stats"} <= names
