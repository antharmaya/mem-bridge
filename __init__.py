"""
Antharmaya Memory Bridge — Hermes Plugin entry point.

Auto-discovered by Hermes plugin system. When loaded, registers
the AntharmayaMemoryProvider with the MemoryManager.
"""

from src.provider import register

__version__ = "0.1.1"
