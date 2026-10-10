#!/usr/bin/env python3
"""Run all Account Hub checks from tests/ using the existing project command."""
from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "tests" / "check_switcher.py"),
                   run_name="__main__")
