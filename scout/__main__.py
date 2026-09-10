"""``python -m scout`` entry point, which is what the container runs."""

import sys

from scout.cli import main

if __name__ == "__main__":
    sys.exit(main())
