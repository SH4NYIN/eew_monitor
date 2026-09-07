"""Double-click launcher for silent background monitoring on Windows."""

import asyncio
import sys

from eew_service import main


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or ["start"])))
