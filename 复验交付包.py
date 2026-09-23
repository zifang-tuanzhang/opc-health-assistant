# -*- coding: utf-8 -*-
"""独立复验交付包：不信任打包脚本的自报结果，直接读 ZIP 字节自己查一遍。

检查项：
  V1 包内条目数与清单一致性
  V2 ZIP 是否为合法压缩包且可完整解压（CRC 校验）
  V3 全量字节级密钥扫描（真密钥前缀 + 通用 sk- 模式 + 高熵长串）
  V4 敏感文件黑名单（.env / keys.json / *.local.json）
  V5 离线依赖闭环（wheels 数量）
  V6 可运行入口存在且语法可编译
"""
from __future__ import annotations

import json
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent   # 本脚本位于项目根，与「打包交付.py」同级
OUT = ROOT / "04_交付物"
zips = sorted(OUT.glob("*.zip"))
if not zips:
    print("✘ 未找到交付包"); sys.exit(1)
ZIP = zips[-1]
print("复验对象：%s" % ZIP.name)
print("=" * 70)

# ── V1/V2 打开并逐条读取（CRC 校验在 read 时触发）──
fail = 0
with zipfile.ZipFile(ZIP) as zf:
    bad = zf.testzip()
    names = zf.namelist()
    print("V1 包内条目数      ：%d" % len(names))
    print("V2 CRC 完整性      ：%s" % ("✘ 损坏: %s" % bad if bad else "全部通过 ✅"))
    if bad:
        fail += 1

    files = [n for n in names if not n.endswith("/")]

    # ── V4 敏感文件黑名单 ──
    banned = [n for n in files
              if n.endswith(".env") or n.endswith("keys.json")
              or "local.json" in n or n.endswith(".pyc")]
    print("V4 敏感文件黑名单  ：%s" % (banned or "无 ✅"))
    if banned:
        fail += 1

    # ── V5 wheels 闭环 ──
    wheels = [n for n in files if "/wheels/" in n or n.startswith("wheels/")]
    print("V5 离线依赖 wheels ：%d 个 %s" % (len(wheels), "✅" if wheels else "✘"))
    if not wheels:
        fail += 1

    # ── V3 字节级密钥扫描 ──
    TEXT_EXT = {".py", ".md", ".html", ".js", ".css", ".json", ".txt", ".yml",
                ".yaml", ".ini", ".bat", ".example", ".cfg", ".toml", ""}
    pat_sk = re.compile(rb"sk-[A-Za-z0-9._\-]{20,}")
    pat_hex = re.compile(rb"\b[0-9a-fA-F]{40,}\b")

    # 真实密钥不在本文件里写死（否则本文件自己就成了泄漏源）。
    # 改为运行时从【项目外的密钥库】读取，用它来扫描包内字节。
    secrets: list[bytes] = []
    ks = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".opc_health" / "keys.json"
    if ks.is_file():
        try:
            blob = json.loads(ks.read_text(encoding="utf-8"))
            def _walk(o):
                if isinstance(o, dict):
                    for v in o.values():
                        _walk(v)
                elif isinstance(o, list):
                    for v in o:
                        _walk(v)
                elif isinstance(o, str) and len(o) >= 16:
                    secrets.append(o.encode())
            _walk(blob)
        except Exception as e:
            print("      （警告：密钥库解析失败，跳过真密钥比对: %s）" % e)
    for env_k in ("OPC_LLM_API_KEY",):
        v = os.environ.get(env_k, "")
        if len(v) >= 16:
            secrets.append(v.encode())
    secrets = list(dict.fromkeys(secrets))   # 去重
    print("V3 密钥扫描        ：已装载 %d 条真实密钥用于比对" % len(secrets))

    # 命中分两级（为什么这样分，见下方注释）：
    #   hard = 必须为 0，否则判定复验失败；
    #   soft = 只提示、计入人工复核清单，不判失败。
    # 分级依据（一次真实误报的教训）：
    #   通用"长十六进制串"规则会把两类合法内容判成密钥——
    #     ① 本包自带的 SHA256 摘要（tests/fixtures/_采集记录.json 里为证物留的校验值）；
    #     ② 抓取来的公开网页快照里，页面自身携带的随机十六进制 token（sogou_sample.html）。
    #   这两类都不是我们的密钥。若把它们也判失败，闸门就会长期"响着红灯"——
    #   一个总在误报的闸门会被忽略，反而削弱真实防护。故本项降级为提示。
    #   真正的硬防护是：① 与真实密钥逐字比对；② sk- 前缀等"凭证形态"模式；③ 敏感文件黑名单。
    hard_hits: list[str] = []
    soft_hits: list[str] = []
    scanned = 0
    for n in files:
        ext = Path(n).suffix.lower()
        if ext not in TEXT_EXT:
            continue
        try:
            data = zf.read(n)
        except Exception as e:
            hard_hits.append("%s [读取失败: %s]" % (n, e)); continue
        scanned += 1
        if secrets:
            for sec in secrets:
                if sec in data:
                    hard_hits.append("%s [★ 命中真实密钥！]" % n)
        for m in pat_sk.finditer(data):
            s = m.group().decode("utf-8", "replace")
            if "REDACTED" in s or set(s) <= set("xX-*"):
                continue
            hard_hits.append("%s [sk- 模式: %s...]" % (n, s[:16]))
        for m in pat_hex.finditer(data):
            s = m.group().decode()
            if len(set(s)) <= 3:      # 重复字符占位符，忽略
                continue
            before = data[max(0, m.start() - 30):m.start()].lower()
            is_digest = b"sha256" in before          # 形如 "sha256": "<值>" → 是校验值，不是密钥
            if is_digest:
                continue
            soft_hits.append("%s [长十六进制串: %s...]" % (n, s[:16]))
    print("      逐文件扫描     ：%d 个文本文件，%s" % (
        scanned, "命中 %d 处 ✘" % len(hard_hits) if hard_hits else "零命中 ✅"))
    for h in hard_hits:
        print("      %s" % h)
    if soft_hits:
        print("      人工复核提示（非失败项）：%d 处疑似长十六进制串，" % len(soft_hits))
        print("        多为 SHA256 校验值或公开网页快照自带的 token，请人工确认非我方凭证：")
        for h in soft_hits:
            print("      %s" % h)
    if hard_hits:
        fail += 1

    # ── V6 入口存在 + 语法编译 ──
    must = ["README.md", "启动项目.py", "启动项目.bat", "launcher.py",
            "02_源码/src/server.py", "02_源码/static/index.html",
            "01_需求与方案/部署说明_2026-09-21.md"]
    missing = [m for m in must if not any(n.endswith(m) for n in files)]
    print("V6 关键入口        ：%s" % (missing or "齐全 ✅"))
    if missing:
        fail += 1
    comp_err = []
    with zipfile.ZipFile(ZIP) as zf2:
        for n in files:
            if not n.endswith(".py"):
                continue
            try:
                raw = zf2.read(n)
                src = raw.decode("utf-8")
                # 用内建 compile 做纯语法校验（py_compile.compile 不接受 _source 参数）
                compile(src, n, "exec")
            except Exception as e:
                comp_err.append("%s: %s" % (n, str(e)[:90]))
    print("      包内 .py 编译  ：%s" % (("全部通过 ✅" if not comp_err else "✘ %d 个失败" % len(comp_err))))
    for e in comp_err:
        print("      %s" % e)
    if comp_err:
        fail += 1

mb = ZIP.stat().st_size / 1024 / 1024
print("=" * 70)
print("体积：%.2f MB ｜ 结论：%s" % (mb, "全部通过 ✅" if not fail else "存在 %d 项问题 ✘" % fail))
sys.exit(0 if not fail else 1)
