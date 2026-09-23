# -*- coding: utf-8 -*-
"""真实性取证（**实网**测试；需真实模型密钥 + 可联网，故不并入离线回归）。

赛题 DoD 要求「真实性证据：≥1 成功检索 + ≥1 失败处理」。本脚本就是取证器：

    E1 成功检索取证：真实调用免密钥检索，打印真实来源标题/URL/日期摘要；
    E2 失败处理取证：把检索后端置为不可用 → 必须「诚实降级、零编造」（比赛红线）；
    E3 留痕对账（R8）：模型声称有结果时，检索留痕必须真有命中。

运行：
    python tests/test_authenticity.py
退出码 0 = 全部通过；1 = 有项不通过（例如出现编造）。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config, llm_client, orchestrator, session  # noqa: E402

HONESTY = ("未查到", "暂未查到", "待核实", "尚待核实", "以官方渠道为准", "无法核验")

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print("[%s] %s | %s" % ("PASS" if ok else "FAIL", name, detail))


def main() -> int:
    print("=" * 92)
    print("真实性取证（实网 + 真实模型）")
    print("跑测信息：检索后端=%s | 模型=%s | 密钥来源=%s" % (
        config.SEARCH_BACKENDS, llm_client.active_model(),
        llm_client.connection_info().get("source")))
    print("=" * 92)

    if not llm_client.is_ready():
        print("!! 未配置模型密钥，无法取证。请先在网页「添加密钥」。")
        return 1

    # ── E1 成功检索取证 ──
    print("\n── E1 成功检索取证（真实免密钥检索）──")
    q = "昆明 抗蛇毒血清 医院"
    hits = __import__("src.search", fromlist=["web_search"]).web_search(q, "昆明", max_results=5)
    report("E1 检索有真实返回", len(hits) > 0, "查询「%s」命中 %d 条" % (q, len(hits)))
    for i, h in enumerate(hits[:5], 1):
        print("   [%d] %s" % (i, h["title"][:60]))
        print("       来源: %s" % h["url"][:100])
        if h.get("snippet"):
            print("       摘要: %s" % h["snippet"][:80])
    real_urls = [h["url"] for h in hits if str(h["url"]).startswith("http")]
    report("E1 来源为真实 http 链接", len(real_urls) == len(hits),
           "真实链接 %d / %d 条" % (len(real_urls), len(hits)))
    print("   原始首条 JSON：%s" % json.dumps(hits[0], ensure_ascii=False)[:220] if hits else "   （无）")

    # ── E3 留痕对账（真实问答，看 retrieval_log）──
    print("\n── E3 检索留痕对账（真实问答）──")
    sid = session.session_store.new_id()
    env = orchestrator.run_turn("昆明哪家医院有抗蛇毒血清？", sid)
    log_line = [(r.action, r.status, r.hit_count) for r in env.retrieval_log]
    print("   ok=%s mode=%s reflection_count=%s" % (env.ok, env.mode, env.reflection_count))
    print("   retrieval_log=%s" % log_line)
    claimed = bool(env.output.query_results or env.output.info_basis)
    hit = any(r.action == "search" and (r.hit_count or 0) > 0 for r in env.retrieval_log)
    report("E3 声称有结果 ⇔ 留痕有命中", (not claimed) or hit,
           "声称有结果=%s / 留痕命中=%s" % (claimed, hit))

    # ── E2 失败处理取证（强制检索不可用）──
    print("\n── E2 失败处理取证（把检索后端置为不可用）──")
    saved = list(config.SEARCH_BACKENDS)
    config.SEARCH_BACKENDS = ["__offline_for_test__"]  # 走真实 web_search 代码路径 → 必然空
    try:
        sid2 = session.session_store.new_id()
        env2 = orchestrator.run_turn("昆明哪家医院有抗蛇毒血清？", sid2)
        tips = " ".join(env2.output.usage_tips or [])
        n_res = len(env2.output.query_results)
        n_basis = len(env2.output.info_basis)
        log2 = [(r.action, r.status, r.hit_count) for r in env2.retrieval_log]
        print("   ok=%s mode=%s degraded=%s" % (env2.ok, env2.mode, env2.degraded))
        print("   retrieval_log=%s" % log2)
        print("   结果条数=%d 依据条数=%d" % (n_res, n_basis))
        print("   usage_tips=%s" % json.dumps(env2.output.usage_tips, ensure_ascii=False))
        report("E2 零编造（结果/依据皆空）", n_res == 0 and n_basis == 0,
               "结果=%d 依据=%d（红线：不得编造）" % (n_res, n_basis))
        report("E2 如实告知（含诚实标志）", any(m in tips for m in HONESTY),
               "命中诚实标志=%s" % [m for m in HONESTY if m in tips])
    finally:
        config.SEARCH_BACKENDS = saved

    print("\n" + "-" * 92)
    passed = sum(1 for _, ok, _ in results if ok)
    print("===== 真实性取证：通过 %d / 失败 %d =====" % (passed, len(results) - passed))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
