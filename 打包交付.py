# -*- coding: utf-8 -*-
"""一键打包交付包：把整个项目打成可直接发给评审的 ZIP。

铁律（与项目密钥纪律一致）：
- **绝不把密钥打进去**：`.env` / `*.local.json` / 任何 `keys.json` 一律排除；
- **必须把离线依赖打进去**：`wheels/` 保留（这是"评审不联网也能装起来"的闭环）；
- 排除虚拟环境与缓存（`.venv` / `.venv_test` / `__pycache__` 等），保持包体干净。

用法：
    双击本文件，或  python 打包交付.py
产物：
    04_交付物/OPC接单吧第三届_交付包_YYYYMMDD.zip
"""

from __future__ import annotations

import datetime
import fnmatch
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "04_交付物"
TOP_NAME = "OPC接单吧第三届实战能力大赛"  # ZIP 内的顶层目录名（解压后即项目根）

EXCLUDE_DIRS = {
    ".venv", ".venv_test", "venv", "env", "__pycache__", ".git",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "node_modules",
    "99_内部留档",
}
EXCLUDE_FILE_PATTERNS = (
    "*.pyc", "*.pyo", ".env", "*.local.json", "keys.json", "*.log",
    ".DS_Store", "*.bak", "*.orig", "*.tmp",
)
# 除上述外，再硬性拦一道"疑似密钥文件"（中文文件名也覆盖）
SECRET_NAME_HINTS = ("密钥", "secret", "credential", "token", "apikey", "api_key")

MUST_HAVE = [
    # 可运行入口
    "README.md", "启动项目.py", "启动项目.bat", "launcher.py", ".gitignore",
    # 打包 / 复验工具（生成交付包 + 独立复验）
    "打包交付.py", "复验交付包.py",
    # 后端依赖与离线闭环
    "02_源码/requirements.txt",
    # 核心源码（编排层 / 护栏层 / 检索层 / 密钥层 / 运行保障）
    "02_源码/src/server.py", "02_源码/src/orchestrator.py", "02_源码/src/guardrails.py",
    "02_源码/src/search.py", "02_源码/src/keystore.py", "02_源码/src/cost_gate.py",
    "02_源码/src/config.py", "02_源码/src/llm_client.py", "02_源码/src/schema.py",
    "02_源码/src/session.py", "02_源码/src/providers.py", "02_源码/src/keys_routes.py",
    "02_源码/src/guard.py", "02_源码/src/__init__.py",
    # 厂商预设与前端
    "02_源码/keys/providers.preset.json", "02_源码/static/index.html",
    "02_源码/static/小程序接口测试.html",
    # 容器化部署
    "02_源码/deploy/Dockerfile", "02_源码/deploy/docker-compose.yml",
    "02_源码/deploy/.env.example",
    # 自动化测试（8 组验收 + 真实性取证 + 检索层解析 + 两轮对抗 + 统一入口）
    "02_源码/跑全部测试.py",
    "02_源码/tests/conftest.py", "02_源码/tests/test_all_suites.py",
    "02_源码/tests/test_smoke.py", "02_源码/tests/test_guardrails.py",
    "02_源码/tests/test_acceptance_8groups.py", "02_源码/tests/test_authenticity.py",
    "02_源码/tests/test_adversarial.py", "02_源码/tests/test_adversarial2.py",
    "02_源码/tests/test_search_parsers.py",
    # 间接提示注入防御（R13）与编排层注入连线（本轮安全加固新增，必须随包）
    "02_源码/tests/test_injection_defense.py",
    "02_源码/tests/test_orchestrator_injection_wiring.py",
    "02_源码/tests/生成8组表格.py",
    # 检索层离线测试样本（真实页面快照 + 采集脚本，保证测试可复跑、不被质疑编造）
    "02_源码/tests/fixtures/采集样本.py",
    "02_源码/tests/fixtures/_采集mso360.py",
    "02_源码/tests/fixtures/_采集记录.json",
    "02_源码/tests/fixtures/sogou_sample.html",
    "02_源码/tests/fixtures/m_so360_sample.html",
    "02_源码/tests/fixtures/so360_block_sample.html",
    "02_源码/tests/fixtures/baidu_block_sample.html",
    # 方案与测试记录（评审查阅用）
    "01_需求与方案/使用与架构说明_赛题逐项对照_2026-09-21.md",
    "01_需求与方案/赛题原文_提取_2026-09-21.txt",
    "01_需求与方案/小程序接口_2026-09-21.md",
    "01_需求与方案/部署说明_2026-09-21.md",
    "01_需求与方案/护栏层_2026-09-21.md",
    "01_需求与方案/编排层_2026-09-21.md",
    "03_测试记录/验收测试_8组_2026-09-21.md",
    "03_测试记录/验收8组_逐组记录_20260921.json",
    "03_测试记录/真实性证据_2026-09-21.md",
    "03_测试记录/对抗提示词自测_2026-09-21.md",
    "03_测试记录/检索层风控与加固_2026-09-21.md",
    "03_测试记录/全量测试结果_20260921.json",
    "03_测试记录/交付包验收_2026-09-21.md",
    # ── 进阶补齐批次新增的交付物与测试（此前漏在 MUST_HAVE 之外，包内没被守住）──
    # 商业交付能力的兜底材料（需重点完善的维度，必须随包）
    "01_需求与方案/商业交付与落地说明_2026-09-22.md",
    # A5 模拟小程序界面页 + 前端断言 / 澄清回归测试
    "02_源码/static/便民就医助手_界面模拟.html",
    "02_源码/tests/test_frontend_smoke.py",
    "02_源码/tests/test_clarify_regression.py",
    "02_源码/tests/test_ops_guarantees.py",
    # 检索层离线测试的**必需样本**：缺任一，test_search_parsers §0 会直接判失败
    "02_源码/tests/fixtures/sm_昆明_三甲医院_心血管内科.html",
    "02_源码/tests/fixtures/sm_昆明_抗蛇毒血清_医院.html",
    # 测试证据
    "03_测试记录/离线全测汇总_20260923.json",
    # 十套统一入口的全量汇总（含「真直连」两层断言设计 + 三态判定 + 第四轮/退化环境变异结果）
    # ⚠️ 原始逐行日志是 *.log，被 EXCLUDE_FILE_PATTERNS 按设计排除；故这份汇总必须随包，
    #    否则「10 套全过」在包内缺一份可读证据。
    "03_测试记录/全量测试汇总_20260923.md",
    "03_测试记录/截图/验收_G2_抗蛇毒血清三态.png",
    "03_测试记录/截图/验收_G4_用药边界.png",
    "03_测试记录/截图/验收_G5_紧急120.png",
]


