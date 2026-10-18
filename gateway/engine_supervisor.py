"""Stable group leader for one gateway-owned engine process.

The owner argv is passed as separate arguments, without a shell. The leader
stays alive after the engine exits so the gateway can still authenticate and
terminate the whole process group by PID plus kernel start time.
"""

from __future__ import annotations

import subprocess
import sys
import time


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return 2
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, shell=False)
    except OSError:
        return 127
    child.wait()
    # Keep the group identity alive until the gateway kills the group. An
    # engine crash is discovered by the declared readiness observation.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
