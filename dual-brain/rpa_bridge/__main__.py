"""RPA Bridge subprocess entry point.

Invoked by the Controller daemon. Not intended for direct user execution.
"""

import sys

from .bridge import RpaBridge

if __name__ == "__main__":
    sys.exit(RpaBridge.main())
