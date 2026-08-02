#!/usr/bin/env python3
"""SeeTheFiles installer (CAP-5).

Registers a Windows Explorer right-click menu item on folders that runs the
packaged runner (SeeTheFiles-Run.exe) which carries its own Python runtime:

    "<this_folder>/SeeTheFiles-Run.exe" "%1"

Because the runner is a self-contained executable, the target machine does NOT
need Python installed. This is the "one-click, zero-friction" install path:
ship the folder (Install.exe + Uninstall.exe + Run.exe + config.toml), the user
double-clicks Install.exe and the menu appears — no Python, no editing files.

Default install is per-user (HKCU), which requires no administrator rights and
is therefore "silent / frictionless". Use --machine (run as admin) to register
for all users via HKLM.

Usage:
    SeeTheFiles-Install.exe          # current user (HKCU), no admin needed
    python install.py                # same, from source
    python install.py --user         # force HKCU (current user)
    python install.py --machine      # all users (HKLM, requires admin)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

APP_NAME = "SeeTheFiles"
MENU_TEXT = "用 SeeTheFiles 查看结构"
# The packaged runner (SeeTheFiles-Run.exe). It embeds its own Python runtime, so
# the target machine does NOT need Python installed. It is distributed alongside
# this installer in the same folder.
RUNNER_NAME = "SeeTheFiles-Run.exe"

# Registry location for folder right-click.
# We target Directory\\shell so it appears on folders (incl. right-click of a folder
# in the left tree and on a folder in the right pane).
#
# IMPORTANT: per-user (HKCU) registrations live under
#   HKEY_CURRENT_USER\Software\Classes\Directory\shell\...
# and machine-wide (HKLM) under
#   HKEY_LOCAL_MACHINE\Software\Classes\Directory\shell\...
# Explorer only reads the `Software\Classes` subtree, so the `Software\Classes\`
# prefix is REQUIRED — without it winreg.CreateKey would create a dead key that
# Explorer never reads (this was the bug that made the menu never appear).
#
# We use a SINGLE backslash inside the raw string. A raw string r"a\b" is the
# two-character sequence `a`, `\`, `b` — i.e. the backslash is already an escape
# for the registry separator; doubling it (r"a\\b") would literally write two
# backslashes into the key name and break the lookup.
REG_KEY = r"Software\Classes\Directory\shell\SeeTheFiles"
REG_CMD = r"Software\Classes\Directory\shell\SeeTheFiles\command"


def _exe_dir() -> Path:
    """Directory of the running executable.

    Under PyInstaller --onefile, __file__ points into the temporary _MEI unpack
    directory which is deleted after the process exits, so we must derive the
    real, persistent install folder from sys.argv[0] (the actual .exe path).
    """
    return Path(sys.argv[0]).resolve().parent


def _resolve_command() -> str:
    """Build the command string pointing at the packaged runner in this folder.

    The runner (SeeTheFiles-Run.exe) carries its own Python runtime, so we never
    reference pythonw/py -3 here. This makes the right-click menu work on machines
    with no Python installed at all.
    """
    runner = _exe_dir() / RUNNER_NAME
    return f'"{runner}" "%1"'


def _write_registry(hive: str) -> bool:
    try:
        import winreg  # type: ignore
    except ImportError:
        sys.stderr.write("[error] 此安装脚本仅适用于 Windows。\n")
        return False
    command = _resolve_command()
    try:
        key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER, REG_KEY)
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, MENU_TEXT)
        winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, str((_exe_dir() / RUNNER_NAME)))
        key.Close()
        cmd_key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER, REG_CMD)
        winreg.SetValueEx(cmd_key, None, 0, winreg.REG_SZ, command)
        cmd_key.Close()
        sys.stderr.write(f"[ok] 已写入 {hive}\\{REG_KEY}\n     命令: {command}\n")
        return True
    except PermissionError:
        sys.stderr.write(f"[error] 没有权限写入 {hive}（需要管理员）。请右键'以管理员身份运行'，或改用 --user。\n")
        return False
    except OSError as exc:
        sys.stderr.write(f"[error] 注册表写入失败: {exc}\n")
        return False


def _delete_registry(hive: str) -> None:
    try:
        import winreg  # type: ignore
    except ImportError:
        sys.stderr.write("[error] 此脚本仅适用于 Windows。\n")
        return
    try:
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER, REG_CMD)
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER, REG_KEY)
        sys.stderr.write(f"[ok] 已从 {hive} 卸载菜单项。\n")
    except FileNotFoundError:
        sys.stderr.write(f"[info] {hive} 下未找到菜单项（无需卸载）。\n")
    except PermissionError:
        sys.stderr.write(f"[error] 没有权限从 {hive} 删除（需要管理员）。\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="SeeTheFiles 右键菜单安装/卸载")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--user", action="store_true", help="仅当前用户 (HKCU)")
    grp.add_argument("--machine", action="store_true", help="所有用户 (HKLM, 需管理员)")
    ap.add_argument("--uninstall", action="store_true", help="卸载菜单项")
    args = ap.parse_args()

    if os.name != "nt":
        sys.stderr.write("[error] 右键注册目前仅支持 Windows。其他平台请直接用 CLI：python see_the_files.py <目录>\n")
        return 1

    # Decide hive.
    # Default is per-user (HKCU): no admin needed, frictionless "one-click" install.
    # Only --machine forces HKLM (all users, requires administrator).
    if args.machine:
        hives = ["HKLM"]
    else:
        hives = ["HKCU"]

    if args.uninstall:
        # Uninstall from both hives so a previous --machine install is also cleaned.
        for h in ("HKLM", "HKCU"):
            _delete_registry(h)
        return 0

    for h in hives:
        if _write_registry(h):
            return 0
        if h == "HKLM":
            sys.stderr.write("[info] 回退到当前用户 (HKCU)…\n")
            continue
    return 1


if __name__ == "__main__":
    sys.exit(main())
