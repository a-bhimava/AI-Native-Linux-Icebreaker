"""GUI Agent subprocess entry point.

Invoked by the Controller daemon. Not intended for direct user execution.
"""

import sys

from .agent import GuiAgent

if __name__ == "__main__":
    sys.exit(GuiAgent.main())
