#!/usr/bin/env python3
"""Entry point used by the ./workshop, workshop.ps1 and workshop.cmd launchers.

It can also be run directly: python3 workshop.py <command>
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workshop_publisher.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
