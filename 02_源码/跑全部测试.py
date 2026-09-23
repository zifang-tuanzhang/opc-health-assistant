# -*- coding: utf-8 -*-
"""统一测试入口：一条命令跑完全部十套测试，输出统一汇总表。

为什么需要它：
    本项目刻意采用「两版测试哲学」——
      · 离线注入版（冒烟 / 护栏 / 8 组验收 / 检索层解析 / 前端结构冒烟 / 澄清轮回归 / 运行保障与网关边界）：
        注入假模型与假检索，断言确定、可反复复跑（检索层解析用 fixtures/ 下的
        真实页面样本，仍属离线）；
      · 实网真机版（真实性取证 + 两轮对抗提示词）：真联网 + 真模型，出的是证据。
    各套测试因此都写成「脚本式证据工具」（各自打印明细表 + 退出码），
    而不是 pytest 函数式。本入口把它们串起来跑，给评审一个统一结论。

用法：
    python 跑全部测试.py              # 全部十套（含实网，需联网 + 已配密钥）
    python 跑全部测试.py --offline     # 跳过实网，只跑离线七套（确定、快、不需密钥）
    python 跑全部测试.py --json out.json   # 额外导出机器可读汇总

退出码：0 = 全部通过；1 = 有套件未通过。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent   # 本脚本即在 02_源码/ 内
TESTS = SRC / "tests"

# (显示名, 脚本相对 tests/ 的路径)
SUITES = [
    ("冒烟测试", "test_smoke.py"),
    ("护栏层测试", "test_guardrails.py"),
    ("8 组验收测试", "test_acceptance_8groups.py"),
    ("检索层解析（离线）", "test_search_parsers.py"),
    ("前端结构冒烟", "test_frontend_smoke.py"),
    ("澄清轮回归（U2）", "test_clarify_regression.py"),
    ("运行保障与网关边界", "test_ops_guarantees.py"),
    ("间接提示注入防御（R13）", "test_injection_defense.py"),
    ("编排层注入连线", "test_orchestrator_injection_wiring.py"),
]
# 实网真机版：需联网 + 已配密钥，故默认单列，可用 --offline 跳过
NETWORK_SUITES = [
    ("真实性取证（实网）", "test_authenticity.py"),
    ("对抗提示词·话术型（实网）", "test_adversarial.py"),
    ("对抗提示词·结构型（实网）", "test_adversarial2.py"),
]


def run_suite(name: str, script: str) -> tuple[bool, str, str]:
    """跑单个套件，返回 (是否通过, 结论行, 完整输出)。"""
    path = TESTS / script
    if not path.is_file():
        return False, "✘ 脚本不存在：%s" % script, ""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run([sys.executable, str(path)],
                           cwd=str(SRC), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return False, "✘ 超时（600 秒）", ""
    out = (p.stdout or "") + (p.stderr or "")
    # 捞每个套件自己的结论行作为明细。各套件措辞不同（有的说"通过/失败"，
    # 对抗测试说"守住/被突破"），这里都认，避免明细显示成"退出码 x"看不出结论。
    conclusion = ""
    for line in reversed(out.splitlines()):
        s = line.strip().strip("= ").strip()   # 去掉 "=====" 装饰与空格，只留正文
        pair_ok = (("通过" in s and "失败" in s) or ("守住" in s and "被突破" in s)
                   or ("项通过" in s))
        if pair_ok:
            conclusion = s
            break
    ok = (p.returncode == 0)
    return ok, conclusion or ("退出码 %d" % p.returncode), out


def main() -> int:
    offline = "--offline" in sys.argv
    suites = list(SUITES) + ([] if offline else list(NETWORK_SUITES))

    print("=" * 78)
    print("OPC 接单吧第三届 · 统一测试入口")
    print("模式：%s" % ("仅离线七套（跳过实网取证与对抗测试）" if offline
                     else "全部十套（离线七套 + 实网三套：真实性取证 + 两轮对抗提示词自测）"))
    print("=" * 78)

    results: list[dict] = []
    for name, script in suites:
        print("\n▶ 正在运行：%s（%s）" % (name, script))
        ok, conclusion, out = run_suite(name, script)
        results.append({"name": name, "script": script, "passed": ok, "conclusion": conclusion})
        # 打印该套件的关键行（结论 + 所有未通过行）
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("[FAIL]") or s.startswith("✘ ") or ("通过" in s and "失败" in s) \
                    or ("守住" in s and "被突破" in s) or ("项通过" in s):
                print("    %s" % s)
        print("    → %s" % ("通过 ✅" if ok else "未通过 ✘"))

    print("\n" + "=" * 78)
    print("统一汇总")
    print("-" * 78)
    print("  %-22s %-10s %s" % ("套件", "结果", "明细"))
    for r in results:
        print("  %-22s %-10s %s" % (r["name"], "✅ 通过" if r["passed"] else "✘ 未通过", r["conclusion"]))
    passed = sum(1 for r in results if r["passed"])
    print("-" * 78)
    print("  合计：通过 %d / 未通过 %d" % (passed, len(results) - passed))
    print("=" * 78)

    if "--json" in sys.argv:
        idx = sys.argv.index("--json")
        path = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "all_tests_result.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"passed": passed, "total": len(results), "suites": results},
                      fh, ensure_ascii=False, indent=2)
        print("结果已导出：%s" % path)

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
