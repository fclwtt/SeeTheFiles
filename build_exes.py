#!/usr/bin/env python3
"""Build the SeeTheFiles Windows executables via PyInstaller.

Produces three self-contained .exe files in ./dist (no Python required on the
target machine):

    SeeTheFiles-Install.exe   - registers the right-click menu (per-user by default)
    SeeTheFiles-Uninstall.exe - removes the right-click menu
    SeeTheFiles-Run.exe       - the runner invoked by the right-click command;
                                carries its own Python runtime, windowless

Usage:
    python build_exes.py            # build all three into ./dist
    python build_exes.py --clean    # remove build/ and dist/ first

After building, ship the whole dist/ folder (the three .exe files plus
config.toml copied alongside). The user double-clicks SeeTheFiles-Install.exe
and the menu appears — no Python, no editing files.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (source script, output exe name, extra PyInstaller flags)
# NOTE: --windowed only makes sense on Windows (suppresses the console window when
# the right-click command launches the runner). On macOS, --windowed forces a .app
# bundle in onefile mode and will error in PyInstaller v7, so we gate it per-platform.
RUN_WINDOWED = ["--windowed"] if sys.platform == "win32" else []
TARGETS = [
    # NOTE: no --uac-admin here. Default install is per-user (HKCU) and must NOT
    # trigger a UAC prompt — otherwise the elevated process writes into the
    # *administrator's* HKCU, and the actual user never sees the menu. Users who
    # want machine-wide install run SeeTheFiles-Install.exe as admin themselves.
    ("install.py", "SeeTheFiles-Install.exe", []),
    ("uninstall.py", "SeeTheFiles-Uninstall.exe", []),
    # --windowed (Windows only) => no console flash when launched from Explorer.
    ("run_app.py", "SeeTheFiles-Run.exe", RUN_WINDOWED),
]


def _run(pyinstaller: str, script: str, out_name: str, extra: list[str]) -> int:
    cmd = [
        pyinstaller,
        "--onefile",
        "--noconfirm",
        "--name", out_name.replace(".exe", ""),
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build"),
        *extra,
        str(HERE / script),
    ]
    print("+", " ".join(cmd))
    return subprocess.call(cmd)


def main() -> int:
    pyinstaller = shutil.which("pyinstaller")
    if not pyinstaller:
        # Fall back to the python -m form.
        pyinstaller = sys.executable
        base = [sys.executable, "-m", "PyInstaller"]
    else:
        base = [pyinstaller]

    if "--clean" in sys.argv[1:]:
        for d in ("build", "dist"):
            p = HERE / d
            if p.exists():
                shutil.rmtree(p)
                print(f"removed {p}")

    if pyinstaller == sys.executable:
        # Rebuild the command list with the -m form.
        for script, out_name, extra in TARGETS:
            cmd = [*base, "--onefile", "--noconfirm", "--name", out_name.replace(".exe", ""),
                   "--distpath", str(HERE / "dist"), "--workpath", str(HERE / "build"),
                   *extra, str(HERE / script)]
            print("+", " ".join(cmd))
            rc = subprocess.call(cmd)
            if rc != 0:
                return rc
    else:
        for script, out_name, extra in TARGETS:
            rc = _run(pyinstaller, script, out_name, extra)
            if rc != 0:
                return rc

    # Copy config.toml next to the runner so it is auto-loaded.
    cfg = HERE / "config.toml"
    if cfg.is_file():
        shutil.copy(cfg, HERE / "dist" / "config.toml")
        print(f"copied config.toml -> dist/")

    print("\nDone. Ship the contents of dist/ :")
    for _, out_name, _ in TARGETS:
        print(f"  - dist/{out_name}")
    print("  - dist/config.toml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
