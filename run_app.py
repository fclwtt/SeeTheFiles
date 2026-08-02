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
from pathlib import Path

from see_the_files import main as _stf_main


def _fatal(msg: str) -> int:
    """Surface errors to the user.

    When packaged --windowed there is no console, so a silent crash would leave
    the user with nothing. Write a small .log next to the exe and also pop a
    message via a temp HTML notice so failures are never invisible.
    """
    log_path = Path(sys.argv[0]).resolve().parent / "SeeTheFiles-Run.log"
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass
    try:
        notice = Path(tempfile.gettempdir()) / "SeeTheFiles_error.html"
        notice.write_text(
            "<!doctype html><meta charset=utf-8><title>SeeTheFiles</title>"
            f"<body style='font-family:sans-serif;padding:24px'>"
            f"<h2>SeeTheFiles 运行出错</h2><pre style='white-space:pre-wrap'>{msg}</pre>"
            f"</body>",
            encoding="utf-8",
        )
        import webbrowser
        webbrowser.open(notice.as_uri())
    except Exception:
        pass
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
    except Exception as exc:  # pragma: no cover - defensive
        return _fatal(f"渲染失败: {exc}")


if __name__ == "__main__":
    sys.exit(main())
