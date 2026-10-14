"""
``hermes antharmaya-bridge <command>`` — Hermes CLI integration.

Hermes imports ``cli.py`` from the *active* memory provider's directory and
calls ``register_cli(subparser)``. The subcommands are the same ones the
standalone ``memory-bridge`` console script provides. They only appear in
``hermes --help`` while ``memory.provider: antharmaya-bridge`` is set.
"""

from .memory_bridge.cli import add_commands


def register_cli(subparser) -> None:
    """Build the ``hermes antharmaya-bridge`` argparse tree."""
    add_commands(subparser, dest="memory_bridge_command")
    subparser.set_defaults(func=lambda _args: subparser.print_help())
