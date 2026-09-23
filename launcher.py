# -*- coding: utf-8 -*-
"""ASCII-named bootstrap for the Chinese-named launcher.

Why this file exists:
    启动项目.bat must stay pure ASCII, because cmd.exe reads batch files using the
    system OEM code page (936/GBK on Chinese Windows). Embedding a Chinese filename
    inside the .bat is therefore fragile. This tiny bootstrap has an ASCII name and
    is the only thing the .bat references; it then locates and executes the
    Chinese-named entry point 启动项目.py (Python reads source as UTF-8, so Unicode
    filenames are safe here).

It carries no logic of its own -- 启动项目.py remains the single source of truth.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "启动项目.py"

if not TARGET.is_file():
    print("  ✘ 未找到启动脚本: %s" % TARGET)
    input("按回车退出...")
    sys.exit(1)

runpy.run_path(str(TARGET), run_name="__main__")
