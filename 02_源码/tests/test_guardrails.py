# -*- coding: utf-8 -*-
"""护栏层测试（Step 5）：Validator 规则 R1~R10 + 反射式打回循环（N=2）。

全部离线、无网络：
- Validator 用「手工构造的 4 段式」直接喂进校验器，逐条验证触发/不误报；
- 打回循环用「注入假模型」替换真实模型调用（llm_client.chat / orchestrator.web_search），
  从而确定性验证「打回 N 轮 → 通过 / 超限降级」两条路径。

运行：激活 venv 后 python tests/test_guardrails.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

# 项目内的 src 包位置：由本文件位置推导，绝不写死绝对路径
# （交付包会被评审解压到任意目录，写死路径会导致测试直接崩，或更糟——静默测到别的目录的源码）
BASE = str(Path(__file__).resolve().parent.parent)   # tests/ 的上一级 = 02_源码/
sys.path.insert(0, BASE)

import src.guardrails as G  # noqa: E402
import src.llm_client as L  # noqa: E402
import src.orchestrator as O  # noqa: E402
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


def mk_result(name="云南省第一人民医院", desc="公开页面介绍设有心血管内科", url="https://example.com/a",
              info_status=None, source_type="权威", campus=None):
    return QueryResult(
        name=name, type="医院", campus=campus, description=desc, info_status=info_status,
        source=Source(title="医院官网", url=url, source_type=source_type, updated_note="来源未标注更新时间"),
    )


_UNSET = object()


def mk_output(results=None, basis=None, tips=None, qc=_UNSET):
    return OutputContract(
        query_condition=QueryCondition(region="昆明") if qc is _UNSET else qc,
        query_results=results if results is not None else [],
        info_basis=basis if basis is not None else [],
        usage_tips=tips if tips is not None else ["请以医院官方渠道为准。"],
    )


def hit_log(n=1, q="昆明 三甲医院"):
    return [RetrievalLogEntry(action="search", query=q, city="昆明", status="ok", hit_count=n)]


def rules_of(output, msg, log):
    r = G.validate_output(output, log, msg)
    return r.rules, r


print("────────── 一、Validator 规则 R1~R10（离线） ──────────")

# U1 R1 结构完备：空壳（无查询条件、无提示、无结果）
_r1, _res1 = rules_of(mk_output(tips=[], qc=None), "昆明哪家医院有心内科", [])
check("U1 R1-空壳", "R1" in _r1, f"命中:{_res1.summary()}")

# U2 R2 来源必填：url 非法
rules, res = rules_of(mk_output(results=[mk_result(url="")], tips=["x"]), "昆明哪家医院有心内科", hit_log())
check("U2 R2-无来源链接", "R2" in rules, rules)

# U3 R6 禁断言
bad = mk_output(results=[mk_result(desc="该院目前可提供该资源")], tips=["x"])
rules, res = rules_of(bad, "昆明哪家医院有抗蛇毒血清", hit_log())
check("U3 R6-禁断言", "R6" in rules, f"命中:{res.summary()}")

# U4 R7 空结果未诚实标注
rules, res = rules_of(mk_output(results=[], basis=[], tips=["欢迎咨询"]), "昆明哪家医院有抗蛇毒血清", hit_log(0))
check("U4 R7-空结果不诚实", "R7" in rules, f"命中:{res.summary()}")

# U5 R7 空结果 + 诚实标注 → 通过
ok_empty = mk_output(results=[], basis=[], tips=["暂未查到可核验的联网信息，请以医院官方渠道为准。"])
rules, res = rules_of(ok_empty, "昆明哪家医院有抗蛇毒血清", hit_log(0))
check("U5 R7-诚实降级通过", res.passed and not rules, f"命中:{res.summary()}")

# U6 R8 检索真实性：有结果但检索零命中
rules, res = rules_of(mk_output(results=[mk_result()], tips=["x"]), "昆明哪家医院有心内科", [])
check("U6 R8-冒充检索", "R8" in rules, f"命中:{res.summary()}")

# U7 R8 反向：有真实命中 → 不报 R8
rules, res = rules_of(mk_output(results=[mk_result()], tips=["x"]), "昆明哪家医院有心内科", hit_log(2))
check("U7 R8-有命中不误报", "R8" not in rules, f"命中:{res.summary()}")

# U8 R5 紧急优先：胸痛但无 120
rules, res = rules_of(mk_output(results=[mk_result()], tips=["建议尽快就诊"]), "我胸痛得厉害怎么办", hit_log())
check("U8 R5-紧急无120", "R5" in rules, f"命中:{res.summary()}")

# U9 R5 正向：含 120 → 不报
rules, res = rules_of(mk_output(results=[mk_result()], tips=["请立即拨打 120 急救，并尽快到急诊。"]),
                      "我胸痛得厉害怎么办", hit_log())
check("U9 R5-含120不误报", "R5" not in rules, f"命中:{res.summary()}")

# U10 R4 边界拒答：用药意图但无拒答话术
rules, res = rules_of(mk_output(results=[mk_result()], tips=["祝您健康"]), "我头疼吃什么药", hit_log())
check("U10 R4-用药无拒答", "R4" in rules, f"命中:{res.summary()}")

# U11 R4 越界：直接给用药建议
rules, res = rules_of(mk_output(results=[mk_result(desc="建议服用布洛芬缓解")], tips=["请遵医嘱"]),
                      "我头疼吃什么药", hit_log())
check("U11 R4-给出用药建议", "R4" in rules, f"命中:{res.summary()}")

# U12 R4 正向：拒答 + 引导就医 → 不报
rules, res = rules_of(mk_output(results=[mk_result()], tips=["不提供诊断与用药建议，请及时就医，由医生判断。"]),
                      "我头疼吃什么药", hit_log())
check("U12 R4-正确拒答不误报", "R4" not in rules, f"命中:{res.summary()}")

# U13 全合规 → passed
good = mk_output(
    results=[mk_result(info_status="公开页面介绍具备相关能力")],
    basis=[InfoBasis(title="医院官网", url="https://example.com/a", note="来源未标注更新时间", source_type="权威")],
    tips=["信息可能更新，请以医院官方渠道为准。"],
)
rules, res = rules_of(good, "昆明哪家三甲医院有心血管内科", hit_log())
check("U13 全合规通过", res.passed, f"命中:{res.summary()}")

# U15 边界拒答（空结果）不应被判 R7——实测真实模型踩过的假阳性
refusal_empty = mk_output(results=[], basis=[], tips=["不提供诊断与用药建议，请及时就医，由医生判断。"])
rules, res = rules_of(refusal_empty, "我头疼得厉害该吃什么药", [])
check("U15 拒答空结果不误报R7", res.passed and "R7" not in rules, f"命中:{res.summary()}")

# U16 紧急急救指引（空结果、含120）不应被判 R7
emergency_empty = mk_output(results=[], basis=[], tips=["请立即拨打 120 或前往医院急诊就诊。"])
rules, res = rules_of(emergency_empty, "我父亲现在胸痛、出冷汗怎么办", [])
check("U16 急救指引空结果不误报R7", res.passed and "R7" not in rules, f"命中:{res.summary()}")

# U14 Reflector 话术含违规点与修正要求，且不含任何事实/答案
prompt = G.build_reflection_prompt(res if not res.passed else G.ValidationResult(False, [G.Violation("R2", "query_results[0].source.url", "缺链接")], [G.RULE_HINTS["R2"]]), 2)
check("U14 Reflector-话术结构", ("第 2 轮" in prompt) and ("R2" in prompt) and ("修正要求" in prompt)
      and ("example.com" not in prompt), prompt.splitlines()[0])

# ── R10 院区纪律 ──
# 依据赛题《基础需求2》「不同院区的地址、科室和医生安排不得混用」与《基础需求3》「标明适用院区」。
# 这四项成对覆盖「该报的报 / 不该报的不报」，防止新规则变成"总在误报的闸门"。
campus_case = "昆医大附一院呈贡院区的心血管内科怎么样"

rules, res = rules_of(mk_output(results=[mk_result()]), campus_case, hit_log())
check("U17 提到院区但结果未标院区 → 判 R10", "R10" in rules, f"命中:{res.summary()}")

rules, res = rules_of(mk_output(results=[mk_result(campus="呈贡院区")]), campus_case, hit_log())
check("U18 结果已填院区字段 → 不误报R10", "R10" not in rules, f"命中:{res.summary()}")

rules, res = rules_of(mk_output(results=[mk_result(desc="呈贡院区公开页面介绍设有心血管内科")]), campus_case, hit_log())
check("U19 院区写在说明里（未填字段）→ 不误报R10", "R10" not in rules, f"命中:{res.summary()}")

rules, res = rules_of(mk_output(results=[mk_result()]), "昆明哪家三甲医院有心血管内科", hit_log())
check("U20 未提院区 → R10 不触发", "R10" not in rules, f"命中:{res.summary()}")

# R10 词表纪律：只放通用词，不得出现任何具体院区名（具体院区名属医院事实，代码禁止硬编码）
check("U21 R10 词表只含通用词（无具体院区名）",
      all(not w.startswith(("呈贡", "西昌", "华兴", "甘美")) for w in G.CAMPUS_WORDS),
      str(G.CAMPUS_WORDS))


# ── R11 同名医院/院区自动消歧（进阶需求1） ──
# 多后端返回同名医院多家时，自动区分院区、不混用地址科室（与 R10 院区纪律呼应）。
# 这四项成对覆盖「正确消歧通过 / 漏标被拦 / 不同名不误报 / 单条不误报」，
# 防止 R11 变成"总在误报的闸门"（生产级稳定性的硬要求）。
_same_name_case = "昆明第一人民医院 心血管内科 哪家好"

# V1 同名组：两条都各自标了不同院区 → 正确消歧，通过（不误报）
_same_ok = mk_output(results=[
    mk_result(name="昆明第一人民医院", desc="本部公开页面介绍设有心血管内科", campus="本部院区"),
    mk_result(name="昆明第一人民医院", desc="北市区院区公开页面介绍设有心血管内科", campus="北市区院区"),
], tips=["同名医院已按院区分别列出，请以官方渠道为准。"])
rules, res = rules_of(_same_ok, _same_name_case, hit_log(2))
check("V1 同名两组各自标院区 → 通过", res.passed and "R11" not in rules, f"命中:{res.summary()}")

# V2 同名组：一条标了院区、另一条漏标 → R11 拦（漏标/混用风险）
_same_mix = mk_output(results=[
    mk_result(name="昆明第一人民医院", desc="本部公开页面介绍设有心血管内科", campus="本部院区"),
    mk_result(name="昆明第一人民医院", desc="北市区院区公开页面介绍设有心血管内科"),  # 漏标 campus
], tips=["x"])
rules, res = rules_of(_same_mix, _same_name_case, hit_log(2))
check("V2 同名组部分漏标 → 判 R11", "R11" in rules, f"命中:{res.summary()}")

# V3 不同医院（不同名）→ R11 不触发
_diff = mk_output(results=[
    mk_result(name="云南省第一人民医院", desc="设心血管内科"),
    mk_result(name="昆明医科大学第一附属医院", desc="设心血管内科"),
], tips=["x"])
rules, res = rules_of(_diff, _same_name_case, hit_log(2))
check("V3 不同名 → R11 不触发", "R11" not in rules, f"命中:{res.summary()}")

# V4 同名单条（无同名组）→ R11 不触发（避免对单条结果误伤降级）
_single = mk_output(results=[mk_result(name="昆明第一人民医院", desc="设心血管内科")], tips=["x"])
rules, res = rules_of(_single, _same_name_case, hit_log(2))
check("V4 同名单条 → R11 不触发", "R11" not in rules, f"命中:{res.summary()}")


# ── R12 多来源冲突主动提示（进阶需求1 联动 A3） ──
# 同一事实不同来源说法不一致时，主动标注"来源间存在冲突，以官方为准"，不得静默合并。
# 这四项成对覆盖「冲突已标注通过 / 冲突未标注被拦 / 同状态不误报 / 不同名不误报」，
# 防止 R12 变成"总在误报的闸门"。
_conflict_case = "昆明第一人民医院 抗蛇毒血清 有没有"

# C1 同名组信息状态发散 + 已标注冲突语 → 通过
_conf_ok = mk_output(results=[
    mk_result(name="昆明第一人民医院", desc="来源A：公开页面介绍具备相关能力", info_status="公开页面介绍具备相关能力", campus="本部院区"),
    mk_result(name="昆明第一人民医院", desc="来源B：目前是否可提供尚待核实", info_status="目前是否可提供尚待核实", campus="北市区院区"),
], tips=["来源间存在冲突，请以官方渠道为准。"])
rules, res = rules_of(_conf_ok, _conflict_case, hit_log(2))
check("C1 冲突已标注 → 通过", res.passed and "R12" not in rules, f"命中:{res.summary()}")

# C2 同名组信息状态发散 + 未标注冲突 → R12 拦
_conf_bad = mk_output(results=[
    mk_result(name="昆明第一人民医院", desc="来源A：公开页面介绍具备相关能力", info_status="公开页面介绍具备相关能力", campus="本部院区"),
    mk_result(name="昆明第一人民医院", desc="来源B：目前是否可提供尚待核实", info_status="目前是否可提供尚待核实", campus="北市区院区"),
], tips=["请参考上述来源。"])
rules, res = rules_of(_conf_bad, _conflict_case, hit_log(2))
check("C2 冲突未标注 → 判 R12", "R12" in rules, f"命中:{res.summary()}")

# C3 同名组但信息状态一致（无发散）→ R12 不触发
_conf_same = mk_output(results=[
    mk_result(name="昆明第一人民医院", desc="来源A：公开页面介绍具备相关能力", info_status="公开页面介绍具备相关能力"),
    mk_result(name="昆明第一人民医院", desc="来源B：公开页面介绍具备相关能力", info_status="公开页面介绍具备相关能力"),
], tips=["x"])
rules, res = rules_of(_conf_same, _conflict_case, hit_log(2))
check("C3 同状态无发散 → R12 不触发", "R12" not in rules, f"命中:{res.summary()}")

# C4 不同名（不同医院）信息状态发散 → R12 不触发（无同名组）
_conf_diff = mk_output(results=[
    mk_result(name="云南省第一人民医院", desc="来源A：公开页面介绍具备相关能力", info_status="公开页面介绍具备相关能力"),
    mk_result(name="昆明医科大学第一附属医院", desc="来源B：目前是否可提供尚待核实", info_status="目前是否可提供尚待核实"),
], tips=["x"])
rules, res = rules_of(_conf_diff, _conflict_case, hit_log(2))
check("C4 不同名 → R12 不触发", "R12" not in rules, f"命中:{res.summary()}")


print("\n────────── 二、反射式打回循环 N=2（注入假 client，离线） ──────────")

VIOLATING_R8 = (
    '{"query_condition":{"region":"昆明","department":"心血管内科"},'
    '"query_results":[{"name":"云南省第一人民医院","type":"医院","description":"设有心血管内科",'
    '"source":{"title":"医院官网","url":"https://example.com/a"}}],'
    '"info_basis":[],"usage_tips":["请以官方渠道为准。"]}'
)
VIOLATING_R6 = (
    '{"query_condition":{"region":"昆明"},'
    '"query_results":[{"name":"云南省第一人民医院","type":"医院","description":"该院目前可提供抗蛇毒血清",'
    '"source":{"title":"医院官网","url":"https://example.com/a"}}],'
    '"info_basis":[],"usage_tips":["请以官方渠道为准。"]}'
)
VALID = (
    '{"query_condition":{"region":"昆明","department":"心血管内科"},'
    '"query_results":[{"name":"云南省第一人民医院","type":"医院","description":"公开页面介绍设有心血管内科",'
    '"source":{"title":"医院官网","url":"https://example.com/a","updated_note":"来源未标注更新时间"}}],'
    '"info_basis":[{"title":"医院官网","url":"https://example.com/a","note":"来源未标注更新时间","source_type":"权威"}],'
    '"usage_tips":["信息可能更新，请以医院官方渠道为准。"]}'
)


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, id_, name, arguments):
        self.id = id_
        self.function = _Fn(name, arguments)


class _Completions:
    def __init__(self, script):
        self._script = list(script)

    def create(self, **kwargs):
        msg = self._script.pop(0) if self._script else _Msg(content="{}")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


class _FakeClient:
    def __init__(self, script):
        self.chat = SimpleNamespace(completions=_Completions(script))


_FAKE_HITS = [{"title": "云南省第一人民医院官网", "url": "https://example.com/a", "snippet": "心血管内科"}]


def run_with(script, message="昆明哪家三甲医院有心血管内科？", hits=None):
    """注入假模型 + 假搜索，跑一轮 run_turn。

    注意（Step 6 重构后的接口变化）：
        编排层已统一改经 ``llm_client.chat(model, messages, **kwargs)`` 调模型，
        ``orchestrator.build_client`` 已不存在。故这里替换的是 ``llm_client.chat``，
        并按同样签名记账，避免"测试引用了已被删掉的函数"这种静默失效。
    """
    queue = list(script)
    real_chat = L.chat
    real_ready = L.is_ready
    real_model = L.active_model
    real_search = O.web_search

    def _fake_chat(model, messages, **kwargs):
        msg = queue.pop(0) if queue else _Msg(content="{}")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

    L.chat = _fake_chat
    L.is_ready = lambda: True
    L.active_model = lambda: "fake-model"
    O.web_search = (lambda *a, **k: list(hits)) if hits is not None else (lambda *a, **k: [])
    try:
        return O.run_turn(message, "guard_test_sid")
    finally:
        L.chat, L.is_ready, L.active_model = real_chat, real_ready, real_model
        O.web_search = real_search


# N1 模型始终违规 → 打回满 N=2 轮后降级交付
env = run_with([
    _Msg(tool_calls=None),
    _Msg(content=VIOLATING_R8),
    _Msg(content=VIOLATING_R8),
    _Msg(content=VIOLATING_R8),
])
guard_entries = [e for e in env.retrieval_log if e.action == "guardrail"]
check("N1 打回N=2", env.reflection_count == 2, f"reflection_count={env.reflection_count}")
check("N1 超限降级", env.degraded is True and env.ok is True, f"degraded={env.degraded} note={env.note}")
check("N1 留痕可观测", len(guard_entries) == 3 and guard_entries[-1].status == "degraded",
      f"guardrail条目={[e.status for e in guard_entries]}")
check("N1 降级仍带提示", any("未完全满足" in t for t in env.output.usage_tips), env.output.usage_tips[-1:])

# N2 打回 1 轮后模型改好 → 通过、不降级
env2 = run_with([
    _Msg(tool_calls=[_ToolCall("call_1", "web_search", '{"query":"昆明 三甲医院 心血管内科","city":"昆明"}')]),
    _Msg(content=VIOLATING_R6),
    _Msg(content=VALID),
], hits=_FAKE_HITS)
check("N2 打回1轮后通过", env2.reflection_count == 1 and env2.degraded is False and env2.ok is True,
      f"reflection_count={env2.reflection_count} note={env2.note}")

# N3 一次通过 → 0 打回、0 降级
env3 = run_with([
    _Msg(tool_calls=[_ToolCall("call_1", "web_search", '{"query":"昆明 三甲医院 心血管内科","city":"昆明"}')]),
    _Msg(content=VALID),
], hits=_FAKE_HITS)
check("N3 一次通过", env3.reflection_count == 0 and env3.degraded is False, f"note={env3.note}")

# N4 检索为空 + 域内提问 → 模型诚实回复（空结果）通过，且未编造
HONEST_EMPTY = ('{"query_condition":{"region":"昆明"},'
                '"query_results":[],"info_basis":[],'
                '"usage_tips":["暂未查到可核验的联网信息，请以医院官方渠道为准。"]}')
env4 = run_with([_Msg(tool_calls=None), _Msg(content=HONEST_EMPTY)], hits=[])
check("N4 诚实降级通过", env4.ok is True and env4.degraded is False and env4.output.query_results == [],
      f"note={env4.note}")

# N5 检索为空但模型编造结果 → R8 拦截并打回
env5 = run_with([_Msg(tool_calls=None), _Msg(content=VIOLATING_R8), _Msg(content=VIOLATING_R8),
                 _Msg(content=VIOLATING_R8)], hits=[])
check("N5 编造被R8拦截", env5.reflection_count == 2 and env5.degraded is True,
      f"reflection_count={env5.reflection_count}")

# N6 解析失败也纳入打回循环
env6 = run_with([_Msg(tool_calls=None), _Msg(content="这不是JSON"), _Msg(content=HONEST_EMPTY)], hits=[])
check("N6 解析失败可打回", env6.reflection_count == 1 and env6.ok is True, f"note={env6.note}")

print(f"\n===== 护栏层测试：通过 {passed} / 失败 {failed} =====")
if failed:
    raise SystemExit(1)
