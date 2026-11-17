"""Support ``python -m codegrinder`` in addition to ``grind``."""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
