#!/usr/bin/env python3
"""Run the Typecho import and create fallbacks for files unavailable in the backup."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "migrate_typecho.py"),
            *sys.argv[1:],
        ],
        cwd=root,
        check=False,
    )
    if result.returncode not in {0, 2}:
        return result.returncode
    repair = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "create_missing_media_placeholders.py"),
            "--output",
            ".",
        ],
        cwd=root,
        check=False,
    )
    return repair.returncode


if __name__ == "__main__":
    raise SystemExit(main())