def should_skip(rel: Path) -> bool:
    if set(rel.parts) & EXCLUDE_DIRS:
        return True
    name = rel.name
    if any(fnmatch.fnmatch(name, p) for p in EXCLUDE_FILE_PATTERNS):
        return True
    low = name.lower()
    # 只拦"像密钥文件"的（.json/.env/.txt/.ini），避免误伤目录名或文档
    if rel.suffix.lower() in (".json", ".env", ".ini", ".txt", ".yaml", ".yml") and \
            any(h in low for h in SECRET_NAME_HINTS):
        return True
    return False


def main() -> int:
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = OUT_DIR / f"OPC接单吧第三届_交付包_{stamp}.zip"

    collected: list[tuple[Path, Path]] = []
    for p in sorted(ROOT.rglob("*")):
        if p.is_dir():
            continue
        rel = p.relative_to(ROOT)
        if rel.parts and rel.parts[0] == OUT_DIR.name:  # 不把交付物目录自身打进去
            continue
        if should_skip(rel):
            continue
        collected.append((p, rel))

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, rel in collected:
            zf.write(src, arcname=str(Path(TOP_NAME) / rel))

    names = [rel.as_posix() for _, rel in collected]
    size_mb = zip_path.stat().st_size / 1024 / 1024

    print("=" * 68)
    print("交付包：%s" % zip_path)
    print("文件数：%d ｜ 体积：%.2f MB" % (len(collected), size_mb))
    print("-" * 68)

    missing = [m for m in MUST_HAVE if m not in names]
    wheels = [n for n in names if n.startswith("wheels/")]
    leaks = [n for n in names
             if n.endswith("keys.json") or n.endswith(".env") or "local.json" in n]

    print("关键文件缺失 ：%s" % (missing or "无 ✅"))
    print("离线依赖 wheels：%d 个 ✅" % len(wheels))
    print("疑似密钥文件  ：%s" % (leaks or "无 ✅（安全）"))
    print("顶层目录      ：%s/" % TOP_NAME)
    print("=" * 68)
    return 0 if (not missing and not leaks and wheels) else 1


if __name__ == "__main__":
    sys.exit(main())
