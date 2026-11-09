#!/usr/bin/env python3
"""Thin launcher so the container (and a local checkout) can run `orz`."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
