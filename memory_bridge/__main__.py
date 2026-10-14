"""``python -m memory_bridge`` — same as the ``memory-bridge`` console script."""

import sys

from .cli import main

sys.exit(main())
