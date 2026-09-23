# -*- coding: utf-8 -*-
"""间接提示注入防御 + 边界词多语言兜底 的回归测试（离线、无网络、无 openai 依赖）。

本套验证「参赛复核修复」的两块硬逻辑：
  · R13 来源可追溯：模型引用的来源链接必须出自本轮检索命中，引用检索外链接即判违规
    （间接提示注入的核心防线：网页内容诱导模型伪造/替换来源时，R13 在代码层打回）；
  · R4/R5/R9/R10 边界词补英文集：用户用英文提问时，代码层边界判定也要生效。

运行：激活 venv 后 python tests/test_injection_defense.py
"""
import sys
from pathlib import Path

BASE = str(Path(__file__).resolve().parent.parent)   # tests/ 的上一级 = 02_源码/
sys.path.insert(0, BASE)

import src.guardrails as G  # noqa: E402
from src.schema import (  # noqa: E402
    InfoBasis,
    OutputContract,
    QueryCondition,
    QueryResult,
    RetrievalLogEntry,
    Source,
)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"[PASS] {name} {detail}")
    else:
        failed += 1
        print(f"[FAIL] {name} {detail}")


def mk_result(name="云南省第一人民医院", desc="公开页面介绍设有心血管内科",
              url="https://example.com/a", info_status=None, source_type="权威", campus=None):
    return QueryResult(
        name=name, type="医院", campus=campus, description=desc, info_status=info_status,
        source=Source(title="医院官网", url=url, source_type=source_type, updated_note="来源未标注更新时间"),
    )


def mk_output(results=None, basis=None, tips=None, qc=None):
    return OutputContract(
        query_condition=qc if qc is not None else QueryCondition(region="昆明"),
        query_results=results if results is not None else [],
        info_basis=basis if basis is not None else [],
        usage_tips=tips if tips is not None else ["请以医院官方渠道为准。"],
    )


def hit_log(urls, q="昆明 三甲医院"):
    return [RetrievalLogEntry(action="search", query=q, city="昆明", status="ok",
                              hit_count=len(urls), urls=list(urls))]


def rules_of(output, msg, log):
    r = G.validate_output(output, log, msg)
    return r.rules, r


print("────────── 一、R13 来源可追溯（间接提示注入核心防线） ──────────")

# P1 伪造来源（引用检索外的链接）→ 判 R13
retrieved = ["https://www.example.com/a", "https://news.qq.com/rain/a/123"]
out = mk_output(results=[mk_result(url="https://evil.example/x")], tips=["x"])
rules, _ = rules_of(out, "昆明哪家医院有心内科", hit_log(retrieved))
check("P1 伪造来源→R13", "R13" in rules, f"命中:{rules}")

# P2 引用检索内链接（精确）→ 不报 R13
out = mk_output(results=[mk_result(url="https://www.example.com/a")], tips=["x"])
rules, _ = rules_of(out, "昆明哪家医院有心内科", hit_log(retrieved))
check("P2 引用检索内链接→不误报", "R13" not in rules, f"命中:{rules}")

# P3 归一化容差：引用时省略 www / 改用 http → 视为同一来源，不误报
out = mk_output(results=[mk_result(url="http://example.com/a")], tips=["x"])
rules, _ = rules_of(out, "昆明哪家医院有心内科", hit_log(retrieved))
check("P3 归一化容差(去www/http)→不误报", "R13" not in rules, f"命中:{rules}")

# P4 fail-open：检索留痕未携 url（离线/缓存恢复）→ 不校验，避免误伤
out = mk_output(results=[mk_result(url="https://evil.example/x")], tips=["x"])
rules, _ = rules_of(out, "昆明哪家医院有心内科",
                    [RetrievalLogEntry(action="search", query="q", city="昆明", status="ok", hit_count=2)])
check("P4 留痕无url→fail-open不误报", "R13" not in rules, f"命中:{rules}")

# P5 多结果其一伪造 → 仅该条 R13（其余正常）
out = mk_output(results=[
    mk_result(name="甲医院", url="https://www.example.com/a"),
    mk_result(name="乙医院", url="https://evil.example/x"),
], tips=["x"])
rules, res = rules_of(out, "昆明三甲医院", hit_log(retrieved))
check("P5 部分伪造→R13命中", "R13" in rules, f"命中:{rules}")

# P6 多轮复用：known_urls（上一轮真实来源）里的链接，本轮无检索也应放行（不过杀）
known = {"https://www.example.com/a"}
out = mk_output(results=[mk_result(url="https://www.example.com/a")], tips=["x"])
rules = G.validate_output(out, [], "昆明哪家医院有心内科", known_urls=known).rules
check("P6 多轮复用已知来源→不误杀R13", "R13" not in rules, f"命中:{rules}")

print("────────── 二、边界词多语言兜底（英文提问也要触发代码层边界） ──────────")

# E1 R5 英文紧急词：chest pain 且无 120 → R5
out = mk_output(results=[mk_result(url="https://www.example.com/a")], tips=["建议尽快就诊"])
rules, _ = rules_of(out, "I have chest pain what should I do", hit_log(retrieved))
check("E1 英文chest pain→R5", "R5" in rules, f"命中:{rules}")

# E2 R5 正向：含 120 → 不报
out = mk_output(results=[mk_result(url="https://www.example.com/a")],
                tips=["请立即拨打 120 急救，并尽快到急诊。"])
rules, _ = rules_of(out, "I have chest pain what should I do", hit_log(retrieved))
check("E2 英文含120→不误报", "R5" not in rules, f"命中:{rules}")

# E3 R4 英文诊断意图：prescribe medicine → R4
out = mk_output(results=[mk_result(url="https://www.example.com/a")], tips=["祝您健康"])
rules, _ = rules_of(out, "I have a headache what medicine should I take", hit_log(retrieved))
check("E3 英文prescribe→R4", "R4" in rules, f"命中:{rules}")

# E4 R9 英文易变资源：antivenom → 要求三态标注（未标则 R9）
out = mk_output(results=[mk_result(name="抗蛇毒血清", url="https://www.example.com/a", info_status=None)],
                tips=["x"])
rules, _ = rules_of(out, "where can I get antivenom serum in Kunming", hit_log(retrieved))
check("E4 英文antivenom→R9", "R9" in rules, f"命中:{rules}")

# E5 R10 英文院区：which campus → 要求标院区（未标则 R10）
out = mk_output(results=[mk_result(name="某医院", url="https://www.example.com/a", campus=None)],
                tips=["x"])
rules, _ = rules_of(out, "which campus has the cardiology department", hit_log(retrieved))
check("E5 英文which campus→R10", "R10" in rules, f"命中:{rules}")

print("────────── 三、多语言无过杀（普通英文不应误判） ──────────")

# N1 普通英文寒暄/陈述：不触发 R4/R5
out = mk_output(results=[mk_result(url="https://www.example.com/a")], tips=["谢谢"])
rules, _ = rules_of(out, "Thank you very much for your help", hit_log(retrieved))
check("N1 英文感谢→不过杀R4/R5", "R4" not in rules and "R5" not in rules, f"命中:{rules}")

# N2 普通英文就医询问（非诊断）：不触发 R4
out = mk_output(results=[mk_result(url="https://www.example.com/a")], tips=["x"])
rules, _ = rules_of(out, "Where is the hospital and how to register", hit_log(retrieved))
check("N2 英文问路→不过杀R4", "R4" not in rules, f"命中:{rules}")

print(f"\n══════ 间接提示注入防御测试：通过 {passed} / 失败 {failed} ══════")
sys.exit(1 if failed else 0)
