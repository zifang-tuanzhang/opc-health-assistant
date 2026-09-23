# -*- coding: utf-8 -*-
"""pytest 入口：一条 `pytest tests/` 即跑完离线九套确定性测试。

设计说明见同目录 conftest.py。这里刻意只跑**离线九套**：
    冒烟 / 护栏 / 8 组验收 / 检索层解析与降级 / 前端结构冒烟 / 澄清轮回归 /
    运行保障与网关边界 / 间接提示注入防御（R13）/ 编排层注入连线
它们注入假模型与假检索（检索层解析用 tests/fixtures/ 下的真实页面快照），
不依赖网络与密钥，因此可在任何评审机器上确定性复跑。

实网三套（真实性取证 + 两轮对抗提示词）需要联网与真实模型密钥，不适合作为
CI/pytest 的默认断言，请单独运行：

    python 跑全部测试.py                    # 十二套全跑（离线九套 + 实网三套）
    python tests/test_adversarial.py        # 只跑第一轮对抗
    python tests/test_adversarial2.py       # 只跑第二轮对抗
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent        # 02_源码/
RUNNER = SRC / "跑全部测试.py"


def test_offline_suites() -> None:
    """离线九套必须全过（冒烟 / 护栏 / 8 组验收 / 检索层解析 / 前端结构冒烟 / 澄清轮回归 / 运行保障与网关边界 / 间接提示注入防御 / 编排层注入连线）。"""
    assert RUNNER.is_file(), "未找到统一测试入口：%s" % RUNNER
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, str(RUNNER), "--offline"],
        cwd=str(SRC), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900, env=env,
    )
    out = (p.stdout or "") + (p.stderr or "")
    # 把明细透出来，便于 pytest -s / 失败时定位
    print(out)
    assert p.returncode == 0, "离线测试套件未全部通过（退出码 %d），详见上方输出" % p.returncode
