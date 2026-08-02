#!/usr/bin/env python3
"""SeeTheFiles runner entry point (packaged as SeeTheFiles-Run.exe via PyInstaller).

This is the program that the right-click menu actually invokes:

    SeeTheFiles-Run.exe "%1"

It receives the right-clicked folder path as argv[1] and delegates to
see_the_files.main(), rendering the self-contained HTML view in the default
browser. Packaged with --windowed so no console window flashes.

The machine that runs this does NOT need Python installed: the PyInstaller
build embeds a full Python runtime. config.toml placed next to this exe is
auto-loaded by see_the_files.load_config(); otherwise built-in defaults apply.
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

from see_the_files import main as _stf_main


def _write_log(text: str) -> Path:
    log_path = Path(sys.argv[0]).resolve().parent / "SeeTheFiles-Run.log"
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass
    return log_path


def _fatal(msg: str) -> int:
    """Surface errors to the user.

    When packaged --windowed there is no console, so a silent crash would leave
    the user with nothing. Use a native Win32 message box (works without a
    console) and also write the full detail to SeeTheFiles-Run.log.

    NOTE: we deliberately do NOT open a file:// HTML page in the browser — modern
    browsers block cross-file:// navigation as a security origin violation, which
    would just produce a confusing second error instead of the real one.
    """
    log_path = _write_log(msg)
    detail = f"{msg}\n\n详细日志已写入:\n{log_path}"
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, detail, "SeeTheFiles 运行出错", 0x10  # MB_ICONERROR
            )
        except Exception:
            pass
    else:
        print(detail, file=sys.stderr)
    return 1


def main() -> int:
    # argv[1] is the folder path passed by Explorer's "%1".
    argv = sys.argv[1:]
    if not argv:
        return _fatal("未收到文件夹路径（右键菜单未正确传入 %1）。")
    target = Path(argv[0])
    if not target.exists():
        return _fatal(f"路径不存在: {target}")
    if not target.is_dir():
        return _fatal(f"不是文件夹: {target}")
    try:
        return _stf_main(argv)
    except Exception:  # pragma: no cover - defensive
        tb = traceback.format_exc()
        return _fatal(f"渲染失败:\n{tb}")


if __name__ == "__main__":
    sys.exit(main())
