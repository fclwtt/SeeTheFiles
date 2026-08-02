#!/usr/bin/env python3
"""SeeTheFiles uninstaller (CAP-5). Removes the right-click menu registration.

Usage:
    python uninstall.py          # remove from HKLM then HKCU
    python uninstall.py --user   # remove from HKCU only
    python uninstall.py --machine# remove from HKLM only (requires admin)
"""

from __future__ import annotations

import argparse
import os
import sys

# Delegate to install.py --uninstall logic to keep a single source of truth.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from install import main as _install_main  # noqa: E402


def main() -> int:
    if os.name != "nt":
        sys.stderr.write("[error] 右键注册仅适用于 Windows。\n")
        return 1
    # Re-invoke install.py with --uninstall and forward any --user/--machine flags.
    import shlex
    argv = ["--uninstall"]
    raw = sys.argv[1:]
    if "--user" in raw:
        argv.append("--user")
    if "--machine" in raw:
        argv.append("--machine")
    return _install_main(argv)


if __name__ == "__main__":
    sys.exit(main())
